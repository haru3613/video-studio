use std::path::PathBuf;
use std::time::SystemTime;

use clap::error::ErrorKind;
use clap::{Parser, Subcommand};
use pipeline::ProjectStore;
use pipeline::application;
use serde::Serialize;
use serde_json::Value;

#[derive(Debug, Parser)]
#[command(name = "hvp-state", about = "Repo-owned Haru pipeline state")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    App {
        #[command(subcommand)]
        command: AppCommand,
    },
    Status {
        project: PathBuf,
    },
    ShouldRun {
        project: PathBuf,
        #[arg(long)]
        gate: String,
        #[arg(long)]
        input_digest: String,
    },
}

#[derive(Debug, Subcommand)]
enum AppCommand {
    Create {
        projects_root: PathBuf,
        project: String,
    },
    Select {
        projects_root: PathBuf,
        project: String,
    },
    RecordSelection {
        project: PathBuf,
        #[arg(long)]
        cron_run_id: String,
        #[arg(long)]
        candidate_id: String,
        #[arg(long)]
        chosen_by: String,
        #[arg(long)]
        chosen_at: u64,
    },
    Status {
        project: PathBuf,
    },
    Verify {
        project: PathBuf,
    },
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments: Vec<_> = std::env::args_os().collect();
    let Some(repo_root) = validated_repo_root() else {
        if arguments.get(1).is_some_and(|argument| argument == "app") {
            let result = application::AppResult::runner_unavailable();
            print_json(&result)?;
            std::process::exit(result.exit_code().into());
        }
        eprintln!("hvp-state binary does not match repository source; run scripts/verify");
        std::process::exit(5);
    };
    let cli = match Cli::try_parse_from(&arguments) {
        Ok(cli) => cli,
        Err(error)
            if arguments.get(1).is_some_and(|argument| argument == "app")
                && !matches!(
                    error.kind(),
                    ErrorKind::DisplayHelp | ErrorKind::DisplayVersion
                ) =>
        {
            let result = application::AppResult::invalid_input();
            print_json(&result)?;
            std::process::exit(result.exit_code().into());
        }
        Err(error) => error.exit(),
    };
    match cli.command {
        Command::App { command } => {
            let result = match command {
                AppCommand::Create {
                    projects_root,
                    project,
                } => application::create(&projects_root, &project),
                AppCommand::Select {
                    projects_root,
                    project,
                } => application::select(&projects_root, &project),
                AppCommand::RecordSelection {
                    project,
                    cron_run_id,
                    candidate_id,
                    chosen_by,
                    chosen_at,
                } => application::record_selection(
                    &project,
                    &cron_run_id,
                    &candidate_id,
                    &chosen_by,
                    chosen_at,
                ),
                AppCommand::Status { project } => {
                    let mut executor = application::ProcessExecutor;
                    application::status(&project, &repo_root, &mut executor)
                }
                AppCommand::Verify { project } => {
                    let mut executor = application::ProcessExecutor;
                    application::verify(&project, &repo_root, &mut executor)
                }
            };
            print_json(&result)?;
            std::process::exit(result.exit_code().into());
        }
        Command::Status { project } => {
            let status = canonical_status(&project, &repo_root)?;
            let snapshot =
                ProjectStore::new(project).canonical_snapshot_at(&status, SystemTime::now())?;
            print_json(&snapshot)?;
        }
        Command::ShouldRun {
            project,
            gate,
            input_digest,
        } => {
            let should_run = ProjectStore::new(project).should_run(&gate, &input_digest)?;
            print_json(&serde_json::json!({"gate": gate, "should_run": should_run}))?;
        }
    }
    Ok(())
}

fn validated_repo_root() -> Option<PathBuf> {
    application::validated_repo_root(&std::env::current_exe().ok()?)
}

fn canonical_status(
    project: &std::path::Path,
    repo_root: &std::path::Path,
) -> Result<Value, Box<dyn std::error::Error>> {
    let result = application::status(project, repo_root, &mut application::ProcessExecutor);
    if result.outcome != "ok" {
        return Err(format!("canonical status failed: {}", result.code).into());
    }
    result
        .data
        .ok_or_else(|| "canonical status returned no data".into())
}

fn print_json(value: &impl Serialize) -> Result<(), serde_json::Error> {
    println!("{}", serde_json::to_string_pretty(value)?);
    Ok(())
}
