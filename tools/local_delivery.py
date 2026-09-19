#!/usr/bin/env python3
"""Honest local delivery status and immutable export bundles.

This lane proves technical playability and source binding only. It deliberately
does not infer pronunciation approval, editorial correctness, visual approval,
or publication authority from a decoded video.
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
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical_layout  # noqa: E402
import render_contract  # noqa: E402
from workspace_barrier import mutation_barrier  # noqa: E402

STATUS_SCHEMA = "video_studio.local_delivery_status.v1"
BUNDLE_SCHEMA = "video_studio.delivery_bundle.v1"
EXPORT_SCHEMA = "video_studio.delivery_export.v1"
IDEMPOTENCY_SCHEMA = "video_studio.delivery_idempotency.v1"
KEY = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SRT_TIME = re.compile(
    r"^(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})\s+-->\s+"
    r"(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})(?:\s+.*)?$"
)

COPY_SPECS = (
    ("final-video", "output/final.mp4", "video/final.mp4", True),
    ("render-receipt", "output/final.mp4.render-result", "qa/render-result.json", True),
    ("captions", "narration-final.srt", "captions/narration-final.srt", False),
    ("cover", "output/cover.png", "cover/cover.png", False),
    ("source-ledger", "sources.md", "sources/sources.md", False),
    ("source-claims", "claims.json", "sources/claims.json", False),
    (
        "project-contract",
        "project-contract.json",
        "source/project-contract.json",
        False,
    ),
)


class DeliveryError(Exception):
    def __init__(self, code: str, message: str, *, outcome: str = "error"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.outcome = outcome


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _direct_project(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise DeliveryError(
            "invalid_input", "project root must be an absolute direct directory"
        )
    resolved = path.resolve(strict=True)
    if resolved != path.absolute():
        raise DeliveryError("invalid_input", "project root must use its canonical path")
    return resolved


def _project_file(project: Path, relative: str) -> Path | None:
    candidate = canonical_layout.direct_path(project / relative, project)
    if candidate is None or not candidate.is_file():
        return None
    return candidate


def _tool(name: str) -> str:
    candidates = (
        Path("/usr/bin") / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
    )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
    raise DeliveryError(
        "media_tool_unavailable", f"{name} is unavailable", outcome="blocked"
    )


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeliveryError("media_probe_failed", str(exc), outcome="blocked") from exc


def _probe(video: Path) -> tuple[dict, list[dict]]:
    command = [
        _tool("ffprobe"),
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(video),
    ]
    result = _run(command, 60)
    if result.returncode != 0:
        raise DeliveryError(
            "media_probe_failed",
            result.stderr.decode("utf-8", "replace")[-1000:],
            outcome="blocked",
        )
    try:
        value = json.loads(result.stdout)
        duration = float(value["format"]["duration"])
        streams = value["streams"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DeliveryError(
            "media_probe_failed", f"invalid ffprobe output: {exc}", outcome="blocked"
        )
    if not isinstance(streams, list) or not all(
        isinstance(stream, dict) for stream in streams
    ):
        raise DeliveryError(
            "media_probe_failed", "ffprobe streams are invalid", outcome="blocked"
        )
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if (
        not videos
        or duration <= 0
        or not isinstance(videos[0].get("width"), int)
        or videos[0]["width"] <= 0
        or not isinstance(videos[0].get("height"), int)
        or videos[0]["height"] <= 0
    ):
        raise DeliveryError(
            "media_probe_failed",
            "video stream or duration is invalid",
            outcome="blocked",
        )
    summary = {
        "duration_seconds": duration,
        "video": {
            "codec": videos[0].get("codec_name"),
            "width": videos[0]["width"],
            "height": videos[0]["height"],
            "pixel_format": videos[0].get("pix_fmt"),
        },
        "audio": [
            {
                "codec": stream.get("codec_name"),
                "channels": stream.get("channels"),
                "sample_rate": stream.get("sample_rate"),
            }
            for stream in audios
        ],
    }
    return summary, streams


def _full_decode(video: Path) -> None:
    result = _run(
        [
            _tool("ffmpeg"),
            "-v",
            "error",
            "-xerror",
            "-i",
            str(video),
            "-f",
            "null",
            "-",
        ],
        300,
    )
    if result.returncode != 0:
        raise DeliveryError(
            "media_decode_failed",
            result.stderr.decode("utf-8", "replace")[-1000:],
            outcome="blocked",
        )


def _seconds(groups: tuple[str, ...]) -> float:
    hour, minute, second, millis = (int(value) for value in groups)
    if minute >= 60 or second >= 60:
        raise DeliveryError(
            "subtitle_invalid", "invalid SRT timestamp", outcome="blocked"
        )
    return hour * 3600 + minute * 60 + second + millis / 1000


def _subtitle_check(path: Path, duration: float) -> dict:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DeliveryError("subtitle_invalid", str(exc), outcome="blocked") from exc
    ranges = []
    for line in lines:
        match = SRT_TIME.match(line.strip())
        if "-->" in line and match is None:
            raise DeliveryError(
                "subtitle_invalid", f"invalid SRT range: {line}", outcome="blocked"
            )
        if match:
            ranges.append((_seconds(match.groups()[:4]), _seconds(match.groups()[4:])))
    if not ranges:
        raise DeliveryError(
            "subtitle_invalid", "no SRT cue ranges found", outcome="blocked"
        )
    previous = 0.0
    for start, end in ranges:
        if start < previous or end <= start or end > duration + 0.5:
            raise DeliveryError(
                "subtitle_invalid",
                f"invalid subtitle range {start:.3f} --> {end:.3f} for {duration:.3f}s video",
                outcome="blocked",
            )
        previous = end
    return {
        "status": "pass",
        "cue_count": len(ranges),
        "first_start_seconds": ranges[0][0],
        "last_end_seconds": ranges[-1][1],
    }


def _entry(project: Path, role: str, relative: str, required: bool) -> dict | None:
    path = _project_file(project, relative)
    if path is None:
        if required:
            raise DeliveryError(
                "delivery_artifact_missing", f"missing {relative}", outcome="blocked"
            )
        return None
    return {
        "role": role,
        "path": relative,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
    }


def _current_input_bindings(project: Path, receipt: dict) -> list[str]:
    problems = []
    for relative, field in (
        ("narration-final.mp3", "narration_sha256"),
        ("editorial-contract.json", "editorial_contract_sha256"),
    ):
        path = _project_file(project, relative)
        if path is not None and receipt.get(field) != _sha256(path):
            problems.append(f"{field} does not bind current {relative}")
    return problems


def technical_status(project_root: Path) -> dict:
    project = _direct_project(project_root)
    blockers: list[dict] = []
    warnings: list[dict] = []
    artifacts: list[dict] = []
    probe = None
    decode_passed = False
    subtitles = {"status": "unperformed", "reason": "captions_missing"}
    video = _project_file(project, "output/final.mp4")
    receipt_path = _project_file(project, "output/final.mp4.render-result")

    for role, source, _destination, required in COPY_SPECS:
        try:
            entry = _entry(project, role, source, required)
        except DeliveryError as exc:
            blockers.append({"code": exc.code, "detail": exc.message})
        else:
            if entry is not None:
                artifacts.append(entry)
            elif role in {"captions", "cover", "source-ledger", "source-claims"}:
                warnings.append(
                    {"code": f"{role}_missing", "detail": f"{source} is not available"}
                )

    receipt = None
    if receipt_path is not None:
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            blockers.append(
                {
                    "code": "render_receipt_invalid",
                    "detail": "render receipt is unreadable",
                }
            )
        if not isinstance(receipt, dict):
            blockers.append(
                {
                    "code": "render_receipt_invalid",
                    "detail": "render receipt is not an object",
                }
            )
            receipt = None

    if video is not None and receipt is not None:
        if not render_contract.valid_final_result(project, receipt, video):
            blockers.append(
                {
                    "code": "render_receipt_invalid",
                    "detail": "render receipt does not bind the current final video and current render inputs",
                }
            )
        for detail in _current_input_bindings(project, receipt):
            blockers.append({"code": "render_input_binding_invalid", "detail": detail})
        try:
            probe, _streams = _probe(video)
            _full_decode(video)
            decode_passed = True
        except DeliveryError as exc:
            blockers.append({"code": exc.code, "detail": exc.message})
        else:
            captions = _project_file(project, "narration-final.srt")
            if captions is not None:
                try:
                    subtitles = _subtitle_check(captions, probe["duration_seconds"])
                except DeliveryError as exc:
                    blockers.append({"code": exc.code, "detail": exc.message})
                    subtitles = {"status": "fail", "reason": exc.message}

    unperformed = [
        {
            "check": "pronunciation_approval",
            "status": "unperformed",
            "reason": "technical delivery does not infer pronunciation approval",
        },
        {
            "check": "human_visual_review",
            "status": "unperformed",
            "reason": "full decode proves decodability, not visual correctness",
        },
        {
            "check": "editorial_and_factual_review",
            "status": "unperformed",
            "reason": "source export does not prove claims or editorial judgment",
        },
        {
            "check": "publication_approval",
            "status": "unperformed",
            "reason": "local delivery is independent of publishing authority",
        },
    ]
    return {
        "schema": STATUS_SCHEMA,
        "project": project.name,
        "project_root": str(project),
        "checked_at": _now(),
        "status": "technical_ready" if not blockers else "blocked",
        "blockers": blockers,
        "warnings": warnings,
        "media_probe": probe,
        "full_decode": "pass" if decode_passed else "unperformed_or_failed",
        "subtitles": subtitles,
        "artifacts": artifacts,
        "content_checks": unperformed,
        "publication_ready": False,
        "human_approval": False,
    }


def _destination_root(argument: Path | None) -> Path:
    raw = argument
    if raw is None:
        configured = os.environ.get("VIDEO_STUDIO_DELIVERY_ROOT", "")
        raw = Path(configured) if configured else None
    if raw is None or not raw.is_absolute():
        raise DeliveryError(
            "delivery_destination_unconfigured",
            "set VIDEO_STUDIO_DELIVERY_ROOT to an absolute local directory",
            outcome="blocked",
        )
    if raw.exists():
        if raw.is_symlink() or not raw.is_dir():
            raise DeliveryError(
                "invalid_input", "delivery root must be a direct directory"
            )
        return raw.resolve(strict=True)
    parent = raw.parent
    if not parent.is_dir() or parent.is_symlink():
        raise DeliveryError(
            "invalid_input", "delivery root parent must be a direct directory"
        )
    raw.mkdir(mode=0o755)
    return raw.resolve(strict=True)


def _bundle_identity(status: dict) -> str:
    stable = {
        "schema": BUNDLE_SCHEMA,
        "project": status["project"],
        "artifacts": [
            {
                "role": item["role"],
                "path": item["path"],
                "sha256": item["sha256"],
                "bytes": item["bytes"],
            }
            for item in status["artifacts"]
        ],
        "media_probe": status["media_probe"],
        "subtitles": status["subtitles"],
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _verify_existing_bundle(bundle: Path, bundle_id: str) -> bool:
    manifest_path = bundle / "artifact-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if (
        manifest.get("schema") != BUNDLE_SCHEMA
        or manifest.get("bundle_id") != bundle_id
    ):
        return False
    artifacts = manifest.get("artifacts")
    if (
        not isinstance(artifacts, list)
        or not artifacts
        or manifest.get("publication_ready") is not False
        or manifest.get("human_approval") is not False
    ):
        return False
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            return False
        path = canonical_layout.direct_path(bundle / artifact.get("path", ""), bundle)
        if (
            path is None
            or not path.is_file()
            or _sha256(path) != artifact.get("sha256")
            or path.stat().st_size != artifact.get("bytes")
        ):
            return False
    return True


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)


def _write_json_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _direct_child_directory(parent: Path, name: str) -> Path:
    child = parent / name
    if child.exists() or child.is_symlink():
        if child.is_symlink() or not child.is_dir():
            raise DeliveryError(
                "delivery_target_conflict", f"unsafe directory: {child}"
            )
    else:
        child.mkdir(mode=0o755)
    return child


def _export_delivery_unlocked(
    project_root: Path,
    *,
    destination: Path | None,
    idempotency_key: str,
) -> dict:
    if KEY.fullmatch(idempotency_key) is None:
        raise DeliveryError("invalid_input", "invalid idempotency key")
    status = technical_status(project_root)
    project = Path(status["project_root"])
    if status["blockers"]:
        raise DeliveryError(
            "delivery_blocked",
            "technical delivery has blockers",
            outcome="blocked",
        )
    root = _destination_root(destination)
    bundle_id = _bundle_identity(status)
    project_destination = _direct_child_directory(root, status["project"])
    bundle = project_destination / bundle_id
    hvp = _direct_child_directory(project, ".hvp")
    delivery_state = _direct_child_directory(hvp, "local-delivery")
    export_state = _direct_child_directory(delivery_state, "exports")
    receipt_path = export_state / f"{idempotency_key}.json"
    receipt = None
    if receipt_path.is_file() and not receipt_path.is_symlink():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DeliveryError(
                "idempotency_conflict", f"unreadable export receipt: {exc}"
            ) from exc
        if not isinstance(receipt, dict) or (
            receipt.get("schema") != IDEMPOTENCY_SCHEMA
            or receipt.get("bundle_id") != bundle_id
            or receipt.get("destination_root") != str(root)
        ):
            raise DeliveryError(
                "idempotency_conflict", "idempotency key is bound to another export"
            )
        if not _verify_existing_bundle(bundle, bundle_id):
            raise DeliveryError(
                "delivery_target_conflict", "recorded bundle is absent or changed"
            )
        return {
            "schema": EXPORT_SCHEMA,
            "project": status["project"],
            "status": "exported",
            "bundle_id": bundle_id,
            "bundle_path": str(bundle),
            "reused": True,
            "publication_ready": False,
            "human_approval": False,
        }

    if bundle.exists():
        if (
            bundle.is_symlink()
            or not bundle.is_dir()
            or not _verify_existing_bundle(bundle, bundle_id)
        ):
            raise DeliveryError(
                "delivery_target_conflict", "immutable bundle target already differs"
            )
        reused = True
    else:
        staging = bundle.parent / f".{bundle_id}.staging-{uuid.uuid4()}"
        staging.mkdir(mode=0o700)
        try:
            exported = []
            expected = {item["path"]: item for item in status["artifacts"]}
            for role, source_relative, destination_relative, required in COPY_SPECS:
                source = _project_file(project, source_relative)
                if source is None:
                    if required:
                        raise DeliveryError(
                            "delivery_artifact_missing", f"missing {source_relative}"
                        )
                    continue
                target = staging / destination_relative
                _copy(source, target)
                expected_source = expected.get(source_relative)
                if (
                    expected_source is None
                    or _sha256(target) != expected_source["sha256"]
                    or target.stat().st_size != expected_source["bytes"]
                ):
                    raise DeliveryError(
                        "delivery_source_changed",
                        f"{source_relative} changed during export",
                        outcome="blocked",
                    )
                exported.append(
                    {
                        "role": role,
                        "path": destination_relative,
                        "source_path": source_relative,
                        "sha256": _sha256(target),
                        "bytes": target.stat().st_size,
                    }
                )
            fresh_status = technical_status(project)
            if fresh_status["blockers"] or _bundle_identity(fresh_status) != bundle_id:
                raise DeliveryError(
                    "delivery_source_changed",
                    "project inputs changed during export",
                    outcome="blocked",
                )
            status = fresh_status
            qa_path = staging / "qa/technical-status.json"
            _write_json_new(qa_path, status)
            exported.append(
                {
                    "role": "technical-qa",
                    "path": "qa/technical-status.json",
                    "source_path": None,
                    "sha256": _sha256(qa_path),
                    "bytes": qa_path.stat().st_size,
                }
            )
            manifest = {
                "schema": BUNDLE_SCHEMA,
                "bundle_id": bundle_id,
                "project": status["project"],
                "created_at": _now(),
                "publication_ready": False,
                "human_approval": False,
                "artifacts": exported,
            }
            _write_json_new(staging / "artifact-manifest.json", manifest)
            try:
                os.rename(staging, bundle)
            except FileExistsError:
                if not _verify_existing_bundle(bundle, bundle_id):
                    raise DeliveryError(
                        "delivery_target_conflict",
                        "concurrent immutable bundle target differs",
                    )
                reused = True
            else:
                reused = False
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    receipt_value = {
        "schema": IDEMPOTENCY_SCHEMA,
        "idempotency_key": idempotency_key,
        "bundle_id": bundle_id,
        "destination_root": str(root),
        "bundle_path": str(bundle),
        "created_at": _now(),
    }
    if not receipt_path.exists():
        _write_json_new(receipt_path, receipt_value)
    return {
        "schema": EXPORT_SCHEMA,
        "project": status["project"],
        "status": "exported",
        "bundle_id": bundle_id,
        "bundle_path": str(bundle),
        "reused": reused,
        "publication_ready": False,
        "human_approval": False,
    }


def export_delivery(
    project_root: Path,
    *,
    destination: Path | None,
    idempotency_key: str,
) -> dict:
    with mutation_barrier(project_root):
        return _export_delivery_unlocked(
            project_root,
            destination=destination,
            idempotency_key=idempotency_key,
        )


def _export_diagnostics_unlocked(
    project_root: Path, *, destination: Path | None, idempotency_key: str
) -> dict:
    """Export bounded evidence of a blocked state, never a delivery-ready claim."""
    import math
    from job_interface import redact

    if KEY.fullmatch(idempotency_key) is None:
        raise DeliveryError("invalid_input", "invalid idempotency key")
    project = _direct_project(project_root)
    status = technical_status(project)

    def clean(value):
        if isinstance(value, dict):
            return {
                key: clean(item)
                for key, item in value.items()
                if key not in {"checked_at", "project_root"}
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            return redact(value.replace(str(project), "<project>"))
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    report = {
        "schema": "video_studio.diagnostic_report.v1",
        "kind": "diagnostic_only",
        "project": project.name,
        "technical_status": clean(status),
        "delivery_ready": False,
        "publication_ready": False,
        "human_approval": False,
    }
    serialized = json.dumps(
        report, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    bundle_id = hashlib.sha256(serialized).hexdigest()
    root = _destination_root(destination)
    collection = _direct_child_directory(root, "diagnostics")
    project_destination = _direct_child_directory(collection, project.name)
    bundle = project_destination / bundle_id
    state = _direct_child_directory(
        _direct_child_directory(
            _direct_child_directory(project, ".hvp"), "local-delivery"
        ),
        "exports",
    )
    receipt_path = state / f"{idempotency_key}.json"
    expected_receipt = {
        "schema": "video_studio.diagnostic_idempotency.v1",
        "bundle_id": bundle_id,
        "destination_root": str(root),
    }
    if receipt_path.exists() or receipt_path.is_symlink():
        try:
            if (
                receipt_path.is_symlink()
                or json.loads(receipt_path.read_text()) != expected_receipt
            ):
                raise ValueError("different request")
        except (OSError, ValueError, UnicodeError) as error:
            raise DeliveryError(
                "idempotency_conflict", "key is bound to another export"
            ) from error

    def existing_matches():
        if bundle.is_symlink() or not bundle.is_dir():
            return False
        evidence = bundle / "diagnostic.json"
        manifest = bundle / "artifact-manifest.json"
        try:
            return (
                not evidence.is_symlink()
                and not manifest.is_symlink()
                and json.loads(evidence.read_text()) == report
                and json.loads(manifest.read_text())
                == {
                    "schema": "video_studio.diagnostic_bundle.v1",
                    "bundle_id": bundle_id,
                    "kind": "diagnostic_only",
                    "delivery_ready": False,
                    "publication_ready": False,
                    "human_approval": False,
                    "artifacts": [
                        {
                            "path": "diagnostic.json",
                            "sha256": _sha256(evidence),
                            "bytes": evidence.stat().st_size,
                        }
                    ],
                }
            )
        except (OSError, ValueError, UnicodeError):
            return False

    reused = bundle.exists()
    if reused or bundle.is_symlink():
        if not existing_matches():
            raise DeliveryError("delivery_target_conflict", "diagnostic bundle differs")
    else:
        staging = project_destination / f".{bundle_id}.staging-{uuid.uuid4()}"
        staging.mkdir(mode=0o700)
        try:
            evidence = staging / "diagnostic.json"
            _write_json_new(evidence, report)
            _write_json_new(
                staging / "artifact-manifest.json",
                {
                    "schema": "video_studio.diagnostic_bundle.v1",
                    "bundle_id": bundle_id,
                    "kind": "diagnostic_only",
                    "delivery_ready": False,
                    "publication_ready": False,
                    "human_approval": False,
                    "artifacts": [
                        {
                            "path": "diagnostic.json",
                            "sha256": _sha256(evidence),
                            "bytes": evidence.stat().st_size,
                        }
                    ],
                },
            )
            try:
                os.rename(staging, bundle)
            except OSError:
                if not existing_matches():
                    raise
                reused = True
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    if not receipt_path.exists():
        _write_json_new(receipt_path, expected_receipt)
    return {
        "schema": "video_studio.diagnostic_export.v1",
        "project": project.name,
        "status": "diagnostic_only",
        "bundle_id": bundle_id,
        "bundle_path": str(bundle),
        "reused": reused,
        "delivery_ready": False,
        "publication_ready": False,
        "human_approval": False,
    }


def export_diagnostics(
    project_root: Path,
    *,
    destination: Path | None,
    idempotency_key: str,
) -> dict:
    with mutation_barrier(project_root):
        return _export_diagnostics_unlocked(
            project_root,
            destination=destination,
            idempotency_key=idempotency_key,
        )


def _response(outcome: str, code: str, project: str | None, data=None) -> dict:
    return {
        "schema_version": 1,
        "outcome": outcome,
        "code": code,
        "project": project,
        "data": data,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    status_parser = subcommands.add_parser("status")
    status_parser.add_argument("project", type=Path)
    export_parser = subcommands.add_parser("export")
    export_parser.add_argument("project", type=Path)
    export_parser.add_argument("--destination", type=Path)
    export_parser.add_argument("--idempotency-key", required=True)
    export_parser.add_argument("--diagnostic", action="store_true")
    args = parser.parse_args(argv)
    project = str(args.project)
    try:
        if args.command == "status":
            data = technical_status(args.project)
            outcome = "ok" if not data["blockers"] else "blocked"
            code = "delivery_technical_ready" if outcome == "ok" else "delivery_blocked"
        else:
            exporter = export_diagnostics if args.diagnostic else export_delivery
            data = exporter(
                args.project,
                destination=args.destination,
                idempotency_key=args.idempotency_key,
            )
            outcome = "ok"
            code = "diagnostics_exported" if args.diagnostic else "delivery_exported"
        print(json.dumps(_response(outcome, code, project, data), sort_keys=True))
        return 0 if outcome == "ok" else 3
    except DeliveryError as exc:
        print(
            json.dumps(
                _response(
                    exc.outcome,
                    exc.code,
                    project,
                    {"message": exc.message},
                ),
                sort_keys=True,
            )
        )
        return (
            3 if exc.outcome == "blocked" else 2 if exc.code == "invalid_input" else 4
        )
    except Exception as exc:
        print(
            json.dumps(
                _response("error", "internal_error", project, {"message": str(exc)})
            )
        )
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
