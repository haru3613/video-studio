#!/usr/bin/env python3
"""Read and resolve dashboard review notes through the shared review store.

This module is deliberately stdlib-only.  It reuses ``ReviewStore`` without
importing the FastAPI review server, so the stable CLI and MCP runner work under
``/usr/bin/python3 -I -S``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent / "dashboard"))
from review_store import ReviewStore  # noqa: E402
import review_domain  # noqa: E402
import version_archive  # noqa: E402


READ_SCHEMA = "video_studio.review_feedback.v1"
RESOLUTION_SCHEMA = "video_studio.review_resolution.v1"
ERROR_SCHEMA = "video_studio.review_feedback_error.v1"
ADD_SCHEMA = "video_studio.review_add.v1"
ADD_REQUEST_SCHEMA = "video_studio.review_add_request.v1"
PACKAGE_SCHEMA = "haru.review_package.v1"
SOURCE = "studio"
MAX_JSON_BYTES = 1024 * 1024
MAX_ASSETS = 100
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
COVER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
KIND_EXTS = {"video": VIDEO_EXTS, "audio": AUDIO_EXTS | VIDEO_EXTS, "cover": COVER_EXTS}
HIDDEN_MEDIA_PREFIXES = (
    (".hvp", "staging", "narration-candidates"),
    (".hvp", "staging", "narration-derivations"),
)


class ReviewFeedbackError(ValueError):
    def __init__(self, code: str, outcome: str, exit_code: int):
        super().__init__(code)
        self.code = code
        self.outcome = outcome
        self.exit_code = exit_code


def _invalid(code: str = "invalid_input") -> ReviewFeedbackError:
    return ReviewFeedbackError(code, "invalid", 2)


def _blocked(code: str) -> ReviewFeedbackError:
    return ReviewFeedbackError(code, "blocked", 3)


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def _workspace_project(value: str) -> tuple[Path, Path]:
    raw = Path(value).expanduser()
    if not raw.is_absolute() or ".." in raw.parts or raw.is_symlink():
        raise _invalid()
    try:
        project = raw.resolve(strict=True)
    except OSError as error:
        raise _invalid("project_not_found") from error
    if not project.is_dir() or not _identifier(project.name):
        raise _invalid("project_not_found")
    projects = project.parent
    workspace = projects.parent
    if projects.name != "projects" or projects.is_symlink() or workspace.is_symlink():
        raise _invalid("project_outside_workspace")
    try:
        if project.parent != (workspace / "projects").resolve(strict=True):
            raise _invalid("project_outside_workspace")
    except OSError as error:
        raise _invalid("project_outside_workspace") from error
    return workspace, project


def _project_by_name(workspace: Path, value: object) -> Path:
    if not _identifier(value):
        raise _invalid("review_store_corrupt")
    raw = workspace / "projects" / str(value)
    if raw.is_symlink():
        raise _blocked("review_stale")
    try:
        resolved = raw.resolve(strict=True)
        projects = (workspace / "projects").resolve(strict=True)
    except OSError as error:
        raise _blocked("review_stale") from error
    if not resolved.is_dir() or resolved.parent != projects:
        raise _blocked("review_stale")
    return resolved


def _asset_parts(value: object, kind: object) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or kind not in KIND_EXTS
    ):
        raise _invalid("review_store_corrupt")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.suffix.lower() not in KIND_EXTS[str(kind)]
    ):
        raise _invalid("review_store_corrupt")
    hidden = [part for part in relative.parts if part.startswith(".")]
    if hidden:
        if not any(relative.parts[: len(prefix)] == prefix for prefix in HIDDEN_MEDIA_PREFIXES):
            raise _invalid("review_store_corrupt")
        if any(part.startswith(".") for part in relative.parts[3:]):
            raise _invalid("review_store_corrupt")
    return relative


def _asset_path(workspace: Path, snapshot: dict) -> Path:
    if snapshot.get("source") != SOURCE:
        raise _invalid("review_store_corrupt")
    project = _project_by_name(workspace, snapshot.get("project"))
    relative = _asset_parts(snapshot.get("path"), snapshot.get("kind"))
    cursor = project
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise _blocked("review_stale")
    try:
        resolved = cursor.resolve(strict=True)
    except OSError as error:
        raise _blocked("review_stale") from error
    if not resolved.is_relative_to(project) or not resolved.is_file():
        raise _blocked("review_stale")
    return resolved


def _stable_hash(path: Path) -> tuple[str, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise _blocked("review_stale") from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise _blocked("review_stale")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
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
        raise _blocked("review_stale")
    return digest.hexdigest(), after.st_size


def _hash_or_missing(workspace: Path, snapshot: dict) -> tuple[str | None, int]:
    try:
        return _stable_hash(_asset_path(workspace, snapshot))
    except ReviewFeedbackError as error:
        if error.code == "review_stale":
            return None, 0
        raise


def _snapshot_fingerprint(workspace: Path, snapshot: dict) -> tuple[str | None, int]:
    digest, size = _hash_or_missing(workspace, snapshot)
    if digest == snapshot.get("sha256") and size == snapshot.get("bytes"):
        return digest, size
    if snapshot.get("kind") == "video" and snapshot.get("path") == "output/final.mp4":
        try:
            project = _project_by_name(workspace, snapshot.get("project"))
            archived = version_archive.archived_video(
                project, snapshot.get("sha256"), snapshot.get("bytes")
            )
        except (OSError, TypeError, ValueError):
            archived = None
        if archived is not None:
            try:
                return _stable_hash(archived)
            except ReviewFeedbackError:
                pass
    return None, 0


def _duration(path: Path | None, kind: str) -> float | None:
    if kind == "cover" or path is None or not path.is_file():
        return None
    ffprobe = shutil.which(
        "ffprobe", path=os.pathsep.join([*os.get_exec_path(), "/opt/homebrew/bin", "/usr/local/bin"])
    )
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        value = float(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return value if result.returncode == 0 and math.isfinite(value) and value >= 0 else None


def _bounded_json(path: Path) -> object | None:
    if path.is_symlink():
        raise _invalid("review_package_invalid")
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise _invalid("review_package_invalid")
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise _invalid("review_package_invalid") from error

    def reject_constant(_: str):
        raise ValueError

    try:
        return json.loads(raw, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise _invalid("review_package_invalid") from error


def _project_json(project: Path, relative: str) -> object | None:
    parts = PurePosixPath(relative)
    if parts.is_absolute() or any(part in {"", ".", ".."} for part in parts.parts):
        raise _invalid("review_package_invalid")
    cursor = project
    for part in parts.parts:
        cursor /= part
        if cursor.is_symlink():
            raise _invalid("review_package_invalid")
    return _bounded_json(cursor)


def _text(value: object, default: str, limit: int = 500) -> str:
    if isinstance(value, str) and value.strip() and len(value) <= limit:
        return value.strip()
    return default


def _chapters(value: object) -> list[dict]:
    if isinstance(value, dict):
        value = value.get("chapters")
    if not isinstance(value, list) or len(value) > 500:
        return []
    result = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        seconds = entry.get("seconds")
        if (
            isinstance(title, str)
            and title.strip()
            and len(title) <= 500
            and isinstance(seconds, (int, float))
            and not isinstance(seconds, bool)
            and math.isfinite(seconds)
            and seconds >= 0
        ):
            result.append({"title": title.strip(), "seconds": float(seconds)})
    return result


def _changes(value: object, asset_ids: set[str]) -> list[dict]:
    if not isinstance(value, list) or len(value) > 500:
        return []
    result = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title")
        description = entry.get("description")
        asset_id = entry.get("asset_id")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            continue
        if not isinstance(description, str) or len(description) > 5000:
            continue
        if asset_id is not None and asset_id not in asset_ids:
            continue
        normalized = {"title": title.strip(), "description": description.strip()}
        if asset_id is not None:
            normalized["asset_id"] = asset_id
        result.append(normalized)
    return result


def _fallback_manifest(project: Path) -> dict:
    title = project.name
    metadata = _project_json(project, "publish-metadata.json")
    if isinstance(metadata, dict):
        title = _text(metadata.get("title"), project.name)
    chapters = _chapters(_project_json(project, "authoring/chapters.json"))
    candidates = (
        ("video-current", "video", "目前影片", "output/final.mp4"),
        ("audio-current", "audio", "目前旁白", "narration-final.mp3"),
        ("cover-current", "cover", "目前封面", "output/cover.png"),
    )
    assets = []
    workspace = project.parent.parent
    for asset_id, kind, label, relative in candidates:
        snapshot = {
            "source": SOURCE,
            "project": project.name,
            "path": relative,
            "kind": kind,
        }
        digest, size = _hash_or_missing(workspace, snapshot)
        if digest is not None and size > 0:
            assets.append(
                {"id": asset_id, "kind": kind, "label": label, "path": relative, "role": "current"}
            )
    return {
        "schema": PACKAGE_SCHEMA,
        "title": title,
        "assets": assets,
        "changes": [],
        "chapters": chapters,
    }


def _current_package(workspace: Path, project: Path) -> tuple[str, list[dict]]:
    manifest = _project_json(project, "authoring/review-package.json")
    if manifest is None:
        manifest = _fallback_manifest(project)
    if not isinstance(manifest, dict) or manifest.get("schema") != PACKAGE_SCHEMA:
        raise _invalid("review_package_invalid")
    raw_assets = manifest.get("assets")
    if not isinstance(raw_assets, list) or len(raw_assets) > MAX_ASSETS:
        raise _invalid("review_package_invalid")

    assets = []
    seen: set[str] = set()
    for raw in raw_assets:
        if not isinstance(raw, dict):
            raise _invalid("review_package_invalid")
        asset_id = raw.get("id")
        kind = raw.get("kind")
        if not _identifier(asset_id) or asset_id in seen or kind not in KIND_EXTS:
            raise _invalid("review_package_invalid")
        source = raw.get("source", SOURCE)
        asset_project = raw.get("project", project.name)
        if source != SOURCE or not _identifier(asset_project):
            raise _invalid("review_package_invalid")
        seen.add(str(asset_id))
        snapshot = {
            "source": source,
            "project": asset_project,
            "path": raw.get("path"),
            "kind": kind,
        }
        _asset_parts(snapshot["path"], kind)
        digest, size = _hash_or_missing(workspace, snapshot)
        try:
            media_path = _asset_path(workspace, snapshot) if digest is not None else None
        except ReviewFeedbackError:
            media_path = None
        preview = raw.get("preview_seconds")
        if not (
            preview is None
            or isinstance(preview, (int, float))
            and not isinstance(preview, bool)
            and math.isfinite(preview)
            and preview > 0
        ):
            raise _invalid("review_package_invalid")
        assets.append(
            {
                "id": asset_id,
                "kind": kind,
                "label": _text(raw.get("label"), str(asset_id)),
                "role": _text(raw.get("role"), "current", 100),
                "source": source,
                "project": asset_project,
                "path": raw.get("path"),
                "sha256": digest,
                "bytes": size,
                "url": (
                    f"/api/review/{quote(SOURCE)}/{quote(project.name)}/asset/"
                    f"{quote(str(asset_id))}?sha256={digest}"
                    if digest is not None
                    else None
                ),
                "duration_seconds": _duration(media_path, str(kind)),
                "preview_seconds": float(preview) if preview is not None else None,
            }
        )

    changes = _changes(manifest.get("changes", []), seen)
    chapters = _chapters(manifest.get("chapters", []))
    basis = {
        "schema": PACKAGE_SCHEMA,
        "title": _text(manifest.get("title"), project.name),
        "assets": [
            {
                key: asset[key]
                for key in (
                    "id",
                    "kind",
                    "label",
                    "role",
                    "source",
                    "project",
                    "path",
                    "sha256",
                    "bytes",
                    "preview_seconds",
                )
            }
            for asset in assets
        ],
        "changes": changes,
        "chapters": chapters,
    }
    package_id = hashlib.sha256(
        json.dumps(
            basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return package_id, assets


def _validate_comment(comment: object) -> dict:
    if not isinstance(comment, dict):
        raise _invalid("review_store_corrupt")
    try:
        parsed_comment_id = uuid.UUID(comment.get("id"))
    except (AttributeError, TypeError, ValueError) as error:
        raise _invalid("review_store_corrupt") from error
    if str(parsed_comment_id) != comment.get("id"):
        raise _invalid("review_store_corrupt")
    asset = comment.get("asset")
    if (
        not isinstance(asset, dict)
        or not _identifier(asset.get("id"))
        or asset.get("kind") not in KIND_EXTS
        or asset.get("source") != SOURCE
        or not _identifier(asset.get("project"))
        or not isinstance(asset.get("sha256"), str)
        or not SHA256.fullmatch(asset["sha256"])
        or isinstance(asset.get("bytes"), bool)
        or not isinstance(asset.get("bytes"), int)
        or asset["bytes"] <= 0
    ):
        raise _invalid("review_store_corrupt")
    _asset_parts(asset.get("path"), asset.get("kind"))
    if (
        not isinstance(comment.get("package_id"), str)
        or not SHA256.fullmatch(comment["package_id"])
        or comment.get("status") not in {"open", "resolved"}
        or not isinstance(comment.get("body"), str)
        or not comment["body"].strip()
        or len(comment["body"]) > 5000
    ):
        raise _invalid("review_store_corrupt")
    return comment


def _public_comment(
    comment: dict, workspace: Path, current_assets: list[dict]
) -> dict:
    comment = _validate_comment(comment)
    asset = comment["asset"]
    current = any(
        item["id"] == asset["id"]
        and item["source"] == asset["source"]
        and item["project"] == asset["project"]
        and item["sha256"] == asset["sha256"]
        for item in current_assets
    )
    digest, size = _snapshot_fingerprint(workspace, asset)
    available = digest == asset["sha256"] and size == asset["bytes"]
    public_asset = {
        key: asset.get(key)
        for key in ("id", "kind", "label", "source", "project", "path", "sha256", "bytes")
    }
    return {
        "id": comment["id"],
        "package_id": comment["package_id"],
        "asset": public_asset,
        "timestamp_seconds": comment.get("timestamp_seconds"),
        "body": comment["body"],
        "status": comment["status"],
        "created_at": comment.get("created_at"),
        "updated_at": comment.get("updated_at"),
        "is_current": current,
        "asset_available": available,
        "stale": not current or not available,
    }


def _store(workspace: Path, project: Path) -> ReviewStore:
    return ReviewStore(project, workspace / ".video-studio" / "review")


def read_feedback(project_value: str) -> dict:
    workspace, project = _workspace_project(project_value)
    package_id, assets = _current_package(workspace, project)
    comments = [
        _public_comment(comment, workspace, assets)
        for comment in _store(workspace, project).read_comments()
    ]
    counts = {
        "total": len(comments),
        "open": sum(comment["status"] == "open" for comment in comments),
        "resolved": sum(comment["status"] == "resolved" for comment in comments),
        "stale": sum(comment["stale"] for comment in comments),
    }
    return {
        "schema": READ_SCHEMA,
        "outcome": "ok",
        "code": "review_feedback",
        "project": project.name,
        "package_id": package_id,
        "assets": [
            {
                key: asset.get(key)
                for key in (
                    "id",
                    "kind",
                    "label",
                    "role",
                    "source",
                    "project",
                    "path",
                    "sha256",
                    "bytes",
                    "duration_seconds",
                    "preview_seconds",
                )
            }
            for asset in assets
        ],
        "counts": counts,
        "comments": comments,
    }


def resolve_feedback(
    project_value: str,
    *,
    comment_id: str,
    status_value: str,
    expected_package_id: str,
    expected_asset_sha256: str,
) -> dict:
    workspace, project = _workspace_project(project_value)
    try:
        parsed_comment_id = uuid.UUID(comment_id)
    except ValueError as error:
        raise _invalid("comment_not_found") from error
    if str(parsed_comment_id) != comment_id:
        raise _invalid("comment_not_found")
    if status_value not in {"open", "resolved"}:
        raise _invalid()
    if not SHA256.fullmatch(expected_package_id) or not SHA256.fullmatch(
        expected_asset_sha256
    ):
        raise _invalid()

    store = _store(workspace, project)

    try:
        comment, code, package_id = review_domain.resolve_comment(
            store,
            comment_id=comment_id,
            status=status_value,
            expected_package_id=expected_package_id,
            expected_asset_sha256=expected_asset_sha256,
        )
    except review_domain.ReviewDomainError as error:
        if error.code in {"review_conflict", "review_stale"}:
            raise _blocked(error.code) from error
        raise _invalid(error.code) from error
    _, assets = _current_package(workspace, project)
    return {
        "schema": RESOLUTION_SCHEMA,
        "outcome": "ok",
        "code": code,
        "project": project.name,
        "package_id": package_id,
        "comment": _public_comment(comment, workspace, assets),
        "effects": {
            "technical_qa_pass": False,
            "human_approval": False,
            "publishing_approval": False,
        },
    }


def _add_request(project: Path, request_id: str) -> dict:
    if not REQUEST_ID.fullmatch(request_id):
        raise _invalid()
    state = project / ".hvp"
    directory = state / "review-requests"
    request_path = directory / f"{request_id}.json"
    for path in (state, directory, request_path):
        if path.is_symlink():
            raise _invalid("review_request_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(request_path, flags)
    except OSError as error:
        raise _invalid("review_request_invalid") from error
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > 64 * 1024
            or metadata.st_mode & 0o077
        ):
            raise _invalid("review_request_invalid")
        raw = b""
        while len(raw) <= 64 * 1024:
            chunk = os.read(fd, 64 * 1024 + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(fd)
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise _invalid("review_request_invalid") from error
    expected = {
        "schema",
        "client_id",
        "package_id",
        "asset_id",
        "asset_sha256",
        "timestamp_seconds",
        "body",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise _invalid("review_request_invalid")
    if value.pop("schema") != ADD_REQUEST_SCHEMA:
        raise _invalid("review_request_invalid")
    return value


def add_feedback(project_value: str, *, request_id: str) -> dict:
    workspace, project = _workspace_project(project_value)
    payload = _add_request(project, request_id)
    store = _store(workspace, project)

    def load_current(_include_durations: bool) -> dict:
        package_id, assets = _current_package(workspace, project)
        return {"package_id": package_id, "assets": assets}

    try:
        comment, code, package_id = review_domain.add_comment(
            store,
            payload,
            load_current=load_current,
            snapshot_fingerprint=lambda asset: _snapshot_fingerprint(workspace, asset),
        )
    except review_domain.ReviewDomainError as error:
        if error.code in {"review_conflict", "review_stale"}:
            raise _blocked(error.code) from error
        raise _invalid() from error
    _, assets = _current_package(workspace, project)
    return {
        "schema": ADD_SCHEMA,
        "outcome": "ok",
        "code": code,
        "project": project.name,
        "package_id": package_id,
        "comment": _public_comment(comment, workspace, assets),
        "effects": {
            "technical_qa_pass": False,
            "human_approval": False,
            "publishing_approval": False,
        },
    }


class ContractParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _invalid()


def _parser() -> argparse.ArgumentParser:
    parser = ContractParser(description="Read, add, or resolve Video Studio review feedback")
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("read")
    read.add_argument("project_root")
    add = commands.add_parser("add")
    add.add_argument("project_root")
    add.add_argument("--request-id", required=True)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("project_root")
    resolve.add_argument("--comment-id", required=True)
    resolve.add_argument("--status", required=True)
    resolve.add_argument("--expected-package-id", required=True)
    resolve.add_argument("--expected-asset-sha256", required=True)
    return parser


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "read":
            result = read_feedback(args.project_root)
        elif args.command == "add":
            result = add_feedback(args.project_root, request_id=args.request_id)
        else:
            result = resolve_feedback(
                args.project_root,
                comment_id=args.comment_id,
                status_value=args.status,
                expected_package_id=args.expected_package_id,
                expected_asset_sha256=args.expected_asset_sha256,
            )
        _emit(result)
        return 0
    except ReviewFeedbackError as error:
        _emit(
            {
                "schema": ERROR_SCHEMA,
                "outcome": error.outcome,
                "code": error.code,
            }
        )
        return error.exit_code
    except (OSError, RuntimeError, TypeError, ValueError):
        _emit(
            {
                "schema": ERROR_SCHEMA,
                "outcome": "error",
                "code": "internal_error",
            }
        )
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
