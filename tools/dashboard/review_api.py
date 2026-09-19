"""FastAPI routes for the localhost creative review hub."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import mimetypes
import re
import secrets
import shutil
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

try:
    from .review_store import ReviewStore
except ImportError:  # server.py is also run directly from tools/dashboard
    from review_store import ReviewStore


PACKAGE_SCHEMA = "haru.review_package.v1"
MAX_JSON_BYTES = 1024 * 1024
MAX_WRITE_BYTES = 64 * 1024
MAX_ASSETS = 100
MAX_COMMENTS_BODY = 5000
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
COVER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
KIND_EXTS = {"video": VIDEO_EXTS, "audio": AUDIO_EXTS | VIDEO_EXTS, "cover": COVER_EXTS}
HIDDEN_MEDIA_PREFIXES = (
    (".hvp", "staging", "narration-candidates"),
    (".hvp", "staging", "narration-derivations"),
)


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def _error(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def _bounded_json_file(path: Path):
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("JSON file is too large")
        return json.loads(
            path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant
        )
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON file: {path.name}") from exc


def _valid_identifier(value) -> bool:
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def _path_has_symlink(base: Path, relative: PurePosixPath) -> bool:
    cursor = base
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return True
    return False


def _project_root(source: str, project: str, roots: dict[str, Path]) -> Path:
    if not _valid_identifier(source) or not _valid_identifier(project):
        raise _error(404, "project not found")
    configured = roots.get(source)
    if configured is None:
        raise _error(404, "project not found")
    try:
        root = Path(configured).resolve(strict=True)
        candidate = Path(configured) / project
        if candidate.is_symlink():
            raise _error(404, "project not found")
        resolved = candidate.resolve(strict=True)
    except (OSError, ValueError):
        raise _error(404, "project not found")
    if resolved.parent != root or not resolved.is_dir():
        raise _error(404, "project not found")
    return resolved


def _asset_parts(value: str, kind: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid asset path")
    posix = PurePosixPath(value)
    if (
        posix.is_absolute()
        or posix.as_posix() != value
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise ValueError("invalid asset path")
    hidden = [part for part in posix.parts if part.startswith(".")]
    if hidden:
        if not any(posix.parts[: len(prefix)] == prefix for prefix in HIDDEN_MEDIA_PREFIXES):
            raise ValueError("hidden asset path is not allowed")
        if any(part.startswith(".") for part in posix.parts[3:]):
            raise ValueError("hidden asset path is not allowed")
    if kind not in KIND_EXTS or posix.suffix.lower() not in KIND_EXTS[kind]:
        raise ValueError("unsupported asset kind or extension")
    return posix


def _asset_path(project_root: Path, value: str, kind: str) -> Path:
    posix = _asset_parts(value, kind)

    cursor = project_root
    try:
        for part in posix.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("asset symlinks are not allowed")
        resolved = cursor.resolve(strict=False)
    except (OSError, ValueError) as exc:
        raise ValueError("invalid asset path") from exc
    if not resolved.is_relative_to(project_root):
        raise ValueError("asset path leaves project")
    return resolved


def _sha256(path: Path) -> tuple[str | None, int]:
    try:
        if not path.is_file() or path.is_symlink():
            return None, 0
        size = path.stat().st_size
        if size <= 0:
            return None, size
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest(), size
    except OSError:
        return None, 0


def _duration(path: Path, kind: str) -> float | None:
    if kind == "cover" or not path.is_file():
        return None
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        homebrew = Path("/opt/homebrew/bin/ffprobe")
        ffprobe = str(homebrew) if homebrew.is_file() else None
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


def _text(value, *, default: str, limit: int = 500) -> str:
    return value.strip() if isinstance(value, str) and value.strip() and len(value) <= limit else default


def _chapters(value) -> list[dict]:
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


def _fallback_manifest(project_root: Path, project: str) -> dict:
    title = project
    metadata_path = project_root / "publish-metadata.json"
    if metadata_path.is_file() and not metadata_path.is_symlink():
        try:
            metadata = _bounded_json_file(metadata_path)
            if isinstance(metadata, dict):
                title = _text(metadata.get("title"), default=project)
        except ValueError:
            pass

    chapters = []
    chapters_path = project_root / "authoring" / "chapters.json"
    if (
        chapters_path.is_file()
        and not _path_has_symlink(project_root, PurePosixPath("authoring/chapters.json"))
    ):
        try:
            chapters = _chapters(_bounded_json_file(chapters_path))
        except ValueError:
            pass

    candidates = (
        ("video-current", "video", "目前影片", "output/final.mp4"),
        ("audio-current", "audio", "目前旁白", "narration-final.mp3"),
        ("cover-current", "cover", "目前封面", "output/cover.png"),
    )
    assets = []
    for asset_id, kind, label, relative in candidates:
        try:
            path = _asset_path(project_root, relative, kind)
        except ValueError:
            continue
        digest, size = _sha256(path)
        if digest is not None and size > 0:
            assets.append(
                {"id": asset_id, "kind": kind, "label": label, "path": relative, "role": "current"}
            )
    return {"schema": PACKAGE_SCHEMA, "title": title, "assets": assets, "changes": [], "chapters": chapters}


def _manifest(project_root: Path, project: str) -> tuple[dict, list[str]]:
    path = project_root / "authoring" / "review-package.json"
    if not path.exists():
        return _fallback_manifest(project_root, project), []
    if _path_has_symlink(project_root, PurePosixPath("authoring/review-package.json")):
        raise _error(422, "review package must not be a symlink")
    try:
        manifest = _bounded_json_file(path)
    except ValueError as exc:
        raise _error(422, str(exc))
    if not isinstance(manifest, dict) or manifest.get("schema") != PACKAGE_SCHEMA:
        raise _error(422, "unsupported review package")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or len(assets) > MAX_ASSETS:
        raise _error(422, "review package assets are invalid")
    return manifest, []


def _normalize_changes(value, asset_ids: set[str]) -> list[dict]:
    if not isinstance(value, list) or len(value) > 500:
        return []
    changes = []
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
        change = {"title": title.strip(), "description": description.strip()}
        if asset_id is not None:
            change["asset_id"] = asset_id
        changes.append(change)
    return changes


def _build_review(
    source: str,
    project: str,
    roots: dict[str, Path],
    token: str,
    storage_root: Path | None,
    *,
    include_comments: bool = True,
    include_durations: bool = True,
):
    requested_root = _project_root(source, project, roots)
    manifest, warnings = _manifest(requested_root, project)
    title = _text(manifest.get("title"), default=project)
    normalized_assets = []
    seen = set()
    raw_assets = manifest.get("assets", [])
    for raw in raw_assets:
        if not isinstance(raw, dict):
            raise _error(422, "review asset is invalid")
        asset_id = raw.get("id")
        kind = raw.get("kind")
        if not _valid_identifier(asset_id) or asset_id in seen or kind not in KIND_EXTS:
            raise _error(422, "review asset id or kind is invalid")
        seen.add(asset_id)
        asset_source = raw.get("source", source)
        asset_project = raw.get("project", project)
        if not _valid_identifier(asset_source) or not _valid_identifier(asset_project):
            raise _error(422, "review asset source or project is invalid")
        try:
            _asset_parts(raw.get("path"), kind)
            asset_project_root = _project_root(asset_source, asset_project, roots)
            path = _asset_path(asset_project_root, raw.get("path"), kind)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            path = None
        except ValueError as exc:
            raise _error(422, str(exc))
        digest, size = _sha256(path) if path is not None else (None, 0)
        if digest is None:
            warnings.append(f"Asset {asset_id} is missing or empty: {raw.get('path')}")
        preview = raw.get("preview_seconds")
        if not (
            preview is None
            or isinstance(preview, (int, float))
            and not isinstance(preview, bool)
            and math.isfinite(preview)
            and preview > 0
        ):
            raise _error(422, "review asset preview_seconds is invalid")
        snapshot = {
            "id": asset_id,
            "kind": kind,
            "label": _text(raw.get("label"), default=asset_id),
            "role": _text(raw.get("role"), default="current", limit=100),
            "source": asset_source,
            "project": asset_project,
            "path": raw.get("path"),
            "sha256": digest,
            "bytes": size,
            "url": (
                f"/api/review/{quote(source)}/{quote(project)}/asset/{quote(asset_id)}?sha256={digest}"
                if digest is not None
                else None
            ),
            "duration_seconds": (
                _duration(path, kind) if digest is not None and include_durations else None
            ),
            "preview_seconds": float(preview) if preview is not None else None,
        }
        normalized_assets.append(snapshot)

    changes = _normalize_changes(manifest.get("changes", []), seen)
    chapters = _chapters(manifest.get("chapters", []))
    package_basis = {
        "schema": PACKAGE_SCHEMA,
        "title": title,
        "assets": [
            {key: asset[key] for key in ("id", "kind", "label", "role", "source", "project", "path", "sha256", "bytes", "preview_seconds")}
            for asset in normalized_assets
        ],
        "changes": changes,
        "chapters": chapters,
    }
    package_id = hashlib.sha256(
        json.dumps(package_basis, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    store = ReviewStore(requested_root, storage_root)
    comments = []
    if include_comments:
        comments = store.read_comments()
        current_by_version = {(asset["id"], asset["sha256"]) for asset in normalized_assets}
        for comment in comments:
            version = (comment.get("asset", {}).get("id"), comment.get("asset", {}).get("sha256"))
            comment["is_current"] = version in current_by_version
            try:
                snapshot_path = _snapshot_path(comment.get("asset"), roots)
                digest, size = _sha256(snapshot_path)
                comment["asset_available"] = digest == version[1] and size == comment["asset"].get("bytes")
            except (HTTPException, ValueError, TypeError):
                comment["asset_available"] = False
    return {
        "project": {"source": source, "name": project, "title": title},
        "package_id": package_id,
        "assets": normalized_assets,
        "changes": changes,
        "chapters": chapters,
        "comments": comments,
        "csrf_token": token,
        "warnings": warnings,
    }, store


def _snapshot_path(snapshot, roots: dict[str, Path]) -> Path:
    if not isinstance(snapshot, dict):
        raise ValueError("invalid asset snapshot")
    kind = snapshot.get("kind")
    root = _project_root(snapshot.get("source"), snapshot.get("project"), roots)
    return _asset_path(root, snapshot.get("path"), kind)


async def _json_request(request: Request, exact_keys: set[str]) -> dict:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise _error(415, "application/json is required")
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_WRITE_BYTES:
                raise _error(413, "request body is too large")
        except ValueError:
            raise _error(400, "invalid content length")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_WRITE_BYTES:
            raise _error(413, "request body is too large")
    try:
        value = json.loads(body, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _error(400, "invalid JSON")
    if not isinstance(value, dict) or set(value) != exact_keys:
        raise _error(422, "request fields are invalid")
    return value


def _require_write_auth(request: Request, token: str) -> None:
    origin = request.headers.get("origin")
    if not origin:
        raise _error(403, "same-origin Origin is required")
    parsed = urlsplit(origin)
    expected = urlsplit(str(request.base_url).rstrip("/"))
    if parsed.scheme not in {"http", "https"} or (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
        raise _error(403, "cross-origin writes are forbidden")
    supplied = request.headers.get("x-haru-review-csrf", "")
    if not secrets.compare_digest(supplied, token):
        raise _error(403, "invalid CSRF token")


def _public_comment(comment: dict) -> dict:
    return {key: value for key, value in comment.items() if not key.startswith("_")}


def _validate_timestamp(value, asset: dict) -> float | None:
    if asset["kind"] == "cover":
        if value is not None:
            raise _error(422, "cover comments require a null timestamp")
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise _error(422, "timestamp must be finite")
    duration = asset.get("duration_seconds")
    if duration is None or value < 0 or value > duration:
        raise _error(422, "timestamp is outside the asset duration")
    return float(value)


def _format_time(value) -> str:
    if value is None:
        return "cover"
    minutes, seconds = divmod(float(value), 60)
    return f"{int(minutes):02d}:{seconds:05.2f}"


def _markdown_export(review: dict) -> str:
    lines = [f"# {review['project']['title']} — review comments", ""]
    for comment in review["comments"]:
        asset = comment["asset"]
        lines.extend(
            [
                f"## {_format_time(comment['timestamp_seconds'])} · {asset['label']}",
                "",
                f"- Status: {comment['status']}",
                f"- Asset: `{asset['id']}` (`{asset['sha256']}`)",
                f"- Version: `{comment['package_id']}`",
                f"- Created: {comment['created_at']}",
                "",
                comment["body"],
                "",
            ]
        )
    return "\n".join(lines)


def register_review_routes(app: FastAPI, roots: dict[str, Path], storage_root: Path | None = None):
    """Attach review-hub routes to an existing localhost dashboard app."""
    token = secrets.token_urlsafe(32)
    normalized_roots = {key: Path(value) for key, value in roots.items()}
    storage = Path(storage_root) if storage_root is not None else None

    @app.get("/api/review/{source}/{project}")
    def get_review(source: str, project: str):
        review, _ = _build_review(source, project, normalized_roots, token, storage)
        review["comments"] = [_public_comment(comment) for comment in review["comments"]]
        return review

    @app.post("/api/review/{source}/{project}/comments", status_code=201)
    async def create_comment(source: str, project: str, request: Request):
        _require_write_auth(request, token)
        payload = await _json_request(
            request,
            {"client_id", "package_id", "asset_id", "asset_sha256", "timestamp_seconds", "body"},
        )
        try:
            uuid.UUID(payload["client_id"])
        except (AttributeError, TypeError, ValueError):
            raise _error(422, "client_id must be a UUID")
        body = payload["body"]
        if not isinstance(body, str) or not body.strip() or len(body) > MAX_COMMENTS_BODY:
            raise _error(422, "comment body must contain 1 to 5000 characters")
        review, store = _build_review(source, project, normalized_roots, token, storage)
        existing = next(
            (item for item in store.read_comments() if item.get("client_id") == payload["client_id"]),
            None,
        )
        if existing is not None:
            same = (
                existing.get("package_id") == payload["package_id"]
                and existing.get("asset", {}).get("id") == payload["asset_id"]
                and existing.get("asset", {}).get("sha256") == payload["asset_sha256"]
                and existing.get("timestamp_seconds") == payload["timestamp_seconds"]
                and existing.get("body") == body.strip()
            )
            if not same:
                raise _error(409, "client_id was already used with different content")
            return {"comment": _public_comment(existing)}
        if payload["package_id"] != review["package_id"]:
            raise _error(409, "review package changed")
        asset = next((item for item in review["assets"] if item["id"] == payload["asset_id"]), None)
        if asset is None or asset["sha256"] is None or payload["asset_sha256"] != asset["sha256"]:
            raise _error(409, "review asset changed")
        timestamp = _validate_timestamp(payload["timestamp_seconds"], asset)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        snapshot_keys = ("id", "kind", "label", "source", "project", "path", "sha256", "bytes", "url")
        snapshot = {key: asset[key] for key in snapshot_keys}

        def add(comments):
            existing = next((item for item in comments if item.get("client_id") == payload["client_id"]), None)
            if existing is not None:
                same = (
                    existing.get("package_id") == payload["package_id"]
                    and existing.get("asset", {}).get("id") == payload["asset_id"]
                    and existing.get("asset", {}).get("sha256") == payload["asset_sha256"]
                    and existing.get("timestamp_seconds") == timestamp
                    and existing.get("body") == body.strip()
                )
                if not same:
                    raise _error(409, "client_id was already used with different content")
                return existing
            try:
                latest, _ = _build_review(
                    source,
                    project,
                    normalized_roots,
                    token,
                    storage,
                    include_comments=False,
                    include_durations=False,
                )
            except (HTTPException, RuntimeError):
                raise _error(409, "review package changed")
            latest_asset = next(
                (item for item in latest["assets"] if item["id"] == payload["asset_id"]),
                None,
            )
            if (
                latest["package_id"] != payload["package_id"]
                or latest_asset is None
                or latest_asset["sha256"] != payload["asset_sha256"]
            ):
                raise _error(409, "review package changed")
            try:
                current_path = _snapshot_path(snapshot, normalized_roots)
            except (HTTPException, ValueError):
                raise _error(409, "review asset changed")
            current_digest, current_size = _sha256(current_path)
            if current_digest != snapshot["sha256"] or current_size != snapshot["bytes"]:
                raise _error(409, "review asset changed")
            comment = {
                "id": str(uuid.uuid4()),
                "client_id": payload["client_id"],
                "package_id": payload["package_id"],
                "asset": snapshot,
                "timestamp_seconds": timestamp,
                "body": body.strip(),
                "status": "open",
                "created_at": now,
                "updated_at": now,
            }
            comments.append(comment)
            return comment

        comment = store.update(add)
        return {"comment": _public_comment(comment)}

    @app.patch("/api/review/{source}/{project}/comments/{comment_id}")
    async def update_comment(source: str, project: str, comment_id: str, request: Request):
        _require_write_auth(request, token)
        payload = await _json_request(request, {"status"})
        if payload["status"] not in {"open", "resolved"}:
            raise _error(422, "invalid comment status")
        try:
            uuid.UUID(comment_id)
        except ValueError:
            raise _error(404, "comment not found")
        requested_root = _project_root(source, project, normalized_roots)
        store = ReviewStore(requested_root, storage)

        def patch(comments):
            comment = next((item for item in comments if item.get("id") == comment_id), None)
            if comment is None:
                raise _error(404, "comment not found")
            if comment.get("status") != payload["status"]:
                comment["status"] = payload["status"]
                comment["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            return comment

        return {"comment": _public_comment(store.update(patch))}

    @app.get("/api/review/{source}/{project}/export")
    def export_comments(source: str, project: str):
        review, _ = _build_review(source, project, normalized_roots, token, storage)
        filename = re.sub(r"[^A-Za-z0-9._-]+", "-", project).strip("-") or "review"
        return PlainTextResponse(
            _markdown_export(review),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}-comments.md"'},
        )

    @app.get("/api/review/{source}/{project}/asset/{asset_id}")
    def get_asset(source: str, project: str, asset_id: str, sha256: str):
        if not _valid_identifier(asset_id) or not SHA256.fullmatch(sha256):
            raise _error(404, "asset version not found")
        review, _ = _build_review(source, project, normalized_roots, token, storage)
        snapshots = [asset for asset in review["assets"] if asset["id"] == asset_id and asset["sha256"] == sha256]
        snapshots.extend(
            comment["asset"]
            for comment in review["comments"]
            if comment.get("asset", {}).get("id") == asset_id
            and comment.get("asset", {}).get("sha256") == sha256
        )
        if not snapshots:
            raise _error(404, "asset version not found")
        try:
            path = _snapshot_path(snapshots[0], normalized_roots)
        except (HTTPException, ValueError):
            raise _error(404, "asset version not found")
        digest, size = _sha256(path)
        if digest is None:
            raise _error(404, "asset version not found")
        if digest != sha256 or size != snapshots[0].get("bytes"):
            raise _error(409, "asset bytes changed")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0])

    return token
