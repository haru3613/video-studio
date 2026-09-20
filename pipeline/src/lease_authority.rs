use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::{Lease, ProjectStore, StoreError};

const PRIVATE_SCHEMA: &str = "haru.private_lease_capability.v1";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct PrivateLease {
    schema: String,
    project_sha256: String,
    owner: String,
    lease_id: String,
    generation: u64,
    expires_at: u64,
    idempotency_key: String,
    runtime_id: String,
    operation_class: String,
    capability: String,
}

#[derive(Debug, Clone)]
pub(crate) struct ResolvedLease {
    pub owner: String,
    pub lease_id: String,
    pub generation: u64,
    pub capability: String,
}

#[derive(Debug, Clone)]
pub(crate) struct LeaseAuthority {
    root: PathBuf,
    runtime_id: String,
}

impl LeaseAuthority {
    pub(crate) fn new(state_root: &Path, runtime_id: &str) -> Self {
        Self {
            root: state_root.join("private-leases"),
            runtime_id: runtime_id.to_owned(),
        }
    }

    pub(crate) fn claim(
        &self,
        project: &Path,
        owner: &str,
        ttl: Duration,
        idempotency_key: &str,
        now: SystemTime,
    ) -> Result<Lease, StoreError> {
        if let Some(existing) = ProjectStore::new(project).current_lease()?
            && existing.owner == owner
            && self
                .read_private(project, &existing.lease_id)
                .is_ok_and(|private| private.idempotency_key == idempotency_key)
        {
            return Ok(existing);
        }
        let lease = ProjectStore::new(project).claim_at(owner, ttl, now)?;
        let private = PrivateLease {
            schema: PRIVATE_SCHEMA.to_owned(),
            project_sha256: project_digest(project)?,
            owner: lease.owner.clone(),
            lease_id: lease.lease_id.clone(),
            generation: lease.generation,
            expires_at: lease.expires_at,
            idempotency_key: idempotency_key.to_owned(),
            runtime_id: self.runtime_id.clone(),
            operation_class: "project_mutation".to_owned(),
            capability: lease.token.clone(),
        };
        if let Err(error) = self.write_private(&private) {
            let _ = ProjectStore::new(project).release_at(owner, &lease.token, now);
            return Err(StoreError::Io(error));
        }
        Ok(lease)
    }

    pub(crate) fn resolve(
        &self,
        project: &Path,
        owner: &str,
        lease_id: &str,
    ) -> Result<ResolvedLease, StoreError> {
        if owner.trim().is_empty() || Uuid::parse_str(lease_id).is_err() {
            return Err(StoreError::LeaseMismatch);
        }
        let private = self.read_private(project, lease_id)?;
        if private.schema != PRIVATE_SCHEMA
            || private.project_sha256 != project_digest(project)?
            || private.owner != owner
            || private.lease_id != lease_id
            || private.runtime_id != self.runtime_id
            || private.operation_class != "project_mutation"
        {
            return Err(StoreError::LeaseMismatch);
        }
        Ok(ResolvedLease {
            owner: private.owner,
            lease_id: private.lease_id,
            generation: private.generation,
            capability: private.capability,
        })
    }

    pub(crate) fn renew(
        &self,
        project: &Path,
        owner: &str,
        lease_id: &str,
        ttl: Duration,
        now: SystemTime,
    ) -> Result<Lease, StoreError> {
        let resolved = self.resolve(project, owner, lease_id)?;
        let lease = ProjectStore::new(project).renew_at(owner, &resolved.capability, ttl, now)?;
        let mut private = self.read_private(project, lease_id)?;
        private.expires_at = lease.expires_at;
        self.write_private(&private).map_err(StoreError::Io)?;
        Ok(lease)
    }

    pub(crate) fn release(
        &self,
        project: &Path,
        owner: &str,
        lease_id: &str,
        now: SystemTime,
    ) -> Result<(), StoreError> {
        let resolved = self.resolve(project, owner, lease_id)?;
        ProjectStore::new(project).release_at(owner, &resolved.capability, now)?;
        match fs::remove_file(self.private_path(project, lease_id)?) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(StoreError::Io(error)),
        }
    }

    fn write_private(&self, value: &PrivateLease) -> io::Result<()> {
        ensure_private_dir(&self.root)?;
        let directory = self.root.join(&value.project_sha256);
        ensure_private_dir(&directory)?;
        let target = directory.join(format!("{}.json", value.lease_id));
        let temporary = directory.join(format!(".{}.{}.tmp", value.lease_id, Uuid::new_v4()));
        let mut bytes = serde_json::to_vec(value).map_err(io::Error::other)?;
        bytes.push(b'\n');
        let result = (|| {
            let mut file = OpenOptions::new()
                .create_new(true)
                .write(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&temporary)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&temporary, &target)?;
            File::open(&directory)?.sync_all()
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result
    }

    fn read_private(&self, project: &Path, lease_id: &str) -> Result<PrivateLease, StoreError> {
        let path = self.private_path(project, lease_id)?;
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(path)?;
        let metadata = file.metadata()?;
        if !metadata.file_type().is_file()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.permissions().mode() & 0o777 != 0o600
        {
            return Err(StoreError::LeaseMismatch);
        }
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;
        Ok(serde_json::from_slice(&bytes)?)
    }

    fn private_path(&self, project: &Path, lease_id: &str) -> Result<PathBuf, StoreError> {
        if Uuid::parse_str(lease_id).is_err() {
            return Err(StoreError::LeaseMismatch);
        }
        Ok(self
            .root
            .join(project_digest(project)?)
            .join(format!("{lease_id}.json")))
    }
}

fn project_digest(project: &Path) -> Result<String, StoreError> {
    let project = project.canonicalize()?;
    Ok(format!(
        "{:x}",
        Sha256::digest(project.as_os_str().as_encoded_bytes())
    ))
}

fn ensure_private_dir(path: &Path) -> io::Result<()> {
    if !path.exists() {
        fs::create_dir_all(path)?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.file_type().is_dir()
        || metadata.file_type().is_symlink()
        || metadata.uid() != unsafe { libc::geteuid() }
        || metadata.permissions().mode() & 0o777 != 0o700
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "private lease directory is unsafe",
        ));
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};
    use tempfile::tempdir;

    #[test]
    fn private_capability_is_owner_bound_and_never_serialized_with_the_lease() {
        let directory = tempdir().unwrap();
        let project = directory.path().join("project");
        let state = directory.path().join("state");
        fs::create_dir(&project).unwrap();
        let authority = LeaseAuthority::new(&state, "runtime-1");
        let lease = authority
            .claim(
                &project,
                "codex",
                Duration::from_secs(60),
                "claim-once",
                SystemTime::now(),
            )
            .unwrap();

        let resolved = authority
            .resolve(&project, "codex", &lease.lease_id)
            .unwrap();
        assert_eq!(resolved.capability, lease.token);
        assert!(matches!(
            authority.resolve(&project, "hermes", &lease.lease_id),
            Err(StoreError::LeaseMismatch)
        ));
        assert!(
            !serde_json::to_string(&lease)
                .unwrap()
                .contains(&lease.token)
        );
        assert_eq!(
            fs::metadata(authority.private_path(&project, &lease.lease_id).unwrap())
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        assert_eq!(
            fs::metadata(&authority.root).unwrap().permissions().mode() & 0o777,
            0o700
        );
    }

    #[test]
    fn unsafe_private_state_root_fails_closed_and_rolls_back_the_public_lease() {
        let directory = tempdir().unwrap();
        let project = directory.path().join("project");
        let state = directory.path().join("state");
        let outside = directory.path().join("outside");
        fs::create_dir(&project).unwrap();
        fs::create_dir_all(&outside).unwrap();
        fs::create_dir_all(&state).unwrap();
        symlink(&outside, state.join("private-leases")).unwrap();
        let authority = LeaseAuthority::new(&state, "runtime-1");

        assert!(matches!(
            authority.claim(
                &project,
                "codex",
                Duration::from_secs(60),
                "claim-once",
                SystemTime::now(),
            ),
            Err(StoreError::Io(_))
        ));
        assert!(
            ProjectStore::new(&project)
                .current_lease()
                .unwrap()
                .is_none()
        );
        assert!(fs::read_dir(&outside).unwrap().next().is_none());
    }
}
