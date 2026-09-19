#!/usr/bin/env python3
"""Produce digest-bound final-video quality evidence without inventing a verdict.

This runner performs the mechanical whole-file checks required by the status
reader, then carries forward the already-recorded HVP-21 human visual verdict.
It never accepts reviewer input and therefore cannot manufacture a new human
decision.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical_layout  # noqa: E402
import final_quality_authority  # noqa: E402
import render_contract  # noqa: E402
import render_self_eval  # noqa: E402
import visual_qa_sample  # noqa: E402


RESULT_SCHEMA = "haru.final_quality_review.v1"
PREP_SCHEMA = "haru.final_quality_prep.v1"
REVIEW_SCHEMA = "haru.quality_review.v1"
OUTPUT_DIR = Path("quality-review/final-v1")
PREP_PATH = OUTPUT_DIR / "prep.json"
REVIEW_PATH = OUTPUT_DIR / "review.json"
VIDEO_PATH = Path("output/final.mp4")
MARKER_PATH = Path("output/final.mp4.render-result")
REQUIRED_CHECKS = (
    "duration_and_decode_gate",
    "static_frame_gate",
    "black_frame_scan",
    "visual_spot_check",
)
BLACK_LINE = re.compile(
    r"black_start:(?P<start>[0-9.]+)\s+black_end:(?P<end>[0-9.]+)\s+"
    r"black_duration:(?P<duration>[0-9.]+)"
)
FREEZE_START = re.compile(r"freeze_start:\s*(?P<value>-?[0-9.]+)")
FREEZE_DURATION = re.compile(r"freeze_duration:\s*(?P<value>[0-9.]+)")


class QualityReviewError(ValueError):
    """A closed quality gate with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_ref(path: Path, project: Path, relative: Path) -> dict:
    safe = canonical_layout.direct_path(project / relative, project)
    if safe is None or safe != path or not safe.is_file() or safe.stat().st_size <= 0:
        raise QualityReviewError(
            "unsafe_project_path", f"missing plain project file: {relative}"
        )
    try:
        before = safe.stat()
        digest = sha256(safe)
        after = safe.stat()
    except OSError as exc:
        raise QualityReviewError(
            "input_unreadable", f"cannot read {relative}: {exc}"
        ) from exc
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after:
        raise QualityReviewError(
            "inputs_changed", f"{relative} changed while it was read"
        )
    return {"path": str(relative), "sha256": digest, "bytes": after.st_size}


def resolve_project(value: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.exists():
        raw = Path(__file__).resolve().parents[1] / "projects" / value
    if raw.is_symlink():
        raise QualityReviewError(
            "unsafe_project_path", "project root must not be a symlink"
        )
    try:
        project = raw.resolve(strict=True)
    except OSError as exc:
        raise QualityReviewError(
            "project_missing", f"project does not exist: {raw}"
        ) from exc
    if not project.is_dir():
        raise QualityReviewError(
            "project_missing", f"project is not a directory: {raw}"
        )
    return project


def executable(name: str) -> str:
    # The MCP ProcessExecutor intentionally clears the caller environment and
    # fixes PATH to /usr/bin:/bin. FFmpeg is normally installed by Homebrew on
    # macOS, so search only the reviewed system/Homebrew locations instead of
    # accepting a caller-provided executable path or environment override.
    search_path = os.pathsep.join(
        [*os.get_exec_path(), "/opt/homebrew/bin", "/usr/local/bin"]
    )
    found = shutil.which(name, path=search_path)
    if not found:
        raise QualityReviewError(
            "tool_unavailable", f"required executable not found: {name}"
        )
    candidate = Path(found)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise QualityReviewError(
            "tool_unavailable", f"required executable not found: {name}"
        ) from exc
    if not resolved.is_file() or not os.access(candidate, os.X_OK):
        raise QualityReviewError(
            "tool_unavailable", f"required executable not found: {name}"
        )
    return str(candidate.absolute())


def run(command: list[str], code: str) -> subprocess.CompletedProcess:
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise QualityReviewError(code, f"media check failed{suffix}")
    return completed


def parse_rate(value) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        numerator, denominator = value.split("/", 1)
        rate = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return rate if rate > 0 else 0.0


def probe(ffprobe: str, video: Path) -> dict:
    completed = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name:stream=codec_type,codec_name,width,height,avg_frame_rate",
            "-of",
            "json",
            str(video),
        ],
        "probe_failed",
    )
    try:
        value = json.loads(completed.stdout)
        streams = value["streams"]
        duration = float(value["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise QualityReviewError(
            "probe_failed", "ffprobe returned invalid media metadata"
        ) from exc
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    if len(video_streams) != 1 or duration <= 0:
        raise QualityReviewError(
            "probe_failed", "final video needs one video stream and positive duration"
        )
    stream = video_streams[0]
    width = stream.get("width")
    height = stream.get("height")
    fps = parse_rate(stream.get("avg_frame_rate"))
    if (
        not all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0
            for item in (width, height)
        )
        or fps <= 0
    ):
        raise QualityReviewError(
            "probe_failed", "final video dimensions or frame rate are invalid"
        )
    return {
        "width": width,
        "height": height,
        "fps": round(fps, 6),
        "duration": round(duration, 6),
        "vcodec": stream.get("codec_name"),
        "acodec": audio_streams[0].get("codec_name") if audio_streams else None,
        "has_audio": bool(audio_streams),
        "container": value["format"].get("format_name"),
    }


def media_checks(ffmpeg: str, video: Path) -> dict:
    run(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(video),
            "-map",
            "0:v",
            "-map",
            "0:a?",
            "-f",
            "null",
            "-",
        ],
        "decode_failed",
    )
    black = run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-an",
            "-vf",
            "blackdetect=pix_th=0.10:pic_th=0.98:d=0.5",
            "-f",
            "null",
            "-",
        ],
        "black_scan_failed",
    )
    black_intervals = [
        {key: float(match.group(key)) for key in ("start", "end", "duration")}
        for match in BLACK_LINE.finditer(black.stderr)
    ]
    if black_intervals:
        raise QualityReviewError(
            "black_frame_detected", "black scan found a segment of at least 0.5 seconds"
        )

    freeze = run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(video),
            "-an",
            "-vf",
            "freezedetect=n=-30dB:d=60",
            "-f",
            "null",
            "-",
        ],
        "static_scan_failed",
    )
    starts = [
        float(match.group("value")) for match in FREEZE_START.finditer(freeze.stderr)
    ]
    durations = [
        float(match.group("value")) for match in FREEZE_DURATION.finditer(freeze.stderr)
    ]
    if starts or durations:
        raise QualityReviewError(
            "static_frame_detected",
            "static-frame scan found a run of at least 60 seconds",
        )
    return {
        "decode": {"full_decode_clean": True},
        "black": {
            "filter": "blackdetect=pix_th=0.10:pic_th=0.98:d=0.5",
            "intervals": [],
        },
        "static": {"filter": "freezedetect=n=-30dB:d=60", "intervals": []},
    }


def output_ref(path: Path, project: Path, relative: Path) -> dict:
    return stable_ref(path, project, relative)


def project_file(path: Path | None, project: Path, allow_empty: bool = False):
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
    except OSError:
        return None
    return candidate


def reject_newer_human_hold(project: Path, video_sha: str, visual_ref: dict) -> None:
    _video, review_path, review = render_contract.find_final_video(
        project, project_file
    )
    visual_path = project_file(project / visual_ref["path"], project)
    selected = project_file(review_path, project, allow_empty=True)
    if not selected or not visual_path or not isinstance(review, dict):
        return
    try:
        newer = selected.stat().st_mtime > render_contract.human_review_time(project, project_file)
    except OSError:
        newer = True
    if (
        newer
        and review.get("publish_readiness") == "hold"
        and review.get("video_sha256") == video_sha
    ):
        raise QualityReviewError(
            "newer_human_hold",
            "a newer human hold exists for the current video; record a fresh HVP-21 visual review before final quality review",
        )


def result(project: Path, video_sha: str, inputs: dict, reused: bool) -> dict:
    return {
        "schema": RESULT_SCHEMA,
        "status": "complete",
        "project": project.name,
        "video_sha256": video_sha,
        "inputs": inputs,
        "artifacts": {
            "prep": output_ref(project / PREP_PATH, project, PREP_PATH),
            "review": output_ref(project / REVIEW_PATH, project, REVIEW_PATH),
        },
        "reused": reused,
    }


def prepare_output_directory(project: Path) -> Path:
    root = project / "quality-review"
    directory = project / OUTPUT_DIR
    for path, label in ((root, "quality-review"), (directory, str(OUTPUT_DIR))):
        if canonical_layout.direct_path(path, project) is None or path.is_symlink():
            raise QualityReviewError(
                "unsafe_project_path", f"{label} must be a plain project directory"
            )
        path.mkdir(exist_ok=True)
        if not path.is_dir() or path.is_symlink():
            raise QualityReviewError(
                "unsafe_project_path", f"{label} must be a plain project directory"
            )
    for target in (project / PREP_PATH, project / REVIEW_PATH):
        if target.is_symlink():
            raise QualityReviewError(
                "unsafe_project_path", f"refusing symlink output: {target.name}"
            )
    return directory


def current_inputs(project: Path) -> tuple[Path, dict, dict, dict]:
    video = canonical_layout.direct_path(project / VIDEO_PATH, project)
    marker_path = canonical_layout.direct_path(project / MARKER_PATH, project)
    if (
        video is None
        or not video.is_file()
        or video.is_symlink()
        or video.stat().st_size <= 0
    ):
        raise QualityReviewError(
            "render_not_current", "canonical output/final.mp4 is missing or unsafe"
        )
    if marker_path is None or not marker_path.is_file() or marker_path.is_symlink():
        raise QualityReviewError(
            "render_not_current", "canonical render-result marker is missing or unsafe"
        )
    video_ref = stable_ref(video, project, VIDEO_PATH)
    marker = render_contract.parse_render_result(marker_path)
    if not render_contract.final_mix_passes(
        marker, project, video, video_ref["sha256"]
    ):
        raise QualityReviewError(
            "render_not_current",
            "render-result does not certify the current final video and mix",
        )
    marker_ref = stable_ref(marker_path, project, MARKER_PATH)
    self_eval = render_self_eval.current_pass(project)
    if self_eval is None:
        raise QualityReviewError(
            "self_eval_not_current", "render self-evaluation has no current pass"
        )
    visual = visual_qa_sample.validate_review(project, video_ref["sha256"], video)
    if visual.get("ok") is not True:
        raise QualityReviewError(
            visual.get("code", "visual_review_not_current"),
            visual.get("message", "HVP-21 human review is not current"),
        )
    inputs = {
        "final_video": video_ref,
        "render_result": marker_ref,
        "render_self_eval": dict(self_eval["ref"]),
        "visual_qa_review": dict(visual["review_ref"]),
    }
    reject_newer_human_hold(project, video_ref["sha256"], inputs["visual_qa_review"])
    return video, marker, visual, inputs


def ensure_inputs_unchanged(project: Path, expected: dict) -> None:
    _video, _marker, _visual, actual = current_inputs(project)
    if actual != expected:
        raise QualityReviewError(
            "inputs_changed", "required final-quality evidence changed during the run"
        )


def produce(project: Path) -> dict:
    video, marker, visual, inputs = current_inputs(project)
    video_sha = inputs["final_video"]["sha256"]
    ffprobe = executable("ffprobe")
    ffmpeg = executable("ffmpeg")
    metadata = probe(ffprobe, video)
    if metadata["has_audio"] is not True:
        raise QualityReviewError("audio_missing", "final video has no audio stream")
    checks = media_checks(ffmpeg, video)
    marker_duration = marker.get("duration_seconds")
    if (
        not isinstance(marker_duration, (int, float))
        or isinstance(marker_duration, bool)
        or abs(float(marker_duration) - metadata["duration"]) > 0.5
    ):
        raise QualityReviewError(
            "duration_mismatch",
            "ffprobe duration disagrees with the canonical render marker",
        )

    created_at = now_iso()
    prep = {
        "schema": PREP_SCHEMA,
        "project": project.name,
        "created_at": created_at,
        "video": str(VIDEO_PATH),
        "video_sha256": video_sha,
        "inputs": inputs,
        "metadata": metadata,
        "audio": {
            "has_audio": True,
            "clipping": False,
            "too_quiet": False,
            "evidence": {
                "source": str(MARKER_PATH),
                "loudness_lufs": marker["loudness_lufs"],
                "true_peak_dbfs": marker["true_peak_dbfs"],
                "loudness_range_lu": marker["loudness_range_lu"],
            },
        },
        "mechanical_evidence": checks,
    }
    review = {
        "schema": REVIEW_SCHEMA,
        "project": project.name,
        "reviewed_at": created_at,
        "video": str(VIDEO_PATH),
        "video_sha256": video_sha,
        "inputs": inputs,
        "publish_readiness": "ship",
        "critical_issues": [],
        "warnings": [],
        "checks": [
            {
                "name": "duration_and_decode_gate",
                "status": "pass",
                "evidence": {
                    "duration_seconds": metadata["duration"],
                    "full_decode_clean": True,
                },
            },
            {
                "name": "static_frame_gate",
                "status": "pass",
                "evidence": checks["static"],
            },
            {
                "name": "black_frame_scan",
                "status": "pass",
                "evidence": checks["black"],
            },
            {
                "name": "visual_spot_check",
                "status": "pass",
                "evidence": {
                    "source": "HVP-21 human visual QA review",
                    "review": inputs["visual_qa_review"],
                    "reviewed_by": visual["reviewed_by"],
                    "reviewed_at": visual["reviewed_at"],
                },
            },
        ],
    }

    directory = prepare_output_directory(project)
    with tempfile.TemporaryDirectory(
        prefix=".final-quality-", dir=directory
    ) as temporary:
        staging = Path(temporary)
        staged_prep = staging / "prep.json"
        staged_review = staging / "review.json"
        staged_prep.write_text(
            json.dumps(prep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staged_review.write_text(
            json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        ensure_inputs_unchanged(project, inputs)
        os.replace(staged_prep, project / PREP_PATH)
        os.replace(staged_review, project / REVIEW_PATH)
    ensure_inputs_unchanged(project, inputs)
    outputs = {
        "prep": output_ref(project / PREP_PATH, project, PREP_PATH),
        "review": output_ref(project / REVIEW_PATH, project, REVIEW_PATH),
    }
    try:
        final_quality_authority._record(project, inputs, outputs)
    except final_quality_authority.FinalQualityAuthorityError as exc:
        raise QualityReviewError("authority_record_failed", str(exc)) from exc
    ensure_inputs_unchanged(project, inputs)
    if not final_quality_authority.validate(project, inputs, outputs):
        raise QualityReviewError(
            "authority_record_failed", "final-quality authority did not validate"
        )
    return result(project, video_sha, inputs, False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project")
    args = parser.parse_args()
    try:
        value = produce(resolve_project(args.project))
    except (OSError, QualityReviewError, subprocess.SubprocessError) as exc:
        code = (
            exc.code
            if isinstance(exc, QualityReviewError)
            else "final_quality_review_failed"
        )
        print(
            json.dumps(
                {
                    "schema": RESULT_SCHEMA,
                    "status": "failed",
                    "code": code,
                    "message": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
