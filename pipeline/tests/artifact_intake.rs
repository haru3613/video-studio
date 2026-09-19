use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use pipeline::ProjectStore;
use pipeline::application::{
    self, ArtifactImportRequest, ArtifactStageRequest, LeaseInput, ProcessExecutor,
    ProduceStagedArtifactRequest,
};
use serde_json::json;
use tempfile::tempdir;

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf()
}

fn fixture() -> (tempfile::TempDir, PathBuf, PathBuf, LeaseInput, String) {
    let directory = tempdir().unwrap();
    let workspace = directory.path().join("workspace");
    let project = workspace.join("projects/demo");
    fs::create_dir_all(workspace.join(".video-studio")).unwrap();
    fs::create_dir_all(workspace.join("inbox")).unwrap();
    fs::create_dir_all(workspace.join("exports")).unwrap();
    fs::create_dir_all(&project).unwrap();
    fs::write(
        workspace.join("workspace.json"),
        serde_json::to_vec(&json!({
            "schema": "video_studio.workspace.v1",
            "workspace_id": "00000000-0000-4000-8000-000000000001",
        }))
        .unwrap(),
    )
    .unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("agent", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let capability = lease.token.clone();
    let lease = LeaseInput::from_lease(&lease);
    (directory, workspace, project, lease, capability)
}

#[test]
fn workspace_reads_expose_ids_without_absolute_project_paths() {
    let (_directory, workspace, project, _lease, _capability) = fixture();
    fs::write(project.join("project-contract.json"), "{}").unwrap();
    fs::create_dir(workspace.join("projects/not initialized")).unwrap();
    assert!(
        !workspace
            .join(".video-studio/workspace-barrier.lock")
            .exists()
    );

    let info = application::workspace_info(&workspace);
    assert_eq!(info.code, "workspace_info");
    assert_eq!(info.data.as_ref().unwrap()["project_count"], 1);
    assert!(
        !serde_json::to_string(info.data.as_ref().unwrap())
            .unwrap()
            .contains(workspace.to_str().unwrap())
    );

    let listed = application::project_list(&workspace);
    assert_eq!(listed.code, "project_list");
    assert_eq!(
        listed.data.as_ref().unwrap()["projects"][0]["project_id"],
        "demo"
    );
    assert_eq!(
        listed.data.as_ref().unwrap()["projects"][0]["initialized"],
        true
    );
    assert!(
        !workspace
            .join(".video-studio/workspace-barrier.lock")
            .exists()
    );
}

#[test]
fn staged_inbox_can_import_and_produce_only_allowlisted_agent_artifacts() {
    let (_directory, workspace, project, lease, _capability) = fixture();
    fs::write(workspace.join("inbox/script.md"), "# Reviewed script\n").unwrap();
    let repo = repo_root();
    let mut executor = ProcessExecutor;
    let staged = application::artifact_stage(
        &ArtifactStageRequest {
            project_root: project.clone(),
            lease: lease.clone(),
            role: "script_notes".to_owned(),
            inbox_path: Some("script.md".to_owned()),
            inline_text: None,
        },
        &repo,
        &mut executor,
    );
    assert_eq!(staged.code, "artifact_staged");
    let stage_id = staged.data.as_ref().unwrap()["stage_id"]
        .as_str()
        .unwrap()
        .to_owned();

    let imported = application::artifact_import(
        &ArtifactImportRequest {
            project_root: project.clone(),
            lease: lease.clone(),
            stage_id: stage_id.clone(),
        },
        &repo,
        &mut executor,
    );
    assert_eq!(imported.code, "artifact_imported");
    assert!(
        project
            .join(imported.data.as_ref().unwrap()["path"].as_str().unwrap())
            .is_file()
    );

    let produced = application::produce_staged_artifact(
        &ProduceStagedArtifactRequest {
            project_root: project.clone(),
            lease: lease.clone(),
            stage_id: stage_id.clone(),
            artifact: "script-proposal.md".to_owned(),
            produced_by: "agent".to_owned(),
        },
        &repo,
        &mut executor,
    );
    assert_eq!(produced.code, "artifact_produced");
    assert_eq!(
        fs::read_to_string(project.join("script-proposal.md")).unwrap(),
        "# Reviewed script\n"
    );

    for forbidden in [
        "narration-final.mp3",
        "output/cover.png",
        "quality-review/editorial-preview/review.json",
        "publish/publish-approval.json",
        "project-contract.json",
    ] {
        let result = application::produce_staged_artifact(
            &ProduceStagedArtifactRequest {
                project_root: project.clone(),
                lease: lease.clone(),
                stage_id: stage_id.clone(),
                artifact: forbidden.to_owned(),
                produced_by: "agent".to_owned(),
            },
            &repo,
            &mut executor,
        );
        assert_eq!(
            result.code, "staged_artifact_target_refused",
            "{forbidden} escaped the staged target allowlist"
        );
    }
}

#[test]
fn staging_requires_exactly_one_bounded_source_and_valid_stage_ids() {
    let (_directory, _workspace, project, lease, _capability) = fixture();
    let repo = repo_root();
    let mut executor = ProcessExecutor;
    let invalid = application::artifact_stage(
        &ArtifactStageRequest {
            project_root: project.clone(),
            lease: lease.clone(),
            role: "script_notes".to_owned(),
            inbox_path: Some("../escape".to_owned()),
            inline_text: Some("both".to_owned()),
        },
        &repo,
        &mut executor,
    );
    assert_eq!(invalid.code, "invalid_input");

    let invalid = application::artifact_import(
        &ArtifactImportRequest {
            project_root: project,
            lease,
            stage_id: "../escape".to_owned(),
        },
        &repo,
        &mut executor,
    );
    assert_eq!(invalid.code, "invalid_input");
}

#[test]
fn full_size_inline_text_uses_a_private_request_file_not_process_argv() {
    let (_directory, _workspace, project, lease, _capability) = fixture();
    let repo = repo_root();
    let mut executor = ProcessExecutor;
    let staged = application::artifact_stage(
        &ArtifactStageRequest {
            project_root: project.clone(),
            lease,
            role: "script_notes".to_owned(),
            inbox_path: None,
            inline_text: Some("x".repeat(1024 * 1024)),
        },
        &repo,
        &mut executor,
    );
    assert_eq!(staged.code, "artifact_staged");
    let requests = project.join(".hvp/intake-requests");
    assert!(requests.is_dir());
    assert_eq!(fs::read_dir(requests).unwrap().count(), 0);
}

#[test]
fn staged_artifact_cannot_cross_verified_lease_owners() {
    let (_directory, _workspace, project, first_lease, first_capability) = fixture();
    let repo = repo_root();
    let mut executor = ProcessExecutor;
    let staged = application::artifact_stage(
        &ArtifactStageRequest {
            project_root: project.clone(),
            lease: first_lease,
            role: "script_notes".to_owned(),
            inbox_path: None,
            inline_text: Some("# Owner-bound script\n".to_owned()),
        },
        &repo,
        &mut executor,
    );
    assert_eq!(staged.code, "artifact_staged");
    let stage_id = staged.data.as_ref().unwrap()["stage_id"]
        .as_str()
        .unwrap()
        .to_owned();

    let store = ProjectStore::new(&project);
    store.release("agent", &first_capability).unwrap();
    let second_lease = store
        .claim_at("reviewer", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let second_lease = LeaseInput::from_lease(&second_lease);

    let imported = application::artifact_import(
        &ArtifactImportRequest {
            project_root: project.clone(),
            lease: second_lease.clone(),
            stage_id: stage_id.clone(),
        },
        &repo,
        &mut executor,
    );
    assert_eq!(imported.code, "stage_owner_mismatch");

    let produced = application::produce_staged_artifact(
        &ProduceStagedArtifactRequest {
            project_root: project.clone(),
            lease: second_lease,
            stage_id,
            artifact: "script-proposal.md".to_owned(),
            produced_by: "reviewer".to_owned(),
        },
        &repo,
        &mut executor,
    );
    assert_eq!(produced.code, "stage_owner_mismatch");
    assert!(!project.join("script-proposal.md").exists());
}
