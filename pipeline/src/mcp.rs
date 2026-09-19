use std::fs::{self, OpenOptions};
use std::io;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use fs2::FileExt;
use rmcp::{
    Json, ServerHandler,
    handler::server::{router::tool::ToolRouter, wrapper::Parameters},
    tool, tool_handler, tool_router,
};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::application::{
    self, AppResult, ApprovePublishRequest, ArtifactImportRequest, ArtifactStageRequest,
    ExportDeliveryRequest, JobMutationRequest, LeaseInput, PreparePublishApprovalRequest,
    ProcessExecutor, ProduceArtifactRequest, ProduceStagedArtifactRequest,
    PronunciationReviewRequest, ReplaceThumbnailRequest, ReviewAddRequest, ReviewResolutionRequest,
    RunNextRequest, SelfEvalFinding, SelfEvalReviewInput, VisualQaRequest,
};
use crate::lease_authority::LeaseAuthority;
use crate::runtime::{RuntimeAuthority, RuntimeBinding, ToolSurface};
use crate::store::{ProjectStore, StoreError, write_json_atomically};

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CreateInput {
    pub schema_version: u32,
    pub projects_root: String,
    pub project: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct SelectInput {
    pub schema_version: u32,
    pub projects_root: String,
    pub project: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ProjectInput {
    pub schema_version: u32,
    pub project_root: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct WorkspaceInput {
    pub schema_version: u32,
    pub workspace_root: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct RecordSelectionInput {
    pub schema_version: u32,
    pub project_root: String,
    pub cron_run_id: String,
    pub candidate_id: String,
    pub chosen_by: String,
    pub chosen_at: u64,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct LeaseClaimInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub ttl_seconds: u64,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct LeaseRenewInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub ttl_seconds: u64,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct LeaseReleaseInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct LeasedInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ExportDeliveryInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub idempotency_key: String,
    #[serde(default)]
    #[schemars(default)]
    pub diagnostic: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ArtifactStageInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub role: String,
    pub inbox_path: Option<String>,
    pub inline_text: Option<String>,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ArtifactImportInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub stage_id: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ProduceStagedArtifactInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub stage_id: String,
    pub artifact: String,
    pub produced_by: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct JobInput {
    pub schema_version: u32,
    pub project_root: String,
    pub job_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct JobLogsInput {
    pub schema_version: u32,
    pub project_root: String,
    pub job_id: String,
    pub max_bytes: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct JobMutationInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub job_id: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ReviewResolveInput {
    pub schema_version: u32,
    pub project_root: String,
    pub comment_id: String,
    pub status: String,
    pub expected_package_id: String,
    pub expected_asset_sha256: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ReviewAddInput {
    pub schema_version: u32,
    pub project_root: String,
    pub client_id: String,
    pub package_id: String,
    pub asset_id: String,
    pub asset_sha256: String,
    pub timestamp_seconds: Option<f64>,
    pub body: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct RunNextInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub runner: String,
    pub tools_root: Option<String>,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PreparePublishInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct VisualQaInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub action: String,
    pub reviewed_by: Option<String>,
    pub verdict: Option<String>,
    pub notes: Option<String>,
    /// Typed self-eval review provenance. Every field is a plain value: there
    /// is no caller-selected command target, tools root, or path in this surface,
    /// and the authority is `attestation_ref`, not these strings.
    pub reviewer_kind: Option<String>,
    pub provider: Option<String>,
    pub model: Option<String>,
    pub capability: Option<String>,
    pub findings: Option<Vec<SelfEvalFinding>>,
    pub attestation_ref: Option<String>,
    pub idempotency_key: String,
}

/// Carry the typed self-eval extras through as one unit. If a caller supplies
/// any of them the application sees a review submission, so handing one to an
/// action that has no business with it is refused rather than ignored -- and a
/// missing field arrives blank, which the application also refuses.
fn self_eval_review(input: &VisualQaInput) -> Option<SelfEvalReviewInput> {
    if input.reviewer_kind.is_none()
        && input.provider.is_none()
        && input.model.is_none()
        && input.capability.is_none()
        && input.findings.is_none()
        && input.attestation_ref.is_none()
    {
        return None;
    }
    Some(SelfEvalReviewInput {
        reviewer_kind: input.reviewer_kind.clone().unwrap_or_default(),
        provider: input.provider.clone().unwrap_or_default(),
        model: input.model.clone().unwrap_or_default(),
        capability: input.capability.clone().unwrap_or_default(),
        findings: input.findings.clone().unwrap_or_default(),
        attestation_ref: input.attestation_ref.clone().unwrap_or_default(),
    })
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PronunciationReviewInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub reviewed_by: String,
    pub verdict: String,
    pub notes: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ProduceArtifactInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub artifact: String,
    pub source_file: String,
    pub produced_by: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ApprovePublishInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub attestation_ref: String,
    pub override_reason: Option<String>,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PreparePublishApprovalInput {
    pub schema_version: u32,
    pub project_root: String,
    pub override_reason: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct PublishInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ReplaceThumbnailInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub updated_by: String,
    pub idempotency_key: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct ReconcileUploadInput {
    pub schema_version: u32,
    pub project_root: String,
    pub owner: String,
    pub lease_id: String,
    pub idempotency_key: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub override_attestation_ref: Option<String>,
}

/// The stored outcome of one keyed mutation.
///
/// It records the runtime that executed it, not just what was executed: a
/// result produced by one runtime is not evidence about another, so a replay
/// arriving at a different runtime is refused instead of being handed back
/// stamped with the current identity.
#[derive(Debug, Clone, Serialize, Deserialize)]
struct OperationReceipt {
    schema_version: u32,
    operation: String,
    runtime_id: String,
    binary_sha256: String,
    input_digest: String,
    result: Option<AppResult>,
}

/// Bumped when the receipt gained its runtime binding. A receipt at any other
/// version cannot be shown to describe this request, so its key is refused.
const OPERATION_RECEIPT_VERSION: u32 = 2;

/// The exact tool surface this build serves, read back from the router so the
/// identity cannot claim a surface the server does not actually expose.
pub fn tool_surface() -> ToolSurface {
    let mut tools = HvpService::tool_router().list_all();
    tools.sort_by(|left, right| left.name.cmp(&right.name));
    let mut names = Vec::with_capacity(tools.len());
    let mut digest = Sha256::new();
    for tool in &tools {
        names.push(tool.name.to_string());
        digest.update(tool.name.as_bytes());
        digest.update([0]);
        digest.update(tool.description.as_deref().unwrap_or_default().as_bytes());
        digest.update([0]);
        digest.update(serde_json::to_vec(&tool.input_schema).unwrap_or_default());
        digest.update([0]);
        digest.update(serde_json::to_vec(&tool.output_schema).unwrap_or_default());
        digest.update([0]);
    }
    ToolSurface {
        names,
        digest: format!("sha256:{:x}", digest.finalize()),
    }
}

#[derive(Debug, Clone)]
pub struct HvpService {
    repo_root: PathBuf,
    runtime: Arc<RuntimeAuthority>,
    lease_authority: Arc<LeaseAuthority>,
    tool_router: ToolRouter<Self>,
}

impl HvpService {
    /// An unpromoted, in-process server. Results still carry a runtime
    /// identity; it reports `promoted: false` rather than pretending.
    pub fn new(repo_root: impl AsRef<Path>) -> Self {
        let repo_root = repo_root.as_ref().to_path_buf();
        let runtime =
            RuntimeAuthority::development(&repo_root, crate::runtime::state_root(), tool_surface())
                .expect("MCP tool surface does not match pipeline/runtime-manifest.json");
        Self::with_authority(repo_root, runtime)
    }

    pub fn with_authority(repo_root: impl AsRef<Path>, runtime: RuntimeAuthority) -> Self {
        let lease_authority =
            LeaseAuthority::new(runtime.state_root(), &runtime.identity().runtime_id);
        Self {
            repo_root: repo_root.as_ref().to_path_buf(),
            runtime: Arc::new(runtime),
            lease_authority: Arc::new(lease_authority),
            tool_router: Self::tool_router(),
        }
    }

    pub fn runtime(&self) -> &RuntimeAuthority {
        &self.runtime
    }

    fn refuse(&self, result: AppResult) -> Json<AppResult> {
        Json(self.runtime.stamp(result))
    }

    /// Everything that must hold before the application layer is entered: the
    /// input version, the project's runtime compatibility, and -- for anything
    /// that writes -- the security capability floor. An incompatible project
    /// short-circuits here, so no gate is evaluated and no executor is spawned.
    fn gate(&self, schema_version: u32, project_root: &str, mutation: bool) -> Option<AppResult> {
        if schema_version != 1 {
            return Some(AppResult::invalid_input());
        }
        let project = Path::new(project_root);
        if let Some(blocked) = self.runtime.project_block(project) {
            return Some(blocked);
        }
        if mutation && let Some(blocked) = self.runtime.mutation_block(project) {
            return Some(blocked);
        }
        None
    }

    fn lease_input(
        &self,
        project: &Path,
        owner: &str,
        lease_id: &str,
    ) -> Result<LeaseInput, Box<AppResult>> {
        match self.lease_authority.resolve(project, owner, lease_id) {
            Ok(resolved) => Ok(LeaseInput {
                owner: resolved.owner,
                lease_id: resolved.lease_id,
                generation: resolved.generation,
                capability: resolved.capability,
            }),
            Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
                Err(Box::new(AppResult::blocked("lease_invalid", project)))
            }
            Err(_) => Err(Box::new(AppResult::error("internal_error", Some(project)))),
        }
    }

    async fn mutate_job(&self, action: &'static str, input: JobMutationInput) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            let operation = if action == "cancel" {
                "job_cancel"
            } else {
                "job_resume"
            };
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                operation,
                &input,
                || {
                    application::mutate_job(
                        action,
                        &JobMutationRequest {
                            project_root: project.clone(),
                            lease: lease.clone(),
                            job_id: input.job_id.clone(),
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }
}

#[tool_router(router = tool_router)]
impl HvpService {
    #[tool(name = "create", description = "Create a canonical HVP project.")]
    async fn create(&self, Parameters(input): Parameters<CreateInput>) -> Json<AppResult> {
        if input.schema_version != 1 {
            return self.refuse(AppResult::invalid_input());
        }
        if let Some(blocked) = self.runtime.mutation_block(Path::new(&input.projects_root)) {
            return self.refuse(blocked);
        }
        let binding = self.runtime.binding();
        self.blocking_mutation(input.projects_root.clone(), move || {
            let root = PathBuf::from(&input.projects_root);
            run_idempotent(
                &binding,
                &root,
                &input.idempotency_key,
                "create",
                &input,
                || application::create(&root, &input.project),
            )
        })
        .await
    }

    #[tool(
        name = "select",
        description = "Select and validate an existing HVP project."
    )]
    async fn select(&self, Parameters(input): Parameters<SelectInput>) -> Json<AppResult> {
        if input.schema_version != 1 {
            return self.refuse(AppResult::invalid_input());
        }
        // Selection resolves a path; it evaluates no gate and writes nothing,
        // so it stays available on a project this runtime may not serve --
        // that is how a caller discovers the incompatibility at all.
        self.blocking(move || application::select(Path::new(&input.projects_root), &input.project))
            .await
    }

    #[tool(
        name = "workspace_info",
        description = "Read initialized workspace identity and project count."
    )]
    async fn workspace_info(
        &self,
        Parameters(input): Parameters<WorkspaceInput>,
    ) -> Json<AppResult> {
        if input.schema_version != 1 {
            return self.refuse(AppResult::invalid_input());
        }
        self.blocking(move || application::workspace_info(Path::new(&input.workspace_root)))
            .await
    }

    #[tool(
        name = "project_list",
        description = "List direct projects in an initialized workspace without exposing artifact paths."
    )]
    async fn project_list(&self, Parameters(input): Parameters<WorkspaceInput>) -> Json<AppResult> {
        if input.schema_version != 1 {
            return self.refuse(AppResult::invalid_input());
        }
        self.blocking(move || application::project_list(Path::new(&input.workspace_root)))
            .await
    }

    #[tool(
        name = "status",
        description = "Read canonical project state and blockers."
    )]
    async fn status(&self, Parameters(input): Parameters<ProjectInput>) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, false) {
            return self.refuse(blocked);
        }
        let repo_root = self.repo_root.clone();
        let state_root = self.runtime.state_root().to_path_buf();
        self.blocking(move || {
            let mut result = application::status(
                Path::new(&input.project_root),
                &repo_root,
                &mut ProcessExecutor,
            );
            if result.code == "status"
                && let Some(data) = result
                    .data
                    .as_mut()
                    .and_then(serde_json::Value::as_object_mut)
            {
                // Read-only operator state, independent of project readiness.
                // Never inspect OAuth material or lift the hold from status.
                data.insert(
                    "upload_hold".to_owned(),
                    serde_json::json!(crate::runtime::upload_hold(&state_root)),
                );
            }
            result
        })
        .await
    }

    #[tool(
        name = "delivery_status",
        description = "Run fresh local technical delivery checks without claiming human or publication approval."
    )]
    async fn delivery_status(
        &self,
        Parameters(input): Parameters<ProjectInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, false) {
            return self.refuse(blocked);
        }
        let repo_root = self.repo_root.clone();
        self.blocking(move || {
            application::delivery_status(
                Path::new(&input.project_root),
                &repo_root,
                &mut ProcessExecutor,
            )
        })
        .await
    }

    #[tool(
        name = "export_delivery",
        description = "Export one immutable local delivery bundle under the operator-configured destination."
    )]
    async fn export_delivery(
        &self,
        Parameters(input): Parameters<ExportDeliveryInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "export_delivery",
                &input,
                || {
                    application::export_delivery(
                        &ExportDeliveryRequest {
                            project_root: project.clone(),
                            lease: lease.clone(),
                            idempotency_key: input.idempotency_key.clone(),
                            diagnostic: input.diagnostic,
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }

    #[tool(
        name = "job_status",
        description = "Read the bounded public status of one durable render job."
    )]
    async fn job_status(&self, Parameters(input): Parameters<JobInput>) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, false) {
            return self.refuse(blocked);
        }
        let repo_root = self.repo_root.clone();
        self.blocking(move || {
            application::job_status(
                Path::new(&input.project_root),
                &repo_root,
                &input.job_id,
                &mut ProcessExecutor,
            )
        })
        .await
    }

    #[tool(
        name = "job_logs",
        description = "Read a bounded, redacted tail of one durable render job log."
    )]
    async fn job_logs(&self, Parameters(input): Parameters<JobLogsInput>) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, false) {
            return self.refuse(blocked);
        }
        let repo_root = self.repo_root.clone();
        self.blocking(move || {
            application::job_logs(
                Path::new(&input.project_root),
                &repo_root,
                &input.job_id,
                input.max_bytes,
                &mut ProcessExecutor,
            )
        })
        .await
    }

    #[tool(
        name = "job_cancel",
        description = "Request cancellation of one durable render job under the active project lease."
    )]
    async fn job_cancel(&self, Parameters(input): Parameters<JobMutationInput>) -> Json<AppResult> {
        self.mutate_job("cancel", input).await
    }

    #[tool(
        name = "job_resume",
        description = "Resume one interrupted durable render job under the active project lease."
    )]
    async fn job_resume(&self, Parameters(input): Parameters<JobMutationInput>) -> Json<AppResult> {
        self.mutate_job("resume", input).await
    }

    #[tool(
        name = "review_add",
        description = "Add one digest-bound local review comment without creating formal approval."
    )]
    async fn review_add(&self, Parameters(input): Parameters<ReviewAddInput>) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "review_add",
                &input,
                || {
                    application::review_add(
                        &ReviewAddRequest {
                            project_root: project.clone(),
                            client_id: input.client_id.clone(),
                            package_id: input.package_id.clone(),
                            asset_id: input.asset_id.clone(),
                            asset_sha256: input.asset_sha256.clone(),
                            timestamp_seconds: input.timestamp_seconds,
                            body: input.body.clone(),
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }

    #[tool(
        name = "review_feedback",
        description = "Read current local review comments without treating feedback as formal approval."
    )]
    async fn review_feedback(
        &self,
        Parameters(input): Parameters<ProjectInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, false) {
            return self.refuse(blocked);
        }
        let repo_root = self.repo_root.clone();
        self.blocking(move || {
            application::review_feedback(
                Path::new(&input.project_root),
                &repo_root,
                &mut ProcessExecutor,
            )
        })
        .await
    }

    #[tool(
        name = "review_resolve",
        description = "Resolve or reopen one digest-bound local review comment under the workspace review lock."
    )]
    async fn review_resolve(
        &self,
        Parameters(input): Parameters<ReviewResolveInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "review_resolve",
                &input,
                || {
                    application::review_resolve(
                        &ReviewResolutionRequest {
                            project_root: project.clone(),
                            comment_id: input.comment_id.clone(),
                            status: input.status.clone(),
                            expected_package_id: input.expected_package_id.clone(),
                            expected_asset_sha256: input.expected_asset_sha256.clone(),
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }

    #[tool(
        name = "artifact_stage",
        description = "Stage one bounded workspace inbox file or inline text blob for a typed project role."
    )]
    async fn artifact_stage(
        &self,
        Parameters(input): Parameters<ArtifactStageInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "artifact_stage",
                &input,
                || {
                    application::artifact_stage(
                        &ArtifactStageRequest {
                            project_root: project.clone(),
                            lease: lease.clone(),
                            role: input.role.clone(),
                            inbox_path: input.inbox_path.clone(),
                            inline_text: input.inline_text.clone(),
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }

    #[tool(
        name = "artifact_import",
        description = "Import one verified staged blob into its non-canonical project imports namespace."
    )]
    async fn artifact_import(
        &self,
        Parameters(input): Parameters<ArtifactImportInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "artifact_import",
                &input,
                || {
                    application::artifact_import(
                        &ArtifactImportRequest {
                            project_root: project.clone(),
                            lease: lease.clone(),
                            stage_id: input.stage_id.clone(),
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }

    #[tool(
        name = "produce_staged_artifact",
        description = "Promote a verified staged text or JSON blob into an explicitly allowlisted agent-authorable artifact."
    )]
    async fn produce_staged_artifact(
        &self,
        Parameters(input): Parameters<ProduceStagedArtifactInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "produce_staged_artifact",
                &input,
                || {
                    application::produce_staged_artifact(
                        &ProduceStagedArtifactRequest {
                            project_root: project.clone(),
                            lease: lease.clone(),
                            stage_id: input.stage_id.clone(),
                            artifact: input.artifact.clone(),
                            produced_by: input.produced_by.clone(),
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }

    #[tool(
        name = "artifact_index",
        description = "Read the canonical project artifact index."
    )]
    async fn artifact_index(&self, Parameters(input): Parameters<ProjectInput>) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, false) {
            return self.refuse(blocked);
        }
        let repo_root = self.repo_root.clone();
        self.blocking(move || {
            application::artifact_index(
                Path::new(&input.project_root),
                &repo_root,
                &mut ProcessExecutor,
            )
        })
        .await
    }

    #[tool(
        name = "record_selection",
        description = "Record the immutable human topic selection for a canonical project."
    )]
    async fn record_selection(
        &self,
        Parameters(input): Parameters<RecordSelectionInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "record_selection",
                &input,
                || {
                    application::record_selection(
                        &project,
                        &input.cron_run_id,
                        &input.candidate_id,
                        &input.chosen_by,
                        input.chosen_at,
                    )
                },
            )
        })
        .await
    }
    #[tool(name = "lease_claim", description = "Claim an opaque project lease.")]
    async fn lease_claim(&self, Parameters(input): Parameters<LeaseClaimInput>) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        if input.ttl_seconds == 0 || input.owner.trim().is_empty() {
            return self.refuse(AppResult::invalid_input());
        }
        let binding = self.runtime.binding();
        let authority = Arc::clone(&self.lease_authority);
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "lease_claim",
                &input,
                || match authority.claim(
                    &project,
                    &input.owner,
                    Duration::from_secs(input.ttl_seconds),
                    &input.idempotency_key,
                    SystemTime::now(),
                ) {
                    Ok(lease) => AppResult::ok(
                        "lease_claimed",
                        &project,
                        Some(serde_json::json!({
                            "schema": "haru.project_lease.v2",
                            "owner": lease.owner,
                            "lease_id": lease.lease_id,
                            "generation": lease.generation,
                            "claimed_at": lease.claimed_at,
                            "expires_at": lease.expires_at,
                            "active": true,
                        })),
                    ),
                    Err(StoreError::LeaseHeld { owner, expires_at }) => AppResult::error_with_data(
                        "lease_held",
                        Some(&project),
                        Some(serde_json::json!({"owner": owner, "expires_at": expires_at})),
                    ),
                    Err(StoreError::InvalidOwner | StoreError::InvalidTtl) => {
                        AppResult::invalid_input()
                    }
                    Err(_) => AppResult::error("internal_error", Some(&project)),
                },
            )
        })
        .await
    }

    #[tool(
        name = "lease_renew",
        description = "Renew the caller's active opaque project lease."
    )]
    async fn lease_renew(&self, Parameters(input): Parameters<LeaseRenewInput>) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        if input.ttl_seconds == 0 {
            return self.refuse(AppResult::invalid_input());
        }
        let binding = self.runtime.binding();
        let authority = Arc::clone(&self.lease_authority);
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "lease_renew",
                &input,
                || match authority.renew(
                    &project,
                    &input.owner,
                    &input.lease_id,
                    Duration::from_secs(input.ttl_seconds),
                    SystemTime::now(),
                ) {
                    Ok(lease) => AppResult::ok(
                        "lease_renewed",
                        &project,
                        Some(serde_json::json!({
                            "schema": "haru.project_lease.v2",
                            "owner": lease.owner,
                            "lease_id": lease.lease_id,
                            "generation": lease.generation,
                            "claimed_at": lease.claimed_at,
                            "expires_at": lease.expires_at,
                            "active": true,
                        })),
                    ),
                    Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
                        AppResult::blocked("lease_invalid", &project)
                    }
                    Err(_) => AppResult::error("internal_error", Some(&project)),
                },
            )
        })
        .await
    }

    #[tool(
        name = "lease_status",
        description = "Read the current non-authorizing project lease status."
    )]
    async fn lease_status(&self, Parameters(input): Parameters<ProjectInput>) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, false) {
            return self.refuse(blocked);
        }
        self.blocking(move || {
            let project = PathBuf::from(&input.project_root);
            match ProjectStore::new(&project).current_lease() {
                Ok(Some(lease)) => AppResult::ok(
                    "lease_active",
                    &project,
                    Some(serde_json::json!({
                        "schema": "haru.project_lease.v2",
                        "owner": lease.owner,
                        "lease_id": lease.lease_id,
                        "generation": lease.generation,
                        "claimed_at": lease.claimed_at,
                        "expires_at": lease.expires_at,
                        "active": true,
                    })),
                ),
                Ok(None) => AppResult::ok(
                    "lease_absent",
                    &project,
                    Some(serde_json::json!({"schema": "haru.project_lease.v2", "active": false})),
                ),
                Err(_) => AppResult::error("internal_error", Some(&project)),
            }
        })
        .await
    }

    #[tool(
        name = "lease_release",
        description = "Release the caller's active opaque project lease."
    )]
    async fn lease_release(
        &self,
        Parameters(input): Parameters<LeaseReleaseInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let binding = self.runtime.binding();
        let authority = Arc::clone(&self.lease_authority);
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "lease_release",
                &input,
                || match authority.release(
                    &project,
                    &input.owner,
                    &input.lease_id,
                    SystemTime::now(),
                ) {
                    Ok(()) => AppResult::ok(
                        "lease_released",
                        &project,
                        Some(serde_json::json!({
                            "schema": "haru.project_lease.v2",
                            "lease_id": input.lease_id,
                            "active": false,
                        })),
                    ),
                    Err(StoreError::LeaseMismatch | StoreError::LeaseExpired) => {
                        AppResult::blocked("lease_invalid", &project)
                    }
                    Err(_) => AppResult::error("internal_error", Some(&project)),
                },
            )
        })
        .await
    }

    #[tool(
        name = "run_next",
        description = "Run one deterministic pipeline gate using the canonical application contract."
    )]
    async fn run_next(&self, Parameters(input): Parameters<RunNextInput>) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "run_next",
                &input,
                || {
                    application::run_next(
                        &RunNextRequest {
                            project_root: project.clone(),
                            lease: lease.clone(),
                            runner: input.runner.clone(),
                            tools_root: input.tools_root.as_ref().map(PathBuf::from),
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }

    #[tool(
        name = "verify",
        description = "Verify a project through the canonical fail-closed verifier."
    )]
    async fn verify(&self, Parameters(input): Parameters<ProjectInput>) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, false) {
            return self.refuse(blocked);
        }
        let repo_root = self.repo_root.clone();
        self.blocking(move || {
            application::verify(
                Path::new(&input.project_root),
                &repo_root,
                &mut ProcessExecutor,
            )
        })
        .await
    }

    #[tool(
        name = "visual_qa",
        description = "Sample/review the final video, record a digest-bound segment-review verdict, or submit an attested render self-evaluation review."
    )]
    async fn visual_qa(&self, Parameters(input): Parameters<VisualQaInput>) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "visual_qa",
                &input,
                || {
                    application::visual_qa(
                        &VisualQaRequest {
                            project_root: project.clone(),
                            lease: lease.clone(),
                            action: input.action.clone(),
                            reviewed_by: input.reviewed_by.clone(),
                            verdict: input.verdict.clone(),
                            notes: input.notes.clone(),
                            self_eval: self_eval_review(&input),
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }

    #[tool(
        name = "pronunciation_review",
        description = "Record the human listening verdict bound to the current G2P plan and probe audio."
    )]
    async fn pronunciation_review(
        &self,
        Parameters(input): Parameters<PronunciationReviewInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "pronunciation_review",
                &input,
                || {
                    application::pronunciation_review(
                        &PronunciationReviewRequest {
                            project_root: project.clone(),
                            lease: lease.clone(),
                            reviewed_by: input.reviewed_by.clone(),
                            verdict: input.verdict.clone(),
                            notes: input.notes.clone(),
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }

    #[tool(
        name = "produce_artifact",
        description = "Validate and atomically promote one staged agent-authored canonical artifact."
    )]
    async fn produce_artifact(
        &self,
        Parameters(input): Parameters<ProduceArtifactInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "produce_artifact",
                &input,
                || {
                    application::produce_artifact(
                        &ProduceArtifactRequest {
                            project_root: project.clone(),
                            lease: lease.clone(),
                            artifact: input.artifact.clone(),
                            source_file: PathBuf::from(&input.source_file),
                            produced_by: input.produced_by.clone(),
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }

    #[tool(
        name = "approve_publish",
        description = "Record the operator's digest-bound approval for the current final video."
    )]
    async fn approve_publish(
        &self,
        Parameters(input): Parameters<ApprovePublishInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "approve_publish",
                &input,
                || {
                    application::approve_publish(
                        &ApprovePublishRequest {
                            project_root: project.clone(),
                            lease: lease.clone(),
                            attestation_ref: input.attestation_ref.clone(),
                            override_reason: input.override_reason.clone(),
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }

    #[tool(
        name = "prepare_publish_approval",
        description = "Prepare a read-only, digest-bound publish approval signing request."
    )]
    async fn prepare_publish_approval(
        &self,
        Parameters(input): Parameters<PreparePublishApprovalInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, false) {
            return self.refuse(blocked);
        }
        let repo_root = self.repo_root.clone();
        self.blocking(move || {
            application::prepare_publish_approval(
                &PreparePublishApprovalRequest {
                    project_root: PathBuf::from(&input.project_root),
                    override_reason: input.override_reason.clone(),
                },
                &repo_root,
                &mut ProcessExecutor,
            )
        })
        .await
    }

    #[tool(
        name = "prepare_publish",
        description = "Prepare a publish pack without uploading or publishing."
    )]
    async fn prepare_publish(
        &self,
        Parameters(input): Parameters<PreparePublishInput>,
    ) -> Json<AppResult> {
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "prepare_publish",
                &input,
                || application::prepare_publish(&project, &repo_root, &lease, &mut ProcessExecutor),
            )
        })
        .await
    }

    #[tool(
        name = "publish",
        description = "Upload the fully approved video and initial thumbnail as unlisted."
    )]
    async fn publish(&self, Parameters(input): Parameters<PublishInput>) -> Json<AppResult> {
        if input.schema_version != 1 {
            return self.refuse(AppResult::invalid_input());
        }
        // The operational hold comes first, ahead of the lease secret, the
        // verifier and the HTTP-capable uploader. Nothing about a held upload
        // should require credential resolution to discover.
        if let Some(held) = self
            .runtime
            .upload_hold_block(Path::new(&input.project_root))
        {
            return self.refuse(held);
        }
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            application::publish(
                &application::PublishRequest {
                    project_root: PathBuf::from(input.project_root),
                    lease,
                    idempotency_key: input.idempotency_key,
                    runtime: binding,
                },
                &repo_root,
                &mut ProcessExecutor,
            )
        })
        .await
    }

    #[tool(
        name = "replace_thumbnail",
        description = "Replace the thumbnail of the already uploaded YouTube video with the current canonical cover."
    )]
    async fn replace_thumbnail(
        &self,
        Parameters(input): Parameters<ReplaceThumbnailInput>,
    ) -> Json<AppResult> {
        if input.schema_version != 1 {
            return self.refuse(AppResult::invalid_input());
        }
        if let Some(held) = self
            .runtime
            .upload_hold_block(Path::new(&input.project_root))
        {
            return self.refuse(held);
        }
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            let project = PathBuf::from(&input.project_root);
            run_idempotent(
                &binding,
                &project,
                &input.idempotency_key,
                "replace_thumbnail",
                &input,
                || {
                    application::replace_thumbnail(
                        &ReplaceThumbnailRequest {
                            project_root: project.clone(),
                            lease: lease.clone(),
                            updated_by: input.updated_by.clone(),
                            idempotency_key: input.idempotency_key.clone(),
                            runtime: binding.clone(),
                        },
                        &repo_root,
                        &mut ProcessExecutor,
                    )
                },
            )
        })
        .await
    }

    #[tool(
        name = "reconcile_upload",
        description = "Reconcile a fenced YouTube upload by authenticated remote readback. Does not create a second session or video."
    )]
    async fn reconcile_upload(
        &self,
        Parameters(input): Parameters<ReconcileUploadInput>,
    ) -> Json<AppResult> {
        if input.schema_version != 1 {
            return self.refuse(AppResult::invalid_input());
        }
        if let Some(blocked) = self.gate(input.schema_version, &input.project_root, true) {
            return self.refuse(blocked);
        }
        let project = PathBuf::from(&input.project_root);
        let lease = match self.lease_input(&project, &input.owner, &input.lease_id) {
            Ok(lease) => lease,
            Err(blocked) => return self.refuse(*blocked),
        };
        let repo_root = self.repo_root.clone();
        let binding = self.runtime.binding();
        self.blocking_mutation(input.project_root.clone(), move || {
            application::reconcile_upload(
                &application::ReconcileUploadRequest {
                    project_root: PathBuf::from(input.project_root),
                    lease,
                    idempotency_key: input.idempotency_key,
                    override_attestation_ref: input.override_attestation_ref,
                    runtime: binding,
                },
                &repo_root,
                &mut ProcessExecutor,
            )
        })
        .await
    }
}

#[tool_handler(
    router = self.tool_router,
    name = "video-studio",
    instructions = "Use version 1 inputs. Paths must be explicit except for OAuth credentials: publishing derives the canonical logical reference internally. Mutation tools require an idempotency key; never pass credential values or paths."
)]
impl ServerHandler for HvpService {}

impl HvpService {
    async fn blocking(
        &self,
        action: impl FnOnce() -> AppResult + Send + 'static,
    ) -> Json<AppResult> {
        let runtime = self.runtime.clone();
        let result = tokio::task::spawn_blocking(action)
            .await
            .unwrap_or_else(|_| AppResult::internal_error(None));
        Json(runtime.stamp(result))
    }

    async fn blocking_mutation(
        &self,
        project_root: String,
        action: impl FnOnce() -> AppResult + Send + 'static,
    ) -> Json<AppResult> {
        let runtime = self.runtime.clone();
        let result = tokio::task::spawn_blocking(move || {
            let project = PathBuf::from(project_root);
            let _workspace_guard = match workspace_mutation_guard(&project) {
                Ok(guard) => guard,
                Err(error) => {
                    return runtime.stamp(AppResult::blocked_with_data(
                        "workspace_barrier_unavailable",
                        &project,
                        Some(serde_json::json!({"detail": error.to_string()})),
                    ));
                }
            };
            let _guard = match runtime.mutation_guard(&project) {
                Ok(guard) => guard,
                Err(blocked) => return runtime.stamp(*blocked),
            };
            runtime.stamp(action())
        })
        .await
        .unwrap_or_else(|_| self.runtime.stamp(AppResult::internal_error(None)));
        Json(result)
    }
}

fn workspace_mutation_guard(path: &Path) -> io::Result<Option<fs::File>> {
    let canonical = path.canonicalize()?;
    let workspace = if canonical.file_name().and_then(|name| name.to_str()) == Some("projects") {
        canonical.parent().map(Path::to_path_buf)
    } else if canonical
        .parent()
        .and_then(Path::file_name)
        .and_then(|name| name.to_str())
        == Some("projects")
    {
        canonical
            .parent()
            .and_then(Path::parent)
            .map(Path::to_path_buf)
    } else {
        None
    };
    let Some(workspace) = workspace else {
        return Ok(None);
    };
    let manifest = workspace.join("workspace.json");
    match fs::symlink_metadata(&manifest) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
        Ok(metadata) if metadata.file_type().is_symlink() || !metadata.is_file() => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "workspace manifest is unsafe",
            ));
        }
        Ok(_) => {}
    }
    let value: serde_json::Value =
        serde_json::from_slice(&fs::read(&manifest)?).map_err(|error| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                format!("workspace manifest: {error}"),
            )
        })?;
    if value.get("schema").and_then(serde_json::Value::as_str) != Some("video_studio.workspace.v1")
        || value
            .get("workspace_id")
            .and_then(serde_json::Value::as_str)
            .and_then(|value| uuid::Uuid::parse_str(value).ok())
            .is_none()
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "workspace manifest is invalid",
        ));
    }
    let state = workspace.join(".video-studio");
    let metadata = fs::symlink_metadata(&state)?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "workspace private state is unsafe",
        ));
    }
    let lock_path = state.join("workspace-barrier.lock");
    if fs::symlink_metadata(&lock_path)
        .is_ok_and(|metadata| metadata.file_type().is_symlink() || !metadata.is_file())
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "workspace barrier is unsafe",
        ));
    }
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(&lock_path)?;
    #[cfg(unix)]
    fs::set_permissions(&lock_path, fs::Permissions::from_mode(0o600))?;
    lock.lock_shared()?;
    Ok(Some(lock))
}

/// Execute one keyed mutation at most once, and hand a duplicate call the
/// result the *same* runtime recorded. `binding` is the identity that is about
/// to execute, so both the stored receipt and the digest its inputs are bound
/// to name a runtime; a replay on a different runtime is refused rather than
/// answered with another runtime's result wearing the current stamp.
fn run_idempotent(
    binding: &RuntimeBinding,
    root: &Path,
    key: &str,
    operation: &str,
    input: &impl Serialize,
    action: impl FnOnce() -> AppResult,
) -> AppResult {
    if !valid_idempotency_key(key) {
        return AppResult::invalid_input();
    }
    let Ok(root) = application::direct_directory(root) else {
        return AppResult::invalid_input();
    };
    let Ok(input) = serde_json::to_vec(input) else {
        return AppResult::internal_error(Some(&root));
    };
    let mut binder = Sha256::new();
    for field in [
        operation.as_bytes(),
        binding.runtime_id.as_bytes(),
        binding.binary_sha256.as_bytes(),
        input.as_slice(),
    ] {
        binder.update(field);
        binder.update([0]);
    }
    let input_digest = format!("sha256:{:x}", binder.finalize());
    let state = root.join(".hvp");
    if fs::create_dir_all(&state).is_err() {
        return AppResult::internal_error(Some(&root));
    }
    let lock_path = state.join("mcp-operations.lock");
    let Ok(lock) = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .open(lock_path)
    else {
        return AppResult::internal_error(Some(&root));
    };
    // ponytail: one lock per project/root; split by key only if MCP mutation throughput matters.
    if lock.lock_exclusive().is_err() {
        return AppResult::internal_error(Some(&root));
    }

    let receipts = state.join("mcp-operations");
    let receipt_path = receipts.join(format!("{key}.json"));
    match fs::read(&receipt_path) {
        Ok(bytes) => {
            let Ok(stored) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
                return AppResult::internal_error(Some(&root));
            };
            if stored
                .get("schema_version")
                .and_then(serde_json::Value::as_u64)
                != Some(u64::from(OPERATION_RECEIPT_VERSION))
            {
                return AppResult::idempotency_conflict(Some(&root));
            }
            let Ok(receipt) = serde_json::from_value::<OperationReceipt>(stored) else {
                return AppResult::internal_error(Some(&root));
            };
            // Attribution before comparison: a receipt written by another
            // runtime is not this runtime's result to return, whatever the
            // inputs were.
            if receipt.runtime_id != binding.runtime_id
                || receipt.binary_sha256 != binding.binary_sha256
            {
                return AppResult::blocked_with_data(
                    "idempotency_runtime_mismatch",
                    &root,
                    Some(serde_json::json!({
                        "schema": "haru.idempotency_runtime_mismatch.v1",
                        "operation": receipt.operation,
                        "recorded_runtime_id": receipt.runtime_id,
                        "recorded_binary_sha256": receipt.binary_sha256,
                        "current_runtime_id": binding.runtime_id,
                        "current_binary_sha256": binding.binary_sha256,
                        "remedy": "replay a mutation on the runtime that executed it, or issue a new idempotency key",
                    })),
                );
            }
            if receipt.operation != operation || receipt.input_digest != input_digest {
                return AppResult::idempotency_conflict(Some(&root));
            }
            return receipt
                .result
                .unwrap_or_else(|| AppResult::blocked("idempotency_incomplete", &root));
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return AppResult::internal_error(Some(&root)),
    }

    let mut receipt = OperationReceipt {
        schema_version: OPERATION_RECEIPT_VERSION,
        operation: operation.to_owned(),
        runtime_id: binding.runtime_id.clone(),
        binary_sha256: binding.binary_sha256.clone(),
        input_digest,
        result: None,
    };
    if write_json_atomically(&receipts, &format!("{key}.json"), &receipt).is_err() {
        return AppResult::internal_error(Some(&root));
    }
    let result = action();
    receipt.result = Some(result.clone());
    if write_json_atomically(&receipts, &format!("{key}.json"), &receipt).is_err() {
        AppResult::internal_error(Some(&root))
    } else {
        result
    }
}

fn valid_idempotency_key(key: &str) -> bool {
    !key.is_empty()
        && key.len() <= 128
        && key
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
}
