use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
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
        project = arguments.get("project_root", arguments.get("workspace_root"))
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
        elif tool == "artifact_stage":
            if arguments["role"] == "reference_image":
                assert arguments["inbox_path"] == "images/card.png"
                assert arguments["inline_text"] is None
            else:
                assert arguments["role"] == "metadata"
                assert arguments["inbox_path"] is None
                assert arguments["inline_text"] == '{"title":"from-file"}'
            response = result("artifact_staged", project, {"stage_id": "a" * 32, "role": arguments["role"]})
        elif tool == "artifact_import":
            assert arguments["stage_id"] == "a" * 32
            response = result("artifact_imported", project, {"stage_id": arguments["stage_id"], "path": "imports/reference_image/a.png"})
        elif tool == "produce_staged_artifact":
            assert arguments["stage_id"] == "a" * 32
            assert arguments["artifact"] == "script.md"
            assert "source_file" not in arguments
            response = result("artifact_produced", project, {"artifact": arguments["artifact"]})
        elif tool == "review_feedback":
            response = result("review_feedback", project, {
                "package_id": "a" * 64,
                "assets": [{"id": "video-current", "sha256": "b" * 64}],
                "comments": [{
                    "id": "comment-1",
                    "package_id": "a" * 64,
                    "asset": {"sha256": "b" * 64},
                }],
            })
        elif tool == "review_add":
            assert arguments["client_id"] in {
                "123e4567-e89b-12d3-a456-426614174000",
                "123e4567-e89b-12d3-a456-426614174001",
            }
            assert arguments["body"] == "timestamped feedback"
            assert arguments["package_id"] == "a" * 64
            assert arguments["asset_sha256"] == "b" * 64
            assert "owner" not in arguments and "lease_id" not in arguments
            response = result("review_comment_added", project, {"comment_id": "comment-1"})
        elif tool == "review_resolve":
            assert arguments["comment_id"] == "comment-1"
            assert arguments["status"] == "resolved"
            assert "owner" not in arguments and "lease_id" not in arguments
            response = result("review_comment_updated", project, {"comment_id": arguments["comment_id"]})
        elif tool == "delivery_status":
            response = result("delivery_status", project, {"status": "ready"})
        elif tool == "export_delivery":
            response = result(
                "diagnostics_exported" if arguments["diagnostic"] else "delivery_exported",
                project,
                {"bundle_id": "bundle-1"},
            )
        elif tool == "workspace_info":
            response = result("workspace_info", project, {"workspace_id": "workspace-1", "project_count": 1})
        elif tool == "project_list":
            response = result("project_list", project, {"workspace_id": "workspace-1", "projects": [{"project_id": "example"}]})
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
    assert_eq!(tools.len(), 34);
    let mut names = tools
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect::<Vec<_>>();
    names.sort_unstable();
    assert_eq!(
        names,
        [
            "approve_publish",
            "artifact_import",
            "artifact_index",
            "artifact_stage",
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
            "produce_staged_artifact",
            "project_list",
            "pronunciation_review",
            "publish",
            "reconcile_upload",
            "record_selection",
            "replace_thumbnail",
            "review_add",
            "review_feedback",
            "review_resolve",
            "run_next",
            "select",
            "status",
            "verify",
            "visual_qa",
            "workspace_info",
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
fn installed_local_commands_use_explicit_source_outside_checkout() {
    use std::os::unix::fs::PermissionsExt;

    let directory = tempdir().unwrap();
    let source = directory.path().join("installed-source");
    let tools = source.join("tools");
    fs::create_dir_all(tools.join("dashboard")).unwrap();
    fs::write(
        source.join("pyproject.toml"),
        "[project]\nname='installed-test'\nversion='0'\n",
    )
    .unwrap();
    fs::copy(
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("tools/workspace.py"),
        tools.join("workspace.py"),
    )
    .unwrap();
    fs::write(tools.join("dashboard/server.py"), "# fixed installed UI\n").unwrap();
    fs::write(tools.join("http_mcp.py"), "# fixed installed HTTP server\n").unwrap();
    let workspace = directory.path().join("workspace");

    let init = Command::new(env!("CARGO_BIN_EXE_video-studio"))
        .args([
            "--json",
            "--server",
            "/missing-mcp",
            "workspace",
            "init",
            "--workspace",
        ])
        .arg(&workspace)
        .env("VIDEO_STUDIO_SOURCE_ROOT", &source)
        .output()
        .unwrap();
    assert!(
        init.status.success(),
        "{}",
        String::from_utf8_lossy(&init.stderr)
    );
    assert_eq!(output_json(&init)["code"], "workspace_ready");

    let capture = directory.path().join("capture");
    let managed = directory.path().join("managed-python");
    fs::write(
        &managed,
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$VIDEO_STUDIO_TEST_CAPTURE\"\n",
    )
    .unwrap();
    fs::set_permissions(&managed, fs::Permissions::from_mode(0o755)).unwrap();
    let doctor = Command::new(env!("CARGO_BIN_EXE_video-studio"))
        .args(["--json", "--server", "/usr/bin/true", "doctor"])
        .env("VIDEO_STUDIO_SOURCE_ROOT", &source)
        .env("VIDEO_STUDIO_PYTHON", &managed)
        .env("VIDEO_STUDIO_WORKSPACE", &workspace)
        .output()
        .unwrap();
    assert!(matches!(doctor.status.code(), Some(0 | 3)));
    let checks = output_json(&doctor)["data"]["checks"].clone();
    assert_eq!(checks["installed_source"], true);
    assert_eq!(checks["mcp_server"], true);
    assert_eq!(checks["managed_python"], true);
    assert_eq!(checks["workspace"], true);

    let ui = Command::new(env!("CARGO_BIN_EXE_video-studio"))
        .args(["ui", "--workspace"])
        .arg(&workspace)
        .args(["--port", "9898"])
        .env("VIDEO_STUDIO_SOURCE_ROOT", &source)
        .env("VIDEO_STUDIO_PYTHON", &managed)
        .env("VIDEO_STUDIO_TEST_CAPTURE", &capture)
        .output()
        .unwrap();
    assert!(ui.status.success());
    let ui_arguments = fs::read_to_string(&capture).unwrap();
    assert!(ui_arguments.contains(source.join("tools/dashboard/server.py").to_str().unwrap()));
    assert!(!ui_arguments.contains(".worktrees/oss-implementation"));

    let config = directory.path().join("http.toml");
    fs::write(&config, "# owner config\n").unwrap();
    let http = Command::new(env!("CARGO_BIN_EXE_video-studio"))
        .args(["serve", "http", "--config"])
        .arg(&config)
        .env("VIDEO_STUDIO_SOURCE_ROOT", &source)
        .env("VIDEO_STUDIO_PYTHON", &managed)
        .env("VIDEO_STUDIO_TEST_CAPTURE", &capture)
        .output()
        .unwrap();
    assert!(http.status.success());
    assert!(
        fs::read_to_string(&capture)
            .unwrap()
            .contains(source.join("tools/http_mcp.py").to_str().unwrap())
    );
    let mcp = Command::new(env!("CARGO_BIN_EXE_video-studio"))
        .arg("--server")
        .arg(&managed)
        .args(["serve", "mcp"])
        .env("VIDEO_STUDIO_TEST_CAPTURE", &capture)
        .output()
        .unwrap();
    assert!(mcp.status.success());
}

#[cfg(unix)]
#[test]
fn installed_workspace_backup_and_restore_use_managed_python_and_release_source() {
    let directory = tempdir().unwrap();
    let source = directory.path().join("installed-source");
    let tools = source.join("tools");
    fs::create_dir_all(&tools).unwrap();
    fs::write(
        source.join("pyproject.toml"),
        "[project]\nname='installed-test'\nversion='0'\n",
    )
    .unwrap();
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    for name in [
        "workspace.py",
        "workspace_backup.py",
        "workspace_barrier.py",
    ] {
        fs::copy(repo.join("tools").join(name), tools.join(name)).unwrap();
    }
    let workspace = directory.path().join("workspace");
    let common = |command: &mut Command| {
        command
            .arg("--json")
            .arg("--server")
            .arg("/missing-mcp")
            .env("VIDEO_STUDIO_SOURCE_ROOT", &source)
            .env("VIDEO_STUDIO_PYTHON", "/usr/bin/python3");
    };
    let mut init_command = Command::new(env!("CARGO_BIN_EXE_video-studio"));
    common(&mut init_command);
    let init = init_command
        .args(["workspace", "init", "--workspace"])
        .arg(&workspace)
        .output()
        .unwrap();
    assert!(
        init.status.success(),
        "{}",
        String::from_utf8_lossy(&init.stderr)
    );
    let project = workspace.join("projects/demo");
    fs::create_dir(&project).unwrap();
    fs::write(project.join("notes.txt"), "durable workspace bytes").unwrap();
    let backups = directory.path().join("backups");
    fs::create_dir(&backups).unwrap();

    let mut backup_command = Command::new(env!("CARGO_BIN_EXE_video-studio"));
    common(&mut backup_command);
    let backup = backup_command
        .args(["workspace", "backup", "--workspace"])
        .arg(&workspace)
        .args(["--destination"])
        .arg(&backups)
        .output()
        .unwrap();
    assert!(
        backup.status.success(),
        "{}",
        String::from_utf8_lossy(&backup.stderr)
    );
    let backup_json = output_json(&backup);
    assert_eq!(backup_json["code"], "workspace_backup_complete");
    let backup_path = PathBuf::from(backup_json["data"]["path"].as_str().unwrap());

    let restored = directory.path().join("restored");
    let mut restore_command = Command::new(env!("CARGO_BIN_EXE_video-studio"));
    common(&mut restore_command);
    let restore = restore_command
        .args(["workspace", "restore", "--backup"])
        .arg(&backup_path)
        .args(["--destination"])
        .arg(&restored)
        .output()
        .unwrap();
    assert!(
        restore.status.success(),
        "{}",
        String::from_utf8_lossy(&restore.stderr)
    );
    assert_eq!(output_json(&restore)["code"], "workspace_restore_complete");
    assert_eq!(
        fs::read_to_string(restored.join("projects/demo/notes.txt")).unwrap(),
        "durable workspace bytes"
    );
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

#[cfg(unix)]
#[test]
fn named_workflow_commands_use_staged_ids_and_shared_mcp_tools() {
    let directory = tempdir().unwrap();
    let server = directory.path().join("workflow-test-server");
    let state = directory.path().join("unused.json");
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
    let mutation = [
        "--owner",
        "agent",
        "--lease-id",
        "lease-1",
        "--idempotency-key",
    ];

    let stage = run(&[
        "artifact",
        "stage",
        "--project-root",
        project.to_str().unwrap(),
        "--role",
        "reference_image",
        "--inbox",
        "images/card.png",
        mutation[0],
        mutation[1],
        mutation[2],
        mutation[3],
        mutation[4],
        "stage-1",
    ]);
    assert!(
        stage.status.success(),
        "{}",
        String::from_utf8_lossy(&stage.stderr)
    );
    assert_eq!(output_json(&stage)["code"], "artifact_staged");

    let raw_input = directory.path().join("artifact-stage.json");
    fs::write(
        &raw_input,
        serde_json::to_vec(&json!({
            "schema_version": 1,
            "project_root": project,
            "owner": "agent",
            "lease_id": "lease-1",
            "role": "reference_image",
            "inbox_path": "images/card.png",
            "inline_text": Value::Null,
            "idempotency_key": "stage-raw",
        }))
        .unwrap(),
    )
    .unwrap();
    let raw_stage = run(&[
        "call",
        "artifact_stage",
        "--input",
        raw_input.to_str().unwrap(),
    ]);
    assert!(raw_stage.status.success());
    assert_eq!(output_json(&raw_stage)["data"], output_json(&stage)["data"]);

    let text_file = directory.path().join("metadata.json");
    fs::write(&text_file, r#"{"title":"from-file"}"#).unwrap();
    let stage_text = run(&[
        "artifact",
        "stage",
        "--project-root",
        project.to_str().unwrap(),
        "--role",
        "metadata",
        "--text-file",
        text_file.to_str().unwrap(),
        mutation[0],
        mutation[1],
        mutation[2],
        mutation[3],
        mutation[4],
        "stage-2",
    ]);
    assert!(stage_text.status.success());
    assert_eq!(output_json(&stage_text)["code"], "artifact_staged");

    let import = run(&[
        "artifact",
        "import",
        "--project-root",
        project.to_str().unwrap(),
        "--stage-id",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        mutation[0],
        mutation[1],
        mutation[2],
        mutation[3],
        mutation[4],
        "import-1",
    ]);
    assert!(import.status.success());
    assert_eq!(output_json(&import)["code"], "artifact_imported");

    let produce = run(&[
        "produce",
        "--project-root",
        project.to_str().unwrap(),
        "--stage-id",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--artifact",
        "script.md",
        "--produced-by",
        "agent",
        mutation[0],
        mutation[1],
        mutation[2],
        mutation[3],
        mutation[4],
        "produce-1",
    ]);
    assert!(produce.status.success());
    assert_eq!(output_json(&produce)["code"], "artifact_produced");

    let review = run(&[
        "review",
        "list",
        "--project-root",
        project.to_str().unwrap(),
    ]);
    assert!(review.status.success());
    assert_eq!(output_json(&review)["code"], "review_feedback");

    let review_body = directory.path().join("review.txt");
    fs::write(&review_body, "timestamped feedback").unwrap();
    let add = run(&[
        "review",
        "add",
        "--project-root",
        project.to_str().unwrap(),
        "--client-id",
        "123e4567-e89b-12d3-a456-426614174000",
        "--package-id",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--asset-id",
        "video-current",
        "--asset-sha256",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "--timestamp-seconds",
        "1.25",
        "--body-file",
        review_body.to_str().unwrap(),
        "--idempotency-key",
        "review-add-1",
    ]);
    assert!(
        add.status.success(),
        "{}",
        String::from_utf8_lossy(&add.stderr)
    );
    assert_eq!(output_json(&add)["code"], "review_comment_added");
    let add_current = run(&[
        "review",
        "add",
        "--project-root",
        project.to_str().unwrap(),
        "--client-id",
        "123e4567-e89b-12d3-a456-426614174001",
        "--asset-id",
        "video-current",
        "--timestamp-seconds",
        "1.25",
        "--body",
        "timestamped feedback",
        "--idempotency-key",
        "review-add-current",
    ]);
    assert!(add_current.status.success());
    assert_eq!(output_json(&add_current)["code"], "review_comment_added");

    let resolve = run(&[
        "review",
        "resolve",
        "--project-root",
        project.to_str().unwrap(),
        "--comment-id",
        "comment-1",
        "--status",
        "resolved",
        "--expected-package-id",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--expected-asset-sha256",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "--idempotency-key",
        "resolve-1",
    ]);
    assert!(resolve.status.success());
    assert_eq!(output_json(&resolve)["code"], "review_comment_updated");
    let resolve_current = run(&[
        "review",
        "resolve",
        "--project-root",
        project.to_str().unwrap(),
        "--comment-id",
        "comment-1",
        "--status",
        "resolved",
        "--idempotency-key",
        "resolve-current",
    ]);
    assert!(resolve_current.status.success());
    assert_eq!(
        output_json(&resolve_current)["code"],
        "review_comment_updated"
    );

    let delivery = run(&[
        "delivery",
        "status",
        "--project-root",
        project.to_str().unwrap(),
    ]);
    assert!(delivery.status.success());
    assert_eq!(output_json(&delivery)["code"], "delivery_status");

    let export = run(&[
        "delivery",
        "export",
        "--project-root",
        project.to_str().unwrap(),
        mutation[0],
        mutation[1],
        mutation[2],
        mutation[3],
        mutation[4],
        "export-1",
    ]);
    assert!(export.status.success());
    assert_eq!(output_json(&export)["code"], "delivery_exported");
    let diagnostic = run(&[
        "delivery",
        "export",
        "--project-root",
        project.to_str().unwrap(),
        "--diagnostic",
        mutation[0],
        mutation[1],
        mutation[2],
        mutation[3],
        mutation[4],
        "diagnostic-1",
    ]);
    assert!(diagnostic.status.success());
    assert_eq!(output_json(&diagnostic)["code"], "diagnostics_exported");

    let list = run(&["list", "--workspace", project.to_str().unwrap()]);
    assert!(list.status.success());
    assert_eq!(output_json(&list)["code"], "project_list");
    let info = run(&[
        "workspace",
        "info",
        "--workspace",
        project.to_str().unwrap(),
    ]);
    assert!(info.status.success());
    assert_eq!(output_json(&info)["code"], "workspace_info");
}
