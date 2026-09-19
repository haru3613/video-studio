import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "dashboard"))

from review_store import ReviewStore  # noqa: E402
import job_interface  # noqa: E402
import portable_jobs  # noqa: E402
from workspace import initialize  # noqa: E402
from workspace_backup import (  # noqa: E402
    BackupError,
    _invalidate_jobs,
    create_backup,
    restore_backup,
)
from workspace_barrier import backup_barrier, mutation_barrier  # noqa: E402


def workspace(tmp_path):
    root = tmp_path / "workspace"
    initialize(root)
    project = root / "projects" / "demo"
    (project / "output").mkdir(parents=True)
    (project / "sources.md").write_text("# source\n", encoding="utf-8")
    (project / "output" / "final.mp4").write_bytes(b"media-bytes")
    return root, project


def job_database(project, status="succeeded"):
    state = project / ".hvp"
    state.mkdir(exist_ok=True)
    database = state / "jobs.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE jobs(
          job_id TEXT PRIMARY KEY,kind TEXT,project TEXT,tools_root TEXT,worker TEXT,
          status TEXT,epoch INTEGER,revision TEXT,snapshot_digest TEXT,
          snapshot_root TEXT,candidate_root TEXT,pid INTEGER,pid_token TEXT,
          process_group INTEGER,exit_code INTEGER,error_code TEXT,
          created_at TEXT,updated_at TEXT
        )"""
    )
    connection.execute(
        """INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "a" * 32,
            "render",
            str(project.resolve()),
            "/installed/tools",
            "/installed/worker",
            status,
            4,
            "b" * 64,
            "c" * 64,
            str(project / "output/.staging/snapshot"),
            str(project / "output/.staging/candidate"),
            4242,
            "old-process-token",
            4242,
            0 if status == "succeeded" else None,
            None,
            "2026-09-20T00:00:00+00:00",
            "2026-09-20T00:01:00+00:00",
        ),
    )
    connection.commit()
    connection.close()
    return database


def add_review(root, project):
    store = ReviewStore(project, root / ".video-studio" / "review")

    def mutate(comments):
        comments.append(
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "package_id": "b" * 64,
                "asset": {
                    "id": "video-current",
                    "kind": "video",
                    "label": "Video",
                    "source": "studio",
                    "project": "demo",
                    "path": "output/final.mp4",
                    "sha256": hashlib.sha256(b"media-bytes").hexdigest(),
                    "bytes": len(b"media-bytes"),
                },
                "body": "Review note",
                "status": "open",
            }
        )

    store.update(mutate)
    return store


def test_backup_restore_roundtrip_rekeys_review_and_invalidates_authority(tmp_path):
    root, project = workspace(tmp_path)
    job_database(project)
    add_review(root, project)
    (project / ".hvp" / "lease.json").write_text('{"secret":"lease"}')
    (project / ".hvp" / "provider-jobs").mkdir()
    (project / ".hvp" / "provider-jobs" / "provider.json").write_text(
        '{"token":"provider-secret"}'
    )
    (project / ".hvp" / "review-requests").mkdir()
    (project / ".hvp" / "review-requests/request.json").write_text("private body")
    intake = project / ".hvp/staging/intake" / ("c" * 32)
    intake.mkdir(parents=True)
    (intake / "blob").write_text("staged source")
    (project / "secrets").mkdir()
    (project / "secrets" / "credential.json").write_text("secret")
    (project / ".env.local").write_text("API_KEY=secret")
    (project / "remotion" / "node_modules").mkdir(parents=True)
    (project / "remotion" / "node_modules" / "cache.js").write_text("cache")
    (project / "publish").mkdir()
    (project / "publish" / "publish-approval.json").write_text('{"approved":true}')

    backups = tmp_path / "backups"
    backups.mkdir()
    result = create_backup(root, backups)
    backup = Path(result["path"])
    manifest = json.loads((backup / "manifest.json").read_text())
    paths = {item["path"] for item in manifest["files"]}
    assert "projects/demo/sources.md" in paths
    assert "projects/demo/output/final.mp4" in paths
    assert "projects/demo/.hvp/jobs.sqlite3" in paths
    assert f"projects/demo/.hvp/staging/intake/{'c' * 32}/blob" in paths
    assert any(path.startswith(".video-studio/review/") for path in paths)
    joined = "\n".join(paths)
    assert "lease.json" not in joined
    assert "provider-jobs" not in joined
    assert "review-requests" not in joined
    assert "node_modules" not in joined
    assert "credential.json" not in joined
    assert ".env" not in joined
    assert "publish-approval.json" not in joined
    backed_jobs = sqlite3.connect(backup / "data/projects/demo/.hvp/jobs.sqlite3")
    assert backed_jobs.execute("SELECT job_id,status FROM jobs").fetchone() == (
        "a" * 32,
        "succeeded",
    )
    backed_jobs.close()

    restored = tmp_path / "restored"
    response = restore_backup(backup, restored)
    assert response["invalidated"]["active_leases"] is True
    assert response["invalidated"]["jobs_invalidated"] == 0
    assert (restored / "projects/demo/sources.md").read_text() == "# source\n"
    assert (restored / "projects/demo/output/final.mp4").read_bytes() == b"media-bytes"
    assert (restored / "inbox").is_dir()
    assert (restored / "exports").is_dir()
    restored_jobs = restored / "projects/demo/.hvp/jobs.sqlite3"
    assert restored_jobs.is_file()
    status_response, status_code = job_interface.status(
        restored / "projects/demo", "a" * 32
    )
    assert status_code == 0
    assert status_response["data"]["status"] == "succeeded"
    assert status_response["data"]["epoch"] == 4
    connection = sqlite3.connect(restored_jobs)
    restored_row = connection.execute(
        """SELECT project,status,epoch,snapshot_digest,snapshot_root,candidate_root,
                  pid,pid_token,process_group FROM jobs"""
    ).fetchone()
    connection.close()
    assert restored_row == (
        str((restored / "projects/demo").resolve()),
        "succeeded",
        4,
        None,
        None,
        None,
        None,
        None,
        None,
    )
    assert not (restored / "projects/demo/.hvp/lease.json").exists()
    assert not (restored / "projects/demo/publish/publish-approval.json").exists()
    comments = ReviewStore(
        restored / "projects/demo", restored / ".video-studio" / "review"
    ).read_comments()
    assert [comment["body"] for comment in comments] == ["Review note"]
    report = json.loads((restored / ".video-studio/restore-report.json").read_text())
    assert report["path_changed"] is True
    assert report["invalidated"]["path_bound_signatures"] is True
    assert any("signing keys" in item for item in response["limitations"])


def test_active_job_refuses_backup_using_real_sqlite_record(tmp_path):
    root, project = workspace(tmp_path)
    job_database(project, "running")
    backups = tmp_path / "backups"
    backups.mkdir()
    with pytest.raises(BackupError) as raised:
        create_backup(root, backups)
    assert raised.value.code == "active_jobs"
    assert list(backups.iterdir()) == []


def test_restore_interrupts_active_job_without_erasing_history_or_resurrecting_worker(
    tmp_path,
):
    root, _project = workspace(tmp_path)
    project = root / "projects/demo"
    database = job_database(project, "running")
    destination = tmp_path / "restored"

    assert _invalidate_jobs(root, destination) == 1
    root.rename(destination)
    restored_project = destination / "projects/demo"
    response, code = job_interface.status(restored_project, "a" * 32)
    assert code == 0
    assert response["data"]["status"] == "interrupted"
    assert response["data"]["epoch"] == 5
    assert response["data"]["error_code"] == "workspace_restored"
    assert response["data"]["can_resume"] is True

    connection = sqlite3.connect(destination / database.relative_to(root))
    row = connection.execute(
        """SELECT project,status,epoch,revision,snapshot_digest,snapshot_root,
                  candidate_root,pid,pid_token,process_group,exit_code,error_code
           FROM jobs WHERE job_id=?""",
        ("a" * 32,),
    ).fetchone()
    connection.close()
    assert row == (
        str(restored_project),
        "interrupted",
        5,
        "b" * 64,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        "workspace_restored",
    )
    assert portable_jobs.promote_candidate(restored_project, "a" * 32, 4) is False
    assert not list((restored_project / ".hvp").glob("jobs.invalidated-*.sqlite3"))


def test_backup_waits_for_concurrent_mutation_and_captures_completed_bytes(tmp_path):
    root, project = workspace(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    started = threading.Event()
    finished = threading.Event()
    outcome = {}

    def backup():
        started.set()
        outcome["result"] = create_backup(root, backups)
        finished.set()

    with mutation_barrier(project):
        thread = threading.Thread(target=backup)
        thread.start()
        assert started.wait(1)
        time.sleep(0.1)
        assert not finished.is_set()
        (project / "sources.md").write_text("# committed mutation\n", encoding="utf-8")
    thread.join(5)
    assert not thread.is_alive()
    backup_root = Path(outcome["result"]["path"])
    assert (
        backup_root / "data/projects/demo/sources.md"
    ).read_text() == "# committed mutation\n"


def test_ui_review_write_waits_for_backup_barrier(tmp_path):
    root, project = workspace(tmp_path)
    store = ReviewStore(project, root / ".video-studio" / "review")
    started = threading.Event()
    finished = threading.Event()

    def write_review():
        started.set()
        store.update(
            lambda comments: comments.append(
                {"id": "00000000-0000-4000-8000-000000000002"}
            )
        )
        finished.set()

    with backup_barrier(root):
        thread = threading.Thread(target=write_review)
        thread.start()
        assert started.wait(1)
        time.sleep(0.1)
        assert not finished.is_set()
    thread.join(5)
    assert finished.is_set()
    assert store.read_comments()[0]["id"].endswith("0002")


def test_corruption_traversal_symlink_and_existing_destination_are_refused(tmp_path):
    root, project = workspace(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    backup = Path(create_backup(root, backups)["path"])
    valid_backups = tmp_path / "valid-backups"
    valid_backups.mkdir()
    valid_backup = Path(create_backup(root, valid_backups)["path"])

    media = backup / "data/projects/demo/output/final.mp4"
    media.write_bytes(b"corrupt")
    with pytest.raises(BackupError) as corrupt:
        restore_backup(backup, tmp_path / "corrupt-restore")
    assert corrupt.value.code == "backup_file_invalid"

    # Create another valid immutable backup before testing manifest traversal.
    media.write_bytes(b"media-bytes")
    manifest = json.loads((backup / "manifest.json").read_text())
    manifest["files"][0]["path"] = "../escape"
    (backup / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BackupError) as traversal:
        restore_backup(backup, tmp_path / "traversal-restore")
    assert traversal.value.code == "backup_manifest_invalid"

    root2, project2 = workspace(tmp_path / "other")
    outside = tmp_path / "outside"
    outside.write_text("outside")
    (project2 / "linked").symlink_to(outside)
    other_backups = tmp_path / "other-backups"
    other_backups.mkdir()
    with pytest.raises(BackupError) as symlink:
        create_backup(root2, other_backups)
    assert symlink.value.code == "workspace_symlink_refused"

    destination = tmp_path / "occupied"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep")
    with pytest.raises(BackupError) as occupied:
        restore_backup(valid_backup, destination)
    assert occupied.value.code == "restore_destination_not_empty"
    assert (destination / "keep.txt").read_text() == "keep"


def test_cli_create_and_restore_are_local_json_operations(tmp_path):
    root, _project = workspace(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    created = subprocess.run(
        [ROOT / "scripts/workspace-backup", "create", root, backups],
        check=False,
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0, created.stderr
    created_json = json.loads(created.stdout)
    assert created_json["code"] == "workspace_backup_complete"
    restored = tmp_path / "cli-restored"
    response = subprocess.run(
        [
            ROOT / "scripts/workspace-backup",
            "restore",
            created_json["data"]["path"],
            restored,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert response.returncode == 0, response.stderr
    value = json.loads(response.stdout)
    assert value["code"] == "workspace_restore_complete"
    assert value["data"]["workspace"] == str(restored.resolve())
