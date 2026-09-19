#!/usr/bin/env python3
"""Report local prerequisites without reading credentials or calling providers."""
import argparse
import json
import platform
import shutil
import subprocess
from pathlib import Path


def inspect():
    commands = {
        "python3": ["--version"],
        "node": ["--version"],
        "ffmpeg": ["-version"],
        "ffprobe": ["-version"],
        "cargo": ["--version"],
        "uv": ["--version"],
    }
    rows = []
    for name, flags in commands.items():
        path = shutil.which(name)
        if name == "cargo" and path is None:
            candidate = Path("/opt/homebrew/opt/rustup/bin/cargo")
            path = str(candidate) if candidate.is_file() else None
        version = None
        if path:
            try:
                result = subprocess.run([path, *flags], capture_output=True, text=True, timeout=15)
                if result.returncode == 0:
                    version = (result.stdout or result.stderr).splitlines()[0]
            except (OSError, subprocess.TimeoutExpired, IndexError):
                pass
        rows.append({"name": name, "available": bool(version), "path": path, "version": version})
    root = Path(__file__).resolve().parents[1]
    rows.append({"name": "project_python_environment", "available": (root / ".venv/bin/python").is_file(), "hint": "uv sync --locked --python 3.11"})
    return {
        "schema": "video_studio.doctor.v1",
        "platform": platform.system(),
        "architecture": platform.machine(),
        "checks": rows,
        "ready": all(item["available"] for item in rows),
        "provider_credentials_checked": False,
        "publishing_configured": False,
    }


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    result = inspect()
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
