#!/usr/bin/env python3
"""Prepare and verify structure-aware frames for final-video human QA.

Since HVP-33 this is the *second* gate on a finished render: the deterministic
render self-evaluation must already hold a current pass for the exact
`output/final.mp4` bytes before a human is asked to look at them. The v2 sample
and review receipts bind that pass by digest, so re-evaluating a render retires
the previous human verdict instead of letting it carry forward.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical_layout  # noqa: E402
import render_contract  # noqa: E402
import render_self_eval  # noqa: E402


# HVP-33 clean cutover. v1 sampling could be produced against any final bytes
# that merely carried a passing render marker; v2 additionally binds the current
# render self-evaluation, so a human is never asked to eyeball a render the
# deterministic gate has not cleared. There is no compatibility path: a v1
# receipt does not validate here, and a v2 receipt is not readable as a v1 one.
SAMPLE_SCHEMA = "haru.visual_qa_sample.v2"
REVIEW_SCHEMA = "haru.visual_qa_review.v2"
OUTPUT_DIR = Path("quality-review/visual-sampling")
SAMPLE_NAME = "visual-qa-sample.json"
REVIEW_NAME = "visual-qa-review.json"
SHEET_NAME = "visual-qa-contact-sheet.png"
INDEX_NAME = "visual-qa-index.md"
INPUT_PATHS = {
    "final_video": Path("output/final.mp4"),
    "storyboard": Path("storyboard-final-timed.json"),
    "storyboard_validation": Path("storyboard-final-timed-validation.json"),
    "srt": Path("narration-final.srt"),
}

# The self-eval gate is defined on exactly these bytes, so the candidate it
# bound is the only thing v2 may sample. `current_inputs` still discovers a
# legacy revisioned final for projects that have one, and then refuses it here
# rather than silently sampling a video nothing evaluated.
SELF_EVAL_CANDIDATE = "output/final.mp4"

# v2 shapes are exact: unknown or missing keys fail closed, which is what makes
# "old v1 receipts never validate" a property of the reader rather than a note.
SAMPLE_KEYS = frozenset(
    {"schema", "project", "created_at", "inputs", "outputs", "frames"}
)
REVIEW_KEYS = frozenset(
    {
        "schema",
        "project",
        "reviewed_by",
        "reviewed_at",
        "verdict",
        "notes",
        "final_video_sha256",
        "contact_sheet_sha256",
        "sample_sha256",
        "render_self_eval",
    }
)
REF_KEYS = frozenset({"path", "sha256", "bytes"})
SHA_RE = re.compile(r"[0-9a-f]{64}")


class SelfEvalNotCurrent(ValueError):
    """No current render self-evaluation pass authorizes whole-video QA.

    Distinct from every other input failure so status reports "the deterministic
    gate has not passed" instead of "the sample inputs changed" — a producer told
    the second thing goes looking for a corrupted receipt that is fine.
    """


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_sha256(path: Path) -> str | None:
    try:
        before = path.stat()
        digest = sha256(path)
        after = path.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            return None
        return digest
    except OSError:
        return None


def read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def timestamp_seconds(value: str) -> float:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})", value.strip())
    if not match:
        raise ValueError(f"invalid SRT timestamp: {value}")
    hours, minutes, seconds, millis = map(int, match.groups())
    if minutes >= 60 or seconds >= 60:
        raise ValueError(f"invalid SRT timestamp: {value}")
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def parse_srt(text: str) -> list[dict]:
    cues = []
    for block in re.split(r"\r?\n\r?\n+", text.strip()):
        lines = block.splitlines()
        if len(lines) < 2 or "-->" not in lines[1]:
            raise ValueError("invalid SRT cue")
        left, right = (part.strip() for part in lines[1].split("-->", 1))
        start = timestamp_seconds(left)
        end = timestamp_seconds(right)
        if end < start:
            raise ValueError("SRT cue has negative duration")
        if end == start:
            continue
        cues.append(
            {
                "index": int(lines[0]),
                "start_seconds": start,
                "end_seconds": end,
                "text": "\n".join(lines[2:]).strip(),
            }
        )
    if not cues:
        raise ValueError("SRT has no cues")
    return cues


def scene_card_type(scene: dict) -> str | None:
    explicit = scene.get("card_type")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    motion_graphic = scene.get("motion_graphic")
    if motion_graphic is not None:
        if not isinstance(motion_graphic, dict) or not isinstance(
            motion_graphic.get("template"), str
        ) or not motion_graphic["template"].strip():
            raise ValueError("motion_graphic.template is required")
        return motion_graphic["template"].strip()
    source = scene.get("source")
    if isinstance(source, str) and source.endswith(
        ("_card", "_panel", "_diagram", "_map", "_graphic")
    ):
        return source
    card = scene.get("card")
    if not (isinstance(card, (str, list)) and card):
        return None
    intent = scene.get("visual_intent")
    if isinstance(intent, str):
        match = re.match(r"\s*([A-Za-z][A-Za-z0-9_-]{1,39})", intent)
        if match:
            return match.group(1)
    return "card"


def plan_samples(
    scenes: list[dict],
    cues: list[dict],
    columns: int = 4,
    visual_motif=None,
    editorial_shots: list[dict] | None = None,
) -> list[dict]:
    if not isinstance(columns, int) or isinstance(columns, bool) or not 2 <= columns <= 8:
        raise ValueError("columns must be an integer from 2 to 8")
    if not isinstance(scenes, list) or not scenes:
        raise ValueError("storyboard has no scenes")

    planned = []
    seen_ids = set()
    for scene in scenes:
        if not isinstance(scene, dict):
            raise ValueError("storyboard scene is invalid")
        scene_id = scene.get("scene_id")
        start = scene.get("start_seconds")
        end = scene.get("end_seconds")
        if (
            not isinstance(scene_id, str)
            or not scene_id
            or scene_id in seen_ids
            or not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or end <= start
        ):
            raise ValueError("storyboard scene identity or timing is invalid")
        seen_ids.add(scene_id)
        overlaps = []
        for cue in cues:
            overlap_start = max(float(start), cue["start_seconds"])
            overlap_end = min(float(end), cue["end_seconds"])
            if overlap_end > overlap_start:
                overlaps.append((overlap_end - overlap_start, overlap_start, overlap_end, cue))
        if not overlaps:
            raise ValueError(f"{scene_id} has no cue-covered sampling interval")
        _, overlap_start, overlap_end, cue = max(
            overlaps, key=lambda item: (item[0], -item[1])
        )
        planned.append(
            {
                "scene_id": scene_id,
                "scene_type": str(scene.get("section") or scene.get("register") or "scene"),
                "card_type": scene_card_type(scene),
                "timestamp_seconds": round((overlap_start + overlap_end) / 2, 3),
                "cue_index": cue["index"],
                "cue_start_seconds": cue["start_seconds"],
                "cue_end_seconds": cue["end_seconds"],
                "reasons": ["scene"],
            }
        )

    by_id = {frame["scene_id"]: frame for frame in planned}
    first_card_types = set()
    motif_scene_ids = []
    for scene in scenes:
        card_type = scene_card_type(scene)
        if card_type and card_type not in first_card_types:
            by_id[scene["scene_id"]]["reasons"].append(f"card_type:{card_type}")
            first_card_types.add(card_type)
        if scene.get("motif") is True:
            by_id[scene["scene_id"]]["reasons"].append("motif")
            motif_scene_ids.append(scene["scene_id"])
    if visual_motif and not motif_scene_ids:
        raise ValueError("storyboard declares visual_motif but marks no motif scenes")

    cold = next(
        (scene for scene in scenes if scene.get("section") == "cold-open"), None
    )
    tail = next(
        (
            scene
            for scene in reversed(scenes)
            if scene.get("section") in {"leopard-tail", "callback", "tail"}
        ),
        None,
    )
    if cold is None or tail is None or cold["scene_id"] == tail["scene_id"]:
        raise ValueError("storyboard needs distinct cold-open and callback-tail scenes")
    for scene in (cold, tail):
        by_id[scene["scene_id"]]["reasons"].append("cold_open_callback")

    pair_ids = [cold["scene_id"], tail["scene_id"]]
    ordered = [by_id[scene_id] for scene_id in pair_ids]
    ordered.extend(frame for frame in planned if frame["scene_id"] not in pair_ids)
    seen_assets = set()
    for shot in editorial_shots or []:
        if not isinstance(shot, dict):
            raise ValueError("editorial shot is invalid")
        event_id = shot.get("event_id")
        start = shot.get("start_seconds")
        end = shot.get("end_seconds")
        asset_path = shot.get("asset_path")
        asset_sha = shot.get("asset_sha256")
        composition = shot.get("composition")
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or end <= start
            or not isinstance(asset_path, str)
            or not asset_path
            or not isinstance(asset_sha, str)
            or not SHA_RE.fullmatch(asset_sha)
            or not isinstance(composition, str)
            or not composition
        ):
            raise ValueError("editorial shot identity or timing is invalid")
        overlaps = []
        for cue in cues:
            overlap_start = max(float(start), cue["start_seconds"])
            overlap_end = min(float(end), cue["end_seconds"])
            if overlap_end > overlap_start:
                overlaps.append((overlap_end - overlap_start, overlap_start, overlap_end, cue))
        if not overlaps:
            raise ValueError(f"{event_id} has no cue-covered asset sampling interval")
        _, overlap_start, overlap_end, cue = max(
            overlaps, key=lambda item: (item[0], -item[1])
        )
        shot_assets = [("primary", asset_path, asset_sha)]
        presenter_path = shot.get("presenter_asset_path")
        presenter_sha = shot.get("presenter_asset_sha256")
        if presenter_path is not None or presenter_sha is not None:
            if (
                not isinstance(presenter_path, str)
                or not presenter_path
                or not isinstance(presenter_sha, str)
                or not SHA_RE.fullmatch(presenter_sha)
            ):
                raise ValueError("editorial presenter asset is invalid")
            shot_assets.append(("presenter", presenter_path, presenter_sha))
        for asset_kind, sampled_path, sampled_sha in shot_assets:
            if sampled_sha in seen_assets:
                continue
            seen_assets.add(sampled_sha)
            ordered.append(
                {
                    "scene_id": f"asset:{event_id}:{asset_kind}",
                    "scene_type": "editorial-asset",
                    "card_type": composition,
                    "event_id": event_id,
                    "asset_path": sampled_path,
                    "asset_sha256": sampled_sha,
                    "timestamp_seconds": round((overlap_start + overlap_end) / 2, 3),
                    "cue_index": cue["index"],
                    "cue_start_seconds": cue["start_seconds"],
                    "cue_end_seconds": cue["end_seconds"],
                    "reasons": ["asset", asset_kind, f"composition:{composition}"],
                }
            )
    for index, frame in enumerate(ordered):
        frame["index"] = index + 1
        frame["coordinate"] = {"row": index // columns, "column": index % columns}
    return ordered


def plain_file(project: Path, relative: Path) -> Path:
    path = canonical_layout.direct_path(project / relative, project)
    if path is None or not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"missing plain project file: {relative}")
    return path


def project_file(
    path: Path | None, project: Path, allow_empty: bool = False
) -> Path | None:
    if path is None:
        return None
    candidate = canonical_layout.direct_path(path, project)
    try:
        if (
            candidate is None
            or not candidate.is_file()
            or (not allow_empty and candidate.stat().st_size <= 0)
        ):
            return None
        with candidate.open("rb") as handle:
            if canonical_layout.TODO_MARKER.encode() in handle.read(4096):
                return None
        return candidate
    except OSError:
        return None


def _checks_pass(data: dict) -> bool:
    checks = data.get("checks", []) if isinstance(data, dict) else []
    if not isinstance(checks, list) or not checks:
        return False
    for check in checks:
        if check is True:
            continue
        if not isinstance(check, dict):
            return False
        status = str(check.get("status", "")).lower()
        if status in {"warn", "warning", "fail", "failed", "missing"}:
            return False
        if ("ok" in check and check["ok"] is not True) or (
            "passed" in check and check["passed"] is not True
        ):
            return False
        if status != "pass" and (
            status or not (check.get("ok") is True or check.get("passed") is True)
        ):
            return False
    return True


def current_inputs(
    project: Path,
    expected_video_sha: str | None = None,
    expected_video_path: Path | None = None,
) -> dict:
    if expected_video_path:
        video = Path(expected_video_path).resolve()
    else:
        video = project_file(project / "output/final.mp4", project)
        if video is None:
            video = render_contract.find_final_video(project, project_file)[0]
    video = project_file(video, project) if video else None
    if video is None:
        raise ValueError("missing canonical final video")
    paths = {
        name: plain_file(project, relative)
        for name, relative in INPUT_PATHS.items()
        if name != "final_video"
    }
    paths["final_video"] = video
    editorial_path = project_file(project / "editorial-contract.json", project)
    if editorial_path:
        paths["editorial_contract"] = editorial_path
    storyboard = read_json(paths["storyboard"])
    validation = read_json(paths["storyboard_validation"])
    editorial = read_json(editorial_path) if editorial_path else None
    if (
        not storyboard
        or storyboard.get("schema") != "haru.storyboard_timed.v1"
        or storyboard.get("project") != project.name
        or storyboard.get("srt", "narration-final.srt") != "narration-final.srt"
        or not isinstance(storyboard.get("scenes"), list)
    ):
        raise ValueError("timed storyboard is invalid")
    if (
        not validation
        or validation.get("schema") != "haru.storyboard_validation.v1"
        or validation.get("ok") is not True
        or not _checks_pass(validation)
    ):
        raise ValueError("timed storyboard validation is invalid")
    if editorial_path and (
        not editorial
        or editorial.get("schema") != "haru.editorial_contract.v1"
        or editorial.get("project") != project.name
        or not isinstance(editorial.get("shots"), list)
    ):
        raise ValueError("editorial contract is invalid")

    video_sha = expected_video_sha or stable_sha256(paths["final_video"])
    if not isinstance(video_sha, str) or not SHA_RE.fullmatch(video_sha):
        raise ValueError("final video digest is unavailable")
    marker = render_contract.parse_render_result(
        plain_file(project, Path(str(video.relative_to(project)) + ".render-result"))
    )
    if not render_contract.final_mix_passes(
        marker, project, paths["final_video"], video_sha
    ):
        raise ValueError("final video is not the verified mixed output")
    if editorial_path:
        editorial_sha = stable_sha256(editorial_path)
        if not editorial_sha or marker.get("editorial_contract_sha256") != editorial_sha:
            raise ValueError("final video is not bound to the editorial contract")

    # HVP-33: the deterministic gate comes first. `current_pass` is the only
    # authority here — it validates the current projection against the external
    # ledger anchor in one read, so a project-tree receipt with no anchor behind
    # it, a stale attempt, or a non-pass status all arrive as `None`.
    self_eval = render_self_eval.current_pass(project)
    if self_eval is None:
        raise SelfEvalNotCurrent(
            "render self-evaluation has no current pass for this project; "
            "whole-video visual QA cannot sample an unevaluated render"
        )
    candidate = next(
        (
            entry
            for entry in self_eval["result"].get("inputs") or []
            if isinstance(entry, dict) and entry.get("path") == SELF_EVAL_CANDIDATE
        ),
        None,
    )
    if not isinstance(candidate, dict) or candidate.get("sha256") != video_sha:
        # Reached by a project that still holds a legacy revisioned final: the
        # self-eval pass covers output/final.mp4, and sampling anything else
        # would put a human verdict on bytes no gate examined.
        raise SelfEvalNotCurrent(
            "the current render self-evaluation pass binds different final-video "
            "bytes than the video being sampled"
        )
    paths["render_self_eval"] = plain_file(
        project, Path(render_self_eval.RESULT_PATH)
    )

    cues = parse_srt(paths["srt"].read_text(encoding="utf-8"))
    digests = {}
    for name, path in paths.items():
        if name == "render_self_eval":
            # Taken from the single validated read, never re-hashed: a second
            # digest of the same path is a second answer nothing arbitrates.
            digests[name] = dict(self_eval["ref"])
            continue
        digest = video_sha if name == "final_video" else stable_sha256(path)
        if not digest:
            raise ValueError(f"{name} changed while hashing")
        digests[name] = {
            "path": str(path.relative_to(project)),
            "sha256": digest,
            "bytes": path.stat().st_size,
        }
    return {
        "paths": paths,
        "storyboard": storyboard,
        "validation": validation,
        "editorial_contract": editorial,
        "cues": cues,
        "digests": digests,
        "render_self_eval": dict(self_eval["ref"]),
    }


def extract_contact_sheet(
    ffmpeg: str, video: Path, frames: list[dict], output: Path
) -> None:
    frames_dir = output.parent / "frames"
    frames_dir.mkdir()
    for frame in frames:
        target = frames_dir / f"frame-{frame['index']:03d}.png"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{frame['timestamp_seconds']:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                "scale=480:-2",
                str(target),
            ],
            check=True,
        )
    columns = max(frame["coordinate"]["column"] for frame in frames) + 1
    rows = max(frame["coordinate"]["row"] for frame in frames) + 1
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            "1",
            "-start_number",
            "1",
            "-i",
            str(frames_dir / "frame-%03d.png"),
            "-frames:v",
            "1",
            "-vf",
            f"tile={columns}x{rows}:padding=8:margin=8:color=black",
            str(output),
        ],
        check=True,
    )


def index_markdown(project: Path, frames: list[dict]) -> str:
    lines = [
        f"# Visual QA sample — {project.name}",
        "",
        "Open and tail are the first two cells for side-by-side callback review.",
        "",
        "| Cell | Time | Scene | Scene type | Card type | Reasons |",
        "| --- | ---: | --- | --- | --- | --- |",
    ]
    for frame in frames:
        coordinate = frame["coordinate"]
        lines.append(
            "| "
            f"r{coordinate['row'] + 1}c{coordinate['column'] + 1} | "
            f"{frame['timestamp_seconds']:.3f}s | {frame['scene_id']} | "
            f"{frame['scene_type']} | {frame['card_type'] or '—'} | "
            f"{', '.join(frame['reasons'])} |"
        )
    return "\n".join(lines) + "\n"


def output_paths(project: Path) -> dict[str, Path]:
    directory = project / OUTPUT_DIR
    return {
        "directory": directory,
        "sample": directory / SAMPLE_NAME,
        "review": directory / REVIEW_NAME,
        "sheet": directory / SHEET_NAME,
        "index": directory / INDEX_NAME,
    }


def generate(project: Path, ffmpeg: str = "ffmpeg", columns: int = 4) -> dict:
    project = project.resolve()
    inputs = current_inputs(project)
    frames = plan_samples(
        inputs["storyboard"]["scenes"],
        inputs["cues"],
        columns,
        inputs["storyboard"].get("visual_motif"),
        inputs["editorial_contract"].get("shots")
        if inputs["editorial_contract"]
        else None,
    )
    paths = output_paths(project)
    review_root = project / "quality-review"
    if canonical_layout.direct_path(review_root, project) is None or review_root.is_symlink():
        raise ValueError("quality-review must be a plain project directory")
    review_root.mkdir(exist_ok=True)
    if canonical_layout.direct_path(paths["directory"], project) is None or paths[
        "directory"
    ].is_symlink():
        raise ValueError("visual sampling output must stay inside the project")
    paths["directory"].mkdir(exist_ok=True)

    executable = shutil.which(ffmpeg) if os.sep not in ffmpeg else ffmpeg
    if not executable:
        raise ValueError(f"ffmpeg not found: {ffmpeg}")
    with tempfile.TemporaryDirectory(prefix=".visual-qa-", dir=review_root) as tmp:
        staging = Path(tmp)
        sheet = staging / SHEET_NAME
        index = staging / INDEX_NAME
        sample = staging / SAMPLE_NAME
        extract_contact_sheet(executable, inputs["paths"]["final_video"], frames, sheet)
        if not sheet.is_file() or sheet.stat().st_size <= 0:
            raise ValueError("ffmpeg did not produce a contact sheet")
        index.write_text(index_markdown(project, frames), encoding="utf-8")
        receipt = {
            "schema": SAMPLE_SCHEMA,
            "project": project.name,
            "created_at": dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
            "inputs": inputs["digests"],
            "outputs": {
                "contact_sheet": {
                    "path": str(OUTPUT_DIR / SHEET_NAME),
                    "sha256": stable_sha256(sheet),
                    "bytes": sheet.stat().st_size,
                    "columns": columns,
                    "rows": math.ceil(len(frames) / columns),
                },
                "index": {
                    "path": str(OUTPUT_DIR / INDEX_NAME),
                    "sha256": stable_sha256(index),
                    "bytes": index.stat().st_size,
                },
            },
            "frames": frames,
        }
        sample.write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(sheet, paths["sheet"])
        os.replace(index, paths["index"])
        os.replace(sample, paths["sample"])
    return receipt


def _failure(code: str, message: str) -> dict:
    return {"ok": False, "code": code, "message": message}


def _entry_matches(entry, expected_path: Path, actual: Path, digest: str) -> bool:
    try:
        return bool(
            isinstance(entry, dict)
            and entry.get("path") == str(expected_path)
            and entry.get("sha256") == digest
            and isinstance(entry.get("bytes"), int)
            and not isinstance(entry.get("bytes"), bool)
            and entry.get("bytes") == actual.stat().st_size
        )
    except OSError:
        return False


def png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
        if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            return None
        width, height = struct.unpack(">II", header[16:24])
        return (width, height) if width > 0 and height > 0 else None
    except (OSError, struct.error):
        return None


def validate_sample(
    project: Path,
    expected_video_sha: str | None = None,
    expected_video_path: Path | None = None,
) -> dict:
    project = project.resolve()
    paths = output_paths(project)
    try:
        inputs = current_inputs(project, expected_video_sha, expected_video_path)
        sample_path = plain_file(project, OUTPUT_DIR / SAMPLE_NAME)
        sheet = plain_file(project, OUTPUT_DIR / SHEET_NAME)
        index = plain_file(project, OUTPUT_DIR / INDEX_NAME)
    except SelfEvalNotCurrent as exc:
        return _failure("self_eval_not_current", str(exc))
    except (OSError, ValueError) as exc:
        return _failure("sample_inputs_invalid", str(exc))
    receipt = read_json(sample_path)
    if (
        not receipt
        or receipt.get("schema") != SAMPLE_SCHEMA
        or set(receipt) != SAMPLE_KEYS
        or receipt.get("project") != project.name
    ):
        # The exact key set is what makes the v1 -> v2 cutover a reader property:
        # a v1 receipt fails on schema, and a v2 receipt with an extra field
        # smuggled in fails here rather than being read past.
        return _failure("sample_receipt_invalid", "sample receipt is malformed")
    declared_inputs = receipt.get("inputs")
    if (
        not isinstance(declared_inputs, dict)
        or set(declared_inputs) != set(inputs["digests"])
        or any(
            not isinstance(declared_inputs.get(name), dict)
            or set(declared_inputs[name]) != REF_KEYS
            or not _entry_matches(
                declared_inputs[name],
                Path(inputs["digests"][name]["path"]),
                inputs["paths"][name],
                inputs["digests"][name]["sha256"],
            )
            for name in inputs["digests"]
        )
    ):
        return _failure("sample_input_mismatch", "sample inputs changed")

    outputs = receipt.get("outputs")
    sheet_entry = outputs.get("contact_sheet") if isinstance(outputs, dict) else None
    index_entry = outputs.get("index") if isinstance(outputs, dict) else None
    sheet_sha = stable_sha256(sheet)
    index_sha = stable_sha256(index)
    if (
        not sheet_sha
        or not index_sha
        or png_dimensions(sheet) is None
        or not _entry_matches(sheet_entry, OUTPUT_DIR / SHEET_NAME, sheet, sheet_sha)
        or not _entry_matches(index_entry, OUTPUT_DIR / INDEX_NAME, index, index_sha)
        or not isinstance(sheet_entry.get("columns"), int)
        or isinstance(sheet_entry.get("columns"), bool)
        or not 2 <= sheet_entry["columns"] <= 8
    ):
        return _failure("sample_output_mismatch", "sample outputs changed")
    try:
        expected_frames = plan_samples(
            inputs["storyboard"]["scenes"],
            inputs["cues"],
            sheet_entry["columns"],
            inputs["storyboard"].get("visual_motif"),
            inputs["editorial_contract"].get("shots")
            if inputs["editorial_contract"]
            else None,
        )
    except ValueError as exc:
        return _failure("sample_plan_invalid", str(exc))
    if receipt.get("frames") != expected_frames or sheet_entry.get("rows") != math.ceil(
        len(expected_frames) / sheet_entry["columns"]
    ):
        return _failure("sample_coverage_invalid", "sample does not cover the storyboard")
    try:
        if index.read_text(encoding="utf-8") != index_markdown(project, expected_frames):
            return _failure("sample_index_invalid", "sample index does not match the plan")
    except (OSError, UnicodeDecodeError):
        return _failure("sample_index_invalid", "sample index is unreadable")
    sample_sha = stable_sha256(sample_path)
    if not sample_sha:
        return _failure("sample_receipt_changed", "sample receipt changed while hashing")
    return {
        "ok": True,
        "code": "sample_valid",
        "video_sha256": inputs["digests"]["final_video"]["sha256"],
        "contact_sheet_sha256": sheet_sha,
        "sample_sha256": sample_sha,
        "render_self_eval": dict(inputs["render_self_eval"]),
        "files": [
            str(OUTPUT_DIR / SAMPLE_NAME),
            str(OUTPUT_DIR / SHEET_NAME),
            str(OUTPUT_DIR / INDEX_NAME),
        ],
    }


def validate_review(
    project: Path,
    expected_video_sha: str | None = None,
    expected_video_path: Path | None = None,
) -> dict:
    project = project.resolve()
    sample = validate_sample(project, expected_video_sha, expected_video_path)
    if not sample["ok"]:
        return sample
    try:
        receipt_path = plain_file(project, OUTPUT_DIR / REVIEW_NAME)
    except (OSError, ValueError) as exc:
        return _failure("review_missing", str(exc))
    receipt = read_json(receipt_path)
    reviewed_at = receipt.get("reviewed_at") if receipt else None
    try:
        parsed_at = dt.datetime.fromisoformat(str(reviewed_at).replace("Z", "+00:00"))
    except ValueError:
        parsed_at = None
    if not (
        receipt
        and receipt.get("schema") == REVIEW_SCHEMA
        and set(receipt) == REVIEW_KEYS
        and receipt.get("project") == project.name
        and isinstance(receipt.get("reviewed_by"), str)
        and bool(receipt["reviewed_by"].strip())
        and parsed_at is not None
        and parsed_at.tzinfo is not None
        and receipt.get("verdict") == "pass"
        and isinstance(receipt.get("notes"), str)
        and bool(receipt["notes"].strip())
        and receipt.get("final_video_sha256") == sample["video_sha256"]
        and receipt.get("contact_sheet_sha256") == sample["contact_sheet_sha256"]
        and receipt.get("sample_sha256") == sample["sample_sha256"]
        # A v2 review is a verdict on one exact self-eval pass. Re-running
        # self-eval replaces that ref, so the previous human verdict stops
        # validating instead of carrying forward onto bytes it never saw.
        and isinstance(receipt.get("render_self_eval"), dict)
        and set(receipt["render_self_eval"]) == REF_KEYS
        and receipt["render_self_eval"] == sample["render_self_eval"]
    ):
        return _failure("review_invalid", "human review is missing, stale, or failed")
    review_sha = stable_sha256(receipt_path)
    if not review_sha:
        return _failure("review_invalid", "human review changed while hashing")
    return {
        **sample,
        "code": "visual_sampling_passed",
        "reviewed_by": receipt["reviewed_by"],
        "reviewed_at": receipt["reviewed_at"],
        # The exact ref HVP-28 binds. An approval must postdate this review, so
        # the digest of the review bytes is what the approval intent carries.
        "review_ref": {
            "path": str(OUTPUT_DIR / REVIEW_NAME),
            "sha256": review_sha,
            "bytes": receipt_path.stat().st_size,
        },
        "files": [*sample["files"], str(OUTPUT_DIR / REVIEW_NAME)],
    }


def record_review(
    project: Path, reviewed_by: str, verdict: str, notes: str
) -> dict:
    project = project.resolve()
    if not reviewed_by.strip() or verdict not in {"pass", "fail"} or not notes.strip():
        raise ValueError("reviewed-by, pass/fail verdict, and notes are required")
    sample = validate_sample(project)
    if not sample["ok"]:
        raise ValueError(sample["message"])
    receipt = {
        "schema": REVIEW_SCHEMA,
        "project": project.name,
        "reviewed_by": reviewed_by.strip(),
        "reviewed_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "verdict": verdict,
        "notes": notes.strip(),
        "final_video_sha256": sample["video_sha256"],
        "contact_sheet_sha256": sample["contact_sheet_sha256"],
        "sample_sha256": sample["sample_sha256"],
        "render_self_eval": dict(sample["render_self_eval"]),
    }
    path = output_paths(project)["review"]
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
    return receipt


def resolve_project(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_dir():
        return candidate.resolve()
    root = Path(__file__).resolve().parents[1]
    return (root / "projects" / value).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample = subparsers.add_parser("sample")
    sample.add_argument("project")
    sample.add_argument("--ffmpeg", default="ffmpeg")
    sample.add_argument("--columns", type=int, default=4)

    review = subparsers.add_parser("review")
    review.add_argument("project")
    review.add_argument("--reviewed-by", required=True)
    review.add_argument("--verdict", choices=("pass", "fail"), required=True)
    review.add_argument("--notes", required=True)

    check = subparsers.add_parser("check")
    check.add_argument("project")

    args = parser.parse_args()
    project = resolve_project(args.project)
    try:
        if args.command == "sample":
            result = generate(project, ffmpeg=args.ffmpeg, columns=args.columns)
        elif args.command == "review":
            result = record_review(
                project, args.reviewed_by, args.verdict, args.notes
            )
        else:
            result = validate_review(project)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["ok"] else 1
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        code = (
            "self_eval_not_current"
            if isinstance(exc, SelfEvalNotCurrent)
            else "visual_qa_failed"
        )
        print(
            json.dumps(
                {"ok": False, "code": code, "message": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
