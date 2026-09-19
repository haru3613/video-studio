use std::fs;
use std::io::Write;
use std::path::Path;
use std::process::{Command, Output, Stdio};
use std::sync::Mutex;

use serde_json::{Value, json};
use tempfile::tempdir;

// Development `hvp-mcp` instances validate the same checkout and binary.
// Serialize these process-level checks so one test cannot observe another
// instance while it is shutting down; the lease test server below is isolated.
static REAL_SERVER_LOCK: Mutex<()> = Mutex::new(());

fn cli(arguments: &[&str], state_root: &Path) -> Output {
    Command::new(env!("CARGO_BIN_EXE_video-studio"))
        .arg("--json")
        .arg("--server")
        .arg(env!("CARGO_BIN_EXE_hvp-mcp"))
        .args(arguments)
        .env("HVP_RUNTIME_STATE", state_root)
        .output()
        .unwrap()
}

fn output_json(output: &Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|error| {
        panic!(
            "CLI stdout was not JSON: {error}; stdout={}; stderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr),
        )
    })
}

fn write_compatible_project(path: &Path) {
    fs::create_dir(path).unwrap();
    fs::write(
        path.join("project-contract.json"),
        serde_json::to_vec(&json!({
            "schema": "haru.project_contract.v1",
            "runtime_contract": {
                "schema": "haru.project_runtime_contract.v1",
                "runtime": "haru.runtime.v1",
                "evaluator": "haru.evaluator.v1",
                "artifact": "haru.artifact.v1"
            },
            "lane_contract": "HVP_TODO_REPLACE_ME",
            "production_profile": "HVP_TODO_REPLACE_ME",
            "publish_target": { "youtube_channel_id": "example-channel" }
        }))
        .unwrap(),
    )
    .unwrap();
}

#[cfg(unix)]
fn write_lease_test_server(path: &Path) {
    use std::os::unix::fs::PermissionsExt;

    fs::write(
        path,
        r###"#!/usr/bin/python3 -S
import json
import os
import sys

state_path = os.environ["VIDEO_STUDIO_TEST_LEASE_STATE"]

def result(code, project, data):
    return {
        "content": [],
        "structuredContent": {
            "schema_version": 1,
            "outcome": "ok",
            "code": code,
            "project": project,
            "data": data,
        },
        "isError": False,
    }

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        response = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "lease-test-server", "version": "1"},
        }
    elif method == "tools/call":
        tool = request["params"]["name"]
        arguments = request["params"]["arguments"]
        project = arguments["project_root"]
        if tool == "lease_claim":
            lease = {"owner": arguments["owner"], "lease_id": "lease-test-1", "active": True}
            with open(state_path, "w") as output:
                json.dump(lease, output)
            response = result("lease_claimed", project, lease)
        elif tool == "lease_renew":
            with open(state_path) as source:
                lease = json.load(source)
            assert arguments["owner"] == lease["owner"]
            assert arguments["lease_id"] == lease["lease_id"]
            response = result("lease_renewed", project, lease)
        elif tool == "lease_release":
            with open(state_path) as source:
                lease = json.load(source)
            assert arguments["lease_id"] == lease["lease_id"]
            os.unlink(state_path)
            response = result("lease_released", project, {"lease_id": lease["lease_id"], "active": False})
        elif tool == "lease_status":
            response = result("lease_absent", project, {"active": False})
        elif tool == "job_status":
            assert arguments == {
                "schema_version": 1,
                "project_root": project,
                "job_id": "0123456789abcdef0123456789abcdef",
            }
            response = result("job_status", project, {"job_id": arguments["job_id"], "status": "running"})
        elif tool == "job_logs":
            assert arguments["job_id"] == "0123456789abcdef0123456789abcdef"
            assert arguments["max_bytes"] == 4096
            response = result("job_logs", project, {"job_id": arguments["job_id"], "text": "bounded"})
        elif tool == "job_cancel":
            assert arguments["owner"] == "test-agent"
            assert arguments["lease_id"] == "lease-test-1"
            assert arguments["idempotency_key"] == "cancel-1"
            assert "executable" not in arguments
            response = result("job_cancelled", project, {"job_id": arguments["job_id"], "status": "cancelled"})
        elif tool == "job_resume":
            assert arguments["owner"] == "test-agent"
            assert "tools_root" not in arguments
            assert "executable" not in arguments
            response = result("job_resumed", project, {"job_id": arguments["job_id"], "status": "running", "epoch": 2})
        else:
            raise AssertionError("unexpected tool " + tool)
    else:
        response = {"tools": []}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": response}), flush=True)
"###,
    )
    .unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}

#[test]
fn tools_discovers_the_complete_real_mcp_surface() {
    let _server = REAL_SERVER_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let directory = tempdir().unwrap();
    let output = cli(&["tools"], &directory.path().join("state"));
    assert!(
        output.status.success(),
        "stderr={}",
        String::from_utf8_lossy(&output.stderr)
    );

    let value = output_json(&output);
    assert_eq!(value["outcome"], "ok");
    let tools = value["data"]["tools"].as_array().unwrap();
    assert_eq!(tools.len(), 28);
    let mut names = tools
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect::<Vec<_>>();
    names.sort_unstable();
    assert_eq!(
        names,
        [
            "approve_publish",
            "artifact_index",
            "create",
            "delivery_status",
            "export_delivery",
            "job_cancel",
            "job_logs",
            "job_resume",
            "job_status",
            "lease_claim",
            "lease_release",
            "lease_renew",
            "lease_status",
            "prepare_publish",
            "prepare_publish_approval",
            "produce_artifact",
            "pronunciation_review",
            "publish",
            "reconcile_upload",
            "record_selection",
            "replace_thumbnail",
            "review_feedback",
            "review_resolve",
            "run_next",
            "select",
            "status",
            "verify",
            "visual_qa",
        ]
    );
}

#[test]
fn generic_call_and_named_lease_status_share_the_real_protocol_semantics() {
    let _server = REAL_SERVER_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let directory = tempdir().unwrap();
    let project = directory.path().join("project");
    let state = directory.path().join("state");
    write_compatible_project(&project);

    let input = directory.path().join("input.json");
    fs::write(
        &input,
        serde_json::to_vec(&json!({
            "schema_version": 1,
            "project_root": project,
        }))
        .unwrap(),
    )
    .unwrap();
    let generic = cli(
        &["call", "lease_status", "--input", input.to_str().unwrap()],
        &state,
    );
    assert!(generic.status.success());
    assert_eq!(output_json(&generic)["code"], "lease_absent");

    let named = cli(
        &[
            "lease",
            "status",
            "--project-root",
            project.to_str().unwrap(),
        ],
        &state,
    );
    assert!(named.status.success());
    assert_eq!(output_json(&named), output_json(&generic));
}

#[test]
fn stdin_input_is_structured_and_application_blocking_controls_the_exit_code() {
    let _server = REAL_SERVER_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let directory = tempdir().unwrap();
    let projects = directory.path().join("projects");
    let state = directory.path().join("state");
    fs::create_dir(&projects).unwrap();

    let mut child = Command::new(env!("CARGO_BIN_EXE_video-studio"))
        .args([
            "--json",
            "--server",
            env!("CARGO_BIN_EXE_hvp-mcp"),
            "call",
            "create",
            "--input",
            "-",
        ])
        .env("HVP_RUNTIME_STATE", &state)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(
            serde_json::to_string(&json!({
                "schema_version": 1,
                "projects_root": projects,
                "project": "must-not-be-created",
                "idempotency_key": "public-cli-test",
            }))
            .unwrap()
            .as_bytes(),
        )
        .unwrap();
    let output = child.wait_with_output().unwrap();

    assert_eq!(
        output.status.code(),
        Some(3),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let value = output_json(&output);
    assert_eq!(value["outcome"], "blocked");
    assert_eq!(value["code"], "runtime_unpromoted");
    assert!(
        !directory
            .path()
            .join("projects/must-not-be-created")
            .exists()
    );
}

#[test]
fn invalid_json_fails_before_any_server_or_workflow_action() {
    let directory = tempdir().unwrap();
    let input = directory.path().join("input.json");
    fs::write(&input, b"[]").unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_video-studio"))
        .args([
            "--json",
            "--server",
            "/definitely/not/a/server",
            "call",
            "status",
            "--input",
            input.to_str().unwrap(),
        ])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(output_json(&output)["code"], "invalid_input");
}

#[test]
fn invalid_job_id_is_rejected_before_server_start() {
    let output = Command::new(env!("CARGO_BIN_EXE_video-studio"))
        .args([
            "--json",
            "--server",
            "/definitely/not/a/server",
            "job",
            "status",
            "--project-root",
            "/tmp/project",
            "--job-id",
            "../another-job",
        ])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert_eq!(output_json(&output)["code"], "invalid_input");
}

#[cfg(unix)]
#[test]
fn named_lease_commands_complete_a_protocol_lifecycle() {
    let directory = tempdir().unwrap();
    let server = directory.path().join("lease-test-server");
    let state = directory.path().join("lease.json");
    let project = directory.path().join("project");
    write_lease_test_server(&server);

    let run = |arguments: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_video-studio"))
            .arg("--json")
            .arg("--server")
            .arg(&server)
            .args(arguments)
            .env("VIDEO_STUDIO_TEST_LEASE_STATE", &state)
            .output()
            .unwrap()
    };

    let claim = run(&[
        "lease",
        "claim",
        "--project-root",
        project.to_str().unwrap(),
        "--owner",
        "test-agent",
        "--ttl-seconds",
        "60",
        "--idempotency-key",
        "claim-1",
    ]);
    assert!(claim.status.success());
    assert_eq!(output_json(&claim)["code"], "lease_claimed");

    let renew = run(&[
        "lease",
        "renew",
        "--project-root",
        project.to_str().unwrap(),
        "--owner",
        "test-agent",
        "--lease-id",
        "lease-test-1",
        "--ttl-seconds",
        "60",
        "--idempotency-key",
        "renew-1",
    ]);
    assert!(renew.status.success());
    assert_eq!(output_json(&renew)["code"], "lease_renewed");

    let release = run(&[
        "lease",
        "release",
        "--project-root",
        project.to_str().unwrap(),
        "--owner",
        "test-agent",
        "--lease-id",
        "lease-test-1",
        "--idempotency-key",
        "release-1",
    ]);
    assert!(release.status.success());
    assert_eq!(output_json(&release)["code"], "lease_released");

    let status = run(&[
        "lease",
        "status",
        "--project-root",
        project.to_str().unwrap(),
    ]);
    assert!(status.status.success());
    assert_eq!(output_json(&status)["code"], "lease_absent");
}

#[cfg(unix)]
#[test]
fn named_job_commands_send_typed_payloads_without_executables() {
    let directory = tempdir().unwrap();
    let server = directory.path().join("job-test-server");
    let unused_state = directory.path().join("unused.json");
    let project = directory.path().join("project");
    let job_id = "0123456789abcdef0123456789abcdef";
    write_lease_test_server(&server);

    let run = |arguments: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_video-studio"))
            .arg("--json")
            .arg("--server")
            .arg(&server)
            .args(arguments)
            .env("VIDEO_STUDIO_TEST_LEASE_STATE", &unused_state)
            .output()
            .unwrap()
    };

    let status = run(&[
        "job",
        "status",
        "--project-root",
        project.to_str().unwrap(),
        "--job-id",
        job_id,
    ]);
    assert!(
        status.status.success(),
        "{}",
        String::from_utf8_lossy(&status.stderr)
    );
    assert_eq!(output_json(&status)["code"], "job_status");

    let logs = run(&[
        "job",
        "logs",
        "--project-root",
        project.to_str().unwrap(),
        "--job-id",
        job_id,
        "--max-bytes",
        "4096",
    ]);
    assert!(logs.status.success());
    assert_eq!(output_json(&logs)["code"], "job_logs");

    let cancel = run(&[
        "job",
        "cancel",
        "--project-root",
        project.to_str().unwrap(),
        "--job-id",
        job_id,
        "--owner",
        "test-agent",
        "--lease-id",
        "lease-test-1",
        "--idempotency-key",
        "cancel-1",
    ]);
    assert!(cancel.status.success());
    assert_eq!(output_json(&cancel)["code"], "job_cancelled");

    let resume = run(&[
        "job",
        "resume",
        "--project-root",
        project.to_str().unwrap(),
        "--job-id",
        job_id,
        "--owner",
        "test-agent",
        "--lease-id",
        "lease-test-1",
        "--idempotency-key",
        "resume-1",
    ]);
    assert!(resume.status.success());
    assert_eq!(output_json(&resume)["code"], "job_resumed");
}
