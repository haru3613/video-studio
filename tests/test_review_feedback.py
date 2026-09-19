from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "tools" / "dashboard"
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

import review_api  # noqa: E402


COMMAND = ROOT / "scripts" / "review-feedback"


def invoke(*arguments: object) -> tuple[int, dict]:
    completed = subprocess.run(
        [str(COMMAND), *(str(argument) for argument in arguments)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.stderr == ""
    return completed.returncode, json.loads(completed.stdout)


def ui_comment_fixture(tmp_path: Path) -> tuple[Path, Path, dict, dict]:
    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "episode"
    (project / "authoring").mkdir(parents=True)
    (project / "output").mkdir()
    (project / "output" / "cover.png").write_bytes(b"cover-v1")
    (project / "authoring" / "review-package.json").write_text(
        json.dumps(
            {
                "schema": "haru.review_package.v1",
                "title": "Episode",
                "assets": [
                    {
                        "id": "cover-current",
                        "kind": "cover",
                        "label": "Cover",
                        "role": "current",
                        "path": "output/cover.png",
                    }
                ],
                "changes": [],
                "chapters": [],
            }
        ),
        encoding="utf-8",
    )
    storage = workspace / ".video-studio" / "review"
    app = FastAPI()
    csrf = review_api.register_review_routes(
        app, {"studio": workspace / "projects"}, storage_root=storage
    )
    client = TestClient(app, base_url="http://localhost")
    review = client.get("/api/review/studio/episode").json()
    asset = review["assets"][0]
    response = client.post(
        "/api/review/studio/episode/comments",
        json={
            "client_id": str(uuid.uuid4()),
            "package_id": review["package_id"],
            "asset_id": asset["id"],
            "asset_sha256": asset["sha256"],
            "timestamp_seconds": None,
            "body": "Please adjust the cover spacing.",
        },
        headers={"Origin": "http://localhost", "X-Haru-Review-CSRF": csrf},
    )
    assert response.status_code == 201, response.text
    return workspace, project, review, response.json()["comment"]


def test_reads_actual_ui_store_without_fastapi_or_private_state(tmp_path: Path) -> None:
    workspace, project, review, created = ui_comment_fixture(tmp_path)
    code, result = invoke("read", project)

    assert code == 0
    assert result["schema"] == "video_studio.review_feedback.v1"
    assert result["package_id"] == review["package_id"]
    assert result["counts"] == {"total": 1, "open": 1, "resolved": 0, "stale": 0}
    comment = result["comments"][0]
    assert comment["id"] == created["id"]
    assert comment["status"] == "open"
    assert comment["is_current"] is True
    assert comment["asset_available"] is True
    assert comment["stale"] is False
    serialized = json.dumps(result)
    assert str(workspace) not in serialized
    assert "csrf" not in serialized.lower()
    assert "session" not in serialized.lower()

    store = next((workspace / ".video-studio" / "review").glob("*/feedback.json"))
    assert json.loads(store.read_text(encoding="utf-8"))["schema"] == "haru.review_feedback.v1"


def test_resolve_and_reopen_are_bound_idempotent_review_only_changes(tmp_path: Path) -> None:
    workspace, project, review, created = ui_comment_fixture(tmp_path)
    before_files = {
        path.relative_to(project).as_posix()
        for path in project.rglob("*")
        if path.is_file()
    }
    args = (
        "resolve",
        project,
        "--comment-id",
        created["id"],
        "--status",
        "resolved",
        "--expected-package-id",
        review["package_id"],
        "--expected-asset-sha256",
        created["asset"]["sha256"],
    )
    code, resolved = invoke(*args)
    assert code == 0
    assert resolved["code"] == "review_comment_updated"
    assert resolved["comment"]["status"] == "resolved"
    assert resolved["effects"] == {
        "technical_qa_pass": False,
        "human_approval": False,
        "publishing_approval": False,
    }
    updated_at = resolved["comment"]["updated_at"]

    code, repeated = invoke(*args)
    assert code == 0
    assert repeated["code"] == "review_comment_unchanged"
    assert repeated["comment"]["updated_at"] == updated_at

    after_files = {
        path.relative_to(project).as_posix()
        for path in project.rglob("*")
        if path.is_file()
    }
    assert after_files == before_files
    assert not (project / "quality-review").exists()
    assert not (project / "publish").exists()


def test_changed_or_symlinked_media_is_stale_and_cannot_be_reopened(tmp_path: Path) -> None:
    _, project, review, created = ui_comment_fixture(tmp_path)
    resolve_args = (
        "resolve",
        project,
        "--comment-id",
        created["id"],
        "--status",
        "resolved",
        "--expected-package-id",
        review["package_id"],
        "--expected-asset-sha256",
        created["asset"]["sha256"],
    )
    assert invoke(*resolve_args)[0] == 0

    cover = project / "output" / "cover.png"
    cover.write_bytes(b"cover-v2")
    code, current = invoke("read", project)
    assert code == 0
    assert current["comments"][0]["stale"] is True
    assert current["comments"][0]["is_current"] is False
    assert current["comments"][0]["asset_available"] is False

    reopen_args = (
        "resolve",
        project,
        "--comment-id",
        created["id"],
        "--status",
        "open",
        "--expected-package-id",
        current["package_id"],
        "--expected-asset-sha256",
        created["asset"]["sha256"],
    )
    code, stale = invoke(*reopen_args)
    assert code == 3
    assert stale["code"] == "review_stale"
    assert invoke("read", project)[1]["comments"][0]["status"] == "resolved"

    outside = tmp_path / "outside.png"
    outside.write_bytes(b"cover-v1")
    cover.unlink()
    cover.symlink_to(outside)
    code, symlinked = invoke("read", project)
    assert code == 0
    assert symlinked["comments"][0]["stale"] is True
    assert str(outside) not in json.dumps(symlinked)


def test_read_does_not_create_review_state(tmp_path: Path) -> None:
    project = tmp_path / "workspace" / "projects" / "empty"
    project.mkdir(parents=True)
    state = tmp_path / "workspace" / ".video-studio" / "review"

    code, result = invoke("read", project)

    assert code == 0
    assert result["comments"] == []
    assert result["counts"] == {"total": 0, "open": 0, "resolved": 0, "stale": 0}
    assert not state.exists()


def test_conflicting_package_or_asset_digest_does_not_mutate(tmp_path: Path) -> None:
    _, project, review, created = ui_comment_fixture(tmp_path)
    base = [
        "resolve",
        project,
        "--comment-id",
        created["id"],
        "--status",
        "resolved",
    ]
    code, package_conflict = invoke(
        *base,
        "--expected-package-id",
        "0" * 64,
        "--expected-asset-sha256",
        created["asset"]["sha256"],
    )
    assert code == 3
    assert package_conflict["code"] == "review_conflict"

    code, asset_conflict = invoke(
        *base,
        "--expected-package-id",
        review["package_id"],
        "--expected-asset-sha256",
        "0" * 64,
    )
    assert code == 3
    assert asset_conflict["code"] == "review_conflict"
    assert invoke("read", project)[1]["comments"][0]["status"] == "open"


@pytest.mark.parametrize("status", ["approved", "published", "qa-pass"])
def test_status_is_limited_to_open_or_resolved(tmp_path: Path, status: str) -> None:
    _, project, review, created = ui_comment_fixture(tmp_path)
    code, result = invoke(
        "resolve",
        project,
        "--comment-id",
        created["id"],
        "--status",
        status,
        "--expected-package-id",
        review["package_id"],
        "--expected-asset-sha256",
        created["asset"]["sha256"],
    )
    assert code == 2
    assert result["code"] == "invalid_input"
