#!/usr/bin/env python3
"""Re-derive an SRT's timestamps from speech-to-text run on the produced audio.

eleven_v3 returns a character alignment alongside its audio, and that alignment
was measured to DRIFT inside long requests. On a 1219-char / 375.92s section,
probing the audio inside each cue's own labelled span found:

    cue 5   10.64-14.44s  labelled 第一句，警方已經闢謠。   heard the same   OK
    cue 60  188.16-190.96 labelled 入境紀錄確認。          heard the same   OK
    cue 90  291.44-293.28 labelled 沒有人會來看。          heard 主管機關公告出去 (2 cues behind)
    cue 111 368.32-374.08 labelled 本來就不是許可業務。     heard cue 110's text

The timestamps run ahead of the audio and the error grows with request length.
Every word is present and correct -- only the times are wrong -- so nothing needs
regenerating, just re-timing.

scribe_v1 returns per-character start/end/logprob for Chinese in the same
response we already pay for and currently discard (verify_pronunciation.py reads
only `text`). Those times are derived from the audio itself, so they cannot drift
relative to it.

CUE SEGMENTATION IS PRESERVED EXACTLY -- same count, same text, same boundaries,
only start/end change. That is deliberate: downstream, cue boundaries are
load-bearing (time_storyboard.py matches scene markers by cue text and derives
scene starts from cue positions; visual_timeline.py validates event contiguity
across cues) while the timings are just numbers. Re-timing in place is the change
with the smallest blast radius.

Alignment is by pinyin syllable, not by character: scribe_v1 returns simplified
Chinese and mishears freely (確認 -> 劝念). We do not want its text, only its
clock, so a heard character only has to SOUND like the script character to anchor
it. That is the same machinery the pronunciation gate already uses.
"""
import json
import pathlib
import re
import sys

from verify_pronunciation import CJK_RE, stt_response, syllables, valid_readings

# Fraction of the script's CJK characters that must anchor to a heard character
# before the re-derived timeline is trustworthy. Below this, STT disagreed with
# the script too broadly for interpolation to mean anything, and the caller
# should keep the provider timeline rather than ship a confidently wrong one.
MIN_ANCHOR_COVERAGE = 0.80

CUE_RE = re.compile(
    r"(\d+)\s*\n(\d\d):(\d\d):(\d\d),(\d\d\d)\s*-->\s*"
    r"(\d\d):(\d\d):(\d\d),(\d\d\d)\s*\n(.*?)(?=\n\s*\n|\s*\Z)",
    re.DOTALL,
)


def parse_srt(text: str) -> list[dict]:
    """Parse every cue, or refuse.

    finditer would silently skip a block it cannot match -- a malformed
    timestamp, say -- and the caller rewrites the file, so a skipped cue is
    narration deleted from the subtitle. The block count is checked against the
    cue count so that cannot happen quietly.
    """
    text = text.replace("\r\n", "\n").lstrip("﻿")
    cues = []
    for match in CUE_RE.finditer(text):
        g = match.groups()
        cues.append({
            "index": int(g[0]),
            "start": int(g[1]) * 3600 + int(g[2]) * 60 + int(g[3]) + int(g[4]) / 1000,
            "end": int(g[5]) * 3600 + int(g[6]) * 60 + int(g[7]) + int(g[8]) / 1000,
            "text": g[9].strip("\n"),
        })
    blocks = len([part for part in re.split(r"\n\s*\n", text.strip()) if part.strip()])
    if blocks != len(cues):
        raise ValueError(f"SRT has {blocks} blocks but {len(cues)} parsed as cues")
    return cues


def format_timestamp(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    secs, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def format_srt(cues: list[dict]) -> str:
    return "".join(
        f"{cue['index']}\n"
        f"{format_timestamp(cue['start'])} --> {format_timestamp(cue['end'])}\n"
        f"{cue['text']}\n\n"
        for cue in cues
    )


def heard_chars(words: list[dict]) -> list[dict]:
    """One entry per heard CJK character, with its measured start/end.

    scribe_v1 emits punctuation as its own `word` with a timestamp, but that
    timestamp covers the pause rather than any utterance, so punctuation is
    dropped and cue edges are taken from spoken characters only.
    """
    out = []
    for word in words:
        if word.get("type") not in (None, "word"):
            continue
        text = word.get("text") or ""
        for char in CJK_RE.findall(text):
            out.append({
                "char": char,
                "start": float(word["start"]),
                "end": float(word["end"]),
            })
    return out


def anchor_script_to_heard(script: list[str], heard: list[dict]) -> dict:
    """Map script character index -> (start, end) for characters STT agreed with.

    Matching is on tone-stripped pinyin so a simplified or misheard character
    still anchors when it sounds right.

    A polyphone read the other way (著 zhe/zhuo) does not anchor and is
    interpolated between its neighbours instead. Recovering those was tried and
    dropped: on the real 964-character section it bought 3 more anchors (97.3%
    -> 97.6%, against an 80% floor) in exchange for a cross-list index lookup
    that had to search by time. Three characters is not worth logic that can
    silently mis-index the timeline.
    """
    import difflib

    script_syllables = syllables(script)
    heard_syllables = syllables([item["char"] for item in heard])
    # Every index below assumes one syllable per character. pypinyin merges
    # runs of non-Han characters (['a','b'] -> ['ab']), which would shift every
    # anchor and go unnoticed, so the invariant is checked rather than trusted.
    if len(script_syllables) != len(script) or len(heard_syllables) != len(heard):
        raise ValueError("pinyin did not return one syllable per character")

    matcher = difflib.SequenceMatcher(a=script_syllables, b=heard_syllables, autojunk=False)
    anchors = {}
    for block in matcher.get_matching_blocks():
        # get_matching_blocks() ends with a zero-size sentinel; range(0) skips it.
        for offset in range(block.size):
            i, j = block.a + offset, block.b + offset
            anchors[i] = (heard[j]["start"], heard[j]["end"])
    return anchors


def char_times(script: list[str], anchors: dict, audio_duration: float) -> list[tuple]:
    """A (start, end) for every script character, interpolating unanchored runs.

    An unanchored run is spread evenly across the silence between the anchors
    that bracket it -- the characters were spoken, STT just did not agree on
    what they were, so their time is bounded even when it is not measured.
    """
    if not script:
        return []
    known = sorted(anchors)
    if not known:
        # Nothing anchored. Spread the script evenly rather than raising: the
        # caller still needs the metrics to see WHY, and the coverage gate is
        # what refuses the result.
        step = audio_duration / len(script)
        return [(i * step, (i + 1) * step) for i in range(len(script))]
    times: list[tuple | None] = [anchors.get(i) for i in range(len(script))]

    def fill(lo: int, hi: int, start: float, end: float) -> None:
        """Fill indices [lo, hi) evenly across [start, end)."""
        span = max(0.0, end - start)
        count = hi - lo
        for step, index in enumerate(range(lo, hi)):
            times[index] = (
                start + span * step / count,
                start + span * (step + 1) / count,
            )

    fill(0, known[0], 0.0, anchors[known[0]][0])
    for left, right in zip(known, known[1:]):
        if right > left + 1:
            fill(left + 1, right, anchors[left][1], anchors[right][0])
    fill(known[-1] + 1, len(script), anchors[known[-1]][1], audio_duration)
    return [item for item in times if item is not None]


def retime(srt_text: str, words: list[dict], audio_duration: float) -> tuple:
    """Return (re-timed SRT text, metrics). Segmentation and text are untouched."""
    cues = parse_srt(srt_text)
    if not cues:
        raise ValueError("no cues parsed from SRT")

    script: list[str] = []
    spans: list[tuple] = []
    for cue in cues:
        start = len(script)
        script.extend(CJK_RE.findall(cue["text"]))
        spans.append((start, len(script)))

    heard = heard_chars(words)
    anchors = anchor_script_to_heard(script, heard)
    coverage = len(anchors) / len(script) if script else 1.0
    times = char_times(script, anchors, audio_duration)

    retimed = []
    for cue, (start, stop) in zip(cues, spans):
        if start == stop:
            # A cue with no spoken characters (punctuation only) keeps a zero
            # -width slot at the previous cue's end rather than inventing time.
            at = retimed[-1]["end"] if retimed else 0.0
            retimed.append({**cue, "start": at, "end": at})
            continue
        retimed.append({
            **cue,
            "start": times[start][0],
            "end": times[stop - 1][1],
        })

    # Cue starts stay non-decreasing and no cue ends before it starts, even if
    # a heard character landed out of order, so the SRT is always playable.
    # The first cue needs the end >= start clamp too, so this indexes rather
    # than zipping pairs.
    for index, cue in enumerate(retimed):
        if index:
            cue["start"] = max(cue["start"], retimed[index - 1]["start"])
        cue["end"] = max(cue["end"], cue["start"])

    deltas = sorted(abs(new["start"] - old["start"]) for old, new in zip(cues, retimed))
    metrics = {
        "cue_count": len(cues),
        "script_chars": len(script),
        "anchored_chars": len(anchors),
        "anchor_coverage": coverage,
        "min_anchor_coverage": MIN_ANCHOR_COVERAGE,
        "anchor_coverage_ok": coverage >= MIN_ANCHOR_COVERAGE,
        "audio_duration_seconds": audio_duration,
        # How far the provider's timeline was from the audio. This is the
        # quantity that was invisible before: a drifting alignment looks
        # identical to a correct one until you measure it against the audio.
        "provider_delta_max_seconds": deltas[-1] if deltas else 0.0,
        "provider_delta_median_seconds": deltas[len(deltas) // 2] if deltas else 0.0,
    }
    return format_srt(retimed), metrics


def transcribe(audio_path: str, lang: str = "zho") -> dict:
    """scribe_v1 on the produced audio, kept whole so its timings survive."""
    return stt_response(audio_path, lang)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--srt", required=True, help="SRT to re-time, in place unless --out")
    parser.add_argument("--audio", help="audio to transcribe; omit to reuse --stt-json")
    parser.add_argument("--stt-json", help="scribe_v1 response JSON, read if present else written")
    parser.add_argument("--language", default="zho")
    parser.add_argument("--out")
    parser.add_argument("--metrics-out")
    args = parser.parse_args()

    if args.stt_json and pathlib.Path(args.stt_json).is_file():
        # Transcription is billed per call, so a re-run with the same audio
        # reuses the saved response rather than paying twice to learn the same
        # timings.
        with open(args.stt_json, encoding="utf-8") as handle:
            response = json.load(handle)
    elif args.audio:
        response = transcribe(args.audio, args.language)
        if args.stt_json:
            with open(args.stt_json, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(response, ensure_ascii=False) + "\n")
    else:
        sys.exit("ERROR: pass --audio, or --stt-json pointing at an existing response")

    duration = response.get("audio_duration_secs")
    if not isinstance(duration, (int, float)):
        sys.exit("ERROR: STT response has no audio_duration_secs")
    with open(args.srt, encoding="utf-8") as handle:
        srt_text = handle.read()
    retimed, metrics = retime(srt_text, response.get("words") or [], float(duration))

    # Metrics first, and always: they are most useful when the gate refuses.
    print(json.dumps(metrics, ensure_ascii=False))
    if args.metrics_out:
        with open(args.metrics_out, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")

    # The gate runs BEFORE the write. --out defaults to rewriting --srt in
    # place, so writing first would destroy the provider timeline this refused
    # to replace -- leaving the caller with neither a trustworthy new one nor
    # the old one to fall back to.
    if not metrics["anchor_coverage_ok"]:
        sys.exit(
            f"ERROR: only {metrics['anchor_coverage']:.1%} of script characters anchored "
            f"(minimum {MIN_ANCHOR_COVERAGE:.0%}); the transcript disagrees with the script "
            f"too broadly to re-time from. SRT left untouched.")
    with open(args.out or args.srt, "w", encoding="utf-8") as handle:
        handle.write(retimed)


if __name__ == "__main__":
    main()
