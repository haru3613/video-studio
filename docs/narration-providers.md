# Narration providers

The normal production path is self-supplied audio plus subtitles. It requires
no TTS provider, account, credentials, or spend journal. Imported material must
still pass the project's audio, subtitle, and alignment gates; it is not a
synthetic take and does not imply pronunciation approval.

Start with the portable-tools setup in the [media-tools README](../media-tools/README.md).
For an automated pronunciation workflow or a demo that deliberately synthesizes
audio, set `VIDEO_STUDIO_TTS_PROVIDER` in that process's operator environment;
the workflow passes only allowlisted settings to its child. A direct generator
invocation may instead use `--provider NAME`.

Provider-backed narration is optional. Select it explicitly with `--provider`
or `VIDEO_STUDIO_TTS_PROVIDER`; there is no default provider, voice, or formal
narration style. The included `elevenlabs` adapter remains available for an
operator who supplies its own voice, credentials, and budget. A mechanical demo
voice is not a substitute for approved narration.

## Running a provider

Every new synthetic take needs a selected provider, voice, and a positive
`--max-credits` or `VIDEO_STUDIO_TTS_MAX_CREDITS`. The journal and cache are
shared across sectioned narration, so an unchanged take is reused without a
provider call. No CI or offline test may make a network or paid provider call.

If a submission crosses the provider boundary but its outcome is unknown, it
must remain `submission_unknown`. Do not retry it. Reconcile only with the same
provider's durable request ID and matching provider history; otherwise start a
separate explicit retake after operator review. See
[`SPEND_JOURNAL.md`](../media-tools/narration/SPEND_JOURNAL.md).

## Adding an installed provider

Providers are installed Python packages registered through the
`video_studio.tts_providers` entry-point group. The entry-point name is the
operator-facing provider name and must be a finite lower-case name containing
only letters, digits, `_`, and `-`. The runner never accepts a module path,
file path, or executable from a request. A provider factory must return a
`TTSProvider` whose `name` matches its entry-point name.

Implement `credits_for()` and `synthesize()` from
`media-tools/narration/providers/base.py`. `synthesize()` must return audio,
alignment units (or explicitly declare `none`), and a durable provider request
ID in metadata for every accepted billable request. Preserve the caller's
alignment contract: subtitle timing is validated against the resulting audio,
and provider timing is only evidence to measure, never an automatic approval.

To support unknown-submission reconciliation, also implement read-only
`history_records([request_id])` that proves the exact request ID, text, voice,
model, and actual cost. Sectioned narration can additionally use optional
`history_costs([request_id])`; its absence only prevents an exact history-cost
lookup and leaves the journal protections in force. Providers must classify
timeouts, I/O failures, malformed responses, and ambiguous acceptance as
`ProviderSubmissionUnknown`; only a proven pre-charge rejection may raise
`ProviderConfirmedFailure`. Never retry unknown submissions inside a provider.

The provider owns its credential loading and any external request. It must not
read arbitrary caller paths or execute caller-supplied commands. Keep credits,
estimates, provider-reported cost, and reconciliation evidence in the existing
spend journal; do not record credentials or narration text there.
