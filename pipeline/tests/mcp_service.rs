use std::fs;
use std::path::Path;
use std::sync::{Arc, Barrier};
use std::time::Duration;

use pipeline::application::{self, AppResult};
use pipeline::mcp::{self, HvpService};
use pipeline::runtime::RuntimeAuthority;
use rmcp::{ServiceExt, model::CallToolRequestParams, transport::TokioChildProcess};
use serde_json::{Value, json};
use tempfile::tempdir;

const TEST_CHANNEL: &str = "UCaaaaaaaaaaaaaaaaaaaaaa";

fn arguments(value: Value) -> serde_json::Map<String, Value> {
    value.as_object().unwrap().clone()
}

fn structured(result: rmcp::model::CallToolResult) -> AppResult {
    serde_json::from_value(result.structured_content.unwrap()).unwrap()
}

#[test]
fn mcp_publish_inputs_reject_all_legacy_caller_authority() {
    let prepare = json!({
        "schema_version": 1,
        "project_root": "/tmp/project",
        "override_reason": "human-reviewed warning exception",
    });
    assert!(serde_json::from_value::<mcp::PreparePublishApprovalInput>(prepare.clone()).is_ok());
    for legacy_field in [
        "approved_by",
        "caller_identity",
        "channel_id",
        "attestation_ref",
        "owner",
        "lease_id",
        "idempotency_key",
    ] {
        let mut legacy = prepare.clone();
        legacy
            .as_object_mut()
            .unwrap()
            .insert(legacy_field.to_owned(), json!("legacy-authority"));
        assert!(
            serde_json::from_value::<mcp::PreparePublishApprovalInput>(legacy).is_err(),
            "prepare approval accepted legacy field {legacy_field}"
        );
    }

    let approval = json!({
        "schema_version": 1,
        "project_root": "/tmp/project",
        "owner": "agent",
        "lease_id": "00000000-0000-4000-8000-000000000001",
        "attestation_ref": "approval-attestation",
        "idempotency_key": "approval-once",
    });
    assert!(serde_json::from_value::<mcp::ApprovePublishInput>(approval.clone()).is_ok());
    let mut missing_attestation = approval.clone();
    missing_attestation
        .as_object_mut()
        .unwrap()
        .remove("attestation_ref");
    assert!(serde_json::from_value::<mcp::ApprovePublishInput>(missing_attestation).is_err());
    let mut approval_with_override = approval.clone();
    approval_with_override.as_object_mut().unwrap().insert(
        "override_reason".to_owned(),
        json!("human-recorded exception"),
    );
    assert!(serde_json::from_value::<mcp::ApprovePublishInput>(approval_with_override).is_ok());
    for legacy_field in [
        "approved_by",
        "channel_id",
        "reapprove",
        "visibility",
        "credential-path",
        "credential_path",
        "upload_credential_file",
    ] {
        let mut legacy = approval.clone();
        legacy
            .as_object_mut()
            .unwrap()
            .insert(legacy_field.to_owned(), json!("legacy-authority"));
        assert!(
            serde_json::from_value::<mcp::ApprovePublishInput>(legacy).is_err(),
            "approval accepted legacy field {legacy_field}"
        );
    }

    let publish = json!({
        "schema_version": 1,
        "project_root": "/tmp/project",
        "owner": "agent",
        "lease_id": "00000000-0000-4000-8000-000000000001",
        "idempotency_key": "upload-once",
    });
    assert!(serde_json::from_value::<mcp::PublishInput>(publish.clone()).is_ok());
    for legacy_field in [
        "approved_by",
        "channel_id",
        "reapprove",
        "visibility",
        "credential-path",
        "credential_path",
        "upload_credential_file",
    ] {
        let mut legacy = publish.clone();
        legacy
            .as_object_mut()
            .unwrap()
            .insert(legacy_field.to_owned(), json!("legacy-authority"));
        assert!(
            serde_json::from_value::<mcp::PublishInput>(legacy).is_err(),
            "publish accepted legacy field {legacy_field}"
        );
    }

    let thumbnail = serde_json::from_value::<mcp::ReplaceThumbnailInput>(json!({
        "schema_version": 1,
        "project_root": "/tmp/project",
        "owner": "agent",
        "lease_id": "00000000-0000-4000-8000-000000000001",
        "upload_credential_file": "/tmp/credential",
        "updated_by": "harvey",
        "idempotency_key": "thumbnail-once",
    }));
    assert!(thumbnail.is_err());

    let reconcile = json!({
        "schema_version": 1,
        "project_root": "/tmp/project",
        "owner": "agent",
        "lease_id": "00000000-0000-4000-8000-000000000001",
        "idempotency_key": "reconcile-once",
    });
    assert!(serde_json::from_value::<mcp::ReconcileUploadInput>(reconcile.clone()).is_ok());
    let mut reconcile_override = reconcile.clone();
    reconcile_override.as_object_mut().unwrap().insert(
        "override_attestation_ref".to_owned(),
        json!("attestation:restart"),
    );
    assert!(serde_json::from_value::<mcp::ReconcileUploadInput>(reconcile_override).is_ok());
    for legacy_field in [
        "video_id",
        "channel_id",
        "visibility",
        "credential_path",
        "upload_credential_file",
    ] {
        let mut legacy = reconcile.clone();
        legacy
            .as_object_mut()
            .unwrap()
            .insert(legacy_field.to_owned(), json!("legacy-authority"));
        assert!(
            serde_json::from_value::<mcp::ReconcileUploadInput>(legacy).is_err(),
            "reconcile accepted legacy field {legacy_field}"
        );
    }
}

#[test]
fn review_resolution_input_is_digest_bound_and_accepts_no_storage_authority() {
    let input = json!({
        "schema_version": 1,
        "project_root": "/workspace/projects/demo",
        "owner": "agent",
        "lease_id": "00000000-0000-4000-8000-000000000001",
        "comment_id": "00000000-0000-4000-8000-000000000002",
        "status": "resolved",
        "expected_package_id": "a".repeat(64),
        "expected_asset_sha256": "b".repeat(64),
        "idempotency_key": "resolve-once",
    });
    assert!(serde_json::from_value::<mcp::ReviewResolveInput>(input.clone()).is_ok());
    for forbidden in ["storage_root", "executable", "source_path", "approval"] {
        let mut hostile = input.clone();
        hostile[forbidden] = json!("caller-controlled");
        assert!(
            serde_json::from_value::<mcp::ReviewResolveInput>(hostile).is_err(),
            "review resolution accepted {forbidden}"
        );
    }
}

/// A server whose runtime state root is the test's own, so the upload hold and
/// the capability floor are never read from -- or answered from -- the
/// operator's promoted runtime.
fn service(repo_root: &Path, state_root: &Path) -> HvpService {
    let runtime =
        RuntimeAuthority::test_promoted(repo_root, state_root.to_path_buf(), mcp::tool_surface())
            .unwrap();
    HvpService::with_authority(repo_root, runtime)
}

/// The compatibility block `create` writes. A project without one is not
/// evaluated at all, so an MCP fixture that skips it is testing the refusal.
fn write_compatible_contract(project: &Path) {
    fs::write(
        project.join("project-contract.json"),
        serde_json::to_vec(&json!({
            "schema": "haru.project_contract.v1",
            "runtime_contract": {
                "schema": "haru.project_runtime_contract.v1",
                "runtime": "haru.runtime.v1",
                "evaluator": "haru.evaluator.v1",
                "artifact": "haru.artifact.v1",
            },
            "lane_contract": "HVP_TODO_REPLACE_ME",
            "production_profile": "HVP_TODO_REPLACE_ME",
            "publish_target": {
                "youtube_channel_id": TEST_CHANNEL,
            },
        }))
        .unwrap(),
    )
    .unwrap();
}
#[cfg(unix)]
#[tokio::test]
async fn opaque_lease_lifecycle_never_exposes_the_capability() {
    use std::os::unix::fs::PermissionsExt;

    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("project");
    let state = directory.path().join("runtime-state");
    fs::create_dir(&repo).unwrap();
    fs::create_dir(&project).unwrap();
    write_compatible_contract(&project);

    let (server_transport, client_transport) = tokio::io::duplex(32 * 1024);
    let server = tokio::spawn(async move {
        service(&repo, &state)
            .serve(server_transport)
            .await
            .unwrap()
            .waiting()
            .await
            .unwrap();
    });
    let client = ().serve(client_transport).await.unwrap();

    let status_arguments = arguments(json!({
        "schema_version": 1,
        "project_root": project,
    }));
    let absent = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_status").with_arguments(status_arguments.clone()),
            )
            .await
            .unwrap(),
    );
    assert_eq!(absent.code, "lease_absent");

    let claim_arguments = arguments(json!({
        "schema_version": 1,
        "project_root": project,
        "owner": "codex",
        "ttl_seconds": 60,
        "idempotency_key": "claim-once",
    }));
    let claimed = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_claim").with_arguments(claim_arguments.clone()),
            )
            .await
            .unwrap(),
    );
    let replayed = structured(
        client
            .call_tool(CallToolRequestParams::new("lease_claim").with_arguments(claim_arguments))
            .await
            .unwrap(),
    );
    assert_eq!(claimed, replayed);
    assert_eq!(claimed.code, "lease_claimed");
    let lease_id = claimed.data.as_ref().unwrap()["lease_id"]
        .as_str()
        .unwrap()
        .to_owned();
    let private_root = directory.path().join("runtime-state/private-leases");
    let private_project = fs::read_dir(&private_root)
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let private_file = fs::read_dir(private_project)
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let private_value: Value = serde_json::from_slice(&fs::read(private_file).unwrap()).unwrap();
    let raw_capability = private_value["capability"].as_str().unwrap().to_owned();
    assert!(!raw_capability.is_empty());

    let held = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_claim").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "hermes",
                    "ttl_seconds": 60,
                    "idempotency_key": "other-owner",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(held.code, "lease_held");

    let wrong_owner = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_renew").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "hermes",
                    "lease_id": lease_id,
                    "ttl_seconds": 120,
                    "idempotency_key": "wrong-owner-renew",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(
        (wrong_owner.outcome.as_str(), wrong_owner.code.as_str()),
        ("blocked", "lease_invalid")
    );

    let renewed = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_renew").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "codex",
                    "lease_id": lease_id,
                    "ttl_seconds": 120,
                    "idempotency_key": "renew-once",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(renewed.code, "lease_renewed");

    let active = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_status").with_arguments(status_arguments.clone()),
            )
            .await
            .unwrap(),
    );
    assert_eq!(active.code, "lease_active");
    assert_eq!(active.data.as_ref().unwrap()["lease_id"], lease_id);

    let released = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_release").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "codex",
                    "lease_id": lease_id,
                    "idempotency_key": "release-once",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(released.code, "lease_released");
    let after = structured(
        client
            .call_tool(CallToolRequestParams::new("lease_status").with_arguments(status_arguments))
            .await
            .unwrap(),
    );
    assert_eq!(after.code, "lease_absent");
    let reacquired = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_claim").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "hermes",
                    "ttl_seconds": 60,
                    "idempotency_key": "claim-after-release",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(reacquired.code, "lease_claimed");
    assert_ne!(reacquired.data.as_ref().unwrap()["lease_id"], lease_id);
    assert_eq!(reacquired.data.as_ref().unwrap()["generation"], 2);
    let reacquired_private = fs::read_dir(
        fs::read_dir(&private_root)
            .unwrap()
            .next()
            .unwrap()
            .unwrap()
            .path(),
    )
    .unwrap()
    .next()
    .unwrap()
    .unwrap()
    .path();
    let reacquired_value: Value =
        serde_json::from_slice(&fs::read(reacquired_private).unwrap()).unwrap();
    let reacquired_capability = reacquired_value["capability"].as_str().unwrap().to_owned();

    for result in [
        claimed,
        held,
        wrong_owner,
        renewed,
        active,
        released,
        after,
        reacquired,
    ] {
        let encoded = serde_json::to_string(&result).unwrap();
        assert!(
            !encoded.contains(&raw_capability) && !encoded.contains(&reacquired_capability),
            "MCP result exposed a raw lease capability: {result:?}"
        );
    }

    assert_eq!(
        fs::metadata(&private_root).unwrap().permissions().mode() & 0o777,
        0o700
    );

    client.cancel().await.unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn expired_opaque_lease_can_be_taken_over_and_stale_owner_stays_blocked() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("project");
    let state = directory.path().join("runtime-state");
    fs::create_dir(&repo).unwrap();
    fs::create_dir(&project).unwrap();
    write_compatible_contract(&project);

    let (server_transport, client_transport) = tokio::io::duplex(32 * 1024);
    let server = tokio::spawn(async move {
        service(&repo, &state)
            .serve(server_transport)
            .await
            .unwrap()
            .waiting()
            .await
            .unwrap();
    });
    let client = ().serve(client_transport).await.unwrap();

    let first = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_claim").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "codex",
                    "ttl_seconds": 1,
                    "idempotency_key": "short-lease",
                }))),
            )
            .await
            .unwrap(),
    );
    let first_lease_id = first.data.as_ref().unwrap()["lease_id"]
        .as_str()
        .unwrap()
        .to_owned();
    let first_generation = first.data.as_ref().unwrap()["generation"].as_u64().unwrap();

    tokio::time::sleep(Duration::from_millis(1_100)).await;

    let takeover = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_claim").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "hermes",
                    "ttl_seconds": 60,
                    "idempotency_key": "take-over",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(takeover.code, "lease_claimed");
    assert_ne!(takeover.data.as_ref().unwrap()["lease_id"], first_lease_id);
    assert_eq!(
        takeover.data.as_ref().unwrap()["generation"],
        first_generation + 1
    );

    let stale = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_renew").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "codex",
                    "lease_id": first_lease_id,
                    "ttl_seconds": 60,
                    "idempotency_key": "stale-renew",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(
        (stale.outcome.as_str(), stale.code.as_str()),
        ("blocked", "lease_invalid")
    );

    client.cancel().await.unwrap();
    server.await.unwrap();
}

#[test]
fn concurrent_shell_callers_cannot_overwrite_a_selection() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("project");
    fs::create_dir(&project).unwrap();
    let barrier = Arc::new(Barrier::new(3));
    let callers: Vec<_> = ["candidate-1", "candidate-2"]
        .into_iter()
        .map(|candidate| {
            let project = project.clone();
            let barrier = barrier.clone();
            std::thread::spawn(move || {
                barrier.wait();
                application::record_selection(
                    &project,
                    "topic-20260729",
                    candidate,
                    "harvey",
                    1785254400,
                )
            })
        })
        .collect();
    barrier.wait();
    let results: Vec<_> = callers
        .into_iter()
        .map(|caller| caller.join().unwrap())
        .collect();

    assert_eq!(
        results
            .iter()
            .filter(|result| result.code == "selection_recorded")
            .count(),
        1
    );
    assert_eq!(
        results
            .iter()
            .filter(|result| result.code == "selection_conflict")
            .count(),
        1
    );
}

#[cfg(unix)]
#[tokio::test]
async fn shell_and_mcp_share_state_and_duplicate_mutation_is_not_reexecuted() {
    use std::os::unix::fs::PermissionsExt;

    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let projects = directory.path().join("projects");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir(&projects).unwrap();
    let script = repo.join("scripts/verify-project");
    fs::write(
        &script,
        "#!/bin/sh\nset -euC\nproject=${1##*/}\nselection=null\nif [ \"$project\" = mcp-created ]; then selection='{\"candidate_id\":\"candidate-2\"}'; fi\nif [ -f \"$1/.hvp/mark-run-once\" ]; then : > \"$1/.hvp/run-once\"; rm \"$1/.hvp/mark-run-once\"; fi\nprintf '{\"schema\":\"haru.pipeline_status.v1\",\"project\":\"%s\",\"overall_status\":\"ready_for_human_upload_approval\",\"blockers\":[],\"blocker_details\":[],\"required_stages\":[],\"stages\":{},\"selection\":%s}\\n' \"$project\" \"$selection\"\n",
    )
    .unwrap();
    fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();

    let shell_created = application::create(&projects, "shell-created");
    assert_eq!(shell_created.code, "project_created");
    let project = projects.join("shell-created").canonicalize().unwrap();

    let state = directory.path().join("runtime-state");
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let first_repo = repo.clone();
    let first_state = state.clone();
    let server = tokio::spawn(async move {
        service(&first_repo, &first_state)
            .serve(server_transport)
            .await
            .unwrap()
            .waiting()
            .await
            .unwrap();
    });
    let client = ().serve(client_transport).await.unwrap();

    assert_eq!(client.peer_info().unwrap().server_info.name, "video-studio");
    let tools = client.list_all_tools().await.unwrap();
    let names: Vec<_> = tools.iter().map(|tool| tool.name.as_ref()).collect();
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
    assert!(tools.iter().all(|tool| tool.output_schema.is_some()));
    let output_schema = serde_json::to_value(tools[0].output_schema.as_ref().unwrap()).unwrap();
    assert_eq!(
        output_schema.pointer("/properties/data/type"),
        Some(&json!([
            "array", "boolean", "null", "number", "object", "string"
        ]))
    );

    let create_arguments = arguments(json!({
        "schema_version": 1,
        "projects_root": projects,
        "project": "mcp-created",
        "idempotency_key": "create-once",
    }));
    let created = structured(
        client
            .call_tool(
                CallToolRequestParams::new("create").with_arguments(create_arguments.clone()),
            )
            .await
            .unwrap(),
    );
    let duplicate_create = structured(
        client
            .call_tool(CallToolRequestParams::new("create").with_arguments(create_arguments))
            .await
            .unwrap(),
    );
    assert_eq!(created.code, "project_created");
    assert_eq!(duplicate_create, created);
    let conflict = structured(
        client
            .call_tool(
                CallToolRequestParams::new("create").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "projects_root": projects,
                    "project": "different-project",
                    "idempotency_key": "create-once",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(conflict.code, "idempotency_conflict");
    let selected = structured(
        client
            .call_tool(
                CallToolRequestParams::new("select").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "projects_root": projects,
                    "project": "mcp-created",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(selected.code, "project_selected");
    let selection_arguments = arguments(json!({
        "schema_version": 1,
        "project_root": projects.join("mcp-created"),
        "cron_run_id": "topic-20260729",
        "candidate_id": "candidate-2",
        "chosen_by": "harvey",
        "chosen_at": 1785254400_u64,
        "idempotency_key": "selection-once",
    }));
    let selection = structured(
        client
            .call_tool(
                CallToolRequestParams::new("record_selection")
                    .with_arguments(selection_arguments.clone()),
            )
            .await
            .unwrap(),
    );
    let duplicate_selection = structured(
        client
            .call_tool(
                CallToolRequestParams::new("record_selection")
                    .with_arguments(selection_arguments.clone()),
            )
            .await
            .unwrap(),
    );
    assert_eq!(selection.code, "selection_recorded");
    assert_eq!(duplicate_selection, selection);
    assert_eq!(
        selection.data.as_ref().unwrap()["project_slug"],
        "mcp-created"
    );
    let selected_status = structured(
        client
            .call_tool(
                CallToolRequestParams::new("status").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": projects.join("mcp-created"),
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(
        selected_status.data.as_ref().unwrap()["selection"]["candidate_id"],
        "candidate-2"
    );
    let conflict = structured(
        client
            .call_tool(
                CallToolRequestParams::new("record_selection").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": projects.join("mcp-created"),
                    "cron_run_id": "topic-20260729",
                    "candidate_id": "candidate-3",
                    "chosen_by": "harvey",
                    "chosen_at": 1785254401_u64,
                    "idempotency_key": "conflicting-selection",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(conflict.code, "selection_conflict");

    let mcp_status = structured(
        client
            .call_tool(
                CallToolRequestParams::new("status").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                }))),
            )
            .await
            .unwrap(),
    );
    let shell_status = application::status(
        &projects.join("shell-created"),
        &repo,
        &mut application::ProcessExecutor,
    );
    assert_eq!(mcp_status.code, shell_status.code);
    let mut project_data = mcp_status.data.clone().unwrap();
    let hold = project_data
        .as_object_mut()
        .unwrap()
        .remove("upload_hold")
        .unwrap();
    assert_eq!(hold["held"], true);
    // Runtime operator state is an MCP addition; canonical project state is shared.
    assert_eq!(Some(project_data), shell_status.data);
    let claim_arguments = arguments(json!({
        "schema_version": 1,
        "project_root": projects.join("shell-created"),
        "owner": "codex",
        "ttl_seconds": 60,
        "idempotency_key": "claim-shell-created",
    }));
    let claimed = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_claim").with_arguments(claim_arguments.clone()),
            )
            .await
            .unwrap(),
    );
    let duplicate_claim = structured(
        client
            .call_tool(CallToolRequestParams::new("lease_claim").with_arguments(claim_arguments))
            .await
            .unwrap(),
    );
    assert_eq!(claimed, duplicate_claim);
    let lease_id = claimed.data.as_ref().unwrap()["lease_id"]
        .as_str()
        .unwrap()
        .to_owned();
    assert!(!serde_json::to_string(&claimed).unwrap().contains("token"));

    fs::write(project.join(".hvp/mark-run-once"), "").unwrap();
    let run_arguments = arguments(json!({
        "schema_version": 1,
        "project_root": projects.join("shell-created"),
        "owner": "codex",
        "lease_id": lease_id,
        "runner": "verify-project",
        "idempotency_key": "verify-once",
    }));
    let first = structured(
        client
            .call_tool(CallToolRequestParams::new("run_next").with_arguments(run_arguments.clone()))
            .await
            .unwrap(),
    );
    let duplicate = structured(
        client
            .call_tool(CallToolRequestParams::new("run_next").with_arguments(run_arguments))
            .await
            .unwrap(),
    );
    assert_eq!(first.outcome, "ok");
    assert_eq!(duplicate, first);

    let artifacts = structured(
        client
            .call_tool(
                CallToolRequestParams::new("artifact_index").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": projects.join("shell-created"),
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(artifacts.code, "artifact_index");
    assert!(artifacts.data.unwrap()["artifacts"].is_array());

    client.cancel().await.unwrap();
    server.await.unwrap();

    let (server_transport, client_transport) = tokio::io::duplex(16 * 1024);
    let restarted_server = tokio::spawn(async move {
        service(&repo, &state)
            .serve(server_transport)
            .await
            .unwrap()
            .waiting()
            .await
            .unwrap();
    });
    let restarted_client = ().serve(client_transport).await.unwrap();
    let resumed_selection = structured(
        restarted_client
            .call_tool(
                CallToolRequestParams::new("record_selection").with_arguments(selection_arguments),
            )
            .await
            .unwrap(),
    );
    assert_eq!(resumed_selection, selection);
    restarted_client.cancel().await.unwrap();
    restarted_server.await.unwrap();
}

/// Blocker 5: a stored mutation receipt belongs to the runtime that executed
/// it. Replaying its key on a different runtime is refused, never answered with
/// the old result wearing the current runtime's stamp.
#[cfg(unix)]
#[tokio::test]
async fn a_replayed_mutation_receipt_is_never_reattributed_to_another_runtime() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let projects = directory.path().join("projects");
    fs::create_dir_all(&repo).unwrap();
    fs::create_dir(&projects).unwrap();
    let state = directory.path().join("runtime-state");

    let served = service(&repo, &state);
    let runtime_id = served.runtime().identity().runtime_id.clone();
    let binary_sha256 = served.runtime().binary_sha256().to_owned();

    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let server = tokio::spawn(async move {
        served
            .serve(server_transport)
            .await
            .unwrap()
            .waiting()
            .await
            .unwrap();
    });
    let client = ().serve(client_transport).await.unwrap();

    let call = arguments(json!({
        "schema_version": 1,
        "projects_root": projects,
        "project": "receipt-bound",
        "idempotency_key": "bind-once",
    }));
    let created = structured(
        client
            .call_tool(CallToolRequestParams::new("create").with_arguments(call.clone()))
            .await
            .unwrap(),
    );
    assert_eq!(created.code, "project_created");

    // The receipt names the runtime that executed it, and the key's input
    // binding covers that identity rather than the inputs alone.
    let receipt_path = projects.join(".hvp/mcp-operations/bind-once.json");
    let receipt: Value = serde_json::from_slice(&fs::read(&receipt_path).unwrap()).unwrap();
    assert_eq!(receipt["schema_version"], json!(2));
    assert_eq!(receipt["runtime_id"], json!(runtime_id));
    assert_eq!(receipt["binary_sha256"], json!(binary_sha256));
    assert_eq!(receipt["operation"], json!("create"));
    assert_eq!(receipt["result"]["code"], json!("project_created"));

    // Same runtime, same key, same input: the stored result, not re-executed.
    let duplicate = structured(
        client
            .call_tool(CallToolRequestParams::new("create").with_arguments(call.clone()))
            .await
            .unwrap(),
    );
    assert_eq!(duplicate, created);

    let foreign = format!("sha256:{}", "9".repeat(64));
    for field in ["runtime_id", "binary_sha256"] {
        let mut stale = receipt.clone();
        stale[field] = json!(foreign);
        fs::write(&receipt_path, serde_json::to_vec(&stale).unwrap()).unwrap();

        let replayed = structured(
            client
                .call_tool(CallToolRequestParams::new("create").with_arguments(call.clone()))
                .await
                .unwrap(),
        );
        assert_eq!(replayed.outcome, "blocked", "{field}");
        assert_eq!(replayed.code, "idempotency_runtime_mismatch", "{field}");
        // Nothing of the other runtime's result came back.
        assert_ne!(replayed.code, created.code, "{field}");
        assert_ne!(replayed.data, created.data, "{field}");

        let data = replayed.data.as_ref().unwrap();
        assert_eq!(data["operation"], json!("create"), "{field}");
        assert_eq!(
            data[&format!("recorded_{field}")],
            json!(foreign),
            "{field}"
        );
        assert_eq!(data["current_runtime_id"], json!(runtime_id), "{field}");
        assert_eq!(
            data["current_binary_sha256"],
            json!(binary_sha256),
            "{field}"
        );
        // The refusal is stamped with the runtime that refused it, which is the
        // whole point: no result is ever labelled with a runtime that did not
        // produce it.
        assert_eq!(
            replayed.runtime.as_ref().unwrap()["runtime_id"],
            json!(runtime_id),
            "{field}"
        );
    }

    // A receipt from before receipts carried a runtime cannot be shown to
    // describe this request either, so its key is refused rather than replayed.
    let mut legacy = receipt.clone();
    legacy["schema_version"] = json!(1);
    let legacy_object = legacy.as_object_mut().unwrap();
    legacy_object.remove("runtime_id");
    legacy_object.remove("binary_sha256");
    fs::write(&receipt_path, serde_json::to_vec(&legacy).unwrap()).unwrap();
    let legacy_replay = structured(
        client
            .call_tool(CallToolRequestParams::new("create").with_arguments(call.clone()))
            .await
            .unwrap(),
    );
    assert_eq!(legacy_replay.outcome, "error");
    assert_eq!(legacy_replay.code, "idempotency_conflict");

    // Restoring the receipt this runtime actually wrote restores the duplicate
    // replay: binding attribution did not break resuming your own call.
    fs::write(&receipt_path, serde_json::to_vec(&receipt).unwrap()).unwrap();
    let restored = structured(
        client
            .call_tool(CallToolRequestParams::new("create").with_arguments(call))
            .await
            .unwrap(),
    );
    assert_eq!(restored, created);

    client.cancel().await.unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn versioned_inputs_fail_closed_without_touching_the_project() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("project");
    fs::create_dir(&repo).unwrap();
    fs::create_dir(&project).unwrap();

    let state = directory.path().join("runtime-state");
    let (server_transport, client_transport) = tokio::io::duplex(16 * 1024);
    let server = tokio::spawn(async move {
        service(&repo, &state)
            .serve(server_transport)
            .await
            .unwrap()
            .waiting()
            .await
            .unwrap();
    });
    let client = ().serve(client_transport).await.unwrap();
    let result = structured(
        client
            .call_tool(
                CallToolRequestParams::new("status").with_arguments(arguments(json!({
                    "schema_version": 2,
                    "project_root": project,
                }))),
            )
            .await
            .unwrap(),
    );

    let runtime = result.runtime.clone();
    assert_eq!(
        AppResult {
            runtime: None,
            ..result
        },
        AppResult::invalid_input()
    );
    // Even a refusal names the runtime that refused: that is how a client
    // notices two of its servers are not the same bytes.
    assert_eq!(
        runtime.as_ref().unwrap()["schema"],
        "haru.runtime_identity.v1"
    );
    assert!(!directory.path().join("project/.hvp").exists());

    client.cancel().await.unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn stdio_binary_serves_the_same_typed_tools_and_names_its_runtime() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("project");
    fs::create_dir(&project).unwrap();
    write_compatible_contract(&project);
    let mut command = tokio::process::Command::new(env!("CARGO_BIN_EXE_hvp-mcp"));
    command.env("HVP_RUNTIME_STATE", directory.path().join("runtime-state"));
    let transport = TokioChildProcess::new(command).unwrap();
    let client = ().serve(transport).await.unwrap();

    let tools = client.list_all_tools().await.unwrap();
    assert_eq!(tools.len(), 28);
    assert!(
        tools
            .iter()
            .any(|tool| tool.name == "prepare_publish_approval")
    );

    let result = structured(
        client
            .call_tool(
                CallToolRequestParams::new("status").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(result.code, "status");
    assert_eq!(result.data.as_ref().unwrap()["upload_hold"]["held"], true);
    assert_eq!(
        result.data.as_ref().unwrap()["upload_hold"]["reason"],
        "upload_hold_default"
    );

    // The served surface and the surface bound into the identity are the same
    // list, computed from the router rather than described next to it.
    let runtime = result.runtime.as_ref().unwrap();
    let served: Vec<_> = tools.iter().map(|tool| tool.name.as_ref()).collect();
    let declared: Vec<_> = runtime["tool_surface"]
        .as_array()
        .unwrap()
        .iter()
        .map(|name| name.as_str().unwrap())
        .collect();
    assert_eq!(served, declared);
    assert_eq!(runtime["tool_surface_digest"], mcp::tool_surface().digest);
    assert_eq!(runtime["verified"], true);
    assert_eq!(runtime["promoted"], false);
    assert!(
        runtime["runtime_id"]
            .as_str()
            .unwrap()
            .starts_with("sha256:")
    );
    let mutation = structured(
        client
            .call_tool(
                CallToolRequestParams::new("create").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "projects_root": directory.path(),
                    "project": "must-not-exist",
                    "idempotency_key": "unpromoted-create",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(
        (mutation.outcome.as_str(), mutation.code.as_str()),
        ("blocked", "runtime_unpromoted")
    );
    assert!(!directory.path().join("must-not-exist").exists());

    client.cancel().await.unwrap();
}

#[tokio::test]
async fn an_incompatible_project_is_refused_before_any_gate_is_evaluated() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("project");
    let evidence = directory.path().join("verifier-ran");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir(&project).unwrap();
    // A verifier that leaves a trace. If the short-circuit is real, this file
    // never appears: the project is refused before an executor is spawned.
    fs::write(
        repo.join("scripts/verify-project"),
        format!(
            "#!/bin/sh\n: > {}\nprintf '{{}}\\n'\n",
            evidence.to_str().unwrap()
        ),
    )
    .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(
            repo.join("scripts/verify-project"),
            fs::Permissions::from_mode(0o755),
        )
        .unwrap();
    }

    let state = directory.path().join("runtime-state");
    let (server_transport, client_transport) = tokio::io::duplex(16 * 1024);
    let server = tokio::spawn(async move {
        service(&repo, &state)
            .serve(server_transport)
            .await
            .unwrap()
            .waiting()
            .await
            .unwrap();
    });
    let client = ().serve(client_transport).await.unwrap();

    for tool in ["status", "artifact_index", "verify"] {
        let input = arguments(json!({
            "schema_version": 1,
            "project_root": project,
        }));
        let result = structured(
            client
                .call_tool(CallToolRequestParams::new(tool).with_arguments(input))
                .await
                .unwrap(),
        );
        assert_eq!(
            (result.outcome.as_str(), result.code.as_str()),
            ("blocked", "runtime_incompatible"),
            "{tool} evaluated a project this runtime cannot serve"
        );
        assert_eq!(
            result.data.as_ref().unwrap()["reason"],
            "missing_runtime_contract"
        );
    }

    let mutation = structured(
        client
            .call_tool(
                CallToolRequestParams::new("record_selection").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "cron_run_id": "topic-20260812",
                    "candidate_id": "candidate-1",
                    "chosen_by": "harvey",
                    "chosen_at": 1785254400_u64,
                    "idempotency_key": "selection-once",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(mutation.code, "runtime_incompatible");

    assert!(
        !evidence.exists(),
        "the verifier ran for an unservable project"
    );
    assert!(
        !project.join(".hvp").exists(),
        "a refused call still wrote state"
    );

    // The same project becomes servable the moment it declares a supported
    // contract -- nothing else about it changed.
    write_compatible_contract(&project);
    let served = structured(
        client
            .call_tool(
                CallToolRequestParams::new("verify").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                }))),
            )
            .await
            .unwrap(),
    );
    assert_ne!(served.code, "runtime_incompatible");
    assert!(evidence.exists());

    client.cancel().await.unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn unconfigured_public_policy_blocks_before_any_credential_or_lease_is_read() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("project");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir(&project).unwrap();
    write_compatible_contract(&project);
    let state = directory.path().join("runtime-state");

    let (server_transport, client_transport) = tokio::io::duplex(16 * 1024);
    let held_state = state.clone();
    let server = tokio::spawn(async move {
        service(&repo, &held_state)
            .serve(server_transport)
            .await
            .unwrap()
            .waiting()
            .await
            .unwrap();
    });
    let client = ().serve(client_transport).await.unwrap();

    // The lease file does not exist. Missing public publisher configuration is
    // the earliest refusal: no lease, credential, or uploader is consulted.
    let publish = structured(
        client
            .call_tool(
                CallToolRequestParams::new("publish").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "agent",
                    "lease_id": "00000000-0000-4000-8000-000000000001",

                    "idempotency_key": "upload-once",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(
        (publish.outcome.as_str(), publish.code.as_str()),
        ("blocked", "publishing_unconfigured")
    );
    assert_eq!(
        publish.data.as_ref().unwrap()["schema"],
        "video_studio.publishing_unconfigured.v1"
    );
    assert_eq!(
        publish.data.as_ref().unwrap()["youtube_channel_configured"],
        false
    );
    assert_eq!(
        publish.data.as_ref().unwrap()["approval_signer_configured"],
        false
    );
    let remedy = publish.data.as_ref().unwrap()["remedy"].as_str().unwrap();
    assert!(remedy.contains("Configure and rebuild a trusted publishing runtime"));

    let thumbnail = structured(
        client
            .call_tool(
                CallToolRequestParams::new("replace_thumbnail").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "agent",
                    "lease_id": "00000000-0000-4000-8000-000000000001",
                    "updated_by": "harvey",
                    "idempotency_key": "thumbnail-once",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(thumbnail.code, "publishing_unconfigured");
    // A refused call burns no idempotency key and writes no receipt.
    assert!(!project.join(".hvp/mcp-operations").exists());

    let reconcile = structured(
        client
            .call_tool(
                CallToolRequestParams::new("reconcile_upload").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "agent",
                    "lease_id": "00000000-0000-4000-8000-000000000001",
                    "idempotency_key": "reconcile-once",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_ne!(reconcile.code, "production_upload_held");

    // Lifting the operational hold cannot manufacture missing channel/signer
    // configuration in the public build.
    pipeline::runtime::set_upload_hold(&state, false, "wave3 verified", "harvey").unwrap();
    let lifted = structured(
        client
            .call_tool(
                CallToolRequestParams::new("publish").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "agent",
                    "lease_id": "00000000-0000-4000-8000-000000000001",

                    "idempotency_key": "upload-once",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(lifted.code, "publishing_unconfigured");

    client.cancel().await.unwrap();
    server.await.unwrap();
}

#[cfg(unix)]
#[tokio::test]
async fn promoted_mcp_call_runs_the_fixed_render_lane_to_final_mp4() {
    use std::os::unix::fs::PermissionsExt;
    use std::process::Command;

    let directory = tempdir().unwrap();
    let project = directory.path().join("project");
    let remotion = project.join("remotion");
    let tools = directory.path().join("tools");
    fs::create_dir_all(remotion.join("node_modules/.bin")).unwrap();
    fs::create_dir_all(project.join("output")).unwrap();
    fs::create_dir_all(project.join("audio")).unwrap();
    fs::create_dir_all(tools.join("video")).unwrap();
    fs::write(remotion.join("package.json"), "{}").unwrap();
    fs::write(remotion.join("package-lock.json"), "{}").unwrap();
    fs::write(remotion.join("remotion.config.ts"), "export default {};\n").unwrap();
    fs::create_dir_all(remotion.join("src")).unwrap();
    fs::write(
        remotion.join("src/index.ts"),
        "export const fixture = true;\n",
    )
    .unwrap();
    let remotion_binary = remotion.join("node_modules/.bin/remotion");
    fs::write(&remotion_binary, "#!/bin/sh\n").unwrap();
    fs::set_permissions(&remotion_binary, fs::Permissions::from_mode(0o755)).unwrap();
    let renderer = tools.join("video/render_and_verify.sh");
    fs::write(
        &renderer,
        r#"#!/bin/sh
case "$1" in
  --verify-only|--loudness-gate-only) exit 0 ;;
esac
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i color=c=black:s=160x90:r=30:d=1 \
  -f lavfi -i sine=frequency=440:sample_rate=48000:duration=1 \
  -filter:a volume=0.02 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$3"
"#,
    )
    .unwrap();
    fs::set_permissions(&renderer, fs::Permissions::from_mode(0o755)).unwrap();
    for (name, frequency) in [("bgm.wav", 220), ("reveal.wav", 880)] {
        assert!(
            Command::new("ffmpeg")
                .args([
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    &format!("sine=frequency={frequency}:sample_rate=48000:duration=1"),
                ])
                .arg(project.join("audio").join(name))
                .status()
                .unwrap()
                .success()
        );
    }
    fs::write(
        project.join("render_plan.json"),
        serde_json::to_vec(&json!({
            "schema": "haru.render_plan.v1",
            "engine": "remotion",
            "remotion_dir": "remotion",
            "composition": "HvpSmoke",
            "output": "output/final.mp4",
            "expected_duration": 1,
            "concurrency": 1,
            "skip_pronunciation_gate": true,
            "audio_mix": {
                "schema": "haru.audio_mix.v1",
                "background_music": {"path": "audio/bgm.wav", "gain_db": -30},
                "sound_effects": [
                    {"path": "audio/reveal.wav", "start_seconds": 0.25, "gain_db": -12}
                ]
            }
        }))
        .unwrap(),
    )
    .unwrap();
    let repo = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf();
    let trusted = Command::new("/usr/bin/python3")
        .args([
            "-S",
            "-c",
            "from pathlib import Path; import sys,template_trust; template_trust.trust(Path(sys.argv[1]))",
        ])
        .arg(&project)
        .env("PYTHONPATH", repo.join("tools"))
        .status()
        .unwrap();
    assert!(trusted.success());
    write_compatible_contract(&project);

    let state = directory.path().join("runtime-state");
    let (server_transport, client_transport) = tokio::io::duplex(16 * 1024);
    let server = tokio::spawn(async move {
        service(&repo, &state)
            .serve(server_transport)
            .await
            .unwrap()
            .waiting()
            .await
            .unwrap();
    });
    let client = ().serve(client_transport).await.unwrap();
    let claimed = structured(
        client
            .call_tool(
                CallToolRequestParams::new("lease_claim").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "agent",
                    "ttl_seconds": 60,
                    "idempotency_key": "render-lease",
                }))),
            )
            .await
            .unwrap(),
    );
    let lease_id = claimed.data.as_ref().unwrap()["lease_id"]
        .as_str()
        .unwrap()
        .to_owned();
    let mut result = structured(
        client
            .call_tool(
                CallToolRequestParams::new("run_next").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "agent",
                    "lease_id": lease_id,
                    "runner": "render-project",
                    "tools_root": tools,
                    "idempotency_key": "render-once"
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "gate_started")
    );
    for poll in 1..=100 {
        tokio::time::sleep(Duration::from_millis(100)).await;
        result = structured(
            client
                .call_tool(
                    CallToolRequestParams::new("run_next").with_arguments(arguments(json!({
                        "schema_version": 1,
                        "project_root": project,
                        "owner": "agent",
                        "lease_id": lease_id,
                        "runner": "render-project",
                        "tools_root": tools,
                        "idempotency_key": format!("render-poll-{poll}")
                    }))),
                )
                .await
                .unwrap(),
        );
        if result.code == "gate_completed" {
            break;
        }
        assert_eq!(
            (result.outcome.as_str(), result.code.as_str()),
            ("ok", "gate_pending")
        );
    }

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "gate_completed")
    );
    assert!(
        fs::metadata(project.join("output/final.mp4"))
            .unwrap()
            .len()
            > 0
    );
    let marker: Value =
        serde_json::from_slice(&fs::read(project.join("output/final.mp4.render-result")).unwrap())
            .unwrap();
    assert_eq!(marker["status"], "render_complete");
    assert_eq!(marker["mix"]["method"], "ffmpeg_loudnorm_two_pass");
    assert_eq!(marker["mix"]["audio_mix"]["schema"], "haru.audio_mix.v1");
    assert_eq!(
        marker["mix"]["audio_mix"]["sound_effects"][0]["path"],
        "audio/reveal.wav"
    );
    assert!(marker["loudness_lufs"].as_f64().unwrap() >= -15.0);
    assert!(marker["loudness_lufs"].as_f64().unwrap() <= -13.0);

    client.cancel().await.unwrap();
    server.await.unwrap();
}
