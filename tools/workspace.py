#!/usr/bin/env python3
"""Create or inspect an explicit, operator-owned Video Studio workspace."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from pathlib import Path

SCHEMA = "video_studio.workspace.v1"


def direct_path(value: Path) -> Path:
    path = Path(os.path.abspath(value.expanduser()))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            allowed = {Path("/tmp"): Path("/private/tmp"), Path("/var"): Path("/private/var")}
            if current not in allowed or current.resolve() != allowed[current]:
                raise ValueError("workspace path contains a symlink")
    return path.resolve()


def initialize(value: Path) -> dict:
    root = direct_path(value)
    paths = [root / "workspace.json", *(root / part for part in ("projects", "inbox", "exports", ".video-studio"))]
    for path in paths:
        direct_path(path)
        if path.exists() and ((path.name == "workspace.json") != path.is_file()):
            raise ValueError(f"unexpected workspace entry: {path.name}")
    manifest = root / "workspace.json"
    if manifest.exists():
        value = json.loads(manifest.read_text())
        if not isinstance(value, dict) or value.get("schema") != SCHEMA:
            raise ValueError("workspace schema is unsupported")
        uuid.UUID(value["workspace_id"])
    else:
        root.mkdir(parents=True, exist_ok=True)
        value = {"schema": SCHEMA, "workspace_id": str(uuid.uuid4())}
        # Exclusive create protects concurrent initialization without replacing
        # an existing workspace identity.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=root, delete=False, encoding="utf-8") as stream:
                temporary = Path(stream.name)
                json.dump(value, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, manifest)
            except FileExistsError:
                return initialize(root)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    for name in ("projects", "inbox", "exports", ".video-studio"):
        path = root / name
        path.mkdir(exist_ok=True, mode=0o700 if name.startswith(".") else 0o755)
        direct_path(path)
    return {"schema_version": 1, "outcome": "ok", "code": "workspace_ready", "data": {**value, "root": str(root), "projects_root": str(root / "projects")}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    try:
        result = initialize(args.workspace)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"schema_version": 1, "outcome": "error", "code": "invalid_workspace", "data": {"message": str(error)}}))
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
