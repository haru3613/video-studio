use std::ffi::OsString;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command as ProcessCommand, Stdio};

#[cfg(test)]
use clap::CommandFactory;
use clap::{Args, Parser, Subcommand, error::ErrorKind};
use rmcp::{
    RoleClient, ServiceExt, model::CallToolRequestParams, service::RunningService,
    transport::TokioChildProcess,
};
use serde_json::{Map, Value, json};

const SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Parser)]
#[command(
    name = "video-studio",
    version,
    about = "Operate a self-hosted Video Studio workflow through MCP",
    long_about = "Operate a self-hosted Video Studio workflow through MCP. Every workflow action is sent to the configured video-studio-mcp server; this client never invokes renderers directly."
)]
struct Cli {
    /// Emit one compact JSON value on stdout. Diagnostics remain on stderr.
    #[arg(long, global = true)]
    json: bool,

    /// Local MCP server executable. Arguments and shell fragments are not accepted.
    #[arg(
        long,
        global = true,
        env = "VIDEO_STUDIO_MCP_SERVER",
        default_value_os_t = default_server(),
        value_name = "EXECUTABLE"
    )]
    server: OsString,

    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Check the installed runtime, dependencies and configured workspace.
    Doctor,
    /// Initialize or inspect a local workspace.
    Workspace {
        #[command(subcommand)]
        command: WorkspaceCommand,
    },
    /// List projects in an operator-owned workspace.
    List(WorkspaceArgs),
    /// List the exact MCP tool surface exposed by the server.
    Tools,
    /// Show negotiated MCP server capabilities and the exact tool surface.
    Capabilities,
    /// Call any MCP workflow tool with a JSON object read from a file or stdin.
    Call {
        tool: String,
        #[arg(long, value_name = "JSON_FILE_OR_DASH")]
        input: PathBuf,
    },
    /// Create a canonical project.
    Create(CreateArgs),
    /// Select and validate an existing canonical project.
    Select(SelectArgs),
    /// Read canonical project state and blockers.
    Status(ProjectArgs),
    /// Read the canonical artifact index.
    Artifacts(ProjectArgs),
    /// Run the canonical project verifier.
    Verify(ProjectArgs),
    /// Run one allowlisted workflow step while holding a lease.
    Run(RunArgs),
    /// Manage the opaque project lease.
    Lease {
        #[command(subcommand)]
        command: LeaseCommand,
    },
    /// Inspect and control durable render jobs.
    Job {
        #[command(subcommand)]
        command: JobCommand,
    },
    /// Stage and import bounded project inputs.
    Artifact {
        #[command(subcommand)]
        command: ArtifactCommand,
    },
    /// Produce one canonical artifact from an immutable staged input.
    Produce(ProduceArgs),
    /// Read or resolve local creative review feedback.
    Review {
        #[command(subcommand)]
        command: ReviewCommand,
    },
    /// Check or export a local delivery bundle.
    Delivery {
        #[command(subcommand)]
        command: DeliveryCommand,
    },
    /// Start the local review UI from the installed source closure.
    Ui(UiArgs),
    /// Serve an MCP transport from the installed runtime.
    Serve {
        #[command(subcommand)]
        command: ServeCommand,
    },
}

#[derive(Debug, Subcommand)]
enum WorkspaceCommand {
    Init(WorkspaceArgs),
    Info(WorkspaceArgs),
    Backup(WorkspaceBackupArgs),
    Restore(WorkspaceRestoreArgs),
}

#[derive(Debug, Args)]
struct WorkspaceArgs {
    #[arg(long, env = "VIDEO_STUDIO_WORKSPACE", default_value_os_t = default_workspace())]
    workspace: PathBuf,
}

#[derive(Debug, Args)]
struct WorkspaceBackupArgs {
    #[command(flatten)]
    workspace: WorkspaceArgs,
    #[arg(long, value_name = "DIRECTORY")]
    destination: PathBuf,
}

#[derive(Debug, Args)]
struct WorkspaceRestoreArgs {
    #[arg(long, value_name = "BACKUP_DIRECTORY")]
    backup: PathBuf,
    #[arg(long, value_name = "NEW_WORKSPACE")]
    destination: PathBuf,
}

#[derive(Debug, Args)]
struct ProjectArgs {
    #[arg(long, value_name = "PATH")]
    project_root: PathBuf,
}

#[derive(Debug, Args)]
struct CreateArgs {
    #[arg(long, value_name = "PATH")]
    projects_root: PathBuf,
    #[arg(long)]
    project: String,
    #[arg(long)]
    idempotency_key: String,
}

#[derive(Debug, Args)]
struct SelectArgs {
    #[arg(long, value_name = "PATH")]
    projects_root: PathBuf,
    #[arg(long)]
    project: String,
}

#[derive(Debug, Args)]
struct RunArgs {
    #[arg(long, value_name = "PATH")]
    project_root: PathBuf,
    #[arg(long)]
    owner: String,
    #[arg(long)]
    lease_id: String,
    /// Server-defined runner name, such as verify-project or render-project.
    #[arg(long)]
    runner: String,
    /// Optional workflow tools root. This is data for the existing MCP contract,
    /// never an executable or shell command.
    #[arg(long, value_name = "PATH")]
    tools_root: Option<PathBuf>,
    #[arg(long)]
    idempotency_key: String,
}

#[derive(Debug, Subcommand)]
enum LeaseCommand {
    Claim(LeaseClaimArgs),
    Renew(LeaseRenewArgs),
    Status(ProjectArgs),
    Release(LeaseReleaseArgs),
}

#[derive(Debug, Subcommand)]
enum JobCommand {
    Status(JobReadArgs),
    Logs(JobLogsArgs),
    Cancel(JobMutationArgs),
    Resume(JobMutationArgs),
}

#[derive(Debug, Args)]
struct JobReadArgs {
    #[arg(long, value_name = "PATH")]
    project_root: PathBuf,
    #[arg(long, value_parser = parse_job_id)]
    job_id: String,
}

#[derive(Debug, Args)]
struct JobLogsArgs {
    #[command(flatten)]
    job: JobReadArgs,
    #[arg(long, default_value_t = 32 * 1024, value_parser = clap::value_parser!(u32).range(1..=64 * 1024))]
    max_bytes: u32,
}

#[derive(Debug, Args)]
struct JobMutationArgs {
    #[command(flatten)]
    job: JobReadArgs,
    #[arg(long)]
    owner: String,
    #[arg(long)]
    lease_id: String,
    #[arg(long)]
    idempotency_key: String,
}

#[derive(Debug, Subcommand)]
enum ArtifactCommand {
    Stage(ArtifactStageArgs),
    Import(ArtifactImportArgs),
}

#[derive(Debug, Args)]
struct ArtifactStageArgs {
    #[arg(long, value_name = "PATH")]
    project_root: PathBuf,
    #[arg(long)]
    role: String,
    #[arg(
        long,
        value_name = "RELATIVE_PATH",
        required_unless_present_any = ["text", "text_file"],
        conflicts_with_all = ["text", "text_file"]
    )]
    inbox: Option<String>,
    #[arg(
        long,
        required_unless_present_any = ["inbox", "text_file"],
        conflicts_with_all = ["inbox", "text_file"]
    )]
    text: Option<String>,
    /// Read inline text from a local UTF-8 file, or `-` for stdin.
    #[arg(
        long,
        value_name = "TEXT_FILE_OR_DASH",
        required_unless_present_any = ["inbox", "text"],
        conflicts_with_all = ["inbox", "text"]
    )]
    text_file: Option<PathBuf>,
    #[command(flatten)]
    mutation: MutationIdentity,
}

#[derive(Debug, Args)]
struct ArtifactImportArgs {
    #[arg(long, value_name = "PATH")]
    project_root: PathBuf,
    #[arg(long, value_parser = parse_stage_id)]
    stage_id: String,
    #[command(flatten)]
    mutation: MutationIdentity,
}

#[derive(Debug, Args)]
struct ProduceArgs {
    #[arg(long, value_name = "PATH")]
    project_root: PathBuf,
    #[arg(long, value_parser = parse_stage_id)]
    stage_id: String,
    #[arg(long)]
    artifact: String,
    #[arg(long)]
    produced_by: String,
    #[command(flatten)]
    mutation: MutationIdentity,
}

#[derive(Debug, Args)]
struct MutationIdentity {
    #[arg(long)]
    owner: String,
    #[arg(long)]
    lease_id: String,
    #[arg(long)]
    idempotency_key: String,
}

#[derive(Debug, Subcommand)]
enum ReviewCommand {
    List(ProjectArgs),
    Add(ReviewAddArgs),
    Resolve(ReviewResolveArgs),
}

#[derive(Debug, Args)]
struct ReviewAddArgs {
    #[arg(long, value_name = "PATH")]
    project_root: PathBuf,
    #[arg(long, value_parser = parse_uuid)]
    client_id: String,
    #[arg(long, value_parser = parse_sha256, requires = "asset_sha256")]
    package_id: Option<String>,
    #[arg(long)]
    asset_id: String,
    #[arg(long, value_parser = parse_sha256, requires = "package_id")]
    asset_sha256: Option<String>,
    #[arg(long, value_parser = parse_timestamp)]
    timestamp_seconds: Option<f64>,
    #[arg(
        long,
        required_unless_present = "body_file",
        conflicts_with = "body_file"
    )]
    body: Option<String>,
    #[arg(
        long,
        value_name = "TEXT_FILE_OR_DASH",
        required_unless_present = "body",
        conflicts_with = "body"
    )]
    body_file: Option<PathBuf>,
    #[arg(long)]
    idempotency_key: String,
}

#[derive(Debug, Args)]
struct ReviewResolveArgs {
    #[arg(long, value_name = "PATH")]
    project_root: PathBuf,
    #[arg(long)]
    comment_id: String,
    #[arg(long, value_parser = ["resolved", "open"])]
    status: String,
    #[arg(long, value_parser = parse_sha256, requires = "expected_asset_sha256")]
    expected_package_id: Option<String>,
    #[arg(long, value_parser = parse_sha256, requires = "expected_package_id")]
    expected_asset_sha256: Option<String>,
    #[arg(long)]
    idempotency_key: String,
}

#[derive(Debug, Subcommand)]
enum DeliveryCommand {
    Status(ProjectArgs),
    Export(DeliveryExportArgs),
}

#[derive(Debug, Args)]
struct DeliveryExportArgs {
    #[arg(long, value_name = "PATH")]
    project_root: PathBuf,
    /// Include bounded diagnostic logs in the immutable local bundle.
    #[arg(long)]
    diagnostic: bool,
    #[command(flatten)]
    mutation: MutationIdentity,
}

#[derive(Debug, Args)]
struct UiArgs {
    #[command(flatten)]
    workspace: WorkspaceArgs,
    #[arg(long, default_value_t = 8787)]
    port: u16,
}

#[derive(Debug, Subcommand)]
enum ServeCommand {
    Mcp,
    Http {
        #[arg(long, value_name = "TOML")]
        config: PathBuf,
    },
}

#[derive(Debug, Args)]
struct LeaseClaimArgs {
    #[arg(long, value_name = "PATH")]
    project_root: PathBuf,
    #[arg(long)]
    owner: String,
    #[arg(long, default_value_t = 300)]
    ttl_seconds: u64,
    #[arg(long)]
    idempotency_key: String,
}

#[derive(Debug, Args)]
struct LeaseRenewArgs {
    #[arg(long, value_name = "PATH")]
    project_root: PathBuf,
    #[arg(long)]
    owner: String,
    #[arg(long)]
    lease_id: String,
    #[arg(long, default_value_t = 300)]
    ttl_seconds: u64,
    #[arg(long)]
    idempotency_key: String,
}

#[derive(Debug, Args)]
struct LeaseReleaseArgs {
    #[arg(long, value_name = "PATH")]
    project_root: PathBuf,
    #[arg(long)]
    owner: String,
    #[arg(long)]
    lease_id: String,
    #[arg(long)]
    idempotency_key: String,
}

enum Request {
    Tools,
    Capabilities,
    Call {
        tool: String,
        arguments: Map<String, Value>,
    },
    Local(LocalRequest),
    ReviewAddAuto {
        project_root: PathBuf,
        client_id: String,
        asset_id: String,
        timestamp_seconds: Option<f64>,
        body: String,
        idempotency_key: String,
    },
    ReviewResolveAuto {
        project_root: PathBuf,
        comment_id: String,
        status: String,
        idempotency_key: String,
    },
}

enum LocalRequest {
    Doctor,
    WorkspaceInit(PathBuf),
    WorkspaceBackup {
        workspace: PathBuf,
        destination: PathBuf,
    },
    WorkspaceRestore {
        backup: PathBuf,
        destination: PathBuf,
    },
    Ui {
        workspace: PathBuf,
        port: u16,
    },
    ServeMcp,
    ServeHttp {
        config: PathBuf,
    },
}

struct Output {
    value: Value,
    exit_code: u8,
}

struct CliFailure {
    code: &'static str,
    exit_code: u8,
    message: String,
}

impl CliFailure {
    fn invalid(message: impl Into<String>) -> Self {
        Self {
            code: "invalid_input",
            exit_code: 2,
            message: message.into(),
        }
    }

    fn unavailable(message: impl Into<String>) -> Self {
        Self {
            code: "mcp_unavailable",
            exit_code: 5,
            message: message.into(),
        }
    }

    fn protocol(message: impl Into<String>) -> Self {
        Self {
            code: "mcp_request_failed",
            exit_code: 5,
            message: message.into(),
        }
    }

    fn value(&self) -> Value {
        json!({
            "schema_version": SCHEMA_VERSION,
            "outcome": "error",
            "code": self.code,
            "project": Value::Null,
            "data": { "message": self.message },
        })
    }
}

fn default_server() -> OsString {
    std::env::var_os("HOME")
        .map(|home| {
            PathBuf::from(home)
                .join(".local/share/video-studio/bin/video-studio-mcp")
                .into_os_string()
        })
        .unwrap_or_default()
}

fn default_workspace() -> PathBuf {
    std::env::var_os("VIDEO_STUDIO_WORKSPACE")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join("VideoStudio")))
        .unwrap_or_default()
}

fn parse_job_id(value: &str) -> Result<String, String> {
    if value.len() == 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        Ok(value.to_owned())
    } else {
        Err("job id must be 32 lowercase hexadecimal characters".to_owned())
    }
}

fn parse_stage_id(value: &str) -> Result<String, String> {
    parse_job_id(value).map_err(|_| "stage id must be 32 lowercase hexadecimal characters".into())
}

fn parse_sha256(value: &str) -> Result<String, String> {
    if value.len() == 64
        && value
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
    {
        Ok(value.to_owned())
    } else {
        Err("value must be a lowercase SHA-256 digest".to_owned())
    }
}

fn parse_uuid(value: &str) -> Result<String, String> {
    let parsed = uuid::Uuid::parse_str(value).map_err(|_| "client id must be a UUID".to_owned())?;
    let canonical = parsed.to_string();
    if canonical == value {
        Ok(canonical)
    } else {
        Err("client id must be a canonical lowercase UUID".to_owned())
    }
}

fn parse_timestamp(value: &str) -> Result<f64, String> {
    let parsed = value
        .parse::<f64>()
        .map_err(|_| "timestamp must be a finite non-negative number".to_owned())?;
    if parsed.is_finite() && parsed >= 0.0 {
        Ok(parsed)
    } else {
        Err("timestamp must be a finite non-negative number".to_owned())
    }
}

pub async fn entry() -> u8 {
    let arguments = std::env::args_os().collect::<Vec<_>>();
    let json_requested = arguments.iter().any(|argument| argument == "--json");
    let cli = match Cli::try_parse_from(&arguments) {
        Ok(cli) => cli,
        Err(error)
            if matches!(
                error.kind(),
                ErrorKind::DisplayHelp | ErrorKind::DisplayVersion
            ) =>
        {
            let _ = error.print();
            return 0;
        }
        Err(error) => {
            if json_requested {
                let failure = CliFailure::invalid(error.to_string());
                eprintln!("video-studio: {}", failure.message.trim());
                print_value(&failure.value(), true);
            } else {
                let _ = error.print();
            }
            return 2;
        }
    };

    let request = match into_request(cli.command) {
        Ok(request) => request,
        Err(failure) => {
            eprintln!("video-studio: {}", failure.message);
            print_value(&failure.value(), cli.json);
            return failure.exit_code;
        }
    };

    match execute(cli.server, request).await {
        Ok(output) => {
            print_value(&output.value, cli.json);
            output.exit_code
        }
        Err(failure) => {
            eprintln!("video-studio: {}", failure.message);
            print_value(&failure.value(), cli.json);
            failure.exit_code
        }
    }
}

fn into_request(command: Command) -> Result<Request, CliFailure> {
    let call = |tool: &str, value: Value| -> Result<Request, CliFailure> {
        Ok(Request::Call {
            tool: tool.to_owned(),
            arguments: object(value)?,
        })
    };

    match command {
        Command::Doctor => Ok(Request::Local(LocalRequest::Doctor)),
        Command::Workspace { command } => match command {
            WorkspaceCommand::Init(args) => {
                Ok(Request::Local(LocalRequest::WorkspaceInit(args.workspace)))
            }
            WorkspaceCommand::Info(args) => call(
                "workspace_info",
                json!({"schema_version": SCHEMA_VERSION, "workspace_root": path(&args.workspace)}),
            ),
            WorkspaceCommand::Backup(args) => Ok(Request::Local(LocalRequest::WorkspaceBackup {
                workspace: args.workspace.workspace,
                destination: args.destination,
            })),
            WorkspaceCommand::Restore(args) => Ok(Request::Local(LocalRequest::WorkspaceRestore {
                backup: args.backup,
                destination: args.destination,
            })),
        },
        Command::List(args) => call(
            "project_list",
            json!({"schema_version": SCHEMA_VERSION, "workspace_root": path(&args.workspace)}),
        ),
        Command::Tools => Ok(Request::Tools),
        Command::Capabilities => Ok(Request::Capabilities),
        Command::Call { tool, input } => {
            if tool.trim().is_empty() {
                return Err(CliFailure::invalid("tool name must not be empty"));
            }
            let value = read_input(&input)?;
            Ok(Request::Call {
                tool,
                arguments: object(value)?,
            })
        }
        Command::Create(args) => call(
            "create",
            json!({
                "schema_version": SCHEMA_VERSION,
                "projects_root": path(&args.projects_root),
                "project": args.project,
                "idempotency_key": args.idempotency_key,
            }),
        ),
        Command::Select(args) => call(
            "select",
            json!({
                "schema_version": SCHEMA_VERSION,
                "projects_root": path(&args.projects_root),
                "project": args.project,
            }),
        ),
        Command::Status(args) => call("status", project_input(args)),
        Command::Artifacts(args) => call("artifact_index", project_input(args)),
        Command::Verify(args) => call("verify", project_input(args)),
        Command::Run(args) => call(
            "run_next",
            json!({
                "schema_version": SCHEMA_VERSION,
                "project_root": path(&args.project_root),
                "owner": args.owner,
                "lease_id": args.lease_id,
                "runner": args.runner,
                "tools_root": args.tools_root.as_deref().map(path),
                "idempotency_key": args.idempotency_key,
            }),
        ),
        Command::Lease { command } => match command {
            LeaseCommand::Claim(args) => call(
                "lease_claim",
                json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&args.project_root),
                    "owner": args.owner,
                    "ttl_seconds": args.ttl_seconds,
                    "idempotency_key": args.idempotency_key,
                }),
            ),
            LeaseCommand::Renew(args) => call(
                "lease_renew",
                json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&args.project_root),
                    "owner": args.owner,
                    "lease_id": args.lease_id,
                    "ttl_seconds": args.ttl_seconds,
                    "idempotency_key": args.idempotency_key,
                }),
            ),
            LeaseCommand::Status(args) => call("lease_status", project_input(args)),
            LeaseCommand::Release(args) => call(
                "lease_release",
                json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&args.project_root),
                    "owner": args.owner,
                    "lease_id": args.lease_id,
                    "idempotency_key": args.idempotency_key,
                }),
            ),
        },
        Command::Job { command } => match command {
            JobCommand::Status(args) => call(
                "job_status",
                json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&args.project_root),
                    "job_id": args.job_id,
                }),
            ),
            JobCommand::Logs(args) => call(
                "job_logs",
                json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&args.job.project_root),
                    "job_id": args.job.job_id,
                    "max_bytes": args.max_bytes,
                }),
            ),
            JobCommand::Cancel(args) => call(
                "job_cancel",
                json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&args.job.project_root),
                    "job_id": args.job.job_id,
                    "owner": args.owner,
                    "lease_id": args.lease_id,
                    "idempotency_key": args.idempotency_key,
                }),
            ),
            JobCommand::Resume(args) => call(
                "job_resume",
                json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&args.job.project_root),
                    "job_id": args.job.job_id,
                    "owner": args.owner,
                    "lease_id": args.lease_id,
                    "idempotency_key": args.idempotency_key,
                }),
            ),
        },
        Command::Artifact { command } => match command {
            ArtifactCommand::Stage(args) => {
                let inline_text = match (args.text, args.text_file) {
                    (Some(value), None) => Some(value),
                    (None, Some(path)) => Some(read_bounded_text(&path, 1024 * 1024)?),
                    (None, None) => None,
                    _ => return Err(CliFailure::invalid("choose exactly one artifact source")),
                };
                call(
                    "artifact_stage",
                    json!({
                        "schema_version": SCHEMA_VERSION,
                        "project_root": path(&args.project_root),
                        "owner": args.mutation.owner,
                        "lease_id": args.mutation.lease_id,
                        "role": args.role,
                        "inbox_path": args.inbox,
                        "inline_text": inline_text,
                        "idempotency_key": args.mutation.idempotency_key,
                    }),
                )
            }
            ArtifactCommand::Import(args) => call(
                "artifact_import",
                json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&args.project_root),
                    "owner": args.mutation.owner,
                    "lease_id": args.mutation.lease_id,
                    "stage_id": args.stage_id,
                    "idempotency_key": args.mutation.idempotency_key,
                }),
            ),
        },
        Command::Produce(args) => call(
            "produce_staged_artifact",
            json!({
                "schema_version": SCHEMA_VERSION,
                "project_root": path(&args.project_root),
                "owner": args.mutation.owner,
                "lease_id": args.mutation.lease_id,
                "stage_id": args.stage_id,
                "artifact": args.artifact,
                "produced_by": args.produced_by,
                "idempotency_key": args.mutation.idempotency_key,
            }),
        ),
        Command::Review { command } => match command {
            ReviewCommand::List(args) => call("review_feedback", project_input(args)),
            ReviewCommand::Add(args) => {
                let body = match (args.body, args.body_file) {
                    (Some(value), None) => value,
                    (None, Some(path)) => read_bounded_text(&path, 20_000)?,
                    _ => return Err(CliFailure::invalid("choose exactly one review body source")),
                };
                if body.trim().is_empty() || body.chars().count() > 5000 {
                    return Err(CliFailure::invalid(
                        "review body must contain 1 to 5000 characters",
                    ));
                }
                match (args.package_id, args.asset_sha256) {
                    (Some(package_id), Some(asset_sha256)) => call(
                        "review_add",
                        json!({
                            "schema_version": SCHEMA_VERSION,
                            "project_root": path(&args.project_root),
                            "client_id": args.client_id,
                            "package_id": package_id,
                            "asset_id": args.asset_id,
                            "asset_sha256": asset_sha256,
                            "timestamp_seconds": args.timestamp_seconds,
                            "body": body,
                            "idempotency_key": args.idempotency_key,
                        }),
                    ),
                    (None, None) => Ok(Request::ReviewAddAuto {
                        project_root: args.project_root,
                        client_id: args.client_id,
                        asset_id: args.asset_id,
                        timestamp_seconds: args.timestamp_seconds,
                        body,
                        idempotency_key: args.idempotency_key,
                    }),
                    _ => Err(CliFailure::invalid(
                        "package id and asset digest must be supplied together",
                    )),
                }
            }
            ReviewCommand::Resolve(args) => {
                match (args.expected_package_id, args.expected_asset_sha256) {
                    (Some(expected_package_id), Some(expected_asset_sha256)) => call(
                        "review_resolve",
                        json!({
                            "schema_version": SCHEMA_VERSION,
                            "project_root": path(&args.project_root),
                            "comment_id": args.comment_id,
                            "status": args.status,
                            "expected_package_id": expected_package_id,
                            "expected_asset_sha256": expected_asset_sha256,
                            "idempotency_key": args.idempotency_key,
                        }),
                    ),
                    (None, None) => Ok(Request::ReviewResolveAuto {
                        project_root: args.project_root,
                        comment_id: args.comment_id,
                        status: args.status,
                        idempotency_key: args.idempotency_key,
                    }),
                    _ => Err(CliFailure::invalid(
                        "expected package and asset digests must be supplied together",
                    )),
                }
            }
        },
        Command::Delivery { command } => match command {
            DeliveryCommand::Status(args) => call("delivery_status", project_input(args)),
            DeliveryCommand::Export(args) => call(
                "export_delivery",
                json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&args.project_root),
                    "owner": args.mutation.owner,
                    "lease_id": args.mutation.lease_id,
                    "idempotency_key": args.mutation.idempotency_key,
                    "diagnostic": args.diagnostic,
                }),
            ),
        },
        Command::Ui(args) => Ok(Request::Local(LocalRequest::Ui {
            workspace: args.workspace.workspace,
            port: args.port,
        })),
        Command::Serve { command } => match command {
            ServeCommand::Mcp => Ok(Request::Local(LocalRequest::ServeMcp)),
            ServeCommand::Http { config } => Ok(Request::Local(LocalRequest::ServeHttp { config })),
        },
    }
}

async fn execute(server: OsString, request: Request) -> Result<Output, CliFailure> {
    if let Request::Local(local) = request {
        return execute_local(server, local);
    }
    if server.is_empty() {
        return Err(CliFailure::invalid("server executable must not be empty"));
    }
    let command = tokio::process::Command::new(&server);
    let transport = TokioChildProcess::new(command)
        .map_err(|error| CliFailure::unavailable(format!("could not start MCP server: {error}")))?;
    let client = ()
        .serve(transport)
        .await
        .map_err(|error| CliFailure::unavailable(format!("MCP initialization failed: {error}")))?;

    // Keep protocol shutdown on the common path even when a request fails.
    let result = execute_request(&client, request).await;
    if let Err(error) = client.cancel().await {
        eprintln!("video-studio: MCP shutdown warning: {error}");
    }
    result
}

fn execute_local(server: OsString, request: LocalRequest) -> Result<Output, CliFailure> {
    match request {
        LocalRequest::Doctor => doctor(server),
        LocalRequest::WorkspaceInit(workspace) => {
            let source = installed_source_root()?;
            run_json_helper(
                bootstrap_python()?,
                &[
                    OsString::from("-I"),
                    OsString::from("-S"),
                    source.join("tools/workspace.py").into_os_string(),
                    workspace.into_os_string(),
                ],
            )
        }
        LocalRequest::WorkspaceBackup {
            workspace,
            destination,
        } => {
            let source = installed_source_root()?;
            run_json_helper(
                managed_python()?,
                &[
                    OsString::from("-E"),
                    OsString::from("-s"),
                    source.join("tools/workspace_backup.py").into_os_string(),
                    OsString::from("create"),
                    workspace.into_os_string(),
                    destination.into_os_string(),
                ],
            )
        }
        LocalRequest::WorkspaceRestore {
            backup,
            destination,
        } => {
            let source = installed_source_root()?;
            run_json_helper(
                managed_python()?,
                &[
                    OsString::from("-E"),
                    OsString::from("-s"),
                    source.join("tools/workspace_backup.py").into_os_string(),
                    OsString::from("restore"),
                    backup.into_os_string(),
                    destination.into_os_string(),
                ],
            )
        }
        LocalRequest::Ui { workspace, port } => {
            let source = installed_source_root()?;
            exec_local(
                managed_python()?,
                &[
                    OsString::from("-E"),
                    OsString::from("-s"),
                    source.join("tools/dashboard/server.py").into_os_string(),
                    OsString::from("--workspace"),
                    workspace.into_os_string(),
                    OsString::from("--port"),
                    port.to_string().into(),
                ],
                "review UI",
            )
        }
        LocalRequest::ServeMcp => exec_local(server, &[], "MCP server"),
        LocalRequest::ServeHttp { config } => {
            let source = installed_source_root()?;
            exec_local(
                managed_python()?,
                &[
                    OsString::from("-E"),
                    OsString::from("-s"),
                    source.join("tools/http_mcp.py").into_os_string(),
                    OsString::from("--config"),
                    config.into_os_string(),
                ],
                "HTTP MCP server",
            )
        }
    }
}

fn installed_source_root() -> Result<PathBuf, CliFailure> {
    if let Some(value) = std::env::var_os("VIDEO_STUDIO_SOURCE_ROOT") {
        return direct_local_directory(PathBuf::from(value), "configured source root");
    }
    let executable = std::env::current_exe()
        .map_err(|error| CliFailure::unavailable(format!("cannot resolve CLI: {error}")))?;
    if let Some(release) = executable.parent().and_then(Path::parent) {
        let source = release.join("source");
        if source.join("pyproject.toml").is_file() {
            return direct_local_directory(source, "installed source root");
        }
    }
    let development = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| CliFailure::unavailable("development source root is unavailable"))?;
    direct_local_directory(development, "development source root")
}

fn managed_python() -> Result<OsString, CliFailure> {
    let candidate = std::env::var_os("VIDEO_STUDIO_PYTHON")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME")
                .map(|home| PathBuf::from(home).join(".local/share/video-studio/python/bin/python"))
        })
        .ok_or_else(|| CliFailure::unavailable("managed Python is not configured"))?;
    direct_local_executable(candidate, "managed Python").map(PathBuf::into_os_string)
}

fn bootstrap_python() -> Result<OsString, CliFailure> {
    let candidate = std::env::var_os("VIDEO_STUDIO_BOOTSTRAP_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/usr/bin/python3"));
    direct_local_executable(candidate, "bootstrap Python").map(PathBuf::into_os_string)
}

fn direct_local_directory(path: PathBuf, label: &str) -> Result<PathBuf, CliFailure> {
    if !path.is_absolute() || path.is_symlink() || !path.is_dir() {
        return Err(CliFailure::unavailable(format!("{label} is unavailable")));
    }
    path.canonicalize()
        .map_err(|error| CliFailure::unavailable(format!("cannot resolve {label}: {error}")))
}

fn direct_local_executable(path: PathBuf, label: &str) -> Result<PathBuf, CliFailure> {
    if !path.is_absolute() || !path.is_file() {
        return Err(CliFailure::unavailable(format!("{label} is unavailable")));
    }
    let resolved = path
        .canonicalize()
        .map_err(|error| CliFailure::unavailable(format!("cannot resolve {label}: {error}")))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if fs::metadata(&resolved)
            .map_err(|error| CliFailure::unavailable(format!("cannot inspect {label}: {error}")))?
            .permissions()
            .mode()
            & 0o111
            == 0
        {
            return Err(CliFailure::unavailable(format!(
                "{label} is not executable"
            )));
        }
    }
    // Keep the configured path rather than the canonical target: a venv's
    // python is commonly a symlink, and executing its resolved base interpreter
    // would discard pyvenv.cfg and the managed dependencies.
    Ok(path)
}

fn run_json_helper(program: OsString, arguments: &[OsString]) -> Result<Output, CliFailure> {
    let result = ProcessCommand::new(program)
        .args(arguments)
        .stdin(Stdio::null())
        .stderr(Stdio::inherit())
        .stdout(Stdio::piped())
        .output()
        .map_err(|error| CliFailure::unavailable(format!("local helper failed: {error}")))?;
    let value: Value = serde_json::from_slice(&result.stdout).map_err(|error| {
        CliFailure::protocol(format!("local helper returned invalid JSON: {error}"))
    })?;
    let application_exit = result_exit_code(Some(&value), None);
    let process_exit = result
        .status
        .code()
        .and_then(|code| u8::try_from(code).ok())
        .unwrap_or(5);
    Ok(Output {
        exit_code: if process_exit == 0 {
            application_exit
        } else {
            process_exit
        },
        value,
    })
}

#[cfg(unix)]
fn exec_local(
    program: OsString,
    arguments: &[OsString],
    label: &str,
) -> Result<Output, CliFailure> {
    use std::os::unix::process::CommandExt;
    let error = ProcessCommand::new(program).args(arguments).exec();
    Err(CliFailure::unavailable(format!(
        "could not start {label}: {error}"
    )))
}

#[cfg(not(unix))]
fn exec_local(
    program: OsString,
    arguments: &[OsString],
    label: &str,
) -> Result<Output, CliFailure> {
    let status = ProcessCommand::new(program)
        .args(arguments)
        .status()
        .map_err(|error| CliFailure::unavailable(format!("could not start {label}: {error}")))?;
    Err(CliFailure::unavailable(format!(
        "{label} exited with {status}"
    )))
}

fn doctor(server: OsString) -> Result<Output, CliFailure> {
    let source = installed_source_root();
    let managed = managed_python();
    let server_path = PathBuf::from(&server);
    let server_available = !server.is_empty() && server_path.is_file() && !server_path.is_symlink();
    let workspace = default_workspace();
    let workspace_available = workspace.join("workspace.json").is_file()
        && workspace.join("projects").is_dir()
        && !workspace.join("projects").is_symlink();
    let command_check = |name: &str, flag: &str| {
        ["/usr/bin", "/opt/homebrew/bin", "/usr/local/bin"]
            .iter()
            .map(|root| Path::new(root).join(name))
            .find(|candidate| candidate.is_file())
            .is_some_and(|candidate| {
                ProcessCommand::new(candidate)
                    .arg(flag)
                    .stdin(Stdio::null())
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                    .is_ok_and(|status| status.success())
            })
    };
    let checks = json!({
        "installed_source": source.is_ok(),
        "mcp_server": server_available,
        "managed_python": managed.is_ok(),
        "workspace": workspace_available,
        "node": command_check("node", "--version"),
        "ffmpeg": command_check("ffmpeg", "-version"),
        "ffprobe": command_check("ffprobe", "-version"),
    });
    let ready = checks
        .as_object()
        .is_some_and(|values| values.values().all(|value| value.as_bool() == Some(true)));
    Ok(Output {
        value: json!({
            "schema_version": 1,
            "outcome": if ready { "ok" } else { "blocked" },
            "code": if ready { "doctor_ready" } else { "doctor_blocked" },
            "project": Value::Null,
            "data": {
                "checks": checks,
                "provider_credentials_checked": false,
                "publishing_configured": false,
                "source_root": source.ok(),
                "managed_python": managed.ok().map(PathBuf::from),
            },
        }),
        exit_code: if ready { 0 } else { 3 },
    })
}

async fn execute_request(
    client: &RunningService<RoleClient, ()>,
    request: Request,
) -> Result<Output, CliFailure> {
    match request {
        Request::Tools => {
            let tools = client
                .list_all_tools()
                .await
                .map_err(|error| CliFailure::protocol(format!("tools/list failed: {error}")))?;
            Ok(Output {
                value: json!({
                    "schema_version": SCHEMA_VERSION,
                    "outcome": "ok",
                    "code": "tools",
                    "project": Value::Null,
                    "data": { "tools": tools },
                }),
                exit_code: 0,
            })
        }
        Request::Capabilities => {
            let tools = client
                .list_all_tools()
                .await
                .map_err(|error| CliFailure::protocol(format!("tools/list failed: {error}")))?;
            let server = client
                .peer_info()
                .map(|info| serde_json::to_value(info.as_ref()))
                .transpose()
                .map_err(|error| {
                    CliFailure::protocol(format!("could not encode capabilities: {error}"))
                })?;
            Ok(Output {
                value: json!({
                    "schema_version": SCHEMA_VERSION,
                    "outcome": "ok",
                    "code": "capabilities",
                    "project": Value::Null,
                    "data": { "server": server, "tools": tools },
                }),
                exit_code: 0,
            })
        }
        Request::Call { tool, arguments } => call_tool(client, tool, arguments).await,
        Request::ReviewAddAuto {
            project_root,
            client_id,
            asset_id,
            timestamp_seconds,
            body,
            idempotency_key,
        } => {
            let feedback = call_tool(
                client,
                "review_feedback".to_owned(),
                object(json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&project_root),
                }))?,
            )
            .await?;
            if feedback.exit_code != 0 {
                return Ok(feedback);
            }
            let data = feedback
                .value
                .get("data")
                .and_then(Value::as_object)
                .ok_or_else(|| CliFailure::protocol("review feedback has no data"))?;
            let package_id = data
                .get("package_id")
                .and_then(Value::as_str)
                .filter(|value| parse_sha256(value).is_ok())
                .ok_or_else(|| CliFailure::protocol("review feedback has no current package"))?;
            let asset_sha256 = data
                .get("assets")
                .and_then(Value::as_array)
                .and_then(|assets| {
                    assets.iter().find(|asset| {
                        asset.get("id").and_then(Value::as_str) == Some(asset_id.as_str())
                    })
                })
                .and_then(|asset| asset.get("sha256"))
                .and_then(Value::as_str)
                .filter(|value| parse_sha256(value).is_ok())
                .ok_or_else(|| {
                    CliFailure::invalid("asset id is not in the current review package")
                })?;
            call_tool(
                client,
                "review_add".to_owned(),
                object(json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&project_root),
                    "client_id": client_id,
                    "package_id": package_id,
                    "asset_id": asset_id,
                    "asset_sha256": asset_sha256,
                    "timestamp_seconds": timestamp_seconds,
                    "body": body,
                    "idempotency_key": idempotency_key,
                }))?,
            )
            .await
        }
        Request::ReviewResolveAuto {
            project_root,
            comment_id,
            status,
            idempotency_key,
        } => {
            let feedback = call_tool(
                client,
                "review_feedback".to_owned(),
                object(json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&project_root),
                }))?,
            )
            .await?;
            if feedback.exit_code != 0 {
                return Ok(feedback);
            }
            let comment = feedback
                .value
                .pointer("/data/comments")
                .and_then(Value::as_array)
                .and_then(|comments| {
                    comments.iter().find(|comment| {
                        comment.get("id").and_then(Value::as_str) == Some(comment_id.as_str())
                    })
                })
                .ok_or_else(|| CliFailure::invalid("comment id is not in current feedback"))?;
            let expected_package_id = comment
                .get("package_id")
                .and_then(Value::as_str)
                .filter(|value| parse_sha256(value).is_ok())
                .ok_or_else(|| CliFailure::protocol("comment has no package binding"))?;
            let expected_asset_sha256 = comment
                .pointer("/asset/sha256")
                .and_then(Value::as_str)
                .filter(|value| parse_sha256(value).is_ok())
                .ok_or_else(|| CliFailure::protocol("comment has no asset binding"))?;
            call_tool(
                client,
                "review_resolve".to_owned(),
                object(json!({
                    "schema_version": SCHEMA_VERSION,
                    "project_root": path(&project_root),
                    "comment_id": comment_id,
                    "status": status,
                    "expected_package_id": expected_package_id,
                    "expected_asset_sha256": expected_asset_sha256,
                    "idempotency_key": idempotency_key,
                }))?,
            )
            .await
        }
        Request::Local(_) => unreachable!("local requests return before MCP startup"),
    }
}

async fn call_tool(
    client: &RunningService<RoleClient, ()>,
    tool: String,
    arguments: Map<String, Value>,
) -> Result<Output, CliFailure> {
    let result = client
        .call_tool(CallToolRequestParams::new(tool).with_arguments(arguments))
        .await
        .map_err(|error| CliFailure::protocol(format!("tools/call failed: {error}")))?;
    let exit_code = result_exit_code(result.structured_content.as_ref(), result.is_error);
    let value = match result.structured_content {
        Some(value) => value,
        None => json!({
            "schema_version": SCHEMA_VERSION,
            "outcome": if result.is_error == Some(true) { "error" } else { "ok" },
            "code": if result.is_error == Some(true) { "tool_error" } else { "tool_result" },
            "project": Value::Null,
            "data": { "content": result.content, "meta": result.meta },
        }),
    };
    Ok(Output { value, exit_code })
}

fn read_input(input: &Path) -> Result<Value, CliFailure> {
    let mut bytes = Vec::new();
    if input == Path::new("-") {
        std::io::stdin().read_to_end(&mut bytes).map_err(|error| {
            CliFailure::invalid(format!("could not read JSON from stdin: {error}"))
        })?;
    } else {
        bytes = std::fs::read(input)
            .map_err(|error| CliFailure::invalid(format!("could not read input file: {error}")))?;
    }
    serde_json::from_slice(&bytes)
        .map_err(|error| CliFailure::invalid(format!("input is not valid JSON: {error}")))
}

fn read_bounded_text(input: &Path, limit: usize) -> Result<String, CliFailure> {
    let mut bytes = Vec::new();
    if input == Path::new("-") {
        std::io::stdin()
            .take((limit + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|error| {
                CliFailure::invalid(format!("could not read text from stdin: {error}"))
            })?;
    } else {
        let file = fs::File::open(input)
            .map_err(|error| CliFailure::invalid(format!("could not read text file: {error}")))?;
        file.take((limit + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|error| CliFailure::invalid(format!("could not read text file: {error}")))?;
    }
    if bytes.len() > limit {
        return Err(CliFailure::invalid("text exceeds the command limit"));
    }
    String::from_utf8(bytes).map_err(|_| CliFailure::invalid("inline text must be UTF-8"))
}

fn object(value: Value) -> Result<Map<String, Value>, CliFailure> {
    value
        .as_object()
        .cloned()
        .ok_or_else(|| CliFailure::invalid("tool input must be a JSON object"))
}

fn project_input(args: ProjectArgs) -> Value {
    json!({ "schema_version": SCHEMA_VERSION, "project_root": path(&args.project_root) })
}

fn path(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

fn result_exit_code(value: Option<&Value>, is_error: Option<bool>) -> u8 {
    let Some(value) = value else {
        return if is_error == Some(true) { 5 } else { 0 };
    };
    let outcome = value.get("outcome").and_then(Value::as_str);
    let code = value.get("code").and_then(Value::as_str);
    match (outcome, code) {
        (Some("ok"), _) => 0,
        (Some("error"), Some("invalid_input")) => 2,
        (Some("blocked"), _) => 3,
        (Some("error"), Some("command_failed")) => 4,
        (Some("error"), _) => 5,
        _ if is_error == Some(true) => 5,
        _ => 5,
    }
}

fn print_value(value: &Value, compact: bool) {
    let serialized = if compact {
        serde_json::to_string(value)
    } else {
        serde_json::to_string_pretty(value)
    };
    match serialized {
        Ok(serialized) => println!("{serialized}"),
        Err(error) => eprintln!("video-studio: could not encode output: {error}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn application_outcomes_map_to_stable_process_exit_codes() {
        assert_eq!(
            result_exit_code(Some(&json!({"outcome":"ok","code":"status"})), None),
            0
        );
        assert_eq!(
            result_exit_code(
                Some(&json!({"outcome":"error","code":"invalid_input"})),
                None
            ),
            2
        );
        assert_eq!(
            result_exit_code(
                Some(&json!({"outcome":"blocked","code":"gate_blocked"})),
                None
            ),
            3
        );
        assert_eq!(
            result_exit_code(
                Some(&json!({"outcome":"error","code":"command_failed"})),
                None
            ),
            4
        );
        assert_eq!(
            result_exit_code(
                Some(&json!({"outcome":"error","code":"internal_error"})),
                None
            ),
            5
        );
    }

    #[test]
    fn non_object_tool_input_is_rejected_before_server_start() {
        assert!(object(json!([])).is_err());
        assert!(object(json!({"schema_version": 1})).is_ok());
    }

    #[test]
    fn clap_surface_keeps_generic_and_ergonomic_entry_points() {
        Cli::command().debug_assert();
    }
}
