#!/usr/bin/env python3
"""Validate the cue-driven visual timeline embedded in a timed storyboard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


CONTRACT = "cue_driven.v1"
PRESENTER_STATES = {"hidden", "talking", "listening", "reaction"}
FORBIDDEN_SEMANTIC_TIMER_KEYS = {
    "cadence_seconds",
    "focus_period_seconds",
    "interval_seconds",
    "period_seconds",
    "rotate_every_seconds",
    "timer_seconds",
}
EPSILON_SECONDS = 0.002


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check(name: str, problems: list[str]) -> dict:
    return {
        "name": name,
        "passed": not problems,
        "detail": problems or "ok",
    }


def validate_timed_storyboard(value) -> list[dict]:
    cue_problems: list[str] = []
    presenter_problems: list[str] = []
    timer_problems: list[str] = []

    if not isinstance(value, dict):
        return [
            _check("visual_events_cue_locked", ["storyboard is not an object"]),
            _check("presenter_states_explicit", ["storyboard is not an object"]),
            _check("no_semantic_timer", ["storyboard is not an object"]),
        ]
    if value.get("schema") != "haru.storyboard_timed.v1":
        cue_problems.append("storyboard schema is not haru.storyboard_timed.v1")
    if value.get("visual_timeline_contract") != CONTRACT:
        cue_problems.append(f"visual_timeline_contract must be {CONTRACT}")

    scenes = value.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        cue_problems.append("storyboard has no scenes")
        scenes = []

    seen_ids: set[str] = set()
    for scene_index, scene in enumerate(scenes):
        label = (
            scene.get("scene_id")
            if isinstance(scene, dict) and isinstance(scene.get("scene_id"), str)
            else f"scene[{scene_index}]"
        )
        if not isinstance(scene, dict):
            cue_problems.append(f"{label}: scene is not an object")
            continue
        scene_start = scene.get("start_seconds")
        scene_end = scene.get("end_seconds")
        events = scene.get("visual_events")
        if (
            not number(scene_start)
            or not number(scene_end)
            or scene_end <= scene_start
        ):
            cue_problems.append(f"{label}: invalid scene timing")
            continue
        if not isinstance(events, list) or not events:
            cue_problems.append(f"{label}: visual_events is required")
            continue

        previous_end = scene_start
        for event_index, event in enumerate(events):
            event_label = f"{label}/event[{event_index}]"
            if not isinstance(event, dict):
                cue_problems.append(f"{event_label}: event is not an object")
                continue
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id.strip():
                cue_problems.append(f"{event_label}: event_id is required")
            elif event_id in seen_ids:
                cue_problems.append(f"{event_label}: duplicate event_id {event_id}")
            else:
                seen_ids.add(event_id)
                event_label = f"{label}/{event_id}"

            start = event.get("start_seconds")
            end = event.get("end_seconds")
            if not number(start) or not number(end) or end <= start:
                cue_problems.append(f"{event_label}: invalid event timing")
            elif (
                abs(start - previous_end) > EPSILON_SECONDS
                or start < scene_start - EPSILON_SECONDS
                or end > scene_end + EPSILON_SECONDS
            ):
                cue_problems.append(f"{event_label}: events do not cover the scene contiguously")
            else:
                previous_end = end

            if not isinstance(event.get("cue"), str) or not event["cue"].strip():
                cue_problems.append(f"{event_label}: cue is required")
            if (
                not isinstance(event.get("visual_state"), str)
                or not event["visual_state"].strip()
            ):
                cue_problems.append(f"{event_label}: visual_state is required")
            if event.get("presenter_state") not in PRESENTER_STATES:
                presenter_problems.append(
                    f"{event_label}: presenter_state must be one of "
                    + ", ".join(sorted(PRESENTER_STATES))
                )
            forbidden = sorted(FORBIDDEN_SEMANTIC_TIMER_KEYS & set(event))
            if forbidden:
                timer_problems.append(
                    f"{event_label}: semantic timer keys are forbidden: "
                    + ", ".join(forbidden)
                )

        if abs(previous_end - scene_end) > EPSILON_SECONDS:
            cue_problems.append(f"{label}: visual events do not reach scene end")

    return [
        _check("visual_events_cue_locked", cue_problems),
        _check("presenter_states_explicit", presenter_problems),
        _check("no_semantic_timer", timer_problems),
    ]


def valid_timed_storyboard(value) -> bool:
    return all(check["passed"] for check in validate_timed_storyboard(value))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("storyboard", type=Path)
    args = parser.parse_args()
    try:
        value = json.loads(args.storyboard.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    checks = validate_timed_storyboard(value)
    print(json.dumps({"ok": all(c["passed"] for c in checks), "checks": checks}))
    return 0 if all(c["passed"] for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
