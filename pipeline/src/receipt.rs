use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use serde::{Deserialize, Serialize};

use crate::inspect::{Artifact, bundle_digest, resolve_project_path};
use crate::store::{ProjectStore, StoreError, unix_seconds, write_json_atomically};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct GateReceipt {
    pub schema_version: u32,
    pub gate: String,
    pub input_digest: String,
    pub output_digest: String,
    pub outputs: Vec<Artifact>,
    pub completed_at: u64,
}

impl ProjectStore {
    pub fn record_gate_at(
        &self,
        owner: &str,
        token: &str,
        gate: &str,
        input_digest: &str,
        output_paths: &[PathBuf],
        now: SystemTime,
    ) -> Result<GateReceipt, StoreError> {
        validate_gate(gate)?;
        self.with_verified_lease_at(owner, token, now, || {
            let mut outputs = Vec::with_capacity(output_paths.len());
            for raw_path in output_paths {
                let path = resolve_output_path(&self.project_root, raw_path);
                if !path.is_file() {
                    return Err(StoreError::MissingArtifact(path));
                }
                outputs.push(Artifact::from_path(&self.project_root, &path)?);
            }
            if outputs.is_empty() {
                return Err(StoreError::MissingArtifact(self.project_root.clone()));
            }

            let receipt = GateReceipt {
                schema_version: 1,
                gate: gate.to_owned(),
                input_digest: input_digest.to_owned(),
                output_digest: bundle_digest(&outputs),
                outputs,
                completed_at: unix_seconds(now)?,
            };
            write_json_atomically(&self.receipts_dir(), &format!("{gate}.json"), &receipt)?;
            Ok(receipt)
        })
    }

    pub fn should_run(&self, gate: &str, input_digest: &str) -> Result<bool, StoreError> {
        validate_gate(gate)?;
        let Some(receipt) = self.load_receipts()?.remove(gate) else {
            return Ok(true);
        };
        if receipt.input_digest != input_digest {
            return Ok(true);
        }
        Ok(!self.receipt_outputs_are_current(&receipt)?)
    }

    pub(crate) fn receipt_outputs_are_current(
        &self,
        receipt: &GateReceipt,
    ) -> Result<bool, StoreError> {
        let mut current = Vec::with_capacity(receipt.outputs.len());
        for recorded in &receipt.outputs {
            let raw = PathBuf::from(&recorded.path);
            let Some(path) = resolve_project_path(&self.project_root, &raw) else {
                return Ok(false);
            };
            if !path.is_file() {
                return Ok(false);
            }
            current.push(Artifact::from_path(&self.project_root, &path)?);
        }
        Ok(bundle_digest(&current) == receipt.output_digest)
    }

    pub(crate) fn load_receipts(&self) -> Result<BTreeMap<String, GateReceipt>, StoreError> {
        let directory = self.receipts_dir();
        if !directory.is_dir() {
            return Ok(BTreeMap::new());
        }
        let mut receipts = BTreeMap::new();
        for entry in fs::read_dir(directory)? {
            let path = entry?.path();
            if path.extension().and_then(|extension| extension.to_str()) != Some("json") {
                continue;
            }
            let receipt: GateReceipt = serde_json::from_slice(&fs::read(&path)?)?;
            validate_gate(&receipt.gate)?;
            if receipt.schema_version != 1 {
                return Err(StoreError::InvalidReceipt(format!(
                    "{} uses unsupported schema_version {}",
                    path.display(),
                    receipt.schema_version
                )));
            }
            if receipt.outputs.is_empty() {
                return Err(StoreError::InvalidReceipt(format!(
                    "{} has no outputs",
                    path.display()
                )));
            }
            if path.file_stem().and_then(|name| name.to_str()) != Some(&receipt.gate) {
                return Err(StoreError::InvalidReceipt(format!(
                    "{} declares gate {}",
                    path.display(),
                    receipt.gate
                )));
            }
            receipts.insert(receipt.gate.clone(), receipt);
        }
        Ok(receipts)
    }

    fn receipts_dir(&self) -> PathBuf {
        self.state_dir().join("receipts")
    }
}

fn validate_gate(gate: &str) -> Result<(), StoreError> {
    if gate.is_empty()
        || !gate.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_' || byte == b'-'
        })
    {
        return Err(StoreError::InvalidGate(gate.to_owned()));
    }
    Ok(())
}

fn resolve_output_path(project: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        project.join(path)
    }
}
