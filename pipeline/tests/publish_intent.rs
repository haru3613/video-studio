use std::ffi::OsString;
use std::fs;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use pipeline::application::{self, CommandExecutor, CommandResult, PreparePublishApprovalRequest};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tempfile::tempdir;

const TEST_CHANNEL: &str = "UCaaaaaaaaaaaaaaaaaaaaaa";

#[derive(Default)]
struct FakeExecutor {
    calls: Vec<(PathBuf, Vec<OsString>)>,
    exit_code: i32,
    data: Option<Value>,
}

impl CommandExecutor for FakeExecutor {
    fn execute(&mut self, program: &Path, arguments: &[OsString]) -> io::Result<CommandResult> {
        self.calls.push((program.to_path_buf(), arguments.to_vec()));
        Ok(CommandResult {
            exit_code: Some(self.exit_code),
            data: self.data.clone(),
        })
    }
}

fn signing_request(project: &Path) -> Value {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let issued_at = rfc3339_utc(now);
    let expires_at = rfc3339_utc(now + 300);
    let project_root_sha256 = format!(
        "{:x}",
        Sha256::digest(project.as_os_str().as_encoded_bytes())
    );
    let intent = json!({
        "schema": "haru.publish_approval.v3",
        "project_id": "ready-video",
        "final_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "final_bytes": 123,
        "metadata_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "cover_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        "channel_id": TEST_CHANNEL,
        "visibility": "unlisted",
        "warnings_acknowledged": [],
        "override_reason": null,
        "runtime_contract": {
            "schema": "haru.project_runtime_contract.v1",
            "runtime": "haru.runtime.v1",
            "evaluator": "haru.evaluator.v1",
            "artifact": "haru.artifact.v1"
        },
        "render_self_eval": {"path":"quality-review/render-self-eval.json","sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","bytes":12},
        "visual_qa_review": {"path":"quality-review/visual-qa-review.json","sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","bytes":13},
        "generation": 1,
        "attestation_ref": "attestation:00000000-0000-4000-8000-000000000001",
        "nonce": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    });
    let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&intent).unwrap()));
    json!({
        "schema": "haru.publish_approval_signing_request.v1",
        "project_root": project,
        "intent": intent,
        "intent_sha256": digest,
        "attestation": {
            "schema": "haru.publish_attestation.v2",
            "attestation_ref": "attestation:00000000-0000-4000-8000-000000000001",
            "project_id": "ready-video",
            "project_root_sha256": project_root_sha256,
            "approval_intent_sha256": digest,
            "nonce": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "generation": 1,
            "issued_at": issued_at,
            "expires_at": expires_at,
            "channel_id": TEST_CHANNEL,
            "visibility": "unlisted",
            "key_id": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            "signature_algorithm": "ecdsa-p256-sha256"
        }
    })
}

fn rfc3339_utc(timestamp: u64) -> String {
    let days = (timestamp / 86_400) as i64;
    let day_seconds = timestamp % 86_400;
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let day_of_era = z - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}+00:00",
        day_seconds / 3_600,
        day_seconds % 3_600 / 60,
        day_seconds % 60
    )
}

#[test]
fn preparation_uses_one_fixed_wrapper_call_and_validates_the_request() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let workspace = directory.path().join("workspace");
    let project = workspace.join("projects/ready-video");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir_all(&project).unwrap();
    let wrapper = repo.join("scripts/hvp-approve");
    fs::write(&wrapper, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o755)).unwrap();
    let project = project.canonicalize().unwrap();
    let repo = repo.canonicalize().unwrap();
    let mut executor = FakeExecutor {
        data: Some(signing_request(&project)),
        ..FakeExecutor::default()
    };

    let result = application::prepare_publish_approval(
        &PreparePublishApprovalRequest {
            project_root: project.clone(),
            override_reason: None,
        },
        &repo,
        &mut executor,
    );

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "approval_intent_prepared")
    );
    assert_eq!(executor.calls.len(), 1);
    assert_eq!(executor.calls[0].0, repo.join("scripts/hvp-approve"));
    assert_eq!(
        executor.calls[0].1,
        vec![
            OsString::from("prepare"),
            project.into_os_string(),
            OsString::from("--workspace"),
            workspace.canonicalize().unwrap().into_os_string(),
        ]
    );
}

#[test]
fn preparation_refuses_tampered_output_and_blocks_an_unenrolled_issuer() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("workspace/projects/ready-video");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir_all(&project).unwrap();
    let wrapper = repo.join("scripts/hvp-approve");
    fs::write(&wrapper, "#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(&wrapper, fs::Permissions::from_mode(0o755)).unwrap();
    let project = project.canonicalize().unwrap();
    let repo = repo.canonicalize().unwrap();

    let mut tampered = signing_request(&project);
    tampered["intent"]["generation"] = json!(2);
    let mut executor = FakeExecutor {
        data: Some(tampered),
        ..FakeExecutor::default()
    };
    let request = PreparePublishApprovalRequest {
        project_root: project.clone(),
        override_reason: None,
    };
    assert_eq!(
        application::prepare_publish_approval(&request, &repo, &mut executor).code,
        "command_failed"
    );

    let mut wrong_root = signing_request(&project);
    wrong_root["attestation"]["project_root_sha256"] =
        json!("0000000000000000000000000000000000000000000000000000000000000000");
    let mut wrong_root_executor = FakeExecutor {
        data: Some(wrong_root),
        ..FakeExecutor::default()
    };
    assert_eq!(
        application::prepare_publish_approval(&request, &repo, &mut wrong_root_executor).code,
        "command_failed"
    );

    let mut unenrolled = FakeExecutor {
        exit_code: 1,
        data: Some(json!({"ok": false, "code": "issuer_not_enrolled"})),
        ..FakeExecutor::default()
    };
    let result = application::prepare_publish_approval(&request, &repo, &mut unenrolled);
    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("blocked", "issuer_not_enrolled")
    );
}
