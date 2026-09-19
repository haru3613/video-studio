use std::ffi::OsString;
use std::fs;
use std::io;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, SystemTime};

use pipeline::ProjectStore;
use pipeline::application::{
    self, ApprovePublishRequest, CommandExecutor, CommandResult, LeaseInput, ProcessExecutor,
    ProduceArtifactRequest, PronunciationReviewRequest, PublishRequest, ReconcileUploadRequest,
    ReplaceThumbnailRequest, ReviewResolutionRequest, RunNextRequest, SelfEvalReviewInput,
    VisualQaRequest, approve_publish, prepare_publish, produce_artifact, pronunciation_review,
    publish, reconcile_upload, replace_thumbnail, run_next, verify, visual_qa,
};

use pipeline::runtime::RuntimeBinding;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tempfile::tempdir;

fn canonical_test_string(hasher: &mut Sha256, value: &str) {
    hasher.update(b"s");
    hasher.update(value.len().to_string().as_bytes());
    hasher.update(b":");
    hasher.update(value.as_bytes());
}

fn canonical_test_update(hasher: &mut Sha256, value: &Value) {
    match value {
        Value::Null => hasher.update(b"n"),
        Value::Bool(true) => hasher.update(b"t"),
        Value::Bool(false) => hasher.update(b"f"),
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                let payload = value.to_string();
                hasher.update(b"i");
                hasher.update(payload.len().to_string().as_bytes());
                hasher.update(b":");
                hasher.update(payload.as_bytes());
            } else if let Some(value) = number.as_u64() {
                let payload = value.to_string();
                hasher.update(b"i");
                hasher.update(payload.len().to_string().as_bytes());
                hasher.update(b":");
                hasher.update(payload.as_bytes());
            } else {
                hasher.update(b"d");
                hasher.update(number.as_f64().unwrap().to_be_bytes());
            }
        }
        Value::String(value) => canonical_test_string(hasher, value),
        Value::Array(values) => {
            hasher.update(b"a");
            hasher.update(values.len().to_string().as_bytes());
            hasher.update(b":");
            for value in values {
                canonical_test_update(hasher, value);
            }
        }
        Value::Object(values) => {
            let mut entries = values.iter().collect::<Vec<_>>();
            entries.sort_by(|(left, _), (right, _)| left.as_bytes().cmp(right.as_bytes()));
            hasher.update(b"o");
            hasher.update(entries.len().to_string().as_bytes());
            hasher.update(b":");
            for (key, value) in entries {
                canonical_test_string(hasher, key);
                canonical_test_update(hasher, value);
            }
        }
    }
}

fn canonical_test_digest(value: &Value) -> String {
    let mut hasher = Sha256::new();
    canonical_test_update(&mut hasher, value);
    format!("{:x}", hasher.finalize())
}

fn run(home: &std::path::Path, arguments: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_hvp-state"))
        .args(arguments)
        .env("HOME", home)
        .env(
            "HARU_VIDEO_STUDIO_SELF_EVAL_STATE_ROOT",
            home.parent().unwrap().join(".self-eval-state"),
        )
        .env(
            "HARU_VIDEO_STUDIO_SELF_EVAL_ATTESTATION_ROOT",
            home.parent().unwrap().join(".self-eval-attestations"),
        )
        .output()
        .unwrap()
}

fn json(output: &Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|error| {
        panic!(
            "stdout was not JSON: {error}; stdout={}; stderr={}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    })
}
/// Runs real repository commands against this test's isolated protected
/// self-evaluation authority, without mutating process-global environment.
struct RootedProcessExecutor {
    state_root: PathBuf,
    attestation_root: PathBuf,
}

impl RootedProcessExecutor {
    fn new(root: &Path) -> Self {
        Self {
            state_root: root.join(".self-eval-state"),
            attestation_root: root.join(".self-eval-attestations"),
        }
    }
}

impl CommandExecutor for RootedProcessExecutor {
    fn execute(
        &mut self,
        program: &Path,
        arguments: &[std::ffi::OsString],
    ) -> io::Result<CommandResult> {
        let output = Command::new(program)
            .args(arguments)
            .env_clear()
            .env("PATH", "/usr/bin:/bin")
            .env("HARU_VIDEO_STUDIO_SELF_EVAL_STATE_ROOT", &self.state_root)
            .env(
                "HARU_VIDEO_STUDIO_SELF_EVAL_ATTESTATION_ROOT",
                &self.attestation_root,
            )
            .output()?;
        Ok(CommandResult {
            exit_code: output.status.code(),
            data: serde_json::from_slice(&output.stdout).ok(),
        })
    }
}
fn lease_input(lease: &pipeline::Lease) -> LeaseInput {
    LeaseInput::from_lease(lease)
}

fn ready_verification(project: &str) -> Value {
    serde_json::json!({
        "schema": "haru.pipeline_status.v1",
        "project": project,
        "overall_status": "ready_for_human_upload_approval",
        "blockers": [],
        "blocker_details": []
    })
}

fn embedded_channel() -> String {
    pipeline::source_fingerprint::manifest().youtube_channel_id
}

fn write_ready_project(root: &Path, slug: &str) -> PathBuf {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let output = Command::new("/usr/bin/python3")
        .args([
            "-S",
            "-c",
            "from pathlib import Path; import sys; from test_agent_status import make_ready_project, write_mina_editorial_fixture; project=make_ready_project(Path(sys.argv[1]), sys.argv[2]); write_mina_editorial_fixture(project)",
            root.to_str().unwrap(),
            slug,
        ])
        .env("PYTHONPATH", repo.join("tools"))
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    let project = root.join("projects").join(slug);
    fs::create_dir_all(project.join(".hvp")).unwrap();
    fs::write(
        project.join(".hvp/selection.json"),
        serde_json::to_vec(&serde_json::json!({
            "schema_version": 1,
            "cron_run_id": "topic-20260729",
            "candidate_id": "candidate-1",
            "chosen_by": "harvey",
            "chosen_at": 1785254400_u64,
            "project_slug": slug,
        }))
        .unwrap(),
    )
    .unwrap();
    project
}

fn update_json(path: &Path, update: impl FnOnce(&mut Value)) {
    let mut value: Value = serde_json::from_slice(&fs::read(path).unwrap()).unwrap();
    update(&mut value);
    fs::write(path, serde_json::to_vec(&value).unwrap()).unwrap();
}

fn copy_tree(source: &Path, target: &Path) {
    fs::create_dir_all(target).unwrap();
    for entry in fs::read_dir(source).unwrap() {
        let entry = entry.unwrap();
        let destination = target.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &destination);
        } else {
            fs::copy(entry.path(), destination).unwrap();
        }
    }
}

#[derive(Default)]
struct FakeExecutor {
    calls: Vec<(PathBuf, Vec<std::ffi::OsString>)>,
    exit_code: i32,
    data: Option<Value>,
    unavailable: bool,
}

impl CommandExecutor for FakeExecutor {
    fn execute(
        &mut self,
        program: &Path,
        arguments: &[std::ffi::OsString],
    ) -> io::Result<CommandResult> {
        self.calls.push((program.to_path_buf(), arguments.to_vec()));
        if self.unavailable {
            Err(io::Error::new(io::ErrorKind::NotFound, "runner missing"))
        } else {
            Ok(CommandResult {
                exit_code: Some(self.exit_code),
                data: self.data.clone(),
            })
        }
    }
}

#[test]
fn create_returns_one_machine_readable_scaffold_next_action() {
    let directory = tempdir().unwrap();
    let projects = directory.path().join("projects");
    fs::create_dir(&projects).unwrap();

    let result = application::create(&projects, "new-video");

    assert_eq!(result.code, "project_created");
    let data = result.data.unwrap();
    assert_eq!(data["next_action"]["command"], "scripts/hvp-scaffold");
    assert_eq!(data["next_action"]["arguments"][0], "scaffold");
    assert_eq!(
        data["next_action"]["arguments"][1],
        projects
            .join("new-video")
            .canonicalize()
            .unwrap()
            .to_string_lossy()
            .as_ref()
    );
    assert_eq!(data["next_action"]["reason"], "canonical_scaffold_required");
}

#[test]
fn app_status_and_verify_have_table_driven_gate_parity() {
    let directory = tempdir().unwrap();
    let home = directory.path().join("home");
    fs::create_dir(&home).unwrap();

    for (case, ready) in [
        ("missing-selection", false),
        ("wrong-pron-sha", false),
        ("wrong-cover-size", false),
        ("shortened-required-checks", false),
        ("stale-review", false),
        ("valid-full-project", false),
    ] {
        let project = write_ready_project(directory.path(), case);
        match case {
            "missing-selection" => {
                fs::remove_file(project.join(".hvp/selection.json")).unwrap();
            }
            "wrong-pron-sha" => {
                update_json(&project.join("narration-final.mp3.pron-ok.json"), |value| {
                    value["sha256"] = Value::String("0".repeat(64))
                })
            }
            "wrong-cover-size" => {
                fs::write(project.join("output/cover.png"), "not a png").unwrap();
            }
            "shortened-required-checks" => update_json(
                &project.join("quality-review/final-v1/review.json"),
                |value| {
                    value["required_checks"] = serde_json::json!(["visual_spot_check"]);
                    value["checks"] =
                        serde_json::json!([{"name": "visual_spot_check", "status": "pass"}]);
                },
            ),
            "stale-review" => {
                fs::write(project.join("output/newer-final-v2.mp4"), "newer video").unwrap();
            }
            "valid-full-project" => {}
            _ => unreachable!(),
        }

        let status = run(&home, &["app", "status", project.to_str().unwrap()]);
        let verification = run(&home, &["app", "verify", project.to_str().unwrap()]);

        assert!(status.status.success(), "{case}: {status:?}");
        assert_eq!(
            verification.status.code(),
            Some(if ready { 0 } else { 4 }),
            "{case}: {verification:?}"
        );
        let status = json(&status);
        let verification = json(&verification);
        for field in [
            "stages",
            "blocker_details",
            "overall_status",
            "segment_mode",
            "segments",
            "next_actionable_segment",
        ] {
            assert_eq!(
                status["data"][field], verification["data"][field],
                "{case}: {field}"
            );
        }
        let expected_next = verification["data"]["required_stages"]
            .as_array()
            .unwrap()
            .iter()
            .find(|gate| verification["data"]["stages"][gate.as_str().unwrap()]["status"] != "pass")
            .cloned()
            .unwrap_or(Value::Null);
        assert_eq!(
            status["data"]["operations"]["next_gate"], expected_next,
            "{case}: resume point"
        );

        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let mut rooted_executor = RootedProcessExecutor::new(directory.path());
        let artifact_index =
            pipeline::application::artifact_index(&project, repo, &mut rooted_executor);
        assert_eq!(artifact_index.code, "artifact_index", "{case}");
        for field in [
            "stages",
            "blocker_details",
            "overall_status",
            "segment_mode",
            "segments",
            "next_actionable_segment",
        ] {
            assert_eq!(
                artifact_index.data.as_ref().unwrap()[field],
                verification["data"][field],
                "{case}: artifact_index {field}"
            );
        }

        let lease = ProjectStore::new(&project)
            .claim_at("parity-test", Duration::from_secs(60), SystemTime::now())
            .unwrap();
        let prepared = prepare_publish(&project, repo, &lease_input(&lease), &mut rooted_executor);
        assert_eq!(prepared.code, "command_failed", "{case}");
        if let Some(prepared) = prepared.data.as_ref() {
            for field in ["stages", "blocker_details", "overall_status"] {
                assert_eq!(
                    prepared[field], verification["data"][field],
                    "{case}: prepare_publish {field}"
                );
            }
        }
        if case == "valid-full-project" {
            assert!(
                verification["data"]["blocker_details"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|detail| detail["code"] == "publish_target_not_passed"),
                "the public default must expose its missing publish policy"
            );
        }
    }
}

#[test]
fn a_second_caller_runs_exactly_one_configured_gate_with_a_valid_lease() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/mina-story");
    fs::create_dir(&repo).unwrap();
    fs::create_dir(repo.join("scripts")).unwrap();
    fs::write(repo.join("scripts/verify-project"), "#!/bin/sh\n").unwrap();
    fs::create_dir_all(&project).unwrap();

    let lease = ProjectStore::new(&project)
        .claim_at("first-agent", Duration::from_secs(60), SystemTime::now())
        .unwrap();

    let second_caller_request = RunNextRequest {
        project_root: project.canonicalize().unwrap(),
        lease: lease_input(&lease),
        runner: "verify-project".to_owned(),
        tools_root: None,
    };
    let mut executor = FakeExecutor {
        data: Some(ready_verification("mina-story")),
        ..FakeExecutor::default()
    };
    let result = run_next(&second_caller_request, &repo, &mut executor);

    assert_eq!(result.outcome, "ok");
    assert_eq!(result.code, "gate_completed");
    assert_eq!(result.data, Some(ready_verification("mina-story")));
    assert_eq!(executor.calls.len(), 1);
    assert_eq!(
        executor.calls[0],
        (
            repo.canonicalize().unwrap().join("scripts/verify-project"),
            vec![project.canonicalize().unwrap().into_os_string()]
        )
    );

    let mut unsupported = second_caller_request.clone();
    unsupported.runner = "provider-command".to_owned();
    let mut blocked_executor = FakeExecutor::default();
    let blocked = run_next(&unsupported, &repo, &mut blocked_executor);
    assert_eq!(
        (blocked.outcome.as_str(), blocked.code.as_str()),
        ("blocked", "unsupported_gate")
    );
    assert!(blocked_executor.calls.is_empty());

    let tools = directory.path().join("tools");
    fs::create_dir(&tools).unwrap();
    fs::write(repo.join("scripts/render-project"), "#!/bin/sh\n").unwrap();
    fs::create_dir(project.join("output")).unwrap();
    let video_bytes = b"video";
    fs::write(project.join("output/final.mp4"), video_bytes).unwrap();
    let video_sha256 = format!("{:x}", Sha256::digest(video_bytes));
    let render_data = serde_json::json!({
        "schema_version": 1,
        "outcome": "ok",
        "code": "render_complete",
        "project": project.canonicalize().unwrap(),
        "data": {
            "schema": "haru.render_result.v1",
            "status": "render_complete",
            "project": "mina-story",
            "output": "output/final.mp4",
            "video_sha256": video_sha256,
            "bytes": video_bytes.len(),
            "duration_seconds": 60.0,
            "loudness_lufs": -14.0,
            "true_peak_dbfs": -1.0,
            "loudness_range_lu": 5.3,
            "mix": {
                "schema": "haru.final_mix.v1",
                "method": "ffmpeg_loudnorm_two_pass",
                "normalization_type": "linear",
                "input_sha256": "b".repeat(64),
                "target": {
                    "integrated_lufs": -14.0,
                    "true_peak_dbfs": -1.0,
                    "loudness_range_lu": 5.3
                }
            }
        }
    });
    let mut render_request = second_caller_request.clone();
    render_request.runner = "render-project".to_owned();
    render_request.tools_root = Some(tools.canonicalize().unwrap());
    let mut render_executor = FakeExecutor {
        data: Some(render_data.clone()),
        ..FakeExecutor::default()
    };
    let rendered = run_next(&render_request, &repo, &mut render_executor);
    assert_eq!(
        (rendered.outcome.as_str(), rendered.code.as_str()),
        ("ok", "gate_completed")
    );
    assert_eq!(rendered.data, Some(render_data.clone()));
    assert_eq!(
        render_executor.calls[0],
        (
            repo.canonicalize().unwrap().join("scripts/render-project"),
            vec![
                project.canonicalize().unwrap().into_os_string(),
                tools.canonicalize().unwrap().into_os_string()
            ]
        )
    );

    fs::write(repo.join("scripts/generate-cover"), "#!/bin/sh\n").unwrap();
    let cover_bytes = b"cover";
    fs::write(project.join("output/cover.png"), cover_bytes).unwrap();
    let cover_data = serde_json::json!({
        "schema": "haru.cover_generation.v1",
        "ok": true,
        "project": "mina-story",
        "status": "complete",
        "output": "output/cover.png",
        "output_sha256": format!("{:x}", Sha256::digest(cover_bytes)),
        "bytes": cover_bytes.len(),
        "width": 1280,
        "height": 720
    });
    let mut cover_request = second_caller_request.clone();
    cover_request.runner = "generate-cover".to_owned();
    cover_request.tools_root = Some(tools.canonicalize().unwrap());
    let mut cover_executor = FakeExecutor {
        data: Some(cover_data.clone()),
        ..FakeExecutor::default()
    };
    let generated = run_next(&cover_request, &repo, &mut cover_executor);
    assert_eq!(generated.code, "gate_completed");
    assert_eq!(generated.data, Some(cover_data));
    assert_eq!(
        cover_executor.calls[0],
        (
            repo.canonicalize().unwrap().join("scripts/generate-cover"),
            vec![
                project.canonicalize().unwrap().into_os_string(),
                tools.canonicalize().unwrap().into_os_string()
            ]
        )
    );

    fs::remove_file(project.join("output/final.mp4")).unwrap();
    let job_id = "a".repeat(32);
    let pending_data = serde_json::json!({
        "schema_version": 1,
        "outcome": "ok",
        "code": "render_started",
        "project": project.canonicalize().unwrap(),
        "data": {
            "schema": "haru.render_job.v2",
            "job_id": job_id,
            "project": "mina-story",
            "status": "running",
            "launcher": "portable-python",
            "epoch": 1,
            "revision": "b".repeat(64),
            "pid": 1234,
            "log": project.canonicalize().unwrap().join("output/.staging").join(&job_id).join("worker.log"),
            "output": "output/final.mp4",
            "started_at": "2026-09-19T00:00:00+00:00"
        }
    });
    let mut pending_executor = FakeExecutor {
        data: Some(pending_data.clone()),
        ..FakeExecutor::default()
    };
    let pending = run_next(&render_request, &repo, &mut pending_executor);
    assert_eq!(
        (pending.outcome.as_str(), pending.code.as_str()),
        ("ok", "gate_started")
    );
    assert_eq!(pending.data, Some(pending_data));
    let mut extended = pending.data.unwrap();
    extended["data"]["worker_path"] = serde_json::json!("/tmp/untrusted-worker.py");
    let mut extended_executor = FakeExecutor {
        data: Some(extended),
        ..FakeExecutor::default()
    };
    assert_eq!(
        run_next(&render_request, &repo, &mut extended_executor).code,
        "command_failed",
        "an unexpected durable-job field must not widen the accepted projection"
    );
    fs::write(project.join("output/final.mp4"), video_bytes).unwrap();

    let mut unmastered_data = render_data.clone();
    unmastered_data["data"]
        .as_object_mut()
        .unwrap()
        .remove("loudness_lufs");
    let mut unmastered_executor = FakeExecutor {
        data: Some(unmastered_data),
        ..FakeExecutor::default()
    };
    let unmastered = run_next(&render_request, &repo, &mut unmastered_executor);
    assert_eq!(
        (unmastered.outcome.as_str(), unmastered.code.as_str()),
        ("error", "command_failed")
    );

    let mut flattened_data = render_data.clone();
    flattened_data["data"]["loudness_range_lu"] = serde_json::json!(0.0);
    flattened_data["data"]["mix"]["target"]["loudness_range_lu"] = serde_json::json!(20.0);
    let mut flattened_executor = FakeExecutor {
        data: Some(flattened_data),
        ..FakeExecutor::default()
    };
    let flattened = run_next(&render_request, &repo, &mut flattened_executor);
    assert_eq!(
        (flattened.outcome.as_str(), flattened.code.as_str()),
        ("error", "command_failed")
    );

    let mut stale_request = second_caller_request.clone();
    stale_request.lease.generation += 1;
    let mut stale_executor = FakeExecutor::default();
    let stale = run_next(&stale_request, &repo, &mut stale_executor);
    assert_eq!(
        (stale.outcome.as_str(), stale.code.as_str()),
        ("blocked", "lease_invalid")
    );
    assert!(stale_executor.calls.is_empty());

    let mut failed_executor = FakeExecutor {
        exit_code: 9,
        ..FakeExecutor::default()
    };
    let failed = run_next(&second_caller_request, &repo, &mut failed_executor);
    assert_eq!(
        (failed.outcome.as_str(), failed.code.as_str()),
        ("error", "command_failed")
    );
    assert_eq!(failed_executor.calls.len(), 1);

    let mut unavailable_executor = FakeExecutor {
        unavailable: true,
        ..FakeExecutor::default()
    };
    let unavailable = run_next(&second_caller_request, &repo, &mut unavailable_executor);
    assert_eq!(
        (unavailable.outcome.as_str(), unavailable.code.as_str()),
        ("error", "runner_unavailable")
    );
    assert_eq!(unavailable_executor.calls.len(), 1);

    fs::remove_file(project.join(".hvp/lease.lock")).unwrap();
    fs::create_dir(project.join(".hvp/lease.lock")).unwrap();
    let mut lock_error_executor = FakeExecutor::default();
    let lock_error = run_next(&second_caller_request, &repo, &mut lock_error_executor);
    assert_eq!(
        (lock_error.outcome.as_str(), lock_error.code.as_str()),
        ("error", "internal_error")
    );
    assert!(lock_error_executor.calls.is_empty());
}

#[test]
fn pronunciation_runners_and_human_review_use_fixed_digest_bound_commands() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/pronunciation-demo");
    let tools = directory.path().join("media-tools");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir_all(project.join(".hvp")).unwrap();
    fs::create_dir(&tools).unwrap();
    fs::write(repo.join("scripts/pronunciation-workflow"), "#!/bin/sh\n").unwrap();
    let plan_bytes = br#"{"schema":"haru.pronunciation_plan.v1"}"#;
    fs::write(project.join("pronunciation-plan.json"), plan_bytes).unwrap();
    let plan_sha = format!("{:x}", Sha256::digest(plan_bytes));

    let lease = ProjectStore::new(&project)
        .claim_at("codex", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let base = RunNextRequest {
        project_root: project.canonicalize().unwrap(),
        lease: lease_input(&lease),
        runner: "analyze-pronunciation".to_owned(),
        tools_root: Some(tools.canonicalize().unwrap()),
    };
    let analysis = serde_json::json!({
        "schema": "haru.pronunciation_analysis.v1",
        "project": "pronunciation-demo",
        "status": "complete",
        "source_sha256": "a".repeat(64),
        "output": "pronunciation-plan.json",
        "output_sha256": plan_sha,
    });
    let mut analyze_executor = FakeExecutor {
        data: Some(analysis.clone()),
        ..FakeExecutor::default()
    };
    let analyzed = run_next(&base, &repo, &mut analyze_executor);
    assert_eq!(analyzed.code, "gate_completed");
    assert_eq!(analyzed.data, Some(analysis));
    assert_eq!(
        analyze_executor.calls[0],
        (
            repo.canonicalize()
                .unwrap()
                .join("scripts/pronunciation-workflow"),
            vec![
                "analyze".into(),
                project.canonicalize().unwrap().into_os_string(),
                tools.canonicalize().unwrap().into_os_string(),
            ]
        )
    );

    let audio = project.join(".hvp/probe.mp3");
    fs::write(&audio, b"probe").unwrap();
    let validation = project.join(".hvp/g2p-validation.json");
    fs::write(&validation, b"validation").unwrap();
    let probe = serde_json::json!({
        "schema": "haru.pronunciation_probe.v1",
        "project": "pronunciation-demo",
        "status": "complete",
        "mode": "ab",
        "source_sha256": "a".repeat(64),
        "plan_sha256": plan_sha,
        "request_sha256": "b".repeat(64),
        "max_credits": 100,
        "credits_spent": 20,
        "g2p_validation": {
            "path": ".hvp/g2p-validation.json",
            "sha256": format!("{:x}", Sha256::digest(b"validation")),
        },
        "audio": [{
            "path": ".hvp/probe.mp3",
            "sha256": format!("{:x}", Sha256::digest(b"probe")),
        }],
    });
    let mut confirm_request = base.clone();
    confirm_request.runner = "confirm-pronunciation".to_owned();
    let mut confirm_executor = FakeExecutor {
        data: Some(probe.clone()),
        ..FakeExecutor::default()
    };
    let confirmed = run_next(&confirm_request, &repo, &mut confirm_executor);
    assert_eq!(confirmed.code, "gate_completed");
    assert_eq!(confirmed.data, Some(probe));
    assert_eq!(confirm_executor.calls[0].1[0], "confirm");

    let candidate_files = [
        ("audio", ".hvp/candidate.mp3", b"candidate".as_slice()),
        ("srt", ".hvp/candidate.srt", b"subtitle".as_slice()),
        ("take", ".hvp/candidate.take.json", b"take".as_slice()),
        ("overrides", ".hvp/overrides.json", b"overrides".as_slice()),
    ];
    let mut artifacts = serde_json::Map::new();
    for (name, path, bytes) in candidate_files {
        fs::write(project.join(path), bytes).unwrap();
        artifacts.insert(
            name.to_owned(),
            serde_json::json!({
                "path": path,
                "sha256": format!("{:x}", Sha256::digest(bytes)),
            }),
        );
    }
    let candidate = serde_json::json!({
        "schema": "haru.narration_generation.v4",
        "project": "pronunciation-demo",
        "status": "complete",
        "generation_mode": "sectioned",
        "alignment_check": {"status": "pass"},
        "srt_alignment": {"source": "stt_forced"},
        "max_credits": 6000,
        "credits_spent": 4514,
        "artifacts": artifacts,
    });
    let mut narration_request = base.clone();
    narration_request.runner = "generate-narration".to_owned();
    let mut narration_executor = FakeExecutor {
        data: Some(candidate.clone()),
        ..FakeExecutor::default()
    };
    let generated = run_next(&narration_request, &repo, &mut narration_executor);
    assert_eq!(generated.code, "gate_completed");
    assert_eq!(generated.data, Some(candidate.clone()));
    assert_eq!(narration_executor.calls[0].1[0], "generate-narration");

    // The SRT pins the cue-driven visual cuts, and eleven_v3's own character
    // alignment drifts several seconds inside a long request while still
    // starting and ending in the right place -- which alignment_check cannot
    // see. A take timed by the provider is not ready to render from.
    for timeline in [
        serde_json::json!({"source": "provider_alignment"}),
        serde_json::json!(null),
    ] {
        let mut provider_timed = candidate.clone();
        if timeline.is_null() {
            provider_timed
                .as_object_mut()
                .unwrap()
                .remove("srt_alignment");
        } else {
            provider_timed["srt_alignment"] = timeline;
        }
        let mut executor = FakeExecutor {
            data: Some(provider_timed),
            ..FakeExecutor::default()
        };
        let refused = run_next(&narration_request, &repo, &mut executor);
        // Pinned to the branch, not merely "not completed" -- assert_ne would
        // also pass for a lease failure that never reached the gate.
        assert_eq!(refused.code, "command_failed");
    }

    // Promotion: the canonical narration is what the render reads, so a receipt
    // claiming a promotion is believed only once the named bytes are on disk
    // under the canonical names with the digests it states.
    let final_audio = b"promoted-audio-bytes";
    let final_srt = b"1\n00:00:00,000 --> 00:00:01,000\ncue\n\n";
    let final_stamp = b"{\"schema\":\"haru.pronunciation_approval.v1\"}";
    fs::write(project.join("narration-final.mp3"), final_audio).unwrap();
    fs::write(project.join("narration-final.srt"), final_srt).unwrap();
    fs::write(
        project.join("narration-final.mp3.pron-ok.json"),
        final_stamp,
    )
    .unwrap();
    let audio_digest = format!("{:x}", Sha256::digest(final_audio));
    let promotion = serde_json::json!({
        "schema": "haru.narration_promotion.v1",
        "project": "pronunciation-demo",
        "status": "complete",
        "accepted_by": "harvey",
        "audio_sha256": audio_digest,
        "candidate_audio": ".hvp/staging/narration-candidates/abc/narration-g2p-listen-1p25.mp3",
        "request_sha256": format!("{:x}", Sha256::digest(b"promotion request")),
        "narration_receipt_sha256": format!("{:x}", Sha256::digest(b"narration receipt")),
        "artifacts": {
            "audio": {"path": "narration-final.mp3", "sha256": audio_digest},
            "srt": {
                "path": "narration-final.srt",
                "sha256": format!("{:x}", Sha256::digest(final_srt)),
            },
            "pronunciation_stamp": {
                "path": "narration-final.mp3.pron-ok.json",
                "sha256": format!("{:x}", Sha256::digest(final_stamp)),
            },
        },
    });
    let mut promote_request = base.clone();
    promote_request.runner = "promote-narration".to_owned();
    let mut promote_executor = FakeExecutor {
        data: Some(promotion.clone()),
        ..FakeExecutor::default()
    };
    let promoted = run_next(&promote_request, &repo, &mut promote_executor);
    assert_eq!(promoted.code, "gate_completed");
    assert_eq!(promote_executor.calls[0].1[0], "promote-narration");

    // Refused: an accepted digest that is not the audio now standing as
    // canonical; a receipt pointing at the staged candidate rather than the
    // canonical name, which would pass while narration-final.mp3 is still the
    // previous take; a hand-assembled receipt missing the bindings only the
    // runner can compute; and canonical bytes swapped after the receipt was
    // written.
    let mut disagreeing = promotion.clone();
    disagreeing["audio_sha256"] = serde_json::json!(format!("{:x}", Sha256::digest(b"other")));
    let staged = project.join(".hvp/staging/elsewhere.mp3");
    fs::create_dir_all(staged.parent().unwrap()).unwrap();
    fs::write(&staged, final_audio).unwrap();
    let mut unpinned = promotion.clone();
    unpinned["artifacts"]["audio"]["path"] = serde_json::json!(".hvp/staging/elsewhere.mp3");
    let mut hand_written = promotion.clone();
    hand_written
        .as_object_mut()
        .unwrap()
        .remove("narration_receipt_sha256");
    for payload in [disagreeing, unpinned, hand_written, promotion.clone()] {
        if payload == promotion {
            fs::write(
                project.join("narration-final.mp3"),
                b"swapped after the receipt",
            )
            .unwrap();
        }
        let mut executor = FakeExecutor {
            data: Some(payload),
            ..FakeExecutor::default()
        };
        assert_eq!(
            run_next(&promote_request, &repo, &mut executor).code,
            "command_failed"
        );
    }
    fs::write(project.join("narration-final.mp3"), final_audio).unwrap();

    fs::write(&validation, b"tampered").unwrap();
    let mut stale_executor = FakeExecutor {
        data: confirm_executor.data.clone(),
        ..FakeExecutor::default()
    };
    let stale = run_next(&confirm_request, &repo, &mut stale_executor);
    assert_eq!(stale.code, "command_failed");

    let review_data = serde_json::json!({
        "schema": "haru.pronunciation_review.v1",
        "project": "pronunciation-demo",
        "verdict": "pass",
        "reviewed_by": "harvey",
        "reviewed_at": "2026-08-05T05:00:00+00:00",
        "notes": "listened",
        "source_sha256": "a".repeat(64),
        "plan_sha256": plan_sha,
        "probe_sha256": "c".repeat(64),
    });
    fs::write(
        project.join(".hvp/pronunciation-review.json"),
        serde_json::to_vec(&review_data).unwrap(),
    )
    .unwrap();
    let request = PronunciationReviewRequest {
        project_root: project.canonicalize().unwrap(),
        lease: base.lease,
        reviewed_by: "harvey".to_owned(),
        verdict: "pass".to_owned(),
        notes: "listened".to_owned(),
    };
    let mut review_executor = FakeExecutor {
        data: Some(review_data.clone()),
        ..FakeExecutor::default()
    };
    let reviewed = pronunciation_review(&request, &repo, &mut review_executor);
    assert_eq!(reviewed.code, "pronunciation_review_recorded");
    assert_eq!(reviewed.data, Some(review_data));
    assert_eq!(review_executor.calls[0].1[0], "review");
}

#[test]
fn visual_qa_uses_the_fixed_sampler_and_binds_its_receipt() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/mina-story");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::write(repo.join("scripts/visual-qa-sample"), "#!/bin/sh\n").unwrap();
    fs::create_dir_all(project.join("quality-review/visual-sampling")).unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("reviewer", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let sample = serde_json::json!({
        "schema": "haru.visual_qa_sample.v2",
        "project": "mina-story",
    });
    fs::write(
        project.join("quality-review/visual-sampling/visual-qa-sample.json"),
        serde_json::to_vec(&sample).unwrap(),
    )
    .unwrap();
    let request = VisualQaRequest {
        project_root: project.clone(),
        lease: lease_input(&lease),
        action: "sample".to_owned(),
        reviewed_by: None,
        verdict: None,
        notes: None,
        self_eval: None,
    };
    let mut executor = FakeExecutor {
        data: Some(sample.clone()),
        ..FakeExecutor::default()
    };

    let result = visual_qa(&request, &repo, &mut executor);

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "visual_qa_sampled")
    );
    assert_eq!(
        executor.calls[0],
        (
            repo.canonicalize()
                .unwrap()
                .join("scripts/visual-qa-sample"),
            vec![
                std::ffi::OsString::from("sample"),
                project.canonicalize().unwrap().into_os_string(),
            ]
        )
    );

    let review = serde_json::json!({
        "schema": "haru.visual_qa_review.v2",
        "project": "mina-story",
        "reviewed_by": "harvey",
        "verdict": "pass",
    });
    fs::write(
        project.join("quality-review/visual-sampling/visual-qa-review.json"),
        serde_json::to_vec(&review).unwrap(),
    )
    .unwrap();
    let mut review_request = request;
    review_request.action = "review".to_owned();
    review_request.reviewed_by = Some("harvey".to_owned());
    review_request.verdict = Some("pass".to_owned());
    review_request.notes = Some("final encoded contact sheet approved".to_owned());
    let mut review_executor = FakeExecutor {
        data: Some(review),
        ..FakeExecutor::default()
    };

    let result = visual_qa(&review_request, &repo, &mut review_executor);

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "visual_qa_recorded")
    );
    assert_eq!(review_executor.calls[0].1[0], "review");
    assert_eq!(review_executor.calls[0].1[2], "--reviewed-by");
    assert_eq!(review_executor.calls[0].1[4], "--verdict");
    assert_eq!(review_executor.calls[0].1[6], "--notes");
}

#[test]
fn segment_render_and_review_are_fixed_digest_bound_mcp_mutations() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/segment-demo");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::write(repo.join("scripts/render-segment"), "#!/bin/sh\n").unwrap();
    fs::create_dir_all(project.join("output/segments")).unwrap();
    fs::create_dir_all(project.join("quality-review/segments/qi")).unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("reviewer", Duration::from_secs(60), SystemTime::now())
        .unwrap();

    let storyboard = serde_json::json!({
        "schema": "haru.storyboard_timed.v1",
        "scenes": (0..4).map(|index| serde_json::json!({
            "scene_id": format!("s{}", index + 1),
            "start_seconds": index * 10,
            "end_seconds": (index + 1) * 10,
            "visual_events": [{
                "event_id": format!("e{}", index + 1),
                "start_seconds": index * 10,
                "end_seconds": (index + 1) * 10
            }]
        })).collect::<Vec<_>>()
    });
    let storyboard_bytes = serde_json::to_vec(&storyboard).unwrap();
    let srt = (0..4)
        .map(|index| {
            format!(
                "{}\n00:00:{:02},000 --> 00:00:{:02},000\ncue {}",
                index + 1,
                index * 10,
                (index + 1) * 10,
                index + 1
            )
        })
        .collect::<Vec<_>>()
        .join("\n\n")
        + "\n";
    fs::write(
        project.join("storyboard-final-timed.json"),
        &storyboard_bytes,
    )
    .unwrap();
    fs::write(project.join("editorial-contract.json"), b"{}\n").unwrap();
    fs::write(project.join("narration-final.mp3"), b"narration").unwrap();
    fs::write(project.join("narration-final.srt"), srt.as_bytes()).unwrap();
    let inputs = serde_json::json!({
        "storyboard": {
            "path": "storyboard-final-timed.json",
            "sha256": format!("{:x}", Sha256::digest(&storyboard_bytes))
        },
        "editorial_contract": {
            "path": "editorial-contract.json",
            "sha256": format!("{:x}", Sha256::digest(b"{}\n"))
        },
        "narration": {
            "path": "narration-final.mp3",
            "sha256": format!("{:x}", Sha256::digest(b"narration"))
        },
        "srt": {
            "path": "narration-final.srt",
            "sha256": format!("{:x}", Sha256::digest(srt.as_bytes()))
        }
    });
    let input_sha = canonical_test_digest(&serde_json::json!({
        "schema": "haru.segment_render_inputs.v1",
        "inputs": inputs
    }));
    let segments = (0..4)
        .map(|index| {
            let segment_id = ["qi", "cheng", "zhuan", "he"][index];
            let start = index * 10;
            let end = (index + 1) * 10;
            serde_json::json!({
                "segment_id": segment_id,
                "ordinal": index + 1,
                "narrative_role": format!("act-{}", index + 1),
                "scene_ids": [format!("s{}", index + 1)],
                "event_ids": [format!("e{}", index + 1)],
                "start": {
                    "scene_id": format!("s{}", index + 1),
                    "event_id": format!("e{}", index + 1),
                    "cue_index": index + 1,
                    "seconds": start
                },
                "end": {
                    "scene_id": format!("s{}", index + 1),
                    "event_id": format!("e{}", index + 1),
                    "cue_index": index + 1,
                    "seconds": end
                },
                "selector": {
                    "kind": "frame_range.v1",
                    "fps": 30,
                    "start_frame": start * 30,
                    "end_frame": end * 30
                },
                "output": format!("output/segments/{:02}-{segment_id}.mp4", index + 1)
            })
        })
        .collect::<Vec<_>>();
    let assembly = serde_json::json!({
        "order": ["qi", "cheng", "zhuan", "he"],
        "transition_policy": "cut.v1",
        "audio_policy": "premix.v1"
    });
    let plan = serde_json::json!({
        "schema": "haru.segment_plan.v1",
        "project": "segment-demo",
        "render_input_sha256": input_sha,
        "inputs": inputs,
        "segments": segments,
        "assembly": assembly
    });
    fs::write(
        project.join("segment-plan.json"),
        serde_json::to_vec(&plan).unwrap(),
    )
    .unwrap();
    let qi = &plan["segments"][0];
    let definition_sha = canonical_test_digest(qi);
    let locality = plan["segments"]
        .as_array()
        .unwrap()
        .iter()
        .map(|segment| {
            serde_json::json!({
                "segment_id": segment["segment_id"],
                "ordinal": segment["ordinal"],
                "start": segment["start"],
                "end": segment["end"],
                "scene_ids": segment["scene_ids"],
                "event_ids": segment["event_ids"],
                "selector": segment["selector"],
                "output": segment["output"]
            })
        })
        .collect::<Vec<_>>();
    let global_dependency_sha = canonical_test_digest(&serde_json::json!({
        "schema": "haru.segment_global_dependencies.v1",
        "editorial_contract_sha256": plan["inputs"]["editorial_contract"]["sha256"],
        "narration_sha256": plan["inputs"]["narration"]["sha256"],
        "storyboard_authority": {"schema": "haru.storyboard_timed.v1"},
        "locality": {"segments": locality, "assembly": plan["assembly"]}
    }));
    let dependency_sha = canonical_test_digest(&serde_json::json!({
        "schema": "haru.segment_dependencies.v1",
        "segment_id": "qi",
        "definition_sha256": definition_sha,
        "global_dependency_sha256": global_dependency_sha,
        "storyboard": {
            "scenes": [storyboard["scenes"][0].clone()],
            "events": [storyboard["scenes"][0]["visual_events"][0].clone()]
        },
        "srt_cues": [{
            "cue_index": 1,
            "start_seconds": 0.0,
            "end_seconds": 10.0,
            "text": "cue 1"
        }]
    }));
    let cheng_definition_sha = canonical_test_digest(&plan["segments"][1]);
    let cheng_dependency_sha = canonical_test_digest(&serde_json::json!({
        "schema": "haru.segment_dependencies.v1",
        "segment_id": "cheng",
        "definition_sha256": cheng_definition_sha,
        "global_dependency_sha256": global_dependency_sha,
        "storyboard": {
            "scenes": [storyboard["scenes"][1].clone()],
            "events": [storyboard["scenes"][1]["visual_events"][0].clone()]
        },
        "srt_cues": [{
            "cue_index": 2,
            "start_seconds": 10.0,
            "end_seconds": 20.0,
            "text": "cue 2"
        }]
    }));
    let video = b"reviewable qi bytes";
    fs::write(project.join("output/segments/01-qi.mp4"), video).unwrap();
    let video_sha = format!("{:x}", Sha256::digest(video));
    let evidence_assets = [
        ("head", "head.jpg"),
        ("tail", "tail.jpg"),
        ("authored_transition", "authored-transition.jpg"),
        ("caption_window", "caption-window.jpg"),
        ("waveform_window", "waveform.png"),
    ];
    let samples = evidence_assets
        .iter()
        .map(|(kind, name)| {
            let bytes = kind.as_bytes();
            fs::write(project.join("quality-review/segments/qi").join(name), bytes).unwrap();
            serde_json::json!({
                "kind": kind,
                "seconds": 0.0,
                "path": format!("quality-review/segments/qi/{name}"),
                "sha256": format!("{:x}", Sha256::digest(bytes)),
                "bytes": bytes.len()
            })
        })
        .collect::<Vec<_>>();
    let evidence = serde_json::json!({
        "schema": "haru.segment_review_evidence.v1",
        "segment_id": "qi",
        "video": "output/segments/01-qi.mp4",
        "video_sha256": video_sha,
        "definition_sha256": definition_sha,
        "dependency_sha256": dependency_sha,
        "samples": samples
    });
    let evidence_bytes = serde_json::to_vec(&evidence).unwrap();
    fs::write(
        project.join("quality-review/segments/qi/evidence.json"),
        &evidence_bytes,
    )
    .unwrap();
    let evidence_sha = format!("{:x}", Sha256::digest(&evidence_bytes));
    let stream_profile = serde_json::json!({
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "codec_tag_string": null,
                "extradata_hash": null,
                "time_base": "1/15360",
                "width": 1920,
                "height": 1080,
                "pix_fmt": "yuv420p",
                "field_order": null,
                "sample_aspect_ratio": null,
                "frame_rate": "30/1",
                "color_range": null,
                "color_space": null,
                "color_transfer": null,
                "color_primaries": null
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "codec_tag_string": null,
                "extradata_hash": null,
                "time_base": "1/48000",
                "sample_rate": 48000,
                "channels": 2,
                "channel_layout": "stereo",
                "sample_fmt": "fltp"
            }
        ]
    });
    let receipt = serde_json::json!({
        "schema": "haru.segment_render.v1",
        "status": "render_complete",
        "project": "segment-demo",
        "segment_id": "qi",
        "output": "output/segments/01-qi.mp4",
        "video_sha256": video_sha,
        "bytes": video.len(),
        "duration_seconds": 10.0,
        "decode_clean": true,
        "definition_sha256": definition_sha,
        "dependency_sha256": dependency_sha,
        "selector": plan["segments"][0]["selector"],
        "stream_profile": stream_profile.clone(),
        "evidence": {
            "path": "quality-review/segments/qi/evidence.json",
            "sha256": evidence_sha
        }
    });
    fs::write(
        project.join("quality-review/segments/qi/render.json"),
        serde_json::to_vec(&receipt).unwrap(),
    )
    .unwrap();

    let render_request = RunNextRequest {
        project_root: project.clone(),
        lease: lease_input(&lease),
        runner: "render-segment".to_owned(),
        tools_root: None,
    };
    let mut render_executor = FakeExecutor {
        data: Some(receipt.clone()),
        ..FakeExecutor::default()
    };
    let rendered = run_next(&render_request, &repo, &mut render_executor);
    assert_eq!(
        (rendered.outcome.as_str(), rendered.code.as_str()),
        ("ok", "gate_completed")
    );
    assert_eq!(
        render_executor.calls[0].1,
        vec![
            std::ffi::OsString::from("render"),
            project.canonicalize().unwrap().into_os_string(),
        ]
    );
    let mut missing_stream_profile = receipt.clone();
    missing_stream_profile
        .as_object_mut()
        .unwrap()
        .remove("stream_profile");
    fs::write(
        project.join("quality-review/segments/qi/render.json"),
        serde_json::to_vec(&missing_stream_profile).unwrap(),
    )
    .unwrap();
    let mut missing_profile_executor = FakeExecutor {
        data: Some(missing_stream_profile),
        ..FakeExecutor::default()
    };
    assert_eq!(
        run_next(&render_request, &repo, &mut missing_profile_executor).code,
        "command_failed"
    );
    fs::write(
        project.join("quality-review/segments/qi/render.json"),
        serde_json::to_vec(&receipt).unwrap(),
    )
    .unwrap();

    #[cfg(unix)]
    {
        let video_path = project.join("output/segments/01-qi.mp4");
        let direct_video_path = project.join("output/segments/qi-direct.mp4");
        fs::rename(&video_path, &direct_video_path).unwrap();
        std::os::unix::fs::symlink("qi-direct.mp4", &video_path).unwrap();
        let mut symlinked_output_executor = FakeExecutor {
            data: Some(receipt.clone()),
            ..FakeExecutor::default()
        };
        let refused_symlinked_output =
            run_next(&render_request, &repo, &mut symlinked_output_executor);
        assert_eq!(refused_symlinked_output.code, "command_failed");
        fs::remove_file(&video_path).unwrap();
        fs::rename(&direct_video_path, &video_path).unwrap();
    }

    fs::write(project.join("narration-final.mp3"), b"changed").unwrap();
    let mut stale_input_executor = FakeExecutor {
        data: Some(receipt.clone()),
        ..FakeExecutor::default()
    };
    let stale_input = run_next(&render_request, &repo, &mut stale_input_executor);
    assert_eq!(stale_input.code, "command_failed");
    fs::write(project.join("narration-final.mp3"), b"narration").unwrap();

    let mut incomplete = receipt.clone();
    incomplete
        .as_object_mut()
        .unwrap()
        .remove("definition_sha256");
    fs::write(
        project.join("quality-review/segments/qi/render.json"),
        serde_json::to_vec(&incomplete).unwrap(),
    )
    .unwrap();
    let mut incomplete_executor = FakeExecutor {
        data: Some(incomplete),
        ..FakeExecutor::default()
    };
    let refused = run_next(&render_request, &repo, &mut incomplete_executor);
    assert_eq!(refused.code, "command_failed");
    fs::write(
        project.join("quality-review/segments/qi/render.json"),
        serde_json::to_vec(&receipt).unwrap(),
    )
    .unwrap();

    let review = serde_json::json!({
        "schema": "haru.segment_review.v1",
        "project": "segment-demo",
        "segment_id": "qi",
        "output": "output/segments/01-qi.mp4",
        "video_sha256": video_sha,
        "definition_sha256": definition_sha,
        "dependency_sha256": dependency_sha,
        "evidence_sha256": evidence_sha,
        "verdict": "pass",
        "reviewed_by": "harvey",
        "reviewed_at": "2026-08-13T00:00:00+00:00",
        "notes": "qi holds"
    });
    fs::write(
        project.join("quality-review/segments/qi/review.json"),
        serde_json::to_vec(&review).unwrap(),
    )
    .unwrap();
    let mut noncanonical_review = review.clone();
    noncanonical_review["reviewed_at"] = serde_json::json!("2026-08-13T00:00:00Z");
    fs::write(
        project.join("quality-review/segments/qi/review.json"),
        serde_json::to_vec(&noncanonical_review).unwrap(),
    )
    .unwrap();
    let mut noncanonical_review_executor = FakeExecutor {
        data: Some(noncanonical_review),
        ..FakeExecutor::default()
    };
    let refused_noncanonical_review = visual_qa(
        &VisualQaRequest {
            project_root: project.clone(),
            lease: lease_input(&lease),
            action: "segment-review".to_owned(),
            reviewed_by: Some("harvey".to_owned()),
            verdict: Some("pass".to_owned()),
            notes: Some("qi holds".to_owned()),
            self_eval: None,
        },
        &repo,
        &mut noncanonical_review_executor,
    );
    assert_eq!(refused_noncanonical_review.code, "command_failed");
    fs::write(
        project.join("quality-review/segments/qi/review.json"),
        serde_json::to_vec(&review).unwrap(),
    )
    .unwrap();
    let review_request = VisualQaRequest {
        project_root: project.clone(),
        lease: lease_input(&lease),
        action: "segment-review".to_owned(),
        reviewed_by: Some("harvey".to_owned()),
        verdict: Some("pass".to_owned()),
        notes: Some("qi holds".to_owned()),
        self_eval: None,
    };
    let mut review_executor = FakeExecutor {
        data: Some(review.clone()),
        ..FakeExecutor::default()
    };
    let reviewed = visual_qa(&review_request, &repo, &mut review_executor);
    assert_eq!(
        (reviewed.outcome.as_str(), reviewed.code.as_str()),
        ("ok", "segment_review_recorded")
    );
    assert_eq!(
        review_executor.calls[0].0,
        repo.canonicalize().unwrap().join("scripts/render-segment")
    );
    assert_eq!(review_executor.calls[0].1[0], "review");

    fs::create_dir_all(project.join("quality-review/segments/cheng")).unwrap();
    let cheng_video = b"reviewable cheng bytes";
    fs::write(project.join("output/segments/02-cheng.mp4"), cheng_video).unwrap();
    let cheng_video_sha = format!("{:x}", Sha256::digest(cheng_video));
    let cheng_samples = evidence_assets
        .iter()
        .map(|(kind, name)| {
            let bytes = format!("cheng-{kind}").into_bytes();
            fs::write(
                project.join("quality-review/segments/cheng").join(name),
                &bytes,
            )
            .unwrap();
            serde_json::json!({
                "kind": kind,
                "seconds": 0.0,
                "path": format!("quality-review/segments/cheng/{name}"),
                "sha256": format!("{:x}", Sha256::digest(&bytes)),
                "bytes": bytes.len()
            })
        })
        .collect::<Vec<_>>();
    let cheng_evidence = serde_json::json!({
        "schema": "haru.segment_review_evidence.v1",
        "segment_id": "cheng",
        "video": "output/segments/02-cheng.mp4",
        "video_sha256": cheng_video_sha,
        "definition_sha256": cheng_definition_sha,
        "dependency_sha256": cheng_dependency_sha,
        "samples": cheng_samples
    });
    let cheng_evidence_bytes = serde_json::to_vec(&cheng_evidence).unwrap();
    fs::write(
        project.join("quality-review/segments/cheng/evidence.json"),
        &cheng_evidence_bytes,
    )
    .unwrap();
    let cheng_evidence_sha = format!("{:x}", Sha256::digest(&cheng_evidence_bytes));
    let cheng_receipt = serde_json::json!({
        "schema": "haru.segment_render.v1",
        "status": "render_complete",
        "project": "segment-demo",
        "segment_id": "cheng",
        "output": "output/segments/02-cheng.mp4",
        "video_sha256": cheng_video_sha,
        "bytes": cheng_video.len(),
        "duration_seconds": 10.0,
        "decode_clean": true,
        "definition_sha256": cheng_definition_sha,
        "dependency_sha256": cheng_dependency_sha,
        "selector": plan["segments"][1]["selector"],
        "stream_profile": stream_profile.clone(),
        "evidence": {
            "path": "quality-review/segments/cheng/evidence.json",
            "sha256": cheng_evidence_sha
        }
    });
    fs::write(
        project.join("quality-review/segments/cheng/render.json"),
        serde_json::to_vec(&cheng_receipt).unwrap(),
    )
    .unwrap();
    let mut cheng_render_executor = FakeExecutor {
        data: Some(cheng_receipt.clone()),
        ..FakeExecutor::default()
    };
    let cheng_rendered = run_next(&render_request, &repo, &mut cheng_render_executor);
    assert_eq!(
        (
            cheng_rendered.outcome.as_str(),
            cheng_rendered.code.as_str()
        ),
        ("ok", "gate_completed")
    );
    let cheng_review = serde_json::json!({
        "schema": "haru.segment_review.v1",
        "project": "segment-demo",
        "segment_id": "cheng",
        "output": "output/segments/02-cheng.mp4",
        "video_sha256": cheng_video_sha,
        "definition_sha256": cheng_definition_sha,
        "dependency_sha256": cheng_dependency_sha,
        "evidence_sha256": cheng_evidence_sha,
        "verdict": "pass",
        "reviewed_by": "harvey",
        "reviewed_at": "2026-08-13T00:00:00+00:00",
        "notes": "cheng holds"
    });
    fs::write(
        project.join("quality-review/segments/cheng/review.json"),
        serde_json::to_vec(&cheng_review).unwrap(),
    )
    .unwrap();
    let mut cheng_review_executor = FakeExecutor {
        data: Some(cheng_review.clone()),
        ..FakeExecutor::default()
    };
    let cheng_reviewed = visual_qa(&review_request, &repo, &mut cheng_review_executor);
    assert_eq!(
        (
            cheng_reviewed.outcome.as_str(),
            cheng_reviewed.code.as_str()
        ),
        ("ok", "segment_review_recorded")
    );
    let mut global_storyboard = storyboard.clone();
    global_storyboard["global_visual_direction"] =
        serde_json::json!("replace palette across all acts");
    let global_storyboard_bytes = serde_json::to_vec(&global_storyboard).unwrap();
    fs::write(
        project.join("storyboard-final-timed.json"),
        &global_storyboard_bytes,
    )
    .unwrap();
    let mut rebound_plan = plan.clone();
    rebound_plan["inputs"]["storyboard"]["sha256"] =
        serde_json::json!(format!("{:x}", Sha256::digest(&global_storyboard_bytes)));
    let rebound_input_sha = canonical_test_digest(&serde_json::json!({
        "schema": "haru.segment_render_inputs.v1",
        "inputs": rebound_plan["inputs"]
    }));
    rebound_plan["render_input_sha256"] = serde_json::json!(rebound_input_sha);
    fs::write(
        project.join("segment-plan.json"),
        serde_json::to_vec(&rebound_plan).unwrap(),
    )
    .unwrap();
    let mut global_storyboard_executor = FakeExecutor {
        data: Some(cheng_review.clone()),
        ..FakeExecutor::default()
    };
    let refused_global_storyboard =
        visual_qa(&review_request, &repo, &mut global_storyboard_executor);
    assert_eq!(refused_global_storyboard.code, "command_failed");
    fs::write(
        project.join("storyboard-final-timed.json"),
        &storyboard_bytes,
    )
    .unwrap();
    fs::write(
        project.join("segment-plan.json"),
        serde_json::to_vec(&plan).unwrap(),
    )
    .unwrap();
    let mut missing_review_time = review.clone();
    missing_review_time
        .as_object_mut()
        .unwrap()
        .remove("reviewed_at");
    fs::write(
        project.join("quality-review/segments/qi/review.json"),
        serde_json::to_vec(&missing_review_time).unwrap(),
    )
    .unwrap();
    let mut missing_review_time_executor = FakeExecutor {
        data: Some(missing_review_time),
        ..FakeExecutor::default()
    };
    let refused_review = visual_qa(&review_request, &repo, &mut missing_review_time_executor);
    assert_eq!(refused_review.code, "command_failed");

    let mut malformed_review_time = review;
    malformed_review_time["reviewed_at"] = serde_json::json!("2026-02-30T00:00:00+00:00");
    fs::write(
        project.join("quality-review/segments/qi/review.json"),
        serde_json::to_vec(&malformed_review_time).unwrap(),
    )
    .unwrap();
    let mut malformed_review_time_executor = FakeExecutor {
        data: Some(malformed_review_time),
        ..FakeExecutor::default()
    };
    let refused_review = visual_qa(&review_request, &repo, &mut malformed_review_time_executor);
    assert_eq!(refused_review.code, "command_failed");
}

#[test]
fn assemble_segments_runner_is_fixed_and_rejects_missing_or_tampered_receipts() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/demo");
    fs::create_dir_all(repo.join("tools")).unwrap();
    fs::write(
        repo.join("tools/segment_assembly.py"),
        "#!/usr/bin/env python3\n",
    )
    .unwrap();
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let fixture = r#"
import pathlib, shutil, sys
sys.path.insert(0, sys.argv[1])
import test_segment_render
case = test_segment_render.SegmentRenderContractTest(
    "test_render_binds_current_definition_dependency_and_exact_output"
)
case.setUp()
try:
    for _ in range(4):
        case.approve_next()
    shutil.copytree(case.project, pathlib.Path(sys.argv[2]), dirs_exist_ok=True)
finally:
    case.tearDown()
"#;
    assert!(
        Command::new("/usr/bin/python3")
            .args([
                "-c",
                fixture,
                source.join("tools").to_str().unwrap(),
                project.to_str().unwrap(),
            ])
            .status()
            .unwrap()
            .success()
    );
    fs::create_dir_all(project.join("quality-review/segments")).unwrap();
    let premix = b"approved assembled premix";
    fs::write(project.join("output/final.pre-loudnorm.mp4"), premix).unwrap();
    let premix_sha = format!("{:x}", Sha256::digest(premix));
    let policy_sha = canonical_test_digest(&serde_json::json!({
        "schema": "haru.segment_assembly_policy.v1",
        "order": ["qi", "cheng", "zhuan", "he"],
        "transition_policy": "cut.v1",
        "audio_policy": "premix.v1"
    }));
    let ids = ["qi", "cheng", "zhuan", "he"];
    let outputs = [
        "output/segments/01-qi.mp4",
        "output/segments/02-cheng.mp4",
        "output/segments/03-zhuan.mp4",
        "output/segments/04-he.mp4",
    ];
    let mut tuples = Vec::new();
    let mut source_receipts = Vec::new();
    let mut profiles = Vec::new();
    for (index, (id, output)) in ids.iter().zip(outputs).enumerate() {
        let root = project.join("quality-review/segments").join(id);
        let render_bytes = fs::read(root.join("render.json")).unwrap();
        let render: Value = serde_json::from_slice(&render_bytes).unwrap();
        let video = fs::read(project.join(output)).unwrap();
        tuples.push(serde_json::json!({
            "segment_id": id,
            "ordinal": index + 1,
            "definition_sha256": render["definition_sha256"],
            "dependency_sha256": render["dependency_sha256"],
            "video_sha256": format!("{:x}", Sha256::digest(&video)),
            "bytes": video.len(),
            "duration_seconds": render["duration_seconds"]
        }));
        let stream_profile = render["stream_profile"].clone();
        source_receipts.push(serde_json::json!({
            "segment_id": id,
            "render_sha256": format!("{:x}", Sha256::digest(&render_bytes)),
            "evidence_sha256": format!("{:x}", Sha256::digest(fs::read(root.join("evidence.json")).unwrap())),
            "review_sha256": format!("{:x}", Sha256::digest(fs::read(root.join("review.json")).unwrap())),
            "stream_profile_sha256": canonical_test_digest(&stream_profile)
        }));
        profiles.push(serde_json::json!({
            "segment_id": id,
            "profile": stream_profile
        }));
    }
    let output_stream_profile = serde_json::json!({
        "streams": profiles[0]["profile"]["streams"].clone(),
        "duration_seconds": 40.0
    });
    let receipt = serde_json::json!({
        "schema": "haru.segment_assembly.v1",
        "project": "demo",
        "output": "output/final.pre-loudnorm.mp4",
        "segments": tuples,
        "source_receipts": source_receipts,
        "transition_policy": "cut.v1",
        "audio_policy": "premix.v1",
        "policy_sha256": policy_sha,
        "method": "ffmpeg_concat_copy",
        "target_profile": {
            "container": "mp4",
            "video": {
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "pix_fmt": "yuv420p",
                "frame_rate": "30/1"
            },
            "audio": {
                "codec_name": "aac",
                "sample_rate": 48000,
                "channels": 2,
                "channel_layout": "stereo"
            }
        },
        "source_stream_profiles": profiles,
        "output_stream_profile": output_stream_profile,
        "output_sha256": premix_sha,
        "bytes": premix.len(),
        "duration_seconds": 40.0,
        "decode_evidence": {
            "full_decode_clean": true,
            "packet_dts_monotonic": true,
            "frame_pts_monotonic": true,
            "duration_within_frame_tolerance": true,
            "frame_tolerance_seconds": 1.0 / 30.0
        }
    });
    let receipt_path = project.join("quality-review/segments/assembly.json");
    fs::write(&receipt_path, serde_json::to_vec(&receipt).unwrap()).unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("assembler", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let request = RunNextRequest {
        project_root: project.canonicalize().unwrap(),
        lease: lease_input(&lease),
        runner: "assemble-segments".to_owned(),
        tools_root: None,
    };
    let mut executor = FakeExecutor {
        data: Some(receipt.clone()),
        ..FakeExecutor::default()
    };
    let accepted = run_next(&request, &repo, &mut executor);
    assert_eq!(
        (accepted.outcome.as_str(), accepted.code.as_str()),
        ("ok", "gate_completed")
    );
    assert_eq!(
        executor.calls,
        vec![(
            repo.canonicalize()
                .unwrap()
                .join("tools/segment_assembly.py"),
            vec![project.canonicalize().unwrap().into_os_string()]
        )]
    );

    let mut tampered = receipt.clone();
    tampered["method"] = serde_json::json!("ffmpeg_deterministic_reencode");
    fs::write(&receipt_path, serde_json::to_vec(&tampered).unwrap()).unwrap();
    let mut tampered_executor = FakeExecutor {
        data: Some(tampered),
        ..FakeExecutor::default()
    };
    assert_eq!(
        run_next(&request, &repo, &mut tampered_executor).code,
        "command_failed"
    );

    let mut coordinated = receipt.clone();
    coordinated["method"] = serde_json::json!("ffmpeg_deterministic_reencode");
    coordinated["source_stream_profiles"][1]["profile"]["streams"][0]["width"] =
        serde_json::json!(1280);
    fs::write(&receipt_path, serde_json::to_vec(&coordinated).unwrap()).unwrap();
    let mut coordinated_executor = FakeExecutor {
        data: Some(coordinated),
        ..FakeExecutor::default()
    };
    assert_eq!(
        run_next(&request, &repo, &mut coordinated_executor).code,
        "command_failed"
    );

    let mut target_tampered = receipt.clone();
    target_tampered["target_profile"]["video"]["width"] = serde_json::json!(1280);
    fs::write(&receipt_path, serde_json::to_vec(&target_tampered).unwrap()).unwrap();
    let mut target_executor = FakeExecutor {
        data: Some(target_tampered),
        ..FakeExecutor::default()
    };
    assert_eq!(
        run_next(&request, &repo, &mut target_executor).code,
        "command_failed"
    );

    let mut output_profile_tampered = receipt.clone();
    output_profile_tampered["output_stream_profile"] = serde_json::json!({});
    fs::write(
        &receipt_path,
        serde_json::to_vec(&output_profile_tampered).unwrap(),
    )
    .unwrap();
    let mut output_profile_executor = FakeExecutor {
        data: Some(output_profile_tampered),
        ..FakeExecutor::default()
    };
    assert_eq!(
        run_next(&request, &repo, &mut output_profile_executor).code,
        "command_failed"
    );

    let mut tolerance_tampered = receipt.clone();
    tolerance_tampered["decode_evidence"]["frame_tolerance_seconds"] = serde_json::json!(-1.0);
    fs::write(
        &receipt_path,
        serde_json::to_vec(&tolerance_tampered).unwrap(),
    )
    .unwrap();
    let mut tolerance_executor = FakeExecutor {
        data: Some(tolerance_tampered),
        ..FakeExecutor::default()
    };
    assert_eq!(
        run_next(&request, &repo, &mut tolerance_executor).code,
        "command_failed"
    );

    let mut nested_profile_tampered = receipt.clone();
    nested_profile_tampered["output_stream_profile"]["streams"][0]["extra"] =
        serde_json::json!("not canonical");
    fs::write(
        &receipt_path,
        serde_json::to_vec(&nested_profile_tampered).unwrap(),
    )
    .unwrap();
    let mut nested_profile_executor = FakeExecutor {
        data: Some(nested_profile_tampered),
        ..FakeExecutor::default()
    };
    assert_eq!(
        run_next(&request, &repo, &mut nested_profile_executor).code,
        "command_failed"
    );

    let mut source_profile_extra = receipt.clone();
    source_profile_extra["source_stream_profiles"][0]["profile"]["extra"] = serde_json::json!(true);
    fs::write(
        &receipt_path,
        serde_json::to_vec(&source_profile_extra).unwrap(),
    )
    .unwrap();
    let mut source_profile_executor = FakeExecutor {
        data: Some(source_profile_extra),
        ..FakeExecutor::default()
    };
    assert_eq!(
        run_next(&request, &repo, &mut source_profile_executor).code,
        "command_failed"
    );

    let mut evidence_extra = receipt.clone();
    evidence_extra["decode_evidence"]["extra"] = serde_json::json!(true);
    fs::write(&receipt_path, serde_json::to_vec(&evidence_extra).unwrap()).unwrap();
    let mut evidence_executor = FakeExecutor {
        data: Some(evidence_extra),
        ..FakeExecutor::default()
    };
    assert_eq!(
        run_next(&request, &repo, &mut evidence_executor).code,
        "command_failed"
    );

    fs::remove_file(receipt_path).unwrap();
    let mut missing_executor = FakeExecutor {
        data: Some(receipt),
        ..FakeExecutor::default()
    };
    assert_eq!(
        run_next(&request, &repo, &mut missing_executor).code,
        "command_failed"
    );
}

#[test]
fn produce_artifact_only_promotes_project_staging_through_the_fixed_producer() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/mina-story");
    let staging = project.join(".hvp/staging");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::write(repo.join("scripts/hvp-produce"), "#!/bin/sh\n").unwrap();
    fs::create_dir_all(&staging).unwrap();
    let source = staging.join("issue-brief.md");
    let content = "Duration target: 720-960 seconds\n";
    fs::write(&source, content).unwrap();
    fs::write(project.join("issue_brief.md"), content).unwrap();
    let digest = format!("{:x}", Sha256::digest(content.as_bytes()));
    let receipt = serde_json::json!({
        "schema": "haru.producer_receipt.v1",
        "project": "mina-story",
        "artifact": "issue_brief.md",
        "output_sha256": digest,
        "bytes": content.len(),
    });
    let lease = ProjectStore::new(&project)
        .claim_at("producer", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let request = ProduceArtifactRequest {
        project_root: project.clone(),
        lease: lease_input(&lease),
        artifact: "issue_brief.md".to_owned(),
        source_file: source.clone(),
        produced_by: "codex".to_owned(),
    };
    let mut executor = FakeExecutor {
        data: Some(receipt),
        ..FakeExecutor::default()
    };

    let result = produce_artifact(&request, &repo, &mut executor);

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "artifact_produced")
    );
    assert_eq!(executor.calls[0].1[1], "issue_brief.md");
    assert_eq!(executor.calls[0].1[2], "--from");
    assert_eq!(executor.calls[0].1[3], source.canonicalize().unwrap());
    assert_eq!(executor.calls[0].1[4], "--produced-by");

    let outside = directory.path().join("outside.md");
    fs::write(&outside, content).unwrap();
    let mut invalid = request;
    invalid.source_file = outside;
    let mut blocked_executor = FakeExecutor::default();
    let blocked = produce_artifact(&invalid, &repo, &mut blocked_executor);
    assert_eq!(blocked.code, "invalid_input");
    assert!(blocked_executor.calls.is_empty());
}

#[test]
fn approve_publish_records_only_the_fixed_digest_bound_human_receipt() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/mina-story");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::write(repo.join("scripts/hvp-approve"), "#!/bin/sh\n").unwrap();
    fs::create_dir_all(project.join("publish")).unwrap();
    let final_sha = "a".repeat(64);
    let self_eval_ref = serde_json::json!({
        "path": "quality-review/render-self-eval/render-self-eval.json",
        "sha256": "c".repeat(64),
        "bytes": 2048,
    });
    let visual_qa_review_ref = serde_json::json!({
        "path": "quality-review/visual-sampling/visual-qa-review.json",
        "sha256": "d".repeat(64),
        "bytes": 512,
    });
    fs::write(
        project.join("publish/publish-approval.json"),
        serde_json::to_vec(&serde_json::json!({
            "schema": "haru.publish_approval.v3",
            "project": "mina-story",
            "attestation_ref": "publish-approval-test",
            "approval_intent_sha256": "b".repeat(64),
            "final_sha256": final_sha,
            "channel_id": embedded_channel(),
            "render_self_eval": self_eval_ref,
            "visual_qa_review": visual_qa_review_ref,
        }))
        .unwrap(),
    )
    .unwrap();
    let output = serde_json::json!({
        "schema": "haru.publish_approval.v3",
        "ok": true,
        "state": "publish_approved",
        "approval_intent_sha256": "b".repeat(64),
        "final_sha256": final_sha,
        "channel_id": embedded_channel(),
        "render_self_eval": self_eval_ref,
        "visual_qa_review": visual_qa_review_ref,
    });
    let lease = ProjectStore::new(&project)
        .claim_at("publisher", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let request = ApprovePublishRequest {
        project_root: project.clone(),
        lease: lease_input(&lease),
        attestation_ref: "publish-approval-test".to_owned(),
        override_reason: None,
    };
    let mut executor = FakeExecutor {
        data: Some(output),
        ..FakeExecutor::default()
    };

    let result = approve_publish(&request, &repo, &mut executor);

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "publish_approved")
    );
    assert_eq!(
        executor.calls,
        vec![(
            repo.join("scripts/hvp-approve").canonicalize().unwrap(),
            vec![
                "approve".into(),
                project.canonicalize().unwrap().into_os_string(),
                "--workspace".into(),
                directory.path().canonicalize().unwrap().into_os_string(),
                "--attestation-ref".into(),
                "publish-approval-test".into(),
            ],
        )]
    );
}

#[test]
fn verify_and_prepare_publish_translate_to_fixed_repo_commands() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("projects/mina-story");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::write(repo.join("scripts/verify-project"), "#!/bin/sh\n").unwrap();
    fs::write(repo.join("scripts/make-publish-pack"), "#!/bin/sh\n").unwrap();
    fs::create_dir_all(&project).unwrap();
    let repo = repo.canonicalize().unwrap();
    let project = project.canonicalize().unwrap();
    let workspace = directory.path().canonicalize().unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("publisher", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let lease_input = lease_input(&lease);

    let mut executor = FakeExecutor {
        data: Some(ready_verification("mina-story")),
        ..FakeExecutor::default()
    };
    let verified = verify(&project, &repo, &mut executor);
    let prepared = prepare_publish(&project, &repo, &lease_input, &mut executor);

    assert_eq!(
        (verified.outcome.as_str(), verified.code.as_str()),
        ("ok", "verification_checked")
    );
    assert_eq!(
        (prepared.outcome.as_str(), prepared.code.as_str()),
        ("ok", "publish_prepared")
    );
    assert_eq!(
        executor.calls,
        vec![
            (
                repo.join("scripts/verify-project"),
                vec![
                    project.clone().into_os_string(),
                    "--workspace".into(),
                    workspace.clone().into_os_string(),
                ]
            ),
            (
                repo.join("scripts/make-publish-pack"),
                vec![
                    project.clone().into_os_string(),
                    "--workspace".into(),
                    workspace.clone().into_os_string(),
                    "--write".into(),
                ]
            ),
            (
                repo.join("scripts/verify-project"),
                vec![
                    project.clone().into_os_string(),
                    "--workspace".into(),
                    workspace.into_os_string(),
                ]
            )
        ]
    );

    let mut failed_executor = FakeExecutor {
        exit_code: 7,
        data: Some(serde_json::json!({
            "overall_status": "in_progress",
            "blocker_details": [{"code": "cover_not_passed"}]
        })),
        ..FakeExecutor::default()
    };
    let failed = verify(&project, &repo, &mut failed_executor);
    assert_eq!(
        (failed.outcome.as_str(), failed.code.as_str()),
        ("error", "command_failed")
    );
    assert_eq!(
        failed.data,
        Some(serde_json::json!({
            "overall_status": "in_progress",
            "blocker_details": [{"code": "cover_not_passed"}]
        }))
    );
    assert_eq!(failed_executor.calls.len(), 1);

    let mut contradictory_executor = FakeExecutor {
        data: Some(ready_verification("different-project")),
        ..FakeExecutor::default()
    };
    let contradictory = verify(&project, &repo, &mut contradictory_executor);
    assert_eq!(
        (contradictory.outcome.as_str(), contradictory.code.as_str()),
        ("error", "command_failed")
    );

    let mut wrong_schema = ready_verification("mina-story");
    wrong_schema["schema"] = serde_json::json!("other");
    let mut wrong_schema_executor = FakeExecutor {
        data: Some(wrong_schema),
        ..FakeExecutor::default()
    };
    let wrong_schema_result = verify(&project, &repo, &mut wrong_schema_executor);
    assert_eq!(
        (
            wrong_schema_result.outcome.as_str(),
            wrong_schema_result.code.as_str()
        ),
        ("error", "command_failed")
    );

    let mut approved = ready_verification("mina-story");
    approved["overall_status"] = serde_json::json!("publish_approved");
    let mut approved_executor = FakeExecutor {
        data: Some(approved),
        ..FakeExecutor::default()
    };
    let approved_result = verify(&project, &repo, &mut approved_executor);
    assert_eq!(
        (
            approved_result.outcome.as_str(),
            approved_result.code.as_str()
        ),
        ("ok", "verification_checked")
    );

    let mut invalid_lease = lease_input.clone();
    invalid_lease.generation += 1;
    let mut blocked_executor = FakeExecutor::default();
    let blocked = prepare_publish(&project, &repo, &invalid_lease, &mut blocked_executor);
    assert_eq!(
        (blocked.outcome.as_str(), blocked.code.as_str()),
        ("blocked", "lease_invalid")
    );
    assert!(blocked_executor.calls.is_empty());
}

#[test]
fn prepare_publish_requires_a_lease_before_writing() {
    let directory = tempdir().unwrap();
    let home = directory.path().join("home");
    let project = directory.path().join("project");
    fs::create_dir(&home).unwrap();
    fs::create_dir(&project).unwrap();

    let output = run(
        &home,
        &["app", "prepare-publish", project.to_str().unwrap()],
    );

    assert_eq!(output.status.code(), Some(2));
    assert_eq!(json(&output)["code"], "invalid_input");
    assert!(!project.join("youtube-publish-pack.md").exists());
}

#[test]
fn publish_verifies_before_running_the_upload_and_passes_canonical_credential_reference() {
    struct SequenceExecutor {
        calls: Vec<(PathBuf, Vec<std::ffi::OsString>)>,
        responses: std::collections::VecDeque<CommandResult>,
    }
    impl CommandExecutor for SequenceExecutor {
        fn execute(
            &mut self,
            program: &Path,
            arguments: &[std::ffi::OsString],
        ) -> io::Result<CommandResult> {
            self.calls.push((program.to_path_buf(), arguments.to_vec()));
            self.responses
                .pop_front()
                .ok_or_else(|| io::Error::other("unexpected call"))
        }
    }

    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let projects = directory.path().join("projects");
    let project = projects.join("upload-project");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir_all(&project).unwrap();
    for name in ["verify-project", "upload-youtube"] {
        let path = repo.join("scripts").join(name);
        fs::write(&path, "#!/bin/sh\n").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let lease = ProjectStore::new(&project)
        .claim_at("publisher", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let request = PublishRequest {
        project_root: project.clone(),
        lease: lease_input(&lease),
        idempotency_key: "upload-once".to_owned(),
        runtime: RuntimeBinding {
            runtime_id: format!("sha256:{}", "a".repeat(64)),
            binary_sha256: format!("sha256:{}", "b".repeat(64)),
        },
    };

    let blocked_status = serde_json::json!({
        "schema": "haru.pipeline_status.v1",
        "project": "upload-project",
        "overall_status": "in_progress",
        "blockers": ["qa missing"],
        "blocker_details": [{"code": "qa_not_passed"}],
    });
    let mut blocked = SequenceExecutor {
        calls: Vec::new(),
        responses: [CommandResult {
            exit_code: Some(4),
            data: Some(blocked_status.clone()),
        }]
        .into(),
    };
    let result = publish(&request, &repo, &mut blocked);
    assert_eq!(result.code, "command_failed");
    assert_eq!(blocked.calls.len(), 1);
    assert_eq!(
        blocked.calls[0].0,
        repo.join("scripts/verify-project").canonicalize().unwrap()
    );

    let upload_result = serde_json::json!({
        "schema": "haru.youtube_upload.v1",
        "ok": true,
        "project": "upload-project",
        "status": "complete",
        "video_id": "abcdefghijk",
        "final_sha256": "1".repeat(64),
        "visibility": "unlisted",
        "runtime_id": format!("sha256:{}", "a".repeat(64)),
        "runtime_binary_sha256": format!("sha256:{}", "b".repeat(64)),
        "channel_id": embedded_channel(),
    });
    let mut ready = SequenceExecutor {
        calls: Vec::new(),
        responses: [
            CommandResult {
                exit_code: Some(0),
                data: Some(ready_verification("upload-project")),
            },
            CommandResult {
                exit_code: Some(0),
                data: Some(upload_result.clone()),
            },
        ]
        .into(),
    };
    let result = publish(&request, &repo, &mut ready);
    assert_eq!(result.code, "youtube_uploaded");
    assert_eq!(result.data, Some(upload_result.clone()));
    assert_eq!(ready.calls.len(), 2);
    assert_eq!(
        ready.calls[1].0,
        repo.join("scripts/upload-youtube").canonicalize().unwrap()
    );
    let upload_args = &ready.calls[1].1;
    assert_eq!(
        upload_args,
        &[
            project.canonicalize().unwrap().into_os_string(),
            "--workspace".into(),
            directory.path().canonicalize().unwrap().into_os_string(),
            "--credential-reference".into(),
            format!("youtube:{}", embedded_channel()).into(),
            "--idempotency-key".into(),
            "upload-once".into(),
            "--runtime-id".into(),
            format!("sha256:{}", "a".repeat(64)).into(),
            "--runtime-binary-sha256".into(),
            format!("sha256:{}", "b".repeat(64)).into(),
        ]
    );

    let mut private_upload_result = upload_result.clone();
    private_upload_result["visibility"] = serde_json::json!("private");
    let mut private = SequenceExecutor {
        calls: Vec::new(),
        responses: [
            CommandResult {
                exit_code: Some(0),
                data: Some(ready_verification("upload-project")),
            },
            CommandResult {
                exit_code: Some(0),
                data: Some(private_upload_result.clone()),
            },
        ]
        .into(),
    };
    let rejected = publish(&request, &repo, &mut private);
    assert_eq!(
        (rejected.outcome.as_str(), rejected.code.as_str()),
        ("error", "command_failed")
    );
    assert_eq!(rejected.data, Some(private_upload_result));
    assert_eq!(private.calls.len(), 2);

    let thumbnail_result = serde_json::json!({
        "schema": "haru.youtube_thumbnail.v1",
        "ok": true,
        "project": "upload-project",
        "status": "complete",
        "video_id": "abcdefghijk",
        "cover_sha256": "2".repeat(64),
        "runtime_id": format!("sha256:{}", "a".repeat(64)),
        "runtime_binary_sha256": format!("sha256:{}", "b".repeat(64)),
    });
    let thumbnail_request = ReplaceThumbnailRequest {
        project_root: project,
        lease: request.lease,
        updated_by: "harvey".to_owned(),
        idempotency_key: "thumbnail-once".to_owned(),
        runtime: RuntimeBinding {
            runtime_id: format!("sha256:{}", "a".repeat(64)),
            binary_sha256: format!("sha256:{}", "b".repeat(64)),
        },
    };
    let mut thumbnail = SequenceExecutor {
        calls: Vec::new(),
        responses: [
            CommandResult {
                exit_code: Some(0),
                data: Some(ready_verification("upload-project")),
            },
            CommandResult {
                exit_code: Some(0),
                data: Some(thumbnail_result.clone()),
            },
        ]
        .into(),
    };
    let replaced = replace_thumbnail(&thumbnail_request, &repo, &mut thumbnail);
    assert_eq!(replaced.code, "youtube_thumbnail_replaced");
    assert_eq!(replaced.data, Some(thumbnail_result));
    let thumbnail_args = &thumbnail.calls[1].1;
    assert!(thumbnail_args.contains(&std::ffi::OsString::from("--thumbnail-only")));
    assert!(thumbnail_args.windows(2).any(|pair| {
        pair[0] == "--credential-reference"
            && pair[1] == OsString::from(format!("youtube:{}", embedded_channel()))
    }));
    assert!(
        !thumbnail_args
            .iter()
            .any(|argument| argument == "--oauth-token-file")
    );

    let reconcile_result = serde_json::json!({
        "schema": "haru.youtube_upload_reconcile.v1",
        "ok": true,
        "project": "upload-project",
        "status": "complete",
        "code": "youtube_upload_reconciled",
        "video_id": "abcdefghijk",
        "final_sha256": "1".repeat(64),
        "visibility": "unlisted",
        "runtime_id": format!("sha256:{}", "a".repeat(64)),
        "runtime_binary_sha256": format!("sha256:{}", "b".repeat(64)),
        "channel_id": embedded_channel(),
    });
    let reconcile_request = ReconcileUploadRequest {
        project_root: thumbnail_request.project_root.clone(),
        lease: thumbnail_request.lease.clone(),
        idempotency_key: "reconcile-once".to_owned(),
        override_attestation_ref: None,
        runtime: thumbnail_request.runtime.clone(),
    };
    let mut reconcile = SequenceExecutor {
        calls: Vec::new(),
        responses: [
            CommandResult {
                exit_code: Some(0),
                data: Some(ready_verification("upload-project")),
            },
            CommandResult {
                exit_code: Some(0),
                data: Some(reconcile_result.clone()),
            },
        ]
        .into(),
    };
    let reconciled = reconcile_upload(&reconcile_request, &repo, &mut reconcile);
    assert_eq!(reconciled.code, "youtube_upload_reconciled");
    assert_eq!(reconciled.data, Some(reconcile_result));
    let reconcile_args = &reconcile.calls[1].1;
    assert!(reconcile_args.contains(&std::ffi::OsString::from("--reconcile")));
    assert!(reconcile_args.windows(2).any(|pair| {
        pair[0] == "--credential-reference"
            && pair[1] == OsString::from(format!("youtube:{}", embedded_channel()))
    }));
    assert!(
        !reconcile_args
            .iter()
            .any(|argument| argument == "--thumbnail-only" || argument == "--oauth-token-file")
    );
}

#[test]
fn app_create_select_and_status_use_only_explicit_neutral_roots() {
    let directory = tempdir().unwrap();
    let home = directory.path().join("isolated-home");
    let projects = directory.path().join("neutral-projects");
    fs::create_dir(&home).unwrap();
    fs::create_dir(&projects).unwrap();
    fs::create_dir(home.join(".openclaw")).unwrap();
    fs::create_dir(home.join(".hermes")).unwrap();
    fs::write(home.join(".openclaw/secret"), "OPENCLAW_SECRET").unwrap();
    fs::write(home.join(".hermes/secret"), "HERMES_SECRET").unwrap();

    let create = run(
        &home,
        &["app", "create", projects.to_str().unwrap(), "mina-story"],
    );
    assert!(create.status.success(), "{create:?}");
    let created = json(&create);
    assert_eq!(created["schema_version"], 1);
    assert_eq!(created["outcome"], "ok");
    assert_eq!(created["code"], "project_created");
    assert_eq!(
        created["project"],
        projects
            .canonicalize()
            .unwrap()
            .join("mina-story")
            .to_str()
            .unwrap()
    );

    let select = run(
        &home,
        &["app", "select", projects.to_str().unwrap(), "mina-story"],
    );
    assert!(select.status.success(), "{select:?}");
    assert_eq!(json(&select)["code"], "project_selected");

    let record_selection = run(
        &home,
        &[
            "app",
            "record-selection",
            projects.join("mina-story").to_str().unwrap(),
            "--cron-run-id",
            "topic-20260729",
            "--candidate-id",
            "candidate-1",
            "--chosen-by",
            "harvey",
            "--chosen-at",
            "1785254400",
        ],
    );
    assert!(record_selection.status.success(), "{record_selection:?}");
    assert_eq!(json(&record_selection)["code"], "selection_recorded");

    let status = run(
        &home,
        &[
            "app",
            "status",
            projects.join("mina-story").to_str().unwrap(),
        ],
    );
    assert!(status.status.success(), "{status:?}");
    let status_json = json(&status);
    assert_eq!(status_json["code"], "status");
    assert_eq!(status_json["data"]["schema_version"], 1);
    assert_eq!(status_json["data"]["project"], "mina-story");
    assert_eq!(
        status_json["data"]["selection"]["candidate_id"],
        "candidate-1"
    );
    let rendered = String::from_utf8(status.stdout).unwrap();
    assert!(!rendered.contains("OPENCLAW_SECRET"));
    assert!(!rendered.contains("HERMES_SECRET"));

    let existing = run(
        &home,
        &["app", "create", projects.to_str().unwrap(), "mina-story"],
    );
    assert_eq!(existing.status.code(), Some(2));
    assert_eq!(json(&existing)["code"], "invalid_input");

    let traversal = run(&home, &["app", "select", projects.to_str().unwrap(), ".."]);
    assert_eq!(traversal.status.code(), Some(2));
    assert_eq!(json(&traversal)["code"], "invalid_input");

    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(&projects, directory.path().join("projects-link")).unwrap();
        let symlink = run(
            &home,
            &[
                "app",
                "select",
                directory.path().join("projects-link").to_str().unwrap(),
                "mina-story",
            ],
        );
        assert_eq!(symlink.status.code(), Some(2));
        assert_eq!(json(&symlink)["code"], "invalid_input");
    }

    let missing_argument = run(&home, &["app", "status"]);
    assert_eq!(missing_argument.status.code(), Some(2));
    assert!(missing_argument.stderr.is_empty());
    assert_eq!(json(&missing_argument)["code"], "invalid_input");

    fs::create_dir_all(projects.join("mina-story/.hvp")).unwrap();
    fs::write(projects.join("mina-story/.hvp/lease.json"), "{broken").unwrap();
    let corrupt_state = run(
        &home,
        &[
            "app",
            "status",
            projects.join("mina-story").to_str().unwrap(),
        ],
    );
    assert_eq!(corrupt_state.status.code(), Some(5));
    assert_eq!(json(&corrupt_state)["code"], "internal_error");
}

#[cfg(unix)]
#[test]
fn shell_entrypoint_ignores_path_bash_with_an_isolated_home() {
    let directory = tempdir().unwrap();
    let home = directory.path().join("home");
    let bin = directory.path().join("bin");
    let projects = directory.path().join("projects");
    let marker = directory.path().join("bash-ran");
    fs::create_dir(&home).unwrap();
    fs::create_dir(&bin).unwrap();
    fs::create_dir(&projects).unwrap();
    let bash = bin.join("bash");
    fs::write(
        &bash,
        "#!/bin/sh\n: > \"$HVP_TEST_BASH_MARKER\"\nexec /bin/bash \"$@\"\n",
    )
    .unwrap();
    fs::set_permissions(&bash, fs::Permissions::from_mode(0o755)).unwrap();

    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let output = Command::new(repo.join("scripts/hvp"))
        .args(["create", projects.to_str().unwrap(), "path-safe-project"])
        .env("HOME", &home)
        .env("PATH", format!("{}:/usr/bin:/bin", bin.display()))
        .env("HVP_TEST_BASH_MARKER", &marker)
        .output()
        .unwrap();

    assert!(output.status.success(), "{output:?}");
    assert!(output.stderr.is_empty());
    assert_eq!(json(&output)["code"], "project_created");
    assert!(!marker.exists());
}

#[cfg(unix)]
#[test]
fn shell_entrypoint_ignores_bash_env_startup() {
    let directory = tempdir().unwrap();
    let home = directory.path().join("home");
    let projects = directory.path().join("projects");
    let startup = directory.path().join("bash-env");
    let marker = directory.path().join("bash-env-ran");
    fs::create_dir(&home).unwrap();
    fs::create_dir(&projects).unwrap();
    fs::write(
        &startup,
        "printf 'PRIVATE_STARTUP_LEAK\\n'\n: > \"$HVP_TEST_BASH_ENV_MARKER\"\n",
    )
    .unwrap();

    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let output = Command::new(repo.join("scripts/hvp"))
        .args(["create", projects.to_str().unwrap(), "startup-safe-project"])
        .env("HOME", &home)
        .env("BASH_ENV", &startup)
        .env("HVP_TEST_BASH_ENV_MARKER", &marker)
        .output()
        .unwrap();

    assert!(output.status.success(), "{output:?}");
    assert!(output.stderr.is_empty(), "{output:?}");
    assert!(!marker.exists(), "{output:?}");
    assert_eq!(json(&output)["code"], "project_created");
}

#[cfg(unix)]
#[test]
fn shell_entrypoint_ignores_exported_shell_functions() {
    let directory = tempdir().unwrap();
    let home = directory.path().join("home");
    let projects = directory.path().join("projects");
    let marker = directory.path().join("shell-function-ran");
    fs::create_dir(&home).unwrap();
    fs::create_dir(&projects).unwrap();

    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let output = Command::new(repo.join("scripts/hvp"))
        .args([
            "create",
            projects.to_str().unwrap(),
            "shell-function-safe-project",
        ])
        .env("HOME", &home)
        .env(
            "BASH_FUNC_cd%%",
            "() { : > \"$HVP_TEST_SHELL_FUNCTION_MARKER\"; builtin cd \"$@\"; }",
        )
        .env("HVP_TEST_SHELL_FUNCTION_MARKER", &marker)
        .output()
        .unwrap();

    assert!(!marker.exists(), "{output:?}");
    assert!(output.status.success(), "{output:?}");
    assert!(output.stderr.is_empty(), "{output:?}");
    assert_eq!(json(&output)["code"], "project_created");
}

#[test]
fn shell_entrypoint_runs_with_an_isolated_home() {
    let directory = tempdir().unwrap();
    let home = directory.path().join("home");
    let projects = directory.path().join("projects");
    fs::create_dir(&home).unwrap();
    fs::create_dir(&projects).unwrap();

    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let output = Command::new(repo.join("scripts/hvp"))
        .args(["create", projects.to_str().unwrap(), "isolated-project"])
        .env("HOME", &home)
        .env_remove("CARGO_HOME")
        .env_remove("RUSTUP_HOME")
        .output()
        .unwrap();

    assert!(output.status.success(), "{output:?}");
    assert!(output.stderr.is_empty(), "{output:?}");
    assert_eq!(json(&output)["code"], "project_created");
}

#[cfg(unix)]
#[test]
fn shell_entrypoint_fails_as_json_without_the_built_binary() {
    let directory = tempdir().unwrap();
    let scripts = directory.path().join("scripts");
    fs::create_dir(&scripts).unwrap();
    let source = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("scripts/hvp-state");
    let entrypoint = scripts.join("hvp-state");
    fs::copy(source, &entrypoint).unwrap();
    fs::set_permissions(&entrypoint, fs::Permissions::from_mode(0o755)).unwrap();

    let output = Command::new(entrypoint)
        .args(["app", "status", "/tmp/project"])
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(5));
    assert!(output.stderr.is_empty());
    assert_eq!(json(&output)["code"], "runner_unavailable");
}

#[cfg(unix)]
#[test]
fn process_executor_clears_the_parent_home() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("project");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir(&project).unwrap();
    let script = repo.join("scripts/verify-project");
    fs::write(
        &script,
        "#!/bin/bash\n[[ -z \"${HOME:-}\" ]] || exit 9\nprintf '%s\\n' '{\"schema\":\"haru.pipeline_status.v1\",\"project\":\"project\",\"overall_status\":\"ready_for_human_upload_approval\",\"blockers\":[],\"blocker_details\":[]}'\n",
    )
    .unwrap();
    fs::set_permissions(&script, fs::Permissions::from_mode(0o755)).unwrap();

    let result = verify(&project, &repo, &mut ProcessExecutor);

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "verification_checked")
    );
}

#[test]
fn review_feedback_is_read_only_and_resolution_is_digest_bound_without_approval_effects() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let project = directory.path().join("workspace/projects/reviewed-video");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir_all(&project).unwrap();
    fs::write(repo.join("scripts/review-feedback"), "#!/bin/sh\n").unwrap();
    let project = project.canonicalize().unwrap();
    let comment_id = "00000000-0000-4000-8000-000000000001";
    let package_id = "a".repeat(64);
    let asset_sha = "b".repeat(64);
    let comment = serde_json::json!({
        "id": comment_id,
        "status": "open",
        "is_current": true,
        "asset_available": true,
        "stale": false,
    });
    let feedback = serde_json::json!({
        "schema": "video_studio.review_feedback.v1",
        "outcome": "ok",
        "code": "review_feedback",
        "project": "reviewed-video",
        "package_id": package_id,
        "counts": {"open": 1, "resolved": 0},
        "comments": [comment],
    });
    let mut read_executor = FakeExecutor {
        data: Some(feedback),
        ..FakeExecutor::default()
    };
    let read = application::review_feedback(&project, &repo, &mut read_executor);
    assert_eq!(
        (read.outcome.as_str(), read.code.as_str()),
        ("ok", "review_feedback")
    );
    assert_eq!(
        read_executor.calls[0].1,
        [OsString::from("read"), project.clone().into_os_string()]
    );

    let lease = ProjectStore::new(&project)
        .claim_at("reviewer", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let request = ReviewResolutionRequest {
        project_root: project.clone(),
        lease: lease_input(&lease),
        comment_id: comment_id.to_owned(),
        status: "resolved".to_owned(),
        expected_package_id: package_id.clone(),
        expected_asset_sha256: asset_sha.clone(),
    };
    let resolved = serde_json::json!({
        "schema": "video_studio.review_resolution.v1",
        "outcome": "ok",
        "code": "review_comment_updated",
        "project": "reviewed-video",
        "package_id": package_id,
        "comment": {
            "id": comment_id,
            "status": "resolved",
        },
        "effects": {
            "technical_qa_approved": false,
            "human_approval": false,
            "publishing": false,
        },
    });
    let mut resolve_executor = FakeExecutor {
        data: Some(resolved.clone()),
        ..FakeExecutor::default()
    };
    let result = application::review_resolve(&request, &repo, &mut resolve_executor);
    assert_eq!(result.code, "review_comment_updated");
    assert_eq!(
        resolve_executor.calls[0].1,
        [
            OsString::from("resolve"),
            project.clone().into_os_string(),
            OsString::from("--comment-id"),
            OsString::from(comment_id),
            OsString::from("--status"),
            OsString::from("resolved"),
            OsString::from("--expected-package-id"),
            OsString::from(&request.expected_package_id),
            OsString::from("--expected-asset-sha256"),
            OsString::from(&request.expected_asset_sha256),
        ]
    );

    let mut dishonest = resolved;
    dishonest["effects"]["human_approval"] = serde_json::json!(true);
    let mut dishonest_executor = FakeExecutor {
        data: Some(dishonest),
        ..FakeExecutor::default()
    };
    assert_eq!(
        application::review_resolve(&request, &repo, &mut dishonest_executor).code,
        "command_failed"
    );
}

#[cfg(unix)]
#[test]
fn source_mismatch_fails_closed_even_when_mtimes_look_current() {
    let directory = tempdir().unwrap();
    let repo = directory.path().join("repo");
    let scripts = repo.join("scripts");
    let manifest = repo.join("pipeline");
    let target = manifest.join("target/debug");
    let source = manifest.join("src");
    let project = directory.path().join("project");
    fs::create_dir_all(&scripts).unwrap();
    fs::create_dir_all(&target).unwrap();
    fs::create_dir_all(&source).unwrap();
    fs::create_dir(&project).unwrap();

    let actual_repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let entrypoint = scripts.join("hvp-state");
    fs::copy(actual_repo.join("scripts/hvp-state"), &entrypoint).unwrap();
    fs::set_permissions(&entrypoint, fs::Permissions::from_mode(0o755)).unwrap();
    let binary = target.join("hvp-state");
    fs::copy(env!("CARGO_BIN_EXE_hvp-state"), &binary).unwrap();
    fs::set_permissions(&binary, fs::Permissions::from_mode(0o755)).unwrap();

    let stale_sources = [
        manifest.join("Cargo.toml"),
        manifest.join("Cargo.lock"),
        repo.join("rust-toolchain.toml"),
        source.join("lib.rs"),
    ];
    for path in &stale_sources {
        fs::write(path, "not the built source").unwrap();
        let file = fs::File::options().write(true).open(path).unwrap();
        file.set_times(
            fs::FileTimes::new().set_modified(std::time::UNIX_EPOCH + Duration::from_secs(1)),
        )
        .unwrap();
    }

    let output = Command::new(entrypoint)
        .args(["app", "status", project.to_str().unwrap()])
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(5), "{output:?}");
    assert!(output.stderr.is_empty());
    assert_eq!(json(&output)["code"], "runner_unavailable");
}

#[cfg(unix)]
#[test]
fn copied_binary_executes_only_the_validated_checkout() {
    let directory = tempdir().unwrap();
    let actual_repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let copied_repo = directory.path().join("repo");
    let copied_manifest = copied_repo.join("pipeline");
    let copied_target = copied_manifest.join("target/debug");
    let project = directory.path().join("project");
    fs::create_dir_all(&copied_target).unwrap();
    fs::create_dir(&project).unwrap();

    for relative in [
        "pipeline/Cargo.toml",
        "pipeline/Cargo.lock",
        "pipeline/build.rs",
        "pipeline/source_fingerprint.rs",
        "rust-toolchain.toml",
        "scripts/hvp",
        "scripts/hvp-state",
        "scripts/verify-project",
        "scripts/render-project",
        "scripts/render-project-detached",
        "scripts/make-publish-pack",
        "scripts/upload-youtube",
        "tools/agent_status.py",
        "tools/render_contract.py",
        "tools/render_project.py",
        "tools/render_project_worker.py",
        "tools/mix_final.py",
        "tools/visual_qa_sample.py",
        "tools/make_publish_pack.py",
        "tools/youtube_upload.py",
    ] {
        let source = actual_repo.join(relative);
        let destination = copied_repo.join(relative);
        fs::create_dir_all(destination.parent().unwrap()).unwrap();
        fs::copy(source, destination).unwrap();
    }
    copy_tree(
        &actual_repo.join("pipeline/src"),
        &copied_manifest.join("src"),
    );
    let binary = copied_target.join("hvp-state");
    fs::copy(env!("CARGO_BIN_EXE_hvp-state"), &binary).unwrap();
    fs::set_permissions(&binary, fs::Permissions::from_mode(0o755)).unwrap();
    fs::set_permissions(
        copied_repo.join("scripts/hvp-state"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    fs::set_permissions(
        copied_repo.join("scripts/verify-project"),
        fs::Permissions::from_mode(0o644),
    )
    .unwrap();

    let output = Command::new(copied_repo.join("scripts/hvp-state"))
        .args(["app", "verify", project.to_str().unwrap()])
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(5), "{output:?}");
    assert!(output.stderr.is_empty());
    assert_eq!(json(&output)["code"], "runner_unavailable");
}

#[cfg(unix)]
#[test]
fn python_runner_ignores_unreviewed_startup_modules() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("project");
    let startup = directory.path().join("startup");
    let marker = directory.path().join("startup-ran");
    fs::create_dir(&project).unwrap();
    fs::create_dir(&startup).unwrap();
    fs::write(
        startup.join("sitecustomize.py"),
        format!(
            "from pathlib import Path\nPath({:?}).write_text('ran')\n",
            marker
        ),
    )
    .unwrap();

    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let _output = Command::new(repo.join("scripts/verify-project"))
        .arg(&project)
        .env("PYTHONPATH", &startup)
        .output()
        .unwrap();

    assert!(!marker.exists());
}

const SELF_EVAL_ROOT: &str = "quality-review/render-self-eval";

/// Write one project-contained file and return the exact `{path,sha256,bytes}`
/// ref the projection has to name it by.
fn self_eval_ref(project: &Path, relative: &str, value: &Value) -> Value {
    let path = project.join(relative);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    let bytes = serde_json::to_vec(value).unwrap();
    fs::write(&path, &bytes).unwrap();
    serde_json::json!({
        "path": relative,
        "sha256": format!("{:x}", Sha256::digest(&bytes)),
        "bytes": bytes.len(),
    })
}

fn self_eval_identity(attempt: u64) -> String {
    format!(
        "{:x}",
        Sha256::digest(format!("self-eval-attempt-{attempt}").as_bytes())
    )
}

/// A complete, internally consistent current projection with every ref it names
/// actually on disk -- exactly the history the engine would have promoted.
fn write_self_eval_state(project: &Path, status: &str, attempt: u64) -> Value {
    let identity = self_eval_identity(attempt);
    let boundary_policy = self_eval_ref(
        project,
        &format!("{SELF_EVAL_ROOT}/boundary-policy.json"),
        &serde_json::json!({
            "schema": "haru.render_self_eval_boundary_policy.v1",
            "algorithm": "haru.render_self_eval_policy.v1",
        }),
    );
    let boundary_plan = self_eval_ref(
        project,
        &format!("{SELF_EVAL_ROOT}/boundary-plan.json"),
        &serde_json::json!({
            "schema": "haru.render_self_eval_boundary_plan.v1",
            "attempt_identity": identity,
        }),
    );
    let attempt_dir = format!("{SELF_EVAL_ROOT}/attempts/attempt-{attempt:02}");
    let evaluation = self_eval_ref(
        project,
        &format!("{attempt_dir}/evaluation.json"),
        &serde_json::json!({
            "schema": "haru.render_self_eval_attempt.v1",
            "attempt": attempt,
            "attempt_identity": identity,
            "status": if matches!(status, "needs_human" | "pass") { "clean" } else { "fail" },
        }),
    );
    let evidence_index = self_eval_ref(
        project,
        &format!("{attempt_dir}/evidence-index.json"),
        &serde_json::json!({
            "schema": "haru.render_self_eval_evidence_index.v1",
            "attempt_identity": identity,
            "total_available_bytes": 4096,
        }),
    );
    // A pass is always reviewed; a deterministic failure never is.
    let review = if status == "pass" {
        self_eval_ref(
            project,
            &format!("{attempt_dir}/review.json"),
            &serde_json::json!({
                "schema": "haru.render_self_eval_review.v1",
                "attempt_identity": identity,
                "verdict": "pass",
            }),
        )
    } else {
        Value::Null
    };
    let sealed = match status {
        "needs_human" => None,
        "pass" => Some("pass"),
        _ => Some("fail"),
    };
    // The exhausted lane is three sealed attempts, so the earlier ordinals carry
    // their own sealed failures rather than being implied by the projection.
    if status == "human_intervention_required" {
        for earlier in 1..attempt {
            let earlier_identity = self_eval_identity(earlier);
            self_eval_ref(
                project,
                &format!("{SELF_EVAL_ROOT}/attempts/attempt-{earlier:02}/outcome.json"),
                &serde_json::json!({
                    "schema": "haru.render_self_eval_outcome.v1",
                    "attempt": earlier,
                    "attempt_identity": earlier_identity,
                    "source": "deterministic",
                    "verdict": "fail",
                }),
            );
        }
    }
    let outcome = match sealed {
        Some(verdict) => self_eval_ref(
            project,
            &format!("{attempt_dir}/outcome.json"),
            &serde_json::json!({
                "schema": "haru.render_self_eval_outcome.v1",
                "attempt": attempt,
                "attempt_identity": identity,
                "source": if verdict == "pass" { "vision" } else { "deterministic" },
                "verdict": verdict,
                "evaluation": evaluation,
                "evidence_index": evidence_index,
                "review": review,
                "findings": [],
                "authority": if verdict == "pass" {
                    serde_json::json!({
                        "schema": "haru.self_eval_vision_attestation.v1",
                        "attestation_ref": "self-eval-attestation:self-eval-vision-0001",
                        "review_intent_sha256": "e".repeat(64),
                        "nonce": "nonce-1",
                        "generation": 1,
                        "consumed_at": "2026-08-14T00:00:00Z",
                    })
                } else {
                    Value::Null
                },
                "sealed_at": "2026-08-14T00:00:00Z",
            }),
        ),
        None => Value::Null,
    };
    let projection = serde_json::json!({
        "schema": "haru.render_self_eval.v1",
        "project": project.file_name().unwrap().to_str().unwrap(),
        "status": status,
        "verdict": status,
        "attempt": attempt,
        "max_attempts": 3,
        "attempt_identity": identity,
        "inputs": [],
        "boundary_policy": boundary_policy,
        "boundary_plan": boundary_plan,
        "evaluation": evaluation,
        "evidence_index": evidence_index,
        "review": review,
        "outcome": outcome,
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
    let path = project.join(SELF_EVAL_ROOT).join("render-self-eval.json");
    fs::write(&path, serde_json::to_vec(&projection).unwrap()).unwrap();
    projection
}

fn read_self_eval_state(project: &Path) -> Option<Value> {
    let bytes = fs::read(project.join(SELF_EVAL_ROOT).join("render-self-eval.json")).ok()?;
    serde_json::from_slice(&bytes).ok()
}

/// Stands in for the engine. On each call it promotes the projection the engine
/// would have written and answers with exactly those bytes, so `reused` is
/// exercised the way the real lane produces it rather than being asserted.
struct SelfEvalExecutor {
    calls: Vec<(PathBuf, Vec<std::ffi::OsString>)>,
    project: PathBuf,
    /// One entry consumed per call. `None` promotes nothing, which is what an
    /// idempotent replay of an already-recorded transition looks like.
    promotions: std::collections::VecDeque<Option<(String, u64)>>,
    /// Overrides the answered payload so stdout can be made to disagree with
    /// the bytes that are actually on disk.
    answer: Option<Value>,
    exit_code: i32,
}

impl SelfEvalExecutor {
    fn new(project: &Path, promotions: Vec<Option<(&str, u64)>>) -> Self {
        Self {
            calls: Vec::new(),
            project: project.to_path_buf(),
            promotions: promotions
                .into_iter()
                .map(|entry| entry.map(|(status, attempt)| (status.to_owned(), attempt)))
                .collect(),
            answer: None,
            exit_code: 0,
        }
    }
}

impl CommandExecutor for SelfEvalExecutor {
    fn execute(
        &mut self,
        program: &Path,
        arguments: &[std::ffi::OsString],
    ) -> io::Result<CommandResult> {
        self.calls.push((program.to_path_buf(), arguments.to_vec()));
        let promoted = match self.promotions.pop_front().flatten() {
            Some((status, attempt)) => Some(write_self_eval_state(&self.project, &status, attempt)),
            None => read_self_eval_state(&self.project),
        };
        Ok(CommandResult {
            exit_code: Some(self.exit_code),
            data: self.answer.clone().or(promoted),
        })
    }
}

fn self_eval_project(directory: &Path, slug: &str) -> (PathBuf, PathBuf) {
    let repo = directory.join("repo");
    let project = directory.join("projects").join(slug);
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::write(repo.join("scripts/render-self-eval"), "#!/bin/sh\n").unwrap();
    fs::create_dir_all(project.join(SELF_EVAL_ROOT)).unwrap();
    (repo, project)
}

fn self_eval_run_request(project: &Path, lease: &pipeline::Lease) -> RunNextRequest {
    RunNextRequest {
        project_root: project.canonicalize().unwrap(),
        lease: lease_input(lease),
        runner: "render-self-eval".to_owned(),
        tools_root: None,
    }
}

fn vision_review(findings: Vec<pipeline::application::SelfEvalFinding>) -> SelfEvalReviewInput {
    SelfEvalReviewInput {
        reviewer_kind: "vision".to_owned(),
        provider: "google".to_owned(),
        model: "gemini-2.5-pro".to_owned(),
        capability: "video_understanding.v1".to_owned(),
        findings,
        attestation_ref: "self-eval-attestation:self-eval-vision-0001".to_owned(),
    }
}

fn finding(
    timestamp_seconds: f64,
    boundary_id: &str,
    category: &str,
) -> pipeline::application::SelfEvalFinding {
    pipeline::application::SelfEvalFinding {
        timestamp_seconds,
        boundary_id: boundary_id.to_owned(),
        category: category.to_owned(),
        severity: "high".to_owned(),
        message: "reviewer reported a boundary defect".to_owned(),
    }
}

fn self_eval_review_request(
    project: &Path,
    lease: &pipeline::Lease,
    verdict: &str,
    review: SelfEvalReviewInput,
) -> VisualQaRequest {
    VisualQaRequest {
        project_root: project.canonicalize().unwrap(),
        lease: lease_input(lease),
        action: "render-self-eval-review".to_owned(),
        reviewed_by: Some("vision-agent".to_owned()),
        verdict: Some(verdict.to_owned()),
        notes: Some(String::new()),
        self_eval: Some(review),
    }
}

#[test]
fn self_eval_runner_is_fixed_and_takes_no_caller_path_or_tools_root() {
    let directory = tempdir().unwrap();
    let (repo, project) = self_eval_project(directory.path(), "mina-story");
    let lease = ProjectStore::new(&project)
        .claim_at("agent", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let request = self_eval_run_request(&project, &lease);
    let mut executor = SelfEvalExecutor::new(
        &project.canonicalize().unwrap(),
        vec![Some(("needs_human", 1)), None],
    );

    let first = run_next(&request, &repo, &mut executor);

    // The engine is invoked as exactly `evaluate <project>`: two argv values,
    // no tools root, and nothing a caller chose.
    assert_eq!(
        executor.calls[0],
        (
            repo.canonicalize()
                .unwrap()
                .join("scripts/render-self-eval"),
            vec![
                std::ffi::OsString::from("evaluate"),
                project.canonicalize().unwrap().into_os_string(),
            ]
        )
    );
    assert_eq!(
        (first.outcome.as_str(), first.code.as_str()),
        ("ok", "self_eval_needs_review")
    );
    let data = first.data.as_ref().unwrap();
    assert_eq!(data.get("reused"), Some(&Value::Bool(false)));
    assert_eq!(
        data.get("result").unwrap().get("schema"),
        Some(&Value::String("haru.render_self_eval.v1".to_owned()))
    );

    // A replay changes no bytes, so it reports the same state with reuse as
    // metadata rather than as a second state or a second code.
    let replay = run_next(&request, &repo, &mut executor);

    assert_eq!(
        (replay.outcome.as_str(), replay.code.as_str()),
        ("ok", "self_eval_needs_review")
    );
    assert_eq!(
        replay.data.as_ref().unwrap().get("reused"),
        Some(&Value::Bool(true))
    );
    assert_eq!(
        replay.data.as_ref().unwrap().get("result"),
        first.data.as_ref().unwrap().get("result")
    );

    // A tools root is refused outright rather than ignored, and refusal happens
    // before the engine is reached.
    let mut refused = SelfEvalExecutor::new(&project.canonicalize().unwrap(), vec![]);
    let with_tools_root = RunNextRequest {
        tools_root: Some(repo.clone()),
        ..self_eval_run_request(&project, &lease)
    };

    let result = run_next(&with_tools_root, &repo, &mut refused);

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("error", "invalid_input")
    );
    assert!(refused.calls.is_empty());
}

#[test]
fn self_eval_maps_every_current_state_to_exactly_one_code() {
    for (status, attempt, expected) in [
        ("needs_human", 1_u64, "self_eval_needs_review"),
        ("fail", 1, "self_eval_failed"),
        ("fail", 2, "self_eval_failed"),
        ("pass", 2, "self_eval_passed"),
        (
            "human_intervention_required",
            3,
            "human_intervention_required",
        ),
    ] {
        let directory = tempdir().unwrap();
        let (repo, project) = self_eval_project(directory.path(), "mina-story");
        let lease = ProjectStore::new(&project)
            .claim_at("agent", Duration::from_secs(60), SystemTime::now())
            .unwrap();
        let mut executor = SelfEvalExecutor::new(
            &project.canonicalize().unwrap(),
            vec![Some((status, attempt))],
        );

        let result = run_next(
            &self_eval_run_request(&project, &lease),
            &repo,
            &mut executor,
        );

        assert_eq!(
            (result.outcome.as_str(), result.code.as_str()),
            ("ok", expected),
            "state {status} at ordinal {attempt}"
        );
    }
}

#[test]
fn self_eval_cannot_report_a_code_the_current_bytes_do_not_justify() {
    // A sealed failure at the last ordinal is the exhausted state and nothing
    // else: the projection may not describe itself as an ordinary failure.
    let directory = tempdir().unwrap();
    let (repo, project) = self_eval_project(directory.path(), "mina-story");
    let canonical = project.canonicalize().unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("agent", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let mut executor = SelfEvalExecutor::new(&canonical, vec![Some(("fail", 3))]);

    let result = run_next(
        &self_eval_run_request(&project, &lease),
        &repo,
        &mut executor,
    );

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("error", "command_failed")
    );

    // Claiming exhaustion without three sealed attempts on disk is refused too.
    let directory = tempdir().unwrap();
    let (repo, project) = self_eval_project(directory.path(), "mina-story");
    let canonical = project.canonicalize().unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("agent", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    write_self_eval_state(&canonical, "human_intervention_required", 3);
    fs::remove_file(canonical.join(format!("{SELF_EVAL_ROOT}/attempts/attempt-02/outcome.json")))
        .unwrap();
    let mut executor = SelfEvalExecutor::new(&canonical, vec![None]);

    let result = run_next(
        &self_eval_run_request(&project, &lease),
        &repo,
        &mut executor,
    );

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("error", "command_failed")
    );

    // Answering with a pass while the project holds a pending evaluation is
    // refused: stdout is not authority, the current bytes are.
    let directory = tempdir().unwrap();
    let (repo, project) = self_eval_project(directory.path(), "mina-story");
    let canonical = project.canonicalize().unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("agent", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let pending = write_self_eval_state(&canonical, "needs_human", 1);
    let mut forged = pending.clone();
    forged["status"] = Value::String("pass".to_owned());
    forged["verdict"] = Value::String("pass".to_owned());
    let mut executor = SelfEvalExecutor::new(&canonical, vec![None]);
    executor.answer = Some(forged);

    let result = run_next(
        &self_eval_run_request(&project, &lease),
        &repo,
        &mut executor,
    );

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("error", "command_failed")
    );

    // Deleting the evidence the projection binds invalidates the state, even
    // though the projection itself is untouched and internally consistent.
    fs::remove_file(canonical.join(format!(
        "{SELF_EVAL_ROOT}/attempts/attempt-01/evidence-index.json"
    )))
    .unwrap();
    let mut executor = SelfEvalExecutor::new(&canonical, vec![None]);
    executor.answer = Some(pending);

    let result = run_next(
        &self_eval_run_request(&project, &lease),
        &repo,
        &mut executor,
    );

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("error", "command_failed")
    );

    // No projection at all is not a state either.
    let directory = tempdir().unwrap();
    let (repo, project) = self_eval_project(directory.path(), "mina-story");
    let canonical = project.canonicalize().unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("agent", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let mut executor = SelfEvalExecutor::new(&canonical, vec![None]);
    executor.answer = Some(serde_json::json!({"schema": "haru.render_self_eval.v1"}));

    let result = run_next(
        &self_eval_run_request(&project, &lease),
        &repo,
        &mut executor,
    );

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("error", "command_failed")
    );
}

#[test]
fn self_eval_review_passes_one_canonical_json_argv_value_and_an_attestation_ref() {
    let directory = tempdir().unwrap();
    let (repo, project) = self_eval_project(directory.path(), "mina-story");
    let canonical = project.canonicalize().unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("reviewer", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    write_self_eval_state(&canonical, "needs_human", 1);
    // Deliberately reversed: the runner normalises findings into the contracted
    // stable order instead of trusting the caller's order.
    let review = vision_review(vec![
        finding(12.0, "scene-9", "black_flash"),
        finding(4.5, "scene-2", "overlay_conflict"),
    ]);
    let request = self_eval_review_request(&project, &lease, "fail", review);
    let mut executor = SelfEvalExecutor::new(&canonical, vec![Some(("fail", 1))]);

    let result = visual_qa(&request, &repo, &mut executor);

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "self_eval_failed")
    );
    let (program, arguments) = &executor.calls[0];
    assert_eq!(
        program,
        &repo
            .canonicalize()
            .unwrap()
            .join("scripts/render-self-eval")
    );
    // Exactly six argv values, the review is one single value, and no path or
    // executable is anywhere among them.
    assert_eq!(arguments.len(), 6);
    assert_eq!(arguments[0], "review");
    assert_eq!(arguments[1], canonical.clone().into_os_string());
    assert_eq!(arguments[2], "--review-json");
    assert_eq!(arguments[4], "--attestation-ref");
    assert_eq!(arguments[5], "self-eval-attestation:self-eval-vision-0001");

    let payload = arguments[3].to_str().unwrap();
    let expected = serde_json::to_string(&serde_json::json!({
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
                "message": "reviewer reported a boundary defect",
            },
            {
                "timestamp_seconds": 12.0,
                "boundary_id": "scene-9",
                "category": "black_flash",
                "severity": "high",
                "message": "reviewer reported a boundary defect",
            },
        ],
    }))
    .unwrap();
    assert_eq!(payload, expected);
    // Sorted keys and minified bytes, top level and inside each finding.
    assert!(payload.starts_with(r#"{"capability":"#), "{payload}");
    assert!(
        payload.contains(r#""findings":[{"boundary_id":"scene-2""#),
        "{payload}"
    );
    assert!(!payload.contains(": "), "{payload}");
    assert!(!payload.ends_with('\n'));
    // The caller never names the unavailable receipt, the nonce or the
    // generation: the engine derives all three.
    for forbidden in ["vision_unavailable", "nonce", "generation", "path"] {
        assert!(
            !payload.contains(forbidden),
            "{forbidden} leaked into {payload}"
        );
    }
}

#[test]
fn self_eval_review_records_unavailability_as_a_still_pending_state() {
    let directory = tempdir().unwrap();
    let (repo, project) = self_eval_project(directory.path(), "mina-story");
    let canonical = project.canonicalize().unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("reviewer", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    write_self_eval_state(&canonical, "needs_human", 1);
    let request =
        self_eval_review_request(&project, &lease, "unavailable", vision_review(Vec::new()));
    // Unavailability advances the generation but leaves the state pending, so
    // the projection is rewritten and the gate still reports needs-review.
    let mut executor = SelfEvalExecutor::new(&canonical, vec![Some(("needs_human", 1))]);

    let result = visual_qa(&request, &repo, &mut executor);

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "self_eval_needs_review")
    );
    assert_eq!(executor.calls[0].1[0], "review");

    // A human structural attestation names no provider or model and carries its
    // one capability; it may then seal the state.
    let fallback = SelfEvalReviewInput {
        reviewer_kind: "human_fallback".to_owned(),
        provider: String::new(),
        model: String::new(),
        capability: "human_structural_attestation.v1".to_owned(),
        findings: Vec::new(),
        attestation_ref: "self-eval-attestation:self-eval-human-0001".to_owned(),
    };
    let mut sealing = SelfEvalExecutor::new(&canonical, vec![Some(("pass", 1))]);

    let result = visual_qa(
        &self_eval_review_request(&project, &lease, "pass", fallback),
        &repo,
        &mut sealing,
    );

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "self_eval_passed")
    );
    assert_eq!(
        sealing.calls[0].1[5],
        "self-eval-attestation:self-eval-human-0001"
    );
}

#[test]
fn self_eval_review_rejects_invalid_provenance_findings_and_attestation_refs() {
    let directory = tempdir().unwrap();
    let (repo, project) = self_eval_project(directory.path(), "mina-story");
    let canonical = project.canonicalize().unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("reviewer", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    write_self_eval_state(&canonical, "needs_human", 1);

    let mutate = |verdict: &str, apply: &dyn Fn(&mut VisualQaRequest)| {
        let mut request = self_eval_review_request(
            &project,
            &lease,
            verdict,
            vision_review(vec![finding(1.0, "scene-1", "black_flash")]),
        );
        apply(&mut request);
        request
    };

    let cases: Vec<(&str, VisualQaRequest)> = vec![
        (
            "unknown reviewer kind",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().reviewer_kind = "operator".to_owned();
            }),
        ),
        (
            "vision verdict outside the enum",
            mutate("changes_requested", &|_| {}),
        ),
        (
            "vision reviewer without a provider",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().provider = "  ".to_owned();
            }),
        ),
        (
            "vision reviewer without a model",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().model = String::new();
            }),
        ),
        (
            "vision reviewer borrowing the human capability",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().capability =
                    "human_structural_attestation.v1".to_owned();
            }),
        ),
        (
            "human fallback without its one capability",
            mutate("fail", &|request| {
                let review = request.self_eval.as_mut().unwrap();
                review.reviewer_kind = "human_fallback".to_owned();
                review.capability = "video_understanding.v1".to_owned();
            }),
        ),
        (
            "human fallback claiming unavailability",
            mutate("unavailable", &|request| {
                let review = request.self_eval.as_mut().unwrap();
                review.reviewer_kind = "human_fallback".to_owned();
                review.capability = "human_structural_attestation.v1".to_owned();
                review.findings.clear();
            }),
        ),
        (
            "blank reviewer identity",
            mutate("fail", &|request| {
                request.reviewed_by = Some("   ".to_owned());
            }),
        ),
        (
            "missing notes",
            mutate("fail", &|request| {
                request.notes = None;
            }),
        ),
        (
            "a pass that still reports findings",
            mutate("pass", &|_| {}),
        ),
        (
            "a fail that reports none",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().findings.clear();
            }),
        ),
        (
            "unavailability that still reports findings",
            mutate("unavailable", &|_| {}),
        ),
        (
            "a non-finite finding timestamp",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().findings[0].timestamp_seconds = f64::NAN;
            }),
        ),
        (
            "a negative finding timestamp",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().findings[0].timestamp_seconds = -0.5;
            }),
        ),
        (
            "a finding without a boundary",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().findings[0].boundary_id = String::new();
            }),
        ),
        (
            "a finding without a message",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().findings[0].message = "  ".to_owned();
            }),
        ),
        (
            "a finding without a severity",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().findings[0].severity = String::new();
            }),
        ),
        (
            "a missing attestation ref",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().attestation_ref = String::new();
            }),
        ),
        (
            "a bare attestation suffix without the protected-root namespace",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().attestation_ref =
                    "self-eval-vision-0001".to_owned();
            }),
        ),
        (
            "an attestation ref escaping its grammar",
            mutate("fail", &|request| {
                request.self_eval.as_mut().unwrap().attestation_ref = "../../etc/passwd".to_owned();
            }),
        ),
        (
            "no typed review at all",
            mutate("fail", &|request| {
                request.self_eval = None;
            }),
        ),
    ];

    for (description, request) in cases {
        let mut executor = SelfEvalExecutor::new(&canonical, vec![]);

        let result = visual_qa(&request, &repo, &mut executor);

        assert_eq!(
            (result.outcome.as_str(), result.code.as_str()),
            ("error", "invalid_input"),
            "{description} should be refused"
        );
        assert!(
            executor.calls.is_empty(),
            "{description} reached the engine"
        );
    }
}

#[test]
fn existing_visual_qa_actions_reject_self_eval_extras_and_stay_compatible() {
    let directory = tempdir().unwrap();
    let (repo, project) = self_eval_project(directory.path(), "mina-story");
    fs::write(repo.join("scripts/visual-qa-sample"), "#!/bin/sh\n").unwrap();
    fs::write(repo.join("scripts/render-segment"), "#!/bin/sh\n").unwrap();
    let canonical = project.canonicalize().unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("reviewer", Duration::from_secs(60), SystemTime::now())
        .unwrap();

    // Sampling and both human reviews refuse the typed extras outright, and the
    // refusal happens before any script is reached.
    for action in ["sample", "review", "segment-review"] {
        let mut executor = SelfEvalExecutor::new(&canonical, vec![]);
        let request = VisualQaRequest {
            project_root: canonical.clone(),
            lease: lease_input(&lease),
            action: action.to_owned(),
            reviewed_by: Some("harvey".to_owned()),
            verdict: Some("pass".to_owned()),
            notes: Some("looks right".to_owned()),
            self_eval: Some(vision_review(Vec::new())),
        };

        let result = visual_qa(&request, &repo, &mut executor);

        assert_eq!(
            (result.outcome.as_str(), result.code.as_str()),
            ("error", "invalid_input"),
            "{action} accepted self-eval extras"
        );
        assert!(executor.calls.is_empty(), "{action} reached a script");
    }

    // Without the extras, sampling still runs exactly the sampler it always did.
    let sample = serde_json::json!({
        "schema": "haru.visual_qa_sample.v2",
        "project": "mina-story",
    });
    fs::create_dir_all(canonical.join("quality-review/visual-sampling")).unwrap();
    fs::write(
        canonical.join("quality-review/visual-sampling/visual-qa-sample.json"),
        serde_json::to_vec(&sample).unwrap(),
    )
    .unwrap();
    let mut executor = FakeExecutor {
        data: Some(sample),
        ..FakeExecutor::default()
    };

    let result = visual_qa(
        &VisualQaRequest {
            project_root: canonical.clone(),
            lease: lease_input(&lease),
            action: "sample".to_owned(),
            reviewed_by: None,
            verdict: None,
            notes: None,
            self_eval: None,
        },
        &repo,
        &mut executor,
    );

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("ok", "visual_qa_sampled")
    );
    assert_eq!(
        executor.calls[0],
        (
            repo.canonicalize()
                .unwrap()
                .join("scripts/visual-qa-sample"),
            vec![
                std::ffi::OsString::from("sample"),
                canonical.clone().into_os_string(),
            ]
        )
    );
}

#[test]
fn self_eval_gate_and_review_stay_leased_and_report_engine_unavailability() {
    let directory = tempdir().unwrap();
    let (repo, project) = self_eval_project(directory.path(), "mina-story");
    let canonical = project.canonicalize().unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("agent", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    write_self_eval_state(&canonical, "needs_human", 1);

    // A stale lease generation is refused before the engine runs, so a caller
    // that lost the lease cannot advance the gate.
    let mut stale = SelfEvalExecutor::new(&canonical, vec![]);
    let mut stale_request = self_eval_run_request(&project, &lease);
    stale_request.lease.generation += 1;

    let result = run_next(&stale_request, &repo, &mut stale);

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("blocked", "lease_invalid")
    );
    assert!(stale.calls.is_empty());

    // The review path is leased the same way.
    let mut stale_review = SelfEvalExecutor::new(&canonical, vec![]);
    let mut review_request = self_eval_review_request(
        &project,
        &lease,
        "fail",
        vision_review(vec![finding(1.0, "scene-1", "black_flash")]),
    );
    review_request.lease.generation += 1;

    let result = visual_qa(&review_request, &repo, &mut stale_review);

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("blocked", "lease_invalid")
    );
    assert!(stale_review.calls.is_empty());

    // A missing engine is named as unavailable, never as a gate result.
    let bare = directory.path().join("bare-repo");
    fs::create_dir_all(bare.join("scripts")).unwrap();
    let mut executor = SelfEvalExecutor::new(&canonical, vec![]);

    let result = run_next(
        &self_eval_run_request(&project, &lease),
        &bare,
        &mut executor,
    );

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("error", "runner_unavailable")
    );
    assert!(executor.calls.is_empty());

    // A non-zero exit is a command failure and never a state.
    let mut failing = SelfEvalExecutor::new(&canonical, vec![None]);
    failing.exit_code = 1;
    failing.answer = Some(serde_json::json!({"error": "render marker is stale"}));

    let result = run_next(
        &self_eval_run_request(&project, &lease),
        &repo,
        &mut failing,
    );

    assert_eq!(
        (result.outcome.as_str(), result.code.as_str()),
        ("error", "command_failed")
    );
}

#[test]
fn delivery_runners_require_real_digest_bound_outputs() {
    let dir = tempdir().unwrap();
    let repo = dir.path().join("repo");
    let project = dir.path().join("projects/delivery");
    fs::create_dir_all(repo.join("scripts")).unwrap();
    fs::create_dir_all(project.join(".hvp")).unwrap();
    for script in ["derive-narration", "final-quality-review"] {
        fs::write(repo.join("scripts").join(script), "#!/bin/sh\n").unwrap();
    }
    let repo = repo.canonicalize().unwrap();
    let project = project.canonicalize().unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("codex", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    for (runner, script, action) in [
        ("derive-narration", "derive-narration", Some("prepare")),
        (
            "promote-derived-narration",
            "derive-narration",
            Some("promote"),
        ),
        ("final-quality-review", "final-quality-review", None),
    ] {
        let request = RunNextRequest {
            project_root: project.canonicalize().unwrap(),
            lease: lease_input(&lease),
            runner: runner.to_owned(),
            tools_root: None,
        };
        let mut executor = FakeExecutor {
            data: Some(serde_json::json!({"status":"complete","project":"delivery"})),
            ..FakeExecutor::default()
        };
        let result = run_next(&request, &repo, &mut executor);
        assert_eq!(
            result.code, "command_failed",
            "{runner} must reach the fixed runner and refuse missing evidence"
        );
        assert_eq!(executor.calls.len(), 1);
        assert_eq!(executor.calls[0].0, repo.join("scripts").join(script));
        let expected = match action {
            Some(action) => vec![
                std::ffi::OsString::from(action),
                project.clone().into_os_string(),
            ],
            None => vec![project.clone().into_os_string()],
        };
        assert_eq!(executor.calls[0].1, expected);

        let request_bytes = b"staged request";
        let request_sha = format!("{:x}", Sha256::digest(request_bytes));
        for name in [
            "narration-derivation-request.json",
            "narration-derivation-acceptance.json",
        ] {
            fs::create_dir_all(project.join(".hvp/staging")).unwrap();
            fs::write(project.join(".hvp/staging").join(name), request_bytes).unwrap();
        }
        let mut source = serde_json::Map::new();
        for (key, name) in [
            ("audio", "narration-final.mp3"),
            ("srt", "narration-final.srt"),
            ("pronunciation_stamp", "narration-final.mp3.pron-ok.json"),
        ] {
            fs::write(project.join(name), key.as_bytes()).unwrap();
            source.insert(key.to_owned(), serde_json::json!({"path":name,"sha256":format!("{:x}",Sha256::digest(key.as_bytes()))}));
        }
        let (schema, names) = match runner {
            "derive-narration" => (
                "haru.narration_derivation.v1",
                vec![
                    (
                        "audio",
                        format!(".hvp/staging/narration-derivations/{request_sha}/narration.mp3"),
                    ),
                    (
                        "srt",
                        format!(".hvp/staging/narration-derivations/{request_sha}/narration.srt"),
                    ),
                ],
            ),
            "promote-derived-narration" => (
                "haru.narration_derivation_promotion.v1",
                vec![
                    ("audio", "narration-final.mp3".into()),
                    ("srt", "narration-final.srt".into()),
                    (
                        "pronunciation_stamp",
                        "narration-final.mp3.pron-ok.json".into(),
                    ),
                ],
            ),
            _ => (
                "haru.final_quality_review.v1",
                vec![
                    ("prep", "quality-review/final-v1/prep.json".into()),
                    ("review", "quality-review/final-v1/review.json".into()),
                ],
            ),
        };
        let mut artifacts = serde_json::Map::new();
        for (key, name) in &names {
            let path = project.join(name);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, key.as_bytes()).unwrap();
            artifacts.insert(
                (*key).to_owned(),
                serde_json::json!({
                    "path": name, "sha256": format!("{:x}", Sha256::digest(key.as_bytes())),
                }),
            );
        }
        fs::create_dir_all(project.join("output")).unwrap();
        fs::write(project.join("output/final.mp4"), b"video").unwrap();
        let payload = serde_json::json!({
            "schema":schema,"status":"complete","project":"delivery",
            "request_sha256":request_sha,"tempo":1.25,"decode_status":"pass",
            "accepted_by":"harvey","derivation_receipt_sha256":"b".repeat(64),
            "audio_sha256":format!("{:x}",Sha256::digest(b"audio")),
            "video_sha256":format!("{:x}",Sha256::digest(b"video")),
            "artifacts":artifacts, "source":source,
        });
        let mut executor = FakeExecutor {
            data: Some(payload.clone()),
            ..FakeExecutor::default()
        };
        assert_eq!(
            run_next(&request, &repo, &mut executor).code,
            "gate_completed"
        );
        fs::write(project.join(&names[0].1), b"changed output").unwrap();
        let mut executor = FakeExecutor {
            data: Some(payload),
            ..FakeExecutor::default()
        };
        assert_eq!(
            run_next(&request, &repo, &mut executor).code,
            "command_failed"
        );
    }
}

#[test]
fn derived_narration_runs_real_media_through_leased_application() {
    let dir = tempdir().unwrap();
    let project = dir.path().join("tempo-demo");
    fs::create_dir_all(project.join(".hvp/staging")).unwrap();
    let project = project.canonicalize().unwrap();
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let audio = project.join("narration-final.mp3");
    let generated = Command::new("ffmpeg")
        .args([
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:a",
            "libmp3lame",
        ])
        .arg(&audio)
        .output()
        .expect("ffmpeg required by the repository media test harness");
    assert!(
        generated.status.success(),
        "{}",
        String::from_utf8_lossy(&generated.stderr)
    );
    let old_audio = fs::read(&audio).unwrap();
    let old_sha = format!("{:x}", Sha256::digest(&old_audio));
    let srt = b"1\n00:00:00,000 --> 00:00:02,000\nfixture tone\n\n";
    fs::write(project.join("narration-final.srt"), srt).unwrap();
    fs::write(project.join("narration-final.mp3.pron-ok.json"), serde_json::to_vec(&serde_json::json!({
        "schema":"haru.pronunciation_approval.v1","status":"pass","sha256":old_sha,"warnings":[],"approved_by":"test-fixture",
    })).unwrap()).unwrap();
    fs::write(
        project.join(".hvp/staging/narration-derivation-request.json"),
        serde_json::to_vec(&serde_json::json!({
            "schema":"haru.narration_derivation_request.v1","tempo":1.25,
            "source_audio_sha256":old_sha,"source_srt_sha256":format!("{:x}",Sha256::digest(srt)),
        }))
        .unwrap(),
    )
    .unwrap();
    let lease = ProjectStore::new(&project)
        .claim_at("fixture", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let mut request = RunNextRequest {
        project_root: project.clone(),
        lease: lease_input(&lease),
        runner: "derive-narration".into(),
        tools_root: None,
    };
    let prepared = run_next(&request, repo, &mut ProcessExecutor);
    assert_eq!(prepared.code, "gate_completed", "{prepared:?}");
    assert_eq!(
        fs::read(&audio).unwrap(),
        old_audio,
        "prepare must not replace canonical audio"
    );
    let prepared = prepared.data.unwrap();
    let candidate = prepared["artifacts"]["audio"]["path"].as_str().unwrap();
    let receipt = project.join(candidate).with_file_name("derivation.json");
    fs::write(
        project.join(".hvp/staging/narration-derivation-acceptance.json"),
        serde_json::to_vec(&serde_json::json!({
            "schema":"haru.narration_derivation_acceptance.v1","accepted_by":"test-fixture",
            "candidate_audio":candidate,"audio_sha256":prepared["artifacts"]["audio"]["sha256"],
            "derivation_receipt_sha256":format!("{:x}",Sha256::digest(fs::read(receipt).unwrap())),
        }))
        .unwrap(),
    )
    .unwrap();
    request.runner = "promote-derived-narration".into();
    let promoted = run_next(&request, repo, &mut ProcessExecutor);
    assert_eq!(promoted.code, "gate_completed", "{promoted:?}");
    assert_ne!(fs::read(&audio).unwrap(), old_audio);
    assert_eq!(
        run_next(&request, repo, &mut ProcessExecutor).data,
        promoted.data,
        "promotion replay must be stable"
    );
    request.runner = "derive-narration".into();
    let stale = run_next(&request, repo, &mut ProcessExecutor);
    assert_eq!(stale.code, "command_failed");
    assert_eq!(stale.data.unwrap()["code"], "derivation_source_mismatch");
}

#[test]
fn final_quality_runs_real_media_through_hermetic_leased_application() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let fixture = Command::new("/usr/bin/python3").args([
        "-S", "-c",
        "from pathlib import Path; import sys; from test_final_quality_review import FinalQualityReviewTest; case=FinalQualityReviewTest(); case.root=Path(sys.argv[1]); case.project()",
    ]).arg(&root).env("PYTHONPATH",repo.join("tools")).output().unwrap();
    assert!(
        fixture.status.success(),
        "{}",
        String::from_utf8_lossy(&fixture.stderr)
    );
    let project = root.join("projects/demo");
    let lease = ProjectStore::new(&project)
        .claim_at("fixture", Duration::from_secs(60), SystemTime::now())
        .unwrap();
    let request = RunNextRequest {
        project_root: project.clone(),
        lease: lease_input(&lease),
        runner: "final-quality-review".into(),
        tools_root: None,
    };
    let mut executor = RootedProcessExecutor::new(&root);
    let reviewed = run_next(&request, repo, &mut executor);
    assert_eq!(reviewed.code, "gate_completed", "{reviewed:?}");
    let prep: Value = serde_json::from_slice(
        &fs::read(project.join("quality-review/final-v1/prep.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        prep["mechanical_evidence"]["decode"]["full_decode_clean"],
        true
    );
    fs::remove_file(project.join("quality-review/visual-sampling/visual-qa-review.json")).unwrap();
    let refused = run_next(&request, repo, &mut executor);
    assert_eq!(
        refused.code, "command_failed",
        "cached QA must not survive removal of human review"
    );
}
