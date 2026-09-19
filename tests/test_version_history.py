import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import portable_jobs
import version_archive
from dashboard import server
from dashboard.review_store import ReviewStore


def test_old_comment_plays_exact_archived_bytes_after_new_render(tmp_path):
    project = tmp_path / "projects/demo"
    output = project / "output"
    output.mkdir(parents=True)
    video = output / "final.mp4"
    old = b"old version bytes"
    video.write_bytes(old)
    checksum = hashlib.sha256(old).hexdigest()
    storage = tmp_path / ".video-studio/review"
    comment = {
        "id": "00000000-0000-4000-8000-000000000001",
        "client_id": "00000000-0000-4000-8000-000000000002",
        "package_id": "a" * 64,
        "asset": {"id": "video-current", "kind": "video", "label": "Video", "source": "studio", "project": "demo", "path": "output/final.mp4", "sha256": checksum, "bytes": len(old), "url": f"/api/review/studio/demo/asset/video-current?sha256={checksum}"},
        "timestamp_seconds": 0, "body": "Old version feedback", "status": "open",
        "created_at": "2026-09-20T00:00:00Z", "updated_at": "2026-09-20T00:00:00Z",
    }
    ReviewStore(project, storage).update(lambda comments: comments.append(comment))
    previous_revision = portable_jobs.project_revision(project)
    portable_jobs._preserve_previous(video, "b" * 32)
    # Promotion replaces the inode; it never edits an archived hard link in place.
    replacement = output / "replacement.mp4"
    replacement.write_bytes(b"new version bytes")
    replacement.replace(video)
    app = server.create_app({"studio": project.parent}, review_storage_root=storage, require_session=False)
    with TestClient(app, base_url="http://localhost") as client:
        review = client.get("/api/review/studio/demo").json()
        assert review["comments"][0]["is_current"] is False
        assert review["comments"][0]["asset_available"] is True
        response = client.get(comment["asset"]["url"])
        assert response.status_code == 200
        assert response.content == old
    assert portable_jobs.project_revision(project) == previous_revision


def test_archive_refuses_redirected_version_directory(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    video = output / "final.mp4"
    video.write_bytes(b"media")
    outside = tmp_path / "outside"
    outside.mkdir()
    (output / "versions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        version_archive.preserve_video(video)
    assert list(outside.iterdir()) == []
