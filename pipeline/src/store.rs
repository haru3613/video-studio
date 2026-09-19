use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use fs2::FileExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use uuid::Uuid;

const STATE_DIR: &str = ".hvp";
const LEASE_FILE: &str = "lease.json";
const LEASE_LOCK: &str = "lease.lock";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Lease {
    pub owner: String,
    pub lease_id: String,
    pub generation: u64,
    pub claimed_at: u64,
    pub expires_at: u64,
    #[serde(skip_serializing, skip_deserializing, default)]
    pub token: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct LeaseRecord {
    schema: String,
    owner: String,
    lease_id: String,
    generation: u64,
    claimed_at: u64,
    expires_at: u64,
    capability_sha256: String,
    active: bool,
}

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("project root does not exist or is not a directory: {0}")]
    InvalidProject(PathBuf),
    #[error("invalid lease owner")]
    InvalidOwner,
    #[error("lease ttl must be greater than zero")]
    InvalidTtl,
    #[error("project is leased by {owner} until {expires_at}")]
    LeaseHeld { owner: String, expires_at: u64 },
    #[error("lease owner or token does not match")]
    LeaseMismatch,
    #[error("lease is expired")]
    LeaseExpired,
    #[error("invalid gate name: {0}")]
    InvalidGate(String),
    #[error("artifact does not exist: {0}")]
    MissingArtifact(PathBuf),
    #[error("artifact changed while it was being hashed: {0}")]
    ArtifactChanged(PathBuf),
    #[error("artifact is outside the project root: {0}")]
    ArtifactOutsideProject(PathBuf),
    #[error("invalid gate receipt: {0}")]
    InvalidReceipt(String),
    #[error("invalid provider request: {0}")]
    InvalidProviderRequest(String),
    #[error("idempotency key conflicts with an existing request: {0}")]
    IdempotencyConflict(String),
    #[error("provider output already exists: {0}")]
    ProviderOutputExists(PathBuf),
    #[error("provider output changed after request preparation: {0}")]
    ProviderOutputChanged(PathBuf),
    #[error("provider job does not exist: {0}")]
    ProviderJobNotFound(String),
    #[error("invalid provider job transition for {key}: {status}")]
    InvalidProviderTransition { key: String, status: String },
    #[error("invalid budget ledger: {0}")]
    InvalidBudget(String),
    #[error("budget exceeded for {scope}: attempted {attempted}, limit {limit}")]
    BudgetExceeded {
        scope: String,
        attempted: u64,
        limit: u64,
    },
    #[error("budget reservation does not exist: {0}")]
    BudgetReservationNotFound(String),
    #[error("system time is before the Unix epoch")]
    InvalidSystemTime,
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
    #[error("invalid JSON: {0}")]
    Json(#[from] serde_json::Error),
    #[error("invalid canonical status: {0}")]
    InvalidStatus(String),
}

#[derive(Debug, Clone)]
pub struct ProjectStore {
    pub(crate) project_root: PathBuf,
}

impl ProjectStore {
    pub fn new(project_root: impl AsRef<Path>) -> Self {
        Self {
            project_root: project_root.as_ref().to_path_buf(),
        }
    }

    pub fn claim_at(
        &self,
        owner: &str,
        ttl: Duration,
        now: SystemTime,
    ) -> Result<Lease, StoreError> {
        if owner.trim().is_empty() {
            return Err(StoreError::InvalidOwner);
        }
        if ttl.as_secs() == 0 {
            return Err(StoreError::InvalidTtl);
        }

        let now = unix_seconds(now)?;
        let expires_at = now
            .checked_add(ttl.as_secs())
            .ok_or(StoreError::InvalidSystemTime)?;
        let _lock = self.exclusive_lock()?;
        let previous = self.read_lease_record_unlocked()?;
        if let Some(existing) = previous.as_ref()
            && existing.active
            && existing.expires_at > now
        {
            return Err(StoreError::LeaseHeld {
                owner: existing.owner.clone(),
                expires_at: existing.expires_at,
            });
        }

        let generation = previous
            .as_ref()
            .map_or(1, |lease| lease.generation.saturating_add(1));
        let token = new_capability();
        let record = LeaseRecord {
            schema: "haru.project_lease.v2".to_owned(),
            owner: owner.to_owned(),
            lease_id: Uuid::new_v4().to_string(),
            generation,
            claimed_at: now,
            expires_at,
            capability_sha256: sha256_text(&token),
            active: true,
        };
        write_json_atomically(&self.state_dir(), LEASE_FILE, &record)?;
        Ok(lease_from_record(record, token))
    }

    pub fn release(&self, owner: &str, token: &str) -> Result<(), StoreError> {
        self.release_at(owner, token, SystemTime::now())
    }

    pub fn renew_at(
        &self,
        owner: &str,
        token: &str,
        ttl: Duration,
        now: SystemTime,
    ) -> Result<Lease, StoreError> {
        if ttl.as_secs() == 0 {
            return Err(StoreError::InvalidTtl);
        }
        let now = unix_seconds(now)?;
        let expires_at = now
            .checked_add(ttl.as_secs())
            .ok_or(StoreError::InvalidSystemTime)?;
        let _lock = self.exclusive_lock()?;
        let mut record = self
            .read_lease_record_unlocked()?
            .ok_or(StoreError::LeaseMismatch)?;
        verify_record(&record, owner, token, now)?;
        record.expires_at = expires_at;
        write_json_atomically(&self.state_dir(), LEASE_FILE, &record)?;
        Ok(lease_from_record(record, token.to_owned()))
    }

    pub fn release_at(&self, owner: &str, token: &str, now: SystemTime) -> Result<(), StoreError> {
        let now = unix_seconds(now)?;
        let _lock = self.exclusive_lock()?;
        let mut record = self
            .read_lease_record_unlocked()?
            .ok_or(StoreError::LeaseMismatch)?;
        verify_record(&record, owner, token, now)?;
        record.active = false;
        write_json_atomically(&self.state_dir(), LEASE_FILE, &record)
    }

    pub fn current_lease(&self) -> Result<Option<Lease>, StoreError> {
        Ok(self
            .read_lease_record_unlocked()?
            .filter(|lease| lease.active)
            .map(|record| lease_from_record(record, String::new())))
    }

    pub(crate) fn state_dir(&self) -> PathBuf {
        self.project_root.join(STATE_DIR)
    }

    fn lease_path(&self) -> PathBuf {
        self.state_dir().join(LEASE_FILE)
    }

    fn open_lock(&self) -> Result<File, StoreError> {
        if !self.project_root.is_dir() {
            return Err(StoreError::InvalidProject(self.project_root.clone()));
        }
        fs::create_dir_all(self.state_dir())?;
        Ok(OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .truncate(false)
            .open(self.state_dir().join(LEASE_LOCK))?)
    }

    fn exclusive_lock(&self) -> Result<File, StoreError> {
        let file = self.open_lock()?;
        file.lock_exclusive()?;
        Ok(file)
    }

    fn read_lease_record_unlocked(&self) -> Result<Option<LeaseRecord>, StoreError> {
        match fs::read(self.lease_path()) {
            Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error.into()),
        }
    }

    pub(crate) fn with_verified_lease_at<T>(
        &self,
        owner: &str,
        token: &str,
        now: SystemTime,
        action: impl FnOnce() -> Result<T, StoreError>,
    ) -> Result<T, StoreError> {
        let now = unix_seconds(now)?;
        let _lock = self.exclusive_lock()?;
        let record = self
            .read_lease_record_unlocked()?
            .ok_or(StoreError::LeaseMismatch)?;
        verify_record(&record, owner, token, now)?;
        action()
    }
    pub(crate) fn with_verified_lease_identity_at<T>(
        &self,
        owner: &str,
        lease_id: &str,
        generation: u64,
        token: &str,
        now: SystemTime,
        action: impl FnOnce() -> Result<T, StoreError>,
    ) -> Result<T, StoreError> {
        let now = unix_seconds(now)?;
        let _lock = self.exclusive_lock()?;
        let record = self
            .read_lease_record_unlocked()?
            .ok_or(StoreError::LeaseMismatch)?;
        verify_record(&record, owner, token, now)?;
        if record.lease_id != lease_id || record.generation != generation {
            return Err(StoreError::LeaseMismatch);
        }
        action()
    }
}

fn verify_record(
    record: &LeaseRecord,
    owner: &str,
    token: &str,
    now: u64,
) -> Result<(), StoreError> {
    if !record.active || record.owner != owner || record.capability_sha256 != sha256_text(token) {
        return Err(StoreError::LeaseMismatch);
    }
    if record.expires_at <= now {
        return Err(StoreError::LeaseExpired);
    }
    Ok(())
}

fn lease_from_record(record: LeaseRecord, token: String) -> Lease {
    Lease {
        owner: record.owner,
        lease_id: record.lease_id,
        generation: record.generation,
        claimed_at: record.claimed_at,
        expires_at: record.expires_at,
        token,
    }
}

fn new_capability() -> String {
    let material = format!("{}:{}:{}", Uuid::new_v4(), Uuid::new_v4(), Uuid::new_v4());
    sha256_text(&material)
}

fn sha256_text(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}

pub(crate) fn unix_seconds(time: SystemTime) -> Result<u64, StoreError> {
    Ok(time
        .duration_since(UNIX_EPOCH)
        .map_err(|_| StoreError::InvalidSystemTime)?
        .as_secs())
}

pub(crate) fn write_json_atomically<T: Serialize>(
    directory: &Path,
    file_name: &str,
    value: &T,
) -> Result<(), StoreError> {
    fs::create_dir_all(directory)?;
    let target = directory.join(file_name);
    let temporary = directory.join(format!(".{file_name}.{}.tmp", Uuid::new_v4()));
    let bytes = serde_json::to_vec_pretty(value)?;

    let result = (|| -> Result<(), StoreError> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        file.write_all(&bytes)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        fs::rename(&temporary, &target)?;
        File::open(directory)?.sync_all()?;
        Ok(())
    })();

    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}
