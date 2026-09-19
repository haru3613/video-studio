"""Persistent, project-scoped storage for review-hub comments."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, TypeVar

try:
    from workspace_barrier import mutation_barrier
except ImportError:  # review server also runs with tools/dashboard as sys.path[0]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from workspace_barrier import mutation_barrier


STORE_SCHEMA = "haru.review_feedback.v1"
T = TypeVar("T")
MACOS_SYSTEM_ALIASES = {
    Path("/var"): Path("/private/var"),
    Path("/tmp"): Path("/private/tmp"),
}


def _validate_storage_base(path: Path) -> None:
    """Reject configured symlink redirects, except macOS's system aliases."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if not current.is_symlink():
            continue
        allowed_target = MACOS_SYSTEM_ALIASES.get(current)
        try:
            target = current.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("review storage root contains a broken symlink") from exc
        if allowed_target is None or target != allowed_target:
            raise RuntimeError("review storage root must not contain symlinks")


class ReviewStore:
    """Serialize feedback updates with flock and publish them atomically."""

    def __init__(self, project_root: Path, storage_root: Path | None = None):
        canonical = project_root.resolve(strict=True)
        self.project_root = canonical
        base = storage_root or (
            Path.home() / ".local" / "state" / "video-studio" / "review-hub"
        )
        bucket = hashlib.sha256(os.fsencode(str(canonical))).hexdigest()
        expanded = Path(base).expanduser()
        self.configured_base = Path(os.path.abspath(os.fspath(expanded)))
        _validate_storage_base(self.configured_base)
        self.base = self.configured_base.resolve(strict=False)
        self.directory = self.base / bucket
        self.path = self.directory / "feedback.json"
        self.lock_path = self.directory / "feedback.lock"

    @contextmanager
    def _locked(self, *, exclusive: bool):
        _validate_storage_base(self.configured_base)
        if self.directory.is_symlink():
            raise RuntimeError("review feedback directory must not be a symlink")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink() or self.lock_path.is_symlink():
            raise RuntimeError("review feedback lock must not be a symlink")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.lock_path, flags, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read_unlocked(self) -> dict:
        if self.path.is_symlink():
            raise RuntimeError("review feedback file must not be a symlink")
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(self.path, flags)
        except FileNotFoundError:
            return {"schema": STORE_SCHEMA, "comments": []}
        except OSError as exc:
            raise RuntimeError("review feedback file cannot be opened safely") from exc
        with os.fdopen(fd, "rb") as source:
            raw = source.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise RuntimeError("review feedback store is unexpectedly large")
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("review feedback store is corrupt") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema") != STORE_SCHEMA
            or not isinstance(document.get("comments"), list)
        ):
            raise RuntimeError("review feedback store has an unsupported schema")
        return document

    def read_comments(self) -> list[dict]:
        _validate_storage_base(self.configured_base)
        if self.directory.is_symlink():
            raise RuntimeError("review feedback directory must not be a symlink")
        if not self.directory.exists():
            return []
        with self._locked(exclusive=False):
            document = self._read_unlocked()
            # The JSON round trip prevents callers from mutating shared state if
            # this implementation later gains a small in-memory cache.
            return json.loads(json.dumps(document["comments"]))

    def update(self, mutator: Callable[[list[dict]], T]) -> T:
        with mutation_barrier(self.project_root):
            with self._locked(exclusive=True):
                document = self._read_unlocked()
                result = mutator(document["comments"])
                self._write_unlocked(document)
                return result

    def _write_unlocked(self, document: dict) -> None:
        payload = (
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n"
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=".feedback.", suffix=".tmp", dir=self.directory
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
