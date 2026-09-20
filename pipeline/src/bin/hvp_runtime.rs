//! The runtime authority command: identity, promotion, rollback, launch-time
//! verification and the operational production-upload hold.
//!
//! This is the only writer of `~/.local/state/video-studio/runtime`. It is
//! deliberately not an MCP tool: promoting bytes and lifting an upload hold are
//! operator actions, not something an agent session can reach.

use std::path::PathBuf;

use clap::{Parser, Subcommand, ValueEnum};
use pipeline::mcp;
use pipeline::runtime::{self, PromotionMode, PromotionRequest, RuntimeError, RuntimeProvenance};

#[derive(Debug, Parser)]
#[command(name = "hvp-runtime", about = "Video Studio runtime authority")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Print the identity of a source tree as this binary computes it.
    Identity {
        #[arg(long)]
        repo: Option<PathBuf>,
        #[arg(long)]
        commit: Option<String>,
        #[arg(long)]
        tree: Option<String>,
        #[arg(long, value_enum, default_value_t = ProvenanceArg::SelfBuild)]
        provenance: ProvenanceArg,
    },
    /// Verify a promoted release against its receipt and this binary.
    Verify {
        #[arg(long)]
        release: Option<PathBuf>,
        /// Verify immutable candidate bytes without consulting live selection.
        #[arg(long)]
        candidate: bool,
    },
    /// Print the active selection.
    Active,
    /// Build a configured trusted-upstream commit and make it active.
    Promote {
        #[arg(long)]
        repo: Option<PathBuf>,
        #[arg(long)]
        upstream_url: String,
        #[arg(long)]
        branch: String,
        /// Promote a runtime that drops a capability the floor requires.
        #[arg(long)]
        allow_capability_drop: bool,
    },
    /// Build clean committed local source into an immutable self-built runtime.
    InstallSource {
        #[arg(long)]
        repo: Option<PathBuf>,
        /// Install a runtime that drops a capability the floor requires.
        #[arg(long)]
        allow_capability_drop: bool,
    },
    /// Roll back to a previously promoted, still-compatible runtime. The id is
    /// a content address: `sha256:` and 64 lowercase hex digits, nothing else.
    Select { runtime_id: String },
    /// Read or change the operational production-upload hold.
    Hold {
        #[command(subcommand)]
        action: HoldAction,
    },
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum ProvenanceArg {
    SelfBuild,
    TrustedUpstream,
}

impl From<ProvenanceArg> for RuntimeProvenance {
    fn from(value: ProvenanceArg) -> Self {
        match value {
            ProvenanceArg::SelfBuild => Self::SelfBuild,
            ProvenanceArg::TrustedUpstream => Self::TrustedUpstream,
        }
    }
}

#[derive(Debug, Subcommand)]
enum HoldAction {
    Status,
    Engage {
        #[arg(long)]
        reason: String,
        #[arg(long)]
        by: String,
    },
    Lift {
        #[arg(long)]
        reason: String,
        #[arg(long)]
        by: String,
    },
}

fn main() -> std::process::ExitCode {
    match run(Cli::parse()) {
        Ok(output) => {
            println!("{output}");
            std::process::ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("hvp-runtime: {error}");
            std::process::ExitCode::from(5)
        }
    }
}

fn run(cli: Cli) -> Result<String, RuntimeError> {
    let state_root = runtime::state_root();
    let surface = mcp::tool_surface();
    match cli.command {
        Command::Identity {
            repo,
            commit,
            tree,
            provenance,
        } => {
            let repo = match repo {
                Some(repo) => repo,
                None => current_repo_root()?,
            };
            let identity = runtime::RuntimeIdentity::compute_with_provenance(
                &repo,
                &surface,
                commit.as_deref(),
                tree.as_deref(),
                provenance.into(),
            )?;
            Ok(serde_json::to_string(&identity)?)
        }
        Command::Verify { release, candidate } => {
            let release =
                release.unwrap_or_else(|| state_root.join(runtime::ACTIVE_LINK).join("release"));
            let identity = if candidate {
                runtime::verify_release_candidate(&release, &surface, None)?
                    .receipt
                    .identity
            } else {
                runtime::verify_release(&release, &state_root, &surface, None)?
                    .receipt
                    .identity
            };
            Ok(serde_json::to_string(&identity)?)
        }
        Command::Active => {
            let path = state_root
                .join(runtime::ACTIVE_LINK)
                .join(runtime::ACTIVE_FILE);
            std::fs::read_to_string(&path)
                .map(|value| value.trim().to_owned())
                .map_err(|_| RuntimeError::NoActiveRuntime)
        }
        Command::Promote {
            repo,
            upstream_url,
            branch,
            allow_capability_drop,
        } => {
            let repo = match repo {
                Some(repo) => repo,
                None => current_repo_root()?,
            };
            let receipt = runtime::promote(
                &PromotionRequest {
                    repo_root: repo,
                    state_root,
                    launcher_path: runtime::launcher_install_path(),
                    allow_capability_drop,
                    source: PromotionMode::TrustedUpstream {
                        url: upstream_url,
                        branch,
                    },
                },
                &surface,
            )?;
            Ok(serde_json::to_string(&receipt)?)
        }
        Command::InstallSource {
            repo,
            allow_capability_drop,
        } => {
            let repo = match repo {
                Some(repo) => repo,
                None => current_repo_root()?,
            };
            let receipt = runtime::promote(
                &PromotionRequest {
                    repo_root: repo,
                    state_root,
                    launcher_path: runtime::launcher_install_path(),
                    allow_capability_drop,
                    source: PromotionMode::SelfBuild,
                },
                &surface,
            )?;
            Ok(serde_json::to_string(&receipt)?)
        }
        Command::Select { runtime_id } => {
            let receipt = runtime::select(
                &state_root,
                &runtime_id,
                &surface,
                &runtime::launcher_install_path(),
            )?;
            Ok(serde_json::to_string(&receipt)?)
        }
        Command::Hold { action } => {
            let hold = match action {
                HoldAction::Status => runtime::upload_hold(&state_root),
                HoldAction::Engage { reason, by } => {
                    runtime::set_upload_hold(&state_root, true, &reason, &by)?
                }
                HoldAction::Lift { reason, by } => {
                    runtime::set_upload_hold(&state_root, false, &reason, &by)?
                }
            };
            Ok(serde_json::to_string(&hold)?)
        }
    }
}

/// The repository this binary is allowed to speak for: the mirrored source of
/// the release it lives in, or the checkout it was built from.
fn current_repo_root() -> Result<PathBuf, RuntimeError> {
    let binary = std::env::current_exe().map_err(|error| {
        RuntimeError::Verification(format!("cannot locate this binary: {error}"))
    })?;
    match runtime::Layout::detect(&binary) {
        Some(runtime::Layout::Release { repo_root, .. }) => Ok(repo_root),
        Some(runtime::Layout::Development { repo_root }) => Ok(repo_root),
        None => Err(RuntimeError::Verification(
            "this binary is not inside a promoted release or a checkout".to_owned(),
        )),
    }
}
