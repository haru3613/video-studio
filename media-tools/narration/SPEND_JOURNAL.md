# TTS spend journal

The synchronous narration adapter uses a local SQLite journal before every
billable provider call. This closes the accepted-but-timeout gap where a process
could resend the same text after the provider charged it but before a take
receipt reached disk.

## Provenance

The state semantics are a Python adaptation of this repository's durable
provider executor:

- source commit reviewed: `c67feba`
- source: `pipeline/src/provider.rs`
- source tests: `pipeline/tests/provider_execution.rs`
- relevant states: prepared, submitted, submission_unknown, failed, succeeded
- relevant invariant: an unknown submission is durable and cannot be submitted
  again under the same idempotency key

The Python adapter adds a synchronous `submitting` boundary because ElevenLabs
returns media in the submission response rather than returning a job handle
first. No provider SDK code or private account data was copied.

## State machine

```text
prepared
  -> submitting
       -> confirmed_failed       proven local/HTTP pre-charge rejection
       -> submission_unknown     timeout, I/O error, 5xx, malformed response,
                                  process restart at submit boundary
       -> submitted              response has durable provider request ID
            -> succeeded         audio, SRT, take receipt, journal all bound
            -> reconciled_spent  provider accepted but local output failed
submission_unknown
  -> reconciled_spent            provider history proves request ID and cost
```

`prepared`, `submitting`, `submission_unknown`, and `submitted` reserve
their estimated credits. `succeeded` and `reconciled_spent` consume actual
credits when the provider reports them. Only `confirmed_failed` releases a
reservation, and it requires a stored proof such as
`provider_http_rejection:400` or a local credential preflight result.

The confirmed no-charge HTTP policy is deliberately narrow:
`400, 401, 402, 403, 404, 413, 422, 429`. These responses reject a request
before synthesis in the adapter contract. Other 4xx responses, including
timeout-like `408` and state-conflict `409`, remain
`submission_unknown`; a generic 4xx class is not enough proof to release.

A process-level file lock spans reservation, provider submission, and receipt
promotion. SQLite `BEGIN IMMEDIATE` transactions make the cap check and
reservation atomic. Concurrent processes therefore cannot both cross a shared
cap. The database and lock are current-user files with mode 0600. The journal
stores request digests, state, cost, output path, and provider request IDs; it
does not store narration text or credentials.

## Idempotency

The base key hashes every provider-affecting value:

- processed text
- provider, voice, model, stability, and speed
- stream mode
- previous/next request IDs and text
- seed

An ordinary rerun uses the completed cache and consumes zero new credits. An
explicit `--retake` creates another numbered spend attempt and consumes a
separate budget reservation. Neither `--retake` nor `--force-budget` can
bypass `submission_unknown`.

`--force-budget` remains parseable because the existing MCP parent passes it
as an approval marker. It never disables the cap. Every paid call requires
`--max-credits` or `VIDEO_STUDIO_TTS_MAX_CREDITS`; when both exist, the
lower value wins. The shared journal applies that cap across every section and
retake.

Existing completed take receipts from before the journal are imported without a
provider call when their audio, SRT, hash, voice, model, and provider request ID
are present.

## Reconciliation

If submission is unknown, use the selected provider's read-only history to find
the request and run the same generator command with:

```sh
--reconcile-request-id REQUEST_ID
```

The adapter queries provider history read-only. It changes the state to
`reconciled_spent` only when history returns the exact request ID, official
credit delta, submitted text, voice ID, and model ID, and all request fields
match the blocked attempt. An unrelated charged request cannot reconcile it.
Absence from the bounded history response is not proof of failure; the attempt
remains blocked. A new paid call then requires explicit `--retake`.

The journal defaults beside the output base as
`.video-studio-tts-spend.sqlite3`. Sectioned narration passes one shared
journal to every section. Use `--spend-journal` or
`VIDEO_STUDIO_TTS_SPEND_JOURNAL` to set an explicit location.
