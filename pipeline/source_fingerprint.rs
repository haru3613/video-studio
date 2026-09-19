//! The runtime source closure, declared once.
//!
//! The hand-written path list this file used to carry was the wrong shape: it
//! could only ever be as complete as the last person who edited it remembered,
//! and it already missed `tools/canonical_layout.py` and
//! `tools/editorial_contract.py` -- files every project gate imports. The
//! closure is now declared as whole trees in `runtime-manifest.json`, so a new
//! runner script or a new transitive Python import is inside the fingerprint
//! the moment it lands, without anybody remembering to say so.
//!
//! The manifest is compiled into the binary as well as hashed on disk. The
//! compiled copy is what the runtime enforces, so an edited on-disk manifest
//! cannot widen or narrow the closure it is checked against; hashing the file
//! makes that edit visible instead of silent.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use sha2::{Digest, Sha256};

pub const MANIFEST_SOURCE: &str = include_str!("runtime-manifest.json");

pub const MANIFEST_SCHEMA: &str = "haru.runtime_manifest.v1";

#[derive(Debug, Clone, Deserialize)]
pub struct Manifest {
    pub schema: String,
    pub runtime_contract: String,
    pub evaluator_contract: String,
    pub artifact_contract: String,
    pub supported_project_contracts: SupportedProjectContracts,
    pub security_capabilities: Vec<String>,
    pub youtube_channel_id: String,
    pub publish_approval_signer: Option<PublishApprovalSigner>,
    pub launcher: Launcher,
    pub state_root: String,
    pub closure: Closure,
    pub binaries: Vec<String>,
    pub tools: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PublishApprovalSigner {
    pub algorithm: String,
    pub key_id: String,
    pub public_key_x963_base64: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SupportedProjectContracts {
    pub runtime: Vec<String>,
    pub evaluator: Vec<String>,
    pub artifact: Vec<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Launcher {
    pub source: String,
    pub install_path: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Closure {
    pub files: Vec<String>,
    pub trees: Vec<Tree>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Tree {
    pub path: String,
    /// Empty means every regular file under the tree.
    pub suffixes: Vec<String>,
}

/// The compiled-in manifest. A malformed manifest is a build defect, not a
/// runtime condition: `build.rs` parses the same bytes before anything links.
pub fn manifest() -> Manifest {
    let manifest: Manifest = serde_json::from_str(MANIFEST_SOURCE)
        .expect("runtime-manifest.json is not a valid manifest");
    assert_eq!(
        manifest.schema, MANIFEST_SCHEMA,
        "runtime-manifest.json declares an unsupported schema"
    );
    manifest
}

pub fn manifest_digest() -> String {
    format!("{:x}", Sha256::digest(MANIFEST_SOURCE.as_bytes()))
}

/// Every closure member, sorted and deduplicated, so two runs over the same
/// tree produce the same list on any filesystem.
pub fn runtime_paths(repo_root: &Path) -> io::Result<Vec<PathBuf>> {
    let repo_root = repo_root.canonicalize()?;
    let manifest = manifest();
    let mut paths = Vec::new();
    for file in &manifest.closure.files {
        let path = repo_root.join(file);
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "runtime closure member is not a direct file: {}",
                    path.display()
                ),
            ));
        }
        let resolved = path.canonicalize()?;
        require_contained(&repo_root, &resolved)?;
        paths.push(resolved);
    }
    for tree in &manifest.closure.trees {
        let path = repo_root.join(&tree.path);
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "runtime closure tree is not a direct directory: {}",
                    path.display()
                ),
            ));
        }
        let resolved = path.canonicalize()?;
        require_contained(&repo_root, &resolved)?;
        collect_files(&repo_root, &resolved, &tree.suffixes, &mut paths)?;
    }
    paths.sort();
    paths.dedup();
    Ok(paths)
}

/// The closure digest: every member's repository-relative path, length and
/// bytes, plus the manifest that decided the membership.
pub fn calculate(repo_root: &Path) -> io::Result<String> {
    let repo_root = repo_root.canonicalize()?;
    let mut digest = Sha256::new();
    digest.update(MANIFEST_SOURCE.as_bytes());
    digest.update([0]);
    for path in runtime_paths(&repo_root)? {
        let relative = path.strip_prefix(&repo_root).map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "runtime closure escaped repository",
            )
        })?;
        digest.update(relative.as_os_str().as_encoded_bytes());
        digest.update([0]);
        let bytes = fs::read(&path)?;
        digest.update(bytes.len().to_le_bytes());
        digest.update(&bytes);
        digest.update([0]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn collect_files(
    repo_root: &Path,
    directory: &Path,
    suffixes: &[String],
    paths: &mut Vec<PathBuf>,
) -> io::Result<()> {
    require_contained(repo_root, directory)?;
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let file_type = entry.file_type()?;
        let path = entry.path();
        if file_type.is_symlink() {
            // A symlink inside the closure is a way to hash one file and run
            // another. The repository forbids tracked symlinks; refuse loudly
            // rather than fingerprint the link target.
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("symlink inside the runtime closure: {}", path.display()),
            ));
        }
        let resolved = path.canonicalize()?;
        require_contained(repo_root, &resolved)?;
        if file_type.is_dir() {
            collect_files(repo_root, &resolved, suffixes, paths)?;
        } else if file_type.is_file() && selected(&resolved, suffixes) {
            paths.push(resolved);
        }
    }
    Ok(())
}

fn require_contained(repo_root: &Path, path: &Path) -> io::Result<()> {
    if path == repo_root || path.starts_with(repo_root) {
        Ok(())
    } else {
        Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("runtime closure escaped repository: {}", path.display()),
        ))
    }
}

fn selected(path: &Path, suffixes: &[String]) -> bool {
    if suffixes.is_empty() {
        return true;
    }
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    suffixes
        .iter()
        .any(|suffix| name.ends_with(suffix.as_str()))
}
