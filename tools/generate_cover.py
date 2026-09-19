#!/usr/bin/env python3
"""Render the approved house thumbnail through one fixed project contract."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_status
import canonical_layout


SCHEMA = "haru.cover_generation.v1"
SPEC_SCHEMA = "haru.cover_spec.v1"
ACCENTS = {"amber", "coral", "teal", "violet"}
MAX_BYTES = 2 * 1024 * 1024


class CoverError(Exception):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def direct_directory(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise CoverError("cover_input_invalid")
    return path.resolve(strict=True)


def direct_file(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CoverError("cover_input_invalid")
    return path.resolve(strict=True)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def load_spec(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CoverError("cover_spec_invalid") from error
    if not isinstance(value, dict) or value.get("schema") != SPEC_SCHEMA:
        raise CoverError("cover_spec_invalid")
    allowed = {
        "schema",
        "kicker",
        "top",
        "bottom",
        "subtitle",
        "expression",
        "accent",
    }
    if set(value) - allowed:
        raise CoverError("cover_spec_invalid")
    for field, limit in (("kicker", 40), ("top", 40), ("bottom", 40), ("subtitle", 80)):
        text = value.get(field, "")
        if not isinstance(text, str) or len(text) > limit or "\x00" in text:
            raise CoverError("cover_spec_invalid")
    if not value.get("bottom", "").strip():
        raise CoverError("cover_spec_invalid")
    if value.get("accent") not in ACCENTS or not re.fullmatch(
        r"[a-z0-9-]{1,40}", value.get("expression", "")
    ):
        raise CoverError("cover_spec_invalid")
    return value


def generate(project_input: Path, tools_input: Path) -> dict:
    project = direct_directory(project_input)
    tools_root = direct_directory(tools_input)
    workspace = direct_directory(project.parent.parent)
    staging = project / ".hvp" / "staging"
    if staging.is_symlink():
        raise CoverError("cover_input_invalid")
    staging.mkdir(parents=True, exist_ok=True)
    spec_path = direct_file(staging / "cover-spec.json")
    generator = direct_file(tools_root / "cover" / "make_cover.py")
    spec = load_spec(spec_path)
    rendered = staging / "cover-generated.png"
    arguments = [
        sys.executable,
        str(generator),
        "--kicker",
        spec.get("kicker", ""),
        "--top",
        spec.get("top", ""),
        "--bottom",
        spec["bottom"],
        "--subtitle",
        spec.get("subtitle", ""),
        "--expression",
        spec["expression"],
        "--accent",
        spec["accent"],
        "--out",
        str(rendered),
    ]
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise CoverError("cover_runner_unavailable") from error
    if completed.returncode != 0:
        raise CoverError("cover_generation_failed")
    rendered = direct_file(rendered)
    if agent_status.png_dimensions(rendered) != (1280, 720):
        raise CoverError("cover_dimensions_invalid")
    if rendered.stat().st_size > MAX_BYTES:
        raise CoverError("cover_too_large")

    producer = canonical_layout.produce(
        project,
        "output/cover.png",
        rendered,
        produced_by="video-studio.generate-cover",
        force=True,
    )
    cover = direct_file(project / "output" / "cover.png")
    result = {
        "schema": SCHEMA,
        "ok": True,
        "project": project.name,
        "status": "complete",
        "output": "output/cover.png",
        "output_sha256": sha256(cover),
        "bytes": cover.stat().st_size,
        "width": 1280,
        "height": 720,
        "spec_sha256": sha256(spec_path),
        "producer_receipt": producer,
        "generated_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
    }
    atomic_json(project / ".hvp" / "cover-generation.json", result)

    _status, artifacts = agent_status.build(project, workspace)
    atomic_json(project / "artifact_manifest.json", artifacts)
    status, artifacts = agent_status.build(project, workspace)
    atomic_json(project / "artifact_manifest.json", artifacts)
    atomic_json(project / "pipeline_status.json", status)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate one canonical HVP cover")
    parser.add_argument("project", type=Path)
    parser.add_argument("tools_root", type=Path)
    args = parser.parse_args()
    try:
        result = generate(args.project, args.tools_root)
    except (CoverError, ValueError) as error:
        code = str(error) if isinstance(error, CoverError) else "cover_generation_failed"
        print(json.dumps({"schema": SCHEMA, "ok": False, "code": code}))
        return 3
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
