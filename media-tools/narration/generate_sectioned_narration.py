#!/usr/bin/env python3
"""Generate cacheable ElevenLabs narration sections with official request stitching."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from generate_narration_with_srt import (
    MODEL,
    VOICE,
    active_fixes,
    apply_pronunciation_fixes,
    load_project_fixes,
    set_active_voice,
    set_project_fixes,
)
import stt_align
from providers import ProviderError
from providers.elevenlabs import ElevenLabsProvider
from zh_normalize import normalize_zh

# Fade applied at both edges of every section before concatenation. 60ms is far
# below a Mandarin syllable (~200ms), so no sound is shortened audibly, but it
# is long enough to remove the one-sample step from digital silence to full
# speech level that made the joins sound cut.
SEAM_FADE_SECONDS = 0.06

# A section must not OPEN on a paragraph shorter than this when the previous
# section can still absorb it. Derived from the prepay-card-fraud script, whose
# section-opening paragraphs measured 8, 13, 20, 20, 21, 24, 36, 42, 46, 50, 58,
# 59, 71 and 116 characters — a clear gap between 24 and 36. Paragraphs in
# general are short there (median 23), so paragraph length alone says nothing;
# this only governs what may START a section.
#
# It is a heuristic, not a guarantee: it cannot tell a continuing beat from a
# deliberate one, so a mid-beat cut on a longer paragraph is still possible. The
# real signal is editorial and belongs in the script.
MIN_SECTION_LEAD_CHARS = 25

# A paragraph opening with one of these continues the sentence before it, so it
# cannot open a section however long it is. Found the hard way: absorbing short
# leads shifted a boundary onto 「是沒有任何一個機關，有義務去統計…」, splitting
# 「那不是媒體不用功。是沒有…」 — a 不是…是… pair — across two requests, which the
# length rule could not see because that paragraph is 31 characters.
#
# Deliberately tiny. 但/所以/因為 open sections perfectly well in this register
# and are NOT listed; these five are the ones that leave a dangling clause.
SECTION_LEAD_CONTINUATIONS = ("是", "也", "卻", "才", "而")


def split_text(text: str, *, target_chars: int = 300,
               max_chars: int = 520,
               min_lead_chars: int = MIN_SECTION_LEAD_CHARS) -> list[dict]:
    """Split on paragraph boundaries; IDs remain positional and human-readable.

    Each section is a separate ElevenLabs request, and the model restarts its
    prosody at every one — it delivers the first line as an opening. So a cut
    that lands mid-beat is audible: 「鐵門拉下。」 ended one section and
    「先走人。」 opened the next, and the staccato run the script intends was
    replaced by a fresh 17 dB attack (caught by ear 2026-08-05).

    Purely counting characters cannot tell a continuing beat from a deliberate
    one — the same script opens a section with 「第五道門，開著。」, 8 chars, and
    that is correct. What we CAN do cheaply is refuse to open a section with a
    very short paragraph when the previous section still has room for it. That
    removes the worst cuts without pretending to editorial judgement.
    """
    if target_chars <= 0 or max_chars < target_chars:
        raise ValueError("require 0 < target_chars <= max_chars")
    if min_lead_chars < 0:
        raise ValueError("require min_lead_chars >= 0")
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    groups: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            raise ValueError(
                f"paragraph has {len(paragraph)} chars, above max_chars={max_chars}; "
                "add a paragraph break at a semantic boundary")
        extra = len(paragraph) + (2 if current else 0)
        if current and current_len + extra > max_chars:
            groups.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += len(paragraph) + (2 if len(current) > 1 else 0)
        if current_len >= target_chars:
            groups.append("\n\n".join(current))
            current = []
            current_len = 0
    if current:
        groups.append("\n\n".join(current))
    groups = _absorb_short_leads(groups, max_chars=max_chars, min_lead_chars=min_lead_chars)
    return [
        {"id": f"section-{index:03d}", "text": group}
        for index, group in enumerate(groups, 1)
    ]


def _absorb_short_leads(groups: list[str], *, max_chars: int,
                        min_lead_chars: int) -> list[str]:
    """Pull a section's very short opening paragraph back into the previous one.

    Left to right, so a section that just grew is measured at its new length
    before the next boundary is considered. The first section is never touched:
    it has no predecessor, and opening the video on a short line is normal.
    """
    if min_lead_chars <= 0 or len(groups) < 2:
        return groups
    merged = [groups[0]]
    for group in groups[1:]:
        paragraphs = group.split("\n\n")
        # Absorb to a fixpoint, not once: pulling one short lead back exposes
        # the next paragraph as the new lead, and that one can be shorter and
        # worse. Absorbing 「所以問題從來不是…」 once left the section opening on
        # 「是我們一直用出事的順序在立法。」, splitting a 不是…是… pair across the
        # cut — a worse break than the one being fixed.
        while (len(paragraphs) > 1
               and (len(paragraphs[0]) < min_lead_chars
                    or paragraphs[0].startswith(SECTION_LEAD_CONTINUATIONS))
               and len(merged[-1]) + 2 + len(paragraphs[0]) <= max_chars):
            merged[-1] = merged[-1] + "\n\n" + paragraphs.pop(0)
        if paragraphs:
            merged.append("\n\n".join(paragraphs))
    return merged


def stitching_context(index: int, manifests: list[dict]) -> tuple[list[str], list[str]]:
    previous = [m.get("request_id") for m in manifests[:index] if m.get("request_id")]
    following = [m.get("request_id") for m in manifests[index + 1:] if m.get("request_id")]
    return previous[-3:], following[:3]


def model_stitching_context(model: str, index: int, sections: list[dict],
                            manifests: list[dict]) -> tuple[list[str], list[str], str | None, str | None]:
    # ElevenLabs currently rejects both request-ID and text continuity fields
    # for eleven_v3 with HTTP 400 unsupported_model (live-verified 2026-07-19).
    if model == "eleven_v3":
        return [], [], None, None
    previous_ids, next_ids = stitching_context(index, manifests)
    return previous_ids, next_ids, None, None


def credit_report(*, full_script_credits: int, generated: list[dict],
                  usage_before: int | None, usage_after: int | None,
                  history_credits: int | None = None) -> dict:
    estimated = sum(int(item.get("credits") or 0) for item in generated)
    provider_costs = [
        int(item["character_cost"])
        for item in generated
        if item.get("character_cost") is not None
    ]
    provider_reported = sum(provider_costs) if provider_costs else None
    subscription_delta = None
    if usage_before is not None and usage_after is not None:
        subscription_delta = usage_after - usage_before
    if history_credits is not None:
        actual = history_credits
        actual_source = "official_history_character_delta"
    elif subscription_delta is not None:
        actual = subscription_delta
        actual_source = "subscription_usage_delta"
    elif len(provider_costs) == len(generated):
        actual = provider_reported
        actual_source = "response_character_cost"
    else:
        actual = estimated
        actual_source = "estimated_from_text"
    return {
        "full_script_credits": full_script_credits,
        "estimated_generated_credits": estimated,
        "provider_reported_credits": provider_reported,
        "official_history_credits": history_credits,
        "actual_subscription_delta": subscription_delta,
        "actual_credits": actual,
        "actual_credits_source": actual_source,
        "credits_avoided_by_cache": max(0, full_script_credits - estimated),
    }


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ffprobe_duration(path: Path) -> float:
    value = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
    ], text=True).strip()
    return float(value)


def ffprobe_audio_format(path: Path) -> tuple[int, int]:
    """(sample_rate, channels) of the first audio stream."""
    value = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,channels", "-of", "csv=p=0", str(path),
    ], text=True).strip().split(",")
    return int(value[0]), int(value[1])


def parse_srt_time(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def format_srt_time(value: float) -> str:
    total_ms = max(0, round(value * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def offset_srt(path: Path, *, offset: float, start_index: int) -> tuple[str, int]:
    output: list[str] = []
    index = start_index
    for block in [b.strip() for b in path.read_text(encoding="utf-8").split("\n\n") if b.strip()]:
        lines = block.splitlines()
        if len(lines) < 3 or " --> " not in lines[1]:
            continue
        start, end = lines[1].split(" --> ")
        output.append(
            f"{index}\n{format_srt_time(parse_srt_time(start) + offset)} --> "
            f"{format_srt_time(parse_srt_time(end) + offset)}\n" + "\n".join(lines[2:])
        )
        index += 1
    return "\n\n".join(output), index


def run_generator(*, section: dict, section_dir: Path, generator: Path,
                  normalize: bool, previous_ids: list[str], next_ids: list[str],
                  previous_text: str | None = None, next_text: str | None = None,
                  seed: int, force_budget: bool, retake: bool,
                  voice: str | None = None,
                  pronunciation_overrides: Path | None = None,
                  spend_journal: Path | None = None,
                  max_credits: int | None = None) -> tuple[dict, bool, str]:
    section_id = section["id"]
    text_path = section_dir / f"{section_id}.txt"
    out_base = section_dir / section_id
    take_path = Path(str(out_base) + ".take.json")
    text_path.write_text(section["text"], encoding="utf-8")
    before = load_json(take_path)
    cmd = [
        sys.executable, str(generator), "--text-file", str(text_path),
        "--out-base", str(out_base), "--seed", str(seed),
    ]
    if voice:
        cmd.extend(["--voice", voice])
    if pronunciation_overrides:
        cmd.extend(["--pronunciation-overrides", str(pronunciation_overrides)])
    if spend_journal:
        cmd.extend(["--spend-journal", str(spend_journal)])
    if max_credits is not None:
        cmd.extend(["--max-credits", str(max_credits)])
    if normalize:
        cmd.append("--normalize")
    for request_id in previous_ids:
        cmd.extend(["--previous-request-id", request_id])
    for request_id in next_ids:
        cmd.extend(["--next-request-id", request_id])
    if previous_text:
        cmd.extend(["--previous-text", previous_text])
    if next_text:
        cmd.extend(["--next-text", next_text])
    if force_budget:
        cmd.append("--force-budget")
    if retake:
        cmd.append("--retake")
    try:
        completed = subprocess.run(cmd, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "no child output").strip()
        detail = detail[-2000:]
        raise RuntimeError(
            f"{section_id} generator failed with exit {error.returncode}: {detail}"
        ) from error
    after = load_json(take_path)
    generated = "OK wrote " in completed.stdout
    if generated and not after.get("request_id"):
        raise RuntimeError(
            f"{section_id} was generated but ElevenLabs returned no request_id; "
            "stopping before another paid section because official stitching cannot continue")
    return after, generated, completed.stdout.strip()


def retime_section(section_id: str, section_dir: Path, *, did_generate: bool) -> dict:
    """Replace the section SRT's provider timings with STT-measured ones.

    eleven_v3's returned alignment drifts inside long requests -- measured two
    cues behind the audio by t=291s on a 375.92s section -- so the SRT it ships
    with cannot be trusted to place a subtitle or a cue-pinned cut. stt_align
    keeps the segmentation and replaces only the clock. See stt_align.py.

    The transcript is cached next to the section: a retake of one section must
    not re-bill transcription for the others, and a re-run with unchanged audio
    should not pay twice to learn the same timings.

    The live .srt stays the authority for cue TEXT and segmentation -- an
    operator who corrects a cue must not have it reverted by the next run. The
    snapshot exists only to measure how far the provider's clock was off, since
    the loop re-times every section on every run and would otherwise be
    comparing against a timeline it had already corrected.

    `did_generate` is what makes the snapshot trustworthy: it is true exactly
    when the child just wrote this take's audio and SRT, which is the only
    moment the .srt on disk is known to carry the provider's own timings. A
    weaker condition (say, "the transcript cache missed") would snapshot an
    already-re-timed SRT whenever that cache was cleared, and then report 0.0s
    drift with every appearance of confidence.
    """
    srt_path = section_dir / f"{section_id}.srt"
    audio_path = section_dir / f"{section_id}.mp3"
    stt_path = section_dir / f"{section_id}.stt.json"
    provider_srt = section_dir / f"{section_id}.provider.srt"
    audio_sha = hashlib.sha256(audio_path.read_bytes()).hexdigest()

    if did_generate:
        provider_srt.write_text(srt_path.read_text(encoding="utf-8"), encoding="utf-8")

    cached = load_json(stt_path)
    # Bound to the audio, not just to existence: a retake writes new audio to
    # the same path, and reusing the old transcript would re-time the new take
    # against the old take's clock.
    transcribed = cached.get("audio_sha256") != audio_sha
    if transcribed:
        cached = stt_align.transcribe(str(audio_path))
        cached["audio_sha256"] = audio_sha
        # Written before the coverage gate below, so a section that fails to
        # anchor can be retried without paying to transcribe it again.
        write_json(stt_path, cached)

    duration = cached.get("audio_duration_secs")
    if not isinstance(duration, (int, float)):
        raise RuntimeError(f"{section_id} transcript has no audio_duration_secs")
    retimed, metrics = stt_align.retime(
        srt_path.read_text(encoding="utf-8"), cached.get("words") or [], float(duration))
    metrics.update(
        source="stt_forced",
        stt_transcribed=transcribed,
        **provider_drift(provider_srt, retimed),
    )
    if not metrics["anchor_coverage_ok"]:
        raise RuntimeError(
            f"{section_id} could not be re-timed: only {metrics['anchor_coverage']:.1%} of "
            f"script characters anchored to the transcript (minimum "
            f"{metrics['min_anchor_coverage']:.0%}). The provider timeline is not a fallback "
            f"-- it is the thing measured to drift -- so neither timeline can be trusted here.")
    srt_path.write_text(retimed, encoding="utf-8")
    # Merged into whatever the child wrote rather than replacing it: this is the
    # child's receipt, and writing over it wholesale would silently drop fields
    # the day the caller's copy stops being the whole file.
    take_path = section_dir / f"{section_id}.take.json"
    write_json(take_path, {**load_json(take_path), "srt_alignment": metrics})
    return metrics


def provider_drift(provider_srt: Path, retimed: str) -> dict:
    """How far the provider's clock was from the audio, or unknown.

    Unknown rather than zero whenever the comparison cannot be made -- no
    snapshot, or a snapshot whose segmentation no longer matches. A drift of
    0.0s is a claim about the provider; absence of evidence must not be able to
    impersonate it.
    """
    unknown = {
        "provider_snapshot": False,
        "provider_delta_max_seconds": None,
        "provider_delta_median_seconds": None,
    }
    if not provider_srt.is_file():
        return unknown
    before = stt_align.parse_srt(provider_srt.read_text(encoding="utf-8"))
    after = stt_align.parse_srt(retimed)
    if len(before) != len(after):
        return unknown
    deltas = sorted(abs(new["start"] - old["start"]) for old, new in zip(before, after))
    return {
        "provider_snapshot": True,
        "provider_delta_max_seconds": deltas[-1] if deltas else 0.0,
        "provider_delta_median_seconds": deltas[len(deltas) // 2] if deltas else 0.0,
    }


def srt_alignment_summary(manifests: list[dict]) -> dict:
    """Worst case across sections, so a caller can gate on one number.

    Worst rather than mean: one badly-timed section is enough to put a
    cue-pinned cut on the wrong word, and averaging would hide it behind the
    sections that came out clean.
    """
    entries = [item.get("srt_alignment") or {} for item in manifests]
    coverages = [item["anchor_coverage"] for item in entries
                 if isinstance(item.get("anchor_coverage"), (int, float))]
    deltas = [item["provider_delta_max_seconds"] for item in entries
              if isinstance(item.get("provider_delta_max_seconds"), (int, float))]
    return {
        "source": "stt_forced",
        "sections": len(entries),
        # A section that reported no coverage leaves this null rather than
        # letting the sections that did report carry it -- a reader must not be
        # able to mistake "not measured" for "measured and fine".
        "min_anchor_coverage": (
            min(coverages) if len(coverages) == len(entries) and entries else None
        ),
        "sections_measured": len(coverages),
        # Null once any section's provider timeline was not snapshotted: the
        # drift is unknown there, not zero.
        "provider_delta_max_seconds": (
            max(deltas) if len(deltas) == len(entries) and entries else None
        ),
        "sections_with_provider_snapshot": len(deltas),
        # This repo meters TTS carefully; transcription is a real per-call cost
        # on the same account, so the take says how many were actually paid for
        # rather than leaving the cache to imply none were.
        "stt_calls": sum(1 for item in entries if item.get("stt_transcribed")),
    }


def merge_sections(section_dir: Path, sections: list[dict], out_base: Path,
                   gap_seconds: float) -> tuple[float, int]:
    mp3s = [section_dir / f"{item['id']}.mp3" for item in sections]
    srts = [section_dir / f"{item['id']}.srt" for item in sections]
    for path in [*mp3s, *srts]:
        if not path.exists():
            raise RuntimeError(f"missing section artifact: {path}")
    out_base.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_base.parent) as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        # Each section's audio starts at full speech level on its first sample —
        # ElevenLabs gives no lead-in. Concatenating straight into digital silence
        # therefore steps from nothing to about -11 dB in one sample at every
        # join, and a listener hears that as a splice ("聽起來像被裁過", caught by
        # ear 2026-08-05 at the section-001/002 join). A short fade at both edges
        # removes the discontinuity without shortening any syllable.
        faded = []
        for mp3 in mp3s:
            faded_wav = temp_dir / f"{mp3.stem}-faded.wav"
            fade_out_start = max(0.0, ffprobe_duration(mp3) - SEAM_FADE_SECONDS)
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mp3),
                "-af", (f"afade=t=in:st=0:d={SEAM_FADE_SECONDS},"
                        f"afade=t=out:st={fade_out_start:.3f}:d={SEAM_FADE_SECONDS}"),
                "-c:a", "pcm_s16le", str(faded_wav),
            ], check=True)
            faded.append(faded_wav)
        # WAV intermediates keep this at ONE lossy encode, the same as before the
        # fade pass existed — fading in mp3 would have added a second generation.
        #
        # The gap MUST match the sections' sample rate and channel count. Raw PCM
        # carries no per-file layout for the concat demuxer to reconcile, so a
        # stereo gap between mono sections is read as twice its length: a 0.30s
        # gap became 0.60s of audio, 3.9s of drift across 13 joins, and the
        # narration alignment gate rejected the take. (It was a hardcoded stereo
        # anullsrc before, harmless only because the gap was then an mp3.)
        sample_rate, channels = ffprobe_audio_format(faded[0])
        layout = {1: "mono", 2: "stereo"}.get(channels)
        if layout is None:
            raise RuntimeError(f"unsupported section channel count: {channels}")
        silence = temp_dir / "silence.wav"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={layout}",
            "-t", str(gap_seconds), "-c:a", "pcm_s16le", str(silence),
        ], check=True)
        concat = temp_dir / "concat.txt"
        concat_paths: list[Path] = []
        for index, part in enumerate(faded):
            if index:
                concat_paths.append(silence)
            concat_paths.append(part)
        concat.write_text("".join(f"file '{path.resolve()}'\n" for path in concat_paths), encoding="utf-8")
        temp_mp3 = temp_dir / "narration.mp3"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat),
            "-c:a", "libmp3lame", "-b:a", "128k", str(temp_mp3),
        ], check=True)
        all_srt: list[str] = []
        next_index = 1
        offset = 0.0
        for index, (part, srt) in enumerate(zip(faded, srts)):
            if index:
                offset += gap_seconds
            block, next_index = offset_srt(srt, offset=offset, start_index=next_index)
            all_srt.append(block)
            # Measure the decoded WAV, not the mp3 header: the concatenated audio
            # is built from these, so cue offsets track what a listener hears.
            offset += ffprobe_duration(part)
        temp_srt = temp_dir / "narration.srt"
        temp_srt.write_text("\n\n".join(all_srt) + "\n", encoding="utf-8")
        os.replace(temp_mp3, Path(str(out_base) + ".mp3"))
        os.replace(temp_srt, Path(str(out_base) + ".srt"))
    duration = ffprobe_duration(Path(str(out_base) + ".mp3"))
    cues = sum(
        1 for line in Path(str(out_base) + ".srt").read_text(encoding="utf-8").splitlines()
        if " --> " in line
    )
    return duration, cues


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text-file", required=True)
    parser.add_argument("--out-base", required=True)
    parser.add_argument("--sections-dir")
    parser.add_argument("--target-chars", type=int, default=300)
    parser.add_argument("--max-chars", type=int, default=520)
    parser.add_argument("--gap-seconds", type=float, default=0.30)
    parser.add_argument("--normalize", action="store_true")
    parser.add_argument("--seed-base", type=int, default=0)
    parser.add_argument("--retake-section", action="append", default=[])
    parser.add_argument(
        "--force-budget",
        action="store_true",
        help="legacy parent approval marker; a configured credit cap is still required",
    )
    parser.add_argument(
        "--max-credits",
        type=int,
        help="shared journal cap; the child also enforces any environment cap",
    )
    parser.add_argument("--spend-journal")
    # Voice is per-video, not per-repo: the module constant is only the default.
    # The voice pronunciation rules in the child generator (narration/pronunciation/
    # voice/) are voice-specific, so a non-default voice must be re-verified with
    # verify_pronunciation.py before it is trusted.
    parser.add_argument("--voice", default=None)
    parser.add_argument("--pronunciation-overrides")
    args = parser.parse_args()
    voice = args.voice or VOICE
    if not voice:
        parser.error("pass --voice or set VIDEO_STUDIO_TTS_VOICE_ID")

    text_path = Path(args.text_file).resolve()
    out_base = Path(args.out_base).resolve()
    section_dir = Path(args.sections_dir).resolve() if args.sections_dir else Path(str(out_base) + "-sections")
    section_dir.mkdir(parents=True, exist_ok=True)
    spend_journal = (
        Path(args.spend_journal).expanduser().resolve()
        if args.spend_journal
        else section_dir / ".video-studio-tts-spend.sqlite3"
    )
    # The wrapper respells too, for its own credit accounting. Without this it
    # would price the run against the default voice's table — the exact leak this
    # file's --voice flag exists to prevent.
    set_active_voice(voice)
    overrides_path = Path(args.pronunciation_overrides).resolve() if args.pronunciation_overrides else None
    set_project_fixes(load_project_fixes(str(overrides_path)) if overrides_path else {})
    text = text_path.read_text(encoding="utf-8").strip()
    sections = split_text(text, target_chars=args.target_chars, max_chars=args.max_chars)
    valid_ids = {item["id"] for item in sections}
    unknown = set(args.retake_section) - valid_ids
    if unknown:
        parser.error(f"unknown --retake-section IDs: {sorted(unknown)}")

    generator = Path(__file__).with_name("generate_narration_with_srt.py")
    manifests = [load_json(section_dir / f"{item['id']}.take.json") for item in sections]
    provider = ElevenLabsProvider()
    usage_before = None

    generated: list[dict] = []
    for index, section in enumerate(sections):
        previous_ids, next_ids, previous_text, next_text = model_stitching_context(
            MODEL, index, sections, manifests
        )
        manifest, did_generate, output = run_generator(
            section=section,
            section_dir=section_dir,
            generator=generator,
            normalize=args.normalize,
            previous_ids=previous_ids,
            next_ids=next_ids,
            previous_text=previous_text,
            next_text=next_text,
            seed=args.seed_base + index,
            force_budget=args.force_budget,
            retake=section["id"] in args.retake_section,
            voice=voice,
            pronunciation_overrides=overrides_path,
            spend_journal=spend_journal,
            max_credits=args.max_credits,
        )
        # Re-time before moving on. The provider's own alignment drifts inside
        # long requests, and merge_sections offsets each section by its measured
        # audio duration, so every section must carry a trustworthy clock before
        # they are stitched.
        manifest["srt_alignment"] = retime_section(
            section["id"], section_dir, did_generate=did_generate)
        manifests[index] = manifest
        print(output)
        if did_generate:
            generated.append(manifest)

    usage_after = None

    history_credits = None
    request_ids = [item.get("request_id") for item in generated if item.get("request_id")]
    if request_ids:
        try:
            exact_costs = {}
            for attempt in range(3):
                exact_costs = provider.history_costs(request_ids)
                if len(exact_costs) == len(request_ids):
                    break
                if attempt < 2:
                    time.sleep(1)
            if len(exact_costs) == len(request_ids):
                history_credits = sum(exact_costs.values())
                for item in generated:
                    if item.get("request_id") in exact_costs:
                        item["official_history_credits"] = exact_costs[item["request_id"]]
            else:
                print(
                    f"WARN official history has {len(exact_costs)}/{len(request_ids)} "
                    "new request IDs; falling back to response headers/estimate",
                    file=sys.stderr,
                )
        except ProviderError as error:
            print(f"WARN official history lookup unavailable: {error}", file=sys.stderr)

    processed_text = normalize_zh(text) if args.normalize else text
    processed_text = apply_pronunciation_fixes(processed_text)
    full_credits = ElevenLabsProvider().credits_for(processed_text, MODEL)
    report = credit_report(
        full_script_credits=full_credits,
        generated=generated,
        usage_before=usage_before,
        usage_after=usage_after,
        history_credits=history_credits,
    )
    duration, cues = merge_sections(section_dir, sections, out_base, args.gap_seconds)
    combined = {
        "schema_version": 1,
        "source": str(text_path),
        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "voice": voice,
        "model": MODEL,
        "normalize": args.normalize,
        "pronunciation_overrides_sha256": (
            hashlib.sha256(overrides_path.read_bytes()).hexdigest() if overrides_path else None
        ),
        "gap_seconds": args.gap_seconds,
        "seam_fade_seconds": SEAM_FADE_SECONDS,
        # The rules actually applied: the voice layer merged with this project's
        # overrides. Callers used to be able to name only the project overrides
        # they passed in, which understates what changed the audio — the voice
        # layer can carry dozens of rules the caller never saw. Reporting it here
        # lets a receipt bind to the real set instead of to this file's sha256,
        # which stopped proving anything once the voice rules moved into data
        # files.
        "effective_pronunciation_fixes": dict(active_fixes()),
        "sections": [
            {"id": item["id"], "chars": len(item["text"]), **manifest}
            for item, manifest in zip(sections, manifests)
        ],
        # Where the SRT's timestamps came from, and how far the provider's own
        # alignment was from the audio. That distance used to be invisible: a
        # drifting alignment reads exactly like a correct one until it is
        # measured against the audio, which is how a take shipped with cues
        # pointing two cues behind what was actually being said.
        "srt_alignment": srt_alignment_summary(manifests),
        "run_credit_report": report,
        "duration_seconds": duration,
        "srt_cues": cues,
    }
    Path(str(out_base) + ".take.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"OK merged {len(sections)} sections | duration={duration:.2f}s cues={cues} | "
        f"actual_credits={report['actual_credits']} "
        f"source={report['actual_credits_source']} | "
        f"cache_saved={report['credits_avoided_by_cache']}"
    )


if __name__ == "__main__":
    main()
