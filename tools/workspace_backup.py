#!/usr/bin/env python3
"""Create and restore verified local Video Studio workspace backups."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workspace_barrier import backup_barrier

SCHEMA = "video_studio.workspace_backup.v1"
RESTORE_SCHEMA = "video_studio.workspace_restore.v1"
WORKSPACE_SCHEMA = "video_studio.workspace.v1"
ACTIVE_JOBS = ("queued", "running", "cancel_requested", "promoting")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
EXCLUDED_NAMES = {
    "__pycache__",
    "node_modules",
    ".cache",
    ".pytest_cache",
    ".ruff_cache",
    ".playwright",
    "playwright-cache",
    "browser-cache",
}
EXCLUDED_HVP = {
    "lease.json",
    "lease.lock",
    "private-leases",
    "mcp-operations",
    "mcp-operations.lock",
    "provider-jobs",
    "provider-staging",
    "youtube-uploads",
    "template-trust.json",
    "render-job.json",
    "intake-requests",
    "review-requests",
}
EXCLUDED_AUTHORITY_FILES = {
    "publish/publish-approval.json",
}
LIMITATIONS = [
    "Provider credentials, OAuth material, UI sessions, and browser caches are excluded.",
    "Publish approvals, active leases, upload attempts, provider jobs, and template-trust authority do not transfer.",
    "External signing keys and external attestation ledgers require separate operator recovery.",
    "A restored workspace has the same workspace ID but a new canonical path; old path-bound signatures are not trusted.",
]


class BackupError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _workspace(root: Path) -> tuple[Path, dict]:
    raw = Path(os.path.abspath(root))
    if raw.is_symlink() or not raw.is_dir():
        raise BackupError(
            "invalid_workspace", "workspace root must be a direct directory"
        )
    root = raw.resolve(strict=True)
    manifest = root / "workspace.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise BackupError("invalid_workspace", "workspace.json is missing")
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("schema") != WORKSPACE_SCHEMA
            or set(value) != {"schema", "workspace_id"}
        ):
            raise ValueError
        uuid.UUID(value["workspace_id"])
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise BackupError("invalid_workspace", "workspace manifest is invalid") from exc
    projects = root / "projects"
    state = root / ".video-studio"
    if (
        projects.is_symlink()
        or state.is_symlink()
        or not projects.is_dir()
        or not state.is_dir()
    ):
        raise BackupError("invalid_workspace", "workspace roots are unsafe")
    return root, value


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise BackupError("backup_manifest_invalid", "invalid backup path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise BackupError(
            "backup_manifest_invalid", "backup path escapes the workspace"
        )
    return value


def _excluded(relative: str, *, directory: bool) -> bool:
    parts = PurePosixPath(relative).parts
    if not parts:
        return False
    if any(part in EXCLUDED_NAMES for part in parts):
        return True
    if any(part.startswith(".env") for part in parts):
        return True
    if any(part.lower() in {"credentials", "secrets"} for part in parts):
        return True
    if relative == ".video-studio/workspace-barrier.lock":
        return True
    if (
        parts[0] == ".video-studio"
        and "review" in parts
        and (parts[-1] == "feedback.lock" or parts[-1].startswith(".feedback."))
    ):
        return True
    if parts[0] == ".video-studio" and len(parts) > 1 and parts[1] != "review":
        return True
    if ".hvp" in parts:
        index = parts.index(".hvp")
        if len(parts) > index + 1 and parts[index + 1] in EXCLUDED_HVP:
            return True
        if (
            len(parts) > index + 2
            and parts[index + 1] == "staging"
            and parts[index + 2] != "intake"
        ):
            return True
    if len(parts) >= 2 and parts[-2:] == ("output", ".staging"):
        return True
    if "output" in parts:
        output_index = parts.index("output")
        if len(parts) > output_index + 1 and parts[output_index + 1] == ".staging":
            return True
    if relative.endswith(("-wal", "-shm", "-journal", ".pyc", ".pyo")):
        return True
    if any(relative.endswith(name) for name in EXCLUDED_AUTHORITY_FILES):
        return True
    return False


def _walk(root: Path):
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        relative_root = "" if current_path == root else _relative(current_path, root)
        kept = []
        for name in sorted(directories):
            path = current_path / name
            relative = "/".join(filter(None, (relative_root, name)))
            if path.is_symlink():
                raise BackupError(
                    "workspace_symlink_refused", f"symlink in workspace: {relative}"
                )
            if not _excluded(relative, directory=True):
                kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            path = current_path / name
            relative = "/".join(filter(None, (relative_root, name)))
            if _excluded(relative, directory=False):
                continue
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise BackupError(
                    "workspace_symlink_refused", f"symlink in workspace: {relative}"
                )
            if not stat.S_ISREG(metadata.st_mode):
                raise BackupError(
                    "workspace_special_file_refused",
                    f"special file in workspace: {relative}",
                )
            yield path, relative, metadata


def _active_jobs(root: Path) -> list[dict]:
    active = []
    for database in root.glob("projects/*/.hvp/jobs.sqlite3"):
        if database.is_symlink() or not database.is_file():
            raise BackupError("jobs_database_invalid", str(database))
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            rows = connection.execute(
                "SELECT job_id,status FROM jobs WHERE status IN (?,?,?,?)",
                ACTIVE_JOBS,
            ).fetchall()
        except sqlite3.Error as exc:
            raise BackupError("jobs_database_invalid", str(database)) from exc
        finally:
            if "connection" in locals():
                connection.close()
                del connection
        active.extend(
            {"project": database.parents[1].name, "job_id": row[0], "status": row[1]}
            for row in rows
        )
    for projection in root.glob("projects/*/.hvp/render-job.json"):
        try:
            value = json.loads(projection.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if value.get("status") in ACTIVE_JOBS and not any(
            item["job_id"] == value.get("job_id") for item in active
        ):
            active.append(
                {
                    "project": projection.parents[1].name,
                    "job_id": value.get("job_id"),
                    "status": value.get("status"),
                }
            )
    return active


def _copy_regular(source: Path, destination: Path, metadata) -> tuple[str, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(source, flags)
    digest = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        before = os.fstat(descriptor)
        with (
            os.fdopen(descriptor, "rb", closefd=False) as reader,
            destination.open("xb") as writer,
        ):
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)

    def identity(value):
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    if identity(before) != identity(after) or identity(before) != identity(metadata):
        destination.unlink(missing_ok=True)
        raise BackupError("workspace_changed", str(source))
    os.chmod(destination, stat.S_IMODE(before.st_mode) & 0o777)
    return digest.hexdigest(), before.st_size


def _backup_sqlite(source: Path, destination: Path) -> tuple[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        destination_db = sqlite3.connect(destination)
        source_db.backup(destination_db)
        destination_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        destination_db.close()
        source_db.close()
    except sqlite3.Error as exc:
        destination.unlink(missing_ok=True)
        raise BackupError("sqlite_backup_failed", str(source)) from exc
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return digest, destination.stat().st_size


def _review_buckets(root: Path) -> list[dict]:
    base = root / ".video-studio/review"
    if not base.exists():
        return []
    projects = {
        hashlib.sha256(
            os.fsencode(str(project.resolve(strict=True)))
        ).hexdigest(): project.name
        for project in (root / "projects").iterdir()
        if project.is_dir() and not project.is_symlink()
    }
    result = []
    for bucket in sorted(base.iterdir()):
        if bucket.is_symlink() or not bucket.is_dir() or bucket.name not in projects:
            raise BackupError("review_bucket_unmapped", bucket.name)
        result.append({"bucket": bucket.name, "project": projects[bucket.name]})
    return result


def _manifest_identity(
    workspace: dict, files: list[dict], review_buckets: list[dict]
) -> str:
    basis = {
        "workspace": {
            "schema": workspace["schema"],
            "workspace_id": workspace["workspace_id"],
            "canonical_root": workspace.get("canonical_root"),
        },
        "files": files,
        "review_buckets": review_buckets,
    }
    payload = json.dumps(basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_backup(workspace_root: Path, destination_root: Path) -> dict:
    workspace_root, workspace = _workspace(workspace_root)
    destination_root = Path(os.path.abspath(destination_root))
    if (
        not destination_root.is_absolute()
        or destination_root.is_symlink()
        or not destination_root.is_dir()
    ):
        raise BackupError(
            "invalid_destination",
            "backup destination must be an existing direct directory",
        )
    destination_root = destination_root.resolve(strict=True)
    if destination_root == workspace_root or destination_root.is_relative_to(
        workspace_root
    ):
        raise BackupError(
            "invalid_destination", "backup destination must be outside the workspace"
        )

    with backup_barrier(workspace_root):
        active = _active_jobs(workspace_root)
        if active:
            raise BackupError("active_jobs", f"active render jobs: {active}")
        review_buckets = _review_buckets(workspace_root)
        staging = Path(
            tempfile.mkdtemp(prefix=".workspace-backup.", dir=destination_root)
        )
        try:
            files = []
            for source, relative, metadata in _walk(workspace_root):
                target = staging / "data" / relative
                if source.suffix.lower() in SQLITE_SUFFIXES:
                    digest, size = _backup_sqlite(source, target)
                    kind = "sqlite"
                else:
                    digest, size = _copy_regular(source, target, metadata)
                    kind = "file"
                files.append(
                    {
                        "path": relative,
                        "sha256": digest,
                        "bytes": size,
                        "mode": stat.S_IMODE(metadata.st_mode) & 0o777,
                        "kind": kind,
                    }
                )
            files.sort(key=lambda item: item["path"])
            backup_id = _manifest_identity(
                {**workspace, "canonical_root": str(workspace_root)},
                files,
                review_buckets,
            )
            manifest = {
                "schema": SCHEMA,
                "backup_id": backup_id,
                "created_at": _now(),
                "source_workspace": {
                    **workspace,
                    "canonical_root": str(workspace_root),
                },
                "files": files,
                "review_buckets": review_buckets,
                "excluded_authority": sorted(
                    [
                        "active leases",
                        "browser and package caches",
                        "MCP idempotency receipts",
                        "private request handoffs and render staging",
                        "provider jobs and staging",
                        "provider/OAuth credentials",
                        "publish approvals and upload attempts",
                        "template trust ledger",
                        "UI sessions and bootstrap codes",
                    ]
                ),
                "limitations": LIMITATIONS,
            }
            _write_json(staging / "manifest.json", manifest)
            final = destination_root / f"backup-{backup_id}"
            if final.exists() or final.is_symlink():
                existing = _load_manifest(final)
                if existing.get("backup_id") == backup_id:
                    _verify_backup(final, existing)
                    shutil.rmtree(staging)
                    return {
                        "schema": SCHEMA,
                        "status": "complete",
                        "backup_id": backup_id,
                        "path": str(final),
                        "reused": True,
                        "files": len(files),
                    }
                raise BackupError("backup_target_conflict", str(final))
            os.rename(staging, final)
            _fsync_directory(destination_root)
            return {
                "schema": SCHEMA,
                "status": "complete",
                "backup_id": backup_id,
                "path": str(final),
                "reused": False,
                "files": len(files),
            }
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise


def _load_manifest(backup: Path) -> dict:
    if backup.is_symlink() or not backup.is_dir():
        raise BackupError("backup_invalid", "backup root is unsafe")
    manifest_path = backup / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BackupError("backup_manifest_invalid", "manifest is missing")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("backup_manifest_invalid", "manifest is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise BackupError("backup_manifest_invalid", "manifest schema is unsupported")
    return value


def _verify_backup(backup: Path, manifest: dict) -> None:
    files = manifest.get("files")
    source = manifest.get("source_workspace")
    review_buckets = manifest.get("review_buckets")
    if (
        not isinstance(files, list)
        or not isinstance(source, dict)
        or source.get("schema") != WORKSPACE_SCHEMA
        or not isinstance(review_buckets, list)
    ):
        raise BackupError("backup_manifest_invalid", "manifest shape is invalid")
    seen = set()
    normalized = []
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256", "bytes", "mode", "kind"}
            or not SHA256.fullmatch(str(item.get("sha256", "")))
            or not isinstance(item.get("bytes"), int)
            or item["bytes"] < 0
            or not isinstance(item.get("mode"), int)
            or not 0 <= item["mode"] <= 0o777
            or item.get("kind") not in {"file", "sqlite"}
        ):
            raise BackupError("backup_manifest_invalid", "file entry is invalid")
        relative = _safe_relative(item["path"])
        if relative in seen:
            raise BackupError("backup_manifest_invalid", "duplicate file entry")
        seen.add(relative)
        normalized.append(item)
        path = backup / "data" / relative
        if path.is_symlink() or not path.is_file():
            raise BackupError("backup_file_invalid", relative)
        if (
            path.stat().st_size != item["bytes"]
            or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
        ):
            raise BackupError("backup_file_invalid", relative)
    expected = _manifest_identity(source, normalized, review_buckets)
    if manifest.get("backup_id") != expected or backup.name != f"backup-{expected}":
        raise BackupError(
            "backup_manifest_invalid", "backup ID does not match contents"
        )
    actual = set()
    data = backup / "data"
    for current, directories, filenames in os.walk(data, followlinks=False):
        current_path = Path(current)
        relative_root = "" if current_path == data else _relative(current_path, data)
        for name in [*directories, *filenames]:
            path = current_path / name
            relative = "/".join(filter(None, (relative_root, name)))
            if path.is_symlink():
                raise BackupError("backup_file_invalid", relative)
        for name in filenames:
            path = current_path / name
            if not path.is_file():
                raise BackupError("backup_file_invalid", str(path))
            actual.add("/".join(filter(None, (relative_root, name))))
    if actual != seen:
        raise BackupError("backup_manifest_invalid", "unlisted backup file")


def _invalidate_jobs(workspace: Path, backup_id: str) -> int:
    changed = 0
    for database in workspace.glob("projects/*/.hvp/jobs.sqlite3"):
        connection = sqlite3.connect(database)
        try:
            changed += connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        except sqlite3.Error as exc:
            raise BackupError("restore_jobs_invalid", str(database)) from exc
        finally:
            connection.close()
        archived = database.with_name(f"jobs.invalidated-{backup_id[:12]}.sqlite3")
        if archived.exists() or archived.is_symlink():
            raise BackupError("restore_jobs_invalid", str(archived))
        os.rename(database, archived)
    return changed


def _rekey_reviews(workspace: Path, final_workspace: Path, mappings: list[dict]) -> int:
    base = workspace / ".video-studio/review"
    if not base.exists():
        return 0
    moved = 0
    for mapping in mappings:
        if not isinstance(mapping, dict) or set(mapping) != {"bucket", "project"}:
            raise BackupError("backup_manifest_invalid", "review mapping is invalid")
        old = base / mapping["bucket"]
        project = workspace / "projects" / mapping["project"]
        if (
            not old.is_dir()
            or old.is_symlink()
            or not project.is_dir()
            or project.is_symlink()
        ):
            raise BackupError("restore_review_invalid", mapping["project"])
        final_project = final_workspace / "projects" / mapping["project"]
        new_name = hashlib.sha256(os.fsencode(str(final_project))).hexdigest()
        new = base / new_name
        if new != old:
            if new.exists() or new.is_symlink():
                raise BackupError("restore_review_invalid", "review bucket collision")
            os.rename(old, new)
            moved += 1
    return moved


def restore_backup(backup_root: Path, destination: Path) -> dict:
    backup = Path(os.path.abspath(backup_root)).resolve(strict=True)
    manifest = _load_manifest(backup)
    _verify_backup(backup, manifest)
    raw_destination = Path(os.path.abspath(destination))
    if raw_destination.is_symlink():
        raise BackupError(
            "restore_destination_not_empty", "restore destination is a symlink"
        )
    try:
        parent = raw_destination.parent.resolve(strict=True)
    except OSError as exc:
        raise BackupError("invalid_destination", "restore parent is unsafe") from exc
    destination = parent / raw_destination.name
    if destination == backup or destination.is_relative_to(backup):
        raise BackupError(
            "invalid_destination", "restore destination must be outside the backup"
        )
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise BackupError(
                "restore_destination_not_empty", "restore destination must be empty"
            )
        destination.rmdir()
    if parent.is_symlink() or not parent.is_dir():
        raise BackupError("invalid_destination", "restore parent is unsafe")
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore.", dir=parent))
    try:
        for item in manifest["files"]:
            relative = _safe_relative(item["path"])
            source = backup / "data" / relative
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = _copy_regular(source, target, source.stat())
            if digest != item["sha256"] or size != item["bytes"]:
                raise BackupError("backup_file_invalid", relative)
            os.chmod(target, item["mode"])
        for name, mode in (
            ("projects", 0o755),
            ("inbox", 0o755),
            ("exports", 0o755),
            (".video-studio", 0o700),
        ):
            directory = staging / name
            if directory.is_symlink():
                raise BackupError("backup_file_invalid", name)
            directory.mkdir(exist_ok=True, mode=mode)
            os.chmod(directory, mode)
        restored_manifest = json.loads(
            (staging / "workspace.json").read_text(encoding="utf-8")
        )
        if restored_manifest.get("workspace_id") != manifest["source_workspace"].get(
            "workspace_id"
        ):
            raise BackupError("backup_manifest_invalid", "workspace identity changed")
        interrupted = _invalidate_jobs(staging, manifest["backup_id"])
        reviews_rekeyed = _rekey_reviews(
            staging, destination, manifest["review_buckets"]
        )
        for private in [staging / ".video-studio", *staging.glob("projects/*/.hvp")]:
            if private.is_dir() and not private.is_symlink():
                os.chmod(private, 0o700)
        for review_directory in (staging / ".video-studio/review").glob("*"):
            if review_directory.is_dir() and not review_directory.is_symlink():
                os.chmod(review_directory, 0o700)
        report = {
            "schema": RESTORE_SCHEMA,
            "restored_at": _now(),
            "backup_id": manifest["backup_id"],
            "source_canonical_root": manifest["source_workspace"]["canonical_root"],
            "destination_canonical_root": str(destination),
            "path_changed": manifest["source_workspace"]["canonical_root"]
            != str(destination),
            "invalidated": {
                "active_leases": True,
                "jobs_invalidated": interrupted,
                "publish_approvals": True,
                "template_trust": True,
                "path_bound_signatures": True,
            },
            "review_buckets_rekeyed": reviews_rekeyed,
            "limitations": LIMITATIONS,
        }
        report_path = staging / ".video-studio/restore-report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(report_path, report)
        os.rename(staging, destination)
        _fsync_directory(parent)
        return {
            "schema": RESTORE_SCHEMA,
            "status": "complete",
            "backup_id": manifest["backup_id"],
            "workspace": str(destination),
            "workspace_id": restored_manifest["workspace_id"],
            "review_buckets_rekeyed": reviews_rekeyed,
            "invalidated": report["invalidated"],
            "limitations": LIMITATIONS,
        }
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _response(outcome: str, code: str, data=None) -> dict:
    return {"schema_version": 1, "outcome": outcome, "code": code, "data": data}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("create")
    backup.add_argument("workspace", type=Path)
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result = create_backup(args.workspace, args.destination)
            code = "workspace_backup_complete"
        else:
            result = restore_backup(args.backup, args.destination)
            code = "workspace_restore_complete"
        print(json.dumps(_response("ok", code, result), sort_keys=True))
        return 0
    except BackupError as exc:
        outcome = (
            "blocked" if exc.code in {"active_jobs", "workspace_changed"} else "error"
        )
        print(
            json.dumps(
                _response(outcome, exc.code, {"message": exc.message}), sort_keys=True
            )
        )
        return 3 if outcome == "blocked" else 2
    except Exception as exc:
        print(
            json.dumps(
                _response("error", "internal_error", {"message": str(exc)}),
                sort_keys=True,
            )
        )
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
