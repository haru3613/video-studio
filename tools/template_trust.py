#!/usr/bin/env python3
"""Local authorization for executable Remotion template code.

Scene content and public media are data. Everything else in a Remotion source
tree (except installed node_modules and documentation) is part of the code
closure, including dependency locks and configuration. Custom closures require
an owner-written project ledger; the bundled narrated closure is recognized by
its exact digest.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path


SCHEMA = "video-studio.template_trust.v1"
AUTH_SCHEMA = "video-studio.template_authorization.v1"
LEDGER = ".hvp/template-trust.json"
IGNORED_FILES = {".gitignore", "README.md"}
DATA_FILES = {"src/content.json"}
LOCKFILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
CONFIGS = {
    "remotion.config.ts",
    "remotion.config.js",
    "remotion.config.mjs",
    "remotion.config.cjs",
}
EXECUTABLE_PUBLIC_SUFFIXES = {
    ".cjs",
    ".css",
    ".html",
    ".js",
    ".jsx",
    ".mjs",
    ".svg",
    ".ts",
    ".tsx",
    ".wasm",
}


class TrustError(ValueError):
    pass


def _direct_directory(value: Path) -> Path:
    path = Path(value)
    if path.is_symlink() or not path.is_dir():
        raise TrustError("directory")
    return path.resolve(strict=True)


def _direct_file(value: Path) -> Path:
    path = Path(value)
    if path.is_symlink() or not path.is_file():
        raise TrustError("file")
    return path.resolve(strict=True)


def _relative_remotion(project: Path, remotion: Path) -> str:
    try:
        relative = remotion.relative_to(project).as_posix()
    except ValueError as error:
        raise TrustError("template outside project") from error
    if not relative or relative == "." or relative.startswith("../"):
        raise TrustError("template path")
    return relative


def remotion_from_plan(project_value: Path) -> Path | None:
    project = _direct_directory(project_value)
    plan = json.loads(_direct_file(project / "render_plan.json").read_text(encoding="utf-8"))
    relative = plan.get("remotion_dir")
    if relative is None:
        return None
    if (
        not isinstance(relative, str)
        or Path(relative).is_absolute()
        or not Path(relative).parts
        or any(part in {"", ".", ".."} for part in Path(relative).parts)
    ):
        raise TrustError("remotion path")
    current = project
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise TrustError("remotion symlink")
    remotion = _direct_directory(current)
    _relative_remotion(project, remotion)
    return remotion


def _is_data(relative: str) -> bool:
    if relative in DATA_FILES or relative == "public":
        return True
    if relative.startswith("public/"):
        return Path(relative).suffix.lower() not in EXECUTABLE_PUBLIC_SUFFIXES
    return False


def code_manifest(remotion_value: Path) -> list[dict]:
    remotion = _direct_directory(remotion_value)
    entries = []
    present = set()
    for root, directories, files in os.walk(remotion, followlinks=False):
        root_path = Path(root)
        relative_root = root_path.relative_to(remotion).as_posix()
        if relative_root == ".":
            relative_root = ""
        kept = []
        for name in sorted(directories):
            path = root_path / name
            relative = "/".join(filter(None, (relative_root, name)))
            if name == "node_modules" and relative == "node_modules":
                if path.is_symlink() or not path.is_dir():
                    raise TrustError("node_modules")
                continue
            if path.is_symlink():
                raise TrustError("template symlink")
            kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            path = root_path / name
            relative = "/".join(filter(None, (relative_root, name)))
            if path.is_symlink():
                raise TrustError("template symlink")
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise TrustError("template special file")
            present.add(relative)
            if _is_data(relative) or relative in IGNORED_FILES:
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append(
                {
                    "path": relative,
                    "sha256": digest,
                    "bytes": metadata.st_size,
                    "executable": bool(metadata.st_mode & 0o111),
                }
            )
    if "package.json" not in present:
        raise TrustError("package.json required")
    if not LOCKFILES.intersection(present):
        raise TrustError("dependency lock required")
    if not CONFIGS.intersection(present):
        raise TrustError("Remotion config required")
    if not any(entry["path"].endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")) for entry in entries):
        raise TrustError("template code required")
    return entries


def code_digest(remotion_value: Path) -> tuple[str, list[dict]]:
    manifest = code_manifest(remotion_value)
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}", manifest


def _bundled() -> tuple[str, Path] | None:
    root = Path(__file__).resolve().parents[1] / "templates/narrated/remotion"
    try:
        digest, _manifest = code_digest(root)
    except (OSError, TrustError):
        return None
    return digest, root


def _ledger(project: Path) -> dict | None:
    path = project / LEDGER
    if not path.exists():
        return None
    try:
        direct = _direct_file(path)
        metadata = direct.stat()
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise TrustError("trust ledger owner")
        if metadata.st_mode & 0o077:
            raise TrustError("trust ledger permissions")
        value = json.loads(direct.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TrustError):
        raise TrustError("trust ledger")
    if value.get("schema") != SCHEMA or not isinstance(value.get("entries"), list):
        raise TrustError("trust ledger")
    return value


def authorize(project_value: Path) -> dict:
    project = _direct_directory(project_value)
    remotion = remotion_from_plan(project)
    if remotion is None:
        return {
            "schema": AUTH_SCHEMA,
            "mode": "not_applicable",
            "template_digest": None,
            "remotion_dir": None,
        }
    relative = _relative_remotion(project, remotion)
    digest, manifest = code_digest(remotion)
    bundled = _bundled()
    if bundled is not None and digest == bundled[0]:
        mode = "bundled"
    else:
        ledger = _ledger(project)
        allowed = bool(
            ledger
            and any(
                entry.get("template_digest") == digest
                and entry.get("remotion_dir") == relative
                for entry in ledger["entries"]
                if isinstance(entry, dict)
            )
        )
        if not allowed:
            raise TrustError("template_untrusted")
        mode = "local_approval"
    return {
        "schema": AUTH_SCHEMA,
        "mode": mode,
        "template_digest": digest,
        "remotion_dir": relative,
        "files": manifest,
    }


def _owner_directory(path: Path) -> Path:
    directory = _direct_directory(path)
    metadata = directory.stat()
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise TrustError("project owner")
    if metadata.st_mode & 0o022:
        raise TrustError("owner directory permissions")
    return directory


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    _owner_directory(path.parent)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def trust(project_value: Path) -> dict:
    project = _owner_directory(project_value)
    remotion = remotion_from_plan(project)
    if remotion is None:
        raise TrustError("template not applicable")
    relative = _relative_remotion(project, remotion)
    digest, manifest = code_digest(remotion)
    bundled = _bundled()
    if bundled is not None and digest == bundled[0]:
        return authorize(project)
    ledger = _ledger(project) or {"schema": SCHEMA, "entries": []}
    entry = {
        "template_digest": digest,
        "remotion_dir": relative,
        "approved_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "approved_by": f"uid:{os.getuid()}" if hasattr(os, "getuid") else "local-owner",
        "files": manifest,
    }
    ledger["entries"] = [
        value
        for value in ledger["entries"]
        if isinstance(value, dict) and value.get("remotion_dir") != relative
    ] + [entry]
    _atomic_json(project / LEDGER, ledger)
    return authorize(project)


def copy_authority(source_project_value: Path, snapshot_project_value: Path) -> None:
    source = _direct_directory(source_project_value)
    snapshot = _direct_directory(snapshot_project_value)
    ledger = source / LEDGER
    if not ledger.exists():
        return
    value = json.loads(_direct_file(ledger).read_text(encoding="utf-8"))
    if value.get("schema") != SCHEMA:
        raise TrustError("trust ledger")
    _atomic_json(snapshot / LEDGER, value)
