#[allow(dead_code)]
#[path = "source_fingerprint.rs"]
mod source_fingerprint;

use std::path::PathBuf;

fn main() {
    let manifest_dir = PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let repo = manifest_dir
        .parent()
        .expect("pipeline manifest has no repository root")
        .to_path_buf();

    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=runtime-manifest.json");
    println!("cargo:rerun-if-changed=source_fingerprint.rs");
    // Directories as well as members: a deleted closure file changes the
    // fingerprint too, and only the directory mtime reports that.
    println!("cargo:rerun-if-changed=src");
    println!("cargo:rerun-if-changed=../scripts");
    println!("cargo:rerun-if-changed=../tools");
    println!("cargo:rerun-if-changed=../rust-toolchain.toml");
    for path in source_fingerprint::runtime_paths(&repo).unwrap() {
        println!("cargo:rerun-if-changed={}", path.display());
    }

    let fingerprint = source_fingerprint::calculate(&repo).unwrap();
    let manifest_digest = source_fingerprint::manifest_digest();
    println!("cargo:rustc-env=HVP_SOURCE_FINGERPRINT={fingerprint}");
    println!("cargo:rustc-env=HVP_RUNTIME_MANIFEST_DIGEST={manifest_digest}");
}
