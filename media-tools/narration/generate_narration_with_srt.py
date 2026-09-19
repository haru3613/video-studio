#!/usr/bin/env python3
"""Generate timestamped narration audio and SRT subtitles.

The command is provider-backed, but all identity, credentials, budget and voice
configuration belongs to the operator. There are no bundled voices, API keys,
account paths, pronunciation tables or spending limits.

Required for a new ElevenLabs take:
  * --voice or VIDEO_STUDIO_TTS_VOICE_ID
  * ELEVENLABS_API_KEY or ELEVENLABS_API_KEY_PATH
  * --max-credits or VIDEO_STUDIO_TTS_MAX_CREDITS

Unchanged takes are reused without contacting the provider. Every paid attempt
is reserved in a durable local journal before submission. An ambiguous timeout
blocks all retries until --reconcile-request-id proves the charge in provider
history. --force-budget is accepted for legacy parent compatibility but never
removes the configured cap or bypasses an unknown submission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from providers import (
    AlignmentUnit,
    ProviderConfirmedFailure,
    ProviderError,
    ProviderSubmissionUnknown,
    get_provider,
)
from spend_journal import (
    BudgetExceeded,
    JournalError,
    SpendJournal,
    SubmissionBlocked,
    payload_digest,
)
from zh_normalize import TAG_RE, normalize_zh

VOICE = os.getenv("VIDEO_STUDIO_TTS_VOICE_ID", "")
MODEL = os.getenv("VIDEO_STUDIO_TTS_MODEL", "eleven_v3")
DRAFT_MODEL = "eleven_flash_v2_5"
SPEED = 1.0
DEFAULT_STABILITY = 0.3
SENTENCE_END = "。！？!?…"
SOFT_BREAK = "，、,;；"
MAX_LINE = 20
OVERRIDES_SCHEMA = "haru.pronunciation_overrides.v1"

_ACTIVE_VOICE: str | None = None
_PROJECT_FIXES: dict[str, str] = {}


def _configured_voice_rules_dir() -> Path | None:
    value = os.getenv("VIDEO_STUDIO_VOICE_RULES_DIR")
    return Path(value).expanduser().resolve() if value else None


def load_voice_fixes(path: str, expected_voice: str | None = None) -> dict[str, str]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    rules = value.get("rules") if isinstance(value, dict) else None
    if not isinstance(value, dict) or not isinstance(rules, list):
        raise ValueError(f"invalid voice pronunciation rules file: {path}")
    if expected_voice is not None and value.get("voice") != expected_voice:
        raise ValueError(f"voice rules declare {value.get('voice')!r}, expected {expected_voice!r}")
    fixes: dict[str, str] = {}
    for item in rules:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("term"), str)
            or not item["term"]
            or not isinstance(item.get("spoken"), str)
            or not item["spoken"]
            or item["term"] in fixes
        ):
            raise ValueError(f"invalid voice pronunciation rule in {path}: {item!r}")
        fixes[item["term"]] = item["spoken"]
    return fixes


def _load_voice_fixes_table() -> dict[str, dict[str, str]]:
    root = _configured_voice_rules_dir()
    if root is None:
        return {}
    if not root.is_dir():
        raise ValueError(f"VIDEO_STUDIO_VOICE_RULES_DIR is not a directory: {root}")
    return {
        item.stem: load_voice_fixes(str(item), expected_voice=item.stem)
        for item in sorted(root.glob("*.json"))
    }


VOICE_FIXES = _load_voice_fixes_table()


def set_active_voice(voice: str) -> None:
    global _ACTIVE_VOICE
    _ACTIVE_VOICE = voice


def set_project_fixes(fixes: dict[str, str]) -> None:
    global _PROJECT_FIXES
    _PROJECT_FIXES = dict(fixes)


def load_project_fixes(path: str) -> dict[str, str]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    terms = value.get("terms") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("schema") != OVERRIDES_SCHEMA
        or not isinstance(terms, list)
    ):
        raise ValueError(f"invalid pronunciation overrides; expected {OVERRIDES_SCHEMA}")
    fixes: dict[str, str] = {}
    for item in terms:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("term"), str)
            or not item["term"]
            or not isinstance(item.get("spoken"), str)
            or not item["spoken"]
            or item["term"] in fixes
        ):
            raise ValueError("invalid pronunciation override entry")
        fixes[item["term"]] = item["spoken"]
    return fixes


def active_fixes() -> dict[str, str]:
    voice_fixes = VOICE_FIXES.get(_ACTIVE_VOICE, {}) if _ACTIVE_VOICE else {}
    return {**voice_fixes, **_PROJECT_FIXES}


def _apply_with_display(text: str, selected_fixes=None):
    fixes = sorted(
        (active_fixes() if selected_fixes is None else selected_fixes).items(),
        key=lambda item: -len(item[0]),
    )
    spoken: list[str] = []
    display: list[str] = []
    index = 0
    while index < len(text):
        tag = TAG_RE.match(text, index)
        if tag:
            for char in tag.group(0):
                spoken.append(char)
                display.append("")
            index = tag.end()
            continue
        for original, replacement in fixes:
            if original and text.startswith(original, index):
                if len(original) != len(replacement):
                    raise ValueError(
                        f"pronunciation override {original!r}->{replacement!r} must preserve character count"
                    )
                for offset, char in enumerate(replacement):
                    spoken.append(char)
                    display.append(original[offset])
                index += len(original)
                break
        else:
            spoken.append(text[index])
            display.append(text[index])
            index += 1
    return "".join(spoken), display


def apply_pronunciation_fixes(text: str) -> str:
    return _apply_with_display(text)[0]


def take_hash(
    voice: str,
    model: str,
    stability: float,
    speed: float,
    text: str,
    provider: str = "elevenlabs",
    pronunciation_rules_sha: str | None = None,
    request_context: dict | None = None,
) -> str:
    payload = {
        "provider": provider,
        "voice": voice,
        "model": model,
        "stability": stability,
        "speed": speed,
        "text": text,
        "pronunciation_rules_sha256": pronunciation_rules_sha,
        "request_context": request_context or {},
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def legacy_take_hash(
    voice: str,
    model: str,
    stability: float,
    speed: float,
    text: str,
    provider: str = "elevenlabs",
) -> str:
    """Pre-journal hash, accepted only to import an already-complete cache."""
    payload = {
        "provider": provider,
        "voice": voice,
        "model": model,
        "stability": stability,
        "speed": speed,
        "text": text,
        "pronunciation_rules_sha256": None,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def srt_time(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _join_units(units) -> str:
    return "".join(unit.text for unit in units)


def build_srt(chars, starts, ends) -> str:
    if not (len(chars) == len(starts) == len(ends)):
        raise ValueError("alignment arrays differ in length")
    units = [AlignmentUnit(char, float(start), float(end)) for char, start, end in zip(chars, starts, ends)]
    return build_srt_units(units, "char")


def build_srt_units(units, granularity: str) -> str:
    if granularity not in {"char", "word"}:
        raise ValueError(f"unknown granularity: {granularity!r}")
    cues = []
    current = []
    current_start = None
    current_length = 0
    for unit in units:
        if not unit.text:
            continue
        if current_start is None:
            current_start = unit.start
        current.append(unit)
        current_length += len(unit.text)
        last = unit.text[-1]
        if last in SENTENCE_END or (last in SOFT_BREAK and current_length >= 8) or current_length >= MAX_LINE:
            value = _join_units(current).strip(" " + SOFT_BREAK)
            if value:
                cues.append((current_start, current[-1].end, value))
            current = []
            current_start = None
            current_length = 0
    if current:
        value = _join_units(current).strip()
        if value:
            cues.append((current_start, current[-1].end, value))
    return "\n".join(
        f"{index}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n"
        for index, (start, end, text) in enumerate(cues, 1)
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)

def _read_limit(value: int | None, force: bool) -> int:
    candidates = []
    if value is not None:
        candidates.append(("explicit --max-credits", value))
    configured = os.getenv("VIDEO_STUDIO_TTS_MAX_CREDITS")
    if configured is not None:
        candidates.append(("VIDEO_STUDIO_TTS_MAX_CREDITS", configured))
    if not candidates:
        raise ValueError(
            "set --max-credits or VIDEO_STUDIO_TTS_MAX_CREDITS before a paid "
            "take; --force-budget is a legacy approval marker, not an "
            "unbounded-spend override"
        )
    limits = []
    for source, raw in candidates:
        try:
            limit = int(raw)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"{source} must be a positive integer"
            ) from error
        if limit <= 0:
            raise ValueError(f"{source} must be a positive integer")
        limits.append(limit)
    _ = force
    return min(limits)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_take(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _journal_path(arguments, out_base: Path) -> Path:
    configured = arguments.spend_journal or os.getenv("VIDEO_STUDIO_TTS_SPEND_JOURNAL")
    if configured:
        return Path(configured).expanduser()
    return out_base.parent.resolve() / ".video-studio-tts-spend.sqlite3"


def _request_payload(arguments, *, voice: str, model: str, spoken: str) -> dict:
    return {
        "provider": arguments.provider,
        "voice": voice,
        "model": model,
        "stability": arguments.stability,
        "speed": arguments.speed,
        "text": spoken,
        "stream": arguments.stream,
        "previous_request_ids": list(arguments.previous_request_id),
        "next_request_ids": list(arguments.next_request_id),
        "previous_text": arguments.previous_text,
        "next_text": arguments.next_text,
        "seed": arguments.seed,
    }


def _actual_credits(metadata: dict, estimated: int) -> int:
    value = metadata.get("character_cost")
    return value if isinstance(value, int) and value >= 0 else estimated


def _write_take(path: Path, receipt: dict) -> None:
    _atomic_write(
        path,
        (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _recover_or_use_cache(
    *,
    journal: SpendJournal,
    receipt: dict,
    audio_path: Path,
    srt_path: Path,
    take_path: Path,
    base_key: str,
    legacy_base_key: str,
    legacy_context: dict,
    request_payload_sha256: str,
    provider_name: str,
    model: str,
    voice: str,
    estimated_credits: int,
    output_base: Path,
) -> bool:
    receipt_hash = receipt.get("hash")
    if (
        receipt_hash not in {base_key, legacy_base_key}
        or receipt.get("voice") != voice
        or receipt.get("model") != model
        or not audio_path.is_file()
        or audio_path.is_symlink()
        or not srt_path.is_file()
        or srt_path.is_symlink()
    ):
        return False
    if receipt_hash == legacy_base_key and (
        receipt.get("previous_request_ids", [])
        != legacy_context["previous_request_ids"]
        or receipt.get("next_request_ids", [])
        != legacy_context["next_request_ids"]
        or receipt.get("seed") != legacy_context["seed"]
        or receipt.get("single_request", False) != legacy_context["stream"]
        or legacy_context["previous_text"] is not None
        or legacy_context["next_text"] is not None
    ):
        raise SubmissionBlocked(
            "legacy take does not prove the current provider context; "
            "an explicit retake is required"
        )
    audio_sha = _sha256_file(audio_path)
    srt_sha = _sha256_file(srt_path)
    if receipt.get("audio_sha256") not in (None, audio_sha):
        return False
    if receipt.get("srt_sha256") not in (None, srt_sha):
        return False

    attempt_id = receipt.get("spend_attempt_id")
    request_id = receipt.get("request_id")
    actual_credits = _actual_credits(receipt, estimated_credits)
    if not isinstance(attempt_id, str):
        if not isinstance(request_id, str) or not request_id.strip():
            raise SubmissionBlocked(
                "legacy take has no provider request ID; use an explicit retake "
                "after deciding how to account for the prior spend"
            )
        attempt = journal.import_legacy_success(
            base_key=base_key,
            provider=provider_name,
            model=model,
            request_payload_sha256=request_payload_sha256,
            estimated_credits=estimated_credits,
            output_base=str(output_base),
            provider_request_id=request_id,
            actual_credits=actual_credits,
        )
        receipt.update(
            hash=base_key,
            spend_attempt_id=attempt.attempt_id,
            spend_journal_schema="video_studio.tts_spend_journal.v1",
            spend_status="succeeded",
            audio_sha256=audio_sha,
            srt_sha256=srt_sha,
        )
        _write_take(take_path, receipt)
        return True

    if (
        not isinstance(receipt.get("audio_sha256"), str)
        or not isinstance(receipt.get("srt_sha256"), str)
    ):
        raise SubmissionBlocked(
            "journal-bound take receipt is missing artifact digests"
        )
    attempt = journal.get(attempt_id)
    if attempt is None or attempt.base_key != base_key:
        raise SubmissionBlocked("take receipt is not bound to its spend journal attempt")
    if attempt.provider_request_id != request_id:
        raise SubmissionBlocked("take receipt provider request ID does not match the journal")
    if attempt.status == "submitted":
        attempt = journal.mark_succeeded(
            attempt.attempt_id,
            actual_credits=actual_credits,
        )
    if attempt.status != "succeeded":
        raise SubmissionBlocked(
            f"cached take is bound to spend state {attempt.status}; reconcile before reuse"
        )
    if receipt.get("spend_status") != "succeeded":
        receipt["spend_status"] = "succeeded"
        receipt["audio_sha256"] = audio_sha
        receipt["srt_sha256"] = srt_sha
        _write_take(take_path, receipt)
    return True


def _reconcile(
    *,
    journal: SpendJournal,
    base_key: str,
    provider,
    provider_request_id: str,
    spoken: str,
    voice: str,
    model: str,
) -> str:
    attempt = journal.latest(base_key)
    if attempt is None:
        raise JournalError("no spend attempt exists for this request")
    if attempt.status == "submitting":
        journal.mark_submission_unknown(
            attempt.attempt_id,
            error="operator reconciliation started after interrupted submit",
        )
        attempt = journal.get(attempt.attempt_id)
    if attempt is None or attempt.status not in {"submission_unknown", "submitted"}:
        raise JournalError(
            f"reconciliation is not allowed from state "
            f"{attempt.status if attempt else 'missing'}"
        )
    if (
        attempt.provider_request_id is not None
        and attempt.provider_request_id != provider_request_id
    ):
        raise SubmissionBlocked(
            "reconciliation request ID conflicts with the durable provider ID"
        )
    records = provider.history_records([provider_request_id])
    record = records.get(provider_request_id)
    if record is None:
        raise SubmissionBlocked(
            "provider history did not prove this request ID; spend remains "
            "submission_unknown and retry is blocked"
        )
    if (
        record.get("text") != spoken
        or record.get("voice_id") != voice
        or record.get("model_id") != model
    ):
        raise SubmissionBlocked(
            "provider history request ID does not match text, voice, and model; "
            "spend remains submission_unknown"
        )
    credits = int(record["character_cost"])
    journal.mark_reconciled_spent(
        attempt.attempt_id,
        provider_request_id=provider_request_id,
        actual_credits=credits,
        proof=f"elevenlabs_history_character_delta:{provider_request_id}:{credits}",
    )
    return (
        f"RECONCILED provider request {provider_request_id} as spent "
        f"({credits} credits); use --retake for a new paid attempt"
    )


def execute(arguments, *, provider=None, stage_hook=None) -> str:
    stage_hook = stage_hook or (lambda _stage, _attempt: None)
    voice = arguments.voice or VOICE
    if not voice:
        raise ValueError("pass --voice or set VIDEO_STUDIO_TTS_VOICE_ID")
    model = DRAFT_MODEL if arguments.draft else arguments.model
    set_active_voice(voice)
    if arguments.pronunciation_overrides:
        set_project_fixes(load_project_fixes(arguments.pronunciation_overrides))
    else:
        set_project_fixes({})

    original = (
        arguments.text
        if arguments.text is not None
        else Path(arguments.text_file).read_text(encoding="utf-8")
    ).strip()
    if not original:
        raise ValueError("narration text is empty")
    processed = normalize_zh(original) if arguments.normalize else original
    if arguments.draft:
        processed = TAG_RE.sub("", processed)
    spoken, display = _apply_with_display(processed)

    out_base = Path(arguments.out_base)
    audio_path = Path(str(out_base) + ".mp3")
    srt_path = Path(str(out_base) + ".srt")
    take_path = Path(str(out_base) + ".take.json")
    payload = _request_payload(arguments, voice=voice, model=model, spoken=spoken)
    request_payload_sha256 = payload_digest(payload)
    base_key = take_hash(
        voice,
        model,
        arguments.stability,
        arguments.speed,
        spoken,
        provider=arguments.provider,
        request_context={
            key: value
            for key, value in payload.items()
            if key not in {"provider", "voice", "model", "stability", "speed", "text"}
        },
    )
    legacy_base_key = legacy_take_hash(
        voice,
        model,
        arguments.stability,
        arguments.speed,
        spoken,
        provider=arguments.provider,
    )
    provider = provider or get_provider(arguments.provider)
    estimated_credits = provider.credits_for(spoken, model)
    journal = SpendJournal(_journal_path(arguments, out_base))

    with journal.locked():
        if arguments.reconcile_request_id:
            return _reconcile(
                journal=journal,
                base_key=base_key,
                provider=provider,
                provider_request_id=arguments.reconcile_request_id,
                spoken=spoken,
                voice=voice,
                model=model,
            )

        if not arguments.retake:
            receipt = _load_take(take_path)
            if _recover_or_use_cache(
                journal=journal,
                receipt=receipt,
                audio_path=audio_path,
                srt_path=srt_path,
                take_path=take_path,
                base_key=base_key,
                legacy_base_key=legacy_base_key,
                legacy_context={
                    "previous_request_ids": list(arguments.previous_request_id),
                    "next_request_ids": list(arguments.next_request_id),
                    "previous_text": arguments.previous_text,
                    "next_text": arguments.next_text,
                    "seed": arguments.seed,
                    "stream": arguments.stream,
                },
                request_payload_sha256=request_payload_sha256,
                provider_name=arguments.provider,
                model=model,
                voice=voice,
                estimated_credits=estimated_credits,
                output_base=out_base,
            ):
                return f"SKIP unchanged take: {audio_path} (no provider request)"

        budget_limit = _read_limit(
            arguments.max_credits,
            arguments.force_budget,
        )
        attempt = journal.reserve(
            base_key=base_key,
            provider=arguments.provider,
            model=model,
            request_payload_sha256=request_payload_sha256,
            estimated_credits=estimated_credits,
            output_base=str(out_base.resolve()),
            budget_limit=budget_limit,
            retake=arguments.retake,
        )
        stage_hook("reserved", attempt)
        journal.mark_submitting(attempt.attempt_id)
        stage_hook("submitting", attempt)
        try:
            result = provider.synthesize(
                spoken,
                voice=voice,
                model=model,
                stability=arguments.stability,
                speed=arguments.speed,
                previous_request_ids=arguments.previous_request_id,
                next_request_ids=arguments.next_request_id,
                previous_text=arguments.previous_text,
                next_text=arguments.next_text,
                seed=arguments.seed,
                stream=arguments.stream,
            )
            stage_hook("provider_returned", attempt)
        except ProviderConfirmedFailure as error:
            journal.mark_confirmed_failure(
                attempt.attempt_id,
                proof=error.proof,
            )
            raise
        except ProviderSubmissionUnknown as error:
            journal.mark_submission_unknown(
                attempt.attempt_id,
                error=str(error),
                provider_request_id=error.provider_request_id,
            )
            raise
        except ProviderError as error:
            journal.mark_submission_unknown(
                attempt.attempt_id,
                error=f"unclassified provider error: {error}",
            )
            raise ProviderSubmissionUnknown(
                "provider submission outcome is unknown; reconciliation required"
            ) from error
        except Exception as error:
            journal.mark_submission_unknown(
                attempt.attempt_id,
                error=f"unexpected submit-boundary error: {type(error).__name__}",
            )
            raise

        metadata = result.metadata or {}
        request_id = metadata.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            journal.mark_submission_unknown(
                attempt.attempt_id,
                error="provider response omitted request_id",
            )
            raise ProviderSubmissionUnknown(
                "provider response omitted request_id; reconciliation required"
            )
        journal.mark_submitted(
            attempt.attempt_id,
            provider_request_id=request_id,
            provider_status="synchronous_response_received",
        )
        stage_hook("submitted", attempt)
        actual_credits = _actual_credits(metadata, estimated_credits)

        try:
            if result.granularity != "char":
                raise ValueError(
                    "timestamped narration requires character alignment"
                )
            chars = [unit.text for unit in result.units]
            if "".join(chars) != spoken or len(chars) != len(display):
                raise ValueError(
                    "provider alignment does not match submitted narration text"
                )
            display_units = [
                AlignmentUnit(fragment, unit.start, unit.end)
                for fragment, unit in zip(display, result.units)
            ]
            srt = build_srt_units(display_units, "char")
            receipt = {
                "schema_version": 1,
                "hash": base_key,
                "provider": arguments.provider,
                "voice": voice,
                "model": model,
                "chars": len(spoken),
                "source_sha256": hashlib.sha256(original.encode()).hexdigest(),
                "credits": estimated_credits,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "request_id": request_id,
                "history_item_id": metadata.get("history_item_id"),
                "character_cost": metadata.get("character_cost"),
                "previous_request_ids": arguments.previous_request_id,
                "next_request_ids": arguments.next_request_id,
                "seed": arguments.seed,
                "single_request": arguments.stream,
                "effective_pronunciation_fixes": active_fixes(),
                "spend_journal_schema": "video_studio.tts_spend_journal.v1",
                "spend_attempt_id": attempt.attempt_id,
                "spend_status": "submitted",
            }
            _atomic_write(audio_path, result.audio)
            _atomic_write(srt_path, srt.encode("utf-8"))
            receipt["audio_sha256"] = _sha256_file(audio_path)
            receipt["srt_sha256"] = _sha256_file(srt_path)
            _write_take(take_path, receipt)
            stage_hook("receipt_written", attempt)
        except Exception as error:
            journal.mark_reconciled_spent(
                attempt.attempt_id,
                provider_request_id=request_id,
                actual_credits=actual_credits,
                proof=f"provider_success_response:{request_id}",
            )
            raise

        journal.mark_succeeded(
            attempt.attempt_id,
            actual_credits=actual_credits,
        )
        receipt["spend_status"] = "succeeded"
        _write_take(take_path, receipt)
        stage_hook("succeeded", attempt)
        duration = result.units[-1].end if result.units else 0.0
        cues = srt.count(" --> ")
        return (
            f"OK wrote {audio_path} and {srt_path} | duration={duration:.2f}s "
            f"cues={cues} | credits={actual_credits} ({model}) | "
            f"spend_attempt={attempt.attempt_id}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--text-file")
    parser.add_argument("--out-base", required=True)
    parser.add_argument("--provider", choices=["elevenlabs"], default="elevenlabs")
    parser.add_argument("--voice")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--speed", type=float, default=SPEED)
    parser.add_argument("--stability", type=float, default=DEFAULT_STABILITY)
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--retake", action="store_true")
    parser.add_argument(
        "--force-budget",
        action="store_true",
        help=(
            "legacy parent approval marker; does not bypass --max-credits or "
            "VIDEO_STUDIO_TTS_MAX_CREDITS"
        ),
    )
    parser.add_argument(
        "--max-credits",
        type=int,
        help="journal-wide cap; combined with the environment cap using the lower value",
    )
    parser.add_argument("--spend-journal")
    parser.add_argument(
        "--reconcile-request-id",
        help="verify this ID in provider history and account an ambiguous submission",
    )
    parser.add_argument("--normalize", action="store_true")
    parser.add_argument("--pronunciation-overrides")
    parser.add_argument("--previous-request-id", action="append", default=[])
    parser.add_argument("--next-request-id", action="append", default=[])
    parser.add_argument("--previous-text")
    parser.add_argument("--next-text")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--stream", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()
    if (
        len(arguments.previous_request_id) > 3
        or len(arguments.next_request_id) > 3
    ):
        parser.error("at most three previous and three next request IDs are supported")
    if (arguments.previous_request_id and arguments.previous_text) or (
        arguments.next_request_id and arguments.next_text
    ):
        parser.error("choose request-ID context or text context for each direction")
    try:
        message = execute(arguments)
    except ValueError as error:
        parser.error(str(error))
    except (BudgetExceeded, JournalError, ProviderError) as error:
        raise SystemExit(f"ERROR: {error}") from error
    print(message)


if __name__ == "__main__":
    main()
