#!/usr/bin/env python3
"""Generate timestamped narration audio and SRT subtitles.

The command is provider-backed, but all identity, credentials, budget and voice
configuration belongs to the operator. There are no bundled voices, API keys,
account paths, pronunciation tables or spending limits.

Required for a new ElevenLabs take:
  * --voice or VIDEO_STUDIO_TTS_VOICE_ID
  * ELEVENLABS_API_KEY or ELEVENLABS_API_KEY_PATH
  * --max-credits/VIDEO_STUDIO_TTS_MAX_CREDITS, or explicit --force-budget

Unchanged takes are reused without contacting the provider.
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

from providers import AlignmentUnit, ProviderError, get_provider
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
) -> str:
    payload = {
        "provider": provider,
        "voice": voice,
        "model": model,
        "stability": stability,
        "speed": speed,
        "text": text,
        "pronunciation_rules_sha256": pronunciation_rules_sha,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
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


def _read_limit(value: int | None, force: bool) -> int | None:
    if force:
        return None
    raw = str(value) if value is not None else os.getenv("VIDEO_STUDIO_TTS_MAX_CREDITS")
    if raw is None:
        raise ValueError(
            "set --max-credits or VIDEO_STUDIO_TTS_MAX_CREDITS before a paid take; "
            "use --force-budget only after explicitly approving this run"
        )
    try:
        limit = int(raw)
    except ValueError as error:
        raise ValueError("TTS max credits must be a positive integer") from error
    if limit <= 0:
        raise ValueError("TTS max credits must be a positive integer")
    return limit


def main() -> None:
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
    parser.add_argument("--force-budget", action="store_true")
    parser.add_argument("--max-credits", type=int)
    parser.add_argument("--normalize", action="store_true")
    parser.add_argument("--pronunciation-overrides")
    parser.add_argument("--previous-request-id", action="append", default=[])
    parser.add_argument("--next-request-id", action="append", default=[])
    parser.add_argument("--previous-text")
    parser.add_argument("--next-text")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()

    if len(args.previous_request_id) > 3 or len(args.next_request_id) > 3:
        parser.error("at most three previous and next request IDs are supported")
    if (args.previous_request_id and args.previous_text) or (
        args.next_request_id and args.next_text
    ):
        parser.error("choose request-ID context or text context for each direction")

    voice = args.voice or VOICE
    if not voice:
        parser.error("pass --voice or set VIDEO_STUDIO_TTS_VOICE_ID")
    model = DRAFT_MODEL if args.draft else args.model
    set_active_voice(voice)
    if args.pronunciation_overrides:
        set_project_fixes(load_project_fixes(args.pronunciation_overrides))
    else:
        set_project_fixes({})

    original = args.text if args.text is not None else Path(args.text_file).read_text(encoding="utf-8")
    original = original.strip()
    if not original:
        parser.error("narration text is empty")
    processed = normalize_zh(original) if args.normalize else original
    if args.draft:
        processed = TAG_RE.sub("", processed)
    spoken, display = _apply_with_display(processed)

    out_base = Path(args.out_base)
    audio_path = Path(str(out_base) + ".mp3")
    srt_path = Path(str(out_base) + ".srt")
    take_path = Path(str(out_base) + ".take.json")
    digest = take_hash(voice, model, args.stability, args.speed, spoken, provider=args.provider)

    if not args.retake and audio_path.is_file() and srt_path.is_file() and take_path.is_file():
        try:
            previous = json.loads(take_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
        if previous.get("hash") == digest and previous.get("voice") == voice:
            print(f"SKIP unchanged take: {audio_path} (no provider request)")
            return

    provider = get_provider(args.provider)
    credits = provider.credits_for(spoken, model)
    try:
        limit = _read_limit(args.max_credits, args.force_budget)
    except ValueError as error:
        parser.error(str(error))
    if limit is not None and credits > limit:
        parser.error(f"estimated {credits} credits exceeds configured limit {limit}")

    try:
        result = provider.synthesize(
            spoken,
            voice=voice,
            model=model,
            stability=args.stability,
            speed=args.speed,
            previous_request_ids=args.previous_request_id,
            next_request_ids=args.next_request_id,
            previous_text=args.previous_text,
            next_text=args.next_text,
            seed=args.seed,
            stream=args.stream,
        )
    except ProviderError as error:
        raise SystemExit(str(error)) from error
    if result.granularity != "char":
        raise SystemExit("ERROR: timestamped narration requires character alignment")
    chars = [unit.text for unit in result.units]
    if "".join(chars) != spoken or len(chars) != len(display):
        raise SystemExit("ERROR: provider alignment does not match submitted narration text")
    display_units = [
        AlignmentUnit(fragment, unit.start, unit.end)
        for fragment, unit in zip(display, result.units)
    ]
    srt = build_srt_units(display_units, "char")
    metadata = result.metadata or {}
    receipt = {
        "schema_version": 1,
        "hash": digest,
        "provider": args.provider,
        "voice": voice,
        "model": model,
        "chars": len(spoken),
        "source_sha256": hashlib.sha256(original.encode()).hexdigest(),
        "credits": credits,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "request_id": metadata.get("request_id"),
        "history_item_id": metadata.get("history_item_id"),
        "character_cost": metadata.get("character_cost"),
        "previous_request_ids": args.previous_request_id,
        "next_request_ids": args.next_request_id,
        "seed": args.seed,
        "single_request": args.stream,
        "effective_pronunciation_fixes": active_fixes(),
    }
    _atomic_write(audio_path, result.audio)
    _atomic_write(srt_path, srt.encode("utf-8"))
    _atomic_write(
        take_path,
        (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    duration = result.units[-1].end if result.units else 0.0
    cues = srt.count(" --> ")
    print(
        f"OK wrote {audio_path} and {srt_path} | duration={duration:.2f}s "
        f"cues={cues} | credits={credits} ({model})"
    )


if __name__ == "__main__":
    main()
