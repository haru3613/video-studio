import json
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

import artifact_intake
import workspace_barrier
from workspace import initialize


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def studio(tmp_path):
    workspace = tmp_path / "workspace"
    initialize(workspace)
    project = workspace / "projects/demo"
    project.mkdir()
    return workspace, project


def test_inline_text_stages_immutable_blob_and_imports_only_to_fixed_namespace(studio):
    workspace, project = studio
    (project / "narration-final.mp3").write_bytes(b"controlled narration")
    (project / "output").mkdir()
    (project / "output/final.mp4").write_bytes(b"controlled final")

    staged = artifact_intake.stage_text(
        workspace, project, "script_notes", "A local production note"
    )
    assert staged == artifact_intake.stage_text(
        workspace, project, "script_notes", "A local production note"
    )
    blob = project / staged["blob"]
    assert blob.read_text() == "A local production note"
    assert blob.stat().st_mode & 0o222 == 0
    assert artifact_intake.resolve_stage(workspace, project, staged["stage_id"]) == staged

    imported = artifact_intake.import_stage(workspace, project, staged["stage_id"])
    assert imported["path"].startswith("imports/script_notes/")
    assert (project / imported["path"]).read_bytes() == blob.read_bytes()
    assert artifact_intake.import_stage(workspace, project, staged["stage_id"]) == imported
    assert (project / "narration-final.mp3").read_bytes() == b"controlled narration"
    assert (project / "output/final.mp4").read_bytes() == b"controlled final"
    assert not (project / "publish").exists()
    assert not (project / "quality-review").exists()


def test_real_inbox_file_is_copied_and_hash_bound(studio):
    workspace, project = studio
    source = workspace / "inbox/reference.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"bounded-image-data"
    source.write_bytes(payload)
    staged = artifact_intake.stage_inbox(
        workspace, project, "reference_image", "reference.png"
    )
    source.write_bytes(b"changed after intake")
    assert (project / staged["blob"]).read_bytes() == payload
    assert staged["sha256"] == __import__("hashlib").sha256(payload).hexdigest()


def test_stage_is_privately_bound_to_workspace_project_and_owner(studio):
    workspace, project = studio
    staged = artifact_intake.stage_text(
        workspace, project, "metadata", '{"bound":true}', "principal-a"
    )
    assert "owner" not in staged
    manifest = json.loads(
        (project / staged["blob"]).with_name("manifest.json").read_text()
    )
    assert manifest["schema"] == artifact_intake.STORED_STAGE_SCHEMA
    assert manifest["workspace_id"] == json.loads(
        (workspace / "workspace.json").read_text()
    )["workspace_id"]
    assert manifest["project_scope"] == "demo"
    assert manifest["owner"] == "principal-a"
    assert manifest["expires_at"] - manifest["created_at"] == 24 * 60 * 60

    with pytest.raises(artifact_intake.IntakeError, match="stage_owner_mismatch"):
        artifact_intake.resolve_stage(
            workspace, project, staged["stage_id"], "principal-b"
        )
    with pytest.raises(artifact_intake.IntakeError, match="stage_owner_mismatch"):
        artifact_intake.import_stage(
            workspace, project, staged["stage_id"], "principal-b"
        )
    assert artifact_intake.resolve_stage(
        workspace, project, staged["stage_id"], "principal-a"
    ) == staged


def test_expired_stage_is_refused_and_cleaned_during_next_stage(studio):
    workspace, project = studio
    staged = artifact_intake.stage_text(
        workspace, project, "metadata", '{"old":true}', "principal-a"
    )
    directory = (project / staged["blob"]).parent
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["created_at"] = 0
    manifest["expires_at"] = artifact_intake.STAGE_TTL_SECONDS
    os.chmod(directory, 0o755)
    os.chmod(manifest_path, 0o644)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(artifact_intake.IntakeError, match="stage_expired"):
        artifact_intake.resolve_stage(
            workspace, project, staged["stage_id"], "principal-a"
        )

    artifact_intake.stage_text(
        workspace, project, "metadata", '{"new":true}', "principal-a"
    )
    assert not directory.exists()


def test_project_stage_quota_is_enforced_after_expiry_cleanup(studio, monkeypatch):
    workspace, project = studio
    monkeypatch.setattr(artifact_intake, "PROJECT_STAGE_QUOTA", 7)
    artifact_intake.stage_text(
        workspace, project, "metadata", '{"a":1}', "principal-a"
    )
    with pytest.raises(artifact_intake.IntakeError, match="stage_quota_exceeded"):
        artifact_intake.stage_text(
            workspace, project, "metadata", '{"b":2}', "principal-a"
        )


def test_legacy_unbound_stage_is_rejected(studio):
    workspace, project = studio
    staged = artifact_intake.stage_text(workspace, project, "metadata", '{"ok":true}')
    directory = (project / staged["blob"]).parent
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for key in (
        "workspace_id",
        "project_scope",
        "owner",
        "created_at",
        "expires_at",
    ):
        manifest.pop(key)
    manifest["schema"] = artifact_intake.STAGE_SCHEMA
    os.chmod(directory, 0o755)
    os.chmod(manifest_path, 0o644)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(artifact_intake.IntakeError, match="stage_invalid"):
        artifact_intake.resolve_stage(workspace, project, staged["stage_id"])


@pytest.mark.parametrize("value", ["/etc/passwd", "../outside.png", "nested/../../outside.png"])
def test_absolute_and_traversal_inbox_paths_are_rejected_without_state(studio, value):
    workspace, project = studio
    with pytest.raises(artifact_intake.IntakeError, match="invalid_inbox_path"):
        artifact_intake.stage_inbox(workspace, project, "reference_image", value)
    assert not (project / ".hvp").exists()


def test_symlink_and_forged_media_are_rejected(studio):
    workspace, project = studio
    outside = workspace / "outside.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\nexternal")
    (workspace / "inbox/link.png").symlink_to(outside)
    with pytest.raises(artifact_intake.IntakeError, match="invalid_inbox_path"):
        artifact_intake.stage_inbox(
            workspace, project, "reference_image", "link.png"
        )

    forged = workspace / "inbox/forged.mp4"
    forged.write_text("export default () => arbitraryCode()")
    with pytest.raises(artifact_intake.IntakeError, match="artifact_type_invalid"):
        artifact_intake.stage_inbox(workspace, project, "source_video", "forged.mp4")


def test_import_refuses_redirected_namespace_and_forged_stage_manifest(studio):
    workspace, project = studio
    staged = artifact_intake.stage_text(workspace, project, "metadata", '{"ok":true}')
    outside = workspace / "outside-imports"
    outside.mkdir()
    (project / "imports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(artifact_intake.IntakeError, match="import_conflict"):
        artifact_intake.import_stage(workspace, project, staged["stage_id"])
    assert list(outside.iterdir()) == []
    (project / "imports").unlink()

    stage_root = project / ".hvp/staging/intake" / staged["stage_id"]
    manifest_path = stage_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["blob"] = "../../narration-final.mp3"
    os.chmod(stage_root, 0o755)
    os.chmod(manifest_path, 0o644)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(artifact_intake.IntakeError, match="stage_invalid"):
        artifact_intake.resolve_stage(workspace, project, staged["stage_id"])


def test_inline_size_role_and_import_conflicts_fail_closed(studio):
    workspace, project = studio
    with pytest.raises(artifact_intake.IntakeError, match="artifact_too_large"):
        artifact_intake.stage_text(
            workspace, project, "script_notes", "x" * (artifact_intake.INLINE_LIMIT + 1)
        )
    with pytest.raises(artifact_intake.IntakeError, match="inline_role_invalid"):
        artifact_intake.stage_text(workspace, project, "source_video", "not video")

    staged = artifact_intake.stage_text(workspace, project, "metadata", '{"ok":true}')
    imported = artifact_intake.import_stage(workspace, project, staged["stage_id"])
    target = project / imported["path"]
    os.chmod(target, 0o644)
    target.write_text("tampered")
    with pytest.raises(artifact_intake.IntakeError, match="import_conflict"):
        artifact_intake.import_stage(workspace, project, staged["stage_id"])


def test_fixed_wrapper_emits_typed_json(studio):
    workspace, project = studio
    result = subprocess.run(
        [
            ROOT / "scripts/artifact-intake",
            "stage-text",
            workspace,
            project,
            "metadata",
            '{"title":"demo"}',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result
    response = json.loads(result.stdout)
    assert response["code"] == "artifact_staged"
    assert response["data"]["role"] == "metadata"


def test_resolve_missing_stage_is_read_only(studio):
    workspace, project = studio
    with pytest.raises(artifact_intake.IntakeError, match="stage_not_found"):
        artifact_intake.resolve_stage(workspace, project, "0" * 32)
    assert not (project / ".hvp").exists()


def test_workspace_catalog_resolves_only_public_project_ids(studio):
    workspace, project = studio
    assert artifact_intake.resolve_project(workspace, "demo") == project.resolve()
    assert artifact_intake.workspace_info(workspace)["projects"] == [
        {"project_id": "demo", "contract_present": False}
    ]
    for forged in ("../demo", "/tmp/demo", "Demo", "demo/child"):
        with pytest.raises(artifact_intake.IntakeError):
            artifact_intake.resolve_project(workspace, forged)


@pytest.mark.parametrize("malformed", ["[]", "null", '{"schema":"video_studio.workspace.v1"}'])
def test_malformed_workspace_shape_fails_with_typed_error(studio, malformed):
    workspace, _project = studio
    (workspace / "workspace.json").write_text(malformed)
    with pytest.raises(artifact_intake.IntakeError, match="invalid_workspace"):
        artifact_intake.workspace_info(workspace)


def test_inline_request_file_supports_full_bound_without_argv(studio):
    workspace, project = studio
    text = "x" * artifact_intake.INLINE_LIMIT
    request_id = hashlib.sha256(
        b"video_studio.inline_intake.v1\0script_notes\0" + text.encode()
    ).hexdigest()[:32]
    request_root = project / ".hvp/intake-requests"
    request_root.mkdir(parents=True)
    request = request_root / f"{request_id}.txt"
    request.write_text(text)
    os.chmod(request, 0o400)

    staged = artifact_intake.stage_inline_request(
        workspace, project, "script_notes", request_id
    )
    assert staged["bytes"] == artifact_intake.INLINE_LIMIT
    assert not request.exists()
    assert (project / staged["blob"]).stat().st_size == artifact_intake.INLINE_LIMIT


def test_direct_intake_waits_for_workspace_backup_barrier(studio):
    workspace, project = studio
    command = [
        ROOT / "scripts/artifact-intake",
        "stage-text",
        workspace,
        project,
        "metadata",
        '{"during":"backup"}',
    ]
    with workspace_barrier.backup_barrier(workspace):
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        __import__("time").sleep(0.15)
        assert process.poll() is None
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert json.loads(stdout)["code"] == "artifact_staged"
