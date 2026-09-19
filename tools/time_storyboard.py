#!/usr/bin/env python3
"""Time a scene plan against the produced narration, and validate the result.

Scene durations must come from the audio that actually exists, never from an
estimate written while planning. A storyboard whose scene boundaries drift from
the narration produces cards that reveal before or after the sentence they
illustrate, and nothing downstream notices — the render succeeds, the video is
just subtly wrong.

So the plan (`storyboard-scenes.json`) carries only a `marker`: the opening
words of the scene, verbatim from the script. This finds each marker in the
SRT, takes that cue's start as the scene's start, and derives every duration
from the gaps. The last scene ends where the audio ends.

Emits two files:

  * `storyboard-final-timed.json` — the plan with real times and frames
  * `storyboard-final-timed-validation.json` — the gate's evidence, with a
    `checks` list. `agent_status.checks_pass` requires that list to be present
    and every entry to pass, so a validation file that merely says `ok: true`
    does not satisfy the gate. An earlier project's validation file had no
    `checks` key at all, which is one reason no real video had ever passed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import visual_timeline

# A scene that sits still for longer than this is a static-run risk; the
# storyboard gate does not measure motion, but flagging the span here is what
# lets the visual pass know where to look.
LONG_SCENE_SECONDS = 75.0

# There is deliberately no motion-graphics check here any more.
#
# There used to be one: at least half the scenes had to carry
# `source: "motion_graphics"`. It could not work, for three reasons.
#
# It tested equality against a value that is not in the field's vocabulary.
# `scene.source` names a card template, and the templates in shipped projects are
# concept_card, compare_panel, node_diagram, list_card, recap_map, chat_mockup,
# cat_talk, receipt_graphic — and `motion_graphic`, singular. A project could be
# built entirely out of designed motion and still score zero, because the check
# wanted the plural. ai-memory-goldfish scores 0/10 that way. This was a type
# error wearing a policy's clothes.
#
# It read a field nothing verifies, so where the plural was used it was
# self-certified. ai-cyber-eval-escape-2026 passed at 0.818 with four scenes
# labelled motion_graphics that hold zero motion shots, and one labelled
# presenter that holds nothing but motion shots. Three projects passed it; none
# of the three had the check tell them anything true.
#
# And it could not be repaired here, because of a circular dependency, not just
# an ordering preference. A shot's composition lives in editorial-contract.json,
# and editorial_contract.py requires that contract to carry the sha256 of
# storyboard-final-timed.json — the file this tool writes. This stage cannot read
# a contract that must hash its own not-yet-existent output.
#
# The floor it was reaching for is enforced properly downstream:
# editorial_contract.MOTION_MIN requires motion graphics to be at least 25% of
# runtime, measured in seconds against digest-bound assets, on every
# presenter-pinned lane (editorial_contract.LANE_PROFILES). manual.v1 is not
# gated on it — agent_status.py takes the `else` branch and passes editorial
# vacuously — and it was not gated on anything real before this either, since the
# copy here was satisfied by a string literal. That gap is real and predates this
# change; it deserves its own ticket rather than a check that cannot measure.
def parse_srt(path: Path):
    def to_seconds(stamp: str) -> float:
        h, m, rest = stamp.split(":")
        s, ms = rest.replace(".", ",").split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    cues = []
    for block in re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip()):
        lines = [ln for ln in block.strip().split("\n") if ln.strip()]
        if len(lines) >= 3 and "-->" in lines[1]:
            start, end = [p.strip() for p in lines[1].split("-->")]
            cues.append({"start": to_seconds(start), "end": to_seconds(end),
                         "text": " ".join(lines[2:]).strip()})
    return cues


def bare(text: str) -> str:
    return re.sub(r"[\s，。、「」『』：；？！—…（）《》〈〉·,.:;?!]", "", text)


def find_marker(marker: str, cues, from_index: int, before_index=None):
    """First cue at or after `from_index` whose text contains the marker.

    Markers are matched with punctuation stripped, because the SRT breaks lines
    on punctuation the script writer did not think about. Searching forward
    only means a phrase repeated later in the script cannot pull a scene
    backwards past one already placed.
    """
    needle = bare(marker)
    limit = len(cues) if before_index is None else before_index
    for i in range(from_index, limit):
        if needle in bare(cues[i]["text"]):
            return i
    # Fall back to a prefix match: the SRT may have split mid-marker.
    for length in range(len(needle) - 1, 3, -1):
        head = needle[:length]
        for i in range(from_index, limit):
            if bare(cues[i]["text"]).startswith(head) or head in bare(cues[i]["text"]):
                return i
    return None


def resolve_visual_events(scenes, cues, fps: int):
    """Replace cue markers with contiguous frame-accurate visual events."""
    unmatched = []
    for scene_index, scene in enumerate(scenes):
        raw_events = scene.get("visual_events")
        if not isinstance(raw_events, list) or not raw_events:
            scene["visual_events"] = []
            continue
        cursor = scene["cue_index"]
        limit = (
            scenes[scene_index + 1]["cue_index"]
            if scene_index + 1 < len(scenes)
            else len(cues)
        )
        timed_events = []
        for event in raw_events:
            if not isinstance(event, dict) or not isinstance(event.get("marker"), str):
                unmatched.append(
                    f"{scene.get('scene_id', scene_index)}: event has no marker"
                )
                continue
            cue_index = find_marker(event["marker"], cues, cursor, limit)
            if cue_index is None:
                unmatched.append(
                    f"{scene.get('scene_id', scene_index)}/{event.get('event_id', '?')}"
                )
                continue
            cursor = cue_index + 1
            timed_events.append(
                {
                    **event,
                    "cue_index": cue_index,
                    "cue": cues[cue_index]["text"],
                    "start_seconds": round(cues[cue_index]["start"], 3),
                }
            )
        for event_index, event in enumerate(timed_events):
            end = (
                timed_events[event_index + 1]["start_seconds"]
                if event_index + 1 < len(timed_events)
                else scene["end_seconds"]
            )
            event["end_seconds"] = end
            event["start_frame"] = round(event["start_seconds"] * fps)
            event["end_frame"] = round(end * fps)
            event["duration_frames"] = event["end_frame"] - event["start_frame"]
        scene["visual_events"] = timed_events
    return unmatched


def audio_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", type=Path)
    ap.add_argument("--plan", default="storyboard-scenes.json")
    args = ap.parse_args()

    project = args.project.resolve()
    plan = json.loads((project / args.plan).read_text(encoding="utf-8"))
    srt = project / plan.get("srt", "narration-final.srt")
    audio = project / plan.get("audio", "narration-final.mp3")
    fps = int(plan.get("fps", 30))
    visual_contract = plan.get("visual_timeline_contract")
    if visual_contract not in {None, visual_timeline.CONTRACT}:
        sys.exit(f"ERROR: unsupported visual_timeline_contract: {visual_contract}")

    cues = parse_srt(srt)
    if not cues:
        sys.exit(f"ERROR: no cues in {srt}")
    total = audio_duration(audio)

    scenes, cursor, unmatched = [], 0, []
    for scene in plan["scenes"]:
        idx = find_marker(scene["marker"], cues, cursor)
        if idx is None:
            unmatched.append(scene["scene_id"])
            continue
        cursor = idx + 1
        scenes.append({**scene, "cue_index": idx,
                       "cue": cues[idx]["text"],
                       "start_seconds": round(cues[idx]["start"], 3)})

    for i, scene in enumerate(scenes):
        end = scenes[i + 1]["start_seconds"] if i + 1 < len(scenes) else round(total, 3)
        scene["end_seconds"] = end
        scene["duration_seconds"] = round(end - scene["start_seconds"], 3)
        scene["start_frame"] = round(scene["start_seconds"] * fps)
        scene["end_frame"] = round(end * fps)
        scene["duration_frames"] = scene["end_frame"] - scene["start_frame"]

    unmatched_visual_events = (
        resolve_visual_events(scenes, cues, fps)
        if visual_contract == visual_timeline.CONTRACT
        else []
    )

    timed = {k: v for k, v in plan.items() if k != "scenes"}
    timed.update({
        "schema": "haru.storyboard_timed.v1",
        "timing_basis": f"cue starts from {srt.name}; last scene ends at audio duration",
        "duration_seconds": round(total, 3),
        "scene_count": len(scenes),
        "scenes": scenes,
    })
    (project / "storyboard-final-timed.json").write_text(
        json.dumps(timed, ensure_ascii=False, indent=2), encoding="utf-8")

    non_positive = [s["scene_id"] for s in scenes if s["duration_seconds"] <= 0]
    out_of_order = [s["scene_id"] for a, s in zip(scenes, scenes[1:])
                    if s["start_seconds"] < a["start_seconds"]]
    long_runs = [{"scene_id": s["scene_id"], "seconds": s["duration_seconds"]}
                 for s in scenes if s["duration_seconds"] > LONG_SCENE_SECONDS]
    multi_row_without_cue = [s["scene_id"] for s in scenes
                             if s.get("multi_row") and not s.get("cue")]
    coverage = round(scenes[-1]["end_seconds"] - scenes[0]["start_seconds"], 3) if scenes else 0.0
    pron_stamp = (project / "narration-final.mp3.pron-ok.json").is_file()

    checks = [
        {"name":"every_scene_matched", "passed": not unmatched,
         "detail": unmatched or "all markers found in the SRT"},
        {"name":"scenes_in_order", "passed": not out_of_order, "detail": out_of_order or "ok"},
        {"name":"no_zero_or_negative_scene", "passed": not non_positive,
         "detail": non_positive or "ok"},
        {"name":"covers_full_audio", "passed": abs(coverage - total) < 1.0,
         "detail": {"coverage_seconds": coverage, "audio_seconds": round(total, 3)}},
        {"name":"starts_at_zero", "passed": bool(scenes) and scenes[0]["start_seconds"] < 1.0,
         "detail": scenes[0]["start_seconds"] if scenes else None},
        {"name":"multi_row_cards_carry_cue", "passed": not multi_row_without_cue,
         "detail": multi_row_without_cue or "ok"},
        {"name":"pronunciation_stamp_exists", "passed": pron_stamp,
         "detail": "narration-final.mp3.pron-ok.json"},
        # Reported, never fatal: a long scene is a warning for the visual pass,
        # not a timing error. Failing here would push the writer to chop beats
        # into pieces to satisfy a validator instead of to serve the edit.
        {"name":"no_long_static_run", "passed": True, "advisory": True,
         "detail": long_runs or f"none over {LONG_SCENE_SECONDS}s"},
    ]
    if visual_contract == visual_timeline.CONTRACT:
        visual_checks = visual_timeline.validate_timed_storyboard(timed)
        if unmatched_visual_events:
            cue_check = next(
                check
                for check in visual_checks
                if check["name"] == "visual_events_cue_locked"
            )
            cue_check["passed"] = False
            details = cue_check["detail"]
            cue_check["detail"] = [
                *(details if isinstance(details, list) else [str(details)]),
                *[f"unmatched visual event: {item}" for item in unmatched_visual_events],
            ]
        checks.extend(visual_checks)
    validation = {
        "schema": "haru.storyboard_validation.v1",
        "ok": all(c["passed"] for c in checks),
        "scene_count": len(scenes),
        "audio_duration_seconds": round(total, 3),
        "checks": checks,
    }
    (project / "storyboard-final-timed-validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")

    for c in checks:
        flag = "ok  " if c["passed"] else "FAIL"
        note = " (advisory)" if c.get("advisory") else ""
        print(f"  [{flag}] {c['name']}{note}")
    print(f"\n{len(scenes)} scenes over {total:.2f}s — ok={validation['ok']}")
    return 0 if validation["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
