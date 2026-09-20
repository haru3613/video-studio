#!/usr/bin/env python3
"""Re-time a project's visuals against the narration that is canonical now.

Promoting a new narration take moves every cue. The visual side is keyed to
those cues in three places, and all three carry ABSOLUTE SECONDS that nothing
else recomputes:

  * `storyboard-final-timed.json` -- scene and event times, derived from the SRT
  * `editorial-contract.json` -- 100+ shots, each pinned to an event's exact
    seconds and bound to the timed storyboard by digest
  * `render_plan.json` -- chapter sound effects at absolute offsets

Left alone after a narration swap, the render cuts the new audio on the old
take's clock and every gate still passes -- the video is just subtly wrong, in
the way this pipeline exists to prevent.

None of this is an editorial decision. The scene plan matches by MARKER TEXT,
the shots match by `event_id`, and the sound effects match by `at_scene`. What
was chosen stays chosen; only the clock moves. That is why this can be a runner
rather than a re-edit.

What it deliberately does NOT do: re-approve the editorial preview. That is a
human verdict on how the cut looks, and the cut just changed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RETIME_SCHEMA = "haru.visual_retime.v1"
STORYBOARD_SCHEMA = "haru.storyboard_timed.v1"
EDITORIAL_SCHEMA = "haru.editorial_contract.v1"
# editorial_contract.py compares shot times to event times with this tolerance,
# so copying them across has to be exact rather than rounded again.
EVENT_TIME_TOLERANCE_SECONDS = 0.002


def tool_environment() -> dict:
    """The child env, with the paths ffmpeg actually installs to.

    The MCP runner is spawned with a minimal PATH that has no /opt/homebrew/bin,
    so ffprobe is simply absent -- which surfaced as an opaque
    storyboard_timing_failed on the first real run through MCP while the same
    command worked from a shell. pronunciation_workflow.py extends PATH the same
    way for the same reason.
    """
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join(
        [environment.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"]
    )
    return environment


def ffprobe_binary() -> str:
    configured = os.environ.get("HARU_FFPROBE")
    if configured:
        return configured
    found = shutil.which(
        "ffprobe",
        path=os.pathsep.join([*os.get_exec_path(), "/opt/homebrew/bin", "/usr/local/bin"]),
    )
    if not found:
        raise RetimeError("runtime_unavailable", "ffprobe is unavailable")
    return found


class RetimeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def direct_file(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise RetimeError("invalid_path", f"expected a direct file: {path.name}")
    return path.resolve(strict=True)


def contained_file(project: Path, value: object) -> Path:
    """A project-relative path from project DATA, resolved without escaping.

    asset_path comes out of the editorial contract, which is a file in the
    project rather than something this runner authored. editorial_contract.py
    and render_project_worker.py both guard the same field this way; doing less
    here would let a contract point ffprobe at anything on the disk and then
    write that path into the project's own JSON.
    """
    if not isinstance(value, str) or not value or value.startswith("/"):
        raise RetimeError("invalid_path", "asset path must be project-relative")
    candidate = project
    for part in Path(value).parts:
        if part in ("..", "/"):
            raise RetimeError("invalid_path", f"asset path escapes the project: {value}")
        candidate = candidate / part
        if candidate.is_symlink():
            raise RetimeError("invalid_path", f"asset path crosses a symlink: {value}")
    resolved = direct_file(candidate)
    if not resolved.is_relative_to(project):
        raise RetimeError("invalid_path", f"asset path escapes the project: {value}")
    return resolved


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetimeError("invalid_artifact", str(exc)) from exc
    if not isinstance(value, dict):
        raise RetimeError("invalid_artifact", f"{path.name} must be an object")
    return value


def replace_file_atomically(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    # NamedTemporaryFile creates 0600; without this every file this rewrites
    # silently loses its group/other read bit.
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def write_json_atomically(path: Path, value: dict) -> None:
    replace_file_atomically(
        path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def time_storyboard(project: Path) -> None:
    """Re-derive the timed storyboard from the canonical SRT and audio."""
    script = direct_file(Path(__file__).resolve().with_name("time_storyboard.py"))
    result = subprocess.run(
        [sys.executable, str(script), str(project)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
        env=tool_environment(),
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "no output").strip()[-500:]
        raise RetimeError("storyboard_timing_failed", f"time_storyboard failed: {tail}")


def storyboard_events(storyboard: dict) -> dict:
    events = {}
    for scene in storyboard.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        for event in scene.get("visual_events", []):
            if isinstance(event, dict) and isinstance(event.get("event_id"), str):
                events[event["event_id"]] = event
    return events


def scene_starts(storyboard: dict) -> dict:
    return {
        scene["scene_id"]: scene["start_seconds"]
        for scene in storyboard.get("scenes", [])
        if isinstance(scene, dict)
        and isinstance(scene.get("scene_id"), str)
        and isinstance(scene.get("start_seconds"), (int, float))
    }


def retime_editorial(project: Path, storyboard_path: Path, storyboard: dict) -> dict:
    """Move every shot onto its own event's new seconds. Nothing else changes.

    A shot names an `event_id`; the timed storyboard says when that event now
    happens. Which asset, which composition, which receipt -- all untouched.
    A shot whose event no longer exists is refused rather than guessed at: the
    edit and the narration have genuinely diverged and a human has to look.
    """
    contract_path = direct_file(project / "editorial-contract.json")
    contract = read_json(contract_path)
    if contract.get("schema") != EDITORIAL_SCHEMA:
        raise RetimeError("invalid_artifact", "editorial contract has an unexpected schema")
    shots = contract.get("shots")
    if not isinstance(shots, list) or not shots:
        raise RetimeError("invalid_artifact", "editorial contract has no shots")

    events = storyboard_events(storyboard)
    missing = sorted(
        {
            shot.get("event_id")
            for shot in shots
            if not isinstance(shot, dict) or shot.get("event_id") not in events
        }
    )
    if missing:
        raise RetimeError(
            "editorial_events_unmatched",
            "the timed storyboard no longer carries these editorial events, so the "
            f"cut and the narration have diverged: {', '.join(map(str, missing[:8]))}",
        )
    # The validator also requires the two sets to match exactly, so an event with
    # no shot is just as fatal -- and far easier to diagnose here than there.
    orphaned = sorted(set(events) - {shot["event_id"] for shot in shots})
    if orphaned:
        raise RetimeError(
            "editorial_events_unmatched",
            "the timed storyboard carries events no shot covers: "
            f"{', '.join(orphaned[:8])}",
        )

    moved = 0
    for shot in shots:
        event = events[shot["event_id"]]
        start, end = event.get("start_seconds"), event.get("end_seconds")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            raise RetimeError(
                "invalid_artifact", f"{shot['event_id']}: event has no times"
            )
        if (
            abs(shot.get("start_seconds", -1) - start) > EVENT_TIME_TOLERANCE_SECONDS
            or abs(shot.get("end_seconds", -1) - end) > EVENT_TIME_TOLERANCE_SECONDS
        ):
            moved += 1
        shot["start_seconds"] = start
        shot["end_seconds"] = end

    contract["storyboard_sha256"] = sha256(storyboard_path)
    write_json_atomically(contract_path, contract)
    return {"shots": len(shots), "shots_moved": moved, "path": "editorial-contract.json"}


def rebind_render_plan(project: Path, contract_path: Path) -> str:
    """Point render_plan.json at the contract as it now stands.

    render_project_worker refuses a plan whose editorial_contract_sha256 does
    not equal the contract on disk, so a re-time that moved every shot but left
    this digest behind blocks the render with a message about binding rather
    than about timing. Nothing else updates it.
    """
    plan_path = direct_file(project / "render_plan.json")
    plan = read_json(plan_path)
    digest = sha256(contract_path)
    if plan.get("editorial_contract_sha256") == digest:
        return "unchanged"
    plan["editorial_contract_sha256"] = digest
    write_json_atomically(plan_path, plan)
    return "rebound"


def retime_sound_effects(project: Path, storyboard: dict) -> dict:
    """Resolve `at_scene` anchors to the scene's new start.

    The four chapter whooshes on this project's previous render sat on scene
    starts to the millisecond -- they mark chapter transitions, they were just
    written down as the seconds those transitions happened to fall on. Recording
    the anchor instead means the next narration swap moves them for free.

    An effect with no `at_scene` is left exactly as written: a hand-placed hit
    that is not a chapter marker has no anchor to follow, and guessing one would
    move a sound the operator put somewhere deliberate.
    """
    plan_path = direct_file(project / "render_plan.json")
    plan = read_json(plan_path)
    effects = (plan.get("audio_mix") or {}).get("sound_effects")
    if not isinstance(effects, list):
        return {"anchored": 0, "unanchored": 0}

    starts = scene_starts(storyboard)
    anchored, moved = 0, 0
    for effect in effects:
        if not isinstance(effect, dict) or "at_scene" not in effect:
            continue
        scene_id = effect["at_scene"]
        if scene_id not in starts:
            raise RetimeError(
                "sound_effect_anchor_unmatched",
                f"sound effect anchored to unknown scene: {scene_id}",
            )
        anchored += 1
        if effect.get("start_seconds") != starts[scene_id]:
            moved += 1
        effect["start_seconds"] = starts[scene_id]
    if anchored:
        write_json_atomically(plan_path, plan)
    return {
        "anchored": anchored,
        "unanchored": sum(
            1 for effect in effects if isinstance(effect, dict) and "at_scene" not in effect
        ),
        "moved": moved,
    }


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [ffprobe_binary(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=False, timeout=60,
        env=tool_environment(),
    )
    try:
        return round(float(result.stdout.strip()), 3)
    except ValueError as exc:
        raise RetimeError("asset_unmeasurable", f"cannot measure {path.name}") from exc


# Files Remotion reaches by staticFile(), which resolves under remotion/public/
# and nowhere else. Each is a copy of a canonical project artifact that nothing
# in the pipeline kept in step: promoting a narration replaced the root file and
# left the renderer reading the previous take, so the preview came out as the
# new cut carrying the old narration -- with every gate green, because every
# gate reads the root.
RENDERER_STATIC_COPIES = (
    "narration-final.mp3",
    "narration-final.srt",
)


def sync_static_copies(project: Path) -> list:
    """Re-copy any canonical artifact the renderer keeps its own copy of.

    Only files that ALREADY exist under remotion/public/ are touched. Their
    presence is what says this composition reads them; creating one a project
    never had would be inventing an input.
    """
    public = project / "remotion/public"
    if public.is_symlink() or not public.is_dir():
        return []
    resynced = []
    for name in RENDERER_STATIC_COPIES:
        source, copy = project / name, public / name
        if not source.is_file() or source.is_symlink():
            continue
        if copy.is_symlink():
            raise RetimeError("invalid_path", f"remotion/public/{name} is a symlink")
        if not copy.is_file():
            continue
        if sha256(copy) != sha256(source):
            replace_file_atomically(copy, source.read_bytes())
            resynced.append(name)
    return resynced


def sync_render_inputs(project: Path, storyboard_path: Path, srt: Path) -> dict:
    """Put the re-timed files where the renderer actually reads them.

    Remotion imports `remotion/public/data/*`, NOT the project root -- and
    nothing else in the pipeline writes that directory. Every gate reads the
    root copies, so a re-time that stopped there would leave the render cutting
    the old timeline while everything reported green. That is the exact failure
    this runner exists to prevent, so the copies are part of the re-time rather
    than a step someone has to remember.

    cues.json is rebuilt from the canonical SRT for the same reason, and
    asset-durations.json is extended to cover any asset the contract newly
    references -- an unmeasured asset is one the renderer cannot lay out.
    """
    # Not every project has one. ai-cyber-eval-escape-2026 imports the contract
    # from the project root directly, so for that layout the re-time is already
    # complete and there is nothing to copy -- refusing would leave it re-timed
    # but receiptless, and failing identically on every retry.
    data = project / "remotion/public/data"
    if data.is_symlink():
        raise RetimeError("invalid_path", "remotion/public/data is a symlink")
    if not data.is_dir():
        return {"synced": False, "reason": "project has no remotion/public/data"}

    contract_path = direct_file(project / "editorial-contract.json")
    for name, source in (
        ("storyboard-final-timed.json", storyboard_path),
        ("editorial-contract.json", contract_path),
    ):
        target = data / name
        if target.is_symlink():
            raise RetimeError("invalid_path", f"{name} in remotion/public/data is a symlink")
        replace_file_atomically(target, source.read_bytes())

    cues = [
        {"start": cue["start"], "end": cue["end"], "text": cue["text"]}
        for cue in parse_srt(srt)
    ]
    if not cues:
        raise RetimeError("invalid_artifact", "the canonical SRT has no cues")
    replace_file_atomically(
        data / "cues.json",
        (json.dumps(cues, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    durations_path = data / "asset-durations.json"
    durations = read_json(durations_path) if durations_path.is_file() else {}
    contract = read_json(contract_path)
    measured = []
    for asset in sorted({shot.get("asset_path") for shot in contract["shots"]
                         if isinstance(shot, dict) and isinstance(shot.get("asset_path"), str)}):
        if asset in durations:
            continue
        durations[asset] = ffprobe_duration(contained_file(project, asset))
        measured.append(asset)
    if measured:
        write_json_atomically(durations_path, durations)
    return {
        "synced": True, "cues": len(cues), "assets_measured": measured,
        "static_copies_resynced": sync_static_copies(project),
    }


def parse_srt(path: Path) -> list:
    import re

    cues = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = [line for line in block.strip().split("\n") if line.strip()]
        if len(lines) >= 3 and "-->" in lines[1]:
            def seconds(stamp: str) -> float:
                hours, minutes, rest = stamp.strip().split(":")
                secs, ms = rest.replace(".", ",").split(",")
                return int(hours) * 3600 + int(minutes) * 60 + int(secs) + int(ms) / 1000

            start, end = lines[1].split("-->")
            cues.append({"start": seconds(start), "end": seconds(end),
                         "text": " ".join(lines[2:]).strip()})
    return cues


def retime(project_path: Path) -> dict:
    project = project_path.resolve(strict=True)
    if not project.is_dir() or project_path.is_symlink():
        raise RetimeError("invalid_path", "expected a direct project directory")

    narration = direct_file(project / "narration-final.mp3")
    narration_srt = direct_file(project / "narration-final.srt")
    direct_file(project / "storyboard-scenes.json")

    state = project / ".hvp"
    if state.is_symlink() or not state.is_dir():
        raise RetimeError("invalid_path", "expected a direct .hvp directory")
    receipt_path = state / "visual-retime.json"
    # Dropped before anything is mutated: it is the only record of which
    # narration the visuals are timed against, and a run that fails partway
    # would otherwise leave it asserting "complete" over a half-re-timed
    # project.
    if receipt_path.is_file() and not receipt_path.is_symlink():
        receipt_path.unlink()

    time_storyboard(project)
    storyboard_path = direct_file(project / "storyboard-final-timed.json")
    validation_path = direct_file(project / "storyboard-final-timed-validation.json")
    storyboard = read_json(storyboard_path)
    validation = read_json(validation_path)
    if storyboard.get("schema") != STORYBOARD_SCHEMA:
        raise RetimeError("invalid_artifact", "timed storyboard has an unexpected schema")
    # time_storyboard exits non-zero when any check fails, so re-deriving the
    # verdict here would be unfalsifiable. Only the shape is checked: a
    # validation file with no checks at all is what an older tool wrote, and
    # agent_status refuses it downstream.
    if not isinstance(validation.get("checks"), list) or not validation["checks"]:
        raise RetimeError("storyboard_timing_failed", "timing validation recorded no checks")

    editorial = retime_editorial(project, storyboard_path, storyboard)
    effects = retime_sound_effects(project, storyboard)
    plan_binding = rebind_render_plan(project, project / "editorial-contract.json")
    render_inputs = sync_render_inputs(project, storyboard_path, narration_srt)

    receipt = {
        "schema": RETIME_SCHEMA,
        "project": project.name,
        "status": "complete",
        # What the visuals are now timed against. A later narration promotion
        # changes this digest, which is how a stale re-time is detectable.
        "narration_sha256": sha256(narration),
        "narration_srt_sha256": sha256(narration_srt),
        "narration_duration_seconds": storyboard.get("duration_seconds"),
        "scene_count": storyboard.get("scene_count"),
        "editorial": editorial,
        "sound_effects": effects,
        "render_plan_binding": plan_binding,
        "render_inputs": render_inputs,
        "artifacts": {
            "storyboard": {
                "path": "storyboard-final-timed.json",
                "sha256": sha256(storyboard_path),
            },
            "storyboard_validation": {
                "path": "storyboard-final-timed-validation.json",
                "sha256": sha256(validation_path),
            },
            "editorial_contract": {
                "path": "editorial-contract.json",
                "sha256": sha256(project / "editorial-contract.json"),
            },
            # What the renderer actually imports, digested separately from the
            # root copies because the whole hazard is the two drifting. Absent
            # for a project layout that has no such directory.
            **({
                "render_editorial_contract": {
                    "path": "remotion/public/data/editorial-contract.json",
                    "sha256": sha256(project / "remotion/public/data/editorial-contract.json"),
                },
                "render_cues": {
                    "path": "remotion/public/data/cues.json",
                    "sha256": sha256(project / "remotion/public/data/cues.json"),
                },
            } if render_inputs["synced"] else {}),
        },
        # The cut moved, so the human verdict on how it looks no longer applies.
        # Stated on the receipt rather than left for someone to remember.
        "requires_editorial_preview_review": True,
    }
    write_json_atomically(receipt_path, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    args = parser.parse_args(argv)
    try:
        value = retime(args.project)
    except RetimeError as exc:
        print(json.dumps(
            {"schema_version": 1, "outcome": "error", "code": exc.code, "data": None}
        ))
        return 2
    print(json.dumps(value, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
