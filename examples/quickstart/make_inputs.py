#!/usr/bin/env python3
"""Create a small, local-only input pack for the Video Studio quickstart."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import wave


SAMPLE_RATE = 48_000
SILENCE_SECONDS = 0.35
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SENTENCES = (
    "When a project feels too big, stop trying to finish everything at once.",
    "Choose one small next step you can complete, then make it visible.",
    "Review what changed, revise what did not work, and let that result guide the next step.",
)


class QuickstartError(RuntimeError):
    pass


def require_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise QuickstartError(f"{name} is required and was not found on PATH")
    return executable


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QuickstartError(f"could not run {Path(command[0]).name}") from error
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or ["unknown error"]
        raise QuickstartError(f"{Path(command[0]).name} failed: {detail[0][:300]}")
    return result


def prepare_output(path: Path) -> Path:
    path = path.expanduser()
    absolute = Path(os.path.abspath(path))
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise QuickstartError("output path must not contain symlinks")
    resolved = path.resolve(strict=False)
    if resolved == REPOSITORY_ROOT or REPOSITORY_ROOT in resolved.parents:
        raise QuickstartError("output must be outside the source repository")
    if path.exists():
        if not path.is_dir():
            raise QuickstartError("output must be a directory")
        if any(path.iterdir()):
            raise QuickstartError("output directory must be empty")
    else:
        path.mkdir(parents=True, mode=0o700)
    return path.resolve()


def wav_frames(path: Path):
    try:
        with wave.open(str(path), "rb") as source:
            params = source.getparams()
            frames = source.readframes(params.nframes)
    except (OSError, EOFError, wave.Error) as error:
        raise QuickstartError(f"invalid WAV produced at {path.name}") from error
    if (
        params.nchannels != 1
        or params.sampwidth != 2
        or params.framerate != SAMPLE_RATE
        or params.comptype != "NONE"
    ):
        raise QuickstartError("generated WAV does not use 48 kHz mono PCM")
    return params, frames


def milliseconds(frame_count: int) -> int:
    return round(frame_count * 1000 / SAMPLE_RATE)


def srt_timestamp(value_ms: int) -> str:
    hours, remainder = divmod(value_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def render_sentences(work: Path, espeak: str, ffmpeg: str) -> list[bytes]:
    rendered: list[bytes] = []
    expected = None
    for index, sentence in enumerate(SENTENCES, start=1):
        raw = work / f"sentence-{index}-raw.wav"
        normalized = work / f"sentence-{index}.wav"
        run([espeak, "-v", "en-us", "-s", "165", "-w", str(raw), sentence])
        run(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(raw),
                "-ar",
                str(SAMPLE_RATE),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(normalized),
            ]
        )
        params, frames = wav_frames(normalized)
        comparable = (
            params.nchannels,
            params.sampwidth,
            params.framerate,
            params.comptype,
        )
        if expected is not None and comparable != expected:
            raise QuickstartError("sentence WAV formats do not match")
        expected = comparable
        rendered.append(frames)
    return rendered


def write_voice_and_timing(work: Path, sentence_frames: list[bytes]):
    gap_frames = round(SILENCE_SECONDS * SAMPLE_RATE)
    silence = b"\0" * gap_frames * 2
    cursor_frames = 0
    cues = []
    scene_ranges = []
    combined = bytearray()

    for index, (sentence, frames) in enumerate(zip(SENTENCES, sentence_frames)):
        cue_start = cursor_frames
        combined.extend(frames)
        cursor_frames += len(frames) // 2
        cue_end = cursor_frames
        cues.append((milliseconds(cue_start), milliseconds(cue_end), sentence))
        if index < len(sentence_frames) - 1:
            combined.extend(silence)
            cursor_frames += gap_frames
        scene_ranges.append((milliseconds(cue_start), milliseconds(cursor_frames)))

    voice = work / "voice.wav"
    with wave.open(str(voice), "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(SAMPLE_RATE)
        destination.writeframes(bytes(combined))

    captions = []
    for index, (start, end, sentence) in enumerate(cues, start=1):
        captions.extend(
            [
                str(index),
                f"{srt_timestamp(start)} --> {srt_timestamp(end)}",
                sentence,
                "",
            ]
        )
    (work / "captions.srt").write_text("\n".join(captions), encoding="utf-8")
    return milliseconds(cursor_frames), scene_ranges


def write_project(work: Path, duration_ms: int, ranges: list[tuple[int, int]]):
    scenes = [
        {
            "id": "choose",
            "start_seconds": 0,
            "end_seconds": ranges[0][1] / 1000,
            "heading": "Shrink the task",
            "text": "Turn the whole project into one finishable action.",
            "visual": {
                "kind": "cards",
                "labels": ["Everything", "Next step", "Done"],
                "active_index": 1,
            },
        },
        {
            "id": "make",
            "start_seconds": ranges[0][1] / 1000,
            "end_seconds": ranges[1][1] / 1000,
            "heading": "Make progress visible",
            "text": "Complete one small step you can inspect.",
            "visual": {
                "kind": "steps",
                "labels": ["Choose", "Make", "Show"],
                "active_index": 1,
            },
        },
        {
            "id": "revise",
            "start_seconds": ranges[1][1] / 1000,
            "end_seconds": duration_ms / 1000,
            "heading": "Review, then revise",
            "text": "Use the result to choose the next useful step.",
            "visual": {"kind": "signal", "active_index": 2},
        },
    ]
    project = {
        "schema": "video_studio.project_spec.v1",
        "title": "One useful next step",
        "kicker": "MAKE · REVIEW · REVISE",
        "format": "landscape",
        "narration": {
            "mode": "import",
            "audio": "voice",
            "captions": "captions",
            "provider": "eSpeak NG mechanical demo voice",
        },
        "assets": [
            {"id": "voice", "kind": "audio", "file": "voice.wav"},
            {"id": "captions", "kind": "subtitle", "file": "captions.srt"},
        ],
        "scenes": scenes,
    }
    (work / "project.json").write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_provenance(work: Path, duration_ms: int):
    quoted = "\n".join(f"> {sentence}" for sentence in SENTENCES)
    content = f"""# Quickstart input provenance

These files were generated locally by `examples/quickstart/make_inputs.py`.
The narration text is original to this repository:

{quoted}

- Voice: eSpeak NG `en-us`, speed 165; deliberately mechanical demo speech
- Network/API calls: none
- Audio processing: sentence WAVs resampled to 48 kHz mono PCM, then concatenated as PCM samples with no tempo or pitch effect
- Duration: {duration_ms / 1000:.3f} seconds, measured from the generated WAV
- Captions: timed from the generated sentence WAV lengths; editorial alignment unreviewed
- Pronunciation and speech quality: unreviewed and not approved for publication

Replace `voice.wav` and `captions.srt` with your own reviewed media for real work.
"""
    (work / "PROVENANCE.md").write_text(content, encoding="utf-8")


def generate(output: Path, *, demo_voice: bool) -> dict:
    if not demo_voice:
        raise QuickstartError(
            "this generator requires the explicit --demo-voice choice"
        )
    destination = prepare_output(output)
    espeak = require_tool("espeak-ng")
    ffmpeg = require_tool("ffmpeg")
    ffprobe = require_tool("ffprobe")

    with tempfile.TemporaryDirectory(prefix=".quickstart-", dir=destination) as temp:
        work = Path(temp)
        sentence_frames = render_sentences(work, espeak, ffmpeg)
        duration_ms, ranges = write_voice_and_timing(work, sentence_frames)
        write_project(work, duration_ms, ranges)
        write_provenance(work, duration_ms)
        probe = run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(work / "voice.wav"),
            ]
        )
        try:
            probed_ms = round(
                float(json.loads(probe.stdout)["format"]["duration"]) * 1000
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise QuickstartError("ffprobe did not return the WAV duration") from error
        if probed_ms != duration_ms:
            raise QuickstartError(
                "generated WAV duration did not match the authored timeline"
            )

        for name in ("voice.wav", "captions.srt", "project.json", "PROVENANCE.md"):
            os.replace(work / name, destination / name)

    return {"output": str(destination), "duration_seconds": duration_ms / 1000}


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Generate local quickstart inputs; this does not render a video."
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="new or empty output directory"
    )
    parser.add_argument(
        "--demo-voice",
        action="store_true",
        help="explicitly use the mechanical eSpeak NG demo voice",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = generate(args.output, demo_voice=args.demo_voice)
    except QuickstartError as error:
        print(f"quickstart inputs: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
