#!/usr/bin/env python3
"""Localhost dashboard and version-bound creative feedback for video projects.

GET /                              single-page UI
GET /api/projects                  project cards for both roots
GET /api/projects/{source}/{name}  pipeline + file inventory detail
GET /media/{source}/{rel_path}     raw file (Range streaming via starlette)

Review comments have scoped local write endpoints; production approvals remain outside this dashboard. The server binds 127.0.0.1.
Spec: docs/superpowers/specs/2026-07-06-studio-dashboard-design.md
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_status
import local_delivery

from review_api import register_review_routes
from overview_catalog import build_overview
from session import SessionAuthority, install_sessions

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
DOC_EXTS = {".md", ".txt", ".json", ".srt"}
EXCLUDED_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".claude"}
MAX_DEPTH = 6
BUCKET_CAP = 500


def bucket_for(name):
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXTS:
        return "videos"
    if ext in IMAGE_EXTS:
        return "images"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in DOC_EXTS:
        return "docs"
    return None


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def scan_files(project_dir):
    """Walk project_dir; return ({bucket: [{path,size,mtime}]}, truncated).

    Live scan on every request — stat only, never reads file contents.
    # ponytail: full rescan per request; add a small TTL cache if the media
    # root ever makes /api/projects feel slow.
    """
    buckets = {"videos": [], "images": [], "audio": [], "docs": []}
    truncated = False

    def walk(d, depth):
        nonlocal truncated
        if depth > MAX_DEPTH:
            return
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            try:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in EXCLUDED_DIRS:
                        continue
                    walk(entry.path, depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    bucket = bucket_for(entry.name)
                    if bucket is None:
                        continue
                    if len(buckets[bucket]) >= BUCKET_CAP:
                        truncated = True
                        continue
                    stat = entry.stat()
                    buckets[bucket].append(
                        {
                            "path": Path(entry.path).relative_to(project_dir).as_posix(),
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                        }
                    )
            except OSError:
                continue

    walk(project_dir, 0)
    return buckets, truncated


def read_lease(project_dir):
    """Active-run signal. `.hvp/lease.lock` is a stale flock mutex and lies;
    `.hvp/lease.json` carries the unix `expires_at` that actually expires."""
    lease = read_json(project_dir / ".hvp" / "lease.json")
    if not isinstance(lease, dict):
        return None
    expires = lease.get("expires_at")
    if not isinstance(expires, (int, float)):
        return None
    return {
        "owner": lease.get("owner"),
        "expires_at": expires,
        "active": expires > dt.datetime.now(dt.timezone.utc).timestamp(),
    }


def upload_info(project_dir, status):
    """uploaded / awaiting_approval / not_ready, plus the YouTube receipt."""
    approval = read_json(project_dir / "publish" / "publish-approval.json")
    approval = approval if isinstance(approval, dict) else {}
    video_id = approval.get("video_id")
    if video_id:
        state = "uploaded"
    elif (status or {}).get("overall_status") == "ready_for_human_upload_approval":
        state = "awaiting_approval"
    else:
        state = "not_ready"
    return {
        "state": state,
        "video_id": video_id,
        "visibility": approval.get("visibility"),
        "uploaded_at": approval.get("uploaded_at"),
        "url": f"https://youtu.be/{video_id}" if video_id else None,
    }


def stage_progress(status):
    """done/total over the canonical gates, plus the first gate not yet passing."""
    stages = (status or {}).get("stages")
    if not isinstance(stages, dict) or not stages:
        return None
    # Count required_stages, not every stage: `upload` is permanently
    # "requires_harvey" by design, so counting it pins finished projects at
    # 16/17 forever and points at a blocker that will never clear.
    required = (status or {}).get("required_stages")
    names = [n for n in required if n in stages] if isinstance(required, list) else list(stages)
    if not names:
        return None
    done, current = 0, None
    for name in names:
        st = stages.get(name)
        if isinstance(st, dict) and st.get("status") == "pass":
            done += 1
        elif current is None:
            current = name
    return {"done": done, "total": len(names), "current": current}


def default_roots():
    workspace = Path(os.environ.get("VIDEO_STUDIO_WORKSPACE", str(Path.home() / "VideoStudio"))).expanduser()
    roots = {"studio": workspace / "projects"}
    archive = os.environ.get("VIDEO_STUDIO_ARCHIVE_ROOT")
    if archive:
        roots["media"] = Path(archive).expanduser()
    return roots


def current_pipeline(project):
    """Canonical projects use the evaluator, never their saved status mirror."""
    contract = project / "project-contract.json"
    if contract.exists() or contract.is_symlink():
        try:
            return agent_status.build(project.resolve(), project.parent.parent.resolve())
        except (OSError, ValueError, TypeError, KeyError):
            return {"overall_status": "in_progress", "blockers": ["project_state_unreadable"]}, None
    reported = read_json(project / "pipeline_status.json")
    if not isinstance(reported, dict):
        return None, None
    # Preserve useful old notes, but a file without a canonical project cannot
    # attest readiness or publication. Actual receipts are checked by the core.
    if reported.get("overall_status") in {"ready", "ready_for_human_upload_approval", "publish_approved"}:
        reported = {**reported, "overall_status": "unverified", "stages": {}}
    return reported, None


def _project_card(source, project_dir):
    status = current_pipeline(project_dir)[0] if source == "studio" else None
    if not isinstance(status, dict):
        status = None
    buckets, _ = scan_files(project_dir)
    thumbnail = None
    if buckets["images"]:
        thumbnail = "/media/" + quote(
            f"{source}/{project_dir.name}/{buckets['images'][0]['path']}", safe="/"
        )
    updated_at = status.get("generated_at") if status else None
    if not isinstance(updated_at, str):
        updated_at = None
    if not updated_at:
        # ponytail: dir mtime misses deep-file changes; good enough to sort legacy projects
        try:
            mtime = project_dir.stat().st_mtime
            updated_at = dt.datetime.fromtimestamp(mtime, dt.timezone.utc).replace(microsecond=0).isoformat()
        except OSError:
            updated_at = "1970-01-01T00:00:00+00:00"
    return {
        "name": project_dir.name,
        "source": source,
        "overall_status": status.get("overall_status") if status else None,
        "updated_at": updated_at,
        "thumbnail": thumbnail,
        "counts": {k: len(v) for k, v in buckets.items()},
        "lease": read_lease(project_dir) if source == "studio" else None,
        "upload": upload_info(project_dir, status) if source == "studio" else None,
        "progress": stage_progress(status),
    }


def list_projects(roots):
    projects, warnings = [], []
    for source, root in roots.items():
        if not root.is_dir():
            warnings.append(f"{source} root not found: {root} (check VIDEO_STUDIO_WORKSPACE / VIDEO_STUDIO_ARCHIVE_ROOT)")
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and not child.name.startswith((".", "_")):
                projects.append(_project_card(source, child))
    projects.sort(key=lambda p: p["updated_at"], reverse=True)
    return {"projects": projects, "warnings": warnings}


def project_detail(source, name, roots):
    root = roots.get(source)
    if root is None or "/" in name or name.startswith("."):
        return None
    project_dir = root / name
    if not project_dir.is_dir() or project_dir.parent != root:
        return None
    buckets, truncated = scan_files(project_dir)
    pipeline, artifacts = current_pipeline(project_dir)
    return {
        "name": name,
        "source": source,
        "pipeline": pipeline,
        "artifact_manifest": artifacts,
        "files": buckets,
        "truncated": truncated,
    }


def resolve_media_path(source, rel_path, roots):
    root = roots.get(source)
    if root is None:
        return None
    try:
        candidate = (root / rel_path).resolve()
        root_resolved = root.resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(root_resolved):
        return None
    # scan_files hides dotfiles so the UI never links these, but /media used to
    # serve them anyway -- including .hvp/lease.json, which carries the lease
    # capability token. Judge the resolved path, so a symlink pointing into a
    # dot-directory is caught too. Only the part below the root is inspected,
    # so a dot in the root's own path is irrelevant.
    if any(part.startswith(".") for part in candidate.relative_to(root_resolved).parts):
        return None
    # The UI only ever links files scan_files bucketed; keep the route to the
    # same set so it can't hand out an unrelated file that lands in a project.
    if bucket_for(candidate.name) is None:
        return None
    if not candidate.is_file():
        return None
    return candidate


def create_app(roots=None, review_storage_root=None, overview_catalog_path=None, *, session_authority=None, require_session=True):
    roots = roots or default_roots()
    app = FastAPI(title="Video Studio")
    if require_session:
        authority = session_authority or SessionAuthority()
        install_sessions(app, authority)
    # Binding 127.0.0.1 does not stop DNS rebinding: a hostile page can point a
    # name at 127.0.0.1 and become same-origin with us. Now that this runs 24/7
    # under launchd, pin the Host header.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "index.html")

    @app.get("/api/projects")
    def projects():
        return list_projects(roots)

    @app.get("/api/overview")
    def overview():
        inventory = list_projects(roots)
        result = build_overview(inventory["projects"], roots, overview_catalog_path)
        result["warnings"] = inventory["warnings"] + result["warnings"]
        return result

    @app.get("/overview/{name}")
    def overview_static(name: str):
        if name not in {"overview.js", "overview.css"}:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(Path(__file__).parent / name)

    @app.get("/api/projects/{source}/{name}")
    def detail(source: str, name: str):
        result = project_detail(source, name, roots)
        if result is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return result

    @app.get("/api/delivery/{source}/{name}")
    def delivery_status(source: str, name: str):
        root = roots.get(source)
        if root is None or not name or "/" in name or name.startswith("."):
            return JSONResponse({"error": "not found"}, status_code=404)
        project = root / name
        if project.is_symlink() or not project.is_dir():
            return JSONResponse({"error": "not found"}, status_code=404)
        try:
            return local_delivery.technical_status(project.resolve(strict=True))
        except (OSError, ValueError, local_delivery.DeliveryError):
            return JSONResponse({"error": "technical_delivery_unavailable"}, status_code=422)

    @app.get("/media/{source}/{rel_path:path}")
    def media(source: str, rel_path: str):
        path = resolve_media_path(source, rel_path, roots)
        if path is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path)

    @app.get("/review/{name}")
    def review_static(name: str):
        if name not in {"review.js", "review.css"}:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(Path(__file__).parent / name)

    register_review_routes(app, roots, storage_root=review_storage_root)
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Local Video Studio review UI")
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get("VIDEO_STUDIO_WORKSPACE", str(Path.home() / "VideoStudio"))))
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    workspace = args.workspace.expanduser().resolve(strict=True)
    projects = workspace / "projects"
    if not projects.is_dir() or projects.is_symlink():
        parser.error("workspace requires a direct projects directory")
    state = workspace / ".video-studio"
    if state.is_symlink():
        parser.error("workspace state must not be a symlink")
    state.mkdir(mode=0o700, exist_ok=True)
    authority = SessionAuthority()
    code_file = state / "ui-code"
    fd = os.open(code_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(authority.code + "\n")
    print(f"Video Studio: http://127.0.0.1:{args.port}/login; one-time code file: {code_file}")
    uvicorn.run(create_app({"studio": projects}, review_storage_root=state / "review", session_authority=authority), host="127.0.0.1", port=args.port)
