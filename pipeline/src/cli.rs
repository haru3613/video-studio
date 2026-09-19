use std::ffi::OsString;
use std::io::Read;
use std::path::{Path, PathBuf};

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
    }
}

async fn execute(server: OsString, request: Request) -> Result<Output, CliFailure> {
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
        Request::Call { tool, arguments } => {
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
    }
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
