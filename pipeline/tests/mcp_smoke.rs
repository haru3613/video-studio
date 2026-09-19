//! The MCP surface of the render self-evaluation gate.
//!
//! Two things are proven here. The served schema exposes typed values only --
//! no executable, no tools root, no caller-chosen path -- and the dispatch that
//! sits behind it reports the state the project actually holds rather than the
//! state a caller asked for.

use std::fs;
use std::path::Path;

use pipeline::application::AppResult;
use pipeline::mcp::{self, HvpService};
use pipeline::runtime::RuntimeAuthority;
use rmcp::{ServiceExt, model::CallToolRequestParams};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tempfile::tempdir;

const SELF_EVAL_ROOT: &str = "quality-review/render-self-eval";
const TEST_CHANNEL: &str = "UCaaaaaaaaaaaaaaaaaaaaaa";

/// Every tool name this build serves. The self-eval gate is an action on
/// `visual_qa` and a runner on `run_next`, so it adds no self-eval tool.
/// Human publish confirmation has its own read-only preparation entry point.
const TOOL_SURFACE: [&str; 28] = [
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
];

fn arguments(value: Value) -> serde_json::Map<String, Value> {
    value.as_object().unwrap().clone()
}

fn structured(result: rmcp::model::CallToolResult) -> AppResult {
    serde_json::from_value(result.structured_content.unwrap()).unwrap()
}

fn service(repo_root: &Path, state_root: &Path) -> HvpService {
    let runtime =
        RuntimeAuthority::test_promoted(repo_root, state_root.to_path_buf(), mcp::tool_surface())
            .unwrap();
    HvpService::with_authority(repo_root, runtime)
}

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

fn property_names(schema: &Value, pointer: &str) -> Vec<String> {
    let mut names = schema
        .pointer(pointer)
        .unwrap_or_else(|| panic!("schema has no {pointer}"))
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect::<Vec<_>>();
    names.sort();
    names
}

/// Find the object shape that declares `marker`, wherever schemars chose to put
/// it, so the assertion survives a `$ref`/`$defs` layout change.
fn shape_declaring(schema: &Value, marker: &str) -> Vec<String> {
    fn walk(value: &Value, marker: &str, found: &mut Option<Vec<String>>) {
        match value {
            Value::Object(object) => {
                if let Some(Value::Object(properties)) = object.get("properties")
                    && properties.contains_key(marker)
                {
                    let mut names = properties.keys().cloned().collect::<Vec<_>>();
                    names.sort();
                    *found = Some(names);
                    return;
                }
                for nested in object.values() {
                    walk(nested, marker, found);
                    if found.is_some() {
                        return;
                    }
                }
            }
            Value::Array(values) => {
                for nested in values {
                    walk(nested, marker, found);
                    if found.is_some() {
                        return;
                    }
                }
            }
            _ => {}
        }
    }
    let mut found = None;
    walk(schema, marker, &mut found);
    found.unwrap_or_else(|| panic!("no shape declares {marker}"))
}

fn self_eval_ref(project: &Path, relative: &str, value: &Value) -> Value {
    let path = project.join(relative);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let bytes = serde_json::to_vec(value).unwrap();
    fs::write(&path, &bytes).unwrap();
    json!({
        "path": relative,
        "sha256": format!("{:x}", Sha256::digest(&bytes)),
        "bytes": bytes.len(),
    })
}

/// A pending projection with every ref it names actually on disk: the state a
/// clean first evaluation leaves behind, awaiting a reviewer.
fn write_pending_self_eval(project: &Path) -> Value {
    let identity = format!("{:x}", Sha256::digest(b"self-eval-attempt-1"));
    let boundary_policy = self_eval_ref(
        project,
        &format!("{SELF_EVAL_ROOT}/boundary-policy.json"),
        &json!({
            "schema": "haru.render_self_eval_boundary_policy.v1",
            "algorithm": "haru.render_self_eval_policy.v1",
        }),
    );
    let boundary_plan = self_eval_ref(
        project,
        &format!("{SELF_EVAL_ROOT}/boundary-plan.json"),
        &json!({
            "schema": "haru.render_self_eval_boundary_plan.v1",
            "attempt_identity": identity,
        }),
    );
    let evaluation = self_eval_ref(
        project,
        &format!("{SELF_EVAL_ROOT}/attempts/attempt-01/evaluation.json"),
        &json!({
            "schema": "haru.render_self_eval_attempt.v1",
            "attempt": 1,
            "attempt_identity": identity,
            "status": "clean",
        }),
    );
    let evidence_index = self_eval_ref(
        project,
        &format!("{SELF_EVAL_ROOT}/attempts/attempt-01/evidence-index.json"),
        &json!({
            "schema": "haru.render_self_eval_evidence_index.v1",
            "attempt_identity": identity,
            "total_available_bytes": 4096,
        }),
    );
    let projection = json!({
        "schema": "haru.render_self_eval.v1",
        "project": project.file_name().unwrap().to_str().unwrap(),
        "status": "needs_human",
        "verdict": "needs_human",
        "attempt": 1,
        "max_attempts": 3,
        "attempt_identity": identity,
        "inputs": [],
        "boundary_policy": boundary_policy,
        "boundary_plan": boundary_plan,
        "evaluation": evaluation,
        "evidence_index": evidence_index,
        "review": Value::Null,
        "outcome": Value::Null,
        "tool": {"algorithm": "haru.render_self_eval_policy.v1"},
        "findings": [],
        "remediation": {
            "allowed_repairs": [],
            "automatic_fix_applied": Value::Null,
            "required_action": "human_review",
        },
        "next_action": "await_human_review",
        "updated_at": "2026-08-14T00:00:00Z",
    });
    fs::write(
        project.join(SELF_EVAL_ROOT).join("render-self-eval.json"),
        serde_json::to_vec(&projection).unwrap(),
    )
    .unwrap();
    projection
}

/// A stand-in engine that records the exact argv it received, counts its own
/// invocations, and answers with the projection currently on disk.
fn write_self_eval_engine(repo: &Path) {
    #[cfg(unix)]
    use std::os::unix::fs::PermissionsExt;

    let script = repo.join("scripts/render-self-eval");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::write(
        &script,
        "#!/bin/sh\nset -eu\nproject=$2\nprintf 'x' >> \"$project/.hvp/self-eval-runs\"\n: > \"$project/.hvp/self-eval-argv\"\nfor argument in \"$@\"; do printf '%s\\n' \"$argument\" >> \"$project/.hvp/self-eval-argv\"; done\ncat \"$project/quality-review/render-self-eval/render-self-eval.json\"\n",
    )
    .unwrap();
    #[cfg(unix)]
    fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();
}

fn recorded_argv(project: &Path) -> Vec<String> {
    fs::read_to_string(project.join(".hvp/self-eval-argv"))
        .unwrap()
        .lines()
        .map(str::to_owned)
        .collect()
}

fn engine_runs(project: &Path) -> usize {
    fs::read_to_string(project.join(".hvp/self-eval-runs"))
        .map(|value| value.len())
        .unwrap_or(0)
}

#[test]
fn visual_qa_input_exposes_only_typed_self_eval_fields() {
    // The typed extras and nothing else. There is no executable, no tools root
    // and no caller-chosen path: the only path is the project itself.
    let base = json!({
        "schema_version": 1,
        "project_root": "/tmp/project",
        "owner": "codex",
        "lease_id": "lease-1",
        "action": "render-self-eval-review",
        "reviewed_by": "vision-agent",
        "verdict": "fail",
        "notes": "",
        "reviewer_kind": "vision",
        "provider": "google",
        "model": "gemini-2.5-pro",
        "capability": "video_understanding.v1",
        "findings": [{
            "timestamp_seconds": 4.5,
            "boundary_id": "scene-2",
            "category": "overlay_conflict",
            "severity": "high",
            "message": "overlay collides with the subtitle",
        }],
        "attestation_ref": "self-eval-attestation:self-eval-vision-0001",
        "idempotency_key": "review-once",
    });
    serde_json::from_value::<mcp::VisualQaInput>(base.clone()).unwrap();

    // Anything that would let a caller choose what runs, or where it reads and
    // writes, is refused by the schema itself rather than ignored downstream.
    for smuggled in [
        "executable",
        "program",
        "command",
        "argv",
        "tools_root",
        "script",
        "review_json_path",
        "review_json",
        "attestation_path",
        "vision_unavailable",
        "nonce",
        "generation",
        "result_path",
    ] {
        let mut input = base.clone();
        input[smuggled] = json!("/tmp/attacker");
        assert!(
            serde_json::from_value::<mcp::VisualQaInput>(input).is_err(),
            "visual_qa accepted caller-chosen field {smuggled}"
        );
    }

    // A finding is exactly five typed values; it cannot carry a path either.
    for smuggled in ["path", "evidence_path", "sha256"] {
        let mut input = base.clone();
        input["findings"][0][smuggled] = json!("/tmp/attacker");
        assert!(
            serde_json::from_value::<mcp::VisualQaInput>(input).is_err(),
            "a finding accepted {smuggled}"
        );
    }

    // A findings entry cannot be a bare string or drop a required value.
    let mut untyped = base.clone();
    untyped["findings"] = json!(["scene-2 looks wrong"]);
    assert!(serde_json::from_value::<mcp::VisualQaInput>(untyped).is_err());
    let mut incomplete = base;
    incomplete["findings"][0]
        .as_object_mut()
        .unwrap()
        .remove("severity");
    assert!(serde_json::from_value::<mcp::VisualQaInput>(incomplete).is_err());
}

#[tokio::test]
async fn served_schema_names_every_typed_field_and_adds_no_tool() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let state = directory.path().join("runtime-state");
    fs::create_dir_all(repo.join("scripts")).unwrap();

    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
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

    let tools = client.list_all_tools().await.unwrap();
    let names: Vec<_> = tools.iter().map(|tool| tool.name.as_ref()).collect();
    assert_eq!(names, TOOL_SURFACE);

    let tool = |name: &str| {
        let tool = tools.iter().find(|tool| tool.name == name).unwrap();
        serde_json::to_value(tool.input_schema.as_ref()).unwrap()
    };

    let prepare_approval = tool("prepare_publish_approval");
    assert_eq!(
        property_names(&prepare_approval, "/properties"),
        ["override_reason", "project_root", "schema_version"]
    );
    assert_eq!(prepare_approval["additionalProperties"], false);

    let visual_qa = tool("visual_qa");
    assert_eq!(
        property_names(&visual_qa, "/properties"),
        [
            "action",
            "attestation_ref",
            "capability",
            "findings",
            "idempotency_key",
            "lease_id",
            "model",
            "notes",
            "owner",
            "project_root",
            "provider",
            "reviewed_by",
            "reviewer_kind",
            "schema_version",
            "verdict",
        ]
    );
    assert_eq!(
        shape_declaring(&visual_qa, "boundary_id"),
        [
            "boundary_id",
            "category",
            "message",
            "severity",
            "timestamp_seconds",
        ]
    );

    // The runner is a name, not a program: `run_next` gained nothing.
    assert_eq!(
        property_names(&tool("run_next"), "/properties"),
        [
            "idempotency_key",
            "lease_id",
            "owner",
            "project_root",
            "runner",
            "schema_version",
            "tools_root",
        ]
    );

    // Nothing anywhere on the served surface lets a caller name a binary.
    let surface = serde_json::to_string(&tools).unwrap();
    for forbidden in ["executable", "\"program\"", "\"argv\"", "interpreter"] {
        assert!(
            !surface.contains(forbidden),
            "served surface exposes {forbidden}"
        );
    }

    client.cancel().await.unwrap();
    server.abort();
}

#[cfg(unix)]
#[tokio::test]
async fn self_eval_review_dispatch_passes_one_canonical_argv_value_and_replays_once() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/mina-story");
    let state = directory.path().join("runtime-state");
    fs::create_dir_all(project.join(".hvp")).unwrap();
    fs::create_dir_all(project.join(SELF_EVAL_ROOT)).unwrap();
    write_compatible_contract(&project);
    write_self_eval_engine(&repo);
    let project = project.canonicalize().unwrap();
    let projection = write_pending_self_eval(&project);

    let served_repo = repo.clone();
    let served_state = state.clone();
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let server = tokio::spawn(async move {
        service(&served_repo, &served_state)
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
                    "owner": "codex",
                    "ttl_seconds": 60,
                    "idempotency_key": "claim-once",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(claimed.code, "lease_claimed");
    let lease_id = claimed.data.as_ref().unwrap()["lease_id"]
        .as_str()
        .unwrap()
        .to_owned();

    // Deliberately reversed findings and a `fail` verdict. The engine here does
    // not transition the projection, so the gate must keep reporting the pending
    // state the project actually holds -- a review never gets to name its own
    // outcome.
    let review = arguments(json!({
        "schema_version": 1,
        "project_root": project,
        "owner": "codex",
        "lease_id": lease_id,
        "action": "render-self-eval-review",
        "reviewed_by": "vision-agent",
        "verdict": "fail",
        "notes": "",
        "reviewer_kind": "vision",
        "provider": "google",
        "model": "gemini-2.5-pro",
        "capability": "video_understanding.v1",
        "findings": [
            {
                "timestamp_seconds": 12.0,
                "boundary_id": "scene-9",
                "category": "black_flash",
                "severity": "medium",
                "message": "single black frame at the seam",
            },
            {
                "timestamp_seconds": 4.5,
                "boundary_id": "scene-2",
                "category": "overlay_conflict",
                "severity": "high",
                "message": "overlay collides with the subtitle",
            },
        ],
        "attestation_ref": "self-eval-attestation:self-eval-vision-0001",
        "idempotency_key": "review-once",
    }));
    let recorded = structured(
        client
            .call_tool(CallToolRequestParams::new("visual_qa").with_arguments(review.clone()))
            .await
            .unwrap(),
    );

    assert_eq!(
        (recorded.outcome.as_str(), recorded.code.as_str()),
        ("ok", "self_eval_needs_review")
    );
    let data = recorded.data.as_ref().unwrap();
    assert_eq!(data["result"], projection);
    // The bytes did not move, so this is reuse -- reported as metadata, never as
    // a state or a code of its own.
    assert_eq!(data["reused"], json!(true));

    let argv = recorded_argv(&project);
    assert_eq!(argv.len(), 6);
    assert_eq!(argv[0], "review");
    assert_eq!(argv[1], project.to_str().unwrap());
    assert_eq!(argv[2], "--review-json");
    assert_eq!(argv[4], "--attestation-ref");
    assert_eq!(argv[5], "self-eval-attestation:self-eval-vision-0001");
    // One argv value, canonical: sorted keys, minified, findings normalised into
    // the contracted ascending order.
    assert_eq!(
        argv[3],
        serde_json::to_string(&json!({
            "reviewer_kind": "vision",
            "verdict": "fail",
            "reviewed_by": "vision-agent",
            "provider": "google",
            "model": "gemini-2.5-pro",
            "capability": "video_understanding.v1",
            "notes": "",
            "findings": [
                {
                    "timestamp_seconds": 4.5,
                    "boundary_id": "scene-2",
                    "category": "overlay_conflict",
                    "severity": "high",
                    "message": "overlay collides with the subtitle",
                },
                {
                    "timestamp_seconds": 12.0,
                    "boundary_id": "scene-9",
                    "category": "black_flash",
                    "severity": "medium",
                    "message": "single black frame at the seam",
                },
            ],
        }))
        .unwrap()
    );
    assert_eq!(engine_runs(&project), 1);

    // The same key replays the recorded result without running the engine again.
    let replay = structured(
        client
            .call_tool(CallToolRequestParams::new("visual_qa").with_arguments(review.clone()))
            .await
            .unwrap(),
    );
    assert_eq!(replay, recorded);
    assert_eq!(engine_runs(&project), 1);

    // A different review under the same key is a conflict, not a second review.
    let mut altered = review;
    altered.insert("verdict".to_owned(), json!("pass"));
    altered.insert("findings".to_owned(), json!([]));
    let conflict = structured(
        client
            .call_tool(CallToolRequestParams::new("visual_qa").with_arguments(altered))
            .await
            .unwrap(),
    );
    assert_eq!(conflict.code, "idempotency_conflict");
    assert_eq!(engine_runs(&project), 1);

    client.cancel().await.unwrap();
    server.abort();
}

#[cfg(unix)]
#[tokio::test]
async fn self_eval_dispatch_refuses_extras_on_existing_actions_and_a_tools_root() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/mina-story");
    let state = directory.path().join("runtime-state");
    fs::create_dir_all(project.join(".hvp")).unwrap();
    fs::create_dir_all(project.join(SELF_EVAL_ROOT)).unwrap();
    fs::create_dir_all(project.join("quality-review/visual-sampling")).unwrap();
    write_compatible_contract(&project);
    write_self_eval_engine(&repo);
    let project = project.canonicalize().unwrap();
    write_pending_self_eval(&project);

    let served_repo = repo.clone();
    let served_state = state.clone();
    let (server_transport, client_transport) = tokio::io::duplex(64 * 1024);
    let server = tokio::spawn(async move {
        service(&served_repo, &served_state)
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
                    "owner": "codex",
                    "ttl_seconds": 60,
                    "idempotency_key": "claim-once",
                }))),
            )
            .await
            .unwrap(),
    );
    let lease_id = claimed.data.as_ref().unwrap()["lease_id"]
        .as_str()
        .unwrap()
        .to_owned();

    // Sampling and both human reviews refuse self-eval authority outright.
    for (index, action) in ["sample", "review", "segment-review"].iter().enumerate() {
        let refused = structured(
            client
                .call_tool(
                    CallToolRequestParams::new("visual_qa").with_arguments(arguments(json!({
                        "schema_version": 1,
                        "project_root": project,
                        "owner": "codex",
                        "lease_id": lease_id,
                        "action": action,
                        "reviewed_by": "harvey",
                        "verdict": "pass",
                        "notes": "looks right",
                        "attestation_ref": "self-eval-attestation:self-eval-vision-0001",
                        "idempotency_key": format!("smuggle-{index}"),
                    }))),
                )
                .await
                .unwrap(),
        );
        assert_eq!(
            (refused.outcome.as_str(), refused.code.as_str()),
            ("error", "invalid_input"),
            "{action} accepted an attestation ref"
        );
        assert_eq!(engine_runs(&project), 0);
    }

    // The self-eval runner takes no tools root, and the refusal happens before
    // the engine is reached.
    let refused = structured(
        client
            .call_tool(
                CallToolRequestParams::new("run_next").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "codex",
                    "lease_id": lease_id,
                    "runner": "render-self-eval",
                    "tools_root": repo,
                    "idempotency_key": "run-with-tools-root",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(
        (refused.outcome.as_str(), refused.code.as_str()),
        ("error", "invalid_input")
    );
    assert_eq!(engine_runs(&project), 0);

    // Without one it runs `evaluate <project>` and reports the pending state.
    let evaluated = structured(
        client
            .call_tool(
                CallToolRequestParams::new("run_next").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "codex",
                    "lease_id": lease_id,
                    "runner": "render-self-eval",
                    "idempotency_key": "run-once",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(
        (evaluated.outcome.as_str(), evaluated.code.as_str()),
        ("ok", "self_eval_needs_review")
    );
    assert_eq!(
        recorded_argv(&project),
        ["evaluate", project.to_str().unwrap()]
    );
    assert_eq!(engine_runs(&project), 1);

    // A caller without the lease cannot advance the gate at all.
    let unleased = structured(
        client
            .call_tool(
                CallToolRequestParams::new("run_next").with_arguments(arguments(json!({
                    "schema_version": 1,
                    "project_root": project,
                    "owner": "impostor",
                    "lease_id": lease_id,
                    "runner": "render-self-eval",
                    "idempotency_key": "run-unleased",
                }))),
            )
            .await
            .unwrap(),
    );
    assert_eq!(
        (unleased.outcome.as_str(), unleased.code.as_str()),
        ("blocked", "lease_invalid")
    );
    assert_eq!(engine_runs(&project), 1);

    client.cancel().await.unwrap();
    server.abort();
}
