import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def installation_fixture(tmp_path):
    source = tmp_path / "source"
    (source / "scripts").mkdir(parents=True)
    (source / "pipeline/target/debug").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/install", source / "scripts/install")
    (source / "scripts/video-studio-installed").write_text("#!/bin/sh\nexit 0\n")
    setup = source / "scripts/setup"
    setup.write_text("#!/bin/sh\nexit 0\n")
    setup.chmod(0o755)
    runtime = source / "pipeline/target/debug/hvp-runtime"
    runtime.write_text("#!/usr/bin/env python3\nimport json,os,pathlib\npathlib.Path(os.environ['TEST_INSTALL_CAPTURE']).write_text(json.dumps({key:os.environ[key] for key in ['HVP_RUNTIME_STATE','HVP_LAUNCHER_PATH']}))\n")
    runtime.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "scripts", "pipeline"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "fixture"], check=True)
    home = tmp_path / "home"
    home.mkdir()
    capture = tmp_path / "capture.json"
    env = {**os.environ, "HOME": str(home), "TEST_INSTALL_CAPTURE": str(capture), "HVP_RUNTIME_STATE": str(tmp_path / "unrelated-runtime"), "HVP_LAUNCHER_PATH": str(tmp_path / "unrelated-launcher")}
    return source, home, capture, env


def test_install_never_inherits_another_studios_runtime_paths(tmp_path):
    source, home, capture, env = installation_fixture(tmp_path)
    result = subprocess.run([str(source / "scripts/install")], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text()) == {
        "HVP_RUNTIME_STATE": str(home / ".local/state/video-studio/runtime"),
        "HVP_LAUNCHER_PATH": str(home / ".local/share/video-studio/bin/video-studio-mcp"),
    }
    assert not (tmp_path / "unrelated-runtime").exists()
    assert not (tmp_path / "unrelated-launcher").exists()


def test_unrelated_cli_collision_is_refused_before_runtime_activation(tmp_path):
    source, home, capture, env = installation_fixture(tmp_path)
    existing = home / ".local/share/video-studio/bin/video-studio"
    existing.parent.mkdir(parents=True)
    existing.write_text("another operator tool")
    result = subprocess.run([str(source / "scripts/install")], env=env, capture_output=True)
    assert result.returncode == 3
    assert existing.read_text() == "another operator tool"
    assert not capture.exists()
