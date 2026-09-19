#!/usr/bin/env python3
"""Parse and validate canonical render and final-mix receipts."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical_layout  # noqa: E402


RENDER_SCHEMA = "haru.render_result.v1"
MIX_SCHEMA = "haru.final_mix.v1"
RENDER_INPUT_REVISION_FIELD = "render_input_revision"
GENERATED_RENDER_OUTPUTS = {
    "output/cover.png",
    "output/final.mp4",
    "output/final.mp4.render-result",
    "output/final.mp4.render.log",
    "output/final.mp4.rendering",
    "output/final.pre-loudnorm.mp4",
    "output/final.pre-loudnorm.raw.mp4",
    "output/render.log",
}


def _generated_cache(relative: str) -> bool:
    parts = relative.split("/")
    return (
        any(
            parts[index : index + 2] == ["node_modules", ".cache"]
            for index in range(len(parts) - 1)
        )
        or "__pycache__" in parts
    )


def _revision_excluded(relative: str) -> bool:
    parts = relative.split("/") if relative else []
    return bool(
        _generated_cache(relative)
        or (parts and parts[0] in {".hvp", "quality-review", "publish"})
        or relative == "output/.staging"
        or relative.startswith("output/.staging/")
        or relative == "output/versions"
        or relative.startswith("output/versions/")
        or relative == "output/archive"
        or relative.startswith("output/archive/")
        or relative == "output/superseded"
        or relative.startswith("output/superseded/")
        or relative in GENERATED_RENDER_OUTPUTS
        or relative
        in {
            "artifact_manifest.json",
            "pipeline_status.json",
            "youtube-publish-pack.md",
        }
        or ".superseded-" in relative
    )


def render_input_revision(project_value: Path) -> str:
    """Digest every render-relevant project byte and path deterministically."""

    project = Path(project_value)
    if project.is_symlink() or not project.is_dir():
        raise ValueError("project")
    project = project.resolve(strict=True)
    digest = hashlib.sha256()
    for current, directories, files in os.walk(project, followlinks=False):
        current_path = Path(current)
        relative_root = (
            current_path.relative_to(project).as_posix()
            if current_path != project
            else ""
        )
        directories[:] = sorted(
            name
            for name in directories
            if not _revision_excluded("/".join(filter(None, (relative_root, name))))
        )
        for name in sorted([*directories, *files]):
            path = current_path / name
            relative = "/".join(filter(None, (relative_root, name)))
            if _revision_excluded(relative):
                continue
            metadata = path.lstat()
            digest.update(relative.encode("utf-8") + b"\0")
            if stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(path)
                if os.path.isabs(target):
                    raise ValueError("absolute input symlink")
                try:
                    path.resolve(strict=True).relative_to(project)
                except (OSError, RuntimeError, ValueError) as error:
                    raise ValueError("escaping input symlink") from error
                digest.update(b"l" + target.encode("utf-8") + b"\0")
            elif stat.S_ISDIR(metadata.st_mode):
                digest.update(b"d\0")
            elif stat.S_ISREG(metadata.st_mode):
                digest.update(
                    b"f" + str(metadata.st_mode & 0o777).encode("ascii") + b"\0"
                )
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            else:
                raise ValueError("special render input")
    return digest.hexdigest()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def valid_sha256(value, *, lowercase: bool = False) -> bool:
    alphabet = "[0-9a-f]" if lowercase else "[0-9a-fA-F]"
    return (
        isinstance(value, str) and re.fullmatch(rf"{alphabet}{{64}}", value) is not None
    )


def valid_audio_mix_receipt(value) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or value.get("schema") != "haru.audio_mix.v1":
        return False
    background = value.get("background_music")
    effects = value.get("sound_effects")
    if not isinstance(effects, list):
        return False
    assets = ([background] if background is not None else []) + effects
    return bool(
        valid_sha256(value.get("plan_sha256"), lowercase=True)
        and (background is None or value.get("ducking") == "sidechaincompress.v1")
        and assets
        and all(
            isinstance(asset, dict)
            and isinstance(asset.get("path"), str)
            and asset["path"]
            and valid_sha256(asset.get("sha256"), lowercase=True)
            for asset in assets
        )
    )


def parse_render_result(path: Path | None) -> dict:
    if not path or not path.exists():
        return {"status": "missing"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return {"status": "unknown", "error": f"cannot read render result: {exc}"}
    if text == "PASS":
        return {"status": "pass", "raw": "PASS"}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"status": "unknown", "raw": text[:200]}
    if not isinstance(data, dict):
        return {"status": "unknown"}
    if data.get("status") in {"render_complete", "pass", "PASS"}:
        data["status"] = "pass"
    return data


def _mix_evidence_passes(
    data: dict,
    *,
    lowercase_digest: bool = False,
    require_nonnegative_target_lra: bool = True,
) -> bool:
    mix = data.get("mix")
    target = mix.get("target") if isinstance(mix, dict) else None
    return bool(
        number(data.get("duration_seconds"))
        and data["duration_seconds"] > 0
        and number(data.get("loudness_lufs"))
        and -15 <= data["loudness_lufs"] <= -13
        and number(data.get("true_peak_dbfs"))
        and data["true_peak_dbfs"] <= -1.0
        and number(data.get("loudness_range_lu"))
        and data["loudness_range_lu"] >= 0
        and isinstance(mix, dict)
        and mix.get("schema") == MIX_SCHEMA
        and mix.get("method") == "ffmpeg_loudnorm_two_pass"
        and mix.get("normalization_type") in {"linear", "dynamic"}
        and valid_audio_mix_receipt(mix.get("audio_mix"))
        and valid_sha256(mix.get("input_sha256"), lowercase=lowercase_digest)
        and isinstance(target, dict)
        and target.get("integrated_lufs") == -14.0
        and target.get("true_peak_dbfs") == -1.0
        and number(target.get("loudness_range_lu"))
        and (not require_nonnegative_target_lra or target["loudness_range_lu"] >= 0)
        and target["loudness_range_lu"] - data["loudness_range_lu"] <= 3.0
    )


def final_mix_passes(data, project: Path, video: Path, actual_video_sha) -> bool:
    try:
        output = str(video.relative_to(project))
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(data, dict)
        and data.get("schema") == RENDER_SCHEMA
        and data.get("status") == "pass"
        and data.get("project") == project.name
        and data.get("output") == output
        and valid_sha256(data.get("video_sha256"))
        and data.get("video_sha256") == actual_video_sha
        and _mix_evidence_passes(data)
        and describes_current_inputs(project, data)
    )


def assembly_binding_state(project: Path, marker) -> str:
    binding = marker.get("assembly") if isinstance(marker, dict) else None
    plan_path = project / "segment-plan.json"
    if not plan_path.exists() and not plan_path.is_symlink():
        return "current" if binding is None else "invalid"
    try:
        import segment_assembly
        import segment_plan
    except ImportError:
        return "invalid"
    plan = segment_plan.validate(project)
    if plan.get("mode") == "legacy_non_segmented":
        return "current" if binding is None else "invalid"
    if plan.get("mode") != "segmented" or not isinstance(binding, dict):
        return "invalid"
    if (
        binding.get("schema") != segment_assembly.SCHEMA
        or binding.get("path") != segment_assembly.RECEIPT_PATH
        or not valid_sha256(binding.get("sha256"), lowercase=True)
    ):
        return "invalid"
    mix = marker.get("mix")
    if not isinstance(mix, dict):
        return "invalid"
    if (
        segment_assembly.current_receipt(project, expected_sha256=binding["sha256"])
        is not None
    ):
        return "current"
    receipt_path = project / segment_assembly.RECEIPT_PATH
    candidates = [
        receipt_path,
        receipt_path.with_name(
            f"{receipt_path.name}.superseded-{binding['sha256'][:12]}"
        ),
    ]
    for raw_candidate in candidates:
        candidate = canonical_layout.direct_path(raw_candidate, project)
        if candidate is None or not candidate.is_file():
            continue
        try:
            if sha256(candidate) != binding["sha256"]:
                continue
            receipt = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if segment_assembly.receipt_shape_ok(
            project, receipt, require_output=False
        ) and mix.get("input_sha256") == receipt.get("output_sha256"):
            return "stale"
    return "invalid"


def describes_current_inputs(project: Path, marker) -> bool:
    """Is this marker about the project as it stands, or about a previous one?

    The marker records which narration and which editorial contract were
    rendered. Nothing compared them to the files on disk, so a render from a
    superseded take stayed "complete" forever: `render-project` returned the old
    marker instantly and never re-rendered, while the narration, the cut, and
    the duration had all moved on. Measured on a real project -- a marker
    claiming 957.6s was returned for a 1093.7s narration.

    Only digests the marker actually carries are compared, and only against
    files that exist: a lane with no editorial contract records none, and
    demanding one would refuse every render outside the longform profile.
    """
    if assembly_binding_state(project, marker) != "current":
        return False
    recorded_revision = (
        marker.get(RENDER_INPUT_REVISION_FIELD) if isinstance(marker, dict) else None
    )
    if not valid_sha256(recorded_revision, lowercase=True):
        return False
    try:
        if recorded_revision != render_input_revision(project):
            return False
    except (OSError, ValueError):
        return False
    for name, field in (
        ("narration-final.mp3", "narration_sha256"),
        ("editorial-contract.json", "editorial_contract_sha256"),
    ):
        recorded = marker.get(field) if isinstance(marker, dict) else None
        if recorded is None:
            continue
        path = project / name
        if path.is_symlink() or not path.is_file():
            continue
        if recorded != sha256(path):
            return False
    return True


def _final_result_shape_ok(project: Path, marker, video: Path) -> bool:
    try:
        video_sha = sha256(video)
        video_bytes = video.stat().st_size
        output = str(video.relative_to(project))
    except (OSError, TypeError, ValueError):
        return False
    return bool(
        isinstance(marker, dict)
        and marker.get("schema") == RENDER_SCHEMA
        and marker.get("status") == "render_complete"
        and marker.get("project") == project.name
        and marker.get("output") == output
        and valid_sha256(marker.get("video_sha256"), lowercase=True)
        and marker["video_sha256"] == video_sha
        and marker.get("bytes") == video_bytes
        and (
            marker.get(RENDER_INPUT_REVISION_FIELD) is None
            or valid_sha256(marker.get(RENDER_INPUT_REVISION_FIELD), lowercase=True)
        )
        and _mix_evidence_passes(
            marker,
            lowercase_digest=True,
            require_nonnegative_target_lra=False,
        )
    )


def valid_final_result(project: Path, marker, video: Path) -> bool:
    return _final_result_shape_ok(project, marker, video) and describes_current_inputs(
        project, marker
    )


def superseded_final_result(project: Path, marker, video: Path) -> bool:
    """A complete, coherent render -- of a version of this project that is gone.

    Worth telling apart from a malformed marker. A malformed one means something
    unexplained happened and the safe move is to stop; this one means the normal
    thing happened -- the narration was re-taken, the cut was re-timed -- and the
    only sane response is to render again. Refusing both alike would make every
    re-take need a hand-cleared output directory.
    """
    return bool(
        _final_result_shape_ok(project, marker, video)
        and assembly_binding_state(project, marker) != "invalid"
        and not describes_current_inputs(project, marker)
    )


def valid_mix_receipt(value) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("schema") == MIX_SCHEMA
        and value.get("status") == "mix_complete"
        and value.get("method") == "ffmpeg_loudnorm_two_pass"
        and value.get("normalization_type") in {"linear", "dynamic"}
        and valid_sha256(value.get("input_sha256"))
        and valid_sha256(value.get("sha256"))
        and isinstance(value.get("bytes"), int)
        and not isinstance(value.get("bytes"), bool)
        and value["bytes"] > 0
        and _mix_evidence_passes({**value, "mix": value})
    )


def final_revision(path: Path) -> int | None:
    matches = re.findall(r"(?:^|[-_])v(\d+)(?=[^0-9]|$)", path.stem.lower())
    return int(matches[-1]) if matches else None


def final_video_candidates(project: Path) -> list[Path]:
    output = project / "output"
    try:
        if output.is_symlink() or not output.is_dir():
            return []
        return [
            path
            for path in output.iterdir()
            if "final" in path.name
            and path.suffix == ".mp4"
            and "pre-loudnorm" not in path.name
            and "compressed" not in path.name
            and "review" not in path.name
        ]
    except OSError:
        return []


def human_review_time(project: Path, project_file) -> float:
    """Ordering comes from the verdict bytes, never a touch of its file."""
    path = project_file(
        project / "quality-review/visual-sampling/visual-qa-review.json", project
    )
    if not path:
        return float("-inf")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        timestamp = dt.datetime.fromisoformat(
            receipt["reviewed_at"].replace("Z", "+00:00")
        )
        if timestamp.tzinfo is None:
            return float("-inf")
        return timestamp.timestamp()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return float("-inf")


def find_final_video(project: Path, project_file):
    reviews = set()
    review_root = project / "quality-review"
    try:
        if review_root.is_dir() and not review_root.is_symlink():
            for review_dir in review_root.iterdir():
                if not review_dir.name.startswith("final"):
                    continue
                candidate = review_dir / "review.json"
                if review_dir.is_symlink():
                    reviews.add(candidate)
                    continue
                try:
                    candidate.lstat()
                except OSError:
                    continue
                reviews.add(candidate)
    except OSError:
        pass

    def evidence_mtime(path):
        try:
            source = path.parent if path.parent.is_symlink() else path
            return source.lstat().st_mtime
        except OSError:
            return float("inf")

    # A later machine report must never erase a human hold. A hold for these
    # exact bytes remains active until the formal HVP-21 review is newer.
    canonical_video = project_file(project / "output/final.mp4", project)
    human_review = project_file(
        project / "quality-review/visual-sampling/visual-qa-review.json", project
    )
    if canonical_video and human_review:
        try:
            current_digest = sha256(canonical_video)
            reviewed_at = human_review_time(project, project_file)
            active_holds = []
            for path in reviews:
                safe = project_file(path, project)
                if not safe or evidence_mtime(path) <= reviewed_at:
                    continue
                try:
                    value = json.loads(safe.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if (
                    isinstance(value, dict)
                    and value.get("publish_readiness") == "hold"
                    and value.get("video_sha256") == current_digest
                ):
                    active_holds.append((path, value))
            if active_holds:
                hold_path, hold = max(
                    active_holds, key=lambda item: evidence_mtime(item[0])
                )
                return canonical_video, hold_path, hold
        except OSError:
            pass

    # The fixed MCP runner writes final-v1 even when a project retains older
    # numbered QA directories. Prefer its newer, exact-input-bound receipt over
    # legacy revision numbering; never hide a later human hold/review.
    fixed = project / "quality-review/final-v1/review.json"
    safe_fixed = project_file(fixed, project)
    if safe_fixed and not any(
        evidence_mtime(other) > evidence_mtime(fixed) for other in reviews
    ):
        try:
            fixed_data = json.loads(safe_fixed.read_text(encoding="utf-8"))
            inputs = fixed_data.get("inputs") if isinstance(fixed_data, dict) else None
            expected_inputs = {
                "final_video": "output/final.mp4",
                "render_result": "output/final.mp4.render-result",
                "render_self_eval": canonical_layout.RENDER_SELF_EVAL_RESULT,
                "visual_qa_review": "quality-review/visual-sampling/visual-qa-review.json",
            }
            current = isinstance(inputs, dict) and all(
                isinstance(inputs.get(key), dict)
                and inputs[key].get("path") == name
                and (source := project_file(project / name, project)) is not None
                and inputs[key].get("sha256") == sha256(source)
                for key, name in expected_inputs.items()
            )
            if (
                current
                and fixed_data.get("schema") == "haru.quality_review.v1"
                and fixed_data.get("video") == "output/final.mp4"
                and fixed_data.get("video_sha256") == inputs["final_video"]["sha256"]
            ):
                return project / "output/final.mp4", fixed, fixed_data
        except (OSError, ValueError, TypeError):
            pass

    versioned = [path for path in reviews if final_revision(path.parent) is not None]
    review_path = max(
        versioned,
        key=lambda path: (final_revision(path.parent), evidence_mtime(path), str(path)),
        default=None,
    )
    newer_unversioned = [
        path
        for path in reviews
        if final_revision(path.parent) is None
        and (not review_path or evidence_mtime(path) >= evidence_mtime(review_path))
    ]
    review_path = (
        max(
            newer_unversioned,
            key=lambda path: (evidence_mtime(path), str(path)),
            default=None,
        )
        or review_path
    )
    safe_review = project_file(review_path, project, allow_empty=True)
    try:
        review_data = (
            json.loads(safe_review.read_text(encoding="utf-8")) if safe_review else None
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        review_data = None
    if (
        isinstance(review_data, dict)
        and isinstance(review_data.get("video"), str)
        and review_data["video"]
    ):
        video = Path(review_data["video"])
        if not video.is_absolute():
            video = project / video
        return video, review_path, review_data
    fallback = max(
        final_video_candidates(project),
        key=lambda path: (final_revision(path) or -1, path.name),
        default=None,
    )
    return (
        fallback,
        review_path,
        {"_error": "latest final review is invalid"} if review_path else None,
    )


def newer_final_candidate(candidate: Path, reviewed: Path) -> bool:
    candidate_revision = final_revision(candidate)
    reviewed_revision = final_revision(reviewed)
    try:
        return candidate.lstat().st_mtime > reviewed.lstat().st_mtime or (
            candidate_revision is not None
            and reviewed_revision is not None
            and candidate_revision > reviewed_revision
        )
    except OSError:
        return True
