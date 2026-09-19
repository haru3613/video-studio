use std::fs;
use std::process::Command;

use tempfile::tempdir;

fn run(arguments: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_hvp-state"))
        .args(arguments)
        .output()
        .unwrap()
}

#[test]
fn lease_lifecycle_is_not_exposed_by_the_shell_cli() {
    let directory = tempdir().unwrap();
    let project = directory.path().join("project");
    fs::create_dir(&project).unwrap();

    for command in ["claim", "renew", "release", "refresh", "record-gate"] {
        let output = run(&[command, project.to_str().unwrap()]);
        assert_eq!(output.status.code(), Some(2), "{command}: {output:?}");
        assert!(output.stdout.is_empty(), "{command} emitted JSON");
    }

    let status = run(&["status", project.to_str().unwrap()]);
    assert!(status.status.success(), "{status:?}");
    assert!(
        !String::from_utf8(status.stdout)
            .unwrap()
            .contains("capability")
    );
}
