use std::ffi::OsString;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, SystemTime};

use pipeline::ProjectStore;
use pipeline::application::{
    self, CommandExecutor, CommandResult, ExportDeliveryRequest, JobMutationRequest, LeaseInput,
    ProcessExecutor,
};
use serde_json::{Value, json};
use tempfile::tempdir;

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

fn envelope(project: &Path, code: &str, payload: Value) -> Value {
    json!({
        "schema_version": 1,
        "outcome": "ok",
        "code": code,
        "project": project,
        "data": payload,
    })
}

fn status_payload(project: &Path) -> Value {
    json!({
        "schema": "video_studio.local_delivery_status.v1",
        "project": project.file_name().unwrap().to_str().unwrap(),
        "status": "technical_ready",
        "blockers": [],
        "warnings": [],
        "publication_ready": false,
        "human_approval": false,
    })
}

fn job_payload(job_id: &str, status: &str) -> Value {
    json!({
        "schema": "haru.render_job.v2",
        "job_id": job_id,
        "kind": "render",
        "status": status,
        "epoch": 1,
        "revision": "a".repeat(64),
        "exit_code": Value::Null,
        "error_code": Value::Null,
        "created_at": "2026-09-19T00:00:00+00:00",
        "updated_at": "2026-09-19T00:00:01+00:00",
        "output": "output/final.mp4",
        "log_available": true,
        "can_cancel": status == "running",
        "can_resume": status == "interrupted",
    })
}

#[test]
fn read_only_delivery_status_uses_only_the_fixed_runner_and_rejects_approval_claims() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/demo");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir_all(&project).unwrap();
    fs::write(repo.join("scripts/local-delivery"), "#!/bin/sh\n").unwrap();
    let project = project.canonicalize().unwrap();
    let mut executor = FakeExecutor {
        data: Some(envelope(
            &project,
            "delivery_technical_ready",
            status_payload(&project),
        )),
        ..FakeExecutor::default()
    };

    let result = application::delivery_status(&project, &repo, &mut executor);
    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "delivery_technical_ready")
    );
    assert_eq!(executor.calls.len(), 1);
    assert_eq!(
        executor.calls[0].0,
        repo.canonicalize().unwrap().join("scripts/local-delivery")
    );
    assert_eq!(
        executor.calls[0].1,
        [OsString::from("status"), project.clone().into_os_string()]
    );

    let mut dishonest = status_payload(&project);
    dishonest["human_approval"] = json!(true);
    let mut executor = FakeExecutor {
        data: Some(envelope(&project, "delivery_technical_ready", dishonest)),
        ..FakeExecutor::default()
    };
    let refused = application::delivery_status(&project, &repo, &mut executor);
    assert_eq!(refused.code, "command_failed");
}

#[test]
fn export_delivery_requires_a_lease_and_accepts_only_typed_non_publishable_output() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/demo");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir_all(&project).unwrap();
    fs::write(repo.join("scripts/local-delivery"), "#!/bin/sh\n").unwrap();
    let project = project.canonicalize().unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("agent", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let request = ExportDeliveryRequest {
        project_root: project.clone(),
        lease: LeaseInput::from_lease(&lease),
        idempotency_key: "delivery-once".to_owned(),
    };
    let payload = json!({
        "schema": "video_studio.delivery_export.v1",
        "project": "demo",
        "status": "exported",
        "bundle_id": "a".repeat(64),
        "bundle_path": directory.path().join("deliveries/demo/bundle"),
        "reused": false,
        "publication_ready": false,
        "human_approval": false,
    });
    let mut executor = FakeExecutor {
        data: Some(envelope(&project, "delivery_exported", payload)),
        ..FakeExecutor::default()
    };

    let result = application::export_delivery(&request, &repo, &mut executor);
    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "delivery_exported")
    );
    assert_eq!(
        executor.calls[0].1,
        [
            OsString::from("export"),
            project.clone().into_os_string(),
            OsString::from("--idempotency-key"),
            OsString::from("delivery-once"),
        ]
    );

    ProjectStore::new(&project)
        .release("agent", &lease.token)
        .unwrap();
    let mut unused = FakeExecutor::default();
    let blocked = application::export_delivery(&request, &repo, &mut unused);
    assert_eq!(blocked.code, "lease_invalid");
    assert!(unused.calls.is_empty());
}

#[cfg(unix)]
#[test]
fn process_executor_forwards_only_runner_specific_trusted_configuration() {
    use std::os::unix::fs::PermissionsExt;

    static ENV_LOCK: Mutex<()> = Mutex::new(());
    let _lock = ENV_LOCK.lock().unwrap();
    let directory = tempdir().unwrap();
    let script = "#!/bin/sh\nprintf '{\"key\":\"%s\",\"hook\":\"%s\",\"secret\":\"%s\",\"path\":\"%s\",\"chromium\":\"%s\",\"delivery\":\"%s\"}\\n' \"$ELEVENLABS_API_KEY\" \"$BASH_ENV\" \"$TOP_SECRET\" \"$PATH\" \"$VIDEO_STUDIO_CHROMIUM\" \"$VIDEO_STUDIO_DELIVERY_ROOT\"\n";
    let pronunciation = directory.path().join("pronunciation-workflow");
    let render = directory.path().join("render-project");
    let delivery = directory.path().join("local-delivery");
    fs::write(&pronunciation, script).unwrap();
    fs::write(&render, script).unwrap();
    fs::write(&delivery, script).unwrap();
    for path in [&pronunciation, &render, &delivery] {
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    unsafe {
        std::env::set_var("ELEVENLABS_API_KEY", "provider-secret");
        std::env::set_var("BASH_ENV", "/tmp/untrusted-hook");
        std::env::set_var("PYTHONPATH", "/tmp/untrusted-python");
        std::env::set_var("TOP_SECRET", "must-not-leak");
        std::env::set_var("VIDEO_STUDIO_CHROMIUM", "/opt/browser/chromium");
        std::env::set_var("VIDEO_STUDIO_DELIVERY_ROOT", "/tmp/deliveries");
    }
    let mut executor = ProcessExecutor;
    let pronunciation_result = executor.execute(&pronunciation, &[]).unwrap();
    let render_result = executor.execute(&render, &[]).unwrap();
    let delivery_result = executor.execute(&delivery, &[]).unwrap();
    unsafe {
        std::env::remove_var("ELEVENLABS_API_KEY");
        std::env::remove_var("BASH_ENV");
        std::env::remove_var("PYTHONPATH");
        std::env::remove_var("TOP_SECRET");
        std::env::remove_var("VIDEO_STUDIO_CHROMIUM");
        std::env::remove_var("VIDEO_STUDIO_DELIVERY_ROOT");
    }

    let pronunciation = pronunciation_result.data.unwrap();
    assert_eq!(pronunciation["key"], "provider-secret");
    assert_eq!(pronunciation["hook"], "");
    assert_eq!(pronunciation["secret"], "");
    assert_eq!(
        pronunciation["path"],
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    );
    let render = render_result.data.unwrap();
    assert_eq!(render["key"], "");
    assert_eq!(render["hook"], "");
    assert_eq!(render["secret"], "");
    assert_eq!(render["chromium"], "/opt/browser/chromium");
    assert_eq!(render["delivery"], "");
    let delivery = delivery_result.data.unwrap();
    assert_eq!(delivery["key"], "");
    assert_eq!(delivery["delivery"], "/tmp/deliveries");
}

#[test]
fn job_operations_use_fixed_argv_and_reject_private_projection_fields() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/demo");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir_all(&project).unwrap();
    fs::write(repo.join("scripts/render-job"), "#!/bin/sh\n").unwrap();
    let project = project.canonicalize().unwrap();
    let job_id = "a".repeat(32);

    let mut status_executor = FakeExecutor {
        data: Some(envelope(
            &project,
            "job_status",
            job_payload(&job_id, "running"),
        )),
        ..FakeExecutor::default()
    };
    let status = application::job_status(&project, &repo, &job_id, &mut status_executor);
    assert_eq!(status.code, "job_status");
    assert_eq!(
        status_executor.calls[0].1,
        [
            OsString::from("status"),
            project.clone().into_os_string(),
            OsString::from(&job_id),
        ]
    );

    let mut leaked = job_payload(&job_id, "running");
    leaked["pid"] = json!(1234);
    let mut leaked_executor = FakeExecutor {
        data: Some(envelope(&project, "job_status", leaked)),
        ..FakeExecutor::default()
    };
    assert_eq!(
        application::job_status(&project, &repo, &job_id, &mut leaked_executor).code,
        "command_failed"
    );

    let lease = ProjectStore::new(&project)
        .claim_at("agent", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let request = JobMutationRequest {
        project_root: project.clone(),
        lease: LeaseInput::from_lease(&lease),
        job_id: job_id.clone(),
    };
    let mut cancel_executor = FakeExecutor {
        data: Some(envelope(
            &project,
            "job_cancelled",
            job_payload(&job_id, "cancelled"),
        )),
        ..FakeExecutor::default()
    };
    let cancelled = application::mutate_job("cancel", &request, &repo, &mut cancel_executor);
    assert_eq!(cancelled.code, "job_cancelled");
    assert_eq!(
        cancel_executor.calls[0].1,
        [
            OsString::from("cancel"),
            project.clone().into_os_string(),
            OsString::from(job_id),
        ]
    );
}
