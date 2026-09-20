use pipeline::mcp::{self, HvpService};
use pipeline::runtime::RuntimeAuthority;
use rmcp::{ServiceExt, transport::stdio};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // These roots are development test seams. A server reached through the
    // stable launcher must never let a client redirect the independent review
    // ledger or the attestation store. This runs before Tokio creates worker
    // threads: Rust 2024 requires the explicit unsafe acknowledgement because
    // process-environment mutation races with concurrent environment access.
    drop_review_authority_seams();
    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()?
        .block_on(serve())
}

fn drop_review_authority_seams() {
    unsafe {
        std::env::remove_var("HARU_VIDEO_STUDIO_SELF_EVAL_STATE_ROOT");
        std::env::remove_var("HARU_VIDEO_STUDIO_SELF_EVAL_ATTESTATION_ROOT");
    }
}

async fn serve() -> Result<(), Box<dyn std::error::Error>> {
    let binary = std::env::current_exe()?;
    // Nothing is served before the runtime proves what it is: a promoted
    // release verifies its receipt, closure, binary digest, contract versions,
    // capability floor and tool surface; a development build must still hash
    // to the checkout it was built from.
    let runtime = match RuntimeAuthority::resolve(&binary, mcp::tool_surface()) {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("hvp-mcp refused to start: {error}");
            std::process::exit(5);
        }
    };
    let repo_root = runtime.repo_root().to_path_buf();
    HvpService::with_authority(repo_root, runtime)
        .serve(stdio())
        .await?
        .waiting()
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;

    use super::drop_review_authority_seams;

    const STATE_ROOT: &str = "HARU_VIDEO_STUDIO_SELF_EVAL_STATE_ROOT";
    const ATTESTATION_ROOT: &str = "HARU_VIDEO_STUDIO_SELF_EVAL_ATTESTATION_ROOT";

    #[test]
    fn startup_drops_review_authority_seams_before_runtime_construction() {
        // This is the real startup operation, exercised in the test process
        // before any Tokio runtime is built. The values are restored so this
        // narrow test cannot affect another test in the binary.
        let state_before = std::env::var_os(STATE_ROOT);
        let attestation_before = std::env::var_os(ATTESTATION_ROOT);
        unsafe {
            std::env::set_var(STATE_ROOT, "relative-untrusted-state");
            std::env::set_var(ATTESTATION_ROOT, "relative-untrusted-attestation");
        }
        drop_review_authority_seams();
        assert_eq!(std::env::var_os(STATE_ROOT), None);
        assert_eq!(std::env::var_os(ATTESTATION_ROOT), None);
        unsafe {
            restore(STATE_ROOT, state_before);
            restore(ATTESTATION_ROOT, attestation_before);
        }
    }

    unsafe fn restore(name: &str, value: Option<OsString>) {
        match value {
            Some(value) => unsafe { std::env::set_var(name, value) },
            None => unsafe { std::env::remove_var(name) },
        }
    }
}
