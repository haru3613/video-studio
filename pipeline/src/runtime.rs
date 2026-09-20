//! One runtime authority.
//!
//! Four clients (Codex, Claude, global Hermes, the Hermes video producer) reach
//! this pipeline through one launcher, and until now nothing proved they were
//! reaching the same bytes: each pointed at whatever `pipeline/target/debug`
//! happened to hold in whichever checkout it had been configured with. This
//! module is the answer to "what exactly is serving me": an identity that binds
//! the promoted commit and tree, the complete runtime source closure, the
//! binary digest, the evaluator/artifact/runtime contract versions, the
//! security-capability floor and the exact MCP tool surface, plus the promotion
//! and startup verification that make the identity a claim a client can check
//! rather than a string the server prints.
//!
//! Everything promoted lives outside the repository, under
//! `~/.local/state/video-studio/runtime`, because a runtime that can be
//! edited by the branch you are working on is not a runtime authority.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::application::{AppResult, ProjectRuntimeContract, read_project_runtime_contract};
use crate::source_fingerprint;
use crate::store::write_json_atomically;

pub const IDENTITY_SCHEMA: &str = "haru.runtime_identity.v1";
pub const RECEIPT_SCHEMA: &str = "haru.runtime_promotion_receipt.v1";
pub const ACTIVE_SCHEMA: &str = "haru.runtime_active.v1";
pub const FLOOR_SCHEMA: &str = "haru.runtime_capability_floor.v1";
pub const UPLOAD_HOLD_SCHEMA: &str = "haru.upload_hold.v1";
pub const PROJECT_CONTRACT_SCHEMA: &str = "haru.project_runtime_contract.v1";

pub const ACTIVE_FILE: &str = "active.json";
pub const ACTIVE_DIGEST_FILE: &str = "active-digest";
pub const ACTIVE_LINK: &str = "active";
pub const SELECTIONS_DIR: &str = "selections";
pub const FLOOR_FILE: &str = "capability-floor.json";
pub const UPLOAD_HOLD_FILE: &str = "upload-hold.json";
pub const DIGESTS_FILE: &str = "DIGESTS";
pub const RELEASES_DIR: &str = "releases";

/// The only runtime-ID grammar this authority accepts: an optional `sha256:`
/// prefix over exactly 64 lowercase hex digits. It returns the bare release
/// directory name, so no caller-supplied component -- `..`, a separator, an
/// absolute path, a name that happens to be a symlink -- is ever joined onto
/// the releases directory.
pub fn canonical_release_name(runtime_id: &str) -> Result<String, RuntimeError> {
    let name = runtime_id.strip_prefix("sha256:").unwrap_or(runtime_id);
    let canonical = name.len() == 64
        && name
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'));
    if !canonical {
        return Err(RuntimeError::Refused(format!(
            "runtime id {runtime_id:?} is not sha256: followed by 64 lowercase hex digits"
        )));
    }
    Ok(name.to_owned())
}

/// Git prints a resolved object name as bare lowercase hex. Anything else is
/// not something to hand back to git as a pinned revision.
fn object_name(value: &str) -> Result<String, RuntimeError> {
    let resolved = matches!(value.len(), 40 | 64)
        && value
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'));
    if !resolved {
        return Err(RuntimeError::Refused(format!(
            "git resolved {value:?}, which is not a commit object name"
        )));
    }
    Ok(value.to_owned())
}

#[derive(Debug, thiserror::Error)]
pub enum RuntimeError {
    #[error("no promoted runtime is active")]
    NoActiveRuntime,
    #[error("promoted runtime failed verification: {0}")]
    Verification(String),
    #[error("promotion refused: {0}")]
    Refused(String),
    #[error("command failed: {0}")]
    Command(String),
    #[error("I/O error: {0}")]
    Io(#[from] io::Error),
    #[error("invalid JSON: {0}")]
    Json(#[from] serde_json::Error),
}

/// The exact MCP tool surface, taken from the router rather than described
/// beside it, so a tool that is added without a manifest entry fails startup.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolSurface {
    pub names: Vec<String>,
    pub digest: String,
}

/// What this runtime is. Every field is either compiled into the binary or
/// recomputed from the promoted bytes at startup; none of it is caller-supplied.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeIdentity {
    pub schema: String,
    pub runtime_id: String,
    pub commit: Option<String>,
    pub tree: Option<String>,
    pub provenance: RuntimeProvenance,
    pub source_fingerprint: String,
    pub manifest_digest: String,
    pub runtime_contract: String,
    pub evaluator_contract: String,
    pub artifact_contract: String,
    pub security_capabilities: Vec<String>,
    pub tool_surface: Vec<String>,
    pub tool_surface_digest: String,
    pub binaries: BTreeMap<String, String>,
}

/// The bytes hashed into `runtime_id`. Field order is the serialization order,
/// so promotion and startup agree without a canonicalization pass.
#[derive(Debug, Serialize)]
struct IdentityDocument<'a> {
    schema: &'a str,
    commit: Option<&'a str>,
    tree: Option<&'a str>,
    provenance: &'a RuntimeProvenance,
    source_fingerprint: &'a str,
    manifest_digest: &'a str,
    runtime_contract: &'a str,
    evaluator_contract: &'a str,
    artifact_contract: &'a str,
    security_capabilities: &'a [String],
    tool_surface: &'a [String],
    tool_surface_digest: &'a str,
    binaries: &'a BTreeMap<String, String>,
}

/// How the immutable source snapshot entered the local runtime store. A local
/// self-build is content-addressed and verified, but it is never represented as
/// an upstream-reviewed release.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RuntimeProvenance {
    SelfBuild,
    TrustedUpstream,
}

impl RuntimeProvenance {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::SelfBuild => "self_build",
            Self::TrustedUpstream => "trusted_upstream",
        }
    }

    fn cli_value(self) -> &'static str {
        match self {
            Self::SelfBuild => "self-build",
            Self::TrustedUpstream => "trusted-upstream",
        }
    }
}

impl RuntimeIdentity {
    /// Compute the identity using the sibling runtime binaries produced by the
    /// same Cargo build as the running process.
    pub fn compute(
        repo_root: &Path,
        surface: &ToolSurface,
        commit: Option<&str>,
        tree: Option<&str>,
    ) -> Result<Self, RuntimeError> {
        let binary_dir = runtime_binary_directory(&std::env::current_exe()?)?;
        Self::compute_with_binaries(repo_root, surface, commit, tree, &binary_dir)
    }

    pub fn compute_with_binaries(
        repo_root: &Path,
        surface: &ToolSurface,
        commit: Option<&str>,
        tree: Option<&str>,
        binary_dir: &Path,
    ) -> Result<Self, RuntimeError> {
        let fingerprint = source_fingerprint::calculate(repo_root)?;
        let binaries = runtime_binary_digests(binary_dir)?;
        Self::assemble(
            &fingerprint,
            surface,
            commit,
            tree,
            RuntimeProvenance::SelfBuild,
            binaries,
        )
    }

    pub fn compute_with_provenance(
        repo_root: &Path,
        surface: &ToolSurface,
        commit: Option<&str>,
        tree: Option<&str>,
        provenance: RuntimeProvenance,
    ) -> Result<Self, RuntimeError> {
        let binary_dir = runtime_binary_directory(&std::env::current_exe()?)?;
        let fingerprint = source_fingerprint::calculate(repo_root)?;
        let binaries = runtime_binary_digests(&binary_dir)?;
        Self::assemble(&fingerprint, surface, commit, tree, provenance, binaries)
    }

    fn assemble(
        fingerprint_hex: &str,
        surface: &ToolSurface,
        commit: Option<&str>,
        tree: Option<&str>,
        provenance: RuntimeProvenance,
        binaries: BTreeMap<String, String>,
    ) -> Result<Self, RuntimeError> {
        let manifest = source_fingerprint::manifest();
        if surface.names != manifest.tools {
            return Err(RuntimeError::Verification(format!(
                "tool surface {:?} does not match the manifest surface {:?}",
                surface.names, manifest.tools
            )));
        }
        let expected_binaries: BTreeSet<_> = manifest.binaries.iter().cloned().collect();
        if binaries.keys().cloned().collect::<BTreeSet<_>>() != expected_binaries {
            return Err(RuntimeError::Verification(
                "runtime binary set does not match the manifest".to_owned(),
            ));
        }
        let source_fingerprint = format!("sha256:{fingerprint_hex}");
        let manifest_digest = format!("sha256:{}", source_fingerprint::manifest_digest());
        let document = IdentityDocument {
            schema: IDENTITY_SCHEMA,
            commit,
            tree,
            provenance: &provenance,
            source_fingerprint: &source_fingerprint,
            manifest_digest: &manifest_digest,
            runtime_contract: &manifest.runtime_contract,
            evaluator_contract: &manifest.evaluator_contract,
            artifact_contract: &manifest.artifact_contract,
            security_capabilities: &manifest.security_capabilities,
            tool_surface: &surface.names,
            tool_surface_digest: &surface.digest,
            binaries: &binaries,
        };
        let runtime_id = format!(
            "sha256:{:x}",
            Sha256::digest(serde_json::to_vec(&document)?)
        );
        Ok(Self {
            schema: IDENTITY_SCHEMA.to_owned(),
            runtime_id,
            commit: commit.map(str::to_owned),
            tree: tree.map(str::to_owned),
            provenance,
            source_fingerprint,
            manifest_digest,
            runtime_contract: manifest.runtime_contract,
            evaluator_contract: manifest.evaluator_contract,
            artifact_contract: manifest.artifact_contract,
            security_capabilities: manifest.security_capabilities,
            tool_surface: surface.names.clone(),
            tool_surface_digest: surface.digest.clone(),
            binaries,
        })
    }

    pub fn embedded(surface: &ToolSurface) -> Result<Self, RuntimeError> {
        let binary_dir = runtime_binary_directory(&std::env::current_exe()?)?;
        Self::assemble(
            env!("HVP_SOURCE_FINGERPRINT"),
            surface,
            None,
            None,
            RuntimeProvenance::SelfBuild,
            runtime_binary_digests(&binary_dir)?,
        )
    }

    pub fn release_directory_name(&self) -> &str {
        self.runtime_id
            .strip_prefix("sha256:")
            .unwrap_or(&self.runtime_id)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PromotionSource {
    pub remote_ref: String,
    pub branch: String,
    pub commit: String,
    pub tree: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LauncherRecord {
    pub install_path: String,
    pub sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PromotionReceipt {
    pub schema: String,
    pub runtime_id: String,
    pub identity: RuntimeIdentity,
    pub source: PromotionSource,
    pub release_root: String,
    pub binaries: BTreeMap<String, String>,
    pub launcher: LauncherRecord,
    pub digests_sha256: String,
    pub toolchain: String,
    pub promoted_at: u64,
    pub receipt_digest: String,
}

impl PromotionReceipt {
    fn digest(&self) -> Result<String, RuntimeError> {
        let mut body = self.clone();
        body.receipt_digest = String::new();
        Ok(format!(
            "sha256:{:x}",
            Sha256::digest(serde_json::to_vec(&body)?)
        ))
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ActiveSelection {
    pub schema: String,
    pub runtime_id: String,
    pub release_root: String,
    pub receipt_digest: String,
    pub digests_sha256: String,
    pub selected_at: u64,
    pub selected_reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CapabilityFloor {
    pub schema: String,
    pub capabilities: Vec<String>,
    pub runtime_id: String,
    pub updated_at: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UploadHold {
    pub schema: String,
    pub held: bool,
    pub reason: String,
    pub changed_at: u64,
    pub changed_by: String,
}

impl UploadHold {
    /// The shape returned whenever the recorded state is absent, unreadable or
    /// not exactly a well-formed release. Production upload is held by default
    /// and stays held through Wave 3; nothing infers a lift.
    pub fn default_held(reason: &str) -> Self {
        Self {
            schema: UPLOAD_HOLD_SCHEMA.to_owned(),
            held: true,
            reason: reason.to_owned(),
            changed_at: 0,
            changed_by: String::new(),
        }
    }
}

/// Read the operational production-upload hold. Fail closed: only a
/// well-formed record that names who lifted it and why can turn upload on.
pub fn upload_hold(state_root: &Path) -> UploadHold {
    let path = state_root.join(UPLOAD_HOLD_FILE);
    let Ok(bytes) = fs::read(&path) else {
        return UploadHold::default_held("upload_hold_default");
    };
    let Ok(hold) = serde_json::from_slice::<UploadHold>(&bytes) else {
        return UploadHold::default_held("upload_hold_unreadable");
    };
    if hold.schema != UPLOAD_HOLD_SCHEMA {
        return UploadHold::default_held("upload_hold_unreadable");
    }
    if hold.held {
        return hold;
    }
    if hold.reason.trim().is_empty() || hold.changed_by.trim().is_empty() || hold.changed_at == 0 {
        return UploadHold::default_held("upload_hold_incomplete_lift");
    }
    hold
}

pub fn set_upload_hold(
    state_root: &Path,
    held: bool,
    reason: &str,
    changed_by: &str,
) -> Result<UploadHold, RuntimeError> {
    if reason.trim().is_empty() || changed_by.trim().is_empty() {
        return Err(RuntimeError::Refused(
            "an upload-hold change needs both a reason and who made it".to_owned(),
        ));
    }
    let hold = UploadHold {
        schema: UPLOAD_HOLD_SCHEMA.to_owned(),
        held,
        reason: reason.to_owned(),
        changed_at: now()?,
        changed_by: changed_by.to_owned(),
    };
    write_json_atomically(state_root, UPLOAD_HOLD_FILE, &hold)
        .map_err(|error| RuntimeError::Refused(error.to_string()))?;
    Ok(hold)
}

/// Where promoted runtimes live. Repo-external by construction; the override
/// exists so tests never touch the operator's real runtime state.
pub fn state_root() -> PathBuf {
    if let Some(root) = std::env::var_os("HVP_RUNTIME_STATE") {
        return PathBuf::from(root);
    }
    expand_home(&source_fingerprint::manifest().state_root)
}

pub fn launcher_install_path() -> PathBuf {
    if let Some(path) = std::env::var_os("HVP_LAUNCHER_PATH") {
        return PathBuf::from(path);
    }
    expand_home(&source_fingerprint::manifest().launcher.install_path)
}

fn expand_home(path: &str) -> PathBuf {
    match path.strip_prefix("~/") {
        Some(rest) => match std::env::var_os("HOME") {
            Some(home) => PathBuf::from(home).join(rest),
            None => PathBuf::from(rest),
        },
        None => PathBuf::from(path),
    }
}

/// How this process was started. A promoted release mirrors its own source, so
/// the repository the runtime executes against is the mirror, never a checkout
/// somebody can edit while it serves.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Layout {
    Release {
        release_root: PathBuf,
        repo_root: PathBuf,
    },
    Development {
        repo_root: PathBuf,
    },
}

impl Layout {
    pub fn detect(binary: &Path) -> Option<Self> {
        let binary_dir = binary.parent()?;
        let release_root = binary_dir.parent()?;
        let mirrored = release_root.join("source");
        if mirrored.join("pipeline/runtime-manifest.json").is_file() {
            return Some(Self::Release {
                release_root: release_root.to_path_buf(),
                repo_root: mirrored,
            });
        }
        // pipeline/target/<profile>/<binary>
        let repo_root = binary_dir.parent()?.parent()?.parent()?;
        if repo_root.join("pipeline/runtime-manifest.json").is_file() {
            return Some(Self::Development {
                repo_root: repo_root.to_path_buf(),
            });
        }
        None
    }
}

/// The runtime identity a stored mutation receipt is bound to. Small and owned
/// on purpose: it is cloned into the blocking task that executes a mutation, so
/// a later replay can be attributed to the runtime that actually ran it rather
/// than to whichever runtime happens to be answering now.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeBinding {
    pub runtime_id: String,
    pub binary_sha256: String,
}

#[derive(Debug, Clone)]
struct RuntimeTrust {
    promoted: bool,
    verified: bool,
    mutations_allowed: bool,
    floor: Value,
}

/// The authority a served process carries: who it is, whether it verified, and
/// what it therefore refuses to do.
#[derive(Debug, Clone)]
pub struct RuntimeAuthority {
    identity: RuntimeIdentity,
    repo_root: PathBuf,
    state_root: PathBuf,
    binary_sha256: String,
    promoted: bool,
    verified: bool,
    supported: source_fingerprint::SupportedProjectContracts,
    stamp: Value,
}

impl RuntimeAuthority {
    /// An unpromoted process reports exactly what it is and is always
    /// read-only. Source matching proves which checkout produced the binary;
    /// it does not grant mutation authority.
    pub fn development(
        repo_root: &Path,
        state_root: PathBuf,
        surface: ToolSurface,
    ) -> Result<Self, RuntimeError> {
        let identity = RuntimeIdentity::embedded(&surface)?;
        let binary_sha256 = std::env::current_exe()
            .ok()
            .and_then(|path| file_digest(&path).ok())
            .unwrap_or_default();
        Self::new(
            identity,
            repo_root.to_path_buf(),
            state_root,
            binary_sha256,
            RuntimeTrust {
                promoted: false,
                verified: false,
                mutations_allowed: false,
                floor: Value::Null,
            },
        )
    }

    /// Hermetic integration tests need to exercise mutation routing without
    /// publishing bytes into the operator's runtime store. This constructor is
    /// absent from release builds, so no shipped server can use it as a bypass.
    #[cfg(debug_assertions)]
    #[doc(hidden)]
    pub fn test_promoted(
        repo_root: &Path,
        state_root: PathBuf,
        surface: ToolSurface,
    ) -> Result<Self, RuntimeError> {
        let identity =
            read_json::<ActiveSelection>(&state_root.join(ACTIVE_LINK).join(ACTIVE_FILE))
                .ok()
                .and_then(|active| {
                    read_json::<PromotionReceipt>(
                        &Path::new(&active.release_root).join("receipt.json"),
                    )
                    .ok()
                    .map(|receipt| receipt.identity)
                })
                .unwrap_or(RuntimeIdentity::embedded(&surface)?);
        let binary_sha256 = std::env::current_exe()
            .ok()
            .and_then(|path| file_digest(&path).ok())
            .unwrap_or_default();
        // A promoted runtime always has a recorded floor, because promotion
        // records one. Bootstrap the hermetic state root the same way rather
        // than exempting this constructor from the fail-closed floor read every
        // served process performs. A floor that exists but cannot be read is
        // left exactly as it is, so it reaches the same refusal it would in a
        // served process instead of being overwritten into a passing one.
        let active_file = state_root.join(ACTIVE_LINK).join(ACTIVE_FILE);
        if !active_file.exists() && matches!(read_floor(&state_root), Ok(FloorState::Absent)) {
            let active = state_root.join(ACTIVE_LINK);
            fs::create_dir_all(&active)?;
            write_floor(
                &active,
                &identity.security_capabilities,
                &identity.runtime_id,
            )?;
        }
        if !active_file.exists() {
            write_json_atomically(
                &state_root.join(ACTIVE_LINK),
                ACTIVE_FILE,
                &ActiveSelection {
                    schema: ACTIVE_SCHEMA.to_owned(),
                    runtime_id: identity.runtime_id.clone(),
                    release_root: repo_root.to_string_lossy().into_owned(),
                    receipt_digest: "test".to_owned(),
                    digests_sha256: "test".to_owned(),
                    selected_at: now()?,
                    selected_reason: "test".to_owned(),
                },
            )
            .map_err(|error| RuntimeError::Refused(error.to_string()))?;
        }
        let (mutations_allowed, floor) =
            evaluate_floor(&state_root, &identity.security_capabilities);
        Self::new(
            identity,
            repo_root.to_path_buf(),
            state_root,
            binary_sha256,
            RuntimeTrust {
                promoted: true,
                verified: true,
                mutations_allowed,
                floor,
            },
        )
    }

    /// A served process: verify the promoted bytes before anything is answered.
    pub fn resolve(binary: &Path, surface: ToolSurface) -> Result<Self, RuntimeError> {
        let layout = Layout::detect(binary).ok_or_else(|| {
            RuntimeError::Verification(
                "binary is not inside a promoted release or a checkout".to_owned(),
            )
        })?;
        let state_root = state_root();
        match layout {
            Layout::Release {
                release_root,
                repo_root,
            } => {
                let verified = verify_release(&release_root, &state_root, &surface, Some(binary))?;
                let (mutations_allowed, floor) = evaluate_floor(
                    &state_root,
                    &verified.receipt.identity.security_capabilities,
                );
                Self::new(
                    verified.receipt.identity.clone(),
                    repo_root,
                    state_root,
                    verified.binary_sha256,
                    RuntimeTrust {
                        promoted: true,
                        verified: true,
                        mutations_allowed,
                        floor,
                    },
                )
            }
            Layout::Development { repo_root } => {
                let fingerprint = source_fingerprint::calculate(&repo_root)?;
                if fingerprint != env!("HVP_SOURCE_FINGERPRINT") {
                    return Err(RuntimeError::Verification(
                        "binary does not match the runtime source closure".to_owned(),
                    ));
                }
                let identity = RuntimeIdentity::compute(&repo_root, &surface, None, None)?;
                let binary_sha256 = file_digest(binary)?;
                Self::new(
                    identity,
                    repo_root,
                    state_root,
                    binary_sha256,
                    RuntimeTrust {
                        promoted: false,
                        verified: true,
                        mutations_allowed: false,
                        floor: Value::Null,
                    },
                )
            }
        }
    }

    fn new(
        identity: RuntimeIdentity,
        repo_root: PathBuf,
        state_root: PathBuf,
        binary_sha256: String,
        trust: RuntimeTrust,
    ) -> Result<Self, RuntimeError> {
        let RuntimeTrust {
            promoted,
            verified,
            mutations_allowed,
            floor,
        } = trust;
        let stamp = serde_json::json!({
            "schema": IDENTITY_SCHEMA,
            "runtime_id": identity.runtime_id,
            "promoted": promoted,
            "verified": verified,
            "mutations_allowed": mutations_allowed,
            "commit": identity.commit,
            "tree": identity.tree,
            "source_fingerprint": identity.source_fingerprint,
            "manifest_digest": identity.manifest_digest,
            "binary_sha256": binary_sha256,
            "runtime_contract": identity.runtime_contract,
            "capability_floor": floor,
            "evaluator_contract": identity.evaluator_contract,
            "artifact_contract": identity.artifact_contract,
            "security_capabilities": identity.security_capabilities,
            "tool_surface_digest": identity.tool_surface_digest,
            "tool_surface": identity.tool_surface,
        });
        Ok(Self {
            supported: source_fingerprint::manifest().supported_project_contracts,
            identity,
            repo_root,
            state_root,
            binary_sha256,
            promoted,
            verified,
            stamp,
        })
    }

    pub fn repo_root(&self) -> &Path {
        &self.repo_root
    }

    pub fn state_root(&self) -> &Path {
        &self.state_root
    }

    pub fn identity(&self) -> &RuntimeIdentity {
        &self.identity
    }

    pub fn binary_sha256(&self) -> &str {
        &self.binary_sha256
    }

    pub fn promoted(&self) -> bool {
        self.promoted
    }

    pub fn verified(&self) -> bool {
        self.verified
    }

    pub fn mutations_allowed(&self) -> bool {
        self.live_mutation_trust().0
    }

    /// The identity a mutation receipt written by this process is bound to.
    pub fn binding(&self) -> RuntimeBinding {
        RuntimeBinding {
            runtime_id: self.identity.runtime_id.clone(),
            binary_sha256: self.binary_sha256.clone(),
        }
    }

    /// Every authoritative result carries the identity that produced it, plus
    /// live authority state so a process displaced after startup cannot claim
    /// it still has mutation authority.
    pub fn stamp(&self, mut result: AppResult) -> AppResult {
        let (allowed, _) = self.live_mutation_trust();
        let mut stamp = self.stamp.clone();
        stamp["mutations_allowed"] = Value::Bool(allowed);
        result.runtime = Some(stamp);
        result
    }

    /// The project-side compatibility authority. A project that does not name a
    /// supported evaluator/artifact/runtime contract is not evaluated at all --
    /// no gate runs, no mutation is attempted, and nothing is inferred from the
    /// project's shape.
    pub fn project_block(&self, project_root: &Path) -> Option<AppResult> {
        let declared = read_project_runtime_contract(project_root);
        let (reason, declared_value) = match &declared {
            ProjectRuntimeContract::Unresolved => return None,
            ProjectRuntimeContract::Missing => ("missing_runtime_contract", Value::Null),
            ProjectRuntimeContract::Malformed => ("malformed_runtime_contract", Value::Null),
            ProjectRuntimeContract::Declared {
                runtime,
                evaluator,
                artifact,
            } => {
                if self.supported.runtime.iter().any(|value| value == runtime)
                    && self
                        .supported
                        .evaluator
                        .iter()
                        .any(|value| value == evaluator)
                    && self
                        .supported
                        .artifact
                        .iter()
                        .any(|value| value == artifact)
                {
                    return None;
                }
                (
                    "unsupported_runtime_contract",
                    serde_json::json!({
                        "runtime": runtime,
                        "evaluator": evaluator,
                        "artifact": artifact,
                    }),
                )
            }
        };
        Some(AppResult::blocked_with_data(
            "runtime_incompatible",
            project_root,
            Some(serde_json::json!({
                "schema": "haru.runtime_incompatible.v1",
                "reason": reason,
                "declared": declared_value,
                "required": {
                    "schema": PROJECT_CONTRACT_SCHEMA,
                    "runtime": self.supported.runtime,
                    "evaluator": self.supported.evaluator,
                    "artifact": self.supported.artifact,
                },
                "remedy": "a reviewed migration must write project-contract.json.runtime_contract; it is never inferred",
            })),
        ))
    }

    /// Mutation authority is revalidated on every call. Startup verification is
    /// necessary but not sufficient: an old process must lose authority as soon
    /// as another generation becomes active or the floor changes.
    pub fn mutation_block(&self, project_root: &Path) -> Option<AppResult> {
        if !self.promoted {
            return Some(AppResult::blocked_with_data(
                "runtime_unpromoted",
                project_root,
                Some(serde_json::json!({
                    "schema": "haru.runtime_unpromoted.v1",
                    "runtime_id": self.identity.runtime_id,
                    "remedy": "restart the client through the promoted ~/.local/bin/video-studio-mcp launcher",
                })),
            ));
        }
        let (allowed, evidence) = self.live_mutation_trust();
        if allowed {
            return None;
        }
        let code = if evidence.get("state").and_then(Value::as_str) == Some("not_active") {
            "runtime_not_active"
        } else {
            "runtime_capability_floor"
        };
        Some(AppResult::blocked_with_data(
            code,
            project_root,
            Some(serde_json::json!({
                "schema": "haru.runtime_capability_floor.v1",
                "runtime_capabilities": self.identity.security_capabilities,
                "floor": evidence,
            })),
        ))
    }
    /// Hold a shared authority lock across the mutation itself. Promotion and
    /// selection take the exclusive side of the same lock, so an old process
    /// cannot pass an authority check and continue writing after a generation
    /// switch commits.
    pub fn mutation_guard(&self, project_root: &Path) -> Result<File, Box<AppResult>> {
        let lock = authority_file(&self.state_root).map_err(|error| {
            Box::new(AppResult::blocked_with_data(
                "runtime_authority_unavailable",
                project_root,
                Some(serde_json::json!({"detail": error.to_string()})),
            ))
        })?;
        lock.lock_shared().map_err(|error| {
            Box::new(AppResult::blocked_with_data(
                "runtime_authority_unavailable",
                project_root,
                Some(serde_json::json!({"detail": error.to_string()})),
            ))
        })?;
        if let Some(blocked) = self.mutation_block(project_root) {
            return Err(Box::new(blocked));
        }
        Ok(lock)
    }

    fn live_mutation_trust(&self) -> (bool, Value) {
        if !self.promoted || !self.verified {
            return (false, Value::Null);
        }
        let active =
            read_json::<ActiveSelection>(&self.state_root.join(ACTIVE_LINK).join(ACTIVE_FILE));
        match active {
            Ok(active)
                if active.schema == ACTIVE_SCHEMA
                    && active.runtime_id == self.identity.runtime_id => {}
            Ok(active) => {
                return (
                    false,
                    serde_json::json!({
                        "state": "not_active",
                        "active_runtime_id": active.runtime_id,
                        "process_runtime_id": self.identity.runtime_id,
                    }),
                );
            }
            Err(error) => {
                return (
                    false,
                    serde_json::json!({
                        "state": "not_active",
                        "detail": error.to_string(),
                        "process_runtime_id": self.identity.runtime_id,
                    }),
                );
            }
        }
        evaluate_floor(&self.state_root, &self.identity.security_capabilities)
    }

    /// The operational production-upload hold. This runs before a credential
    /// path is opened, before the lease secret is read, before the verifier
    /// runs and before any HTTP-capable runner is executed.
    pub fn upload_hold_block(&self, project_root: &Path) -> Option<AppResult> {
        let manifest = source_fingerprint::manifest();
        if manifest.youtube_channel_id.trim().is_empty()
            || manifest.publish_approval_signer.is_none()
        {
            return Some(AppResult::blocked_with_data(
                "publishing_unconfigured",
                project_root,
                Some(serde_json::json!({
                    "schema": "video_studio.publishing_unconfigured.v1",
                    "youtube_channel_configured": !manifest.youtube_channel_id.trim().is_empty(),
                    "approval_signer_configured": manifest.publish_approval_signer.is_some(),
                    "remedy": "Configure and rebuild a trusted publishing runtime. A local self-build never acquires publishing authority from installation alone.",
                })),
            ));
        }
        let hold = upload_hold(&self.state_root);
        if !hold.held {
            return None;
        }
        Some(AppResult::blocked_with_data(
            "production_upload_held",
            project_root,
            Some(serde_json::json!({
                "schema": UPLOAD_HOLD_SCHEMA,
                "held": true,
                "reason": hold.reason,
                "remedy": "Inspect `hvp-runtime hold status` and follow docs/publish-approval.md: provision the canonical OAuth credential, obtain current external review and publish attestations, and verify the Wave 3 upload fence before an operator records a hold lift. Then retry through MCP; a hold lift alone does not satisfy project approval.",
            })),
        ))
    }
}

#[derive(Debug)]
pub struct VerifiedRelease {
    pub receipt: PromotionReceipt,
    pub active: ActiveSelection,
    pub binary_sha256: String,
}

/// A release that has proven everything about itself except that it is the one
/// currently selected.
#[derive(Debug)]
pub struct VerifiedCandidate {
    /// The canonicalized release root the proof was carried out against.
    pub release_root: PathBuf,
    pub receipt: PromotionReceipt,
    pub binary_sha256: String,
}

/// Everything a release proves about itself: the receipt covers its own bytes,
/// the mirrored closure and the binaries still hash to the digest list, and the
/// contract versions, capabilities and tool surface are the ones this binary
/// enforces. This is the whole check minus the active selection, so a rollback
/// candidate can be proven whole before anything is made to point at it.
pub fn verify_release_candidate(
    release_root: &Path,
    surface: &ToolSurface,
    binary: Option<&Path>,
) -> Result<VerifiedCandidate, RuntimeError> {
    let release_root = release_root.canonicalize().map_err(|error| {
        RuntimeError::Verification(format!("release root is unreadable: {error}"))
    })?;
    let receipt: PromotionReceipt = read_json(&release_root.join("receipt.json"))?;
    if receipt.schema != RECEIPT_SCHEMA {
        return Err(RuntimeError::Verification(
            "promotion receipt schema is unsupported".to_owned(),
        ));
    }
    let recomputed = receipt.digest()?;
    if recomputed != receipt.receipt_digest {
        return Err(RuntimeError::Verification(
            "promotion receipt digest does not cover its own contents".to_owned(),
        ));
    }
    let provenance_matches_source = match receipt.identity.provenance {
        RuntimeProvenance::SelfBuild => receipt.source.remote_ref == "local-committed-source",
        RuntimeProvenance::TrustedUpstream => {
            !receipt.source.remote_ref.trim().is_empty()
                && receipt.source.remote_ref != "local-committed-source"
                && !receipt.source.branch.trim().is_empty()
        }
    };
    if !provenance_matches_source {
        return Err(RuntimeError::Verification(
            "runtime provenance does not match its recorded source".to_owned(),
        ));
    }

    let digests_path = release_root.join(DIGESTS_FILE);
    let digests = read_regular_file(&digests_path)?;
    if format!("sha256:{:x}", Sha256::digest(&digests)) != receipt.digests_sha256 {
        return Err(RuntimeError::Verification(
            "release digest list does not match the receipt".to_owned(),
        ));
    }
    verify_digest_listing(&release_root, &digests)?;

    let repo_root = release_root.join("source");
    let fingerprint = format!("sha256:{}", source_fingerprint::calculate(&repo_root)?);
    if fingerprint != receipt.identity.source_fingerprint {
        return Err(RuntimeError::Verification(
            "mirrored runtime source closure does not match the receipt".to_owned(),
        ));
    }
    let mirrored_manifest = fs::read(repo_root.join("pipeline/runtime-manifest.json"))?;
    let embedded_manifest_digest = format!("sha256:{}", source_fingerprint::manifest_digest());
    if format!("sha256:{:x}", Sha256::digest(&mirrored_manifest)) != embedded_manifest_digest
        || receipt.identity.manifest_digest != embedded_manifest_digest
    {
        return Err(RuntimeError::Verification(
            "runtime manifest differs between the binary, the mirror and the receipt".to_owned(),
        ));
    }

    let manifest = source_fingerprint::manifest();
    if receipt.identity.runtime_contract != manifest.runtime_contract
        || receipt.identity.evaluator_contract != manifest.evaluator_contract
        || receipt.identity.artifact_contract != manifest.artifact_contract
        || receipt.identity.security_capabilities != manifest.security_capabilities
    {
        return Err(RuntimeError::Verification(
            "promoted contract versions or capabilities differ from this binary".to_owned(),
        ));
    }
    if receipt.identity.tool_surface != surface.names
        || receipt.identity.tool_surface_digest != surface.digest
    {
        return Err(RuntimeError::Verification(
            "promoted MCP tool surface differs from this binary".to_owned(),
        ));
    }

    let commit = receipt.source.commit.clone();
    let tree = receipt.source.tree.clone();
    if receipt.binaries != receipt.identity.binaries {
        return Err(RuntimeError::Verification(
            "promotion receipt binary attestations differ from runtime identity".to_owned(),
        ));
    }
    let recomputed_identity = RuntimeIdentity::assemble(
        fingerprint.trim_start_matches("sha256:"),
        surface,
        Some(&commit),
        Some(&tree),
        receipt.identity.provenance,
        runtime_binary_digests(&release_root.join("bin"))?,
    )?;
    if recomputed_identity != receipt.identity
        || recomputed_identity.runtime_id != receipt.runtime_id
        || release_root.file_name().and_then(|name| name.to_str())
            != Some(recomputed_identity.release_directory_name())
    {
        return Err(RuntimeError::Verification(
            "promoted runtime identity does not recompute to its own digest".to_owned(),
        ));
    }

    let mut binary_sha256 = String::new();
    if let Some(binary) = binary {
        let name = binary
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or_default()
            .to_owned();
        binary_sha256 = file_digest(binary)?;
        if receipt.binaries.get(&name) != Some(&binary_sha256) {
            return Err(RuntimeError::Verification(format!(
                "running binary {name} is not the promoted binary"
            )));
        }
    }

    Ok(VerifiedCandidate {
        release_root,
        receipt,
        binary_sha256,
    })
}

/// Verify a promoted release against its own receipt, the active selection and
/// this binary's compiled-in contract. Called by the launcher before exec and
/// again by the served process before it answers anything.
pub fn verify_release(
    release_root: &Path,
    state_root: &Path,
    surface: &ToolSurface,
    binary: Option<&Path>,
) -> Result<VerifiedRelease, RuntimeError> {
    let candidate = verify_release_candidate(release_root, surface, binary)?;
    let active: ActiveSelection = read_json(&state_root.join(ACTIVE_LINK).join(ACTIVE_FILE))
        .map_err(|_| RuntimeError::NoActiveRuntime)?;
    if active.schema != ACTIVE_SCHEMA
        || active.runtime_id != candidate.receipt.runtime_id
        || active.receipt_digest != candidate.receipt.receipt_digest
        || active.digests_sha256 != candidate.receipt.digests_sha256
        || Path::new(&active.release_root) != candidate.release_root
    {
        return Err(RuntimeError::Verification(
            "this release is not the active selection".to_owned(),
        ));
    }
    Ok(VerifiedRelease {
        receipt: candidate.receipt,
        active,
        binary_sha256: candidate.binary_sha256,
    })
}

/// What the recorded security-capability floor says. Reading it is fallible on
/// purpose: an unreadable, malformed or wrong-schema floor is a fail-closed
/// condition, never an absent one, and absence itself is never a pass.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FloorState {
    /// Nothing has ever been recorded. Only the first promotion -- which runs
    /// before any runtime holds mutation authority -- may proceed from here.
    Absent,
    Recorded(CapabilityFloor),
}

pub fn read_floor(state_root: &Path) -> Result<FloorState, RuntimeError> {
    let bytes = match fs::read(state_root.join(ACTIVE_LINK).join(FLOOR_FILE)) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(FloorState::Absent),
        Err(error) => {
            return Err(RuntimeError::Verification(format!(
                "security capability floor is unreadable: {error}"
            )));
        }
    };
    let floor: CapabilityFloor = serde_json::from_slice(&bytes).map_err(|error| {
        RuntimeError::Verification(format!("security capability floor is malformed: {error}"))
    })?;
    if floor.schema != FLOOR_SCHEMA {
        return Err(RuntimeError::Verification(format!(
            "security capability floor declares schema {:?}, not {FLOOR_SCHEMA}",
            floor.schema
        )));
    }
    Ok(FloorState::Recorded(floor))
}

fn write_floor(
    selection_root: &Path,
    capabilities: &[String],
    runtime_id: &str,
) -> Result<(), RuntimeError> {
    let floor = CapabilityFloor {
        schema: FLOOR_SCHEMA.to_owned(),
        capabilities: capabilities.to_vec(),
        runtime_id: runtime_id.to_owned(),
        updated_at: now()?,
    };
    write_json_atomically(selection_root, FLOOR_FILE, &floor)
        .map_err(|error| RuntimeError::Refused(error.to_string()))
}

fn missing_capabilities(required: &[String], held: &[String]) -> Vec<String> {
    required
        .iter()
        .filter(|capability| !held.iter().any(|have| have == *capability))
        .cloned()
        .collect()
}

/// Mutation authority against the recorded floor, plus the evidence a refusal
/// reports. Every path that is not a well-formed floor this runtime satisfies
/// resolves to no mutation authority.
fn evaluate_floor(state_root: &Path, capabilities: &[String]) -> (bool, Value) {
    match read_floor(state_root) {
        Ok(FloorState::Recorded(floor)) => {
            let missing = missing_capabilities(&floor.capabilities, capabilities);
            (
                missing.is_empty(),
                serde_json::json!({
                    "state": "recorded",
                    "capabilities": floor.capabilities,
                    "missing": missing,
                    "recorded_for": floor.runtime_id,
                }),
            )
        }
        Ok(FloorState::Absent) => (
            false,
            serde_json::json!({
                "state": "absent",
                "detail": "no security capability floor is recorded; only the first promotion runs without one",
            }),
        ),
        Err(error) => (
            false,
            serde_json::json!({ "state": "unusable", "detail": error.to_string() }),
        ),
    }
}

/// The project-side compatibility block a freshly created project starts with.
pub fn project_runtime_contract_value() -> Value {
    let manifest = source_fingerprint::manifest();
    serde_json::json!({
        "schema": PROJECT_CONTRACT_SCHEMA,
        "runtime": manifest.runtime_contract,
        "evaluator": manifest.evaluator_contract,
        "artifact": manifest.artifact_contract,
    })
}

/// What an operator chooses about an installation. This is deliberately a CLI
/// concern and is never exposed to MCP clients.
#[derive(Debug, Clone)]
pub struct PromotionRequest {
    pub repo_root: PathBuf,
    pub state_root: PathBuf,
    pub launcher_path: PathBuf,
    pub allow_capability_drop: bool,
    pub source: PromotionMode,
}

#[derive(Debug, Clone)]
pub enum PromotionMode {
    /// Build the exact clean, committed `HEAD` in the operator's checkout.
    /// This proves byte identity, not third-party review or publisher trust.
    SelfBuild,
    /// Fetch and require the checkout to equal an explicitly configured
    /// upstream branch before building its immutable commit.
    TrustedUpstream { url: String, branch: String },
}

/// Build one clean committed snapshot in isolation, mirror it immutably and
/// make it active. Trusted-upstream installs additionally fetch the configured
/// branch and require `HEAD` to equal that fetched commit.
pub fn promote(
    request: &PromotionRequest,
    surface: &ToolSurface,
) -> Result<PromotionReceipt, RuntimeError> {
    let _authority_lock = authority_lock(&request.state_root)?;
    let repo = request.repo_root.canonicalize().map_err(|error| {
        RuntimeError::Refused(format!("repository root is unreadable: {error}"))
    })?;
    if git(&repo, &["rev-parse", "--is-inside-work-tree"])? != "true" {
        return Err(RuntimeError::Refused("not a git work tree".to_owned()));
    }
    if !git(&repo, &["status", "--porcelain"])?.is_empty() {
        return Err(RuntimeError::Refused(
            "the source checkout is dirty; promote only reviewed, committed bytes".to_owned(),
        ));
    }
    let floor = match read_floor(&request.state_root)? {
        FloorState::Recorded(floor) => Some(floor),
        FloorState::Absent => {
            if request.state_root.join(RELEASES_DIR).exists() {
                return Err(RuntimeError::Refused(
                    "promoted releases exist but no security capability floor is recorded; \
                         the first-promotion bootstrap has already happened"
                        .to_owned(),
                ));
            }
            None
        }
    };

    let fetch_ref = format!("refs/video-studio/promotions/{}", uuid::Uuid::new_v4());
    let (commit, tree, provenance, origin, fetched) = match &request.source {
        PromotionMode::SelfBuild => {
            let commit = object_name(&git(&repo, &["rev-parse", "HEAD^{commit}"])?)?;
            let tree = object_name(&git(&repo, &["rev-parse", "HEAD^{tree}"])?)?;
            let branch = git(&repo, &["symbolic-ref", "--quiet", "--short", "HEAD"])
                .unwrap_or_else(|_| "detached".to_owned());
            (
                commit.clone(),
                tree.clone(),
                RuntimeProvenance::SelfBuild,
                PromotionSource {
                    remote_ref: "local-committed-source".to_owned(),
                    branch,
                    commit,
                    tree,
                },
                false,
            )
        }
        PromotionMode::TrustedUpstream { url, branch } => {
            if url.trim().is_empty() || url.starts_with('-') || branch.trim().is_empty() {
                return Err(RuntimeError::Refused(
                    "trusted-upstream promotion requires a non-option URL and non-empty branch"
                        .to_owned(),
                ));
            }
            reject_git_url_rewrites(&repo)?;
            git(&repo, &["check-ref-format", "--branch", branch])?;
            let head_ref = git(&repo, &["symbolic-ref", "--quiet", "HEAD"]).map_err(|_| {
                RuntimeError::Refused(
                    "HEAD is detached; trusted-upstream promotion requires its configured branch"
                        .to_owned(),
                )
            })?;
            if head_ref != format!("refs/heads/{branch}") {
                return Err(RuntimeError::Refused(format!(
                    "HEAD is on {head_ref}, not refs/heads/{branch}"
                )));
            }
            trusted_git_fetch(&repo, url, branch, &fetch_ref)?;
            let commit = object_name(&git(
                &repo,
                &["rev-parse", &format!("{fetch_ref}^{{commit}}")],
            )?)?;
            let head = object_name(&git(&repo, &["rev-parse", "HEAD^{commit}"])?)?;
            if head != commit {
                return Err(RuntimeError::Refused(format!(
                    "HEAD {head} is not fetched trusted upstream {url} {branch} {commit}; merge before promoting"
                )));
            }
            let tree = object_name(&git(&repo, &["rev-parse", &format!("{commit}^{{tree}}")])?)?;
            (
                commit.clone(),
                tree.clone(),
                RuntimeProvenance::TrustedUpstream,
                PromotionSource {
                    remote_ref: format!("{url}#{branch}"),
                    branch: branch.clone(),
                    commit,
                    tree,
                },
                true,
            )
        }
    };
    let result = {
        let staging = request
            .state_root
            .join("staging")
            .join(format!("{commit}-{}", uuid::Uuid::new_v4()));
        let operation = (|| {
            fs::create_dir_all(staging.join("source"))?;
            let archive = staging.join("source.tar");
            git(
                &repo,
                &[
                    "archive",
                    "--format=tar",
                    "-o",
                    path_argument(&archive)?,
                    &commit,
                ],
            )?;
            run(
                "/usr/bin/tar",
                &[
                    "-xf",
                    path_argument(&archive)?,
                    "-C",
                    path_argument(&staging.join("source"))?,
                ],
                None,
                &[],
            )?;
            fs::remove_file(&archive)?;

            let staged_source = staging.join("source").canonicalize()?;
            let cargo = trusted_cargo_path()?;
            run_frozen_build(
                &cargo,
                &staged_source.join("pipeline/Cargo.toml"),
                &staging.join("target"),
            )?;
            let toolchain = format!(
                "{}; cargo_sha256={}",
                run(path_argument(&cargo)?, &["--version"], None, &[])?,
                file_digest(&cargo)?
            );

            let built = staging.join("target/release");
            let staged_runtime = built.join("hvp-runtime");
            let reported = run(
                path_argument(&staged_runtime)?,
                &[
                    "identity",
                    "--repo",
                    path_argument(&staged_source)?,
                    "--commit",
                    &commit,
                    "--tree",
                    &tree,
                    "--provenance",
                    provenance.cli_value(),
                ],
                None,
                &[],
            )?;
            let identity: RuntimeIdentity = serde_json::from_str(&reported)?;
            let recomputed_fingerprint =
                format!("sha256:{}", source_fingerprint::calculate(&staged_source)?);
            let staged_manifest =
                read_regular_file(&staged_source.join("pipeline/runtime-manifest.json"))?;
            let staged_manifest_digest = format!("sha256:{:x}", Sha256::digest(&staged_manifest));
            if identity.source_fingerprint != recomputed_fingerprint
                || identity.manifest_digest != staged_manifest_digest
                || identity.commit.as_deref() != Some(commit.as_str())
                || identity.tree.as_deref() != Some(tree.as_str())
                || identity.tool_surface != surface.names
                || identity.binaries != runtime_binary_digests(&built)?
            {
                return Err(RuntimeError::Refused(
                    "the built runtime does not agree with the promoted source closure and binaries"
                        .to_owned(),
                ));
            }
            if let Some(floor) = &floor {
                let missing =
                    missing_capabilities(&floor.capabilities, &identity.security_capabilities);
                if !missing.is_empty() && !request.allow_capability_drop {
                    return Err(RuntimeError::Refused(format!(
                        "promotion drops security capabilities below the floor: {missing:?}"
                    )));
                }
            }

            let releases = request.state_root.join(RELEASES_DIR);
            let release_root = releases.join(canonical_release_name(&identity.runtime_id)?);
            if !release_root.exists() {
                stage_release(&ReleasePlan {
                    staging: &staging.join("release"),
                    source: &staged_source,
                    binaries: &built,
                    identity: &identity,
                    origin: origin.clone(),
                    release_root: &release_root,
                    launcher_path: &request.launcher_path,
                    toolchain,
                })?;
            }
            let candidate = verify_release_candidate(&release_root, surface, None)?;
            if candidate.receipt.identity != identity {
                return Err(RuntimeError::Verification(
                    "an existing release directory does not match the freshly built runtime"
                        .to_owned(),
                ));
            }
            run(
                path_argument(&release_root.join("bin/hvp-runtime"))?,
                &[
                    "verify",
                    "--candidate",
                    "--release",
                    path_argument(&release_root)?,
                ],
                None,
                &[("HVP_RUNTIME_STATE", request.state_root.clone())],
            )?;
            let launcher = verified_launcher_bytes(&release_root, &candidate.receipt)?;
            ensure_stable_launcher(
                &request.state_root,
                &request.launcher_path,
                &launcher,
                &candidate.receipt,
            )?;
            if provenance == RuntimeProvenance::SelfBuild {
                set_upload_hold(
                    &request.state_root,
                    true,
                    "self-built runtime installed; publishing requires a new explicit approval",
                    "hvp-runtime install-source",
                )?;
            }
            activate(
                &request.state_root,
                &release_root,
                &candidate.receipt,
                provenance.as_str(),
            )?;
            Ok(candidate.receipt)
        })();
        let _ = fs::remove_dir_all(&staging);
        operation
    };
    if fetched {
        let _ = git(&repo, &["update-ref", "-d", &fetch_ref]);
    }
    result
}

/// Everything needed to publish one immutable release directory.
pub struct ReleasePlan<'a> {
    /// Scratch directory the release is assembled in before it is renamed.
    pub staging: &'a Path,
    /// The exact source closure the binaries were built from.
    pub source: &'a Path,
    /// Directory holding the freshly built binaries.
    pub binaries: &'a Path,
    pub identity: &'a RuntimeIdentity,
    pub origin: PromotionSource,
    /// Final, content-addressed home of the release.
    pub release_root: &'a Path,
    pub launcher_path: &'a Path,
    pub toolchain: String,
}

/// Mirror the closure and the binaries into a sealed, content-addressed release
/// and publish it with one rename. Assembly happens under `staging`, so a crash
/// leaves scratch behind rather than a half-written runtime a client can reach.
pub fn stage_release(plan: &ReleasePlan) -> Result<PromotionReceipt, RuntimeError> {
    let staged = plan.staging;
    let _ = fs::remove_dir_all(staged);
    fs::create_dir_all(staged.join("bin"))?;
    for path in source_fingerprint::runtime_paths(plan.source)? {
        let relative = path
            .strip_prefix(plan.source)
            .map_err(|_| RuntimeError::Refused("closure member escaped the source".to_owned()))?;
        let target = staged.join("source").join(relative);
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::copy(&path, &target)?;
    }

    let manifest = source_fingerprint::manifest();
    let mut binaries = BTreeMap::new();
    for name in &manifest.binaries {
        let target = staged.join("bin").join(name);
        fs::copy(plan.binaries.join(name), &target)?;
        fs::set_permissions(&target, fs::Permissions::from_mode(0o555))?;
        binaries.insert(name.clone(), file_digest(&target)?);
    }
    if binaries != plan.identity.binaries {
        let _ = fs::remove_dir_all(staged);
        return Err(RuntimeError::Verification(
            "staged binary digests do not match the runtime identity".to_owned(),
        ));
    }
    write_json_atomically(staged, "runtime-identity.json", plan.identity)
        .map_err(|error| RuntimeError::Refused(error.to_string()))?;

    let digests = digest_listing(staged)?;
    write_atomically(&staged.join(DIGESTS_FILE), digests.as_bytes(), 0o444)?;

    let launcher_bytes = fs::read(staged.join("source").join(&manifest.launcher.source))?;
    let mut receipt = PromotionReceipt {
        schema: RECEIPT_SCHEMA.to_owned(),
        runtime_id: plan.identity.runtime_id.clone(),
        identity: plan.identity.clone(),
        source: plan.origin.clone(),
        release_root: plan.release_root.to_string_lossy().into_owned(),
        binaries,
        launcher: LauncherRecord {
            install_path: plan.launcher_path.to_string_lossy().into_owned(),
            sha256: format!("sha256:{:x}", Sha256::digest(&launcher_bytes)),
        },
        digests_sha256: format!("sha256:{:x}", Sha256::digest(digests.as_bytes())),
        toolchain: plan.toolchain.clone(),
        promoted_at: now()?,
        receipt_digest: String::new(),
    };
    receipt.receipt_digest = receipt.digest()?;
    write_json_atomically(staged, "receipt.json", &receipt)
        .map_err(|error| RuntimeError::Refused(error.to_string()))?;

    // Publish first, then seal: renaming a directory updates its own `..`, so a
    // read-only staged directory can be refused the move that makes it real.
    if let Some(parent) = plan.release_root.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::rename(staged, plan.release_root)?;
    seal(plan.release_root)?;
    fs::set_permissions(plan.release_root, fs::Permissions::from_mode(0o555))?;
    Ok(receipt)
}

/// Roll back to a previously promoted runtime.
///
/// The candidate is proven whole before a single byte of live state moves: the
/// id has to be the canonical `sha256:<64 hex>` grammar, the directory it names
/// has to resolve inside the releases directory, the release has to verify
/// against its own receipt, digest list, mirrored closure, contract versions
/// and tool surface, the receipt has to be the one that release is named after,
/// the recorded security floor has to be readable and satisfied, and the
/// launcher bytes have to match the receipt. Only then are the launcher and the
/// active selection written. A refusal on any of those leaves the previously
/// active runtime and the installed launcher byte-for-byte as they were.
pub fn select(
    state_root: &Path,
    runtime_id: &str,
    surface: &ToolSurface,
    launcher_path: &Path,
) -> Result<PromotionReceipt, RuntimeError> {
    let name = canonical_release_name(runtime_id)?;
    let _authority_lock = authority_lock(state_root)?;
    let releases = state_root
        .join(RELEASES_DIR)
        .canonicalize()
        .map_err(|error| {
            RuntimeError::Refused(format!("no promoted releases directory: {error}"))
        })?;
    let candidate = verify_release_candidate(&releases.join(&name), surface, None)?;
    if candidate.release_root.parent() != Some(releases.as_path()) {
        return Err(RuntimeError::Refused(format!(
            "runtime {runtime_id} resolves outside {}",
            releases.display()
        )));
    }
    let receipt = candidate.receipt;
    if receipt.runtime_id != format!("sha256:{name}") {
        return Err(RuntimeError::Verification(format!(
            "release directory {name} holds a receipt for {}",
            receipt.runtime_id
        )));
    }
    let floor = match read_floor(state_root)? {
        FloorState::Recorded(floor) => floor,
        FloorState::Absent => {
            return Err(RuntimeError::Refused(
                "no security capability floor is recorded; a rollback cannot establish one"
                    .to_owned(),
            ));
        }
    };
    let missing =
        missing_capabilities(&floor.capabilities, &receipt.identity.security_capabilities);
    if !missing.is_empty() {
        return Err(RuntimeError::Refused(format!(
            "runtime {runtime_id} is below the security capability floor: {missing:?}"
        )));
    }

    let launcher = verified_launcher_bytes(&candidate.release_root, &receipt)?;
    ensure_stable_launcher(state_root, launcher_path, &launcher, &receipt)?;
    activate_with_floor(
        state_root,
        &candidate.release_root,
        &receipt,
        &floor,
        "rollback",
    )?;
    Ok(receipt)
}
pub fn activate(
    state_root: &Path,
    release_root: &Path,
    receipt: &PromotionReceipt,
    reason: &str,
) -> Result<(), RuntimeError> {
    let floor = CapabilityFloor {
        schema: FLOOR_SCHEMA.to_owned(),
        capabilities: receipt.identity.security_capabilities.clone(),
        runtime_id: receipt.runtime_id.clone(),
        updated_at: now()?,
    };
    activate_with_floor(state_root, release_root, receipt, &floor, reason)
}

fn activate_with_floor(
    state_root: &Path,
    release_root: &Path,
    receipt: &PromotionReceipt,
    floor: &CapabilityFloor,
    reason: &str,
) -> Result<(), RuntimeError> {
    let resolved = release_root.canonicalize()?;
    let selection = ActiveSelection {
        schema: ACTIVE_SCHEMA.to_owned(),
        runtime_id: receipt.runtime_id.clone(),
        release_root: resolved.to_string_lossy().into_owned(),
        receipt_digest: receipt.receipt_digest.clone(),
        digests_sha256: receipt.digests_sha256.clone(),
        selected_at: now()?,
        selected_reason: reason.to_owned(),
    };
    let pointer = format!(
        "{}  {}\n",
        receipt.digests_sha256.trim_start_matches("sha256:"),
        receipt.runtime_id
    );

    // Build a complete immutable selection generation off to the side. The
    // `active` symlink is the sole commit point, so clients can observe either
    // the entire old generation or the entire new one, never mixed
    // active.json/digest/release state.
    let selections = state_root.join(SELECTIONS_DIR);
    fs::create_dir_all(&selections)?;
    let generation = uuid::Uuid::new_v4().to_string();
    let staging = selections.join(format!(".{generation}"));
    let published = selections.join(&generation);
    fs::create_dir(&staging)?;
    let result = (|| {
        write_json_atomically(&staging, ACTIVE_FILE, &selection)
            .map_err(|error| RuntimeError::Refused(error.to_string()))?;
        write_json_atomically(&staging, FLOOR_FILE, floor)
            .map_err(|error| RuntimeError::Refused(error.to_string()))?;
        write_atomically(&staging.join(ACTIVE_DIGEST_FILE), pointer.as_bytes(), 0o444)?;
        std::os::unix::fs::symlink(&resolved, staging.join("release"))?;
        File::open(&staging)?.sync_all()?;
        fs::rename(&staging, &published)?;
        File::open(&selections)?.sync_all()?;

        let link = state_root.join(ACTIVE_LINK);
        let temporary = state_root.join(format!(".active.{generation}"));
        std::os::unix::fs::symlink(&published, &temporary)?;
        if let Err(error) = fs::rename(&temporary, &link) {
            let _ = fs::remove_file(&temporary);
            return Err(RuntimeError::Io(error));
        }
        File::open(state_root)?.sync_all()?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_dir_all(&staging);
    }
    result
}

/// The launcher bytes a release is entitled to install, checked against the
/// receipt before anything is written anywhere.
fn verified_launcher_bytes(
    release_root: &Path,
    receipt: &PromotionReceipt,
) -> Result<Vec<u8>, RuntimeError> {
    let manifest = source_fingerprint::manifest();
    let source = release_root.join("source").join(&manifest.launcher.source);
    let bytes = read_regular_file(&source)?;
    if format!("sha256:{:x}", Sha256::digest(&bytes)) != receipt.launcher.sha256 {
        return Err(RuntimeError::Verification(
            "launcher bytes differ from the promotion receipt".to_owned(),
        ));
    }
    Ok(bytes)
}

/// The installed launcher is a stable bootstrap. Once an active runtime
/// exists, a promotion may only reuse byte-identical launcher semantics. That
/// leaves the active-generation symlink as the only live commit point.
fn ensure_stable_launcher(
    state_root: &Path,
    launcher_path: &Path,
    bytes: &[u8],
    receipt: &PromotionReceipt,
) -> Result<(), RuntimeError> {
    if receipt.launcher.install_path != launcher_path.to_string_lossy() {
        return Err(RuntimeError::Verification(
            "promotion receipt names a different launcher install path".to_owned(),
        ));
    }
    let candidate_digest = format!("sha256:{:x}", Sha256::digest(bytes));
    let active_path = state_root.join(ACTIVE_LINK).join(ACTIVE_FILE);
    if active_path.exists() {
        let active: ActiveSelection = read_json(&active_path)?;
        let active_receipt: PromotionReceipt =
            read_json(&Path::new(&active.release_root).join("receipt.json"))?;
        if active_receipt.digest()? != active.receipt_digest
            || active_receipt.launcher.sha256 != candidate_digest
        {
            return Err(RuntimeError::Refused(
                "candidate launcher is not compatible with the active runtime".to_owned(),
            ));
        }
    }
    match fs::symlink_metadata(launcher_path) {
        Ok(metadata) => {
            if metadata.file_type().is_symlink()
                || !metadata.is_file()
                || file_digest(launcher_path)? != candidate_digest
            {
                return Err(RuntimeError::Refused(
                    "installed stable launcher differs from the promoted launcher".to_owned(),
                ));
            }
            Ok(())
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            install_launcher(launcher_path, bytes)
        }
        Err(error) => Err(RuntimeError::Io(error)),
    }
}

fn install_launcher(launcher_path: &Path, bytes: &[u8]) -> Result<(), RuntimeError> {
    if let Some(parent) = launcher_path.parent() {
        fs::create_dir_all(parent)?;
    }
    write_atomically(launcher_path, bytes, 0o755)
}

fn digest_listing(release: &Path) -> Result<String, RuntimeError> {
    let mut members = BTreeSet::new();
    collect_regular(&release.join("bin"), release, &mut members)?;
    collect_regular(&release.join("source"), release, &mut members)?;
    members.insert("runtime-identity.json".to_owned());
    let mut listing = String::new();
    for relative in members {
        let digest = file_digest(&release.join(&relative))?;
        listing.push_str(digest.trim_start_matches("sha256:"));
        listing.push_str("  ");
        listing.push_str(&relative);
        listing.push('\n');
    }
    Ok(listing)
}

fn verify_digest_listing(release: &Path, bytes: &[u8]) -> Result<(), RuntimeError> {
    let listing = std::str::from_utf8(bytes)
        .map_err(|_| RuntimeError::Verification("release digest list is not UTF-8".to_owned()))?;
    let mut declared = BTreeMap::new();
    for line in listing.lines() {
        let Some((expected, relative)) = line.split_once("  ") else {
            return Err(RuntimeError::Verification(
                "release digest list is malformed".to_owned(),
            ));
        };
        if expected.len() != 64
            || !expected
                .bytes()
                .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
            || !canonical_relative_path(relative)
            || declared
                .insert(relative.to_owned(), expected.to_owned())
                .is_some()
        {
            return Err(RuntimeError::Verification(
                "release digest list contains a duplicate or non-canonical member".to_owned(),
            ));
        }
    }
    let mut actual_members = BTreeSet::new();
    collect_regular(&release.join("bin"), release, &mut actual_members)?;
    collect_regular(&release.join("source"), release, &mut actual_members)?;
    actual_members.insert("runtime-identity.json".to_owned());
    if declared.keys().cloned().collect::<BTreeSet<_>>() != actual_members {
        return Err(RuntimeError::Verification(
            "release digest list omits or invents release members".to_owned(),
        ));
    }
    for (relative, expected) in declared {
        let actual = file_digest(&release.join(&relative))?;
        if actual.strip_prefix("sha256:") != Some(expected.as_str()) {
            return Err(RuntimeError::Verification(format!(
                "release member changed since promotion: {relative}"
            )));
        }
    }
    Ok(())
}

fn canonical_relative_path(value: &str) -> bool {
    !value.is_empty()
        && !value.contains('\\')
        && Path::new(value)
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
}

fn collect_regular(
    directory: &Path,
    base: &Path,
    members: &mut BTreeSet<String>,
) -> Result<(), RuntimeError> {
    let directory_metadata = fs::symlink_metadata(directory)?;
    if directory_metadata.file_type().is_symlink() || !directory_metadata.is_dir() {
        return Err(RuntimeError::Verification(format!(
            "release directory is not a direct directory: {}",
            directory.display()
        )));
    }
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let path = entry.path();
        let file_type = entry.file_type()?;
        if file_type.is_symlink() {
            return Err(RuntimeError::Verification(format!(
                "release member is a symlink: {}",
                path.display()
            )));
        }
        if file_type.is_dir() {
            collect_regular(&path, base, members)?;
        } else if file_type.is_file() {
            let relative = path
                .strip_prefix(base)
                .map_err(|_| RuntimeError::Refused("release member escaped".to_owned()))?
                .to_str()
                .ok_or_else(|| {
                    RuntimeError::Verification("release member path is not UTF-8".to_owned())
                })?
                .to_owned();
            if !canonical_relative_path(&relative) || !members.insert(relative) {
                return Err(RuntimeError::Verification(
                    "release contains a duplicate or non-canonical member".to_owned(),
                ));
            }
        } else {
            return Err(RuntimeError::Verification(format!(
                "release member is not a regular file: {}",
                path.display()
            )));
        }
    }
    Ok(())
}

/// Make the mirrored release read-only, deepest first, so the directories are
/// still writable while their contents are being sealed.
fn seal(release: &Path) -> Result<(), RuntimeError> {
    for entry in fs::read_dir(release)? {
        let entry = entry?;
        let path = entry.path();
        if entry.file_type()?.is_dir() {
            seal(&path)?;
            fs::set_permissions(&path, fs::Permissions::from_mode(0o555))?;
        } else {
            let mode = fs::metadata(&path)?.permissions().mode();
            let sealed = if mode & 0o111 != 0 { 0o555 } else { 0o444 };
            fs::set_permissions(&path, fs::Permissions::from_mode(sealed))?;
        }
    }
    Ok(())
}

fn write_atomically(path: &Path, bytes: &[u8], mode: u32) -> Result<(), RuntimeError> {
    let directory = path
        .parent()
        .ok_or_else(|| RuntimeError::Refused("target has no directory".to_owned()))?;
    fs::create_dir_all(directory)?;
    let temporary = directory.join(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("file"),
        uuid::Uuid::new_v4()
    ));
    let result = (|| -> io::Result<()> {
        let mut file = File::create(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(mode))?;
        fs::rename(&temporary, path)?;
        File::open(directory)?.sync_all()
    })();
    if let Err(error) = result {
        let _ = fs::remove_file(&temporary);
        return Err(RuntimeError::Io(error));
    }
    Ok(())
}

pub fn file_digest(path: &Path) -> io::Result<String> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "digest input is not a direct regular file",
        ));
    }
    let mut file = File::open(path)?;
    let opened = file.metadata()?;
    if !opened.is_file() || metadata.dev() != opened.dev() || metadata.ino() != opened.ino() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "digest input changed while it was opened",
        ));
    }
    digest_open_file(&mut file)
}

fn digest_open_file(file: &mut File) -> io::Result<String> {
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("sha256:{:x}", hasher.finalize()))
}

fn read_regular_file(path: &Path) -> Result<Vec<u8>, RuntimeError> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(RuntimeError::Verification(format!(
            "expected a direct regular file: {}",
            path.display()
        )));
    }
    let mut file = File::open(path)?;
    let opened = file.metadata()?;
    if metadata.dev() != opened.dev() || metadata.ino() != opened.ino() {
        return Err(RuntimeError::Verification(format!(
            "file changed while it was opened: {}",
            path.display()
        )));
    }
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    Ok(bytes)
}

fn runtime_binary_directory(executable: &Path) -> Result<PathBuf, RuntimeError> {
    let parent = executable
        .parent()
        .ok_or_else(|| RuntimeError::Verification("runtime binary has no directory".to_owned()))?;
    if parent.file_name().and_then(|value| value.to_str()) == Some("deps") {
        return parent.parent().map(Path::to_path_buf).ok_or_else(|| {
            RuntimeError::Verification("test runtime binary has no profile directory".to_owned())
        });
    }
    Ok(parent.to_path_buf())
}

fn runtime_binary_digests(directory: &Path) -> Result<BTreeMap<String, String>, RuntimeError> {
    let mut binaries = BTreeMap::new();
    for name in source_fingerprint::manifest().binaries {
        binaries.insert(name.clone(), file_digest(&directory.join(name))?);
    }
    Ok(binaries)
}

fn authority_file(state_root: &Path) -> Result<File, RuntimeError> {
    fs::create_dir_all(state_root)?;
    let path = state_root.join(".authority.lock");
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(&path)?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
    Ok(lock)
}

fn authority_lock(state_root: &Path) -> Result<File, RuntimeError> {
    let lock = authority_file(state_root)?;
    lock.lock_exclusive()?;
    Ok(lock)
}

fn reject_git_url_rewrites(repo: &Path) -> Result<(), RuntimeError> {
    let common = git(repo, &["rev-parse", "--git-common-dir"])?;
    let common = PathBuf::from(common);
    let config = if common.is_absolute() {
        common.join("config")
    } else {
        repo.join(common).join("config")
    };
    let text = String::from_utf8_lossy(&read_regular_file(&config)?).to_ascii_lowercase();
    if text.contains("insteadof") || text.contains("[include") {
        return Err(RuntimeError::Refused(
            "repository-local Git URL rewrites/includes are forbidden during promotion".to_owned(),
        ));
    }
    Ok(())
}

fn trusted_git_fetch(
    repo: &Path,
    upstream_url: &str,
    branch: &str,
    destination_ref: &str,
) -> Result<(), RuntimeError> {
    let mut command = Command::new("/usr/bin/git");
    command
        .current_dir(repo)
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_TERMINAL_PROMPT", "0")
        .args([
            "fetch",
            "--no-tags",
            "--force",
            upstream_url,
            &format!("refs/heads/{branch}:{destination_ref}"),
        ]);
    if let Some(home) = std::env::var_os("HOME") {
        command.env("HOME", home);
    }
    if let Some(socket) = std::env::var_os("SSH_AUTH_SOCK") {
        command.env("SSH_AUTH_SOCK", socket);
    }
    let output = command.output().map_err(|error| {
        RuntimeError::Command(format!("trusted protected-branch fetch: {error}"))
    })?;
    if !output.status.success() {
        return Err(RuntimeError::Command(format!(
            "trusted protected-branch fetch failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )));
    }
    Ok(())
}

fn trusted_cargo_path() -> Result<PathBuf, RuntimeError> {
    for variable in [
        "CARGO",
        "RUSTC",
        "RUSTC_WRAPPER",
        "RUSTFLAGS",
        "CARGO_ENCODED_RUSTFLAGS",
    ] {
        if std::env::var_os(variable).is_some() {
            return Err(RuntimeError::Refused(format!(
                "{variable} must be unset for a promotion build"
            )));
        }
    }
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| RuntimeError::Refused("HOME is not set".to_owned()))?;
    let triple = match (std::env::consts::ARCH, std::env::consts::OS) {
        ("aarch64", "macos") => "aarch64-apple-darwin",
        ("x86_64", "macos") => "x86_64-apple-darwin",
        ("aarch64", "linux") => "aarch64-unknown-linux-gnu",
        ("x86_64", "linux") => "x86_64-unknown-linux-gnu",
        (arch, os) => {
            return Err(RuntimeError::Refused(format!(
                "unsupported promotion builder host {arch}-{os}"
            )));
        }
    };
    let cargo = home
        .join(".rustup/toolchains")
        .join(format!("1.97.1-{triple}"))
        .join("bin/cargo");
    let metadata = fs::symlink_metadata(&cargo)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(RuntimeError::Refused(
            "the pinned Cargo toolchain is not a direct regular file".to_owned(),
        ));
    }
    Ok(cargo)
}

fn run_frozen_build(cargo: &Path, manifest: &Path, target: &Path) -> Result<(), RuntimeError> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| RuntimeError::Refused("HOME is not set".to_owned()))?;
    let bin = cargo.parent().ok_or_else(|| {
        RuntimeError::Refused("pinned Cargo path has no bin directory".to_owned())
    })?;
    let rustc = bin.join("rustc");
    let rustdoc = bin.join("rustdoc");
    for tool in [&rustc, &rustdoc] {
        let metadata = fs::symlink_metadata(tool)?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(RuntimeError::Refused(format!(
                "pinned build tool is not a direct regular file: {}",
                tool.display()
            )));
        }
    }
    let output = Command::new(cargo)
        .env_clear()
        .env("HOME", &home)
        .env("CARGO_HOME", home.join(".cargo"))
        .env("RUSTUP_HOME", home.join(".rustup"))
        .env("PATH", format!("{}:/usr/bin:/bin", bin.display()))
        .env("RUSTC", rustc)
        .env("RUSTDOC", rustdoc)
        .env("CARGO_NET_OFFLINE", "true")
        .env("CARGO_TARGET_DIR", target)
        .args([
            "build",
            "--release",
            "--frozen",
            "--bins",
            "--manifest-path",
        ])
        .arg(manifest)
        .output()
        .map_err(|error| RuntimeError::Command(format!("pinned cargo build: {error}")))?;
    if !output.status.success() {
        return Err(RuntimeError::Command(format!(
            "pinned cargo build failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )));
    }
    Ok(())
}

fn read_json<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<T, RuntimeError> {
    Ok(serde_json::from_slice(&read_regular_file(path)?)?)
}

fn now() -> Result<u64, RuntimeError> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .map_err(|_| RuntimeError::Refused("system time is before the Unix epoch".to_owned()))
}

fn path_argument(path: &Path) -> Result<&str, RuntimeError> {
    path.to_str().ok_or_else(|| {
        RuntimeError::Refused(format!("path is not valid UTF-8: {}", path.display()))
    })
}

fn git(repo: &Path, arguments: &[&str]) -> Result<String, RuntimeError> {
    run("/usr/bin/git", arguments, Some(repo), &[])
}

fn run(
    program: &str,
    arguments: &[&str],
    cwd: Option<&Path>,
    environment: &[(&str, PathBuf)],
) -> Result<String, RuntimeError> {
    let mut command = Command::new(program);
    command.env_clear().env("PATH", "/usr/bin:/bin");
    command.args(arguments);
    if let Some(cwd) = cwd {
        command.current_dir(cwd);
    }
    for (key, value) in environment {
        command.env(key, value);
    }
    let output = command
        .output()
        .map_err(|error| RuntimeError::Command(format!("{program}: {error}")))?;
    if !output.status.success() {
        return Err(RuntimeError::Command(format!(
            "{program} {arguments:?} failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        )));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}
