use std::fs;
use std::time::{Duration, UNIX_EPOCH};

use pipeline::{GateStatus, ProjectStore, StoreError};
use serde_json::{Value, json};
use tempfile::tempdir;

fn status(project: &str, tts: &str) -> Value {
    json!({
        "schema": "haru.pipeline_status.v1",
        "project": project,
        "required_stages": ["selection", "proposal", "tts"],
        "stages": {
            "selection": {"status": "pass", "files": ["project-contract.json"]},
            "proposal": {"status": "pass", "files": ["script-proposal.md"]},
            "tts": {
                "status": tts,
                "files": ["narration-final.mp3"],
                "warnings": if tts == "warn" { json!(["pron SHA mismatch"]) } else { json!([]) }
            }
        }
    })
}

#[test]
fn derives_resume_and_artifacts_only_from_canonical_status() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir(&project).unwrap();
    fs::write(project.join("project-contract.json"), "{}").unwrap();
    fs::write(project.join("script-proposal.md"), "proposal").unwrap();
    fs::write(project.join("narration-final.mp3"), "audio").unwrap();

    let snapshot = ProjectStore::new(&project)
        .canonical_snapshot_at(
            &status("demo", "pass"),
            UNIX_EPOCH + Duration::from_secs(3_000),
        )
        .unwrap();

    assert_eq!(snapshot.last_successful_gate.as_deref(), Some("tts"));
    assert_eq!(snapshot.next_gate(), None);
    assert_eq!(snapshot.gates["tts"].status, GateStatus::Complete);
    assert_eq!(snapshot.gates["tts"].artifacts[0].bytes, 5);
}

#[test]
fn warning_stops_resume_even_when_the_artifact_exists() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir(&project).unwrap();
    fs::write(project.join("project-contract.json"), "{}").unwrap();
    fs::write(project.join("script-proposal.md"), "proposal").unwrap();
    fs::write(project.join("narration-final.mp3"), "audio").unwrap();

    let snapshot = ProjectStore::new(&project)
        .canonical_snapshot_at(
            &status("demo", "warn"),
            UNIX_EPOCH + Duration::from_secs(4_000),
        )
        .unwrap();

    assert_eq!(snapshot.gates["tts"].status, GateStatus::Warning);
    assert_eq!(snapshot.last_successful_gate.as_deref(), Some("proposal"));
    assert_eq!(snapshot.next_gate(), Some("tts"));
}

#[test]
fn rejects_status_for_different_project() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir(&project).unwrap();

    let result = ProjectStore::new(&project).canonical_snapshot_at(
        &status("other", "pass"),
        UNIX_EPOCH + Duration::from_secs(4_500),
    );

    assert!(matches!(result, Err(StoreError::InvalidStatus(_))));
}
