#[path = "../cli.rs"]
mod cli;

fn main() -> std::process::ExitCode {
    let code = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime.block_on(cli::entry()),
        Err(error) => {
            eprintln!("video-studio: could not start the async runtime: {error}");
            5
        }
    };
    std::process::ExitCode::from(code)
}
