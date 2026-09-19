#!/usr/bin/env python3
"""Validate the fail-closed long-form editorial production contract.

One contract, one presenter. The presenter is named by the project's
`production_profile`; everything else in this module is profile-independent.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse


class Presenter(NamedTuple):
    """Who anchors a profile, and what its A-roll shots must be tagged with."""

    id: str
    aroll_asset_role: str
    label: str


PROFILES = {
    "host_longform.v1": Presenter("host", "host_aroll", "Host"),
    "technical_host.v1": Presenter("technical_host", "technical_host_aroll", "Technical host"),
}
# Which presenter each lane is pinned to. `None` means the lane forbids one:
# only a pinned lane is gated at render, so a lane that allowed a profile without
# being gated would read as presenter-bound while skipping every presenter check.
# This is the one place the mapping lives — canonical_layout gates ingest on it,
# render_project_worker gates render on it, and agent_status is cross-checked
# against it by test. Three copies of it is how they drift apart.
LANE_PROFILES = {
    "social_issue_longform.v1": "host_longform.v1",
    "tech_longform.v1": "technical_host.v1",
    "manual.v1": None,
}

# Presenter assumed when a contract declares no usable profile: the fallback that
# lets validate_project report every other fault in one pass, the profile pinned to
# the social lane, and the default for callers scaffolding a new project.
PROFILE = "host_longform.v1"
COMPOSITIONS = {"aroll_full", "broll_full", "broll_pip", "motion_graphics"}
AROLL_MIN = 0.15
AROLL_MAX = 0.20
BROLL_MIN = 0.45
MOTION_MIN = 0.25
AROLL_MAX_GAP_SECONDS = 90.0
EXTERNAL_BROLL_KINDS = {"stock", "primary_source_capture", "news_footage"}
SOURCE_KINDS = {*EXTERNAL_BROLL_KINDS, "creator_owned"}
EXTERNAL_BROLL_MIN = 0.70
ASSET_MAX_SECONDS = 45.0
ASSET_MAX_USES = 3


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def read_object(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def direct_file(project: Path, value) -> Path | None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        return None
    candidate = project
    for part in Path(value).parts:
        if part in {"", ".", ".."}:
            return None
        candidate /= part
        if candidate.is_symlink():
            return None
    try:
        if not candidate.is_file() or not candidate.resolve().is_relative_to(project.resolve()):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def valid_video(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(32)
    except OSError:
        return False
    return len(header) >= 12 and header[4:8] == b"ftyp"


def bound_video(project: Path, path_value, sha_value) -> bool:
    path = direct_file(project, path_value)
    return bool(
        path
        and valid_video(path)
        and isinstance(sha_value, str)
        and len(sha_value) == 64
        and digest(path) == sha_value
    )


def storyboard_events(storyboard: dict):
    events = {}
    for scene in storyboard.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        for event in scene.get("visual_events", []):
            if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
                continue
            events[event["event_id"]] = event
    return events


def valid_motion_canvas_receipt(project: Path, shot: dict) -> bool:
    receipt_path = direct_file(project, shot.get("producer_receipt_path"))
    receipt = read_object(receipt_path) if receipt_path else None
    source = direct_file(project, receipt.get("source") if receipt else None)
    return bool(
        receipt
        and receipt.get("schema") == "haru.motion_canvas_render.v1"
        and receipt.get("engine") == "motion_canvas"
        and str(receipt.get("engine_version") or "").strip()
        and str(receipt.get("design_id") or "").strip()
        and str(receipt.get("semantic_purpose") or "").strip()
        and isinstance(receipt.get("cue_ids"), list)
        and shot.get("event_id") in receipt["cue_ids"]
        and receipt.get("output") == shot.get("asset_path")
        and receipt.get("output_sha256") == shot.get("asset_sha256")
        and shot.get("producer_receipt_sha256") == digest(receipt_path)
        and source
        and receipt.get("source_sha256") == digest(source)
    )


def broll_source_receipt(project: Path, shot: dict, presenter: Presenter) -> dict | None:
    receipt_path = direct_file(project, shot.get("source_receipt_path"))
    receipt = read_object(receipt_path) if receipt_path else None
    if not receipt:
        return None
    try:
        acquired_at = dt.datetime.fromisoformat(
            str(receipt.get("acquired_at", "")).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    strings = ("provider", "search_query", "license_or_usage_basis", "semantic_purpose")
    brand_characters = receipt.get("brand_characters")
    cue_ids = receipt.get("cue_ids")
    source_kind = receipt.get("source_kind")
    valid = bool(
        receipt.get("schema") == "haru.broll_source.v1"
        and shot.get("source_receipt_sha256") == digest(receipt_path)
        and receipt.get("output") == shot.get("asset_path")
        and receipt.get("output_sha256") == shot.get("asset_sha256")
        and source_kind in SOURCE_KINDS
        and acquired_at.tzinfo is not None
        and all(isinstance(receipt.get(name), str) and receipt[name].strip() for name in strings)
        and isinstance(cue_ids, list)
        and shot.get("event_id") in cue_ids
        and isinstance(brand_characters, list)
        and all(character == presenter.id for character in brand_characters)
    )
    if source_kind in EXTERNAL_BROLL_KINDS:
        source_url = receipt.get("source_url")
        parsed = urlparse(source_url) if isinstance(source_url, str) else None
        valid = valid and bool(parsed and parsed.scheme == "https" and parsed.netloc)
    else:
        valid = valid and all(
            isinstance(receipt.get(name), str) and receipt[name].strip()
            for name in ("approved_by", "source_reference", "usage_reason")
        )
    return receipt if valid else None


def validate_project(project: Path, *, require_preview: bool) -> dict:
    project = Path(project)
    problems = []
    storyboard_path = direct_file(project, "storyboard-final-timed.json")
    contract_path = direct_file(project, "editorial-contract.json")
    storyboard = read_object(storyboard_path) if storyboard_path else None
    contract = read_object(contract_path) if contract_path else None

    if not storyboard or storyboard.get("schema") != "haru.storyboard_timed.v1":
        problems.append("timed storyboard is missing or invalid")
        storyboard = {}
    if not contract or contract.get("schema") != "haru.editorial_contract.v1":
        problems.append("editorial contract is missing or invalid")
        contract = {}
    if contract.get("project") != project.name:
        problems.append("editorial contract project does not match directory")
    declared_profile = contract.get("production_profile")
    presenter = PROFILES.get(declared_profile)
    if presenter is None:
        problems.append(f"production_profile must be one of {sorted(PROFILES)}")
        # Keep validating with the default presenter so the operator sees every
        # other problem in one pass instead of one profile error at a time.
        presenter = PROFILES[PROFILE]
    if storyboard_path and contract.get("storyboard_sha256") != digest(storyboard_path):
        problems.append("editorial contract is not bound to the timed storyboard")

    # Remotion imports its contract from remotion/public/data/, not from here,
    # so the two drifting apart renders a cut nothing in this file describes --
    # every ratio, every digest, every diversity rule checked against a contract
    # the video was not made from. Only meaningful for projects that have that
    # directory; others import the root copy directly.
    render_copy = project / "remotion/public/data/editorial-contract.json"
    if (
        contract_path
        and render_copy.is_file()
        and not render_copy.is_symlink()
        and digest(render_copy) != digest(contract_path)
    ):
        problems.append(
            "the renderer's editorial contract copy does not match this one "
            "(run retime-visuals)"
        )

    events = storyboard_events(storyboard)
    shots = contract.get("shots") if isinstance(contract.get("shots"), list) else []
    shot_ids = [shot.get("event_id") for shot in shots if isinstance(shot, dict)]
    if len(shot_ids) != len(set(shot_ids)) or set(shot_ids) != set(events):
        problems.append("storyboard events and editorial shots must match exactly")

    total = storyboard.get("audio_duration_seconds")
    if not isinstance(total, (int, float)) or isinstance(total, bool) or total <= 0:
        ends = [event.get("end_seconds") for event in events.values()]
        total = max((value for value in ends if isinstance(value, (int, float))), default=0)

    seconds = {"aroll": 0.0, "broll": 0.0, "motion_graphics": 0.0}
    usage = {name: {} for name in seconds}
    motion_design_ids = set()
    external_broll_seconds = 0.0
    arroll_ranges = []
    for shot in shots:
        if not isinstance(shot, dict):
            problems.append("editorial shot must be an object")
            continue
        event_id = shot.get("event_id")
        label = event_id if isinstance(event_id, str) else "unknown-shot"
        composition = shot.get("composition")
        if composition not in COMPOSITIONS:
            problems.append(f"{label}: unsupported composition")
            continue
        event = events.get(event_id)
        start = shot.get("start_seconds")
        end = shot.get("end_seconds")
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or end <= start
        ):
            problems.append(f"{label}: invalid shot timing")
            continue
        if event and (
            abs(start - event.get("start_seconds", -1)) > 0.002
            or abs(end - event.get("end_seconds", -1)) > 0.002
        ):
            problems.append(f"{label}: shot timing does not match storyboard event")
        duration = end - start

        if not bound_video(project, shot.get("asset_path"), shot.get("asset_sha256")):
            problems.append(f"{label}: direct digest-bound video asset is required")
        if composition == "aroll_full":
            if shot.get("asset_role") != presenter.aroll_asset_role:
                problems.append(f"{label}: asset_role must be {presenter.aroll_asset_role}")
            if shot.get("presenter_id") != presenter.id:
                problems.append(f"{label}: presenter_id must be {presenter.id}")
            seconds["aroll"] += duration
            key = shot.get("asset_sha256")
            if isinstance(key, str) and len(key) == 64:
                entry = usage["aroll"].setdefault(key, {"seconds": 0.0, "uses": 0})
                entry["seconds"] += duration
                entry["uses"] += 1
            arroll_ranges.append((start, end))
        elif composition in {"broll_full", "broll_pip"}:
            if shot.get("asset_role") != "broll":
                problems.append(f"{label}: asset_role must be broll")
            seconds["broll"] += duration
            source_receipt = broll_source_receipt(project, shot, presenter)
            if not source_receipt:
                problems.append(f"{label}: valid B-roll source receipt is required")
            elif source_receipt.get("source_kind") in EXTERNAL_BROLL_KINDS:
                external_broll_seconds += duration
            key = shot.get("asset_sha256")
            if isinstance(key, str) and len(key) == 64:
                entry = usage["broll"].setdefault(key, {"seconds": 0.0, "uses": 0})
                entry["seconds"] += duration
                entry["uses"] += 1
            if composition == "broll_pip" and not bound_video(
                project,
                shot.get("presenter_asset_path"),
                shot.get("presenter_asset_sha256"),
            ):
                problems.append(
                    f"{label}: broll_pip requires a digest-bound {presenter.label} presenter video"
                )
            if composition == "broll_pip" and shot.get("presenter_id") != presenter.id:
                problems.append(f"{label}: presenter_id must be {presenter.id}")
        else:
            seconds["motion_graphics"] += duration
            if shot.get("engine") != "motion_canvas" or shot.get("asset_role") != "motion_canvas":
                problems.append("motion_graphics requires engine motion_canvas and asset_role motion_canvas")
            if not valid_motion_canvas_receipt(project, shot):
                problems.append(f"{label}: valid Motion Canvas producer receipt is required")
            receipt_path = direct_file(project, shot.get("producer_receipt_path"))
            receipt = read_object(receipt_path) if receipt_path else None
            key = shot.get("asset_sha256")
            if isinstance(key, str) and len(key) == 64:
                entry = usage["motion_graphics"].setdefault(
                    key, {"seconds": 0.0, "uses": 0}
                )
                entry["seconds"] += duration
                entry["uses"] += 1
            if receipt and isinstance(receipt.get("design_id"), str):
                motion_design_ids.add(receipt["design_id"])

    ratios = {
        name: round(value / total, 4) if total else 0.0
        for name, value in seconds.items()
    }
    if not AROLL_MIN <= ratios["aroll"] <= AROLL_MAX:
        problems.append("A-roll ratio must be between 0.15 and 0.20")
    if ratios["broll"] < BROLL_MIN:
        problems.append("B-roll ratio must be at least 0.45")
    if ratios["motion_graphics"] < MOTION_MIN:
        problems.append("Motion Canvas ratio must be at least 0.25")

    labels = {"aroll": "A-roll", "broll": "B-roll", "motion_graphics": "Motion design"}
    diversity = {}
    for mode, entries in usage.items():
        required = math.ceil(seconds[mode] / ASSET_MAX_SECONDS) if seconds[mode] else 0
        diversity[mode] = {
            "unique_assets": len(entries),
            "required_unique_assets": required,
            "max_asset_seconds": round(
                max((entry["seconds"] for entry in entries.values()), default=0.0), 3
            ),
            "max_asset_uses": max(
                (entry["uses"] for entry in entries.values()), default=0
            ),
        }
        if mode == "motion_graphics":
            diversity[mode]["unique_designs"] = len(motion_design_ids)
        if len(entries) < required:
            problems.append(f"{labels[mode]} needs at least {required} unique source assets")
        if mode == "motion_graphics" and len(motion_design_ids) < required:
            problems.append(f"Motion design needs at least {required} unique semantic designs")
        if any(entry["seconds"] > ASSET_MAX_SECONDS for entry in entries.values()):
            problems.append(
                f"{labels[mode]} asset reuse cannot exceed {ASSET_MAX_SECONDS:g} seconds"
            )
        if any(entry["uses"] > ASSET_MAX_USES for entry in entries.values()):
            problems.append(
                f"{labels[mode]} asset reuse cannot exceed {ASSET_MAX_USES} shots"
            )

    external_broll_ratio = (
        external_broll_seconds / seconds["broll"] if seconds["broll"] else 0.0
    )
    if seconds["broll"] and external_broll_ratio < EXTERNAL_BROLL_MIN:
        problems.append("At least 70% of B-roll must come from external live sourcing")

    cursor = 0.0
    for start, end in sorted(arroll_ranges):
        if start - cursor > AROLL_MAX_GAP_SECONDS:
            problems.append(
                f"{presenter.label} A-roll anchors must appear at least every "
                f"{AROLL_MAX_GAP_SECONDS:g} seconds"
            )
            break
        cursor = end
    else:
        if total - cursor > AROLL_MAX_GAP_SECONDS:
            problems.append(
                f"{presenter.label} A-roll anchors must appear at least every "
                f"{AROLL_MAX_GAP_SECONDS:g} seconds"
            )

    if require_preview:
        review_path = direct_file(project, "quality-review/editorial-preview/review.json")
        review = read_object(review_path) if review_path else None
        if not review or review.get("schema") != "haru.editorial_preview_review.v1":
            problems.append("editorial preview review is missing or invalid")
        else:
            preview = direct_file(project, review.get("preview"))
            if not preview or not bound_video(project, review.get("preview"), review.get("preview_sha256")):
                problems.append("editorial preview is not a digest-bound video")
            duration = review.get("duration_seconds")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not 60 <= duration <= 90:
                problems.append("editorial preview duration must be between 60 and 90 seconds")
            if not contract_path or review.get("editorial_contract_sha256") != digest(contract_path):
                problems.append("editorial preview review is not bound to the editorial contract")
            if review.get("verdict") != "pass" or not str(review.get("reviewed_by") or "").strip():
                problems.append("editorial preview must have an explicit pass verdict and reviewer")

    return {
        "schema": "haru.editorial_contract_validation.v1",
        "profile": declared_profile,
        "ok": not problems,
        "problems": problems,
        "ratios": ratios,
        "sourcing": {"external_broll_ratio": round(external_broll_ratio, 4)},
        "diversity": diversity,
    }
