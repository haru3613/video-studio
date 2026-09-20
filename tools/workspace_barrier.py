#!/usr/bin/env python3
"""Workspace-wide advisory barrier shared by backup and local writers."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

WORKSPACE_SCHEMA = "video_studio.workspace.v1"
LOCK_NAME = "workspace-barrier.lock"


def _manifest(root: Path) -> dict | None:
    path = root / "workspace.json"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != WORKSPACE_SCHEMA:
            return None
        uuid.UUID(value["workspace_id"])
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None
    return value


def workspace_for(value: Path) -> Path | None:
    """Return an initialized workspace for its root/projects/project path.

    Legacy unmanaged project trees intentionally return None and acquire no
    lock or create any state.
    """

    path = Path(os.path.abspath(value))
    try:
        path = path.resolve(strict=True)
    except OSError:
        return None
    candidates = [path]
    if path.name == "projects":
        candidates.append(path.parent)
    elif path.parent.name == "projects":
        candidates.append(path.parent.parent)
    for candidate in candidates:
        if _manifest(candidate) is not None:
            projects = candidate / "projects"
            try:
                if (
                    projects.is_symlink()
                    or projects.resolve(strict=True).parent != candidate
                ):
                    return None
            except OSError:
                return None
            if path == candidate or path == projects or path.parent == projects:
                return candidate
    return None


def _open_lock(workspace: Path) -> int:
    state = workspace / ".video-studio"
    if state.is_symlink() or not state.is_dir():
        raise RuntimeError("workspace private state is unavailable")
    lock = state / LOCK_NAME
    if lock.is_symlink():
        raise RuntimeError("workspace barrier must not be a symlink")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock, flags, 0o600)
    os.fchmod(fd, 0o600)
    return fd


@contextmanager
def barrier(value: Path, *, exclusive: bool):
    workspace = workspace_for(value)
    if workspace is None:
        yield None
        return
    fd = _open_lock(workspace)
    with os.fdopen(fd, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield workspace
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def mutation_barrier(value: Path):
    with barrier(value, exclusive=False) as workspace:
        yield workspace


@contextmanager
def backup_barrier(value: Path):
    workspace = workspace_for(value)
    if workspace is None or workspace != Path(value).resolve(strict=True):
        raise ValueError("backup requires an initialized workspace root")
    with barrier(workspace, exclusive=True):
        yield workspace
