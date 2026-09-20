use std::fs;
use std::path::PathBuf;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, UNIX_EPOCH};

use pipeline::{GateStatus, ProjectSnapshot, ProjectStore, StoreError};
use serde_json::{Value, json};
use tempfile::tempdir;

fn render_status(project: &str) -> Value {
    json!({
        "schema": "haru.pipeline_status.v1",
        "project": project,
        "required_stages": ["render"],
        "stages": {
            "render": {"status": "pass", "files": ["output.mp4"]}
        }
    })
}

#[test]
fn matching_receipt_reuses_output_until_input_or_bytes_change() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir(&project).unwrap();
    let output = project.join("output.mp4");
    fs::write(&output, "first output").unwrap();
    let store = ProjectStore::new(&project);
    let started = UNIX_EPOCH + Duration::from_secs(5_000);
    let lease = store
        .claim_at("hermes", Duration::from_secs(60), started)
        .unwrap();

    let status = render_status("demo");
    let initial = store.canonical_snapshot_at(&status, started).unwrap();
    let render_input = initial.gates["render"].input_digest.clone();
    store
        .record_gate_at(
            "hermes",
            &lease.token,
            "render",
            &render_input,
            &[PathBuf::from("output.mp4")],
            started + Duration::from_secs(1),
        )
        .unwrap();

    let refreshed = store
        .refresh_from_status_at(
            "hermes",
            &lease.token,
            &status,
            started + Duration::from_secs(2),
        )
        .unwrap();
    assert_eq!(refreshed.gates["render"].status, GateStatus::Complete);
    assert!(refreshed.receipts.contains_key("render"));
    assert!(!store.should_run("render", &render_input).unwrap());
    assert!(store.should_run("render", "sha256:input-v2").unwrap());

    fs::write(&output, "changed output").unwrap();
    assert!(store.should_run("render", &render_input).unwrap());
}

#[test]
fn mutations_require_the_current_unexpired_lease() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir(&project).unwrap();
    fs::write(project.join("output.mp4"), "output").unwrap();
    let store = ProjectStore::new(&project);
    let started = UNIX_EPOCH + Duration::from_secs(6_000);
    let lease = store
        .claim_at("openclaw", Duration::from_secs(10), started)
        .unwrap();

    assert!(matches!(
        store.record_gate_at(
            "hermes",
            &lease.token,
            "render",
            "sha256:input",
            &[PathBuf::from("output.mp4")],
            started + Duration::from_secs(1),
        ),
        Err(StoreError::LeaseMismatch)
    ));
    assert!(matches!(
        store.record_gate_at(
            "openclaw",
            &lease.token,
            "render",
            "sha256:input",
            &[PathBuf::from("output.mp4")],
            started + Duration::from_secs(11),
        ),
        Err(StoreError::LeaseExpired)
    ));
}

#[test]
fn concurrent_snapshot_refreshes_leave_one_complete_json_document() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir(&project).unwrap();
    let store = Arc::new(ProjectStore::new(&project));
    let started = UNIX_EPOCH + Duration::from_secs(7_000);
    let lease = store
        .claim_at("codex", Duration::from_secs(120), started)
        .unwrap();
    let status = Arc::new(render_status("demo"));

    let handles: Vec<_> = (1..=8)
        .map(|offset| {
            let store = Arc::clone(&store);
            let status = Arc::clone(&status);
            let token = lease.token.clone();
            thread::spawn(move || {
                store
                    .refresh_from_status_at(
                        "codex",
                        &token,
                        &status,
                        started + Duration::from_secs(offset),
                    )
                    .unwrap();
            })
        })
        .collect();
    for handle in handles {
        handle.join().unwrap();
    }

    let bytes = fs::read(project.join(".hvp/state.json")).unwrap();
    let snapshot: ProjectSnapshot = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(snapshot.schema_version, 1);
    assert_eq!(snapshot.project, "demo");
    assert!((7_001..=7_008).contains(&snapshot.updated_at));
    assert!(fs::read_dir(project.join(".hvp")).unwrap().all(|entry| {
        !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .ends_with(".tmp")
    }));
}

#[test]
fn receipt_outputs_cannot_escape_the_project() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir(&project).unwrap();
    fs::write(directory.path().join("outside.mp4"), "outside").unwrap();
    let store = ProjectStore::new(&project);
    let started = UNIX_EPOCH + Duration::from_secs(8_000);
    let lease = store
        .claim_at("hermes", Duration::from_secs(60), started)
        .unwrap();

    assert!(matches!(
        store.record_gate_at(
            "hermes",
            &lease.token,
            "render",
            "sha256:input",
            &[PathBuf::from("../outside.mp4")],
            started + Duration::from_secs(1),
        ),
        Err(StoreError::ArtifactOutsideProject(_))
    ));
}

#[test]
fn unsupported_receipt_schema_fails_closed() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir_all(project.join(".hvp/receipts")).unwrap();
    fs::write(
        project.join(".hvp/receipts/render.json"),
        r#"{
          "schema_version": 99,
          "gate": "render",
          "input_digest": "sha256:input",
          "output_digest": "sha256:output",
          "outputs": [],
          "completed_at": 1
        }"#,
    )
    .unwrap();

    assert!(matches!(
        ProjectStore::new(&project).should_run("render", "sha256:input"),
        Err(StoreError::InvalidReceipt(_))
    ));
}
