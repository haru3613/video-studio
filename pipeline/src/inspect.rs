use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::provider::ProviderJobReceipt;
use crate::receipt::GateReceipt;
use crate::store::{Lease, ProjectStore, StoreError, unix_seconds, write_json_atomically};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GateStatus {
    Missing,
    Complete,
    Warning,
    Failed,
}

impl GateStatus {
    fn allows_resume_past(self) -> bool {
        self == Self::Complete
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Artifact {
    pub path: String,
    pub bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GateSnapshot {
    pub status: GateStatus,
    pub input_digest: String,
    pub output_digest: Option<String>,
    pub artifacts: Vec<Artifact>,
    pub notes: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LeaseStatus {
    pub owner: String,
    pub claimed_at: u64,
    pub expires_at: u64,
}

impl From<Lease> for LeaseStatus {
    fn from(lease: Lease) -> Self {
        Self {
            owner: lease.owner,
            claimed_at: lease.claimed_at,
            expires_at: lease.expires_at,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProjectSnapshot {
    pub schema_version: u32,
    pub project: String,
    pub updated_at: u64,
    pub gate_order: Vec<String>,
    pub last_successful_gate: Option<String>,
    pub lease: Option<LeaseStatus>,
    pub gates: BTreeMap<String, GateSnapshot>,
    pub receipts: BTreeMap<String, GateReceipt>,
    #[serde(default)]
    pub provider_jobs: BTreeMap<String, ProviderJobReceipt>,
}

impl ProjectSnapshot {
    pub fn next_gate(&self) -> Option<&str> {
        self.gate_order.iter().find_map(|gate| {
            self.gates
                .get(gate)
                .is_none_or(|snapshot| !snapshot.status.allows_resume_past())
                .then_some(gate.as_str())
        })
    }
}

impl ProjectStore {
    pub fn canonical_snapshot_at(
        &self,
        status: &Value,
        now: SystemTime,
    ) -> Result<ProjectSnapshot, StoreError> {
        if !self.project_root.is_dir() {
            return Err(StoreError::InvalidProject(self.project_root.clone()));
        }
        let result = status
            .as_object()
            .filter(|result| {
                result.get("schema").and_then(Value::as_str) == Some("haru.pipeline_status.v1")
                    && result.get("project").and_then(Value::as_str)
                        == self.project_root.file_name().and_then(|name| name.to_str())
            })
            .ok_or_else(|| StoreError::InvalidStatus("schema or project mismatch".to_owned()))?;
        let gate_order = result
            .get("required_stages")
            .and_then(Value::as_array)
            .ok_or_else(|| StoreError::InvalidStatus("required_stages is missing".to_owned()))?
            .iter()
            .map(|gate| {
                gate.as_str()
                    .filter(|gate| !gate.is_empty())
                    .map(str::to_owned)
                    .ok_or_else(|| {
                        StoreError::InvalidStatus(
                            "required_stages contains an invalid gate".to_owned(),
                        )
                    })
            })
            .collect::<Result<Vec<_>, _>>()?;
        let stage_values = result
            .get("stages")
            .and_then(Value::as_object)
            .ok_or_else(|| StoreError::InvalidStatus("stages is missing".to_owned()))?;

        let receipts = self.load_receipts()?;
        let mut gates = BTreeMap::new();
        let mut chain = digest_parts(std::iter::empty::<&str>());
        let mut last_successful_gate = None;
        let mut contiguous = true;

        for name in &gate_order {
            let stage = stage_values
                .get(name)
                .and_then(Value::as_object)
                .ok_or_else(|| StoreError::InvalidStatus(format!("stage {name} is missing")))?;
            let status = match stage.get("status").and_then(Value::as_str) {
                Some("pass") => GateStatus::Complete,
                Some("warn") => GateStatus::Warning,
                Some("missing") => GateStatus::Missing,
                _ => GateStatus::Failed,
            };
            let mut artifacts = Vec::new();
            if let Some(files) = stage.get("files").and_then(Value::as_array) {
                for relative in files.iter().filter_map(Value::as_str) {
                    let path = self.project_root.join(relative);
                    if path.is_file() {
                        artifacts.push(Artifact::from_path(&self.project_root, &path)?);
                    }
                }
            }
            let output_digest = (!artifacts.is_empty()).then(|| bundle_digest(&artifacts));
            let input_digest = chain.clone();
            chain = digest_parts([
                chain.as_str(),
                name,
                status_name(status),
                output_digest.as_deref().unwrap_or(""),
            ]);
            if contiguous && status.allows_resume_past() {
                last_successful_gate = Some(name.clone());
            } else {
                contiguous = false;
            }
            let notes = ["warnings", "notes"]
                .into_iter()
                .filter_map(|field| stage.get(field).and_then(Value::as_array))
                .flatten()
                .map(|note| {
                    note.as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| note.to_string())
                })
                .collect();
            gates.insert(
                name.clone(),
                GateSnapshot {
                    status,
                    input_digest,
                    output_digest,
                    artifacts,
                    notes,
                },
            );
        }

        Ok(ProjectSnapshot {
            schema_version: 1,
            project: self
                .project_root
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("project")
                .to_owned(),
            updated_at: unix_seconds(now)?,
            gate_order,
            last_successful_gate,
            lease: self.current_lease()?.map(LeaseStatus::from),
            gates,
            receipts,
            provider_jobs: self.load_provider_jobs()?,
        })
    }

    pub fn refresh_from_status_at(
        &self,
        owner: &str,
        token: &str,
        status: &Value,
        now: SystemTime,
    ) -> Result<ProjectSnapshot, StoreError> {
        self.with_verified_lease_at(owner, token, now, || {
            let snapshot = self.canonical_snapshot_at(status, now)?;
            write_json_atomically(&self.state_dir(), "state.json", &snapshot)?;
            Ok(snapshot)
        })
    }

    pub fn load_snapshot(&self) -> Result<Option<ProjectSnapshot>, StoreError> {
        match fs::read(self.state_dir().join("state.json")) {
            Ok(bytes) => Ok(Some(serde_json::from_slice(&bytes)?)),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error.into()),
        }
    }
}

pub(crate) fn resolve_project_path(project: &Path, path: &Path) -> Option<PathBuf> {
    if path.is_absolute() {
        path.starts_with(project).then(|| path.to_path_buf())
    } else {
        Some(project.join(path))
    }
}

impl Artifact {
    pub(crate) fn from_path(project: &Path, path: &Path) -> Result<Self, StoreError> {
        let project = project.canonicalize()?;
        let path = path.canonicalize()?;
        let relative = path
            .strip_prefix(&project)
            .map_err(|_| StoreError::ArtifactOutsideProject(path.clone()))?;
        let (bytes, sha256) = hash_file_with_metadata(&path)?;
        Ok(Self {
            path: relative.to_string_lossy().into_owned(),
            bytes,
            sha256,
        })
    }
}

fn hash_file_with_metadata(path: &Path) -> Result<(u64, String), StoreError> {
    let mut file = File::open(path)?;
    let before = file.metadata()?;
    let before_modified = before.modified().ok();
    let mut hash = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hash.update(&buffer[..read]);
    }
    let after = file.metadata()?;
    if before.len() != after.len() || before_modified != after.modified().ok() {
        return Err(StoreError::ArtifactChanged(path.to_path_buf()));
    }
    Ok((after.len(), format!("{:x}", hash.finalize())))
}

pub(crate) fn bundle_digest(artifacts: &[Artifact]) -> String {
    let mut sorted = artifacts.to_vec();
    sorted.sort_by(|left, right| left.path.cmp(&right.path));
    let mut hash = Sha256::new();
    for artifact in sorted {
        hash.update(artifact.path.as_bytes());
        hash.update([0]);
        hash.update(artifact.sha256.as_bytes());
        hash.update([0]);
        hash.update(artifact.bytes.to_string().as_bytes());
        hash.update([0]);
    }
    format!("sha256:{:x}", hash.finalize())
}

fn digest_parts<'a>(parts: impl IntoIterator<Item = &'a str>) -> String {
    let mut hash = Sha256::new();
    for part in parts {
        hash.update(part.as_bytes());
        hash.update([0]);
    }
    format!("sha256:{:x}", hash.finalize())
}

fn status_name(status: GateStatus) -> &'static str {
    match status {
        GateStatus::Missing => "missing",
        GateStatus::Complete => "complete",
        GateStatus::Warning => "warning",
        GateStatus::Failed => "failed",
    }
}
