//! What the runtime authority has to get right: a closure nobody can forget to
//! extend, a promotion that only ever accepts protected-branch bytes, a release
//! that stops verifying the moment a byte moves, and an upload hold that is on
//! unless somebody deliberately turned it off.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use pipeline::application;
use pipeline::mcp;
use pipeline::runtime::{
    self, PromotionMode, PromotionRequest, PromotionSource, ReleasePlan, RuntimeAuthority,
    RuntimeError, RuntimeIdentity, RuntimeProvenance, ToolSurface,
};
use pipeline::source_fingerprint;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tempfile::{TempDir, tempdir};

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf()
}

fn relative_closure(root: &Path) -> Vec<String> {
    source_fingerprint::runtime_paths(root)
        .unwrap()
        .into_iter()
        .map(|path| {
            path.strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .into_owned()
        })
        .collect()
}

#[test]
fn stable_launcher_matches_the_known_bootstrap_bytes() {
    // Runtime selection rejects a launcher that differs from a release it is
    // about to activate. Keep the bootstrap launcher fixed until a deliberate
    // launcher migration updates this contract.
    assert_eq!(
        format!(
            "{:x}",
            Sha256::digest(fs::read(repo_root().join("scripts/video-studio-mcp")).unwrap())
        ),
        "47c1d58252d55878bc9f8b48e5a5ee9332da22451e6c2f18358040a8b1829a69"
    );
}

#[test]
fn the_closure_covers_every_runner_and_transitive_python_import() {
    let repo = repo_root();
    let closure = relative_closure(&repo);
    let manifest = source_fingerprint::manifest();
    assert_eq!(manifest.state_root, "~/.local/state/video-studio/runtime");
    assert_eq!(
        manifest.launcher.install_path,
        "~/.local/share/video-studio/bin/video-studio-mcp"
    );
    assert!(manifest.youtube_channel_id.is_empty());
    assert!(manifest.publish_approval_signer.is_none());

    for required in [
        "rust-toolchain.toml",
        ".python-version",
        "pyproject.toml",
        "uv.lock",
        "pipeline/Cargo.lock",
        "pipeline/runtime-manifest.json",
        "pipeline/source_fingerprint.rs",
        "pipeline/src/runtime.rs",
        "scripts/verify-project",
        "scripts/video-studio-mcp",
        "scripts/workspace-backup",
        "media-tools/narration/providers/base.py",
        "tools/agent_status.py",
        "tools/local_delivery.py",
        "tools/workspace_backup.py",
        "tools/workspace_barrier.py",
        "tools/dashboard/index.html",
        "templates/narrated/remotion/package-lock.json",
        "examples/narrated/cue-driven-demo/project.json",
        // The two the previous hand-written list forgot. Every project gate
        // imports canonical_layout, which imports editorial_contract, and a
        // runtime could change what a gate accepts without its fingerprint
        // moving at all.
        "tools/canonical_layout.py",
        "tools/editorial_contract.py",
    ] {
        assert!(
            closure.iter().any(|path| path == required),
            "{required} is outside the runtime closure"
        );
    }

    // Whole trees, not a curated subset: the point is that nobody has to
    // remember to add the next runner or the next module it imports.
    let scripts = fs::read_dir(repo.join("scripts"))
        .unwrap()
        .filter(|entry| entry.as_ref().unwrap().file_type().unwrap().is_file())
        .count();
    assert_eq!(
        closure
            .iter()
            .filter(|path| path.starts_with("scripts/"))
            .count(),
        scripts
    );
    assert!(
        closure
            .iter()
            .filter(|path| path.starts_with("tools/") && path.ends_with(".py"))
            .count()
            > 30
    );
}

/// Copy the closure of `source` into a fresh tree, preserving relative paths.
fn mirror_closure(source: &Path) -> TempDir {
    let mirror = tempdir().unwrap();
    for path in source_fingerprint::runtime_paths(source).unwrap() {
        let target = mirror.path().join(path.strip_prefix(source).unwrap());
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::copy(&path, &target).unwrap();
    }
    mirror
}

#[test]
fn changing_one_closure_member_changes_the_fingerprint() {
    let repo = repo_root();
    let mirror = mirror_closure(&repo);
    assert_eq!(
        source_fingerprint::calculate(mirror.path()).unwrap(),
        source_fingerprint::calculate(&repo).unwrap(),
        "a faithful mirror of the closure must fingerprint identically"
    );

    let drifted = mirror.path().join("tools/canonical_layout.py");
    let mut contents = fs::read_to_string(&drifted).unwrap();
    contents.push_str("\n# an unreviewed edit to a gate the runtime executes\n");
    fs::write(&drifted, contents).unwrap();

    assert_ne!(
        source_fingerprint::calculate(mirror.path()).unwrap(),
        source_fingerprint::calculate(&repo).unwrap()
    );
}

#[cfg(unix)]
#[test]
fn a_symlinked_closure_tree_root_is_refused_before_traversal() {
    use std::os::unix::fs::symlink;

    let repo = repo_root();
    let mirror = mirror_closure(&repo);
    let external = tempdir().unwrap();
    fs::write(external.path().join("canonical_layout.py"), "outside\n").unwrap();
    fs::remove_dir_all(mirror.path().join("tools")).unwrap();
    symlink(external.path(), mirror.path().join("tools")).unwrap();

    let error = source_fingerprint::runtime_paths(mirror.path())
        .expect_err("a closure tree root must never be followed through a symlink");
    assert_eq!(error.kind(), std::io::ErrorKind::InvalidData);
    assert!(
        error.to_string().contains("not a direct directory"),
        "{error}"
    );
}

#[cfg(unix)]
#[test]
fn stable_launcher_ignores_hostile_path_utilities() {
    use std::os::unix::fs::PermissionsExt;

    let root = tempdir().unwrap();
    let home = root.path().join("home");
    let state = home.join(".local/state/video-studio/runtime");
    let selection = state.join("active");
    let release = root.path().join("release");
    let bin = release.join("bin");
    let hostile = root.path().join("hostile");
    fs::create_dir_all(&selection).unwrap();
    fs::create_dir_all(&bin).unwrap();
    fs::create_dir_all(&hostile).unwrap();

    let verifier = b"#!/bin/sh\nexit 0\n";
    let server = b"#!/bin/sh\nset -eu\n[ -z \"${HVP_RUNTIME_STATE:-}\" ]\n[ -z \"${HARU_VIDEO_STUDIO_ATTESTATION_ROOT:-}\" ]\n";
    fs::write(bin.join("hvp-runtime"), verifier).unwrap();
    fs::write(bin.join("hvp-mcp"), server).unwrap();
    for executable in [bin.join("hvp-runtime"), bin.join("hvp-mcp")] {
        fs::set_permissions(executable, fs::Permissions::from_mode(0o755)).unwrap();
    }
    let digests = format!(
        "{:x}  bin/hvp-mcp\n{:x}  bin/hvp-runtime\n",
        Sha256::digest(server),
        Sha256::digest(verifier),
    );
    fs::write(release.join("DIGESTS"), &digests).unwrap();
    fs::write(release.join("receipt.json"), "{}\n").unwrap();
    fs::write(
        selection.join("active-digest"),
        format!("{:x}  fixture\n", Sha256::digest(digests.as_bytes())),
    )
    .unwrap();
    std::os::unix::fs::symlink(&release, selection.join("release")).unwrap();

    let marker = root.path().join("hostile-ran");
    for utility in ["cut", "shasum"] {
        let path = hostile.join(utility);
        fs::write(
            &path,
            format!("#!/bin/sh\nprintf pwned > '{}'\nexit 1\n", marker.display()),
        )
        .unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    let status = Command::new(repo_root().join("scripts/video-studio-mcp"))
        .env("HOME", &home)
        .env("HVP_RUNTIME_STATE", root.path().join("decoy-state"))
        .env(
            "HARU_VIDEO_STUDIO_ATTESTATION_ROOT",
            root.path().join("decoy-attestations"),
        )
        .env(
            "HARU_VIDEO_STUDIO_SELF_EVAL_STATE_ROOT",
            root.path().join("decoy-review-ledger"),
        )
        .env(
            "HARU_VIDEO_STUDIO_SELF_EVAL_ATTESTATION_ROOT",
            root.path().join("decoy-review-attestations"),
        )
        .env("PATH", &hostile)
        .status()
        .unwrap();
    assert!(status.success());
    assert!(
        !marker.exists(),
        "launcher executed an inherited-PATH utility"
    );

    // The same isolated HOME must fail closed when either the immutable bytes
    // or the active-generation pointer is stale.
    fs::write(bin.join("hvp-mcp"), b"#!/bin/sh\nexit 7\n").unwrap();
    fs::set_permissions(bin.join("hvp-mcp"), fs::Permissions::from_mode(0o755)).unwrap();
    assert!(
        !Command::new(repo_root().join("scripts/video-studio-mcp"))
            .env("HOME", &home)
            .status()
            .unwrap()
            .success(),
        "launcher accepted a corrupted active runtime"
    );
    fs::write(bin.join("hvp-mcp"), server).unwrap();
    fs::set_permissions(bin.join("hvp-mcp"), fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(
        selection.join("active-digest"),
        format!("{}  stale\n", "0".repeat(64)),
    )
    .unwrap();
    assert!(
        !Command::new(repo_root().join("scripts/video-studio-mcp"))
            .env("HOME", &home)
            .status()
            .unwrap()
            .success(),
        "launcher accepted a stale active-generation pointer"
    );
}
#[test]
fn the_identity_binds_the_exact_tool_surface_the_router_serves() {
    let surface = mcp::tool_surface();
    let manifest = source_fingerprint::manifest();
    assert_eq!(surface.names, manifest.tools);

    let identity = RuntimeIdentity::embedded(&surface).unwrap();
    assert_eq!(identity, RuntimeIdentity::embedded(&surface).unwrap());
    assert_eq!(identity.tool_surface_digest, surface.digest);
    assert_eq!(identity.runtime_contract, manifest.runtime_contract);
    assert_eq!(identity.provenance, RuntimeProvenance::SelfBuild);

    // Binding a commit is what makes a promoted runtime a different identity
    // from the development build of the same tree.
    let promoted =
        RuntimeIdentity::compute(&repo_root(), &surface, Some("abc"), Some("def")).unwrap();
    assert_ne!(promoted.runtime_id, identity.runtime_id);
    let trusted = RuntimeIdentity::compute_with_provenance(
        &repo_root(),
        &surface,
        Some("abc"),
        Some("def"),
        RuntimeProvenance::TrustedUpstream,
    )
    .unwrap();
    assert_eq!(promoted.provenance, RuntimeProvenance::SelfBuild);
    assert_eq!(trusted.provenance, RuntimeProvenance::TrustedUpstream);
    assert_ne!(promoted.runtime_id, trusted.runtime_id);

    let mut names = surface.names.clone();
    names.push("undeclared_tool".to_owned());
    let drifted = ToolSurface {
        names,
        digest: surface.digest.clone(),
    };
    assert!(
        RuntimeIdentity::embedded(&drifted).is_err(),
        "a tool the manifest never declared must not produce an identity"
    );
}

#[test]
fn a_created_project_declares_the_runtime_contract_and_still_owes_its_lane() {
    let directory = tempdir().unwrap();
    let projects = directory.path().join("projects");
    fs::create_dir(&projects).unwrap();

    let created = application::create(&projects, "fresh-project");
    assert_eq!(created.code, "project_created");

    let project = projects.join("fresh-project");
    let contract: Value =
        serde_json::from_slice(&fs::read(project.join("project-contract.json")).unwrap()).unwrap();
    assert_eq!(contract["schema"], "haru.project_contract.v1");
    assert_eq!(
        contract["runtime_contract"]["schema"],
        "haru.project_runtime_contract.v1"
    );
    // The lane is still the human's call; only compatibility is written.
    assert_eq!(contract["lane_contract"], application::SCAFFOLD_MARKER);

    let authority = RuntimeAuthority::development(
        &projects,
        directory.path().join("runtime-state"),
        mcp::tool_surface(),
    )
    .unwrap();
    assert!(authority.project_block(&project).is_none());
    let mutation = authority.mutation_block(&project).unwrap();
    assert_eq!(mutation.code, "runtime_unpromoted");
    assert_eq!(
        mutation.data.as_ref().unwrap()["schema"],
        "haru.runtime_unpromoted.v1"
    );

    let legacy = projects.join("legacy-project");
    fs::create_dir(&legacy).unwrap();
    fs::write(
        legacy.join("project-contract.json"),
        br#"{"schema":"haru.project_contract.v1","lane_contract":"manual.v1"}"#,
    )
    .unwrap();
    let blocked = authority.project_block(&legacy).unwrap();
    assert_eq!(blocked.code, "runtime_incompatible");
    assert_eq!(
        blocked.data.as_ref().unwrap()["reason"],
        "missing_runtime_contract"
    );

    let future = projects.join("future-project");
    fs::create_dir(&future).unwrap();
    fs::write(
        future.join("project-contract.json"),
        br#"{"schema":"haru.project_contract.v1","runtime_contract":{"schema":"haru.project_runtime_contract.v1","runtime":"haru.runtime.v9","evaluator":"haru.evaluator.v9","artifact":"haru.artifact.v9"}}"#,
    )
    .unwrap();
    assert_eq!(
        authority
            .project_block(&future)
            .unwrap()
            .data
            .as_ref()
            .unwrap()["reason"],
        "unsupported_runtime_contract"
    );
}

#[test]
fn production_upload_stays_held_until_a_complete_lift_is_recorded() {
    let directory = tempdir().unwrap();
    let state = directory.path();

    // Absent state is held state. Nothing has to be written to be safe.
    assert!(runtime::upload_hold(state).held);
    assert_eq!(runtime::upload_hold(state).reason, "upload_hold_default");

    fs::write(state.join("upload-hold.json"), b"not json at all").unwrap();
    assert!(runtime::upload_hold(state).held);

    // A lift that does not say who or why is not a lift.
    fs::write(
        state.join("upload-hold.json"),
        serde_json::to_vec(&json!({
            "schema": "haru.upload_hold.v1",
            "held": false,
            "reason": "",
            "changed_at": 0,
            "changed_by": "",
        }))
        .unwrap(),
    )
    .unwrap();
    assert!(runtime::upload_hold(state).held);
    assert_eq!(
        runtime::upload_hold(state).reason,
        "upload_hold_incomplete_lift"
    );

    assert!(runtime::set_upload_hold(state, false, "  ", "harvey").is_err());
    let lifted = runtime::set_upload_hold(state, false, "wave 3 verified", "harvey").unwrap();
    assert!(!lifted.held);
    assert!(!runtime::upload_hold(state).held);

    let reengaged = runtime::set_upload_hold(state, true, "reopened", "harvey").unwrap();
    assert!(reengaged.held);
    assert!(runtime::upload_hold(state).held);
}

/// A git repository with one committed branch, usable as a configured trusted
/// upstream without relying on any project-specific default remote.
fn protected_repository() -> TempDir {
    let directory = tempdir().unwrap();
    let repo = directory.path();
    for arguments in [
        vec!["init", "-b", "main", "."],
        vec!["config", "user.email", "fixture@example.invalid"],
        vec!["config", "user.name", "fixture"],
    ] {
        assert!(
            Command::new("git")
                .args(&arguments)
                .current_dir(repo)
                .status()
                .unwrap()
                .success()
        );
    }
    fs::write(repo.join("reviewed.txt"), "reviewed\n").unwrap();
    for arguments in [vec!["add", "-A"], vec!["commit", "-m", "reviewed"]] {
        assert!(
            Command::new("git")
                .args(&arguments)
                .current_dir(repo)
                .status()
                .unwrap()
                .success()
        );
    }
    directory
}

fn promotion_error(repo: &Path, state: &Path) -> RuntimeError {
    runtime::promote(
        &PromotionRequest {
            repo_root: repo.to_path_buf(),
            state_root: state.to_path_buf(),
            launcher_path: state.join("bin/video-studio-mcp"),
            allow_capability_drop: false,
            source: PromotionMode::TrustedUpstream {
                url: repo.to_string_lossy().into_owned(),
                branch: "main".to_owned(),
            },
        },
        &mcp::tool_surface(),
    )
    .expect_err("promotion should have been refused")
}

/// Record a floor the way promotion does, so a fixture that publishes a release
/// without running `promote` still leaves the state root in the shape every
/// fail-closed floor read expects.
fn record_floor(state: &Path, capabilities: &[String], runtime_id: &str) {
    let active = state.join("active");
    fs::create_dir_all(&active).unwrap();
    fs::write(
        active.join("capability-floor.json"),
        serde_json::to_vec(&json!({
            "schema": "haru.runtime_capability_floor.v1",
            "capabilities": capabilities,
            "runtime_id": runtime_id,
            "updated_at": 1_785_254_400_u64,
        }))
        .unwrap(),
    )
    .unwrap();
}

fn active_selection_bytes(state: &Path) -> Vec<u8> {
    fs::read(state.join("active/active.json")).unwrap()
}

fn active_selection(state: &Path) -> Value {
    serde_json::from_slice(&active_selection_bytes(state)).unwrap()
}

#[test]
fn promotion_refuses_anything_that_is_not_reviewed_protected_bytes() {
    let source = protected_repository();
    let repo = source.path();
    let state = tempdir().unwrap();

    fs::write(repo.join("scratch.txt"), "work in progress\n").unwrap();
    let dirty = promotion_error(repo, state.path());
    assert!(
        matches!(&dirty, RuntimeError::Refused(reason) if reason.contains("dirty")),
        "{dirty}"
    );
    fs::remove_file(repo.join("scratch.txt")).unwrap();

    assert!(
        Command::new("git")
            .args(["checkout", "--detach"])
            .current_dir(repo)
            .status()
            .unwrap()
            .success()
    );
    let detached = promotion_error(repo, state.path());
    assert!(
        matches!(&detached, RuntimeError::Refused(reason) if reason.contains("detached")),
        "{detached}"
    );
    assert!(
        Command::new("git")
            .args(["checkout", "main"])
            .current_dir(repo)
            .status()
            .unwrap()
            .success()
    );

    // A local URL rewrite could redirect even a literal fetch URL, so it is
    // rejected before promotion performs any network or build work.
    assert!(
        Command::new("git")
            .args([
                "config",
                "--local",
                "url.file:///tmp/attacker.insteadOf",
                repo.to_str().unwrap(),
            ])
            .current_dir(repo)
            .status()
            .unwrap()
            .success()
    );
    let rewritten = promotion_error(repo, state.path());
    assert!(
        matches!(&rewritten, RuntimeError::Refused(reason) if reason.contains("URL rewrites")),
        "{rewritten}"
    );

    assert!(
        !state.path().join("active").exists(),
        "a refused promotion still changed the active runtime"
    );
    assert!(!state.path().join("bin/video-studio-mcp").exists());
}

#[test]
fn self_build_refuses_uncommitted_source_without_consulting_a_remote() {
    let source = protected_repository();
    let state = tempdir().unwrap();
    fs::write(source.path().join("uncommitted.txt"), "not reviewed\n").unwrap();
    let refused = runtime::promote(
        &PromotionRequest {
            repo_root: source.path().to_path_buf(),
            state_root: state.path().to_path_buf(),
            launcher_path: state.path().join("bin/video-studio-mcp"),
            allow_capability_drop: false,
            source: PromotionMode::SelfBuild,
        },
        &mcp::tool_surface(),
    )
    .expect_err("self-build must reject uncommitted source");
    assert!(
        matches!(&refused, RuntimeError::Refused(reason) if reason.contains("dirty")),
        "{refused}"
    );
    assert!(!state.path().join("active").exists());
}

#[cfg(unix)]
struct Staged {
    state: TempDir,
    release: PathBuf,
    surface: ToolSurface,
    launcher: PathBuf,
    runtime_id: String,
}

/// Publish a release from the real closure. The binaries are stubs on purpose:
/// every check under test hashes them, and copying three debug binaries would
/// buy nothing but seconds.
#[cfg(unix)]
fn staged_release() -> Staged {
    let repo = repo_root();
    let state = tempdir().unwrap();
    let surface = mcp::tool_surface();

    let binaries = state.path().join("built");
    fs::create_dir_all(&binaries).unwrap();
    for name in source_fingerprint::manifest().binaries {
        fs::write(binaries.join(&name), format!("stub for {name}\n")).unwrap();
    }
    let identity = RuntimeIdentity::compute_with_binaries(
        &repo,
        &surface,
        Some("1111111111111111111111111111111111111111"),
        Some("2222222222222222222222222222222222222222"),
        &binaries,
    )
    .unwrap();

    let release = state
        .path()
        .join("releases")
        .join(identity.release_directory_name());
    let launcher = state.path().join("bin/video-studio-mcp");
    let receipt = runtime::stage_release(&ReleasePlan {
        staging: &state.path().join("staging/release"),
        source: &repo,
        binaries: &binaries,
        identity: &identity,
        origin: PromotionSource {
            remote_ref: "local-committed-source".to_owned(),
            branch: "main".to_owned(),
            commit: "1111111111111111111111111111111111111111".to_owned(),
            tree: "2222222222222222222222222222222222222222".to_owned(),
        },
        release_root: &release,
        launcher_path: &launcher,
        toolchain: "cargo 1.97.0 (fixture)".to_owned(),
    })
    .unwrap();
    runtime::activate(state.path(), &release, &receipt, "promotion").unwrap();
    record_floor(
        state.path(),
        &receipt.identity.security_capabilities,
        &receipt.runtime_id,
    );

    Staged {
        runtime_id: receipt.runtime_id.clone(),
        state,
        release,
        surface,
        launcher,
    }
}

#[cfg(unix)]
#[test]
fn generic_self_build_allows_local_mutations_but_never_publishing() {
    let staged = staged_release();
    let authority = RuntimeAuthority::test_promoted(
        &repo_root(),
        staged.state.path().to_path_buf(),
        mcp::tool_surface(),
    )
    .unwrap();
    assert!(authority.mutations_allowed());
    assert!(
        authority
            .mutation_block(Path::new("/tmp/local-render"))
            .is_none()
    );

    // Even an old operator-state lift cannot turn a generic build into a
    // publisher: channel and signer configuration are part of the runtime.
    runtime::set_upload_hold(staged.state.path(), false, "fixture", "fixture").unwrap();
    let blocked = authority
        .upload_hold_block(Path::new("/tmp/local-render"))
        .expect("an unconfigured runtime must fail closed for publishing");
    assert_eq!(blocked.code, "publishing_unconfigured");
}

/// Undo the release seal so a test can move a byte. Permissions are not part of
/// any digest, so this on its own must leave verification passing.
#[cfg(unix)]
fn relax(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    if path.is_dir() {
        for entry in fs::read_dir(path).unwrap() {
            relax(&entry.unwrap().path());
        }
    }
}

#[cfg(unix)]
#[test]
fn a_promoted_release_stops_verifying_when_a_single_byte_moves() {
    let staged = staged_release();
    let state = staged.state.path();

    let verified = runtime::verify_release(&staged.release, state, &staged.surface, None).unwrap();
    assert_eq!(verified.receipt.runtime_id, staged.runtime_id);
    assert_eq!(verified.active.selected_reason, "promotion");
    assert_eq!(
        verified.receipt.identity.commit.as_deref(),
        Some("1111111111111111111111111111111111111111")
    );
    // The receipt is what the launcher trusts, so it has to cover itself.
    assert!(verified.receipt.receipt_digest.starts_with("sha256:"));

    // The receipt names the launcher bytes it expects to be installed.
    assert_eq!(
        verified.receipt.launcher.install_path,
        staged.launcher.to_string_lossy()
    );

    // A running binary is checked against the receipt, not against itself.
    runtime::verify_release(
        &staged.release,
        state,
        &staged.surface,
        Some(&staged.release.join("bin/hvp-mcp")),
    )
    .unwrap();

    relax(&staged.release);
    runtime::verify_release(&staged.release, state, &staged.surface, None)
        .expect("relaxing permissions must not change what the digests cover");

    let gate = staged.release.join("source/tools/canonical_layout.py");
    let mut contents = fs::read_to_string(&gate).unwrap();
    contents.push_str("\n# swapped after promotion\n");
    fs::write(&gate, contents).unwrap();
    let error = runtime::verify_release(&staged.release, state, &staged.surface, None)
        .expect_err("an edited gate must not serve");
    assert!(
        matches!(&error, RuntimeError::Verification(reason) if reason.contains("canonical_layout.py")),
        "{error}"
    );
}

#[cfg(unix)]
#[test]
fn a_swapped_binary_and_a_rewritten_receipt_are_both_refused() {
    let staged = staged_release();
    let state = staged.state.path();
    relax(&staged.release);

    let binary = staged.release.join("bin/hvp-mcp");
    fs::write(&binary, "a different program\n").unwrap();
    let swapped = runtime::verify_release(&staged.release, state, &staged.surface, None)
        .expect_err("a swapped binary must not serve");
    assert!(
        matches!(&swapped, RuntimeError::Verification(reason) if reason.contains("bin/hvp-mcp")),
        "{swapped}"
    );

    let staged = staged_release();
    let state = staged.state.path();
    relax(&staged.release);
    let receipt_path = staged.release.join("receipt.json");
    let mut receipt: Value = serde_json::from_slice(&fs::read(&receipt_path).unwrap()).unwrap();
    receipt["source"]["commit"] = json!("9999999999999999999999999999999999999999");
    fs::write(&receipt_path, serde_json::to_vec(&receipt).unwrap()).unwrap();
    let rewritten = runtime::verify_release(&staged.release, state, &staged.surface, None)
        .expect_err("a rewritten receipt must not serve");
    assert!(
        matches!(&rewritten, RuntimeError::Verification(reason) if reason.contains("receipt digest")),
        "{rewritten}"
    );
}

#[cfg(unix)]
#[test]
fn rollback_is_refused_below_the_security_capability_floor() {
    let staged = staged_release();
    let state = staged.state.path();

    // Selecting the release we just published is fine, and it is what installs
    // the one stable launcher every client registers.
    runtime::select(state, &staged.runtime_id, &staged.surface, &staged.launcher).unwrap();
    assert_eq!(
        fs::read(&staged.launcher).unwrap(),
        fs::read(repo_root().join("scripts/video-studio-mcp")).unwrap()
    );

    let mut capabilities = source_fingerprint::manifest().security_capabilities;
    capabilities.push("wave3_upload_fence.v1".to_owned());
    record_floor(state, &capabilities, &staged.runtime_id);

    let installed = fs::read(&staged.launcher).unwrap();
    let active = active_selection_bytes(state);
    let refused = runtime::select(state, &staged.runtime_id, &staged.surface, &staged.launcher)
        .expect_err("a runtime below the floor must not be selectable");
    assert!(
        matches!(&refused, RuntimeError::Refused(reason) if reason.contains("wave3_upload_fence.v1")),
        "{refused}"
    );
    assert_eq!(fs::read(&staged.launcher).unwrap(), installed);
    assert_eq!(active_selection_bytes(state), active);

    // And a process that somehow starts on it serves reads without mutations.
    let authority =
        RuntimeAuthority::test_promoted(&repo_root(), state.to_path_buf(), mcp::tool_surface())
            .unwrap();
    assert!(!authority.mutations_allowed());
    let blocked = authority.mutation_block(Path::new("/tmp/project")).unwrap();
    assert_eq!(blocked.code, "runtime_capability_floor");
    assert_eq!(
        blocked.data.as_ref().unwrap().pointer("/floor/state"),
        Some(&json!("recorded"))
    );
    assert_eq!(
        blocked.data.as_ref().unwrap().pointer("/floor/missing"),
        Some(&json!(["wave3_upload_fence.v1"]))
    );
}

#[cfg(unix)]
#[test]
fn installation_never_overwrites_an_unrelated_existing_launcher() {
    let staged = staged_release();
    fs::create_dir_all(staged.launcher.parent().unwrap()).unwrap();
    let existing = b"#!/bin/sh\necho independently-managed-launcher\n";
    fs::write(&staged.launcher, existing).unwrap();
    let active = active_selection_bytes(staged.state.path());

    let refused = runtime::select(
        staged.state.path(),
        &staged.runtime_id,
        &staged.surface,
        &staged.launcher,
    )
    .expect_err("an unrelated launcher must never be overwritten");
    assert!(
        matches!(&refused, RuntimeError::Refused(reason) if reason.contains("stable launcher differs")),
        "{refused}"
    );
    assert_eq!(fs::read(&staged.launcher).unwrap(), existing);
    assert_eq!(active_selection_bytes(staged.state.path()), active);
}

/// Installation has two explicit provenance modes. A self-build never claims
/// to be upstream-reviewed, while upstream promotion requires an operator to
/// name both its URL and branch.
#[test]
fn install_commands_make_provenance_explicit() {
    let help = Command::new(env!("CARGO_BIN_EXE_hvp-runtime"))
        .args(["--help"])
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&help.stdout);
    assert!(text.contains("install-source"), "{text}");
    assert!(text.contains("promote"), "{text}");

    let promote = Command::new(env!("CARGO_BIN_EXE_hvp-runtime"))
        .args(["promote", "--help"])
        .output()
        .unwrap();
    let promote = String::from_utf8_lossy(&promote.stdout);
    assert!(promote.contains("--upstream-url"), "{promote}");
    assert!(promote.contains("--branch"), "{promote}");

    let identity = Command::new(env!("CARGO_BIN_EXE_hvp-runtime"))
        .args([
            "identity",
            "--repo",
            repo_root().to_str().unwrap(),
            "--commit",
            "1111111111111111111111111111111111111111",
            "--tree",
            "2222222222222222222222222222222222222222",
            "--provenance",
            "self-build",
        ])
        .output()
        .unwrap();
    assert!(identity.status.success(), "{:?}", identity.stderr);
    let identity: Value = serde_json::from_slice(&identity.stdout).unwrap();
    assert_eq!(identity["provenance"], json!("self_build"));

    let source = protected_repository();
    let repo = source.path();
    let state = tempdir().unwrap();
    assert!(
        Command::new("git")
            .args([
                "config",
                "--local",
                "url.file:///tmp/attacker.insteadOf",
                repo.to_str().unwrap(),
            ])
            .current_dir(repo)
            .status()
            .unwrap()
            .success()
    );
    let refused = promotion_error(repo, state.path());
    assert!(
        matches!(&refused, RuntimeError::Refused(reason) if reason.contains("URL rewrites")),
        "{refused}"
    );
    assert!(!state.path().join("active").exists());
    assert!(!state.path().join("bin/video-studio-mcp").exists());
}

/// Blocker 3: a runtime ID is a content address or it is nothing. Nothing that
/// fails the grammar is ever joined onto the releases directory.
#[cfg(unix)]
#[test]
fn rollback_refuses_every_runtime_id_that_is_not_a_content_address() {
    let staged = staged_release();
    let state = staged.state.path();
    let active = active_selection_bytes(state);

    for hostile in [
        "../../../../etc",
        "sha256:../../../../etc/passwd",
        "sha256:..",
        "..",
        "/etc/passwd",
        "sha256:/etc/passwd",
        "sha256:releases/../../secrets",
        // Right length, wrong alphabet: uppercase hex and a non-hex letter.
        "sha256:AAAA111111111111111111111111111111111111111111111111111111111111",
        "sha256:zzzz111111111111111111111111111111111111111111111111111111111111",
        // Right alphabet, wrong length.
        "sha256:abc123",
        "",
        "sha256:",
    ] {
        let refused = runtime::select(state, hostile, &staged.surface, &staged.launcher)
            .expect_err("a non-canonical runtime id must never be joined onto a path");
        assert!(
            matches!(&refused, RuntimeError::Refused(reason)
                if reason.contains("64 lowercase hex digits")),
            "{hostile:?} was refused for the wrong reason: {refused}"
        );
        assert!(
            !staged.launcher.exists(),
            "{hostile:?} reached the launcher install"
        );
        assert_eq!(active_selection_bytes(state), active);
    }

    // A well-formed id for a release that was never promoted is refused too,
    // and is refused as a missing release rather than as bad input.
    let absent = format!("sha256:{}", "b".repeat(64));
    let refused = runtime::select(state, &absent, &staged.surface, &staged.launcher)
        .expect_err("an unpromoted runtime id must not be selectable");
    assert!(
        matches!(&refused, RuntimeError::Verification(reason) if reason.contains("release root is unreadable")),
        "{refused}"
    );
    assert_eq!(active_selection_bytes(state), active);
}

/// Blocker 2: the candidate is proven before anything points at it, so a
/// rollback that fails verification changes neither the active selection nor
/// the installed launcher.
#[cfg(unix)]
#[test]
fn a_rollback_to_an_unverifiable_release_changes_no_live_state() {
    let staged = staged_release();
    let state = staged.state.path();

    // The fixture published and activated the release but installed no
    // launcher, so an absent launcher afterwards proves `install_launcher`
    // never ran and a byte-identical selection proves `activate` never did.
    assert!(!staged.launcher.exists());
    let active = active_selection_bytes(state);

    relax(&staged.release);
    let member = staged.release.join("source/tools/canonical_layout.py");
    let mut contents = fs::read_to_string(&member).unwrap();
    contents.push_str("\n# swapped after promotion\n");
    fs::write(&member, contents).unwrap();

    let refused = runtime::select(state, &staged.runtime_id, &staged.surface, &staged.launcher)
        .expect_err("an unverifiable candidate must not be selectable");
    assert!(
        matches!(&refused, RuntimeError::Verification(reason) if reason.contains("canonical_layout.py")),
        "{refused}"
    );
    assert!(
        !staged.launcher.exists(),
        "a refused rollback installed a launcher"
    );
    assert_eq!(
        active_selection_bytes(state),
        active,
        "a refused rollback rewrote the active selection"
    );

    assert_eq!(
        active_selection(state)["selected_reason"],
        json!("promotion")
    );
}

#[cfg(unix)]
#[test]
fn a_failed_active_generation_commit_keeps_the_old_pointer_and_launcher() {
    use std::os::unix::fs::PermissionsExt;

    let staged = staged_release();
    let state = staged.state.path();
    let active_link = state.join("active");
    let old_target = fs::read_link(&active_link).unwrap();
    let old_selection = active_selection_bytes(state);
    let old_floor = fs::read(state.join("active/capability-floor.json")).unwrap();
    fs::create_dir_all(staged.launcher.parent().unwrap()).unwrap();
    fs::write(
        &staged.launcher,
        fs::read(staged.release.join("source/scripts/video-studio-mcp")).unwrap(),
    )
    .unwrap();
    let old_launcher = fs::read(&staged.launcher).unwrap();

    let selections = state.join("selections");
    fs::set_permissions(&selections, fs::Permissions::from_mode(0o555)).unwrap();
    let refused = runtime::select(state, &staged.runtime_id, &staged.surface, &staged.launcher)
        .expect_err("an unwritable selection store must fail before the active pointer moves");
    fs::set_permissions(&selections, fs::Permissions::from_mode(0o755)).unwrap();

    assert!(matches!(refused, RuntimeError::Io(_)), "{refused}");
    assert_eq!(fs::read_link(active_link).unwrap(), old_target);
    assert_eq!(active_selection_bytes(state), old_selection);
    assert_eq!(
        fs::read(state.join("active/capability-floor.json")).unwrap(),
        old_floor,
        "a failed generation commit changed the capability floor"
    );
    assert_eq!(fs::read(&staged.launcher).unwrap(), old_launcher);
}

/// Blocker 4: the capability floor is tri-state and fallible. Absence is only
/// ever the first-promotion bootstrap; anything unreadable fails closed.
#[cfg(unix)]
#[test]
fn a_floor_that_cannot_be_read_is_never_read_as_no_floor() {
    let staged = staged_release();
    let state = staged.state.path();
    let floor_path = state.join("active/capability-floor.json");

    assert!(matches!(
        runtime::read_floor(state).unwrap(),
        runtime::FloorState::Recorded(_)
    ));

    // Deleting the floor does not hand back an unconstrained runtime: the only
    // moment absence is allowed is the first promotion.
    fs::remove_file(&floor_path).unwrap();
    assert_eq!(
        runtime::read_floor(state).unwrap(),
        runtime::FloorState::Absent
    );
    let refused = runtime::select(state, &staged.runtime_id, &staged.surface, &staged.launcher)
        .expect_err("a rollback cannot run without a recorded floor");
    assert!(
        matches!(&refused, RuntimeError::Refused(reason) if reason.contains("no security capability floor is recorded")),
        "{refused}"
    );
    assert!(!staged.launcher.exists());

    for (label, bytes) in [
        ("truncated", "{\"schema\":\"haru.runtime_capabil".to_owned()),
        ("not json", "capabilities: everything\n".to_owned()),
        (
            "wrong schema",
            json!({
                "schema": "haru.runtime_capability_floor.v2",
                "capabilities": [],
                "runtime_id": staged.runtime_id,
                "updated_at": 1_785_254_400_u64,
            })
            .to_string(),
        ),
        (
            "wrong shape",
            json!({
                "schema": "haru.runtime_capability_floor.v1",
                "capabilities": "everything",
                "runtime_id": staged.runtime_id,
                "updated_at": 1_785_254_400_u64,
            })
            .to_string(),
        ),
    ] {
        fs::write(&floor_path, &bytes).unwrap();
        let error = runtime::read_floor(state)
            .expect_err(&format!("a {label} floor must not read as a usable floor"));
        assert!(
            matches!(&error, RuntimeError::Verification(_)),
            "{label}: {error}"
        );

        let refused = runtime::select(state, &staged.runtime_id, &staged.surface, &staged.launcher)
            .expect_err("a rollback cannot run against an unreadable floor");
        assert!(
            matches!(&refused, RuntimeError::Verification(reason) if reason.contains("capability floor")),
            "{label}: {refused}"
        );
        assert!(!staged.launcher.exists(), "{label} reached the launcher");

        // A served process on the same state reports no mutation authority and
        // says why, instead of quietly deciding there is nothing to satisfy.
        let authority =
            RuntimeAuthority::test_promoted(&repo_root(), state.to_path_buf(), mcp::tool_surface())
                .unwrap();
        assert!(!authority.mutations_allowed(), "{label}");
        let blocked = authority.mutation_block(Path::new("/tmp/project")).unwrap();
        assert_eq!(blocked.code, "runtime_capability_floor");
        assert_eq!(
            blocked.data.as_ref().unwrap().pointer("/floor/state"),
            Some(&json!("unusable")),
            "{label}"
        );
    }
}

/// Blocker 4, the other half: absence is the first-promotion bootstrap and
/// nothing else. Once releases exist, a deleted floor is a refusal.
#[test]
fn a_deleted_floor_cannot_re_enter_the_first_promotion_bootstrap() {
    let source = protected_repository();
    let state = tempdir().unwrap();
    fs::create_dir_all(state.path().join("releases")).unwrap();

    let refused = promotion_error(source.path(), state.path());
    assert!(
        matches!(&refused, RuntimeError::Refused(reason)
            if reason.contains("first-promotion bootstrap has already happened")),
        "{refused}"
    );

    // The same refusal is reached before any build work, so a malformed floor
    // never gets as far as producing bytes either.
    fs::create_dir(state.path().join("active")).unwrap();
    fs::write(
        state.path().join("active/capability-floor.json"),
        "{\"schema\": \"haru.runtime_capability_floor.v1\", \"capabilities\":",
    )
    .unwrap();
    let malformed = promotion_error(source.path(), state.path());
    assert!(
        matches!(&malformed, RuntimeError::Verification(reason)
            if reason.contains("security capability floor is malformed")),
        "{malformed}"
    );
}

#[cfg(unix)]
#[test]
fn an_active_process_loses_mutation_authority_after_generation_switch() {
    let staged = staged_release();
    let state = staged.state.path();
    let authority =
        RuntimeAuthority::test_promoted(&repo_root(), state.to_path_buf(), mcp::tool_surface())
            .unwrap();
    assert!(
        authority
            .mutation_block(Path::new("/tmp/project"))
            .is_none()
    );

    let active_path = state.join("active/active.json");
    let mut active: Value = serde_json::from_slice(&fs::read(&active_path).unwrap()).unwrap();
    relax(active_path.parent().unwrap());
    active["runtime_id"] = json!(format!("sha256:{}", "f".repeat(64)));
    fs::write(&active_path, serde_json::to_vec(&active).unwrap()).unwrap();

    let blocked = authority.mutation_block(Path::new("/tmp/project")).unwrap();
    assert_eq!(blocked.code, "runtime_not_active");
}

#[cfg(unix)]
#[test]
fn selection_waits_for_an_in_flight_mutation_authority_guard() {
    use std::sync::mpsc;
    use std::time::Duration;

    let staged = staged_release();
    fs::create_dir_all(staged.launcher.parent().unwrap()).unwrap();
    fs::write(
        &staged.launcher,
        fs::read(repo_root().join("scripts/video-studio-mcp")).unwrap(),
    )
    .unwrap();
    let authority = RuntimeAuthority::test_promoted(
        &repo_root(),
        staged.state.path().to_path_buf(),
        mcp::tool_surface(),
    )
    .unwrap();
    let guard = authority.mutation_guard(Path::new("/tmp/project")).unwrap();
    let state = staged.state.path().to_path_buf();
    let runtime_id = staged.runtime_id.clone();
    let surface = staged.surface.clone();
    let launcher = staged.launcher.clone();
    let (sent, received) = mpsc::channel();
    std::thread::spawn(move || {
        sent.send(runtime::select(&state, &runtime_id, &surface, &launcher))
            .unwrap();
    });

    assert!(
        received.recv_timeout(Duration::from_millis(100)).is_err(),
        "selection committed while a mutation still held shared authority"
    );
    drop(guard);
    received
        .recv_timeout(Duration::from_secs(5))
        .expect("selection did not resume after the mutation completed")
        .unwrap();
}
