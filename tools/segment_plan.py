#!/usr/bin/env python3
"""Canonical read-only validation for optional four-act segment plans.

``segment-plan.json`` is deliberately a project artifact rather than a runner
input.  This module establishes the stable plan shape that later render,
review, and assembly runners consume; it never infers a plan or a review.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path

import canonical_layout

SCHEMA = "haru.segment_plan.v1"
STORYBOARD_SCHEMA = "haru.storyboard_timed.v1"
PLAN_PATH = "segment-plan.json"
SEGMENTS = ("qi", "cheng", "zhuan", "he")
TRANSITION_POLICY = "cut.v1"
AUDIO_POLICY = "premix.v1"
INPUT_PATHS = {
    "storyboard": "storyboard-final-timed.json",
    "editorial_contract": "editorial-contract.json",
    "narration": "narration-final.mp3",
    "srt": "narration-final.srt",
}
OUTPUTS = {
    segment_id: f"output/segments/{ordinal:02d}-{segment_id}.mp4"
    for ordinal, segment_id in enumerate(SEGMENTS, start=1)
}
EPSILON = 0.000_001


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_update(digest, value) -> None:
    if value is None:
        digest.update(b"n")
    elif isinstance(value, bool):
        digest.update(b"t" if value else b"f")
    elif isinstance(value, int):
        if -(1 << 63) <= value <= (1 << 64) - 1:
            payload = str(value).encode("ascii")
            digest.update(b"i" + str(len(payload)).encode("ascii") + b":" + payload)
        else:
            try:
                shared_value = float(value)
            except OverflowError as exc:
                raise ValueError("JSON integer is outside the shared number range") from exc
            if not math.isfinite(shared_value):
                raise ValueError("JSON integer is outside the shared number range")
            digest.update(b"d" + struct.pack(">d", shared_value))
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number must be finite")
        digest.update(b"d" + struct.pack(">d", value))
    elif isinstance(value, str):
        try:
            payload = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("JSON string contains a non-Unicode-scalar value") from exc
        digest.update(b"s" + str(len(payload)).encode("ascii") + b":" + payload)
    elif isinstance(value, list):
        digest.update(b"a" + str(len(value)).encode("ascii") + b":")
        for item in value:
            _canonical_update(digest, item)
    elif isinstance(value, dict):
        encoded_keys = []
        for key in value:
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            try:
                encoded_keys.append((key.encode("utf-8"), key))
            except UnicodeEncodeError as exc:
                raise ValueError("JSON object key contains a non-Unicode-scalar value") from exc
        encoded_keys.sort(key=lambda item: item[0])
        digest.update(b"o" + str(len(encoded_keys)).encode("ascii") + b":")
        for _encoded, key in encoded_keys:
            _canonical_update(digest, key)
            _canonical_update(digest, value[key])
    else:
        raise ValueError(f"unsupported canonical JSON value: {type(value).__name__}")


def _canonical_digest(value) -> str:
    digest = hashlib.sha256()
    _canonical_update(digest, value)
    return digest.hexdigest()


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

def _frame_index(seconds: float, fps: int) -> int:
    return round(seconds * fps)


def _same_time(left, right) -> bool:
    return _is_number(left) and _is_number(right) and abs(float(left) - float(right)) <= EPSILON


def _direct_file(project: Path, relative: str) -> Path | None:
    candidate = canonical_layout.direct_path(project / relative, project)
    if candidate is None:
        return None
    try:
        return candidate if candidate.is_file() and not candidate.is_symlink() else None
    except OSError:
        return None


def _load_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"cannot parse {path.name}: {exc}"
    return (value, None) if isinstance(value, dict) else (None, f"{path.name} must be a JSON object")


def _parse_srt(path: Path) -> tuple[list[dict], str | None]:
    def seconds(stamp: str) -> float:
        hours, minutes, remainder = stamp.strip().replace(".", ",").split(":")
        second, millis = remainder.split(",")
        return int(hours) * 3600 + int(minutes) * 60 + int(second) + int(millis) / 1000

    try:
        blocks = path.read_text(encoding="utf-8").strip().replace("\r\n", "\n").split("\n\n")
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"cannot read {path.name}: {exc}"
    cues = []
    for ordinal, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 3 or "-->" not in lines[1] or not any(line.strip() for line in lines[2:]):
            return [], f"SRT cue {ordinal} is malformed"
        try:
            start_text, end_text = (part.strip() for part in lines[1].split("-->", 1))
            start, end = seconds(start_text), seconds(end_text)
        except (TypeError, ValueError):
            return [], f"SRT cue {ordinal} has an invalid timestamp"
        if end < start:
            return [], f"SRT cue {ordinal} has negative duration"
        if end == start:
            continue
        cues.append({
            "cue_index": ordinal,
            "start_seconds": start,
            "end_seconds": end,
            "text": "\n".join(lines[2:]),
        })
    return (cues, None) if cues else ([], "SRT has no positive-duration cues")


def _problems(prefix: str, problems: list[str], values, expected) -> None:
    if values != expected:
        problems.append(f"{prefix} must be {expected!r}; got {values!r}")


def _boundary(segment: dict, key: str, scene: dict, event: dict, cue: dict, problems: list[str]) -> float | None:
    value = segment.get(key)
    label = f"segment {segment.get('segment_id', '?')} {key}"
    if not isinstance(value, dict):
        problems.append(f"{label} must be an object")
        return None
    required = {"scene_id", "event_id", "cue_index", "seconds"}
    if set(value) != required:
        problems.append(f"{label} must contain exactly {sorted(required)}")
        return None
    seconds = value["seconds"]
    if (
        value["scene_id"] != scene["scene_id"]
        or value["event_id"] != event["event_id"]
        or value["cue_index"] != cue["cue_index"]
        or not _is_number(seconds)
    ):
        problems.append(f"{label} does not identify its canonical scene, event, and cue seam")
        return None
    expected = event["start_seconds"] if key == "start" else event["end_seconds"]
    cue_expected = cue["start_seconds"] if key == "start" else cue["end_seconds"]
    scene_expected = scene["start_seconds"] if key == "start" else scene["end_seconds"]
    if not (_same_time(seconds, expected) and _same_time(seconds, cue_expected) and _same_time(seconds, scene_expected)):
        problems.append(f"{label} is not a shared scene/event/SRT boundary")
        return None
    return float(seconds)


def _canonical_storyboard(storyboard: dict, problems: list[str]) -> tuple[list[dict], list[dict]]:
    scenes = storyboard.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        problems.append("storyboard-final-timed.json must contain non-empty scenes")
        return [], []
    normalized_scenes, normalized_events = [], []
    seen_scenes, seen_events = set(), set()
    for scene_index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            problems.append(f"storyboard scene {scene_index} must be an object")
            continue
        scene_id = scene.get("scene_id")
        start, end = scene.get("start_seconds"), scene.get("end_seconds")
        events = scene.get("visual_events")
        if not isinstance(scene_id, str) or not scene_id or scene_id in seen_scenes or not _is_number(start) or not _is_number(end) or float(end) <= float(start):
            problems.append(f"storyboard scene {scene_index} has invalid identity or timing")
            continue
        if not isinstance(events, list) or not events:
            problems.append(f"storyboard scene {scene_id} must contain visual_events")
            continue
        copied = {"scene_id": scene_id, "start_seconds": float(start), "end_seconds": float(end), "events": [], "raw": scene}
        seen_scenes.add(scene_id)
        for event_index, event in enumerate(events):
            if not isinstance(event, dict):
                problems.append(f"storyboard event {scene_id}/{event_index} must be an object")
                continue
            event_id = event.get("event_id")
            event_start, event_end = event.get("start_seconds"), event.get("end_seconds")
            if not isinstance(event_id, str) or not event_id or event_id in seen_events or not _is_number(event_start) or not _is_number(event_end) or float(event_end) <= float(event_start):
                problems.append(f"storyboard event {scene_id}/{event_index} has invalid identity or timing")
                continue
            if float(event_start) < float(start) - EPSILON or float(event_end) > float(end) + EPSILON:
                problems.append(f"storyboard event {event_id} falls outside scene {scene_id}")
                continue
            copied_event = {"event_id": event_id, "start_seconds": float(event_start), "end_seconds": float(event_end), "scene_id": scene_id, "raw": event}
            copied["events"].append(copied_event)
            normalized_events.append(copied_event)
            seen_events.add(event_id)
        normalized_scenes.append(copied)
    return normalized_scenes, normalized_events


def _render_input_digest(inputs: dict) -> str:
    return _canonical_digest({"schema": "haru.segment_render_inputs.v1", "inputs": inputs})


def _global_dependency_digest(
    current_inputs: dict,
    segments: list[dict],
    assembly: dict,
    storyboard_authority: dict,
) -> str:
    locality = []
    for segment in segments:
        locality.append({
            key: segment.get(key)
            for key in (
                "segment_id",
                "ordinal",
                "start",
                "end",
                "scene_ids",
                "event_ids",
                "selector",
                "output",
            )
        })
    return _canonical_digest({
        "schema": "haru.segment_global_dependencies.v1",
        "editorial_contract_sha256": current_inputs["editorial_contract"]["sha256"],
        "narration_sha256": current_inputs["narration"]["sha256"],
        "storyboard_authority": storyboard_authority,
        "locality": {"segments": locality, "assembly": assembly},
    })


def _segment_dependency_digest(
    segment: dict,
    definition_sha256: str,
    global_dependency_sha256: str,
    scenes: list[dict],
    events: list[dict],
    cues: list[dict],
) -> str:
    return _canonical_digest({
        "schema": "haru.segment_dependencies.v1",
        "segment_id": segment["segment_id"],
        "definition_sha256": definition_sha256,
        "global_dependency_sha256": global_dependency_sha256,
        "storyboard": {
            "scenes": [scene["raw"] for scene in scenes],
            "events": [event["raw"] for event in events],
        },
        "srt_cues": cues,
    })


def validate(project: Path) -> dict:
    """Return the full optional-plan result without mutating project state."""
    raw_plan = project / PLAN_PATH
    if not raw_plan.exists() and not raw_plan.is_symlink():
        return {"mode": "legacy_non_segmented", "present": False, "path": PLAN_PATH, "problems": [], "segments": [], "next_actionable_segment": None}
    plan_candidate = canonical_layout.direct_path(raw_plan, project)
    if plan_candidate is None:
        return {"mode": "invalid_segment_plan", "present": True, "path": PLAN_PATH, "problems": ["segment-plan.json must be a direct regular file inside the project"], "segments": [], "next_actionable_segment": None}
    if plan_candidate.is_symlink() or not plan_candidate.is_file():
        return {"mode": "invalid_segment_plan", "present": True, "path": PLAN_PATH, "problems": ["segment-plan.json must be a direct regular file inside the project"], "segments": [], "next_actionable_segment": None}

    plan, error = _load_json(plan_candidate)
    if error:
        return {"mode": "invalid_segment_plan", "present": True, "path": PLAN_PATH, "problems": [error], "segments": [], "next_actionable_segment": None}
    try:
        _canonical_digest(plan)
    except ValueError as exc:
        return {
            "mode": "invalid_segment_plan",
            "present": True,
            "path": PLAN_PATH,
            "problems": [f"segment-plan.json contains unsupported canonical JSON: {exc}"],
            "segments": [],
            "next_actionable_segment": None,
        }

    problems: list[str] = []
    if plan.get("schema") != SCHEMA:
        problems.append(f"segment-plan.json schema must be {SCHEMA}")
    if plan.get("project") != project.name:
        problems.append("segment-plan.json project must match the project directory")

    inputs = plan.get("inputs")
    current_inputs = {}
    if not isinstance(inputs, dict) or set(inputs) != set(INPUT_PATHS):
        problems.append(f"inputs must contain exactly {sorted(INPUT_PATHS)}")
    else:
        for name, relative in INPUT_PATHS.items():
            binding = inputs.get(name)
            source = _direct_file(project, relative)
            if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
                problems.append(f"inputs.{name} must contain exactly path and sha256")
                continue
            if binding.get("path") != relative:
                problems.append(f"inputs.{name}.path must be {relative}")
                continue
            if not canonical_layout.valid_sha256(binding.get("sha256")):
                problems.append(f"inputs.{name}.sha256 must be a lowercase SHA-256")
                continue
            if source is None:
                problems.append(f"current input {relative} is missing or not a direct file")
                continue
            current = _sha256(source)
            current_inputs[name] = {"path": relative, "sha256": current}
            if binding["sha256"] != current:
                problems.append(f"inputs.{name}.sha256 does not match current {relative}")
    if isinstance(inputs, dict) and set(inputs) == set(INPUT_PATHS) and len(current_inputs) == len(INPUT_PATHS):
        if plan.get("render_input_sha256") != _render_input_digest(current_inputs):
            problems.append("render_input_sha256 does not match the current canonical input digest")
    elif "render_input_sha256" not in plan:
        problems.append("render_input_sha256 is required")

    storyboard_path = _direct_file(project, INPUT_PATHS["storyboard"])
    storyboard, storyboard_error = _load_json(storyboard_path) if storyboard_path else (None, "storyboard-final-timed.json is missing")
    storyboard_authority = {}
    if storyboard_error:
        problems.append(storyboard_error)
        scenes, events = [], []
    elif not isinstance(storyboard, dict):
        problems.append("storyboard-final-timed.json must be a JSON object")
        scenes, events = [], []
    else:
        try:
            _canonical_digest(storyboard)
        except ValueError as exc:
            return {
                "mode": "invalid_segment_plan",
                "present": True,
                "path": PLAN_PATH,
                "problems": [f"storyboard-final-timed.json contains unsupported canonical JSON: {exc}"],
                "segments": [],
                "next_actionable_segment": None,
            }
        if storyboard.get("schema") != STORYBOARD_SCHEMA:
            problems.append(f"storyboard-final-timed.json schema must be {STORYBOARD_SCHEMA}")
        storyboard_authority = {
            key: value for key, value in storyboard.items() if key != "scenes"
        }
        scenes, events = _canonical_storyboard(storyboard, problems)
    srt_path = _direct_file(project, INPUT_PATHS["srt"])
    cues, cue_error = _parse_srt(srt_path) if srt_path else ([], "narration-final.srt is missing")
    if cue_error:
        problems.append(cue_error)

    segments = plan.get("segments")
    if not isinstance(segments, list):
        problems.append("segments must be an ordered list")
        segments = []
    if len(segments) != len(SEGMENTS):
        problems.append("segments must contain exactly four entries")

    scene_by_id = {scene["scene_id"]: scene for scene in scenes}
    event_by_id = {event["event_id"]: event for event in events}
    all_scene_ids = [scene["scene_id"] for scene in scenes]
    all_event_ids = [event["event_id"] for event in events]
    covered_scenes, covered_events, records, record_sources = [], [], [], []
    previous_end = None

    for index, segment in enumerate(segments):
        label = f"segments[{index}]"
        if not isinstance(segment, dict):
            problems.append(f"{label} must be an object")
            continue
        segment_id = segment.get("segment_id")
        expected_id = SEGMENTS[index] if index < len(SEGMENTS) else None
        if segment_id != expected_id:
            problems.append(f"{label}.segment_id must be {expected_id!r}; got {segment_id!r}")
        ordinal = segment.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal != index + 1:
            problems.append(f"{label}.ordinal must be the integer {index + 1}")
        if not isinstance(segment.get("narrative_role"), str) or not segment["narrative_role"].strip():
            problems.append(f"{label}.narrative_role must be a non-empty string")
        segment_scene_ids = segment.get("scene_ids")
        segment_event_ids = segment.get("event_ids")
        if (
            not isinstance(segment_scene_ids, list)
            or not segment_scene_ids
            or not all(isinstance(value, str) and value for value in segment_scene_ids)
        ):
            problems.append(f"{label}.scene_ids must be a non-empty list of scene IDs")
            segment_scene_ids = []
        if (
            not isinstance(segment_event_ids, list)
            or not segment_event_ids
            or not all(isinstance(value, str) and value for value in segment_event_ids)
        ):
            problems.append(f"{label}.event_ids must be a non-empty list of event IDs")
            segment_event_ids = []
        covered_scenes.extend(segment_scene_ids)
        covered_events.extend(segment_event_ids)
        segment_scenes = [scene_by_id[scene_id] for scene_id in segment_scene_ids if scene_id in scene_by_id]
        expected_events = [event["event_id"] for scene in segment_scenes for event in scene["events"]]
        if len(segment_scenes) != len(segment_scene_ids):
            problems.append(f"{label}.scene_ids references an unknown scene")
        if segment_event_ids != expected_events:
            problems.append(f"{label}.event_ids must exactly match the owned storyboard events in order")
        if not segment_scenes or not expected_events:
            continue
        first_scene, last_scene = segment_scenes[0], segment_scenes[-1]
        first_event, last_event = event_by_id[expected_events[0]], event_by_id[expected_events[-1]]
        first_cue = next((cue for cue in cues if _same_time(cue["start_seconds"], first_event["start_seconds"])), None)
        last_cue = next((cue for cue in reversed(cues) if _same_time(cue["end_seconds"], last_event["end_seconds"])), None)
        if first_cue is None or last_cue is None:
            problems.append(f"{label} boundary does not align with a canonical SRT cue")
            continue
        start = _boundary(segment, "start", first_scene, first_event, first_cue, problems)
        end = _boundary(segment, "end", last_scene, last_event, last_cue, problems)
        if start is not None and end is not None:
            if end <= start:
                problems.append(f"{label} must have positive duration")
            if previous_end is not None and not _same_time(start, previous_end):
                problems.append(f"{label} does not begin at the preceding segment boundary")
            previous_end = end
        selector = segment.get("selector")
        if (
            not isinstance(selector, dict)
            or set(selector) != {"kind", "fps", "start_frame", "end_frame"}
            or selector.get("kind") != "frame_range.v1"
            or not isinstance(selector.get("fps"), int)
            or isinstance(selector.get("fps"), bool)
            or selector["fps"] <= 0
            or not isinstance(selector.get("start_frame"), int)
            or isinstance(selector.get("start_frame"), bool)
            or not isinstance(selector.get("end_frame"), int)
            or isinstance(selector.get("end_frame"), bool)
            or selector["end_frame"] <= selector["start_frame"]
        ):
            problems.append(f"{label}.selector must be a fixed positive frame_range.v1 selector")
        elif start is not None and end is not None and (
            selector["start_frame"] != _frame_index(start, selector["fps"])
            or selector["end_frame"] != _frame_index(end, selector["fps"])
        ):
            problems.append(f"{label}.selector frame range does not match its canonical boundaries")
        if segment.get("output") != OUTPUTS.get(segment_id):
            problems.append(f"{label}.output must be fixed at {OUTPUTS.get(segment_id)!r}")
        definition_sha256 = _canonical_digest(segment)
        records.append({
            "segment_id": segment_id,
            "ordinal": segment.get("ordinal"),
            "narrative_role": segment.get("narrative_role"),
            "status": "planned",
            "blockers": [],
            "definition_sha256": definition_sha256,
            "current_digest": None,
            "dependency_sha256": None,
            "review_receipt": None,
            "selector": selector,
            "output": segment.get("output"),
        })
        record_sources.append({
            "segment": segment,
            "scenes": segment_scenes,
            "events": [event_by_id[event_id] for event_id in expected_events],
            "start": start,
            "end": end,
        })

    _problems("scene ownership", problems, covered_scenes, all_scene_ids)
    _problems("event ownership", problems, covered_events, all_event_ids)
    if scenes and segments and records:
        first = records[0]
        last = records[-1]
        if first.get("segment_id") == "qi":
            first_raw = segments[0].get("start") if isinstance(segments[0], dict) else None
            if not isinstance(first_raw, dict) or not _same_time(first_raw.get("seconds"), scenes[0]["start_seconds"]):
                problems.append("qi must begin at the first canonical storyboard boundary")
        if last.get("segment_id") == "he":
            last_raw = segments[-1].get("end") if isinstance(segments[-1], dict) else None
            if not isinstance(last_raw, dict) or not _same_time(last_raw.get("seconds"), scenes[-1]["end_seconds"]):
                problems.append("he must end at the final canonical storyboard boundary")

    assembly = plan.get("assembly")
    if not isinstance(assembly, dict) or set(assembly) != {"order", "transition_policy", "audio_policy"}:
        problems.append("assembly must contain exactly order, transition_policy, and audio_policy")
    else:
        if assembly["order"] != list(SEGMENTS):
            problems.append("assembly.order must be ['qi', 'cheng', 'zhuan', 'he']")
        if assembly.get("transition_policy") != TRANSITION_POLICY:
            problems.append(
                f"assembly.transition_policy must be {TRANSITION_POLICY!r}"
            )
        if assembly.get("audio_policy") != AUDIO_POLICY:
            problems.append(f"assembly.audio_policy must be {AUDIO_POLICY!r}")

    owned_cue_indexes = []
    segment_cues = []
    if len(record_sources) == len(SEGMENTS):
        for source in record_sources:
            start, end = source["start"], source["end"]
            if start is None or end is None:
                segment_cues.append([])
                continue
            owned = [
                cue
                for cue in cues
                if cue["start_seconds"] >= start - EPSILON
                and cue["end_seconds"] <= end + EPSILON
            ]
            crossing = [
                cue
                for cue in cues
                if cue["start_seconds"] < end - EPSILON
                and cue["end_seconds"] > start + EPSILON
                and cue not in owned
            ]
            if not owned:
                problems.append(
                    f"segment {source['segment'].get('segment_id', '?')} owns no complete SRT cues"
                )
            if crossing:
                problems.append(
                    f"segment {source['segment'].get('segment_id', '?')} has an SRT cue crossing its boundary"
                )
            segment_cues.append(owned)
            owned_cue_indexes.extend(cue["cue_index"] for cue in owned)
    _problems(
        "SRT cue ownership",
        problems,
        owned_cue_indexes,
        [cue["cue_index"] for cue in cues],
    )

    if not problems and len(records) == len(SEGMENTS):
        global_dependency_sha256 = _global_dependency_digest(
            current_inputs, segments, assembly, storyboard_authority
        )
        for record, source, owned_cues in zip(records, record_sources, segment_cues):
            dependency_sha256 = _segment_dependency_digest(
                source["segment"],
                record["definition_sha256"],
                global_dependency_sha256,
                source["scenes"],
                source["events"],
                owned_cues,
            )
            record["dependency_sha256"] = dependency_sha256
            record["current_digest"] = dependency_sha256
    else:
        global_dependency_sha256 = None

    if not problems and len(records) != len(SEGMENTS):
        problems.append("a valid plan must emit exactly four segment records")
    valid = not problems
    if valid:
        records[0]["actionable"] = True
        for record in records[1:]:
            record["actionable"] = False
    return {
        "mode": "segmented" if valid else "invalid_segment_plan",
        "present": True,
        "path": PLAN_PATH,
        "sha256": _sha256(plan_candidate),
        "render_input_sha256": plan.get("render_input_sha256"),
        "global_dependency_sha256": global_dependency_sha256,
        "assembly": assembly if valid else None,
        "assembly_policy_sha256": (
            _canonical_digest(
                {
                    "schema": "haru.segment_assembly_policy.v1",
                    "order": assembly["order"],
                    "transition_policy": assembly["transition_policy"],
                    "audio_policy": assembly["audio_policy"],
                }
            )
            if valid
            else None
        ),
        "problems": problems,
        "segments": records if valid else [],
        "next_actionable_segment": "qi" if valid else None,
    }
