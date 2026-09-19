#!/usr/bin/env python3
"""Render and review the earliest actionable narrative segment.

The caller never chooses an executable, output path, or frame range: those
come from the current ``haru.segment_plan.v1`` authority. Production advances
strictly through qi, cheng, zhuan, and he; assembly remains a separate gate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical_layout
import segment_plan

RENDER_SCHEMA = "haru.segment_render.v1"
REVIEW_SCHEMA = "haru.segment_review.v1"
EVIDENCE_SCHEMA = "haru.segment_review_evidence.v1"
REVIEW_VERDICTS = ("pass", "changes_requested")


class SegmentError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomically(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def write_bytes_atomically(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def direct_file(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise SegmentError("invalid_path", f"expected a direct file: {path.name}")
    return path.resolve(strict=True)


def direct_directory(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise SegmentError("invalid_path", "expected a direct directory")
    return path.resolve(strict=True)


def valid_reviewed_at(value) -> bool:
    if not isinstance(value, str) or len(value) != 25 or not value.endswith("+00:00"):
        return False
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S+00:00")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%S+00:00") == value


def review_dir(segment_id: str) -> Path:
    return Path("quality-review") / "segments" / segment_id


def render_receipt_path(project: Path, segment_id: str) -> Path:
    return project / review_dir(segment_id) / "render.json"


def review_receipt_path(project: Path, segment_id: str) -> Path:
    return project / review_dir(segment_id) / "review.json"


def evidence_path(project: Path, segment_id: str) -> Path:
    return project / review_dir(segment_id) / "evidence.json"


def prepare_review_dir(project: Path, segment_id: str) -> Path:
    target = project / review_dir(segment_id)
    if target.is_symlink():
        raise SegmentError(
            "invalid_path", "segment review directory must not be a symlink"
        )
    target.mkdir(parents=True, exist_ok=True)
    candidate = canonical_layout.direct_path(target, project)
    if candidate is None or candidate.is_symlink() or not candidate.is_dir():
        raise SegmentError("invalid_path", "segment review directory must be direct")
    return candidate


def current_segment(project: Path) -> dict:
    plan = apply_lifecycle(segment_plan.validate(project), project)
    if plan["mode"] != "segmented":
        raise SegmentError(
            "segment_plan_invalid", "a valid four-act segment plan is required"
        )
    segment_id = plan.get("next_actionable_segment")
    if segment_id is None:
        raise SegmentError(
            "segment_not_actionable", "all four segments have current approvals"
        )
    if segment_id not in segment_plan.SEGMENTS:
        raise SegmentError(
            "segment_plan_invalid", "the actionable segment is not canonical"
        )
    records = plan.get("segments") or []
    if len(records) != len(segment_plan.SEGMENTS):
        raise SegmentError(
            "segment_plan_invalid",
            "a valid plan must emit exactly four segment records",
        )
    record = next(
        (item for item in records if item.get("segment_id") == segment_id), None
    )
    if record is None:
        raise SegmentError(
            "segment_plan_invalid", f"{segment_id} is missing from the validated plan"
        )
    selector = record.get("selector") or {}
    if (
        selector.get("kind") != "frame_range.v1"
        or not isinstance(selector.get("start_frame"), int)
        or not isinstance(selector.get("end_frame"), int)
        or selector["end_frame"] <= selector["start_frame"]
    ):
        raise SegmentError(
            "invalid_selector", f"{segment_id} selector is not a fixed frame_range.v1"
        )
    if record.get("output") != segment_plan.OUTPUTS[segment_id]:
        raise SegmentError(
            "invalid_output", f"{segment_id} output is not the canonical path"
        )
    return {"plan": plan, "record": record}


def read_render_plan(project: Path) -> dict:
    path = canonical_layout.direct_path(project / "render_plan.json", project)
    if path is None or path.is_symlink() or not path.is_file():
        raise SegmentError(
            "invalid_render_plan", "render_plan.json must be a direct project file"
        )
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SegmentError(
            "invalid_render_plan", f"cannot parse render_plan.json: {exc}"
        ) from exc
    composition = plan.get("composition")
    remotion_dir = plan.get("remotion_dir")
    if not isinstance(composition, str) or not composition:
        raise SegmentError(
            "invalid_render_plan", "render plan does not name a composition"
        )
    if not isinstance(remotion_dir, str) or not remotion_dir:
        raise SegmentError(
            "invalid_render_plan", "render plan does not name remotion_dir"
        )
    remotion = canonical_layout.direct_path(project / remotion_dir, project)
    if remotion is None or remotion.is_symlink() or not remotion.is_dir():
        raise SegmentError(
            "invalid_render_plan", "remotion_dir must be a direct project directory"
        )
    return {"composition": composition, "remotion": remotion}


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        duration = float((result.stdout or "").strip())
    except ValueError as exc:
        raise SegmentError(
            "segment_unmeasurable", f"cannot measure {path.name}"
        ) from exc
    if result.returncode != 0 or duration <= 0:
        raise SegmentError("segment_unmeasurable", f"cannot measure {path.name}")
    return duration


def normalize_stream_profile(value) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("streams"), list):
        raise SegmentError("segment_unmeasurable", "invalid stream profile")
    videos = [
        stream
        for stream in value["streams"]
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    audios = [
        stream
        for stream in value["streams"]
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if len(value["streams"]) != 2 or len(videos) != 1 or len(audios) != 1:
        raise SegmentError(
            "segment_unmeasurable",
            "segment must contain exactly one video stream and one audio stream",
        )
    video = videos[0]
    frame_rate = (
        video.get("frame_rate")
        or video.get("avg_frame_rate")
        or video.get("r_frame_rate")
    )
    if (
        not isinstance(video.get("codec_name"), str)
        or isinstance(video.get("width"), bool)
        or not isinstance(video.get("width"), int)
        or video["width"] <= 0
        or isinstance(video.get("height"), bool)
        or not isinstance(video.get("height"), int)
        or video["height"] <= 0
        or not isinstance(video.get("pix_fmt"), str)
        or not isinstance(frame_rate, str)
        or not frame_rate
    ):
        raise SegmentError("segment_unmeasurable", "invalid video stream profile")
    video_profile = {
        "codec_type": "video",
        "codec_name": video["codec_name"],
        "codec_tag_string": video.get("codec_tag_string"),
        "extradata_hash": video.get("extradata_hash"),
        "time_base": video.get("time_base"),
        "width": video["width"],
        "height": video["height"],
        "pix_fmt": video["pix_fmt"],
        "field_order": video.get("field_order"),
        "sample_aspect_ratio": video.get("sample_aspect_ratio"),
        "frame_rate": frame_rate,
        "color_range": video.get("color_range"),
        "color_space": video.get("color_space"),
        "color_transfer": video.get("color_transfer"),
        "color_primaries": video.get("color_primaries"),
    }
    streams = [video_profile]
    audio = audios[0]
    try:
        sample_rate = int(audio.get("sample_rate"))
        channels = int(audio.get("channels"))
    except (TypeError, ValueError) as error:
        raise SegmentError(
            "segment_unmeasurable", "invalid audio stream profile"
        ) from error
    channel_layout = audio.get("channel_layout")
    if (
        not isinstance(audio.get("codec_name"), str)
        or sample_rate <= 0
        or channels <= 0
        or not isinstance(channel_layout, str)
        or not channel_layout
    ):
        raise SegmentError("segment_unmeasurable", "invalid audio stream profile")
    audio_profile = {
        "codec_type": "audio",
        "codec_name": audio["codec_name"],
        "codec_tag_string": audio.get("codec_tag_string"),
        "extradata_hash": audio.get("extradata_hash"),
        "time_base": audio.get("time_base"),
        "sample_rate": sample_rate,
        "channels": channels,
        "channel_layout": channel_layout,
        "sample_fmt": audio.get("sample_fmt"),
    }
    streams.append(audio_profile)
    return {"streams": streams}


def probe_stream_profile(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_data_hash",
            "sha256",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        value = json.loads(result.stdout or "")
    except json.JSONDecodeError as error:
        raise SegmentError(
            "segment_unmeasurable", f"cannot probe {path.name}"
        ) from error
    if result.returncode != 0:
        raise SegmentError("segment_unmeasurable", f"cannot probe {path.name}")
    return normalize_stream_profile(value)


def decode_clean(path: Path) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and not (result.stderr or "").strip()


def default_renderer(
    remotion: Path, composition: str, output: Path, selector: dict, home: Path
) -> None:
    first = selector["start_frame"]
    last = selector["end_frame"] - 1
    result = subprocess.run(
        [
            "npx",
            "remotion",
            "render",
            composition,
            str(output),
            f"--frames={first}-{last}",
            "--concurrency=1",
        ],
        cwd=str(remotion),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        env={
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "npm_config_offline": "true",
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
    )
    if result.returncode != 0 or not output.is_file():
        tail = (result.stderr or result.stdout or "no output").strip()[-800:]
        raise SegmentError("segment_render_failed", f"remotion render failed: {tail}")


def evidence_offsets(project: Path, record: dict, duration: float) -> dict[str, float]:
    selector = record["selector"]
    fps = selector["fps"]
    source_start = selector["start_frame"] / fps
    frame_count = selector["end_frame"] - selector["start_frame"]
    video_duration = frame_count / fps
    tail = max(0.0, min(duration, video_duration) - (2 / fps))
    caption = min(duration / 4, tail)
    transition = min(duration / 2, tail)
    raw_plan = read_direct_json(project / segment_plan.PLAN_PATH, project)
    storyboard = read_direct_json(
        project / segment_plan.INPUT_PATHS["storyboard"], project
    )
    if raw_plan is not None and storyboard is not None:
        raw_segment = next(
            (
                item
                for item in raw_plan.get("segments", [])
                if isinstance(item, dict)
                and item.get("segment_id") == record["segment_id"]
            ),
            None,
        )
        scenes = storyboard.get("scenes")
        if isinstance(raw_segment, dict) and isinstance(scenes, list):
            owned = raw_segment.get("scene_ids")
            if isinstance(owned, list) and len(owned) > 1:
                transition_scene = next(
                    (
                        scene
                        for scene in scenes
                        if isinstance(scene, dict) and scene.get("scene_id") == owned[1]
                    ),
                    None,
                )
                if isinstance(transition_scene, dict):
                    candidate = transition_scene.get("start_seconds")
                    if isinstance(candidate, (int, float)) and not isinstance(
                        candidate, bool
                    ):
                        transition = min(
                            max(float(candidate) - source_start, 0.0), tail
                        )
    cues, _error = segment_plan._parse_srt(project / segment_plan.INPUT_PATHS["srt"])
    target = source_start + duration / 4
    caption_cue = next(
        (
            cue
            for cue in cues
            if cue["start_seconds"] >= target
            and cue["start_seconds"] <= source_start + duration
        ),
        None,
    )
    if caption_cue is not None:
        caption = min(max(caption_cue["start_seconds"] - source_start, 0.0), tail)
    return {
        "head": 0.0,
        "tail": tail,
        "authored_transition": transition,
        "caption_window": caption,
    }


def render_evidence_assets(
    project: Path, record: dict, video: Path, duration: float
) -> list[dict]:
    root = prepare_review_dir(project, record["segment_id"])
    work_root = project / ".hvp"
    work = Path(tempfile.mkdtemp(prefix="segment-evidence-", dir=work_root))
    samples = []
    names = {
        "head": "head.jpg",
        "tail": "tail.jpg",
        "authored_transition": "authored-transition.jpg",
        "caption_window": "caption-window.jpg",
    }
    try:
        for kind, seconds in evidence_offsets(project, record, duration).items():
            raw = work / names[kind]
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{seconds:.6f}",
                    "-i",
                    str(video),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(raw),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0 or not raw.is_file() or raw.stat().st_size <= 0:
                raise SegmentError(
                    "segment_evidence_failed",
                    f"cannot extract {kind}: {(result.stderr or 'no output').strip()[-400:]}",
                )
            target = root / names[kind]
            write_bytes_atomically(target, raw.read_bytes())
            samples.append(
                {
                    "kind": kind,
                    "seconds": seconds,
                    "path": str(review_dir(record["segment_id"]) / names[kind]),
                    "sha256": sha256(target),
                    "bytes": target.stat().st_size,
                }
            )
        raw_waveform = work / "waveform.png"
        waveform = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(video),
                "-filter_complex",
                "aformat=channel_layouts=mono,showwavespic=s=1600x240:colors=white",
                "-frames:v",
                "1",
                str(raw_waveform),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if (
            waveform.returncode != 0
            or not raw_waveform.is_file()
            or raw_waveform.stat().st_size <= 0
        ):
            raise SegmentError(
                "segment_evidence_failed",
                f"cannot render waveform: {(waveform.stderr or 'no output').strip()[-400:]}",
            )
        waveform_target = root / "waveform.png"
        write_bytes_atomically(waveform_target, raw_waveform.read_bytes())
        samples.append(
            {
                "kind": "waveform_window",
                "seconds": 0.0,
                "duration_seconds": duration,
                "path": str(review_dir(record["segment_id"]) / "waveform.png"),
                "sha256": sha256(waveform_target),
                "bytes": waveform_target.stat().st_size,
            }
        )
        return samples
    finally:
        for child in work.iterdir():
            try:
                child.unlink()
            except OSError:
                pass
        try:
            work.rmdir()
        except OSError:
            pass


def write_evidence(
    project: Path, record: dict, video: Path, duration: float, *, generator=None
) -> dict:
    samples = (generator or render_evidence_assets)(project, record, video, duration)
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "segment_id": record["segment_id"],
        "video": record["output"],
        "video_sha256": sha256(video),
        "definition_sha256": record["definition_sha256"],
        "dependency_sha256": record["dependency_sha256"],
        "samples": samples,
    }
    write_json_atomically(evidence_path(project, record["segment_id"]), evidence)
    return evidence


def read_direct_json(path: Path, project: Path) -> dict | None:
    candidate = canonical_layout.direct_path(path, project)
    if candidate is None or candidate.is_symlink() or not candidate.is_file():
        return None
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def current_render(
    project: Path, plan: dict, record: dict
) -> tuple[dict, Path, Path] | None:
    segment_id = record.get("segment_id")
    output = record.get("output")
    if not isinstance(segment_id, str) or not isinstance(output, str):
        return None
    render = read_direct_json(render_receipt_path(project, segment_id), project)
    evidence_file = evidence_path(project, segment_id)
    evidence = read_direct_json(evidence_file, project)
    video = canonical_layout.direct_path(project / output, project)
    if render is None or evidence is None or video is None:
        return None
    if video.is_symlink() or not video.is_file():
        return None
    evidence_ref = render.get("evidence")
    samples = evidence.get("samples")
    required_samples = {
        "head",
        "tail",
        "authored_transition",
        "caption_window",
        "waveform_window",
    }
    expected_assets = {
        "head": str(review_dir(segment_id) / "head.jpg"),
        "tail": str(review_dir(segment_id) / "tail.jpg"),
        "authored_transition": str(review_dir(segment_id) / "authored-transition.jpg"),
        "caption_window": str(review_dir(segment_id) / "caption-window.jpg"),
        "waveform_window": str(review_dir(segment_id) / "waveform.png"),
    }
    sample_assets_current = isinstance(samples, list) and len(samples) == len(
        expected_assets
    )
    if sample_assets_current:
        for sample in samples:
            if not isinstance(sample, dict):
                sample_assets_current = False
                break
            kind = sample.get("kind")
            relative = sample.get("path")
            asset = (
                canonical_layout.direct_path(project / relative, project)
                if isinstance(relative, str)
                else None
            )
            if (
                kind not in expected_assets
                or relative != expected_assets[kind]
                or asset is None
                or asset.is_symlink()
                or not asset.is_file()
                or sample.get("sha256") != sha256(asset)
                or sample.get("bytes") != asset.stat().st_size
                or isinstance(sample.get("seconds"), bool)
                or not isinstance(sample.get("seconds"), (int, float))
                or sample["seconds"] < 0
                or not math.isfinite(sample["seconds"])
            ):
                sample_assets_current = False
                break
    try:
        stream_profile_current = render.get("stream_profile") == probe_stream_profile(
            video
        )
    except (OSError, SegmentError):
        stream_profile_current = False
    if (
        render.get("schema") != RENDER_SCHEMA
        or render.get("status") != "render_complete"
        or render.get("project") != project.name
        or render.get("segment_id") != segment_id
        or render.get("output") != output
        or render.get("definition_sha256") != record.get("definition_sha256")
        or render.get("dependency_sha256") != record.get("dependency_sha256")
        or render.get("selector") != record.get("selector")
        or render.get("decode_clean") is not True
        or isinstance(render.get("duration_seconds"), bool)
        or not isinstance(render.get("duration_seconds"), (int, float))
        or render["duration_seconds"] <= 0
        or not math.isfinite(render["duration_seconds"])
        or render.get("video_sha256") != sha256(video)
        or render.get("bytes") != video.stat().st_size
        or not stream_profile_current
        or not isinstance(evidence_ref, dict)
        or evidence_ref.get("path") != str(review_dir(segment_id) / "evidence.json")
        or evidence_ref.get("sha256") != sha256(evidence_file)
        or evidence.get("schema") != EVIDENCE_SCHEMA
        or evidence.get("segment_id") != segment_id
        or evidence.get("video") != output
        or evidence.get("video_sha256") != render["video_sha256"]
        or evidence.get("definition_sha256") != record.get("definition_sha256")
        or evidence.get("dependency_sha256") != record.get("dependency_sha256")
        or not sample_assets_current
        or {item.get("kind") for item in samples} != required_samples
    ):
        return None
    return render, video, evidence_file


def render_current_segment(
    project_path: Path, *, renderer=None, evidence_generator=None
) -> dict:
    project = direct_directory(project_path)
    current = current_segment(project)
    record = current["record"]
    render_plan = read_render_plan(project)
    output = project / record["output"]
    if output.is_symlink():
        raise SegmentError("invalid_path", "segment output must not be a symlink")
    if canonical_layout.direct_path(output, project) is None:
        raise SegmentError("invalid_path", "segment output escapes the project")
    state = project / ".hvp"
    state.mkdir(exist_ok=True)
    home = (
        direct_directory(state)
        if state.is_dir() and not state.is_symlink()
        else project
    )
    work = Path(tempfile.mkdtemp(dir=str(state if state.is_dir() else project)))
    raw = work / "segment.mp4"
    try:
        (renderer or default_renderer)(
            render_plan["remotion"],
            render_plan["composition"],
            raw,
            record["selector"],
            home,
        )
        if raw.is_symlink() or not raw.is_file() or raw.stat().st_size <= 0:
            raise SegmentError(
                "segment_render_failed", "renderer produced no segment bytes"
            )
        duration = probe_duration(raw)
        if not decode_clean(raw):
            raise SegmentError(
                "segment_decode_failed", "segment failed a full decode scan"
            )
        payload = raw.read_bytes()
        write_bytes_atomically(output, payload)
    finally:
        try:
            raw.unlink()
        except OSError:
            pass
        try:
            work.rmdir()
        except OSError:
            pass
    video = direct_file(output)
    stream_profile = probe_stream_profile(video)
    write_evidence(project, record, video, duration, generator=evidence_generator)
    receipt = {
        "schema": RENDER_SCHEMA,
        "status": "render_complete",
        "project": project.name,
        "segment_id": record["segment_id"],
        "output": record["output"],
        "video_sha256": sha256(video),
        "bytes": video.stat().st_size,
        "duration_seconds": duration,
        "decode_clean": True,
        "stream_profile": stream_profile,
        "definition_sha256": record["definition_sha256"],
        "dependency_sha256": record["dependency_sha256"],
        "selector": record["selector"],
        "evidence": {
            "path": str(review_dir(record["segment_id"]) / "evidence.json"),
            "sha256": sha256(evidence_path(project, record["segment_id"])),
        },
        "rendered_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
    }
    write_json_atomically(render_receipt_path(project, record["segment_id"]), receipt)
    review = review_receipt_path(project, record["segment_id"])
    if review.exists() or review.is_symlink():
        try:
            review.unlink()
        except OSError:
            pass
    return receipt


def review_current_segment(
    project_path: Path, reviewed_by: str, verdict: str, notes: str
) -> dict:
    project = direct_directory(project_path)
    if not reviewed_by.strip() or verdict not in REVIEW_VERDICTS or not notes.strip():
        raise SegmentError(
            "invalid_review",
            "a named reviewer, pass or changes_requested, and notes are required",
        )
    current = current_segment(project)
    record = current["record"]
    rendered = current_render(project, current["plan"], record)
    if rendered is None:
        raise SegmentError(
            "segment_stale",
            f"the rendered {record['segment_id']} segment is not the current plan definition",
        )
    receipt, _video, evidence_file = rendered
    review = {
        "schema": REVIEW_SCHEMA,
        "project": project.name,
        "segment_id": record["segment_id"],
        "output": record["output"],
        "video_sha256": receipt["video_sha256"],
        "definition_sha256": record["definition_sha256"],
        "dependency_sha256": record["dependency_sha256"],
        "evidence_sha256": sha256(evidence_file),
        "verdict": verdict,
        "reviewed_by": reviewed_by.strip(),
        "reviewed_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "notes": notes.strip(),
    }
    write_json_atomically(review_receipt_path(project, record["segment_id"]), review)
    return review


def apply_lifecycle(plan: dict, project: Path) -> dict:
    if plan.get("mode") != "segmented":
        return plan
    records = []
    for record in plan.get("segments") or []:
        updated = dict(record)
        segment_id = updated.get("segment_id")
        review_file = review_receipt_path(project, segment_id)
        rendered = current_render(project, plan, updated)
        render = rendered[0] if rendered is not None else None
        evidence_file = rendered[2] if rendered is not None else None
        review = read_direct_json(review_file, project)
        if render is not None and review is not None and evidence_file is not None:
            review_current = (
                review.get("schema") == REVIEW_SCHEMA
                and review.get("project") == project.name
                and review.get("segment_id") == segment_id
                and review.get("output") == updated.get("output")
                and review.get("video_sha256") == render["video_sha256"]
                and review.get("definition_sha256") == updated.get("definition_sha256")
                and review.get("dependency_sha256") == updated.get("dependency_sha256")
                and review.get("evidence_sha256") == sha256(evidence_file)
                and review.get("verdict") in REVIEW_VERDICTS
                and isinstance(review.get("reviewed_by"), str)
                and bool(review["reviewed_by"].strip())
                and valid_reviewed_at(review.get("reviewed_at"))
                and isinstance(review.get("notes"), str)
                and bool(review["notes"].strip())
            )
            if review_current and review.get("verdict") == "pass":
                updated["status"] = "approved"
                updated["review_receipt"] = str(review_dir(segment_id) / "review.json")
            elif review_current and review.get("verdict") == "changes_requested":
                updated["status"] = "changes_requested"
                updated["review_receipt"] = str(review_dir(segment_id) / "review.json")
            else:
                updated["status"] = "review_pending"
        elif render is not None:
            updated["status"] = "review_pending"
        records.append(updated)
    next_actionable = next(
        (
            record["segment_id"]
            for record in records
            if record.get("status") != "approved"
        ),
        None,
    )
    for record in records:
        record["actionable"] = record.get("segment_id") == next_actionable
    updated = dict(plan)
    updated["segments"] = records
    updated["next_actionable_segment"] = next_actionable
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("project", type=Path)
    review_parser = subparsers.add_parser("review")
    review_parser.add_argument("project", type=Path)
    review_parser.add_argument("--reviewed-by", required=True)
    review_parser.add_argument(
        "--verdict", required=True, choices=list(REVIEW_VERDICTS)
    )
    review_parser.add_argument("--notes", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "render":
            value = render_current_segment(args.project)
        else:
            value = review_current_segment(
                args.project, args.reviewed_by, args.verdict, args.notes
            )
    except SegmentError as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "outcome": "error",
                    "code": exc.code,
                    "data": None,
                }
            )
        )
        return 3
    print(json.dumps(value, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
