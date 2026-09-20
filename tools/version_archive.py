"""Content-addressed local media history; no review or publishing authority."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

SHA256 = re.compile(r"^[0-9a-f]{64}$")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def archived_video(project: Path, sha256: str, size: int) -> Path | None:
    if not isinstance(sha256, str) or SHA256.fullmatch(sha256) is None:
        return None
    current = project
    for part in ("output", "versions", sha256, "final.mp4"):
        current /= part
        if current.is_symlink():
            return None
    try:
        if current.is_file() and current.stat().st_size == size and digest(current) == sha256:
            return current
    except OSError:
        pass
    return None


def preserve_video(video: Path) -> Path:
    if video.name != "final.mp4" or video.parent.name != "output" or video.is_symlink() or not video.is_file():
        raise ValueError("only a direct canonical final video can be archived")
    project = video.parent.parent
    checksum = digest(video)
    size = video.stat().st_size
    versions = video.parent / "versions"
    directory = versions / checksum
    for path in (video.parent, versions, directory):
        if path.is_symlink():
            raise ValueError("version archive contains a symlink")
        path.mkdir(exist_ok=True)
        if not path.is_dir():
            raise ValueError("version archive is not a directory")
    existing = archived_video(project, checksum, size)
    if existing is not None:
        return existing
    destination = directory / "final.mp4"
    try:
        os.link(video, destination, follow_symlinks=False)
    except FileExistsError:
        existing = archived_video(project, checksum, size)
        if existing is None:
            raise ValueError("existing version archive conflicts with its content digest")
        return existing
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    for path in (directory, versions, video.parent):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return destination
