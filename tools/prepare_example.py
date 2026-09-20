#!/usr/bin/env python3
"""Author the neutral example into a project already created through MCP.

This prepares inputs only. Rendering, delivery verification, and export still
use the shared workflow interface. No pronunciation or review pass is created.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workspace import direct_path
from workspace_barrier import mutation_barrier

ROOT = Path(__file__).resolve().parents[1]


def run(arguments, **kwargs):
    subprocess.run(arguments, check=True, stdin=subprocess.DEVNULL, **kwargs)


def timestamp(milliseconds):
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def _prepare_unlocked(project: Path, speech: bool):
    project = direct_path(project)
    contract_path = project / "project-contract.json"
    if not contract_path.is_file() or contract_path.is_symlink():
        raise ValueError("create the project through video-studio create first")
    contract = json.loads(contract_path.read_text())
    if not isinstance(contract.get("runtime_contract"), dict):
        raise ValueError("project is missing its runtime compatibility declaration")
    for relative in (
        "remotion",
        "output",
        "storyboard-final-timed.json",
        "render_plan.json",
        "cues.json",
        "composition-content.json",
        "narration-final.srt",
        "script-proposal.md",
        "sources.md",
        "example-audio.json",
    ):
        direct_path(project / relative)
    if (project / "remotion").exists() or (project / "remotion").is_symlink():
        raise ValueError("example inputs already exist; use a fresh project")
    ffmpeg = shutil.which("ffmpeg")
    espeak = shutil.which("espeak-ng") if speech else None
    if not ffmpeg or (speech and not espeak):
        raise ValueError(
            "install ffmpeg and espeak-ng (for --speech) before preparing the example"
        )
    example = ROOT / "examples/narrated/cue-driven-demo"
    content = json.loads((example / "composition-content.json").read_text())
    source_contract = json.loads((example / "project-contract.json").read_text())
    contract.update(
        {
            key: value
            for key, value in source_contract.items()
            if key != "runtime_contract"
        }
    )
    shutil.copytree(
        ROOT / "templates/narrated/remotion",
        project / "remotion",
        ignore=shutil.ignore_patterns("node_modules", ".cache", "out"),
    )
    for filename in ("storyboard-final-timed.json", "render_plan.json", "cues.json"):
        shutil.copy2(example / filename, project / filename)
    (project / "output").mkdir(exist_ok=True)
    assets = project / "remotion/public/assets"
    assets.mkdir(parents=True, exist_ok=True)
    audio = assets / "demo-audio.wav"
    if speech:
        with tempfile.TemporaryDirectory() as temporary:
            temporary = Path(temporary)
            segments = []
            for index, cue in enumerate(content["captions"]):
                original = temporary / f"spoken-{index}.wav"
                subprocess.run(
                    [
                        espeak,
                        "-v",
                        "en-us",
                        "-s",
                        "160",
                        "-w",
                        str(original),
                        "--stdin",
                    ],
                    input=cue["text"],
                    text=True,
                    check=True,
                )
                segment = temporary / f"padded-{index}.wav"
                duration = (cue["endMs"] - cue["startMs"]) / 1000
                probe = subprocess.check_output(
                    [
                        shutil.which("ffprobe") or "ffprobe",
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "csv=p=0",
                        str(original),
                    ],
                    text=True,
                )
                rate = max(1.0, float(probe.strip()) / duration)
                run(
                    [
                        ffmpeg,
                        "-v",
                        "error",
                        "-i",
                        str(original),
                        "-af",
                        f"atempo={rate},apad",
                        "-t",
                        str(duration),
                        "-ar",
                        "48000",
                        "-ac",
                        "1",
                        str(segment),
                    ]
                )
                segments.append(segment)
            arguments = [ffmpeg, "-v", "error"]
            for segment in segments:
                arguments += ["-i", str(segment)]
            inputs = "".join(f"[{index}:a]" for index in range(len(segments)))
            arguments += [
                "-filter_complex",
                f"{inputs}concat=n={len(segments)}:v=0:a=1[a]",
                "-map",
                "[a]",
                str(audio),
            ]
            run(arguments)
        content["media"]["narration"] = {
            "path": "assets/demo-audio.wav",
            "kind": "local_audio",
            "label": "Original example text synthesized locally with eSpeak NG; pronunciation unreviewed",
        }
    else:
        run(
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:sample_rate=48000:duration=24",
                "-af",
                "volume=0.025",
                str(audio),
            ]
        )
        content["media"]["narration"]["path"] = "assets/demo-audio.wav"
    (project / "remotion/src/content.json").write_text(
        json.dumps(content, ensure_ascii=False, indent=2) + "\n"
    )
    (project / "composition-content.json").write_text(
        json.dumps(content, ensure_ascii=False, indent=2) + "\n"
    )
    (project / "narration-final.srt").write_text(
        "\n".join(
            f"{index}\n{timestamp(cue['startMs'])} --> {timestamp(cue['endMs'])}\n{cue['text']}\n"
            for index, cue in enumerate(content["captions"], 1)
        )
    )
    (project / "script-proposal.md").write_text(
        "# From signal to decision\n\n"
        + "\n\n".join(cue["text"] for cue in content["captions"])
        + "\n"
    )
    (project / "sources.md").write_text(
        "# Example sources\n\nOriginal conceptual text and vector graphics authored for Video Studio.\nAudio is generated locally; no private media or paid API is used.\nNo factual-reporting, pronunciation, human-review, or publication approval is claimed.\n"
    )
    (project / "example-audio.json").write_text(
        json.dumps(
            {
                "schema": "video_studio.example_audio.v1",
                "generator": "espeak-ng" if speech else "ffmpeg-sine",
                "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
                "speech": speech,
                "pronunciation_reviewed": False,
            },
            indent=2,
        )
        + "\n"
    )
    contract_path.write_text(json.dumps(contract, indent=2) + "\n")
    return {
        "project": str(project),
        "speech": speech,
        "next": "npm ci inside remotion/, then use the MCP render-project runner",
    }


def prepare(project: Path, speech: bool):
    with mutation_barrier(project):
        return _prepare_unlocked(project, speech)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument(
        "--speech",
        action="store_true",
        help="use the local eSpeak NG engine; requires espeak-ng",
    )
    args = parser.parse_args()
    try:
        result = prepare(args.project, args.speech)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(2, f"prepare-example: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
