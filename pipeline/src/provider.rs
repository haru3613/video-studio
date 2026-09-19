use std::collections::BTreeMap;
use std::ffi::OsStr;
use std::fs::{self, File, OpenOptions};
use std::io;
use std::path::{Component, Path, PathBuf};
use std::time::SystemTime;

use fs2::FileExt;
use serde::{Deserialize, Serialize};

use crate::inspect::Artifact;
use crate::store::{ProjectStore, StoreError, unix_seconds, write_json_atomically};

const PROVIDER_JOBS_DIR: &str = "provider-jobs";
const PROVIDER_STAGING_DIR: &str = "provider-staging";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UsageAmount {
    pub unit: String,
    pub amount: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OutputPolicy {
    FailIfExists,
    Replace,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderJobStatus {
    Prepared,
    Submitted,
    SubmissionUnknown,
    TimedOut,
    Failed,
    Promoting,
    Succeeded,
}

impl ProviderJobStatus {
    fn name(self) -> &'static str {
        match self {
            Self::Prepared => "prepared",
            Self::Submitted => "submitted",
            Self::SubmissionUnknown => "submission_unknown",
            Self::TimedOut => "timed_out",
            Self::Failed => "failed",
            Self::Promoting => "promoting",
            Self::Succeeded => "succeeded",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProviderRequest {
    pub provider: String,
    pub model: String,
    pub gate: String,
    pub input_digest: String,
    pub idempotency_key: String,
    pub estimated_cost: UsageAmount,
    pub output_path: String,
    pub output_policy: OutputPolicy,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProviderJobReceipt {
    pub schema_version: u32,
    pub project: String,
    pub provider: String,
    pub model: String,
    pub gate: String,
    pub input_digest: String,
    pub idempotency_key: String,
    pub estimated_cost: UsageAmount,
    pub actual_cost: Option<UsageAmount>,
    pub output_path: String,
    pub staging_path: String,
    pub output_policy: OutputPolicy,
    pub supersedes: Option<Artifact>,
    pub status: ProviderJobStatus,
    pub provider_job_id: Option<String>,
    pub provider_status: Option<String>,
    pub output_digest: Option<String>,
    pub error: Option<String>,
    pub prepared_at: u64,
    pub updated_at: u64,
    pub completed_at: Option<u64>,
}

impl ProjectStore {
    pub fn prepare_provider_at(
        &self,
        owner: &str,
        token: &str,
        request: &ProviderRequest,
        now: SystemTime,
    ) -> Result<ProviderJobReceipt, StoreError> {
        validate_provider_request(request)?;
        self.with_verified_lease_at(owner, token, now, || {
            if let Some(existing) = self.provider_job_unlocked(&request.idempotency_key)? {
                if existing.matches_request(request) {
                    return Ok(existing);
                }
                return Err(StoreError::IdempotencyConflict(
                    request.idempotency_key.clone(),
                ));
            }

            let output = resolve_project_relative(&self.project_root, &request.output_path)?;
            let supersedes = if output.exists() {
                if !output.is_file() {
                    return Err(StoreError::ProviderOutputExists(output));
                }
                if request.output_policy == OutputPolicy::FailIfExists {
                    return Err(StoreError::ProviderOutputExists(output));
                }
                Some(Artifact::from_path(&self.project_root, &output)?)
            } else {
                None
            };
            let staging_path = format!(
                ".hvp/{PROVIDER_STAGING_DIR}/{}.part",
                request.idempotency_key
            );
            let staging = self.project_root.join(&staging_path);
            fs::create_dir_all(self.state_dir().join(PROVIDER_STAGING_DIR))?;
            if staging.exists() {
                return Err(StoreError::InvalidProviderRequest(format!(
                    "staging path already exists: {}",
                    staging.display()
                )));
            }

            let timestamp = unix_seconds(now)?;
            let receipt = ProviderJobReceipt {
                schema_version: 1,
                project: project_name(&self.project_root),
                provider: request.provider.clone(),
                model: request.model.clone(),
                gate: request.gate.clone(),
                input_digest: request.input_digest.clone(),
                idempotency_key: request.idempotency_key.clone(),
                estimated_cost: request.estimated_cost.clone(),
                actual_cost: None,
                output_path: request.output_path.clone(),
                staging_path,
                output_policy: request.output_policy,
                supersedes,
                status: ProviderJobStatus::Prepared,
                provider_job_id: None,
                provider_status: None,
                output_digest: None,
                error: None,
                prepared_at: timestamp,
                updated_at: timestamp,
                completed_at: None,
            };
            self.write_provider_job(&receipt)?;
            Ok(receipt)
        })
    }

    pub fn provider_job(
        &self,
        idempotency_key: &str,
    ) -> Result<Option<ProviderJobReceipt>, StoreError> {
        validate_idempotency_key(idempotency_key)?;
        self.provider_job_unlocked(idempotency_key)
    }

    pub fn load_provider_jobs(&self) -> Result<BTreeMap<String, ProviderJobReceipt>, StoreError> {
        let directory = self.provider_jobs_dir();
        if !directory.is_dir() {
            return Ok(BTreeMap::new());
        }
        let mut jobs = BTreeMap::new();
        for entry in fs::read_dir(directory)? {
            let path = entry?.path();
            if path.extension().and_then(OsStr::to_str) != Some("json") {
                continue;
            }
            let receipt: ProviderJobReceipt = serde_json::from_slice(&fs::read(&path)?)?;
            validate_provider_receipt(&path, &receipt)?;
            jobs.insert(receipt.idempotency_key.clone(), receipt);
        }
        Ok(jobs)
    }

    pub fn mark_provider_submitted_at(
        &self,
        owner: &str,
        token: &str,
        idempotency_key: &str,
        provider_job_id: &str,
        provider_status: &str,
        now: SystemTime,
    ) -> Result<ProviderJobReceipt, StoreError> {
        validate_nonempty("provider_job_id", provider_job_id)?;
        validate_nonempty("provider_status", provider_status)?;
        self.update_provider_at(owner, token, idempotency_key, now, |receipt, timestamp| {
            if receipt.status == ProviderJobStatus::Submitted
                && receipt.provider_job_id.as_deref() == Some(provider_job_id)
            {
                return Ok(());
            }
            require_status(
                receipt,
                &[
                    ProviderJobStatus::Prepared,
                    ProviderJobStatus::SubmissionUnknown,
                ],
            )?;
            receipt.status = ProviderJobStatus::Submitted;
            receipt.provider_job_id = Some(provider_job_id.to_owned());
            receipt.provider_status = Some(provider_status.to_owned());
            receipt.updated_at = timestamp;
            Ok(())
        })
    }

    pub fn mark_provider_submission_unknown_at(
        &self,
        owner: &str,
        token: &str,
        idempotency_key: &str,
        error: &str,
        now: SystemTime,
    ) -> Result<ProviderJobReceipt, StoreError> {
        validate_nonempty("error", error)?;
        self.update_provider_at(owner, token, idempotency_key, now, |receipt, timestamp| {
            if receipt.status == ProviderJobStatus::SubmissionUnknown {
                return Ok(());
            }
            require_status(receipt, &[ProviderJobStatus::Prepared])?;
            receipt.status = ProviderJobStatus::SubmissionUnknown;
            receipt.error = Some(error.to_owned());
            receipt.updated_at = timestamp;
            Ok(())
        })
    }

    pub fn mark_provider_timed_out_at(
        &self,
        owner: &str,
        token: &str,
        idempotency_key: &str,
        provider_status: &str,
        now: SystemTime,
    ) -> Result<ProviderJobReceipt, StoreError> {
        validate_nonempty("provider_status", provider_status)?;
        self.update_provider_at(owner, token, idempotency_key, now, |receipt, timestamp| {
            require_status(
                receipt,
                &[ProviderJobStatus::Submitted, ProviderJobStatus::TimedOut],
            )?;
            receipt.status = ProviderJobStatus::TimedOut;
            receipt.provider_status = Some(provider_status.to_owned());
            receipt.updated_at = timestamp;
            Ok(())
        })
    }

    pub fn mark_provider_failed_at(
        &self,
        owner: &str,
        token: &str,
        idempotency_key: &str,
        provider_status: &str,
        error: &str,
        now: SystemTime,
    ) -> Result<ProviderJobReceipt, StoreError> {
        validate_nonempty("provider_status", provider_status)?;
        validate_nonempty("error", error)?;
        self.update_provider_at(owner, token, idempotency_key, now, |receipt, timestamp| {
            require_status(
                receipt,
                &[
                    ProviderJobStatus::Prepared,
                    ProviderJobStatus::Submitted,
                    ProviderJobStatus::SubmissionUnknown,
                    ProviderJobStatus::TimedOut,
                ],
            )?;
            receipt.status = ProviderJobStatus::Failed;
            receipt.provider_status = Some(provider_status.to_owned());
            receipt.error = Some(error.to_owned());
            receipt.updated_at = timestamp;
            Ok(())
        })
    }

    pub fn complete_provider_at(
        &self,
        owner: &str,
        token: &str,
        idempotency_key: &str,
        provider_status: &str,
        actual_cost: UsageAmount,
        now: SystemTime,
    ) -> Result<ProviderJobReceipt, StoreError> {
        validate_nonempty("provider_status", provider_status)?;
        validate_usage(&actual_cost)?;
        self.with_verified_lease_at(owner, token, now, || {
            let timestamp = unix_seconds(now)?;
            let mut receipt = self
                .provider_job_unlocked(idempotency_key)?
                .ok_or_else(|| StoreError::ProviderJobNotFound(idempotency_key.to_owned()))?;
            require_status(
                &receipt,
                &[
                    ProviderJobStatus::Submitted,
                    ProviderJobStatus::TimedOut,
                    ProviderJobStatus::Promoting,
                ],
            )?;

            let staging = self.project_root.join(&receipt.staging_path);
            let output = resolve_project_relative(&self.project_root, &receipt.output_path)?;
            if receipt.status != ProviderJobStatus::Promoting {
                if !is_direct_file(&staging)? {
                    return Err(StoreError::MissingArtifact(staging));
                }
                verify_prepared_output(&self.project_root, &output, &receipt)?;
                let staged = Artifact::from_path(&self.project_root, &staging)?;
                receipt.status = ProviderJobStatus::Promoting;
                receipt.provider_status = Some(provider_status.to_owned());
                receipt.actual_cost = Some(actual_cost.clone());
                receipt.output_digest = Some(format!("sha256:{}", staged.sha256));
                receipt.updated_at = timestamp;
                self.write_provider_job(&receipt)?;
            } else if receipt.actual_cost.as_ref() != Some(&actual_cost) {
                return Err(StoreError::IdempotencyConflict(
                    receipt.idempotency_key.clone(),
                ));
            }

            if is_direct_file(&staging)? {
                verify_output_digest(&self.project_root, &staging, &receipt)?;
                ensure_direct_parent(&self.project_root, &output)?;
                fs::rename(&staging, &output)?;
                File::open(&output)?.sync_all()?;
                if let Some(parent) = output.parent() {
                    File::open(parent)?.sync_all()?;
                }
            } else if !is_direct_file(&output)? {
                return Err(StoreError::MissingArtifact(staging));
            } else {
                verify_output_digest(&self.project_root, &output, &receipt)?;
            }

            let artifact = Artifact::from_path(&self.project_root, &output)?;
            receipt.status = ProviderJobStatus::Succeeded;
            receipt.provider_status = Some(provider_status.to_owned());
            receipt.output_digest = Some(format!("sha256:{}", artifact.sha256));
            receipt.actual_cost = Some(actual_cost);
            receipt.error = None;
            receipt.updated_at = timestamp;
            receipt.completed_at = Some(timestamp);
            self.write_provider_job(&receipt)?;
            Ok(receipt)
        })
    }

    fn update_provider_at(
        &self,
        owner: &str,
        token: &str,
        idempotency_key: &str,
        now: SystemTime,
        update: impl FnOnce(&mut ProviderJobReceipt, u64) -> Result<(), StoreError>,
    ) -> Result<ProviderJobReceipt, StoreError> {
        validate_idempotency_key(idempotency_key)?;
        self.with_verified_lease_at(owner, token, now, || {
            let timestamp = unix_seconds(now)?;
            let mut receipt = self
                .provider_job_unlocked(idempotency_key)?
                .ok_or_else(|| StoreError::ProviderJobNotFound(idempotency_key.to_owned()))?;
            update(&mut receipt, timestamp)?;
            self.write_provider_job(&receipt)?;
            Ok(receipt)
        })
    }

    fn provider_jobs_dir(&self) -> PathBuf {
        self.state_dir().join(PROVIDER_JOBS_DIR)
    }

    fn provider_job_unlocked(
        &self,
        idempotency_key: &str,
    ) -> Result<Option<ProviderJobReceipt>, StoreError> {
        validate_idempotency_key(idempotency_key)?;
        let path = self
            .provider_jobs_dir()
            .join(format!("{idempotency_key}.json"));
        match fs::read(&path) {
            Ok(bytes) => {
                let receipt: ProviderJobReceipt = serde_json::from_slice(&bytes)?;
                validate_provider_receipt(&path, &receipt)?;
                Ok(Some(receipt))
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error.into()),
        }
    }

    fn write_provider_job(&self, receipt: &ProviderJobReceipt) -> Result<(), StoreError> {
        write_json_atomically(
            &self.provider_jobs_dir(),
            &format!("{}.json", receipt.idempotency_key),
            receipt,
        )
    }
}

impl ProviderJobReceipt {
    fn matches_request(&self, request: &ProviderRequest) -> bool {
        self.schema_version == 1
            && self.provider == request.provider
            && self.model == request.model
            && self.gate == request.gate
            && self.input_digest == request.input_digest
            && self.idempotency_key == request.idempotency_key
            && self.estimated_cost == request.estimated_cost
            && self.output_path == request.output_path
            && self.output_policy == request.output_policy
    }
}

fn require_status(
    receipt: &ProviderJobReceipt,
    allowed: &[ProviderJobStatus],
) -> Result<(), StoreError> {
    if allowed.contains(&receipt.status) {
        Ok(())
    } else {
        Err(StoreError::InvalidProviderTransition {
            key: receipt.idempotency_key.clone(),
            status: receipt.status.name().to_owned(),
        })
    }
}

fn validate_provider_request(request: &ProviderRequest) -> Result<(), StoreError> {
    validate_nonempty("provider", &request.provider)?;
    validate_nonempty("model", &request.model)?;
    validate_nonempty("gate", &request.gate)?;
    validate_idempotency_key(&request.idempotency_key)?;
    validate_usage(&request.estimated_cost)?;
    validate_digest(&request.input_digest)?;
    validate_relative_path(&request.output_path)
}

fn validate_provider_receipt(path: &Path, receipt: &ProviderJobReceipt) -> Result<(), StoreError> {
    if receipt.schema_version != 1
        || path.file_stem().and_then(OsStr::to_str) != Some(&receipt.idempotency_key)
        || receipt.staging_path
            != format!(
                ".hvp/{PROVIDER_STAGING_DIR}/{}.part",
                receipt.idempotency_key
            )
        || receipt.updated_at < receipt.prepared_at
    {
        return invalid_provider_receipt(path);
    }
    validate_nonempty("project", &receipt.project)?;
    validate_provider_request(&ProviderRequest {
        provider: receipt.provider.clone(),
        model: receipt.model.clone(),
        gate: receipt.gate.clone(),
        input_digest: receipt.input_digest.clone(),
        idempotency_key: receipt.idempotency_key.clone(),
        estimated_cost: receipt.estimated_cost.clone(),
        output_path: receipt.output_path.clone(),
        output_policy: receipt.output_policy,
    })?;
    validate_relative_path(&receipt.staging_path)?;
    if let Some(amount) = &receipt.actual_cost {
        validate_usage(amount)?;
        if amount.unit != receipt.estimated_cost.unit {
            return invalid_provider_receipt(path);
        }
    }
    if let Some(digest) = &receipt.output_digest {
        validate_digest(digest)?;
    }
    if let Some(job_id) = &receipt.provider_job_id {
        validate_nonempty("provider_job_id", job_id)?;
    }
    if let Some(status) = &receipt.provider_status {
        validate_nonempty("provider_status", status)?;
    }
    if let Some(error) = &receipt.error {
        validate_nonempty("error", error)?;
    }

    let valid_state = match receipt.status {
        ProviderJobStatus::Prepared => {
            receipt.provider_job_id.is_none()
                && receipt.provider_status.is_none()
                && receipt.actual_cost.is_none()
                && receipt.output_digest.is_none()
                && receipt.error.is_none()
                && receipt.completed_at.is_none()
        }
        ProviderJobStatus::Submitted | ProviderJobStatus::TimedOut => {
            receipt.provider_job_id.is_some()
                && receipt.provider_status.is_some()
                && receipt.actual_cost.is_none()
                && receipt.output_digest.is_none()
                && receipt.completed_at.is_none()
        }
        ProviderJobStatus::SubmissionUnknown => {
            receipt.provider_job_id.is_none()
                && receipt.actual_cost.is_none()
                && receipt.output_digest.is_none()
                && receipt.error.is_some()
                && receipt.completed_at.is_none()
        }
        ProviderJobStatus::Failed => {
            receipt.provider_status.is_some()
                && receipt.actual_cost.is_none()
                && receipt.output_digest.is_none()
                && receipt.error.is_some()
                && receipt.completed_at.is_none()
        }
        ProviderJobStatus::Promoting => {
            receipt.provider_job_id.is_some()
                && receipt.provider_status.is_some()
                && receipt.actual_cost.is_some()
                && receipt.output_digest.is_some()
                && receipt.completed_at.is_none()
        }
        ProviderJobStatus::Succeeded => {
            receipt.provider_job_id.is_some()
                && receipt.provider_status.is_some()
                && receipt.actual_cost.is_some()
                && receipt.output_digest.is_some()
                && receipt.error.is_none()
                && receipt
                    .completed_at
                    .is_some_and(|time| time >= receipt.prepared_at)
        }
    };
    if !valid_state {
        return invalid_provider_receipt(path);
    }
    Ok(())
}

fn invalid_provider_receipt(path: &Path) -> Result<(), StoreError> {
    Err(StoreError::InvalidProviderRequest(format!(
        "invalid provider receipt: {}",
        path.display()
    )))
}

fn validate_nonempty(field: &str, value: &str) -> Result<(), StoreError> {
    if value.trim().is_empty() || value.chars().any(char::is_control) {
        return Err(StoreError::InvalidProviderRequest(field.to_owned()));
    }
    Ok(())
}

fn validate_usage(amount: &UsageAmount) -> Result<(), StoreError> {
    validate_nonempty("cost unit", &amount.unit)?;
    if amount.amount == 0 {
        return Err(StoreError::InvalidProviderRequest(
            "cost amount must be greater than zero".to_owned(),
        ));
    }
    Ok(())
}

fn validate_digest(digest: &str) -> Result<(), StoreError> {
    let Some(value) = digest.strip_prefix("sha256:") else {
        return Err(StoreError::InvalidProviderRequest(
            "input_digest".to_owned(),
        ));
    };
    if value.len() != 64 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(StoreError::InvalidProviderRequest(
            "input_digest".to_owned(),
        ));
    }
    Ok(())
}

fn validate_idempotency_key(key: &str) -> Result<(), StoreError> {
    if key.is_empty()
        || key.len() > 128
        || !key
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    {
        return Err(StoreError::InvalidProviderRequest(
            "idempotency_key".to_owned(),
        ));
    }
    Ok(())
}

fn validate_relative_path(path: &str) -> Result<(), StoreError> {
    let path = Path::new(path);
    if path.as_os_str().is_empty()
        || path.is_absolute()
        || !path
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
    {
        return Err(StoreError::InvalidProviderRequest("output_path".to_owned()));
    }
    Ok(())
}

fn resolve_project_relative(project: &Path, path: &str) -> Result<PathBuf, StoreError> {
    validate_relative_path(path)?;
    Ok(project.join(path))
}

fn project_name(project: &Path) -> String {
    project
        .file_name()
        .and_then(OsStr::to_str)
        .unwrap_or("project")
        .to_owned()
}

fn is_direct_file(path: &Path) -> Result<bool, StoreError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => Ok(metadata.is_file() && !metadata.file_type().is_symlink()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.into()),
    }
}

fn verify_prepared_output(
    project: &Path,
    output: &Path,
    receipt: &ProviderJobReceipt,
) -> Result<(), StoreError> {
    match (&receipt.supersedes, is_direct_file(output)?) {
        (None, false) => Ok(()),
        (Some(expected), true) => {
            let current = Artifact::from_path(project, output)?;
            if &current == expected {
                Ok(())
            } else {
                Err(StoreError::ProviderOutputChanged(output.to_path_buf()))
            }
        }
        (None, true) | (Some(_), false) => {
            Err(StoreError::ProviderOutputChanged(output.to_path_buf()))
        }
    }
}

fn verify_output_digest(
    project: &Path,
    output: &Path,
    receipt: &ProviderJobReceipt,
) -> Result<(), StoreError> {
    let actual = Artifact::from_path(project, output)?;
    if receipt.output_digest.as_deref() == Some(&format!("sha256:{}", actual.sha256)) {
        Ok(())
    } else {
        Err(StoreError::ProviderOutputChanged(output.to_path_buf()))
    }
}

fn ensure_direct_parent(project: &Path, output: &Path) -> Result<(), StoreError> {
    let relative = output
        .strip_prefix(project)
        .map_err(|_| StoreError::ArtifactOutsideProject(output.to_path_buf()))?;
    let mut current = project.to_path_buf();
    if let Some(parent) = relative.parent() {
        for component in parent.components() {
            let Component::Normal(name) = component else {
                return Err(StoreError::ArtifactOutsideProject(output.to_path_buf()));
            };
            current.push(name);
            match fs::symlink_metadata(&current) {
                Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {}
                Ok(_) => {
                    return Err(StoreError::ArtifactOutsideProject(current));
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    fs::create_dir(&current)?;
                }
                Err(error) => return Err(error.into()),
            }
        }
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct BudgetCaps {
    pub project: u64,
    pub cycle: u64,
    pub day: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BudgetRequest {
    pub idempotency_key: String,
    pub provider: String,
    pub project: String,
    pub cycle: String,
    pub day: String,
    pub amount: UsageAmount,
    pub caps: BudgetCaps,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BudgetReservation {
    pub idempotency_key: String,
    pub provider: String,
    pub project: String,
    pub cycle: String,
    pub day: String,
    pub amount: UsageAmount,
    pub caps: BudgetCaps,
    pub reserved_at: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct SpentPeriod {
    pub total: u64,
    pub projects: BTreeMap<String, u64>,
    pub days: BTreeMap<String, u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BudgetCommit {
    pub reservation: BudgetReservation,
    pub actual: UsageAmount,
    pub completed_at: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BudgetLedger {
    pub schema_version: u32,
    pub unit: Option<String>,
    pub spent: BTreeMap<String, SpentPeriod>,
    pub reservations: BTreeMap<String, BudgetReservation>,
    pub committed: BTreeMap<String, BudgetCommit>,
}

impl Default for BudgetLedger {
    fn default() -> Self {
        Self {
            schema_version: 1,
            unit: None,
            spent: BTreeMap::new(),
            reservations: BTreeMap::new(),
            committed: BTreeMap::new(),
        }
    }
}

#[derive(Debug, Clone)]
pub struct BudgetStore {
    path: PathBuf,
}

impl BudgetStore {
    pub fn new(path: impl AsRef<Path>) -> Self {
        Self {
            path: path.as_ref().to_path_buf(),
        }
    }

    pub fn load(&self) -> Result<BudgetLedger, StoreError> {
        let ledger = self.load_unlocked()?;
        validate_ledger(&ledger)?;
        Ok(ledger)
    }

    pub fn reserve_at(
        &self,
        request: &BudgetRequest,
        now: SystemTime,
    ) -> Result<BudgetReservation, StoreError> {
        validate_budget_request(request)?;
        let _lock = self.lock()?;
        let mut ledger = self.load_unlocked()?;
        validate_ledger(&ledger)?;
        if let Some(existing) = ledger.reservations.get(&request.idempotency_key) {
            if existing.matches(request) {
                return Ok(existing.clone());
            }
            return Err(StoreError::IdempotencyConflict(
                request.idempotency_key.clone(),
            ));
        }
        if ledger.committed.contains_key(&request.idempotency_key) {
            return Err(StoreError::IdempotencyConflict(
                request.idempotency_key.clone(),
            ));
        }
        if let Some(unit) = &ledger.unit
            && unit != &request.amount.unit
        {
            return Err(StoreError::InvalidBudget(format!(
                "ledger unit is {unit}, request unit is {}",
                request.amount.unit
            )));
        }

        let period = ledger
            .spent
            .get(&request.cycle)
            .cloned()
            .unwrap_or_default();
        let mut cycle_used = period.total;
        let mut project_used = period.projects.get(&request.project).copied().unwrap_or(0);
        let mut day_used = period.days.get(&request.day).copied().unwrap_or(0);
        for reservation in ledger.reservations.values().filter(|reservation| {
            reservation.cycle == request.cycle && reservation.amount.unit == request.amount.unit
        }) {
            cycle_used = checked_add(cycle_used, reservation.amount.amount)?;
            if reservation.project == request.project {
                project_used = checked_add(project_used, reservation.amount.amount)?;
            }
            if reservation.day == request.day {
                day_used = checked_add(day_used, reservation.amount.amount)?;
            }
        }
        check_cap(
            "project",
            checked_add(project_used, request.amount.amount)?,
            request.caps.project,
        )?;
        check_cap(
            "cycle",
            checked_add(cycle_used, request.amount.amount)?,
            request.caps.cycle,
        )?;
        check_cap(
            "day",
            checked_add(day_used, request.amount.amount)?,
            request.caps.day,
        )?;

        let reservation = BudgetReservation {
            idempotency_key: request.idempotency_key.clone(),
            provider: request.provider.clone(),
            project: request.project.clone(),
            cycle: request.cycle.clone(),
            day: request.day.clone(),
            amount: request.amount.clone(),
            caps: request.caps,
            reserved_at: unix_seconds(now)?,
        };
        ledger.unit = Some(request.amount.unit.clone());
        ledger
            .reservations
            .insert(request.idempotency_key.clone(), reservation.clone());
        self.write(&ledger)?;
        Ok(reservation)
    }

    pub fn commit_at(
        &self,
        idempotency_key: &str,
        actual: UsageAmount,
        now: SystemTime,
    ) -> Result<BudgetCommit, StoreError> {
        validate_idempotency_key(idempotency_key)?;
        validate_usage(&actual)?;
        let _lock = self.lock()?;
        let mut ledger = self.load_unlocked()?;
        validate_ledger(&ledger)?;
        if let Some(existing) = ledger.committed.get(idempotency_key) {
            if existing.actual == actual {
                return Ok(existing.clone());
            }
            return Err(StoreError::IdempotencyConflict(idempotency_key.to_owned()));
        }
        let reservation = ledger
            .reservations
            .remove(idempotency_key)
            .ok_or_else(|| StoreError::BudgetReservationNotFound(idempotency_key.to_owned()))?;
        if reservation.amount.unit != actual.unit {
            return Err(StoreError::InvalidBudget(format!(
                "reservation unit is {}, actual unit is {}",
                reservation.amount.unit, actual.unit
            )));
        }
        let period = ledger.spent.entry(reservation.cycle.clone()).or_default();
        period.total = checked_add(period.total, actual.amount)?;
        let project = period
            .projects
            .entry(reservation.project.clone())
            .or_default();
        *project = checked_add(*project, actual.amount)?;
        let day = period.days.entry(reservation.day.clone()).or_default();
        *day = checked_add(*day, actual.amount)?;
        let commit = BudgetCommit {
            reservation,
            actual,
            completed_at: unix_seconds(now)?,
        };
        ledger
            .committed
            .insert(idempotency_key.to_owned(), commit.clone());
        self.write(&ledger)?;
        Ok(commit)
    }

    pub fn release(&self, idempotency_key: &str) -> Result<(), StoreError> {
        validate_idempotency_key(idempotency_key)?;
        let _lock = self.lock()?;
        let mut ledger = self.load_unlocked()?;
        validate_ledger(&ledger)?;
        if ledger.reservations.remove(idempotency_key).is_none() {
            return Err(StoreError::BudgetReservationNotFound(
                idempotency_key.to_owned(),
            ));
        }
        self.write(&ledger)
    }

    fn load_unlocked(&self) -> Result<BudgetLedger, StoreError> {
        match fs::read(&self.path) {
            Ok(bytes) => Ok(serde_json::from_slice(&bytes)?),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(BudgetLedger::default()),
            Err(error) => Err(error.into()),
        }
    }

    fn write(&self, ledger: &BudgetLedger) -> Result<(), StoreError> {
        let parent = self
            .path
            .parent()
            .ok_or_else(|| StoreError::InvalidBudget("ledger path has no parent".to_owned()))?;
        let file_name = self
            .path
            .file_name()
            .and_then(OsStr::to_str)
            .ok_or_else(|| StoreError::InvalidBudget("ledger filename is not UTF-8".to_owned()))?;
        write_json_atomically(parent, file_name, ledger)
    }

    fn lock(&self) -> Result<File, StoreError> {
        let parent = self
            .path
            .parent()
            .ok_or_else(|| StoreError::InvalidBudget("ledger path has no parent".to_owned()))?;
        fs::create_dir_all(parent)?;
        let file_name = self
            .path
            .file_name()
            .and_then(OsStr::to_str)
            .ok_or_else(|| StoreError::InvalidBudget("ledger filename is not UTF-8".to_owned()))?;
        let lock = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(parent.join(format!("{file_name}.lock")))?;
        lock.lock_exclusive()?;
        Ok(lock)
    }
}

impl BudgetReservation {
    fn matches(&self, request: &BudgetRequest) -> bool {
        self.provider == request.provider
            && self.project == request.project
            && self.cycle == request.cycle
            && self.day == request.day
            && self.amount == request.amount
            && self.caps == request.caps
    }
}

fn validate_budget_request(request: &BudgetRequest) -> Result<(), StoreError> {
    validate_idempotency_key(&request.idempotency_key)?;
    validate_nonempty("provider", &request.provider)?;
    validate_nonempty("project", &request.project)?;
    validate_nonempty("cycle", &request.cycle)?;
    validate_nonempty("day", &request.day)?;
    validate_usage(&request.amount)?;
    if request.caps.project == 0 || request.caps.cycle == 0 || request.caps.day == 0 {
        return Err(StoreError::InvalidBudget(
            "budget caps must be greater than zero".to_owned(),
        ));
    }
    Ok(())
}

fn validate_ledger(ledger: &BudgetLedger) -> Result<(), StoreError> {
    if ledger.schema_version != 1 {
        return Err(StoreError::InvalidBudget(format!(
            "unsupported schema_version {}",
            ledger.schema_version
        )));
    }
    if let Some(unit) = &ledger.unit {
        validate_nonempty("ledger unit", unit)?;
        for (key, reservation) in &ledger.reservations {
            if key != &reservation.idempotency_key || reservation.amount.unit != *unit {
                return Err(StoreError::InvalidBudget(format!(
                    "invalid reservation {key}"
                )));
            }
            validate_budget_request(&BudgetRequest {
                idempotency_key: reservation.idempotency_key.clone(),
                provider: reservation.provider.clone(),
                project: reservation.project.clone(),
                cycle: reservation.cycle.clone(),
                day: reservation.day.clone(),
                amount: reservation.amount.clone(),
                caps: reservation.caps,
            })?;
        }
        for (key, commit) in &ledger.committed {
            if key != &commit.reservation.idempotency_key || commit.actual.unit != *unit {
                return Err(StoreError::InvalidBudget(format!("invalid commit {key}")));
            }
        }
    } else if !ledger.reservations.is_empty()
        || !ledger.committed.is_empty()
        || !ledger.spent.is_empty()
    {
        return Err(StoreError::InvalidBudget(
            "ledger has entries without a unit".to_owned(),
        ));
    }
    Ok(())
}

fn checked_add(left: u64, right: u64) -> Result<u64, StoreError> {
    left.checked_add(right)
        .ok_or_else(|| StoreError::InvalidBudget("amount overflow".to_owned()))
}

fn check_cap(scope: &str, attempted: u64, limit: u64) -> Result<(), StoreError> {
    if attempted > limit {
        Err(StoreError::BudgetExceeded {
            scope: scope.to_owned(),
            attempted,
            limit,
        })
    } else {
        Ok(())
    }
}
