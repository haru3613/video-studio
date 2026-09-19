use std::collections::BTreeMap;
use std::ffi::OsString;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::SystemTime;

use fs2::FileExt;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::runtime::RuntimeBinding;
use crate::store::write_json_atomically;
use crate::{ProjectStore, StoreError};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
pub struct AppResult {
    pub schema_version: u32,
    pub outcome: String,
    pub code: String,
    pub project: Option<String>,
    #[schemars(schema_with = "json_value_schema")]
    pub data: Option<Value>,
    /// The runtime that produced this result. Attached at the MCP boundary so
    /// a client can prove which promoted bytes answered it; absent only for
    /// direct in-process and CLI calls, which already know their own binary.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    #[schemars(schema_with = "json_value_schema")]
    pub runtime: Option<Value>,
}

fn json_value_schema(_: &mut schemars::SchemaGenerator) -> schemars::Schema {
    schemars::json_schema!({
        "type": ["array", "boolean", "null", "number", "object", "string"]
    })
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
pub struct SelectionReceipt {
    pub schema_version: u32,
    pub cron_run_id: String,
    pub candidate_id: String,
    pub chosen_by: String,
    pub chosen_at: u64,
    pub project_slug: String,
}

impl AppResult {
    pub(crate) fn ok(code: &str, project: &Path, data: Option<Value>) -> Self {
        Self {
            schema_version: 1,
            outcome: "ok".to_owned(),
            code: code.to_owned(),
            project: Some(project.to_string_lossy().into_owned()),
            data,
            runtime: None,
        }
    }

    pub fn blocked(code: &str, project: &Path) -> Self {
        Self::blocked_with_data(code, project, None)
    }

    pub fn blocked_with_data(code: &str, project: &Path, data: Option<Value>) -> Self {
        Self {
            schema_version: 1,
            outcome: "blocked".to_owned(),
            code: code.to_owned(),
            project: Some(project.to_string_lossy().into_owned()),
            data,
            runtime: None,
        }
    }

    pub(crate) fn error(code: &str, project: Option<&Path>) -> Self {
        Self::error_with_data(code, project, None)
    }

    pub(crate) fn error_with_data(code: &str, project: Option<&Path>, data: Option<Value>) -> Self {
        Self {
            schema_version: 1,
            outcome: "error".to_owned(),
            code: code.to_owned(),
            project: project.map(|path| path.to_string_lossy().into_owned()),
            data,
            runtime: None,
        }
    }

    pub fn invalid_input() -> Self {
        Self {
            schema_version: 1,
            outcome: "error".to_owned(),
            code: "invalid_input".to_owned(),
            project: None,
            data: None,
            runtime: None,
        }
    }

    pub fn runner_unavailable() -> Self {
        Self::error("runner_unavailable", None)
    }

    pub fn internal_error(project: Option<&Path>) -> Self {
        Self::error("internal_error", project)
    }

    pub fn idempotency_conflict(project: Option<&Path>) -> Self {
        Self::error("idempotency_conflict", project)
    }

    pub fn exit_code(&self) -> u8 {
        match (self.outcome.as_str(), self.code.as_str()) {
            ("ok", _) => 0,
            ("error", "invalid_input") => 2,
            ("blocked", _) => 3,
            ("error", "command_failed") => 4,
            _ => 5,
        }
    }
}

/// Mirrors `tools/canonical_layout.py::TODO_MARKER`. A freshly created project
/// carries the runtime compatibility block immediately -- it is never inferred
/// later -- while the lane fields stay marked so the scaffold, the checklist
/// and the gates all still report the selection as outstanding.
pub const SCAFFOLD_MARKER: &str = "HVP_TODO_REPLACE_ME";

pub fn create(projects_root: &Path, slug: &str) -> AppResult {
    let Ok(root) = neutral_root(projects_root) else {
        return AppResult::invalid_input();
    };
    if !valid_slug(slug) {
        return AppResult::invalid_input();
    }
    let runtime_contract = crate::runtime::project_runtime_contract_value();
    match crate::scaffold::create_project(&root, slug, runtime_contract.clone()) {
        Ok((project, written)) => AppResult::ok(
            "project_created",
            &project,
            Some(serde_json::json!({
                "runtime_contract": runtime_contract,
                "scaffold": written,
                "next_action": {
                    "command": "scripts/hvp-scaffold",
                    "arguments": ["scaffold", project],
                    "reason": "canonical_scaffold_required",
                },
            })),
        ),
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => AppResult::invalid_input(),
        Err(_) => AppResult::error("internal_error", Some(&root.join(slug))),
    }
}

/// What a project says about the runtime contracts it was built against.
/// `project-contract.json.runtime_contract` names evaluator/artifact/runtime
/// contract versions -- never a build fingerprint -- so a compatible runtime
/// can serve a project promoted by a different one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProjectRuntimeContract {
    /// The path is not a project directory at all. The compatibility authority
    /// says nothing about it; the ordinary invalid-input path owns that case.
    Unresolved,
    Missing,
    Malformed,
    Declared {
        runtime: String,
        evaluator: String,
        artifact: String,
    },
}

pub fn read_project_runtime_contract(project_root: &Path) -> ProjectRuntimeContract {
    let Ok(project) = direct_directory(project_root) else {
        return ProjectRuntimeContract::Unresolved;
    };
    let path = project.join("project-contract.json");
    if fs::symlink_metadata(&path).is_err_and(|error| error.kind() == io::ErrorKind::NotFound) {
        return ProjectRuntimeContract::Missing;
    }
    let Ok(path) = direct_file(&path) else {
        return ProjectRuntimeContract::Malformed;
    };
    let Ok(bytes) = fs::read(path) else {
        return ProjectRuntimeContract::Malformed;
    };
    let Ok(Value::Object(contract)) = serde_json::from_slice::<Value>(&bytes) else {
        return ProjectRuntimeContract::Malformed;
    };
    let Some(Value::Object(declared)) = contract.get("runtime_contract") else {
        return ProjectRuntimeContract::Missing;
    };
    if declared.get("schema").and_then(Value::as_str)
        != Some(crate::runtime::PROJECT_CONTRACT_SCHEMA)
    {
        return ProjectRuntimeContract::Malformed;
    }
    let field = |name: &str| {
        declared
            .get(name)
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .map(str::to_owned)
    };
    match (field("runtime"), field("evaluator"), field("artifact")) {
        (Some(runtime), Some(evaluator), Some(artifact)) => ProjectRuntimeContract::Declared {
            runtime,
            evaluator,
            artifact,
        },
        _ => ProjectRuntimeContract::Malformed,
    }
}

pub fn select(projects_root: &Path, slug: &str) -> AppResult {
    let Ok(root) = neutral_root(projects_root) else {
        return AppResult::invalid_input();
    };
    if !valid_slug(slug) {
        return AppResult::invalid_input();
    }
    let project = root.join(slug);
    let Ok(project) = direct_directory(&project) else {
        return AppResult::invalid_input();
    };
    AppResult::ok("project_selected", &project, None)
}

fn workspace_manifest(root: &Path) -> io::Result<(PathBuf, Value)> {
    let root = direct_directory(root)?;
    let manifest_path = direct_file(&root.join("workspace.json"))?;
    let manifest: Value = serde_json::from_slice(&fs::read(manifest_path)?).map_err(|error| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("workspace manifest: {error}"),
        )
    })?;
    if manifest.get("schema").and_then(Value::as_str) != Some("video_studio.workspace.v1")
        || manifest
            .get("workspace_id")
            .and_then(Value::as_str)
            .and_then(|value| uuid::Uuid::parse_str(value).ok())
            .is_none()
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid workspace manifest",
        ));
    }
    let projects = direct_directory(&root.join("projects"))?;
    if projects.parent() != Some(root.as_path()) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid projects root",
        ));
    }
    Ok((root, manifest))
}

fn workspace_projects(root: &Path) -> io::Result<Vec<Value>> {
    let mut projects = Vec::new();
    let projects_root = root.join("projects");
    for entry in fs::read_dir(&projects_root)? {
        let entry = entry?;
        let metadata = entry.file_type()?;
        let Some(project_id) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        if metadata.is_symlink() || !metadata.is_dir() || !valid_slug(&project_id) {
            continue;
        }
        let project = entry.path().canonicalize()?;
        if project.parent() != Some(projects_root.as_path()) {
            continue;
        }
        let contract = project.join("project-contract.json");
        projects.push(serde_json::json!({
            "project_id": project_id,
            "initialized": contract.is_file() && !contract.is_symlink(),
        }));
    }
    projects.sort_by(|left, right| {
        left["project_id"]
            .as_str()
            .cmp(&right["project_id"].as_str())
    });
    Ok(projects)
}

pub fn workspace_info(workspace_root: &Path) -> AppResult {
    let Ok((root, manifest)) = workspace_manifest(workspace_root) else {
        return AppResult::invalid_input();
    };
    let Ok(projects) = workspace_projects(&root) else {
        return AppResult::error("internal_error", Some(&root));
    };
    AppResult::ok(
        "workspace_info",
        &root,
        Some(serde_json::json!({
            "schema": "video_studio.workspace_info.v1",
            "workspace_id": manifest["workspace_id"],
            "project_count": projects.len(),
        })),
    )
}

pub fn project_list(workspace_root: &Path) -> AppResult {
    let Ok((root, manifest)) = workspace_manifest(workspace_root) else {
        return AppResult::invalid_input();
    };
    match workspace_projects(&root) {
        Ok(projects) => AppResult::ok(
            "project_list",
            &root,
            Some(serde_json::json!({
                "schema": "video_studio.project_list.v1",
                "workspace_id": manifest["workspace_id"],
                "projects": projects,
            })),
        ),
        Err(_) => AppResult::error("internal_error", Some(&root)),
    }
}

pub fn record_selection(
    project_root: &Path,
    cron_run_id: &str,
    candidate_id: &str,
    chosen_by: &str,
    chosen_at: u64,
) -> AppResult {
    let Ok(project) = direct_directory(project_root) else {
        return AppResult::invalid_input();
    };
    let Some(project_slug) = project.file_name().and_then(|name| name.to_str()) else {
        return AppResult::invalid_input();
    };
    if !valid_slug(project_slug)
        || !valid_receipt_id(cron_run_id)
        || !valid_receipt_id(candidate_id)
        || !valid_receipt_id(chosen_by)
        || chosen_at == 0
    {
        return AppResult::invalid_input();
    }
    let receipt = SelectionReceipt {
        schema_version: 1,
        cron_run_id: cron_run_id.to_owned(),
        candidate_id: candidate_id.to_owned(),
        chosen_by: chosen_by.to_owned(),
        chosen_at,
        project_slug: project_slug.to_owned(),
    };
    let state = project.join(".hvp");
    if fs::create_dir_all(&state).is_err() {
        return AppResult::internal_error(Some(&project));
    }
    let Ok(lock) = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .open(state.join("selection.lock"))
    else {
        return AppResult::internal_error(Some(&project));
    };
    if lock.lock_exclusive().is_err() {
        return AppResult::internal_error(Some(&project));
    }
    match read_selection(&project) {
        Ok(Some(existing)) if existing == receipt => AppResult::ok(
            "selection_recorded",
            &project,
            serde_json::to_value(existing).ok(),
        ),
        Ok(Some(_)) => AppResult::blocked("selection_conflict", &project),
        Ok(None) => {
            if write_json_atomically(&state, "selection.json", &receipt).is_err() {
                return AppResult::internal_error(Some(&project));
            }
            AppResult::ok(
                "selection_recorded",
                &project,
                serde_json::to_value(receipt).ok(),
            )
        }
        Err(()) => AppResult::internal_error(Some(&project)),
    }
}

pub fn status(
    project_root: &Path,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let result = match execute_verifier(&project, &repo, executor) {
        Ok(result) => result,
        Err(_) => return AppResult::error("runner_unavailable", Some(&project)),
    };
    if !matches!(result.exit_code, Some(0 | 1))
        || !verification_status_valid(&result.data, &project)
    {
        return AppResult::error_with_data("command_failed", Some(&project), result.data);
    }
    let Some(mut data) = result.data else {
        return AppResult::error("command_failed", Some(&project));
    };
    {
        let Some(object) = data.as_object_mut() else {
            return AppResult::error("internal_error", Some(&project));
        };
        object.insert("schema_version".to_owned(), Value::from(1));
    }
    let Ok(snapshot) = ProjectStore::new(&project).canonical_snapshot_at(&data, SystemTime::now())
    else {
        return AppResult::error("internal_error", Some(&project));
    };
    let next_gate = snapshot.next_gate().map(str::to_owned);
    let Some(object) = data.as_object_mut() else {
        return AppResult::error("internal_error", Some(&project));
    };
    object.insert(
        "operations".to_owned(),
        serde_json::json!({
            "lease": snapshot.lease,
            "receipts": snapshot.receipts,
            "provider_jobs": snapshot.provider_jobs,
            "last_successful_gate": snapshot.last_successful_gate,
            "next_gate": next_gate,
        }),
    );
    AppResult::ok("status", &project, Some(data))
}

pub fn artifact_index(
    project_root: &Path,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(project_root) else {
        return AppResult::invalid_input();
    };
    let status = status(&project, repo_root, executor);
    if status.outcome != "ok" {
        return status;
    }
    let Some(gate_truth) = status.data else {
        return AppResult::error("internal_error", Some(&project));
    };
    let mut artifacts = BTreeMap::new();
    if let Some(stages) = gate_truth.get("stages").and_then(Value::as_object) {
        for relative in stages
            .values()
            .filter_map(|stage| stage.get("files").and_then(Value::as_array))
            .flatten()
            .filter_map(Value::as_str)
        {
            let path = project.join(relative);
            if path.is_file() {
                let Ok(artifact) = crate::Artifact::from_path(&project, &path) else {
                    return AppResult::error("internal_error", Some(&project));
                };
                artifacts.insert(artifact.path.clone(), artifact);
            }
        }
    }
    let data = serde_json::json!({
        "schema_version": 1,
        "artifacts": artifacts.into_values().collect::<Vec<_>>(),
        "stages": gate_truth.get("stages"),
        "blocker_details": gate_truth.get("blocker_details"),
        "overall_status": gate_truth.get("overall_status"),
        "segment_mode": gate_truth.get("segment_mode"),
        "segments": gate_truth.get("segments"),
        "next_actionable_segment": gate_truth.get("next_actionable_segment"),
    });
    AppResult::ok("artifact_index", &project, Some(data))
}

fn valid_slug(slug: &str) -> bool {
    let bytes = slug.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 64
        && bytes[0].is_ascii_lowercase()
        && bytes[bytes.len() - 1].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

fn valid_receipt_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}

fn read_selection(project: &Path) -> Result<Option<SelectionReceipt>, ()> {
    match fs::read(project.join(".hvp/selection.json")) {
        Ok(bytes) => {
            let receipt: SelectionReceipt = serde_json::from_slice(&bytes).map_err(|_| ())?;
            if receipt.schema_version != 1 {
                return Err(());
            }
            Ok(Some(receipt))
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(_) => Err(()),
    }
}

fn neutral_root(path: &Path) -> io::Result<PathBuf> {
    direct_directory(path)
}

pub(crate) fn direct_directory(path: &Path) -> io::Result<PathBuf> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid root"));
    }
    path.canonicalize()
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandResult {
    pub exit_code: Option<i32>,
    pub data: Option<Value>,
}

pub trait CommandExecutor {
    fn execute(&mut self, program: &Path, arguments: &[OsString]) -> io::Result<CommandResult>;
}

pub struct ProcessExecutor;

impl CommandExecutor for ProcessExecutor {
    fn execute(&mut self, program: &Path, arguments: &[OsString]) -> io::Result<CommandResult> {
        // Keep execution hermetic while forwarding the two reviewed external
        // authority roots the self-eval engine is explicitly allowed to use.
        // No caller value is turned into argv and no other parent environment
        // (credentials, Python startup hooks, HOME, etc.) crosses the boundary.
        let mut command = Command::new(program);
        command
            .args(arguments)
            .env_clear()
            .env("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        for name in [
            "HARU_VIDEO_STUDIO_SELF_EVAL_STATE_ROOT",
            "HARU_VIDEO_STUDIO_SELF_EVAL_ATTESTATION_ROOT",
        ] {
            if let Some(value) = std::env::var_os(name).filter(|value| !value.is_empty()) {
                command.env(name, value);
            }
        }
        match program.file_name().and_then(|name| name.to_str()) {
            Some("pronunciation-workflow") => {
                let mut configured_python = false;
                for name in [
                    "ELEVENLABS_API_KEY",
                    "ELEVENLABS_API_KEY_PATH",
                    "VIDEO_STUDIO_TTS_VOICE_ID",
                    "VIDEO_STUDIO_TTS_MODEL",
                    "VIDEO_STUDIO_TTS_MAX_CREDITS",
                    "VIDEO_STUDIO_TTS_SPEND_JOURNAL",
                    "VIDEO_STUDIO_TTS_PYTHON",
                    "HARU_TTS_PYTHON",
                    "VIDEO_STUDIO_G2PW_PYTHON",
                    "VIDEO_STUDIO_G2PW_MODEL_DIR",
                    "VIDEO_STUDIO_G2PW_BERT_MODEL",
                    "HARU_G2PW_PYTHON",
                    "HARU_G2PW_MODEL_DIR",
                    "HARU_G2PW_BERT_MODEL",
                    "VIDEO_STUDIO_VOICE_RULES_DIR",
                ] {
                    if let Some(value) = std::env::var_os(name).filter(|value| !value.is_empty()) {
                        if matches!(name, "VIDEO_STUDIO_TTS_PYTHON" | "HARU_TTS_PYTHON") {
                            configured_python = true;
                        }
                        command.env(name, value);
                    }
                }
                if !configured_python
                    && let Some(home) = std::env::var_os("HOME").filter(|value| !value.is_empty())
                {
                    let managed =
                        PathBuf::from(home).join(".local/share/video-studio/python/bin/python");
                    if managed.is_file() {
                        command.env("VIDEO_STUDIO_TTS_PYTHON", managed);
                    }
                }
            }
            Some("generate-cover") => {
                for name in ["VIDEO_STUDIO_COVER_ASSET_DIR", "VIDEO_STUDIO_CHROMIUM"] {
                    if let Some(value) = std::env::var_os(name).filter(|value| !value.is_empty()) {
                        command.env(name, value);
                    }
                }
            }
            Some("render-project" | "render-job") => {
                if let Some(value) =
                    std::env::var_os("VIDEO_STUDIO_CHROMIUM").filter(|value| !value.is_empty())
                {
                    command.env("VIDEO_STUDIO_CHROMIUM", value);
                }
            }
            Some("local-delivery") => {
                if let Some(value) =
                    std::env::var_os("VIDEO_STUDIO_DELIVERY_ROOT").filter(|value| !value.is_empty())
                {
                    command.env("VIDEO_STUDIO_DELIVERY_ROOT", value);
                }
            }
            _ => {}
        }
        let output = command.output()?;
        Ok(CommandResult {
            exit_code: output.status.code(),
            data: serde_json::from_slice(&output.stdout).ok(),
        })
    }
}

fn verification_ready(data: &Option<Value>, project: &Path) -> bool {
    verification_status_valid(data, project)
        && data
            .as_ref()
            .and_then(Value::as_object)
            .is_some_and(|result| {
                matches!(
                    result.get("overall_status").and_then(Value::as_str),
                    Some("ready_for_human_upload_approval" | "publish_approved")
                ) && result
                    .get("blockers")
                    .and_then(Value::as_array)
                    .is_some_and(Vec::is_empty)
                    && result
                        .get("blocker_details")
                        .and_then(Value::as_array)
                        .is_some_and(Vec::is_empty)
            })
}

fn verification_status_valid(data: &Option<Value>, project: &Path) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    result.get("schema").and_then(Value::as_str) == Some("haru.pipeline_status.v1")
        && result.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
}

fn render_ready(data: &Option<Value>, project: &Path) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    let Ok(artifact) = crate::Artifact::from_path(project, &project.join("output/final.mp4"))
    else {
        return false;
    };
    result.get("outcome").and_then(Value::as_str) == Some("ok")
        && result.get("code").and_then(Value::as_str) == Some("render_complete")
        && result.get("project").and_then(Value::as_str) == Some(project.to_string_lossy().as_ref())
        && result
            .get("data")
            .and_then(Value::as_object)
            .is_some_and(|data| {
                data.get("schema").and_then(Value::as_str) == Some("haru.render_result.v1")
                    && data.get("status").and_then(Value::as_str) == Some("render_complete")
                    && data.get("project").and_then(Value::as_str)
                        == project.file_name().and_then(|name| name.to_str())
                    && data.get("output").and_then(Value::as_str) == Some("output/final.mp4")
                    && data
                        .get("video_sha256")
                        .and_then(Value::as_str)
                        .is_some_and(|digest| {
                            digest.len() == 64
                                && digest.bytes().all(|byte| byte.is_ascii_hexdigit())
                                && digest == artifact.sha256
                        })
                    && data
                        .get("bytes")
                        .and_then(Value::as_u64)
                        .is_some_and(|bytes| bytes > 0 && bytes == artifact.bytes)
                    && data
                        .get("loudness_lufs")
                        .and_then(Value::as_f64)
                        .is_some_and(|lufs| (-15.0..=-13.0).contains(&lufs))
                    && data
                        .get("duration_seconds")
                        .and_then(Value::as_f64)
                        .is_some_and(|seconds| seconds > 0.0)
                    && data
                        .get("true_peak_dbfs")
                        .and_then(Value::as_f64)
                        .is_some_and(|peak| peak <= -1.0)
                    && data
                        .get("loudness_range_lu")
                        .and_then(Value::as_f64)
                        .is_some_and(|lra| lra >= 0.0)
                    && data
                        .get("mix")
                        .and_then(Value::as_object)
                        .is_some_and(|mix| {
                            mix.get("schema").and_then(Value::as_str) == Some("haru.final_mix.v1")
                                && mix.get("method").and_then(Value::as_str)
                                    == Some("ffmpeg_loudnorm_two_pass")
                                && mix
                                    .get("normalization_type")
                                    .and_then(Value::as_str)
                                    .is_some_and(|kind| matches!(kind, "linear" | "dynamic"))
                                && mix.get("input_sha256").and_then(Value::as_str).is_some_and(
                                    |digest| {
                                        digest.len() == 64
                                            && digest.bytes().all(|byte| byte.is_ascii_hexdigit())
                                    },
                                )
                                && mix.get("target").and_then(Value::as_object).is_some_and(
                                    |target| {
                                        target.get("integrated_lufs").and_then(Value::as_f64)
                                            == Some(-14.0)
                                            && target.get("true_peak_dbfs").and_then(Value::as_f64)
                                                == Some(-1.0)
                                            && target
                                                .get("loudness_range_lu")
                                                .and_then(Value::as_f64)
                                                .is_some_and(|target_lra| {
                                                    target_lra >= 0.0
                                                        && data
                                                            .get("loudness_range_lu")
                                                            .and_then(Value::as_f64)
                                                            .is_some_and(|measured_lra| {
                                                                target_lra - measured_lra <= 3.0
                                                            })
                                                })
                                    },
                                )
                        })
                    && segmented_assembly_ready(data, project)
            })
}

fn segmented_assembly_ready(data: &serde_json::Map<String, Value>, project: &Path) -> bool {
    match fs::symlink_metadata(project.join("segment-plan.json")) {
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return !data.contains_key("assembly");
        }
        Err(_) => return false,
    }
    let Some(binding) = data.get("assembly").and_then(Value::as_object) else {
        return false;
    };
    let Ok(assembly_artifact) = crate::Artifact::from_path(
        project,
        &project.join(crate::segment_authority::ASSEMBLY_RECEIPT),
    ) else {
        return false;
    };
    if binding.get("schema").and_then(Value::as_str) != Some("haru.segment_assembly.v1")
        || binding.get("path").and_then(Value::as_str)
            != Some(crate::segment_authority::ASSEMBLY_RECEIPT)
        || binding.get("sha256").and_then(Value::as_str) != Some(&assembly_artifact.sha256)
    {
        return false;
    }
    let Some(authority) = crate::segment_authority::SegmentAuthority::load(project) else {
        return false;
    };
    let mix_input_sha256 = data
        .get("mix")
        .and_then(Value::as_object)
        .and_then(|mix| mix.get("input_sha256"))
        .and_then(Value::as_str);
    authority.assembly_current(project, data.get("assembly"), mix_input_sha256)
}

fn render_in_progress(data: &Option<Value>, project: &Path) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    let code = result.get("code").and_then(Value::as_str);
    const JOB_FIELDS: &[&str] = &[
        "epoch",
        "job_id",
        "launcher",
        "log",
        "output",
        "pid",
        "project",
        "revision",
        "schema",
        "started_at",
        "status",
    ];
    result.get("outcome").and_then(Value::as_str) == Some("ok")
        && matches!(code, Some("render_started" | "render_running"))
        && result.get("project").and_then(Value::as_str) == Some(project.to_string_lossy().as_ref())
        && result
            .get("data")
            .and_then(Value::as_object)
            .is_some_and(|data| {
                let preserved = data.get("previous_final_preserved");
                data.keys().all(|key| {
                    JOB_FIELDS.contains(&key.as_str()) || key == "previous_final_preserved"
                }) && preserved.is_none_or(|value| value == &Value::Bool(true))
                    && data.len() == JOB_FIELDS.len() + usize::from(preserved.is_some())
                    && data.get("schema").and_then(Value::as_str) == Some("haru.render_job.v2")
                    && data
                        .get("job_id")
                        .and_then(Value::as_str)
                        .is_some_and(valid_job_id)
                    && data.get("project").and_then(Value::as_str)
                        == project.file_name().and_then(|name| name.to_str())
                    && data
                        .get("status")
                        .and_then(Value::as_str)
                        .is_some_and(|status| {
                            matches!(
                                status,
                                "queued" | "running" | "cancel_requested" | "promoting"
                            )
                        })
                    && data.get("launcher").and_then(Value::as_str) == Some("portable-python")
                    && data
                        .get("epoch")
                        .and_then(Value::as_u64)
                        .is_some_and(|epoch| epoch > 0)
                    && data
                        .get("revision")
                        .and_then(Value::as_str)
                        .is_some_and(is_sha256)
                    && data
                        .get("pid")
                        .is_some_and(|pid| pid.is_null() || pid.as_u64().is_some())
                    && data
                        .get("started_at")
                        .and_then(Value::as_str)
                        .is_some_and(|value| !value.is_empty())
                    && data.get("output").and_then(Value::as_str) == Some("output/final.mp4")
                    && data.get("log").and_then(Value::as_str)
                        == Some(
                            project
                                .join("output/.staging")
                                .join(
                                    data.get("job_id")
                                        .and_then(Value::as_str)
                                        .unwrap_or_default(),
                                )
                                .join("worker.log")
                                .to_string_lossy()
                                .as_ref(),
                        )
            })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LeaseInput {
    pub owner: String,
    pub lease_id: String,
    pub generation: u64,
    pub(crate) capability: String,
}
impl LeaseInput {
    pub fn from_lease(lease: &crate::Lease) -> Self {
        Self {
            owner: lease.owner.clone(),
            lease_id: lease.lease_id.clone(),
            generation: lease.generation,
            capability: lease.token.clone(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunNextRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub runner: String,
    pub tools_root: Option<PathBuf>,
}

/// One reviewer-reported finding. The exact key set the engine's review intent
/// digest covers, so a caller can add nothing to it and omit nothing from it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct SelfEvalFinding {
    pub timestamp_seconds: f64,
    pub boundary_id: String,
    pub category: String,
    pub severity: String,
    pub message: String,
}

/// The typed extras a `render-self-eval-review` submission carries, held as one
/// unit so an action with no business reviewing a self-evaluation is refused
/// rather than quietly ignoring them.
///
/// A caller names no path and no executable anywhere in here: the reviewer
/// strings are provenance metadata, the authority is the protected attestation
/// the engine consumes, and every ref -- including the vision-unavailable
/// receipt a human fallback binds -- is derived by the engine itself.
#[derive(Debug, Clone, PartialEq)]
pub struct SelfEvalReviewInput {
    pub reviewer_kind: String,
    pub provider: String,
    pub model: String,
    pub capability: String,
    pub findings: Vec<SelfEvalFinding>,
    pub attestation_ref: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct VisualQaRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub action: String,
    pub reviewed_by: Option<String>,
    pub verdict: Option<String>,
    pub notes: Option<String>,
    pub self_eval: Option<SelfEvalReviewInput>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PronunciationReviewRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub reviewed_by: String,
    pub verdict: String,
    pub notes: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProduceArtifactRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub artifact: String,
    pub source_file: PathBuf,
    pub produced_by: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ApprovePublishRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub attestation_ref: String,
    pub override_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreparePublishApprovalRequest {
    pub project_root: PathBuf,
    pub override_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PublishRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub idempotency_key: String,
    pub runtime: RuntimeBinding,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplaceThumbnailRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub updated_by: String,
    pub idempotency_key: String,
    pub runtime: RuntimeBinding,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReconcileUploadRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub idempotency_key: String,
    pub override_attestation_ref: Option<String>,
    pub runtime: RuntimeBinding,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ExportDeliveryRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub idempotency_key: String,
    pub diagnostic: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JobMutationRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub job_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReviewResolutionRequest {
    pub project_root: PathBuf,
    pub comment_id: String,
    pub status: String,
    pub expected_package_id: String,
    pub expected_asset_sha256: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ReviewAddRequest {
    pub project_root: PathBuf,
    pub client_id: String,
    pub package_id: String,
    pub asset_id: String,
    pub asset_sha256: String,
    pub timestamp_seconds: Option<f64>,
    pub body: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArtifactStageRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub role: String,
    pub inbox_path: Option<String>,
    pub inline_text: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArtifactImportRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub stage_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProduceStagedArtifactRequest {
    pub project_root: PathBuf,
    pub lease: LeaseInput,
    pub stage_id: String,
    pub artifact: String,
    pub produced_by: String,
}

fn cover_ready(data: &Option<Value>, project: &Path) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    let Ok(artifact) = crate::Artifact::from_path(project, &project.join("output/cover.png"))
    else {
        return false;
    };
    result.get("schema").and_then(Value::as_str) == Some("haru.cover_generation.v1")
        && result.get("ok").and_then(Value::as_bool) == Some(true)
        && result.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && result.get("status").and_then(Value::as_str) == Some("complete")
        && result.get("output").and_then(Value::as_str) == Some("output/cover.png")
        && result.get("output_sha256").and_then(Value::as_str) == Some(&artifact.sha256)
        && result.get("bytes").and_then(Value::as_u64) == Some(artifact.bytes)
        && result.get("width").and_then(Value::as_u64) == Some(1280)
        && result.get("height").and_then(Value::as_u64) == Some(720)
}

fn pronunciation_plan_ready(data: &Option<Value>, project: &Path) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    let Ok(artifact) =
        crate::Artifact::from_path(project, &project.join("pronunciation-plan.json"))
    else {
        return false;
    };
    result.get("schema").and_then(Value::as_str) == Some("haru.pronunciation_analysis.v1")
        && result.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && result.get("status").and_then(Value::as_str) == Some("complete")
        && result.get("output").and_then(Value::as_str) == Some("pronunciation-plan.json")
        && result.get("output_sha256").and_then(Value::as_str) == Some(&artifact.sha256)
}

fn pronunciation_probe_ready(data: &Option<Value>, project: &Path) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    let Ok(plan) = crate::Artifact::from_path(project, &project.join("pronunciation-plan.json"))
    else {
        return false;
    };
    let (Some(credits_spent), Some(max_credits)) = (
        result.get("credits_spent").and_then(Value::as_u64),
        result.get("max_credits").and_then(Value::as_u64),
    ) else {
        return false;
    };
    if result.get("schema").and_then(Value::as_str) != Some("haru.pronunciation_probe.v1")
        || result.get("project").and_then(Value::as_str)
            != project.file_name().and_then(|name| name.to_str())
        || result.get("status").and_then(Value::as_str) != Some("complete")
        || result.get("plan_sha256").and_then(Value::as_str) != Some(&plan.sha256)
        || credits_spent > max_credits
    {
        return false;
    }
    let artifact_matches = |item: &Value| {
        let Some(path) = item.get("path").and_then(Value::as_str) else {
            return false;
        };
        let Ok(artifact) = crate::Artifact::from_path(project, &project.join(path)) else {
            return false;
        };
        artifact.path == path
            && item.get("sha256").and_then(Value::as_str) == Some(&artifact.sha256)
    };
    if result.get("mode").and_then(Value::as_str) == Some("ab")
        && !result.get("g2p_validation").is_some_and(artifact_matches)
    {
        return false;
    }
    result
        .get("audio")
        .and_then(Value::as_array)
        .filter(|audio| !audio.is_empty())
        .is_some_and(|audio| audio.iter().all(artifact_matches))
}

/// The preview is the last thing a human looks at before a full render, so the
/// receipt is believed only once the named bytes are on disk and the cut it
/// claims to show is the cut that exists now.
fn editorial_preview_rendered(data: &Option<Value>, project: &Path) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    let contract = crate::Artifact::from_path(project, &project.join("editorial-contract.json"));
    let preview = crate::Artifact::from_path(
        project,
        &project.join("quality-review/editorial-preview/preview.mp4"),
    );
    result.get("schema").and_then(Value::as_str) == Some("haru.editorial_preview.v1")
        && result.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && result.get("status").and_then(Value::as_str) == Some("complete")
        && contract.is_ok_and(|artifact| {
            result
                .get("editorial_contract_sha256")
                .and_then(Value::as_str)
                == Some(&artifact.sha256)
        })
        && preview.is_ok_and(|artifact| {
            artifact.path == "quality-review/editorial-preview/preview.mp4"
                && result.get("preview_sha256").and_then(Value::as_str) == Some(&artifact.sha256)
        })
}

fn segment_render_ready(data: &Option<Value>, project: &Path) -> bool {
    let Some(result) = data.as_ref() else {
        return false;
    };
    let Some(segment_id) = result.get("segment_id").and_then(Value::as_str) else {
        return false;
    };
    let Some(authority) = crate::segment_authority::SegmentAuthority::load(project) else {
        return false;
    };
    let Some((index, segment)) = authority.binding(segment_id) else {
        return false;
    };
    authority.preceding_approvals_current(project, index)
        && !crate::segment_authority::review_current(project, segment, None, None)
        && crate::segment_authority::render_current(project, segment, Some(result))
}

fn segment_assembly_ready(data: &Option<Value>, project: &Path) -> bool {
    let Some(authority) = crate::segment_authority::SegmentAuthority::load(project) else {
        return false;
    };
    authority.assembly_receipt_current(project, data.as_ref())
}

/// A re-time rewrites the files the render cuts against, so the receipt is
/// believed only once those files are on disk with the digests it states -- and
/// only if it is timed against the narration that is canonical right now, since
/// a re-time from before the last promotion describes the wrong clock entirely.
fn visuals_retimed(data: &Option<Value>, project: &Path) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    let Some(Value::Object(artifacts)) = result.get("artifacts") else {
        return false;
    };
    let canonical_narration =
        crate::Artifact::from_path(project, &project.join("narration-final.mp3"));
    result.get("schema").and_then(Value::as_str) == Some("haru.visual_retime.v1")
        && result.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && result.get("status").and_then(Value::as_str) == Some("complete")
        && canonical_narration.is_ok_and(|narration| {
            result.get("narration_sha256").and_then(Value::as_str) == Some(&narration.sha256)
        })
        // The renderer copies are checked alongside the root ones, because the
        // two drifting apart is the entire hazard this runner exists to close.
        // A project without that directory reports synced: false and is not
        // expected to have them.
        && [
            ("storyboard", "storyboard-final-timed.json"),
            (
                "storyboard_validation",
                "storyboard-final-timed-validation.json",
            ),
            ("editorial_contract", "editorial-contract.json"),
        ]
        .iter()
        .chain(
            if result
                .get("render_inputs")
                .and_then(|inputs| inputs.get("synced"))
                == Some(&Value::Bool(true))
            {
                [
                    (
                        "render_editorial_contract",
                        "remotion/public/data/editorial-contract.json",
                    ),
                    ("render_cues", "remotion/public/data/cues.json"),
                ]
                .iter()
            } else {
                [].iter()
            },
        )
        .all(|(key, name)| {
            let Some(item) = artifacts.get(*key) else {
                return false;
            };
            if item.get("path").and_then(Value::as_str) != Some(*name) {
                return false;
            }
            let Ok(artifact) = crate::Artifact::from_path(project, &project.join(name)) else {
                return false;
            };
            artifact.path == *name
                && item.get("sha256").and_then(Value::as_str) == Some(&artifact.sha256)
        })
}

/// The canonical narration is what the render reads, so the receipt claiming a
/// promotion is only believed once the named bytes are actually on disk under
/// the canonical names with the digests the receipt states.
fn narration_promoted(data: &Option<Value>, project: &Path) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    let Some(Value::Object(artifacts)) = result.get("artifacts") else {
        return false;
    };
    result.get("schema").and_then(Value::as_str) == Some("haru.narration_promotion.v1")
        && result.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && result.get("status").and_then(Value::as_str) == Some("complete")
        && result
            .get("accepted_by")
            .and_then(Value::as_str)
            .is_some_and(|who| !who.trim().is_empty())
        // The audio the human accepted is the audio now standing as canonical.
        && result.get("audio_sha256").and_then(Value::as_str)
            == artifacts
                .get("audio")
                .and_then(|item| item.get("sha256"))
                .and_then(Value::as_str)
        // The bindings only the Python knows how to compute. Their absence is
        // what a hand-assembled receipt looks like.
        && ["candidate_audio", "request_sha256", "narration_receipt_sha256"]
            .iter()
            .all(|field| {
                result
                    .get(*field)
                    .and_then(Value::as_str)
                    .is_some_and(|value| !value.is_empty())
            })
        // Pinned to the canonical names, not merely to some path inside the
        // project: otherwise a receipt pointing at the staged candidate passes
        // while narration-final.mp3 is absent or still the previous take.
        && [
            ("audio", "narration-final.mp3"),
            ("srt", "narration-final.srt"),
            ("pronunciation_stamp", "narration-final.mp3.pron-ok.json"),
        ]
        .iter()
        .all(|(key, name)| {
            let Some(item) = artifacts.get(*key) else {
                return false;
            };
            if item.get("path").and_then(Value::as_str) != Some(*name) {
                return false;
            }
            let Ok(artifact) = crate::Artifact::from_path(project, &project.join(name)) else {
                return false;
            };
            // Artifact::from_path canonicalizes, so a symlinked canonical name
            // pointing at an in-project file would otherwise satisfy this. The
            // Python refuses that before writing, but this gate is documented
            // as an independent confirmation and has to earn it.
            artifact.path == *name
                && item.get("sha256").and_then(Value::as_str) == Some(&artifact.sha256)
        })
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

fn delivery_ref_matches(project: &Path, entry: &Value, expected_path: &str) -> bool {
    if entry.get("path").and_then(Value::as_str) != Some(expected_path) {
        return false;
    }
    let Ok(actual) = crate::Artifact::from_path(project, &project.join(expected_path)) else {
        return false;
    };
    actual.path == expected_path
        && entry.get("sha256").and_then(Value::as_str) == Some(&actual.sha256)
}

fn delivery_request_matches(project: &Path, path: &str, digest: &str) -> bool {
    delivery_ref_matches(
        project,
        &serde_json::json!({"path":path,"sha256":digest}),
        path,
    )
}

// A completed runner is evidence only when its fixed output bytes agree.
fn delivery_outputs_ready(data: &Option<Value>, project: &Path, runner: &str) -> bool {
    let Some(result) = data.as_ref().and_then(Value::as_object) else {
        return false;
    };
    let schema = match runner {
        "derive-narration" => "haru.narration_derivation.v1",
        "promote-derived-narration" => "haru.narration_derivation_promotion.v1",
        "final-quality-review" => "haru.final_quality_review.v1",
        _ => return false,
    };
    if result.get("schema").and_then(Value::as_str) != Some(schema)
        || result.get("status").and_then(Value::as_str) != Some("complete")
        || result.get("project").and_then(Value::as_str)
            != project.file_name().and_then(|name| name.to_str())
    {
        return false;
    }
    let Some(artifacts) = result.get("artifacts").and_then(Value::as_object) else {
        return false;
    };
    let request_sha = result
        .get("request_sha256")
        .and_then(Value::as_str)
        .unwrap_or("");
    let expected: Vec<(&str, String)> = match runner {
        "derive-narration" => {
            if !is_sha256(request_sha)
                || !delivery_request_matches(
                    project,
                    ".hvp/staging/narration-derivation-request.json",
                    request_sha,
                )
                || ![
                    ("audio", "narration-final.mp3"),
                    ("srt", "narration-final.srt"),
                    ("pronunciation_stamp", "narration-final.mp3.pron-ok.json"),
                ]
                .iter()
                .all(|(key, path)| {
                    result
                        .get("source")
                        .and_then(|source| source.get(*key))
                        .is_some_and(|entry| delivery_ref_matches(project, entry, path))
                })
                || result.get("decode_status").and_then(Value::as_str) != Some("pass")
                || !result
                    .get("tempo")
                    .and_then(Value::as_f64)
                    .is_some_and(|v| (0.5..=2.0).contains(&v))
            {
                return false;
            }
            vec![
                (
                    "audio",
                    format!(".hvp/staging/narration-derivations/{request_sha}/narration.mp3"),
                ),
                (
                    "srt",
                    format!(".hvp/staging/narration-derivations/{request_sha}/narration.srt"),
                ),
            ]
        }
        "promote-derived-narration" => {
            if !is_sha256(request_sha)
                || !delivery_request_matches(
                    project,
                    ".hvp/staging/narration-derivation-acceptance.json",
                    request_sha,
                )
                || !result
                    .get("derivation_receipt_sha256")
                    .and_then(Value::as_str)
                    .is_some_and(is_sha256)
                || !result
                    .get("accepted_by")
                    .and_then(Value::as_str)
                    .is_some_and(|v| !v.trim().is_empty())
                || result.get("audio_sha256")
                    != artifacts.get("audio").and_then(|v| v.get("sha256"))
            {
                return false;
            }
            vec![
                ("audio", "narration-final.mp3".into()),
                ("srt", "narration-final.srt".into()),
                (
                    "pronunciation_stamp",
                    "narration-final.mp3.pron-ok.json".into(),
                ),
            ]
        }
        _ => {
            let Ok(video) = crate::Artifact::from_path(project, &project.join("output/final.mp4"))
            else {
                return false;
            };
            if video.path != "output/final.mp4"
                || result.get("video_sha256").and_then(Value::as_str) != Some(&video.sha256)
            {
                return false;
            }
            vec![
                ("prep", "quality-review/final-v1/prep.json".into()),
                ("review", "quality-review/final-v1/review.json".into()),
            ]
        }
    };
    expected.iter().all(|(key, name)| {
        artifacts
            .get(*key)
            .is_some_and(|entry| delivery_ref_matches(project, entry, name))
    })
}

fn narration_candidate_ready(data: &Option<Value>, project: &Path) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    let (Some(credits), Some(max_credits), Some(artifacts)) = (
        result.get("credits_spent").and_then(Value::as_u64),
        result.get("max_credits").and_then(Value::as_u64),
        result.get("artifacts").and_then(Value::as_object),
    ) else {
        return false;
    };
    result.get("schema").and_then(Value::as_str) == Some("haru.narration_generation.v4")
        && result.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && result.get("status").and_then(Value::as_str) == Some("complete")
        && result.get("generation_mode").and_then(Value::as_str) == Some("sectioned")
        && result
            .get("alignment_check")
            .and_then(|check| check.get("status"))
            .and_then(Value::as_str)
            == Some("pass")
        // The SRT pins the cue-driven visual cuts, and eleven_v3's own
        // character alignment was measured to drift several seconds inside a
        // long request while starting and ending in the right place -- which
        // alignment_check above cannot see. A take is only ready to render from
        // if its timeline was re-derived from the audio.
        && result
            .get("srt_alignment")
            .and_then(|check| check.get("source"))
            .and_then(Value::as_str)
            == Some("stt_forced")
        && credits <= max_credits
        && ["audio", "srt", "take", "overrides"].iter().all(|name| {
            let Some(item) = artifacts.get(*name) else {
                return false;
            };
            let Some(path) = item.get("path").and_then(Value::as_str) else {
                return false;
            };
            let Ok(artifact) = crate::Artifact::from_path(project, &project.join(path)) else {
                return false;
            };
            item.get("sha256").and_then(Value::as_str) == Some(&artifact.sha256)
        })
}

pub fn run_next(
    request: &RunNextRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let (program, arguments) = match request.runner.as_str() {
        "verify-project" => (
            repo.join("scripts/verify-project"),
            vec![project.clone().into_os_string()],
        ),
        "render-project" => {
            let Some(tools_root) = &request.tools_root else {
                return AppResult::invalid_input();
            };
            let Ok(tools_root) = direct_directory(tools_root) else {
                return AppResult::invalid_input();
            };
            (
                repo.join("scripts/render-project"),
                vec![
                    project.clone().into_os_string(),
                    tools_root.into_os_string(),
                ],
            )
        }
        "generate-cover" => {
            let Some(tools_root) = &request.tools_root else {
                return AppResult::invalid_input();
            };
            let Ok(tools_root) = direct_directory(tools_root) else {
                return AppResult::invalid_input();
            };
            (
                repo.join("scripts/generate-cover"),
                vec![
                    project.clone().into_os_string(),
                    tools_root.into_os_string(),
                ],
            )
        }
        // Neither promotion nor re-timing needs a tools_root: both work on
        // artifacts already inside the project and call no provider.
        "retime-visuals" => (
            repo.join("scripts/retime-visuals"),
            vec![project.clone().into_os_string()],
        ),
        "editorial-preview" => (
            repo.join("scripts/editorial-preview"),
            vec![OsString::from("render"), project.clone().into_os_string()],
        ),
        "assemble-segments" => (
            repo.join("tools/segment_assembly.py"),
            vec![project.clone().into_os_string()],
        ),
        "render-segment" => (
            repo.join("scripts/render-segment"),
            vec![OsString::from("render"), project.clone().into_os_string()],
        ),
        "derive-narration" | "promote-derived-narration" => (
            repo.join("scripts/derive-narration"),
            vec![
                OsString::from(if request.runner == "derive-narration" {
                    "prepare"
                } else {
                    "promote"
                }),
                project.clone().into_os_string(),
            ],
        ),
        "final-quality-review" => (
            repo.join("scripts/final-quality-review"),
            vec![project.clone().into_os_string()],
        ),
        "promote-narration" => (
            repo.join("scripts/pronunciation-workflow"),
            vec![
                OsString::from("promote-narration"),
                project.clone().into_os_string(),
            ],
        ),
        "analyze-pronunciation" | "confirm-pronunciation" | "generate-narration" => {
            let Some(tools_root) = &request.tools_root else {
                return AppResult::invalid_input();
            };
            let Ok(tools_root) = direct_directory(tools_root) else {
                return AppResult::invalid_input();
            };
            (
                repo.join("scripts/pronunciation-workflow"),
                vec![
                    OsString::from(match request.runner.as_str() {
                        "analyze-pronunciation" => "analyze",
                        "confirm-pronunciation" => "confirm",
                        _ => "generate-narration",
                    }),
                    project.clone().into_os_string(),
                    tools_root.into_os_string(),
                ],
            )
        }
        // The self-eval gate takes no tools root and no caller path at all: the
        // engine lives in the repo and derives every path it touches. A caller
        // that supplies one is refused rather than having it ignored.
        SELF_EVAL_RUNNER => {
            if request.tools_root.is_some() {
                return AppResult::invalid_input();
            }
            (
                repo.join("scripts/render-self-eval"),
                vec![OsString::from("evaluate"), project.clone().into_os_string()],
            )
        }
        _ => return AppResult::blocked("unsupported_gate", &project),
    };
    let Ok(program) = direct_file(&program) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    // Snapshot the projection before the engine runs so reuse can be derived
    // from the bytes rather than taken on the engine's word.
    let self_eval_before = (request.runner == SELF_EVAL_RUNNER)
        .then(|| self_eval_current_bytes(&project))
        .flatten();
    let token = &request.lease.capability;
    let mut executor_failed = false;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || match executor.execute(&program, &arguments) {
            Ok(result) => Ok(result),
            Err(error) => {
                executor_failed = true;
                Err(StoreError::Io(error))
            }
        },
    );
    match execution {
        // The gate reports the state the project actually holds. A zero exit is
        // not enough: the engine's stdout has to be the current projection's
        // exact bytes, and the code is read out of those bytes, so the runner
        // can never announce a state or an ordinal the history does not carry.
        Ok(result) if result.exit_code == Some(0) && request.runner == SELF_EVAL_RUNNER => {
            match self_eval_outcome(&result.data, &project, self_eval_before.as_deref()) {
                Some((code, reused)) => {
                    AppResult::ok(code, &project, self_eval_payload(result.data, reused))
                }
                None => AppResult::error_with_data("command_failed", Some(&project), result.data),
            }
        }
        Ok(result)
            if result.exit_code == Some(0)
                && request.runner == "verify-project"
                && verification_ready(&result.data, &project) =>
        {
            AppResult::ok("gate_completed", &project, result.data)
        }
        Ok(result)
            if result.exit_code == Some(0)
                && request.runner == "render-project"
                && render_ready(&result.data, &project) =>
        {
            AppResult::ok("gate_completed", &project, result.data)
        }
        Ok(result)
            if result.exit_code == Some(0)
                && request.runner == "render-project"
                && render_in_progress(&result.data, &project) =>
        {
            let code = if result
                .data
                .as_ref()
                .and_then(|data| data.get("code"))
                .and_then(Value::as_str)
                == Some("render_started")
            {
                "gate_started"
            } else {
                "gate_pending"
            };
            AppResult::ok(code, &project, result.data)
        }
        Ok(result)
            if result.exit_code == Some(0)
                && request.runner == "generate-cover"
                && cover_ready(&result.data, &project) =>
        {
            AppResult::ok("gate_completed", &project, result.data)
        }
        Ok(result)
            if result.exit_code == Some(0)
                && request.runner == "analyze-pronunciation"
                && pronunciation_plan_ready(&result.data, &project) =>
        {
            AppResult::ok("gate_completed", &project, result.data)
        }
        Ok(result)
            if result.exit_code == Some(0)
                && request.runner == "confirm-pronunciation"
                && pronunciation_probe_ready(&result.data, &project) =>
        {
            AppResult::ok("gate_completed", &project, result.data)
        }
        Ok(result)
            if result.exit_code == Some(0)
                && request.runner == "generate-narration"
                && narration_candidate_ready(&result.data, &project) =>
        {
            AppResult::ok("gate_completed", &project, result.data)
        }
        Ok(result)
            if result.exit_code == Some(0)
                && request.runner == "promote-narration"
                && narration_promoted(&result.data, &project) =>
        {
            AppResult::ok("gate_completed", &project, result.data)
        }
        Ok(result)
            if result.exit_code == Some(0)
                && request.runner == "retime-visuals"
                && visuals_retimed(&result.data, &project) =>
        {
            AppResult::ok("gate_completed", &project, result.data)
        }
        Ok(result)
            if result.exit_code == Some(0)
                && request.runner == "editorial-preview"
                && editorial_preview_rendered(&result.data, &project) =>
        {
            AppResult::ok("gate_completed", &project, result.data)
        }
        Ok(result)
            if result.exit_code == Some(0)
                && request.runner == "render-segment"
                && segment_render_ready(&result.data, &project) =>
        {
            AppResult::ok("gate_completed", &project, result.data)
        }
        Ok(result)
            if result.exit_code == Some(0)
                && request.runner == "assemble-segments"
                && segment_assembly_ready(&result.data, &project) =>
        {
            AppResult::ok("gate_completed", &project, result.data)
        }
        Ok(result)
            if result.exit_code == Some(0)
                && delivery_outputs_ready(&result.data, &project, &request.runner) =>
        {
            AppResult::ok("gate_completed", &project, result.data)
        }
        Ok(result) => AppResult::error_with_data("command_failed", Some(&project), result.data),
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) if executor_failed => {
            AppResult::error("runner_unavailable", Some(&project))
        }
        Err(StoreError::Io(_)) => AppResult::error("internal_error", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

pub fn pronunciation_review(
    request: &PronunciationReviewRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    if request.reviewed_by.trim().is_empty()
        || request.notes.trim().is_empty()
        || !matches!(request.verdict.as_str(), "pass" | "fail")
    {
        return AppResult::invalid_input();
    }
    let Ok(program) = direct_file(&repo.join("scripts/pronunciation-workflow")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let arguments = [
        OsString::from("review"),
        project.clone().into_os_string(),
        OsString::from("--reviewed-by"),
        request.reviewed_by.clone().into(),
        OsString::from("--verdict"),
        request.verdict.clone().into(),
        OsString::from("--notes"),
        request.notes.clone().into(),
    ];
    let token = &request.lease.capability;
    let mut executor_failed = false;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || {
            executor.execute(&program, &arguments).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })
        },
    );
    match execution {
        Ok(result)
            if result.exit_code == Some(0)
                && pronunciation_review_ready(&result.data, &project) =>
        {
            AppResult::ok("pronunciation_review_recorded", &project, result.data)
        }
        Ok(result) => AppResult::error_with_data("command_failed", Some(&project), result.data),
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) if executor_failed => {
            AppResult::error("runner_unavailable", Some(&project))
        }
        Err(StoreError::Io(_)) => AppResult::error("internal_error", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

fn pronunciation_review_ready(data: &Option<Value>, project: &Path) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    if result.get("schema").and_then(Value::as_str) != Some("haru.pronunciation_review.v1")
        || result.get("project").and_then(Value::as_str)
            != project.file_name().and_then(|value| value.to_str())
        || !matches!(
            result.get("verdict").and_then(Value::as_str),
            Some("pass" | "fail")
        )
    {
        return false;
    }
    fs::read(project.join(".hvp/pronunciation-review.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .is_some_and(|stored| stored == *data.as_ref().unwrap())
}

pub fn visual_qa(
    request: &VisualQaRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let (program_name, arguments) = match request.action.as_str() {
        "sample"
            if request.reviewed_by.is_none()
                && request.verdict.is_none()
                && request.notes.is_none()
                && request.self_eval.is_none() =>
        {
            (
                "visual-qa-sample",
                vec![OsString::from("sample"), project.clone().into_os_string()],
            )
        }
        "review" | "segment-review" => {
            let (Some(reviewed_by), Some(verdict), Some(notes)) = (
                request.reviewed_by.as_ref(),
                request.verdict.as_ref(),
                request.notes.as_ref(),
            ) else {
                return AppResult::invalid_input();
            };
            // Sampling and the two human reviews have no business carrying
            // self-eval authority, so the typed extras are refused outright
            // instead of being silently dropped on the way to the script.
            if request.self_eval.is_some() {
                return AppResult::invalid_input();
            }
            let valid_verdict = if request.action == "segment-review" {
                matches!(verdict.as_str(), "pass" | "changes_requested")
            } else {
                matches!(verdict.as_str(), "pass" | "fail")
            };
            if reviewed_by.trim().is_empty() || notes.trim().is_empty() || !valid_verdict {
                return AppResult::invalid_input();
            }
            (
                if request.action == "segment-review" {
                    "render-segment"
                } else {
                    "visual-qa-sample"
                },
                vec![
                    OsString::from("review"),
                    project.clone().into_os_string(),
                    OsString::from("--reviewed-by"),
                    reviewed_by.clone().into(),
                    OsString::from("--verdict"),
                    verdict.clone().into(),
                    OsString::from("--notes"),
                    notes.clone().into(),
                ],
            )
        }
        // Only the repo-owned engine is ever invoked, and the whole review is
        // one canonical argv value plus the protected attestation ref.
        SELF_EVAL_REVIEW_ACTION => {
            let Some(review) = request.self_eval.as_ref() else {
                return AppResult::invalid_input();
            };
            let Some(payload) = self_eval_review_json(request, review) else {
                return AppResult::invalid_input();
            };
            (
                "render-self-eval",
                vec![
                    OsString::from("review"),
                    project.clone().into_os_string(),
                    OsString::from("--review-json"),
                    OsString::from(payload),
                    OsString::from("--attestation-ref"),
                    OsString::from(review.attestation_ref.clone()),
                ],
            )
        }
        _ => return AppResult::invalid_input(),
    };
    let Ok(program) = direct_file(&repo.join("scripts").join(program_name)) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    // Same derivation as the runner: reuse is a fact about the bytes, so an
    // idempotent replay of a review is visible without trusting the engine.
    let self_eval_before = (request.action == SELF_EVAL_REVIEW_ACTION)
        .then(|| self_eval_current_bytes(&project))
        .flatten();
    let token = &request.lease.capability;
    let mut executor_failed = false;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || {
            executor.execute(&program, &arguments).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })
        },
    );
    match execution {
        // A recorded review reports the state the projection now holds -- a
        // sealed pass or fail, or the still-pending state that vision
        // unavailability leaves behind -- and never a state of its own.
        Ok(result) if result.exit_code == Some(0) && request.action == SELF_EVAL_REVIEW_ACTION => {
            match self_eval_outcome(&result.data, &project, self_eval_before.as_deref()) {
                Some((code, reused)) => {
                    AppResult::ok(code, &project, self_eval_payload(result.data, reused))
                }
                None => AppResult::error_with_data("command_failed", Some(&project), result.data),
            }
        }
        Ok(result)
            if result.exit_code == Some(0)
                && visual_qa_receipt_ready(&result.data, &project, &request.action) =>
        {
            AppResult::ok(
                match request.action.as_str() {
                    "sample" => "visual_qa_sampled",
                    "segment-review" => "segment_review_recorded",
                    _ => "visual_qa_recorded",
                },
                &project,
                result.data,
            )
        }
        Ok(result) => AppResult::error_with_data("command_failed", Some(&project), result.data),
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) if executor_failed => {
            AppResult::error("runner_unavailable", Some(&project))
        }
        Err(StoreError::Io(_)) => AppResult::error("internal_error", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

fn segment_review_ready(data: &Option<Value>, project: &Path) -> bool {
    let Some(review) = data.as_ref() else {
        return false;
    };
    let Some(segment_id) = review.get("segment_id").and_then(Value::as_str) else {
        return false;
    };
    let Some(authority) = crate::segment_authority::SegmentAuthority::load(project) else {
        return false;
    };
    let Some((index, segment)) = authority.binding(segment_id) else {
        return false;
    };
    authority.preceding_approvals_current(project, index)
        && crate::segment_authority::review_current(project, segment, Some(review), None)
}

fn visual_qa_receipt_ready(data: &Option<Value>, project: &Path, action: &str) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    let (schema, receipt) = match action {
        "sample" => (
            "haru.visual_qa_sample.v2",
            project.join("quality-review/visual-sampling/visual-qa-sample.json"),
        ),
        "review" => (
            "haru.visual_qa_review.v2",
            project.join("quality-review/visual-sampling/visual-qa-review.json"),
        ),
        "segment-review" => {
            let Some(segment_id) = result.get("segment_id").and_then(Value::as_str) else {
                return false;
            };
            (
                "haru.segment_review.v1",
                project.join(format!("quality-review/segments/{segment_id}/review.json")),
            )
        }
        _ => return false,
    };
    if action == "segment-review" && !segment_review_ready(data, project) {
        return false;
    }
    if result.get("schema").and_then(Value::as_str) != Some(schema)
        || result.get("project").and_then(Value::as_str)
            != project.file_name().and_then(|value| value.to_str())
        || (action == "segment-review"
            && !matches!(
                result.get("verdict").and_then(Value::as_str),
                Some("pass" | "changes_requested")
            ))
    {
        return false;
    }
    let Ok(receipt) = direct_file(&receipt) else {
        return false;
    };
    fs::read(receipt)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .is_some_and(|stored| stored == *data.as_ref().unwrap())
}

/// The `run_next` runner and the `visual_qa` action that drive the render
/// self-evaluation gate. Both are fixed names: neither carries a tools root, a
/// caller-chosen executable, nor any caller-chosen path.
pub const SELF_EVAL_RUNNER: &str = "render-self-eval";
pub const SELF_EVAL_REVIEW_ACTION: &str = "render-self-eval-review";

/// Mirrors the engine's exported `RESULT_PATH`. This file is the only thing
/// that decides what a self-eval call is allowed to report.
pub const SELF_EVAL_RESULT: &str = "quality-review/render-self-eval/render-self-eval.json";
const SELF_EVAL_ATTEMPTS: &str = "quality-review/render-self-eval/attempts";
const SELF_EVAL_SCHEMA: &str = "haru.render_self_eval.v1";
const SELF_EVAL_OUTCOME_SCHEMA: &str = "haru.render_self_eval_outcome.v1";
const SELF_EVAL_HUMAN_CAPABILITY: &str = "human_structural_attestation.v1";
const SELF_EVAL_MAX_ATTEMPTS: u64 = 3;

/// The exact key set of `haru.render_self_eval.v1`. A projection missing a key
/// or carrying an extra one is not the contracted object, so it can never
/// justify a code.
const SELF_EVAL_RESULT_KEYS: [&str; 19] = [
    "attempt",
    "attempt_identity",
    "boundary_plan",
    "boundary_policy",
    "evaluation",
    "evidence_index",
    "findings",
    "inputs",
    "max_attempts",
    "next_action",
    "outcome",
    "project",
    "remediation",
    "review",
    "schema",
    "status",
    "tool",
    "updated_at",
    "verdict",
];

/// The exact bytes of the current projection, or `None` when the project holds
/// no self-eval state at all. Read as a direct project-contained file, so a
/// symlink swapped in over the canonical name is not state.
fn self_eval_current_bytes(project: &Path) -> Option<Vec<u8>> {
    fs::read(direct_file(&project.join(SELF_EVAL_RESULT)).ok()?).ok()
}

/// A `{path,sha256,bytes}` ref is believed only when the project-contained,
/// non-symlink bytes it names are on disk right now with exactly that digest
/// and that length. Returns those bytes so a caller can bind what they name.
fn self_eval_ref_bytes(project: &Path, value: Option<&Value>) -> Option<Vec<u8>> {
    let Some(Value::Object(reference)) = value else {
        return None;
    };
    if reference.len() != 3 {
        return None;
    }
    let relative = reference.get("path").and_then(Value::as_str)?;
    let digest = reference.get("sha256").and_then(Value::as_str)?;
    let bytes = reference.get("bytes").and_then(Value::as_u64)?;
    if digest.len() != 64 || !digest.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    // A ref names a project-relative component path and nothing else, so no
    // absolute path, parent escape or bare `.` can reach the filesystem.
    let relative_path = Path::new(relative);
    if relative_path
        .components()
        .any(|component| !matches!(component, std::path::Component::Normal(_)))
    {
        return None;
    }
    let resolved = direct_file(&project.join(relative_path)).ok()?;
    let artifact = crate::Artifact::from_path(project, &resolved).ok()?;
    if artifact.path != relative || artifact.sha256 != digest || artifact.bytes != bytes {
        return None;
    }
    fs::read(&resolved).ok()
}
fn self_eval_evaluation_has_status(
    project: &Path,
    result: &serde_json::Map<String, Value>,
    attempt: u64,
    expected: &str,
) -> bool {
    let Some(bytes) = self_eval_ref_bytes(project, result.get("evaluation")) else {
        return false;
    };
    let Ok(Value::Object(evaluation)) = serde_json::from_slice::<Value>(&bytes) else {
        return false;
    };
    evaluation.get("schema").and_then(Value::as_str) == Some("haru.render_self_eval_attempt.v1")
        && evaluation.get("attempt").and_then(Value::as_u64) == Some(attempt)
        && evaluation.get("attempt_identity") == result.get("attempt_identity")
        && evaluation.get("status").and_then(Value::as_str) == Some(expected)
}

/// The terminal outcome a projection names has to be sealed bytes that agree
/// with the projection about which attempt, which identity, which evidence and
/// which verdict they seal. A projection cannot assert a seal by itself.
fn self_eval_sealed(
    project: &Path,
    result: &serde_json::Map<String, Value>,
    attempt: u64,
    verdict: &str,
) -> bool {
    let Some(bytes) = self_eval_ref_bytes(project, result.get("outcome")) else {
        return false;
    };
    let Ok(Value::Object(outcome)) = serde_json::from_slice::<Value>(&bytes) else {
        return false;
    };
    let source = outcome.get("source").and_then(Value::as_str);
    let expected_evaluation = match source {
        Some("deterministic") => "fail",
        Some("vision" | "human_fallback") => "clean",
        _ => return false,
    };
    let authority_bound = match source {
        Some("deterministic") => {
            result.get("review") == Some(&Value::Null)
                && outcome.get("authority") == Some(&Value::Null)
        }
        Some("vision" | "human_fallback") => {
            result.get("review").is_some_and(|review| !review.is_null())
                && outcome
                    .get("authority")
                    .is_some_and(|authority| !authority.is_null())
        }
        _ => false,
    };
    outcome.get("schema").and_then(Value::as_str) == Some(SELF_EVAL_OUTCOME_SCHEMA)
        && outcome.get("verdict").and_then(Value::as_str) == Some(verdict)
        && outcome.get("attempt").and_then(Value::as_u64) == Some(attempt)
        && outcome.get("attempt_identity") == result.get("attempt_identity")
        && outcome.get("evaluation") == result.get("evaluation")
        && outcome.get("evidence_index") == result.get("evidence_index")
        && outcome.get("review") == result.get("review")
        && authority_bound
        && self_eval_evaluation_has_status(project, result, attempt, expected_evaluation)
}

/// An exhausted lane is a fact about the immutable history: three sealed
/// attempts on disk and no fourth attempt directory. The projection does not
/// get to claim exhaustion on its own.
fn self_eval_history_exhausted(project: &Path) -> bool {
    let all_failed = (1..=SELF_EVAL_MAX_ATTEMPTS).all(|ordinal| {
        let path = project.join(format!(
            "{SELF_EVAL_ATTEMPTS}/attempt-{ordinal:02}/outcome.json"
        ));
        let Ok(path) = direct_file(&path) else {
            return false;
        };
        let Ok(Value::Object(outcome)) = fs::read(path)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .ok_or(())
        else {
            return false;
        };
        outcome.get("schema").and_then(Value::as_str) == Some(SELF_EVAL_OUTCOME_SCHEMA)
            && outcome.get("attempt").and_then(Value::as_u64) == Some(ordinal)
            && outcome.get("verdict").and_then(Value::as_str) == Some("fail")
    });
    let fourth = project.join(format!(
        "{SELF_EVAL_ATTEMPTS}/attempt-{:02}",
        SELF_EVAL_MAX_ATTEMPTS + 1
    ));
    all_failed
        && matches!(
            fs::symlink_metadata(fourth),
            Err(error) if error.kind() == io::ErrorKind::NotFound
        )
}

/// The one code the exact current projection justifies, read out of `current`
/// -- the bytes that are on disk right now.
///
/// The tool's stdout is never authority on its own: it has to be byte-identical
/// to the current projection, and the code is derived from that projection. So
/// neither the runner nor a review can report a state the project does not
/// actually hold, and a tool that seals attempt 3 cannot report a plain
/// `self_eval_failed`.
fn self_eval_code(data: &Option<Value>, current: &[u8], project: &Path) -> Option<&'static str> {
    let Some(Value::Object(result)) = data else {
        return None;
    };
    if serde_json::from_slice::<Value>(current).ok()?.as_object() != Some(result) {
        return None;
    }
    if result.len() != SELF_EVAL_RESULT_KEYS.len()
        || !SELF_EVAL_RESULT_KEYS
            .iter()
            .all(|key| result.contains_key(*key))
    {
        return None;
    }
    if result.get("schema").and_then(Value::as_str) != Some(SELF_EVAL_SCHEMA)
        || result.get("project").and_then(Value::as_str)
            != project.file_name().and_then(|name| name.to_str())
        || result.get("max_attempts").and_then(Value::as_u64) != Some(SELF_EVAL_MAX_ATTEMPTS)
    {
        return None;
    }
    let status = result.get("status").and_then(Value::as_str)?;
    if result.get("verdict").and_then(Value::as_str) != Some(status) {
        return None;
    }
    let attempt = result.get("attempt").and_then(Value::as_u64)?;
    if attempt == 0 || attempt > SELF_EVAL_MAX_ATTEMPTS {
        return None;
    }
    let identity = result.get("attempt_identity").and_then(Value::as_str)?;
    if identity.len() != 64 || !identity.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    // The refs every projection always carries have to name bytes on disk now.
    for key in [
        "boundary_policy",
        "boundary_plan",
        "evaluation",
        "evidence_index",
    ] {
        self_eval_ref_bytes(project, result.get(key))?;
    }
    // V1 mutates no media, so the remediation block stays empty and null.
    let remediation = result.get("remediation").and_then(Value::as_object)?;
    if remediation.len() != 3
        || !remediation
            .get("allowed_repairs")
            .and_then(Value::as_array)
            .is_some_and(Vec::is_empty)
        || remediation.get("automatic_fix_applied") != Some(&Value::Null)
        || !remediation
            .get("required_action")
            .is_some_and(|action| action.is_string() || action.is_null())
    {
        return None;
    }
    match status {
        // Pending: a clean evaluation is reviewable only while nothing terminal
        // has been sealed over it.
        "needs_human" => (result.get("outcome") == Some(&Value::Null)
            && result.get("review") == Some(&Value::Null)
            && self_eval_evaluation_has_status(project, result, attempt, "clean"))
        .then_some("self_eval_needs_review"),
        "pass" => self_eval_sealed(project, result, attempt, "pass").then_some("self_eval_passed"),
        // Ordinals 1-2 only: a sealed fail at ordinal 3 is the exhausted state.
        "fail" => (attempt < SELF_EVAL_MAX_ATTEMPTS
            && self_eval_sealed(project, result, attempt, "fail"))
        .then_some("self_eval_failed"),
        "human_intervention_required" => (attempt == SELF_EVAL_MAX_ATTEMPTS
            && self_eval_sealed(project, result, attempt, "fail")
            && self_eval_history_exhausted(project))
        .then_some("human_intervention_required"),
        _ => None,
    }
}

/// What this call is allowed to report: the code the current projection
/// justifies, and whether the projection changed at all.
///
/// Reuse is derived, never self-reported. An unchanged call leaves the exact
/// bytes alone and any real transition rewrites them, so a tool can neither
/// claim a fresh attempt it did not seal nor hide one it did.
fn self_eval_outcome(
    data: &Option<Value>,
    project: &Path,
    before: Option<&[u8]>,
) -> Option<(&'static str, bool)> {
    let current = self_eval_current_bytes(project)?;
    let code = self_eval_code(data, &current, project)?;
    Some((code, before == Some(current.as_slice())))
}

/// The payload a self-eval call answers with: the exact current projection, and
/// reuse as metadata beside it. Reuse is never a state or a code of its own.
fn self_eval_payload(result: Option<Value>, reused: bool) -> Option<Value> {
    Some(serde_json::json!({"result": result, "reused": reused}))
}

/// The attestation ref grammar the protected self-eval attestation root uses.
fn valid_self_eval_attestation_ref(value: &str) -> bool {
    const PREFIX: &str = "self-eval-attestation:";
    let Some(suffix) = value.strip_prefix(PREFIX) else {
        return false;
    };
    let mut bytes = suffix.bytes();
    bytes
        .next()
        .is_some_and(|byte| byte.is_ascii_alphanumeric())
        && suffix.len() <= 128
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}
/// The exact `--review-json` argv value: sorted keys, minified, no newline.
///
/// Rust builds it from typed fields, so a caller cannot smuggle in a key the
/// engine would have to reject -- notably not `vision_unavailable`, whose ref
/// the engine derives, nor the nonce and generation it reads only from the
/// protected attestation leaf. Findings are normalised into the contracted
/// stable order rather than trusted to arrive sorted.
fn self_eval_review_json(
    request: &VisualQaRequest,
    review: &SelfEvalReviewInput,
) -> Option<String> {
    let (Some(reviewed_by), Some(verdict), Some(notes)) = (
        request.reviewed_by.as_ref(),
        request.verdict.as_ref(),
        request.notes.as_ref(),
    ) else {
        return None;
    };
    // Reviewer provenance is metadata, but it is recorded provenance, and each
    // reviewer kind carries exactly the provenance it can honestly have: a
    // vision reviewer names its provider, model and capability, while a human
    // structural attestation has no provider or model and names exactly one
    // capability -- which a vision reviewer may never borrow.
    if reviewed_by.trim().is_empty() || !valid_self_eval_attestation_ref(&review.attestation_ref) {
        return None;
    }
    let valid_reviewer = match review.reviewer_kind.as_str() {
        "vision" => {
            matches!(verdict.as_str(), "pass" | "fail" | "unavailable")
                && !review.provider.trim().is_empty()
                && !review.model.trim().is_empty()
                && !review.capability.trim().is_empty()
                && review.capability != SELF_EVAL_HUMAN_CAPABILITY
        }
        "human_fallback" => {
            matches!(verdict.as_str(), "pass" | "fail")
                && review.capability == SELF_EVAL_HUMAN_CAPABILITY
        }
        _ => false,
    };
    // A pass asserts there is nothing to report; a fail has to say what failed;
    // unavailability reports no findings at all because nothing was reviewed.
    let valid_findings = match verdict.as_str() {
        "fail" => !review.findings.is_empty(),
        _ => review.findings.is_empty(),
    };
    if !valid_reviewer || !valid_findings {
        return None;
    }
    let mut findings = Vec::with_capacity(review.findings.len());
    for finding in &review.findings {
        if !finding.timestamp_seconds.is_finite()
            || finding.timestamp_seconds < 0.0
            || finding.boundary_id.trim().is_empty()
            || finding.category.trim().is_empty()
            || finding.severity.trim().is_empty()
            || finding.message.trim().is_empty()
        {
            return None;
        }
        findings.push(finding.clone());
    }
    findings.sort_by(|left, right| {
        left.timestamp_seconds
            .total_cmp(&right.timestamp_seconds)
            .then_with(|| left.boundary_id.cmp(&right.boundary_id))
            .then_with(|| left.category.cmp(&right.category))
            .then_with(|| left.severity.cmp(&right.severity))
            .then_with(|| left.message.cmp(&right.message))
    });
    // Routing every value through `Value` is what makes the bytes canonical:
    // a `serde_json` object is a sorted map, so keys cannot come out in
    // declaration order.
    let payload = serde_json::json!({
        "reviewer_kind": review.reviewer_kind,
        "verdict": verdict,
        "reviewed_by": reviewed_by,
        "provider": review.provider,
        "model": review.model,
        "capability": review.capability,
        "notes": notes,
        "findings": serde_json::to_value(&findings).ok()?,
    });
    serde_json::to_string(&payload).ok()
}

fn valid_stage_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

fn intake_role(value: &str) -> bool {
    matches!(
        value,
        "source_video"
            | "reference_image"
            | "reference_audio"
            | "background_music"
            | "sound_effect"
            | "subtitle"
            | "script_notes"
            | "storyboard_data"
            | "metadata"
    )
}

fn safe_inbox_path(value: &str) -> bool {
    let path = Path::new(value);
    !value.is_empty()
        && value.len() <= 1024
        && !path.is_absolute()
        && path
            .components()
            .all(|component| matches!(component, std::path::Component::Normal(_)))
}

fn inline_intake_request(project: &Path, text: &str) -> io::Result<(String, PathBuf)> {
    let request_id = uuid::Uuid::new_v4().simple().to_string();
    let state = project.join(".hvp");
    let metadata = fs::symlink_metadata(&state)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "unsafe project state",
        ));
    }
    let requests = state.join("intake-requests");
    match fs::symlink_metadata(&requests) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "unsafe intake requests",
            ));
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => fs::create_dir(&requests)?,
        Err(error) => return Err(error),
    }
    #[cfg(unix)]
    fs::set_permissions(&requests, fs::Permissions::from_mode(0o700))?;
    let path = requests.join(format!("{request_id}.txt"));
    match OpenOptions::new().create_new(true).write(true).open(&path) {
        Ok(mut output) => {
            output.write_all(text.as_bytes())?;
            output.sync_all()?;
            #[cfg(unix)]
            fs::set_permissions(&path, fs::Permissions::from_mode(0o400))?;
        }
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            let metadata = fs::symlink_metadata(&path)?;
            if metadata.file_type().is_symlink()
                || !metadata.is_file()
                || fs::read(&path)? != text.as_bytes()
            {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "inline intake request conflicts",
                ));
            }
        }
        Err(error) => return Err(error),
    }
    Ok((request_id, path))
}

fn artifact_stage_valid(data: &Value, project: &Path, role: &str) -> bool {
    let Some(envelope) = data.as_object() else {
        return false;
    };
    let Some(stage) = envelope.get("data").and_then(Value::as_object) else {
        return false;
    };
    envelope.get("schema_version").and_then(Value::as_u64) == Some(1)
        && envelope.get("outcome").and_then(Value::as_str) == Some("ok")
        && envelope.get("code").and_then(Value::as_str) == Some("artifact_staged")
        && envelope.get("project").and_then(Value::as_str)
            == Some(project.to_string_lossy().as_ref())
        && stage.get("schema").and_then(Value::as_str) == Some("video_studio.artifact_stage.v1")
        && stage
            .get("stage_id")
            .and_then(Value::as_str)
            .is_some_and(valid_stage_id)
        && stage
            .get("role")
            .and_then(Value::as_str)
            .is_some_and(intake_role)
        && stage.get("role").and_then(Value::as_str) == Some(role)
        && stage
            .get("extension")
            .and_then(Value::as_str)
            .is_some_and(|value| {
                value.starts_with('.')
                    && value.len() <= 8
                    && value
                        .bytes()
                        .skip(1)
                        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
            })
        && stage
            .get("sha256")
            .and_then(Value::as_str)
            .is_some_and(is_sha256)
        && stage
            .get("bytes")
            .and_then(Value::as_u64)
            .is_some_and(|value| value > 0)
        && stage
            .get("blob")
            .and_then(Value::as_str)
            .is_some_and(|blob| {
                blob == format!(
                    ".hvp/staging/intake/{}/blob",
                    stage
                        .get("stage_id")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                )
            })
}

fn artifact_import_valid(data: &Value, project: &Path, stage_id: &str) -> bool {
    let Some(envelope) = data.as_object() else {
        return false;
    };
    let Some(imported) = envelope.get("data").and_then(Value::as_object) else {
        return false;
    };
    let path = imported
        .get("path")
        .and_then(Value::as_str)
        .unwrap_or_default();
    envelope.get("schema_version").and_then(Value::as_u64) == Some(1)
        && envelope.get("outcome").and_then(Value::as_str) == Some("ok")
        && envelope.get("code").and_then(Value::as_str) == Some("artifact_imported")
        && envelope.get("project").and_then(Value::as_str)
            == Some(project.to_string_lossy().as_ref())
        && imported.get("schema").and_then(Value::as_str) == Some("video_studio.artifact_import.v1")
        && imported.get("stage_id").and_then(Value::as_str) == Some(stage_id)
        && imported
            .get("role")
            .and_then(Value::as_str)
            .is_some_and(intake_role)
        && imported
            .get("sha256")
            .and_then(Value::as_str)
            .is_some_and(is_sha256)
        && imported
            .get("bytes")
            .and_then(Value::as_u64)
            .is_some_and(|value| value > 0)
        && safe_inbox_path(path)
        && path.starts_with("imports/")
}

fn intake_failure(result: CommandResult, project: &Path) -> AppResult {
    let code = result
        .data
        .as_ref()
        .and_then(|value| value.get("code"))
        .and_then(Value::as_str)
        .unwrap_or("command_failed")
        .to_owned();
    if result.exit_code == Some(3)
        && matches!(
            code.as_str(),
            "stage_conflict" | "import_conflict" | "stage_quota_exceeded"
        )
    {
        AppResult::blocked_with_data(&code, project, result.data)
    } else if result.exit_code == Some(2)
        && matches!(
            code.as_str(),
            "invalid_input"
                | "invalid_path"
                | "invalid_workspace"
                | "invalid_project"
                | "invalid_inbox_path"
                | "unsupported_role"
                | "artifact_too_large"
                | "artifact_type_invalid"
                | "inline_role_invalid"
                | "invalid_owner"
                | "invalid_stage_id"
                | "stage_not_found"
                | "stage_invalid"
                | "stage_owner_mismatch"
                | "stage_expired"
                | "invalid_request_id"
                | "inline_request_not_found"
        )
    {
        AppResult::error_with_data(&code, Some(project), result.data)
    } else {
        AppResult::error_with_data("command_failed", Some(project), result.data)
    }
}

pub fn artifact_stage(
    request: &ArtifactStageRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let source_count =
        usize::from(request.inbox_path.is_some()) + usize::from(request.inline_text.is_some());
    if !intake_role(&request.role)
        || source_count != 1
        || request
            .inbox_path
            .as_deref()
            .is_some_and(|value| !safe_inbox_path(value))
        || request
            .inline_text
            .as_deref()
            .is_some_and(|value| value.is_empty() || value.len() > 1024 * 1024)
    {
        return AppResult::invalid_input();
    }
    let Ok(workspace) = project_workspace(&project) else {
        return AppResult::invalid_input();
    };
    let Ok(program) = direct_file(&repo.join("scripts/artifact-intake")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let token = &request.lease.capability;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || {
            let (arguments, request_path) = match (&request.inbox_path, &request.inline_text) {
                (Some(path), None) => (
                    vec![
                        OsString::from("stage-inbox"),
                        workspace.clone().into_os_string(),
                        project.clone().into_os_string(),
                        request.role.clone().into(),
                        path.clone().into(),
                        request.lease.owner.clone().into(),
                    ],
                    None,
                ),
                (None, Some(text)) => {
                    let (request_id, path) =
                        inline_intake_request(&project, text).map_err(StoreError::Io)?;
                    (
                        vec![
                            OsString::from("stage-inline-file"),
                            workspace.clone().into_os_string(),
                            project.clone().into_os_string(),
                            request.role.clone().into(),
                            request_id.into(),
                            request.lease.owner.clone().into(),
                        ],
                        Some(path),
                    )
                }
                _ => {
                    return Err(StoreError::InvalidProviderRequest(
                        "invalid intake source".to_owned(),
                    ));
                }
            };
            let result = executor
                .execute(&program, &arguments)
                .map_err(StoreError::Io);
            if let Some(path) = request_path {
                match fs::remove_file(path) {
                    Ok(()) => {}
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                    Err(error) if result.is_ok() => return Err(StoreError::Io(error)),
                    Err(_) => {}
                }
            }
            result
        },
    );
    match execution {
        Ok(result)
            if result.exit_code == Some(0)
                && artifact_stage_valid(
                    result.data.as_ref().unwrap_or(&Value::Null),
                    &project,
                    &request.role,
                ) =>
        {
            AppResult::ok(
                "artifact_staged",
                &project,
                result.data.and_then(|value| value.get("data").cloned()),
            )
        }
        Ok(result) => intake_failure(result, &project),
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) => AppResult::error("runner_unavailable", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

pub fn artifact_import(
    request: &ArtifactImportRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    if !valid_stage_id(&request.stage_id) {
        return AppResult::invalid_input();
    }
    let Ok(workspace) = project_workspace(&project) else {
        return AppResult::invalid_input();
    };
    let Ok(program) = direct_file(&repo.join("scripts/artifact-intake")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let arguments = [
        OsString::from("import"),
        workspace.into_os_string(),
        project.clone().into_os_string(),
        request.stage_id.clone().into(),
        request.lease.owner.clone().into(),
    ];
    let token = &request.lease.capability;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || {
            executor
                .execute(&program, &arguments)
                .map_err(StoreError::Io)
        },
    );
    match execution {
        Ok(result)
            if result.exit_code == Some(0)
                && artifact_import_valid(
                    result.data.as_ref().unwrap_or(&Value::Null),
                    &project,
                    &request.stage_id,
                ) =>
        {
            AppResult::ok(
                "artifact_imported",
                &project,
                result.data.and_then(|value| value.get("data").cloned()),
            )
        }
        Ok(result) => intake_failure(result, &project),
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) => AppResult::error("runner_unavailable", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

const STAGED_PRODUCE_TARGETS: &[(&str, &[&str])] = &[
    (
        "script_notes",
        &["script-proposal.md", "sources.md", "issue_brief.md"],
    ),
    (
        "storyboard_data",
        &["storyboard-final-timed.json", "editorial-contract.json"],
    ),
    ("metadata", &["claims.json", "publish-metadata.json"]),
];

pub fn produce_staged_artifact(
    request: &ProduceStagedArtifactRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    if !valid_stage_id(&request.stage_id) || request.produced_by.trim().is_empty() {
        return AppResult::invalid_input();
    }
    let Ok(workspace) = project_workspace(&project) else {
        return AppResult::invalid_input();
    };
    let Ok(program) = direct_file(&repo.join("scripts/artifact-intake")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let arguments = [
        OsString::from("resolve"),
        workspace.into_os_string(),
        project.clone().into_os_string(),
        request.stage_id.clone().into(),
        request.lease.owner.clone().into(),
    ];
    let token = &request.lease.capability;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || {
            executor
                .execute(&program, &arguments)
                .map_err(StoreError::Io)
        },
    );
    let resolved = match execution {
        Ok(result)
            if result.exit_code == Some(0)
                && artifact_stage_valid(
                    result.data.as_ref().unwrap_or(&Value::Null),
                    &project,
                    result
                        .data
                        .as_ref()
                        .and_then(|value| value.pointer("/data/role"))
                        .and_then(Value::as_str)
                        .unwrap_or_default(),
                ) =>
        {
            result
        }
        Ok(result) => return intake_failure(result, &project),
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            return AppResult::blocked("lease_invalid", &project);
        }
        Err(StoreError::Io(_)) => {
            return AppResult::error("runner_unavailable", Some(&project));
        }
        Err(_) => return AppResult::error("internal_error", Some(&project)),
    };
    let Some(stage) = resolved.data.and_then(|value| value.get("data").cloned()) else {
        return AppResult::error("command_failed", Some(&project));
    };
    if stage.get("stage_id").and_then(Value::as_str) != Some(request.stage_id.as_str()) {
        return AppResult::error("command_failed", Some(&project));
    }
    let role = stage
        .get("role")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let allowed = STAGED_PRODUCE_TARGETS
        .iter()
        .find(|(named, _)| *named == role)
        .is_some_and(|(_, targets)| targets.contains(&request.artifact.as_str()));
    if !allowed {
        return AppResult::blocked_with_data(
            "staged_artifact_target_refused",
            &project,
            Some(serde_json::json!({
                "role": role,
                "artifact": request.artifact,
                "allowed_targets": STAGED_PRODUCE_TARGETS,
            })),
        );
    }
    let Some(blob) = stage.get("blob").and_then(Value::as_str) else {
        return AppResult::error("command_failed", Some(&project));
    };
    produce_artifact(
        &ProduceArtifactRequest {
            project_root: project,
            lease: request.lease.clone(),
            artifact: request.artifact.clone(),
            source_file: request.project_root.join(blob),
            produced_by: request.produced_by.clone(),
        },
        &repo,
        executor,
    )
}

pub fn produce_artifact(
    request: &ProduceArtifactRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let Ok(source) = direct_file(&request.source_file) else {
        return AppResult::invalid_input();
    };
    let staging = project.join(".hvp/staging");
    if request.artifact.is_empty()
        || request.produced_by.trim().is_empty()
        || !source.starts_with(&staging)
    {
        return AppResult::invalid_input();
    }
    let Ok(program) = direct_file(&repo.join("scripts/hvp-produce")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let arguments = [
        project.clone().into_os_string(),
        request.artifact.clone().into(),
        OsString::from("--from"),
        source.into_os_string(),
        OsString::from("--produced-by"),
        request.produced_by.clone().into(),
    ];
    let token = &request.lease.capability;
    let mut executor_failed = false;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || {
            executor.execute(&program, &arguments).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })
        },
    );
    match execution {
        Ok(result)
            if result.exit_code == Some(0)
                && producer_receipt_ready(&result.data, &project, &request.artifact) =>
        {
            AppResult::ok("artifact_produced", &project, result.data)
        }
        Ok(result) => AppResult::error_with_data("command_failed", Some(&project), result.data),
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) if executor_failed => {
            AppResult::error("runner_unavailable", Some(&project))
        }
        Err(StoreError::Io(_)) => AppResult::error("internal_error", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

fn producer_receipt_ready(data: &Option<Value>, project: &Path, artifact_path: &str) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    if result.get("schema").and_then(Value::as_str) != Some("haru.producer_receipt.v1")
        || result.get("project").and_then(Value::as_str)
            != project.file_name().and_then(|value| value.to_str())
        || result.get("artifact").and_then(Value::as_str) != Some(artifact_path)
    {
        return false;
    }
    crate::Artifact::from_path(project, &project.join(artifact_path))
        .ok()
        .is_some_and(|artifact| {
            result.get("output_sha256").and_then(Value::as_str) == Some(&artifact.sha256)
                && result.get("bytes").and_then(Value::as_u64) == Some(artifact.bytes)
        })
}

pub fn approve_publish(
    request: &ApprovePublishRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let Ok(workspace) = project_workspace(&project) else {
        return AppResult::invalid_input();
    };
    if request.attestation_ref.trim().is_empty()
        || request
            .override_reason
            .as_ref()
            .is_some_and(|value| value.trim().is_empty())
    {
        return AppResult::invalid_input();
    }
    let Ok(program) = direct_file(&repo.join("scripts/hvp-approve")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let mut arguments = vec![
        OsString::from("approve"),
        project.clone().into_os_string(),
        OsString::from("--workspace"),
        workspace.into_os_string(),
        OsString::from("--attestation-ref"),
        request.attestation_ref.clone().into(),
    ];
    if let Some(reason) = &request.override_reason {
        arguments.extend([OsString::from("--override-reason"), reason.clone().into()]);
    }
    let token = &request.lease.capability;
    let mut executor_failed = false;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || {
            executor.execute(&program, &arguments).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })
        },
    );
    match execution {
        Ok(result)
            if result.exit_code == Some(0)
                && publish_approval_ready(
                    &result.data,
                    &project,
                    &request.attestation_ref,
                    &crate::source_fingerprint::manifest().youtube_channel_id,
                ) =>
        {
            AppResult::ok("publish_approved", &project, result.data)
        }
        Ok(result) => AppResult::error_with_data("command_failed", Some(&project), result.data),
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) if executor_failed => {
            AppResult::error("runner_unavailable", Some(&project))
        }
        Err(StoreError::Io(_)) => AppResult::error("internal_error", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

fn publish_approval_ready(
    data: &Option<Value>,
    project: &Path,
    attestation_ref: &str,
    canonical_channel: &str,
) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    let stored = fs::read(project.join("publish/publish-approval.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
    let final_sha = result.get("final_sha256").and_then(Value::as_str);
    let intent_sha = result.get("approval_intent_sha256").and_then(Value::as_str);
    result.get("schema").and_then(Value::as_str) == Some("haru.publish_approval.v3")
        && result.get("ok").and_then(Value::as_bool) == Some(true)
        && result.get("state").and_then(Value::as_str) == Some("publish_approved")
        && result.get("channel_id").and_then(Value::as_str) == Some(canonical_channel)
        && final_sha.is_some()
        && intent_sha.is_some()
        && stored.as_ref().is_some_and(|receipt| {
            receipt.get("schema").and_then(Value::as_str) == Some("haru.publish_approval.v3")
                && receipt.get("project").and_then(Value::as_str)
                    == project.file_name().and_then(|value| value.to_str())
                && receipt.get("attestation_ref").and_then(Value::as_str) == Some(attestation_ref)
                && receipt
                    .get("approval_intent_sha256")
                    .and_then(Value::as_str)
                    == intent_sha
                && receipt.get("final_sha256").and_then(Value::as_str) == final_sha
                && receipt.get("channel_id").and_then(Value::as_str) == Some(canonical_channel)
        })
}

pub fn prepare_publish_approval(
    request: &PreparePublishApprovalRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let Ok(workspace) = project_workspace(&project) else {
        return AppResult::invalid_input();
    };
    if request
        .override_reason
        .as_ref()
        .is_some_and(|value| value.trim().is_empty())
    {
        return AppResult::invalid_input();
    }
    let Ok(program) = direct_file(&repo.join("scripts/hvp-approve")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let mut arguments = vec![
        OsString::from("prepare"),
        project.clone().into_os_string(),
        OsString::from("--workspace"),
        workspace.into_os_string(),
    ];
    if let Some(reason) = &request.override_reason {
        arguments.extend([OsString::from("--override-reason"), reason.clone().into()]);
    }
    match executor.execute(&program, &arguments) {
        Ok(result)
            if result.exit_code == Some(0)
                && publish_approval_request_ready(&result.data, &project) =>
        {
            AppResult::ok("approval_intent_prepared", &project, result.data)
        }
        Ok(result)
            if result
                .data
                .as_ref()
                .and_then(|value| value.get("code"))
                .and_then(Value::as_str)
                == Some("issuer_not_enrolled") =>
        {
            AppResult::blocked_with_data("issuer_not_enrolled", &project, result.data)
        }
        Ok(result) => AppResult::error_with_data("command_failed", Some(&project), result.data),
        Err(_) => AppResult::error("runner_unavailable", Some(&project)),
    }
}

fn publish_approval_request_ready(data: &Option<Value>, project: &Path) -> bool {
    let Some(Value::Object(request)) = data else {
        return false;
    };
    if request.len() != 5
        || request.get("schema").and_then(Value::as_str)
            != Some("haru.publish_approval_signing_request.v1")
        || request.get("project_root").and_then(Value::as_str) != project.to_str()
    {
        return false;
    }
    let Some(intent @ Value::Object(_)) = request.get("intent") else {
        return false;
    };
    if intent.get("schema").and_then(Value::as_str) != Some("haru.publish_approval.v3")
        || !intent
            .get("runtime_contract")
            .is_some_and(valid_approval_runtime_contract)
        || ["final_sha256", "metadata_sha256", "cover_sha256"]
            .iter()
            .any(|field| {
                !intent
                    .get(*field)
                    .and_then(Value::as_str)
                    .is_some_and(lower_hex_64)
            })
        || ["render_self_eval", "visual_qa_review"]
            .iter()
            .any(|field| {
                !intent
                    .get(*field)
                    .and_then(|reference| reference.get("sha256"))
                    .and_then(Value::as_str)
                    .is_some_and(lower_hex_64)
            })
    {
        return false;
    }
    let Some(intent_sha256) = request.get("intent_sha256").and_then(Value::as_str) else {
        return false;
    };
    let actual_digest = format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(intent).unwrap_or_default())
    );
    if !lower_hex_64(intent_sha256) || actual_digest != intent_sha256 {
        return false;
    }
    let Some(Value::Object(attestation)) = request.get("attestation") else {
        return false;
    };
    let project_root_sha256 = format!(
        "{:x}",
        Sha256::digest(project.as_os_str().as_encoded_bytes()),
    );
    const ATTESTATION_FIELDS: [&str; 13] = [
        "schema",
        "attestation_ref",
        "project_id",
        "project_root_sha256",
        "approval_intent_sha256",
        "nonce",
        "generation",
        "issued_at",
        "expires_at",
        "channel_id",
        "visibility",
        "key_id",
        "signature_algorithm",
    ];
    attestation.len() == ATTESTATION_FIELDS.len()
        && ATTESTATION_FIELDS
            .iter()
            .all(|field| attestation.contains_key(*field))
        && attestation.get("schema").and_then(Value::as_str) == Some("haru.publish_attestation.v2")
        && attestation
            .get("approval_intent_sha256")
            .and_then(Value::as_str)
            == Some(intent_sha256)
        && attestation.get("project_id").and_then(Value::as_str)
            == project.file_name().and_then(|value| value.to_str())
        && attestation
            .get("project_root_sha256")
            .and_then(Value::as_str)
            == Some(project_root_sha256.as_str())
        && attestation.get("visibility").and_then(Value::as_str) == Some("unlisted")
        && attestation
            .get("key_id")
            .and_then(Value::as_str)
            .is_some_and(lower_hex_64)
        && attestation
            .get("nonce")
            .and_then(Value::as_str)
            .is_some_and(lower_hex_64)
        && attestation
            .get("attestation_ref")
            .and_then(Value::as_str)
            .is_some_and(valid_attestation_ref)
        && ["issued_at", "expires_at"].iter().all(|field| {
            attestation
                .get(*field)
                .and_then(Value::as_str)
                .is_some_and(|value| value.ends_with("+00:00") && value.len() == 25)
        })
        && attestation
            .get("signature_algorithm")
            .and_then(Value::as_str)
            == Some("ecdsa-p256-sha256")
        && attestation.get("attestation_ref") == intent.get("attestation_ref")
        && attestation.get("nonce") == intent.get("nonce")
        && attestation.get("generation") == intent.get("generation")
        && attestation.get("channel_id") == intent.get("channel_id")
}

fn lower_hex_64(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_attestation_ref(value: &str) -> bool {
    value
        .strip_prefix("attestation:")
        .and_then(|value| {
            uuid::Uuid::parse_str(value)
                .ok()
                .map(|parsed| (value, parsed))
        })
        .is_some_and(|(value, parsed)| parsed.get_version_num() == 4 && parsed.to_string() == value)
}

fn valid_approval_runtime_contract(value: &Value) -> bool {
    let Some(contract) = value.as_object() else {
        return false;
    };
    contract.len() == 4
        && contract.get("schema").and_then(Value::as_str)
            == Some("haru.project_runtime_contract.v1")
        && ["runtime", "evaluator", "artifact"].iter().all(|field| {
            contract
                .get(*field)
                .and_then(Value::as_str)
                .is_some_and(|value| !value.trim().is_empty())
        })
}

pub fn verify(
    project_root: &Path,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    match execute_verifier(&project, &repo, executor) {
        Ok(result) if result.exit_code == Some(0) && verification_ready(&result.data, &project) => {
            AppResult::ok("verification_checked", &project, result.data)
        }
        Ok(result) => AppResult::error_with_data("command_failed", Some(&project), result.data),
        Err(_) => AppResult::error("runner_unavailable", Some(&project)),
    }
}

fn local_delivery_envelope(
    value: &Option<Value>,
    project: &Path,
) -> Option<(String, String, Value)> {
    let envelope = value.as_ref()?.as_object()?;
    if envelope.get("schema_version").and_then(Value::as_u64) != Some(1)
        || envelope.get("project").and_then(Value::as_str)
            != Some(project.to_string_lossy().as_ref())
    {
        return None;
    }
    let outcome = envelope.get("outcome")?.as_str()?.to_owned();
    let code = envelope.get("code")?.as_str()?.to_owned();
    let data = envelope.get("data")?.clone();
    Some((outcome, code, data))
}

fn local_delivery_payload_valid(data: &Value, project: &Path, export: bool) -> bool {
    let Some(payload) = data.as_object() else {
        return false;
    };
    let expected_schema = if export {
        "video_studio.delivery_export.v1"
    } else {
        "video_studio.local_delivery_status.v1"
    };
    payload.get("schema").and_then(Value::as_str) == Some(expected_schema)
        && payload.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && payload.get("publication_ready").and_then(Value::as_bool) == Some(false)
        && payload.get("human_approval").and_then(Value::as_bool) == Some(false)
}

fn diagnostic_export_valid(data: &Value, project: &Path) -> bool {
    let Some(payload) = data.as_object() else {
        return false;
    };
    payload.get("schema").and_then(Value::as_str) == Some("video_studio.diagnostic_export.v1")
        && payload.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && payload.get("status").and_then(Value::as_str) == Some("diagnostic_only")
        && payload.get("delivery_ready").and_then(Value::as_bool) == Some(false)
        && payload.get("publication_ready").and_then(Value::as_bool) == Some(false)
        && payload.get("human_approval").and_then(Value::as_bool) == Some(false)
}

/// Run fresh, read-only technical delivery checks. This intentionally does not
/// call the publication verifier and never promotes technical playability into
/// pronunciation, content, visual, or publication approval.
pub fn delivery_status(
    project_root: &Path,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let Ok(program) = direct_file(&repo.join("scripts/local-delivery")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let arguments = [OsString::from("status"), project.clone().into_os_string()];
    match executor.execute(&program, &arguments) {
        Ok(result) => {
            let Some((outcome, code, data)) = local_delivery_envelope(&result.data, &project)
            else {
                return AppResult::error_with_data("command_failed", Some(&project), result.data);
            };
            if !local_delivery_payload_valid(&data, &project, false) {
                return AppResult::error_with_data("command_failed", Some(&project), Some(data));
            }
            match (result.exit_code, outcome.as_str(), code.as_str()) {
                (Some(0), "ok", "delivery_technical_ready") => {
                    AppResult::ok("delivery_technical_ready", &project, Some(data))
                }
                (Some(3), "blocked", "delivery_blocked") => {
                    AppResult::blocked_with_data("delivery_blocked", &project, Some(data))
                }
                _ => AppResult::error_with_data("command_failed", Some(&project), Some(data)),
            }
        }
        Err(_) => AppResult::error("runner_unavailable", Some(&project)),
    }
}

pub fn export_delivery(
    request: &ExportDeliveryRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    if !valid_receipt_id(&request.idempotency_key) {
        return AppResult::invalid_input();
    }
    let Ok(program) = direct_file(&repo.join("scripts/local-delivery")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let mut arguments = vec![
        OsString::from("export"),
        project.clone().into_os_string(),
        OsString::from("--idempotency-key"),
        request.idempotency_key.clone().into(),
    ];
    if request.diagnostic {
        arguments.push(OsString::from("--diagnostic"));
    }
    let token = &request.lease.capability;
    let mut executor_failed = false;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || {
            executor.execute(&program, &arguments).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })
        },
    );
    match execution {
        Ok(result) => {
            let Some((outcome, code, data)) = local_delivery_envelope(&result.data, &project)
            else {
                return AppResult::error_with_data("command_failed", Some(&project), result.data);
            };
            let expected_code = if request.diagnostic {
                "diagnostics_exported"
            } else {
                "delivery_exported"
            };
            let payload_valid = if request.diagnostic {
                diagnostic_export_valid(&data, &project)
            } else {
                local_delivery_payload_valid(&data, &project, true)
                    && data.get("status").and_then(Value::as_str) == Some("exported")
            };
            if result.exit_code == Some(0)
                && outcome == "ok"
                && code == expected_code
                && payload_valid
                && data
                    .get("bundle_id")
                    .and_then(Value::as_str)
                    .is_some_and(is_sha256)
                && data
                    .get("bundle_path")
                    .and_then(Value::as_str)
                    .is_some_and(|path| Path::new(path).is_absolute())
            {
                AppResult::ok(expected_code, &project, Some(data))
            } else if result.exit_code == Some(3) && outcome == "blocked" {
                AppResult::blocked_with_data(&code, &project, Some(data))
            } else {
                AppResult::error_with_data("command_failed", Some(&project), Some(data))
            }
        }
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) if executor_failed => {
            AppResult::error("runner_unavailable", Some(&project))
        }
        Err(StoreError::Io(_)) => AppResult::error("internal_error", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

fn valid_job_id(value: &str) -> bool {
    value.len() == 32
        && value
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

fn job_envelope(value: &Option<Value>, project: &Path) -> Option<(String, String, Option<Value>)> {
    let envelope = value.as_ref()?.as_object()?;
    if envelope.get("schema_version").and_then(Value::as_u64) != Some(1)
        || envelope.get("project").and_then(Value::as_str)
            != Some(project.to_string_lossy().as_ref())
    {
        return None;
    }
    Some((
        envelope.get("outcome")?.as_str()?.to_owned(),
        envelope.get("code")?.as_str()?.to_owned(),
        envelope
            .get("data")
            .cloned()
            .filter(|value| !value.is_null()),
    ))
}

fn public_job_valid(data: &Value, job_id: &str) -> bool {
    let Some(job) = data.as_object() else {
        return false;
    };
    const ALLOWED: &[&str] = &[
        "can_cancel",
        "can_resume",
        "created_at",
        "epoch",
        "error_code",
        "exit_code",
        "job_id",
        "kind",
        "log_available",
        "output",
        "revision",
        "schema",
        "status",
        "updated_at",
    ];
    job.keys().all(|key| ALLOWED.contains(&key.as_str()))
        && job.get("schema").and_then(Value::as_str) == Some("haru.render_job.v2")
        && job.get("job_id").and_then(Value::as_str) == Some(job_id)
        && job.get("kind").and_then(Value::as_str) == Some("render")
        && job.get("output").and_then(Value::as_str) == Some("output/final.mp4")
        && job
            .get("epoch")
            .and_then(Value::as_u64)
            .is_some_and(|value| value > 0)
        && job
            .get("revision")
            .and_then(Value::as_str)
            .is_some_and(is_sha256)
        && job
            .get("created_at")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty())
        && job
            .get("updated_at")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty())
        && job.get("log_available").and_then(Value::as_bool).is_some()
        && job.get("can_cancel").and_then(Value::as_bool).is_some()
        && job.get("can_resume").and_then(Value::as_bool).is_some()
        && job
            .get("status")
            .and_then(Value::as_str)
            .is_some_and(|status| {
                matches!(
                    status,
                    "queued"
                        | "running"
                        | "cancel_requested"
                        | "promoting"
                        | "succeeded"
                        | "failed"
                        | "cancelled"
                        | "interrupted"
                )
            })
}

fn job_logs_valid(data: &Value, job_id: &str, max_bytes: u64) -> bool {
    let Some(logs) = data.as_object() else {
        return false;
    };
    const ALLOWED: &[&str] = &[
        "job_id",
        "redacted",
        "returned_bytes",
        "schema",
        "status",
        "text",
        "total_bytes",
        "truncated",
    ];
    logs.keys().all(|key| ALLOWED.contains(&key.as_str()))
        && logs.get("schema").and_then(Value::as_str) == Some("video-studio.job_logs.v1")
        && logs.get("job_id").and_then(Value::as_str) == Some(job_id)
        && logs.get("text").and_then(Value::as_str).is_some()
        && logs.get("status").and_then(Value::as_str).is_some()
        && logs.get("total_bytes").and_then(Value::as_u64).is_some()
        && logs
            .get("returned_bytes")
            .and_then(Value::as_u64)
            .is_some_and(|bytes| bytes <= max_bytes)
        && logs.get("redacted").and_then(Value::as_bool).is_some()
        && logs.get("truncated").and_then(Value::as_bool).is_some()
}

fn job_read_result(
    result: CommandResult,
    project: &Path,
    expected_code: &str,
    validator: impl FnOnce(&Value) -> bool,
) -> AppResult {
    let Some((outcome, code, data)) = job_envelope(&result.data, project) else {
        return AppResult::error_with_data("command_failed", Some(project), result.data);
    };
    if result.exit_code == Some(0)
        && outcome == "ok"
        && code == expected_code
        && data.as_ref().is_some_and(validator)
    {
        AppResult::ok(expected_code, project, data)
    } else if result.exit_code == Some(2) && outcome == "error" && code == "job_not_found" {
        AppResult::error("job_not_found", Some(project))
    } else if result.exit_code == Some(2) && outcome == "error" && code == "invalid_input" {
        AppResult::invalid_input()
    } else {
        AppResult::error_with_data("command_failed", Some(project), data)
    }
}

pub fn job_status(
    project_root: &Path,
    repo_root: &Path,
    job_id: &str,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    if !valid_job_id(job_id) {
        return AppResult::invalid_input();
    }
    let Ok(program) = direct_file(&repo.join("scripts/render-job")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let arguments = [
        OsString::from("status"),
        project.clone().into_os_string(),
        job_id.into(),
    ];
    match executor.execute(&program, &arguments) {
        Ok(result) => job_read_result(result, &project, "job_status", |data| {
            public_job_valid(data, job_id)
        }),
        Err(_) => AppResult::error("runner_unavailable", Some(&project)),
    }
}

pub fn job_logs(
    project_root: &Path,
    repo_root: &Path,
    job_id: &str,
    max_bytes: Option<u32>,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let max_bytes = u64::from(max_bytes.unwrap_or(32 * 1024));
    if !valid_job_id(job_id) || !(1..=65_536).contains(&max_bytes) {
        return AppResult::invalid_input();
    }
    let Ok(program) = direct_file(&repo.join("scripts/render-job")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let arguments = [
        OsString::from("logs"),
        project.clone().into_os_string(),
        job_id.into(),
        max_bytes.to_string().into(),
    ];
    match executor.execute(&program, &arguments) {
        Ok(result) => job_read_result(result, &project, "job_logs", |data| {
            job_logs_valid(data, job_id, max_bytes)
        }),
        Err(_) => AppResult::error("runner_unavailable", Some(&project)),
    }
}

pub fn mutate_job(
    action: &str,
    request: &JobMutationRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    if !matches!(action, "cancel" | "resume") || !valid_job_id(&request.job_id) {
        return AppResult::invalid_input();
    }
    let Ok(program) = direct_file(&repo.join("scripts/render-job")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let arguments = [
        OsString::from(action),
        project.clone().into_os_string(),
        request.job_id.clone().into(),
    ];
    let token = &request.lease.capability;
    let mut executor_failed = false;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || {
            executor.execute(&program, &arguments).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })
        },
    );
    match execution {
        Ok(result) => {
            let Some((outcome, code, data)) = job_envelope(&result.data, &project) else {
                return AppResult::error_with_data("command_failed", Some(&project), result.data);
            };
            let data_valid = data
                .as_ref()
                .is_some_and(|value| public_job_valid(value, &request.job_id));
            let allowed_ok = match action {
                "cancel" => matches!(code.as_str(), "job_cancelled" | "job_terminal"),
                "resume" => matches!(
                    code.as_str(),
                    "job_resumed" | "job_running" | "job_terminal"
                ),
                _ => false,
            };
            if result.exit_code == Some(0) && outcome == "ok" && allowed_ok && data_valid {
                AppResult::ok(&code, &project, data)
            } else if result.exit_code == Some(3)
                && outcome == "blocked"
                && code == "job_commit_in_progress"
                && data_valid
            {
                AppResult::blocked_with_data(&code, &project, data)
            } else if result.exit_code == Some(2) && outcome == "error" && code == "job_not_found" {
                AppResult::error("job_not_found", Some(&project))
            } else {
                AppResult::error_with_data("command_failed", Some(&project), data)
            }
        }
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) if executor_failed => {
            AppResult::error("runner_unavailable", Some(&project))
        }
        Err(StoreError::Io(_)) => AppResult::error("internal_error", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

fn valid_comment_id(value: &str) -> bool {
    uuid::Uuid::parse_str(value)
        .ok()
        .is_some_and(|parsed| parsed.to_string() == value)
}

fn review_feedback_valid(data: &Value, project: &Path) -> bool {
    let Some(value) = data.as_object() else {
        return false;
    };
    value.get("schema").and_then(Value::as_str) == Some("video_studio.review_feedback.v1")
        && value.get("outcome").and_then(Value::as_str) == Some("ok")
        && value.get("code").and_then(Value::as_str) == Some("review_feedback")
        && value.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && value
            .get("package_id")
            .and_then(Value::as_str)
            .is_some_and(is_sha256)
        && value.get("counts").and_then(Value::as_object).is_some()
        && value
            .get("comments")
            .and_then(Value::as_array)
            .is_some_and(|comments| {
                comments.iter().all(|comment| {
                    let Some(comment) = comment.as_object() else {
                        return false;
                    };
                    comment
                        .get("id")
                        .and_then(Value::as_str)
                        .is_some_and(valid_comment_id)
                        && comment
                            .get("status")
                            .and_then(Value::as_str)
                            .is_some_and(|status| matches!(status, "open" | "resolved"))
                        && ["is_current", "asset_available", "stale"]
                            .iter()
                            .all(|field| comment.get(*field).and_then(Value::as_bool).is_some())
                        && !comment.keys().any(|key| {
                            matches!(
                                key.as_str(),
                                "storage_root" | "credential" | "token" | "executable"
                            )
                        })
                })
            })
}

fn review_resolution_valid(
    data: &Value,
    project: &Path,
    request: &ReviewResolutionRequest,
) -> bool {
    let Some(value) = data.as_object() else {
        return false;
    };
    let Some(comment) = value.get("comment").and_then(Value::as_object) else {
        return false;
    };
    value.get("schema").and_then(Value::as_str) == Some("video_studio.review_resolution.v1")
        && value.get("outcome").and_then(Value::as_str) == Some("ok")
        && value
            .get("code")
            .and_then(Value::as_str)
            .is_some_and(|code| {
                matches!(code, "review_comment_updated" | "review_comment_unchanged")
            })
        && value.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && value.get("package_id").and_then(Value::as_str)
            == Some(request.expected_package_id.as_str())
        && comment.get("id").and_then(Value::as_str) == Some(request.comment_id.as_str())
        && comment.get("status").and_then(Value::as_str) == Some(request.status.as_str())
        && value
            .get("effects")
            .and_then(Value::as_object)
            .is_some_and(|effects| {
                !effects.is_empty()
                    && effects
                        .values()
                        .all(|effect| effect.as_bool() == Some(false))
            })
}

fn review_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn review_add_request_file(
    project: &Path,
    request: &ReviewAddRequest,
) -> io::Result<(String, PathBuf)> {
    let value = serde_json::json!({
        "schema": "video_studio.review_add_request.v1",
        "client_id": request.client_id,
        "package_id": request.package_id,
        "asset_id": request.asset_id,
        "asset_sha256": request.asset_sha256,
        "timestamp_seconds": request.timestamp_seconds,
        "body": request.body.trim(),
    });
    let payload = serde_json::to_vec(&value).map_err(|error| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("review request: {error}"),
        )
    })?;
    let request_id = format!("{:x}", Sha256::digest(&payload))[..32].to_owned();
    let state = project.join(".hvp");
    match fs::symlink_metadata(&state) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "unsafe project state",
            ));
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => fs::create_dir(&state)?,
        Err(error) => return Err(error),
    }
    #[cfg(unix)]
    fs::set_permissions(&state, fs::Permissions::from_mode(0o700))?;
    let requests = state.join("review-requests");
    match fs::symlink_metadata(&requests) {
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_dir() => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "unsafe review requests",
            ));
        }
        Ok(_) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => fs::create_dir(&requests)?,
        Err(error) => return Err(error),
    }
    #[cfg(unix)]
    fs::set_permissions(&requests, fs::Permissions::from_mode(0o700))?;
    let path = requests.join(format!("{request_id}.json"));
    match OpenOptions::new().create_new(true).write(true).open(&path) {
        Ok(mut output) => {
            output.write_all(&payload)?;
            output.sync_all()?;
            #[cfg(unix)]
            fs::set_permissions(&path, fs::Permissions::from_mode(0o400))?;
        }
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
            let metadata = fs::symlink_metadata(&path)?;
            if metadata.file_type().is_symlink()
                || !metadata.is_file()
                || fs::read(&path)? != payload
            {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "review request conflicts",
                ));
            }
        }
        Err(error) => return Err(error),
    }
    Ok((request_id, path))
}

fn review_add_valid(data: &Value, project: &Path, request: &ReviewAddRequest) -> bool {
    let Some(value) = data.as_object() else {
        return false;
    };
    let Some(comment) = value.get("comment").and_then(Value::as_object) else {
        return false;
    };
    value.get("schema").and_then(Value::as_str) == Some("video_studio.review_add.v1")
        && value.get("outcome").and_then(Value::as_str) == Some("ok")
        && value
            .get("code")
            .and_then(Value::as_str)
            .is_some_and(|code| matches!(code, "review_comment_added" | "review_comment_existing"))
        && value.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && value.get("package_id").and_then(Value::as_str) == Some(request.package_id.as_str())
        && comment
            .get("id")
            .and_then(Value::as_str)
            .is_some_and(valid_comment_id)
        && comment
            .get("asset")
            .and_then(Value::as_object)
            .and_then(|asset| asset.get("id"))
            .and_then(Value::as_str)
            == Some(request.asset_id.as_str())
        && comment
            .get("asset")
            .and_then(Value::as_object)
            .and_then(|asset| asset.get("sha256"))
            .and_then(Value::as_str)
            == Some(request.asset_sha256.as_str())
        && value
            .get("effects")
            .and_then(Value::as_object)
            .is_some_and(|effects| {
                !effects.is_empty()
                    && effects
                        .values()
                        .all(|effect| effect.as_bool() == Some(false))
            })
}

pub fn review_add(
    request: &ReviewAddRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    if !valid_comment_id(&request.client_id)
        || !is_sha256(&request.package_id)
        || !review_identifier(&request.asset_id)
        || !is_sha256(&request.asset_sha256)
        || request
            .timestamp_seconds
            .is_some_and(|value| !value.is_finite() || value < 0.0)
        || request.body.trim().is_empty()
        || request.body.len() > 5000
    {
        return AppResult::invalid_input();
    }
    let Ok(program) = direct_file(&repo.join("scripts/review-feedback")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let (request_id, request_path) = match review_add_request_file(&project, request) {
        Ok(value) => value,
        Err(_) => return AppResult::error("internal_error", Some(&project)),
    };
    let arguments = [
        OsString::from("add"),
        project.clone().into_os_string(),
        OsString::from("--request-id"),
        request_id.into(),
    ];
    let result = executor.execute(&program, &arguments);
    match fs::remove_file(request_path) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(_) if result.is_ok() => return AppResult::error("internal_error", Some(&project)),
        Err(_) => {}
    }
    match result {
        Ok(result)
            if result.exit_code == Some(0)
                && review_add_valid(
                    result.data.as_ref().unwrap_or(&Value::Null),
                    &project,
                    request,
                ) =>
        {
            let code = result
                .data
                .as_ref()
                .and_then(|data| data.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("review_comment_added")
                .to_owned();
            AppResult::ok(&code, &project, result.data)
        }
        Ok(result) if result.exit_code == Some(3) => {
            let code = result
                .data
                .as_ref()
                .and_then(|data| data.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("review_conflict")
                .to_owned();
            if matches!(code.as_str(), "review_conflict" | "review_stale") {
                AppResult::blocked_with_data(&code, &project, result.data)
            } else {
                AppResult::error_with_data("command_failed", Some(&project), result.data)
            }
        }
        Ok(result) if result.exit_code == Some(2) => {
            let code = result
                .data
                .as_ref()
                .and_then(|data| data.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("invalid_input")
                .to_owned();
            AppResult::error_with_data(&code, Some(&project), result.data)
        }
        Ok(result) => AppResult::error_with_data("command_failed", Some(&project), result.data),
        Err(_) => AppResult::error("runner_unavailable", Some(&project)),
    }
}

pub fn review_feedback(
    project_root: &Path,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let Ok(program) = direct_file(&repo.join("scripts/review-feedback")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let arguments = [OsString::from("read"), project.clone().into_os_string()];
    match executor.execute(&program, &arguments) {
        Ok(result)
            if result.exit_code == Some(0)
                && review_feedback_valid(
                    result.data.as_ref().unwrap_or(&Value::Null),
                    &project,
                ) =>
        {
            AppResult::ok("review_feedback", &project, result.data)
        }
        Ok(result) if result.exit_code == Some(2) => {
            let code = result
                .data
                .as_ref()
                .and_then(|data| data.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("invalid_input")
                .to_owned();
            if code == "invalid_input" {
                AppResult::invalid_input()
            } else {
                AppResult::error_with_data(&code, Some(&project), result.data)
            }
        }
        Ok(result) => AppResult::error_with_data("command_failed", Some(&project), result.data),
        Err(_) => AppResult::error("runner_unavailable", Some(&project)),
    }
}

pub fn review_resolve(
    request: &ReviewResolutionRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    if !valid_comment_id(&request.comment_id)
        || !matches!(request.status.as_str(), "open" | "resolved")
        || !is_sha256(&request.expected_package_id)
        || !is_sha256(&request.expected_asset_sha256)
    {
        return AppResult::invalid_input();
    }
    let Ok(program) = direct_file(&repo.join("scripts/review-feedback")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let arguments = [
        OsString::from("resolve"),
        project.clone().into_os_string(),
        OsString::from("--comment-id"),
        request.comment_id.clone().into(),
        OsString::from("--status"),
        request.status.clone().into(),
        OsString::from("--expected-package-id"),
        request.expected_package_id.clone().into(),
        OsString::from("--expected-asset-sha256"),
        request.expected_asset_sha256.clone().into(),
    ];
    match executor.execute(&program, &arguments) {
        Ok(result)
            if result.exit_code == Some(0)
                && review_resolution_valid(
                    result.data.as_ref().unwrap_or(&Value::Null),
                    &project,
                    request,
                ) =>
        {
            let code = result
                .data
                .as_ref()
                .and_then(|data| data.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("review_comment_updated")
                .to_owned();
            AppResult::ok(&code, &project, result.data)
        }
        Ok(result) if result.exit_code == Some(3) => {
            let code = result
                .data
                .as_ref()
                .and_then(|data| data.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("review_conflict")
                .to_owned();
            if matches!(code.as_str(), "review_conflict" | "review_stale") {
                AppResult::blocked_with_data(&code, &project, result.data)
            } else {
                AppResult::error_with_data("command_failed", Some(&project), result.data)
            }
        }
        Ok(result) if result.exit_code == Some(2) => {
            let code = result
                .data
                .as_ref()
                .and_then(|data| data.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("invalid_input")
                .to_owned();
            if code == "invalid_input" {
                AppResult::invalid_input()
            } else {
                AppResult::error_with_data(&code, Some(&project), result.data)
            }
        }
        Ok(result) => AppResult::error_with_data("command_failed", Some(&project), result.data),
        Err(_) => AppResult::error("runner_unavailable", Some(&project)),
    }
}

fn execute_verifier(
    project: &Path,
    repo: &Path,
    executor: &mut impl CommandExecutor,
) -> io::Result<CommandResult> {
    let program = direct_file(&repo.join("scripts/verify-project"))?;
    let workspace = project_workspace(project)?;
    executor.execute(
        &program,
        &[
            project.to_path_buf().into_os_string(),
            OsString::from("--workspace"),
            workspace.into_os_string(),
        ],
    )
}

fn project_workspace(project: &Path) -> io::Result<PathBuf> {
    let projects = project
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid project root"))?;
    let workspace = projects
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "invalid project root"))?;
    direct_directory(workspace)
}

pub fn prepare_publish(
    project_root: &Path,
    repo_root: &Path,
    lease: &LeaseInput,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    let Ok(project) = direct_directory(project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let Ok(program) = direct_file(&repo.join("scripts/make-publish-pack")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let Ok(verifier) = direct_file(&repo.join("scripts/verify-project")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let Ok(workspace) = project_workspace(&project) else {
        return AppResult::invalid_input();
    };
    let token = &lease.capability;
    let arguments = [
        project.clone().into_os_string(),
        OsString::from("--workspace"),
        workspace.clone().into_os_string(),
        OsString::from("--write"),
    ];
    let verifier_arguments = [
        project.clone().into_os_string(),
        OsString::from("--workspace"),
        workspace.into_os_string(),
    ];
    let mut executor_failed = false;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &lease.owner,
        &lease.lease_id,
        lease.generation,
        token,
        SystemTime::now(),
        || {
            let result = executor.execute(&program, &arguments).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })?;
            if result.exit_code != Some(0) {
                return Ok(result);
            }
            executor
                .execute(&verifier, &verifier_arguments)
                .map_err(|error| {
                    executor_failed = true;
                    StoreError::Io(error)
                })
        },
    );
    match execution {
        Ok(result) if result.exit_code == Some(0) && verification_ready(&result.data, &project) => {
            AppResult::ok("publish_prepared", &project, result.data)
        }
        Ok(result) => AppResult::error_with_data("command_failed", Some(&project), result.data),
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) if executor_failed => {
            AppResult::error("runner_unavailable", Some(&project))
        }
        Err(StoreError::Io(_)) => AppResult::error("internal_error", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

pub fn publish(
    request: &PublishRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    if !request.project_root.is_absolute() {
        return AppResult::invalid_input();
    }
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let Some(workspace) = project.parent().and_then(Path::parent) else {
        return AppResult::invalid_input();
    };
    let Ok(workspace) = direct_directory(workspace) else {
        return AppResult::invalid_input();
    };
    let Ok(uploader) = direct_file(&repo.join("scripts/upload-youtube")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    if !valid_receipt_id(&request.idempotency_key) {
        return AppResult::invalid_input();
    }
    let token = &request.lease.capability;
    let arguments = [
        project.clone().into_os_string(),
        OsString::from("--workspace"),
        workspace.into_os_string(),
        OsString::from("--credential-reference"),
        canonical_youtube_credential_reference(),
        OsString::from("--idempotency-key"),
        request.idempotency_key.clone().into(),
        OsString::from("--runtime-id"),
        request.runtime.runtime_id.clone().into(),
        OsString::from("--runtime-binary-sha256"),
        request.runtime.binary_sha256.clone().into(),
    ];
    let mut executor_failed = false;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || {
            let verified = execute_verifier(&project, &repo, executor).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })?;
            if verified.exit_code != Some(0) || !verification_ready(&verified.data, &project) {
                return Ok(verified);
            }
            executor.execute(&uploader, &arguments).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })
        },
    );
    match execution {
        Ok(result)
            if result.exit_code == Some(0)
                && youtube_upload_ready(&result.data, &project, &request.runtime) =>
        {
            AppResult::ok("youtube_uploaded", &project, result.data)
        }
        Ok(result) => {
            let code = result
                .data
                .as_ref()
                .and_then(|data| data.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("command_failed")
                .to_owned();
            AppResult::error_with_data(&code, Some(&project), result.data)
        }
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) if executor_failed => {
            AppResult::error("runner_unavailable", Some(&project))
        }
        Err(StoreError::Io(_)) => AppResult::error("internal_error", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

pub fn replace_thumbnail(
    request: &ReplaceThumbnailRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    if !request.project_root.is_absolute()
        || request.updated_by.trim().is_empty()
        || !valid_receipt_id(&request.idempotency_key)
    {
        return AppResult::invalid_input();
    }
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let Some(workspace) = project.parent().and_then(Path::parent) else {
        return AppResult::invalid_input();
    };
    let Ok(workspace) = direct_directory(workspace) else {
        return AppResult::invalid_input();
    };
    let Ok(uploader) = direct_file(&repo.join("scripts/upload-youtube")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let token = &request.lease.capability;
    let arguments = [
        project.clone().into_os_string(),
        OsString::from("--workspace"),
        workspace.into_os_string(),
        OsString::from("--credential-reference"),
        canonical_youtube_credential_reference(),
        OsString::from("--idempotency-key"),
        request.idempotency_key.clone().into(),
        OsString::from("--thumbnail-only"),
        OsString::from("--updated-by"),
        request.updated_by.clone().into(),
        OsString::from("--runtime-id"),
        request.runtime.runtime_id.clone().into(),
        OsString::from("--runtime-binary-sha256"),
        request.runtime.binary_sha256.clone().into(),
    ];
    let mut executor_failed = false;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || {
            let verified = execute_verifier(&project, &repo, executor).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })?;
            if verified.exit_code != Some(0) || !verification_ready(&verified.data, &project) {
                return Ok(verified);
            }
            executor.execute(&uploader, &arguments).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })
        },
    );
    match execution {
        Ok(result)
            if result.exit_code == Some(0)
                && youtube_thumbnail_ready(&result.data, &project, &request.runtime) =>
        {
            AppResult::ok("youtube_thumbnail_replaced", &project, result.data)
        }
        Ok(result) => {
            let code = result
                .data
                .as_ref()
                .and_then(|data| data.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("command_failed")
                .to_owned();
            AppResult::error_with_data(&code, Some(&project), result.data)
        }
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) if executor_failed => {
            AppResult::error("runner_unavailable", Some(&project))
        }
        Err(StoreError::Io(_)) => AppResult::error("internal_error", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

pub fn reconcile_upload(
    request: &ReconcileUploadRequest,
    repo_root: &Path,
    executor: &mut impl CommandExecutor,
) -> AppResult {
    if !request.project_root.is_absolute() || !valid_receipt_id(&request.idempotency_key) {
        return AppResult::invalid_input();
    }
    if request
        .override_attestation_ref
        .as_ref()
        .is_some_and(|value| value.trim().is_empty())
    {
        return AppResult::invalid_input();
    }
    let Ok(project) = direct_directory(&request.project_root) else {
        return AppResult::invalid_input();
    };
    let Ok(repo) = direct_directory(repo_root) else {
        return AppResult::invalid_input();
    };
    let Some(workspace) = project.parent().and_then(Path::parent) else {
        return AppResult::invalid_input();
    };
    let Ok(workspace) = direct_directory(workspace) else {
        return AppResult::invalid_input();
    };
    let Ok(uploader) = direct_file(&repo.join("scripts/upload-youtube")) else {
        return AppResult::error("runner_unavailable", Some(&project));
    };
    let token = &request.lease.capability;
    let mut arguments = vec![
        project.clone().into_os_string(),
        OsString::from("--workspace"),
        workspace.into_os_string(),
        OsString::from("--credential-reference"),
        canonical_youtube_credential_reference(),
        OsString::from("--idempotency-key"),
        request.idempotency_key.clone().into(),
        OsString::from("--runtime-id"),
        request.runtime.runtime_id.clone().into(),
        OsString::from("--runtime-binary-sha256"),
        request.runtime.binary_sha256.clone().into(),
        OsString::from("--reconcile"),
    ];
    if let Some(reference) = &request.override_attestation_ref {
        arguments.push(OsString::from("--override-attestation-ref"));
        arguments.push(reference.clone().into());
    }
    let mut executor_failed = false;
    let execution = ProjectStore::new(&project).with_verified_lease_identity_at(
        &request.lease.owner,
        &request.lease.lease_id,
        request.lease.generation,
        token,
        SystemTime::now(),
        || {
            let verified = execute_verifier(&project, &repo, executor).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })?;
            if verified.exit_code != Some(0) || !verification_ready(&verified.data, &project) {
                return Ok(verified);
            }
            executor.execute(&uploader, &arguments).map_err(|error| {
                executor_failed = true;
                StoreError::Io(error)
            })
        },
    );
    match execution {
        Ok(result)
            if result.exit_code == Some(0)
                && youtube_reconcile_ready(&result.data, &project, &request.runtime) =>
        {
            let code = result
                .data
                .as_ref()
                .and_then(|data| data.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("youtube_upload_reconciled")
                .to_owned();
            AppResult::ok(&code, &project, result.data)
        }
        Ok(result) => {
            let code = result
                .data
                .as_ref()
                .and_then(|data| data.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("command_failed")
                .to_owned();
            if code == "youtube_upload_absence_unproved" {
                AppResult::blocked_with_data(&code, &project, result.data)
            } else {
                AppResult::error_with_data(&code, Some(&project), result.data)
            }
        }
        Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
            AppResult::blocked("lease_invalid", &project)
        }
        Err(StoreError::Io(_)) if executor_failed => {
            AppResult::error("runner_unavailable", Some(&project))
        }
        Err(StoreError::Io(_)) => AppResult::error("internal_error", Some(&project)),
        Err(_) => AppResult::error("internal_error", Some(&project)),
    }
}

fn canonical_youtube_credential_reference() -> OsString {
    format!(
        "youtube:{}",
        crate::source_fingerprint::manifest().youtube_channel_id
    )
    .into()
}

fn youtube_upload_ready(data: &Option<Value>, project: &Path, runtime: &RuntimeBinding) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    result.get("schema").and_then(Value::as_str) == Some("haru.youtube_upload.v1")
        && result.get("ok").and_then(Value::as_bool) == Some(true)
        && result.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && result.get("status").and_then(Value::as_str) == Some("complete")
        && result
            .get("video_id")
            .and_then(Value::as_str)
            .is_some_and(|value| {
                value.len() == 11
                    && value
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
            })
        && result
            .get("final_sha256")
            .and_then(Value::as_str)
            .is_some_and(|value| {
                value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
            })
        && result.get("visibility").and_then(Value::as_str) == Some("unlisted")
        && result.get("runtime_id").and_then(Value::as_str) == Some(&runtime.runtime_id)
        && result.get("runtime_binary_sha256").and_then(Value::as_str)
            == Some(&runtime.binary_sha256)
        && result.get("channel_id").and_then(Value::as_str)
            == Some(
                crate::source_fingerprint::manifest()
                    .youtube_channel_id
                    .as_str(),
            )
}

fn youtube_thumbnail_ready(data: &Option<Value>, project: &Path, runtime: &RuntimeBinding) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    result.get("schema").and_then(Value::as_str) == Some("haru.youtube_thumbnail.v1")
        && result.get("ok").and_then(Value::as_bool) == Some(true)
        && result.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && result.get("status").and_then(Value::as_str) == Some("complete")
        && result
            .get("video_id")
            .and_then(Value::as_str)
            .is_some_and(valid_video_id)
        && result
            .get("cover_sha256")
            .and_then(Value::as_str)
            .is_some_and(|value| {
                value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
            })
        && result.get("runtime_id").and_then(Value::as_str) == Some(&runtime.runtime_id)
        && result.get("runtime_binary_sha256").and_then(Value::as_str)
            == Some(&runtime.binary_sha256)
}

fn youtube_reconcile_ready(data: &Option<Value>, project: &Path, runtime: &RuntimeBinding) -> bool {
    let Some(Value::Object(result)) = data else {
        return false;
    };
    let status = result.get("status").and_then(Value::as_str);
    let code = result.get("code").and_then(Value::as_str);
    let video_ok = match (status, code) {
        (Some("complete"), Some("youtube_upload_reconciled")) => result
            .get("video_id")
            .and_then(Value::as_str)
            .is_some_and(valid_video_id),
        (Some("prepared"), Some("youtube_upload_restart_authorized")) => {
            result.get("video_id").is_none() || result.get("video_id") == Some(&Value::Null)
        }
        _ => false,
    };
    result.get("schema").and_then(Value::as_str) == Some("haru.youtube_upload_reconcile.v1")
        && result.get("ok").and_then(Value::as_bool) == Some(true)
        && result.get("project").and_then(Value::as_str)
            == project.file_name().and_then(|name| name.to_str())
        && result
            .get("final_sha256")
            .and_then(Value::as_str)
            .is_some_and(|value| {
                value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit())
            })
        && result.get("visibility").and_then(Value::as_str) == Some("unlisted")
        && result.get("runtime_id").and_then(Value::as_str) == Some(&runtime.runtime_id)
        && result.get("runtime_binary_sha256").and_then(Value::as_str)
            == Some(&runtime.binary_sha256)
        && result.get("channel_id").and_then(Value::as_str)
            == Some(
                crate::source_fingerprint::manifest()
                    .youtube_channel_id
                    .as_str(),
            )
        && video_ok
}

fn valid_video_id(value: &str) -> bool {
    value.len() == 11
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

pub(crate) fn direct_file(path: &Path) -> io::Result<PathBuf> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "invalid file"));
    }
    path.canonicalize()
}

/// The repository these bytes are allowed to execute against: the mirrored
/// source of a promoted release, or the checkout a development binary was
/// built from. Either way the closure has to hash to what was linked in.
pub fn validated_repo_root(binary: &Path) -> Option<PathBuf> {
    let repo_root = match crate::runtime::Layout::detect(binary)? {
        crate::runtime::Layout::Release { repo_root, .. } => repo_root,
        crate::runtime::Layout::Development { repo_root } => repo_root,
    };
    crate::source_fingerprint::calculate(&repo_root)
        .is_ok_and(|fingerprint| fingerprint == env!("HVP_SOURCE_FINGERPRINT"))
        .then_some(repo_root)
}

#[cfg(test)]
mod render_postcheck_tests {
    use super::render_ready;
    use serde_json::{Value, json};
    use std::fs;
    use tempfile::tempdir;

    fn otherwise_valid_render(project: &std::path::Path, assembly: Option<Value>) -> Option<Value> {
        fs::create_dir_all(project.join("output")).unwrap();
        fs::write(project.join("output/final.mp4"), b"final").unwrap();
        let artifact =
            crate::Artifact::from_path(project, &project.join("output/final.mp4")).unwrap();
        let mut data = json!({
            "schema": "haru.render_result.v1",
            "status": "render_complete",
            "project": project.file_name().unwrap().to_str().unwrap(),
            "output": "output/final.mp4",
            "video_sha256": artifact.sha256,
            "bytes": artifact.bytes,
            "duration_seconds": 1.0,
            "loudness_lufs": -14.0,
            "true_peak_dbfs": -1.1,
            "loudness_range_lu": 1.0,
            "mix": {
                "schema": "haru.final_mix.v1",
                "method": "ffmpeg_loudnorm_two_pass",
                "normalization_type": "linear",
                "input_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
                "target": {
                    "integrated_lufs": -14.0,
                    "true_peak_dbfs": -1.0,
                    "loudness_range_lu": 4.0
                }
            }
        });
        if let Some(assembly) = assembly {
            data["assembly"] = assembly;
        }
        Some(json!({
            "outcome": "ok",
            "code": "render_complete",
            "project": project.to_string_lossy(),
            "data": data
        }))
    }

    #[test]
    fn rerender_preservation_receipt_is_accepted_without_opening_job_schema() {
        let directory = tempdir().unwrap();
        let project = directory.path().join("demo");
        let job = "a".repeat(32);
        let mut result = json!({
            "outcome": "ok", "code": "render_started", "project": project,
            "data": {
                "schema": "haru.render_job.v2", "job_id": job, "project": "demo",
                "status": "running", "launcher": "portable-python", "epoch": 1,
                "revision": "b".repeat(64), "pid": 1234,
                "log": project.join("output/.staging").join(&job).join("worker.log"),
                "output": "output/final.mp4", "started_at": "2026-09-20T00:00:00Z",
                "previous_final_preserved": true
            }
        });
        assert!(super::render_in_progress(&Some(result.clone()), &project));
        result["data"]["previous_final_preserved"] = json!("true");
        assert!(!super::render_in_progress(&Some(result.clone()), &project));
        result["data"]["previous_final_preserved"] = json!(true);
        result["data"]["worker_path"] = json!("/tmp/untrusted");
        assert!(!super::render_in_progress(&Some(result), &project));
    }

    #[test]
    fn segmented_render_postcheck_rejects_missing_assembly_binding() {
        let directory = tempdir().unwrap();
        let project = directory.path().join("demo");
        fs::create_dir(&project).unwrap();
        fs::write(project.join("segment-plan.json"), "{}").unwrap();
        let returned = otherwise_valid_render(&project, None);
        assert!(!render_ready(&returned, &project));
    }

    #[test]
    fn segmented_render_postcheck_rejects_mismatched_assembly_digest() {
        let directory = tempdir().unwrap();
        let project = directory.path().join("demo");
        fs::create_dir_all(project.join("quality-review/segments")).unwrap();
        fs::write(project.join("segment-plan.json"), "{}").unwrap();
        fs::write(project.join("quality-review/segments/assembly.json"), "{}").unwrap();
        let returned = otherwise_valid_render(
            &project,
            Some(json!({
                "schema": "haru.segment_assembly.v1",
                "path": "quality-review/segments/assembly.json",
                "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
            })),
        );
        assert!(!render_ready(&returned, &project));
    }

    #[test]
    fn legacy_postcheck_rejects_a_segmented_binding_after_the_plan_disappears() {
        let directory = tempdir().unwrap();
        let project = directory.path().join("demo");
        fs::create_dir(&project).unwrap();
        let returned = otherwise_valid_render(
            &project,
            Some(json!({
                "schema": "haru.segment_assembly.v1",
                "path": "quality-review/segments/assembly.json",
                "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
            })),
        );
        assert!(!render_ready(&returned, &project));
    }
}
