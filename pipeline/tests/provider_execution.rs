use std::fs;
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::{Duration, UNIX_EPOCH};

use pipeline::{
    BudgetCaps, BudgetRequest, BudgetStore, OutputPolicy, ProjectStore, ProviderJobStatus,
    ProviderRequest, StoreError, UsageAmount,
};
use tempfile::tempdir;

fn usage(amount: u64) -> UsageAmount {
    UsageAmount {
        unit: "elevenlabs_credits".to_owned(),
        amount,
    }
}

fn request(policy: OutputPolicy) -> ProviderRequest {
    ProviderRequest {
        provider: "replicate".to_owned(),
        model: "kwaivgi/kling-v3-omni-video".to_owned(),
        gate: "render".to_owned(),
        input_digest: "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            .to_owned(),
        idempotency_key: "render-v1".to_owned(),
        estimated_cost: usage(6),
        output_path: "output/final.mp4".to_owned(),
        output_policy: policy,
    }
}

#[test]
fn request_receipt_precedes_submission_and_duplicate_prepare_reuses_it() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir(&project).unwrap();
    let store = ProjectStore::new(&project);
    let started = UNIX_EPOCH + Duration::from_secs(10_000);
    let lease = store
        .claim_at("codex", Duration::from_secs(60), started)
        .unwrap();

    let prepared = store
        .prepare_provider_at(
            "codex",
            &lease.token,
            &request(OutputPolicy::FailIfExists),
            started + Duration::from_secs(1),
        )
        .unwrap();

    assert_eq!(prepared.schema_version, 1);
    assert_eq!(prepared.project, "demo");
    assert_eq!(prepared.provider, "replicate");
    assert_eq!(prepared.model, "kwaivgi/kling-v3-omni-video");
    assert_eq!(prepared.gate, "render");
    assert_eq!(prepared.idempotency_key, "render-v1");
    assert_eq!(prepared.estimated_cost, usage(6));
    assert_eq!(prepared.status, ProviderJobStatus::Prepared);
    assert!(project.join(".hvp/provider-jobs/render-v1.json").is_file());

    let duplicate = store
        .prepare_provider_at(
            "codex",
            &lease.token,
            &request(OutputPolicy::FailIfExists),
            started + Duration::from_secs(2),
        )
        .unwrap();
    assert_eq!(duplicate, prepared);

    let mut conflict = request(OutputPolicy::FailIfExists);
    conflict.model = "another/model".to_owned();
    assert!(matches!(
        store.prepare_provider_at(
            "codex",
            &lease.token,
            &conflict,
            started + Duration::from_secs(3)
        ),
        Err(StoreError::IdempotencyConflict(key)) if key == "render-v1"
    ));
}

#[test]
fn corrupted_receipt_cannot_redirect_provider_staging() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir(&project).unwrap();
    let store = ProjectStore::new(&project);
    let started = UNIX_EPOCH + Duration::from_secs(10_500);
    let lease = store
        .claim_at("codex", Duration::from_secs(60), started)
        .unwrap();
    store
        .prepare_provider_at(
            "codex",
            &lease.token,
            &request(OutputPolicy::FailIfExists),
            started + Duration::from_secs(1),
        )
        .unwrap();

    let path = project.join(".hvp/provider-jobs/render-v1.json");
    let mut receipt: serde_json::Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    receipt["staging_path"] = "../../outside.part".into();
    fs::write(&path, serde_json::to_vec(&receipt).unwrap()).unwrap();

    assert!(matches!(
        store.provider_job("render-v1"),
        Err(StoreError::InvalidProviderRequest(_))
    ));
}

#[test]
fn submitted_job_survives_timeout_for_cross_process_resume() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir(&project).unwrap();
    let store = ProjectStore::new(&project);
    let started = UNIX_EPOCH + Duration::from_secs(11_000);
    let lease = store
        .claim_at("hermes", Duration::from_secs(60), started)
        .unwrap();
    store
        .prepare_provider_at(
            "hermes",
            &lease.token,
            &request(OutputPolicy::FailIfExists),
            started + Duration::from_secs(1),
        )
        .unwrap();
    store
        .mark_provider_submitted_at(
            "hermes",
            &lease.token,
            "render-v1",
            "prediction-123",
            "starting",
            started + Duration::from_secs(2),
        )
        .unwrap();
    let duplicate_submission = store
        .mark_provider_submitted_at(
            "hermes",
            &lease.token,
            "render-v1",
            "prediction-123",
            "starting",
            started + Duration::from_secs(2),
        )
        .unwrap();
    assert_eq!(
        duplicate_submission.provider_job_id.as_deref(),
        Some("prediction-123")
    );
    assert!(matches!(
        store.mark_provider_submitted_at(
            "hermes",
            &lease.token,
            "render-v1",
            "prediction-duplicate",
            "starting",
            started + Duration::from_secs(2)
        ),
        Err(StoreError::InvalidProviderTransition { .. })
    ));
    store
        .mark_provider_timed_out_at(
            "hermes",
            &lease.token,
            "render-v1",
            "processing",
            started + Duration::from_secs(3),
        )
        .unwrap();

    let resumed = ProjectStore::new(&project)
        .provider_job("render-v1")
        .unwrap()
        .unwrap();
    assert_eq!(resumed.status, ProviderJobStatus::TimedOut);
    assert_eq!(resumed.provider_job_id.as_deref(), Some("prediction-123"));
    assert_eq!(resumed.provider_status.as_deref(), Some("processing"));
    assert_eq!(
        ProjectStore::new(&project).load_provider_jobs().unwrap()["render-v1"]
            .provider_job_id
            .as_deref(),
        Some("prediction-123")
    );

    let duplicate = store
        .prepare_provider_at(
            "hermes",
            &lease.token,
            &request(OutputPolicy::FailIfExists),
            started + Duration::from_secs(4),
        )
        .unwrap();
    assert_eq!(duplicate.provider_job_id.as_deref(), Some("prediction-123"));
}

#[test]
fn replacement_refuses_to_overwrite_an_output_changed_after_prepare() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir_all(project.join("output")).unwrap();
    fs::write(project.join("output/final.mp4"), "old video").unwrap();
    let store = ProjectStore::new(&project);
    let started = UNIX_EPOCH + Duration::from_secs(12_500);
    let lease = store
        .claim_at("codex", Duration::from_secs(60), started)
        .unwrap();
    let prepared = store
        .prepare_provider_at(
            "codex",
            &lease.token,
            &request(OutputPolicy::Replace),
            started + Duration::from_secs(1),
        )
        .unwrap();
    store
        .mark_provider_submitted_at(
            "codex",
            &lease.token,
            "render-v1",
            "prediction-789",
            "succeeded",
            started + Duration::from_secs(2),
        )
        .unwrap();
    fs::write(project.join("output/final.mp4"), "newer approved video").unwrap();
    fs::write(project.join(&prepared.staging_path), "provider video").unwrap();

    assert!(matches!(
        store.complete_provider_at(
            "codex",
            &lease.token,
            "render-v1",
            "succeeded",
            usage(5),
            started + Duration::from_secs(3)
        ),
        Err(StoreError::ProviderOutputChanged(_))
    ));
    assert_eq!(
        fs::read_to_string(project.join("output/final.mp4")).unwrap(),
        "newer approved video"
    );
}

#[test]
fn existing_output_fails_closed_unless_replace_records_and_promotes_supersession() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir_all(project.join("output")).unwrap();
    fs::write(project.join("output/final.mp4"), "approved old video").unwrap();
    let store = ProjectStore::new(&project);
    let started = UNIX_EPOCH + Duration::from_secs(12_000);
    let lease = store
        .claim_at("codex", Duration::from_secs(60), started)
        .unwrap();

    assert!(matches!(
        store.prepare_provider_at(
            "codex",
            &lease.token,
            &request(OutputPolicy::FailIfExists),
            started + Duration::from_secs(1)
        ),
        Err(StoreError::ProviderOutputExists(_))
    ));

    let prepared = store
        .prepare_provider_at(
            "codex",
            &lease.token,
            &request(OutputPolicy::Replace),
            started + Duration::from_secs(2),
        )
        .unwrap();
    assert_eq!(
        prepared
            .supersedes
            .as_ref()
            .map(|artifact| artifact.path.as_str()),
        Some("output/final.mp4")
    );
    store
        .mark_provider_submitted_at(
            "codex",
            &lease.token,
            "render-v1",
            "prediction-456",
            "processing",
            started + Duration::from_secs(3),
        )
        .unwrap();
    fs::create_dir_all(project.join(&prepared.staging_path).parent().unwrap()).unwrap();
    fs::write(project.join(&prepared.staging_path), "new video").unwrap();

    let completed = store
        .complete_provider_at(
            "codex",
            &lease.token,
            "render-v1",
            "succeeded",
            usage(5),
            started + Duration::from_secs(4),
        )
        .unwrap();

    assert_eq!(completed.status, ProviderJobStatus::Succeeded);
    assert_eq!(completed.actual_cost, Some(usage(5)));
    assert_eq!(completed.provider_status.as_deref(), Some("succeeded"));
    assert_eq!(completed.completed_at, Some(12_004));
    assert!(
        completed
            .output_digest
            .as_deref()
            .unwrap()
            .starts_with("sha256:")
    );
    assert_eq!(
        fs::read_to_string(project.join("output/final.mp4")).unwrap(),
        "new video"
    );
    assert!(completed.supersedes.is_some());
}

#[test]
fn submission_unknown_and_failed_states_remain_durable_without_resubmission() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("demo");
    fs::create_dir(&project).unwrap();
    let store = ProjectStore::new(&project);
    let started = UNIX_EPOCH + Duration::from_secs(13_000);
    let lease = store
        .claim_at("codex", Duration::from_secs(60), started)
        .unwrap();
    store
        .prepare_provider_at(
            "codex",
            &lease.token,
            &request(OutputPolicy::FailIfExists),
            started + Duration::from_secs(1),
        )
        .unwrap();

    let unknown = store
        .mark_provider_submission_unknown_at(
            "codex",
            &lease.token,
            "render-v1",
            "POST timed out before a response",
            started + Duration::from_secs(2),
        )
        .unwrap();
    assert_eq!(unknown.status, ProviderJobStatus::SubmissionUnknown);
    assert_eq!(
        store
            .prepare_provider_at(
                "codex",
                &lease.token,
                &request(OutputPolicy::FailIfExists),
                started + Duration::from_secs(3),
            )
            .unwrap()
            .status,
        ProviderJobStatus::SubmissionUnknown
    );
    let failed = store
        .mark_provider_failed_at(
            "codex",
            &lease.token,
            "render-v1",
            "not_found",
            "provider confirmed that no job exists",
            started + Duration::from_secs(4),
        )
        .unwrap();
    assert_eq!(failed.status, ProviderJobStatus::Failed);
    assert_eq!(failed.provider_status.as_deref(), Some("not_found"));
    assert_eq!(
        ProjectStore::new(&project)
            .provider_job("render-v1")
            .unwrap()
            .unwrap()
            .error
            .as_deref(),
        Some("provider confirmed that no job exists")
    );
}

#[test]
fn concurrent_budget_reservations_cannot_both_cross_the_cap() {
    let directory = tempdir().unwrap();
    let budget = Arc::new(BudgetStore::new(directory.path().join("tts-budget.json")));
    let barrier = Arc::new(Barrier::new(3));
    let started = UNIX_EPOCH + Duration::from_secs(14_000);

    let handles: Vec<_> = ["tts-a", "tts-b"]
        .into_iter()
        .map(|key| {
            let budget = Arc::clone(&budget);
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                barrier.wait();
                budget.reserve_at(
                    &BudgetRequest {
                        idempotency_key: key.to_owned(),
                        provider: "elevenlabs".to_owned(),
                        project: "demo".to_owned(),
                        cycle: "2026-07-17".to_owned(),
                        day: "2026-07-28".to_owned(),
                        amount: usage(6),
                        caps: BudgetCaps {
                            project: 10,
                            cycle: 10,
                            day: 10,
                        },
                    },
                    started,
                )
            })
        })
        .collect();
    barrier.wait();
    let results: Vec<_> = handles
        .into_iter()
        .map(|handle| handle.join().unwrap())
        .collect();

    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert_eq!(
        results
            .iter()
            .filter(|result| matches!(result, Err(StoreError::BudgetExceeded { .. })))
            .count(),
        1
    );

    let winner = results.into_iter().find_map(Result::ok).unwrap();
    budget
        .commit_at(
            &winner.idempotency_key,
            usage(5),
            started + Duration::from_secs(1),
        )
        .unwrap();
    let ledger = budget.load().unwrap();
    assert_eq!(ledger.spent["2026-07-17"].total, 5);
    assert_eq!(ledger.spent["2026-07-17"].projects["demo"], 5);
    assert_eq!(ledger.spent["2026-07-17"].days["2026-07-28"], 5);
    assert!(ledger.reservations.is_empty());
}
