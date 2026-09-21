#!/usr/bin/env python3
"""Fixed JSON interface for one project's durable render job.

This is the only process surface the Rust application needs.  It accepts job
data, never executable paths, and returns a small public projection instead of
the supervisor's PID, process token, worker path or staging paths.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import portable_jobs


JOB_ID = re.compile(r"[0-9a-f]{32}")
DEFAULT_LOG_BYTES = 32 * 1024
MAX_LOG_BYTES = 64 * 1024
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|CREDENTIAL|AUTH)[A-Z0-9_]*)\s*=\s*([^\s]+)"
)
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
URL_AUTH = re.compile(r"(https?://)[^/@\s:]+:[^/@\s]+@", re.IGNORECASE)
COMMON_TOKEN = re.compile(r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,})\b")


def envelope(project, outcome, code, data=None):
    return {
        "schema_version": 1,
        "outcome": outcome,
        "code": code,
        "project": str(project) if project is not None else None,
        "data": data,
    }


def direct_project(value) -> Path:
    project = Path(value)
    if project.is_symlink() or not project.is_dir():
        raise ValueError("project")
    return project.resolve(strict=True)


def valid_job_id(value) -> str:
    if not isinstance(value, str) or JOB_ID.fullmatch(value) is None:
        raise ValueError("job id")
    return value


def public_job(project: Path, job: dict) -> dict:
    if job.get("project") != str(project) or not JOB_ID.fullmatch(job.get("job_id", "")):
        raise ValueError("job binding")
    status = job.get("status")
    return {
        "schema": portable_jobs.JOB_SCHEMA,
        "job_id": job["job_id"],
        "kind": job["kind"],
        "status": status,
        "epoch": job["epoch"],
        "revision": job["revision"],
        "exit_code": job.get("exit_code"),
        "error_code": job.get("error_code"),
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "output": "output/final.mp4",
        "log_available": has_log(project, job["job_id"]),
        "can_cancel": status in {"queued", "running", "cancel_requested"},
        "can_resume": can_resume(project, job),
    }


def can_resume(project: Path, job: dict) -> bool:
    if job.get("status") not in {"failed", "cancelled", "interrupted"}:
        return False
    try:
        return portable_jobs.project_revision(project) == job.get("revision")
    except (OSError, ValueError):
        return False


def read_job(project: Path, job_id: str) -> dict | None:
    """Read an existing job without creating state, a DB, WAL, or projection."""
    job_id = valid_job_id(job_id)
    state = project / ".hvp"
    database = state / "jobs.sqlite3"
    if (
        state.is_symlink()
        or not state.is_dir()
        or database.is_symlink()
        or not database.is_file()
    ):
        return None
    # `mode=rw` refuses a missing database while still allowing SQLite to read
    # an active WAL created by the supervisor. This interface executes only a
    # parameterized SELECT and never runs schema setup or a write transaction.
    connection = sqlite3.connect(
        f"{database.as_uri()}?mode=rw", uri=True, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        job = dict(row) if row is not None else None
        if job and job["status"] in {"running", "cancel_requested", "promoting"}:
            if not portable_jobs._same_process(job):
                previous = job["status"]
                job["status"] = (
                    "cancelled" if previous == "cancel_requested" else "interrupted"
                )
                job["error_code"] = f"worker_{job['status']}"
        return job
    finally:
        connection.close()


def bound_job(project: Path, job_id: str, read_only=False) -> dict:
    job = (
        read_job(project, job_id)
        if read_only
        else portable_jobs.get_job(project, valid_job_id(job_id))
    )
    if job is None or job.get("project") != str(project):
        raise LookupError("job not found")
    return job


def log_path(project: Path, job_id: str) -> Path:
    path = project / "output/.staging" / valid_job_id(job_id) / "worker.log"
    try:
        path.parent.resolve(strict=True).relative_to(
            (project / "output/.staging").resolve(strict=True)
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError("job log") from error
    if path.is_symlink():
        raise ValueError("job log")
    return path


def has_log(project: Path, job_id: str) -> bool:
    try:
        return log_path(project, job_id).is_file()
    except (OSError, ValueError):
        return False


def failure_log_path(project: Path, job: dict) -> Path | None:
    """Return the worker-owned render detail only for a failed attempt."""
    if job.get("status") != "failed" or not isinstance(job.get("epoch"), int):
        return None
    path = (
        project
        / "output/.staging"
        / valid_job_id(job["job_id"])
        / f"attempt-{job['epoch']}"
        / "snapshot"
        / project.name
        / "output/final.mp4.render.log"
    )
    staging = project / "output/.staging"
    try:
        path.parent.resolve(strict=True).relative_to(staging.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None
    if path.is_symlink() or not path.is_file():
        return None
    return path


def redact(text: str) -> str:
    text = SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = BEARER.sub("Bearer [REDACTED]", text)
    text = OPENAI_KEY.sub("[REDACTED]", text)
    text = JWT.sub("[REDACTED]", text)
    text = COMMON_TOKEN.sub("[REDACTED]", text)
    return URL_AUTH.sub(r"\1[REDACTED]@", text)


def status(project_value, job_id):
    project = direct_project(project_value)
    try:
        job = bound_job(project, job_id, read_only=True)
    except LookupError:
        return envelope(project, "error", "job_not_found"), 2
    return envelope(project, "ok", "job_status", public_job(project, job)), 0


def logs(project_value, job_id, limit=DEFAULT_LOG_BYTES):
    project = direct_project(project_value)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LOG_BYTES:
        raise ValueError("log limit")
    try:
        job = bound_job(project, job_id, read_only=True)
    except LookupError:
        return envelope(project, "error", "job_not_found"), 2
    paths = [log_path(project, job_id)]
    detail = failure_log_path(project, job)
    if detail is not None:
        paths.append(detail)
    total = 0
    chunks = []
    for path in paths:
        try:
            size = path.stat().st_size
            total += size
            with path.open("rb") as handle:
                handle.seek(max(0, size - limit))
                chunks.append(handle.read(limit))
        except FileNotFoundError:
            continue
    payload = b"\n".join(chunks)[-limit:]
    text = redact(payload.decode("utf-8", errors="replace"))
    return (
        envelope(
            project,
            "ok",
            "job_logs",
            {
                "schema": "video-studio.job_logs.v1",
                "job_id": job["job_id"],
                "status": job["status"],
                "text": text,
                "total_bytes": total,
                "returned_bytes": len(payload),
                "truncated": total > len(payload),
                "redacted": text != payload.decode("utf-8", errors="replace"),
            },
        ),
        0,
    )


def cancel(project_value, job_id):
    project = direct_project(project_value)
    try:
        before = bound_job(project, job_id)
    except LookupError:
        return envelope(project, "error", "job_not_found"), 2
    job = portable_jobs.cancel(project, job_id)
    data = public_job(project, job)
    if job["status"] == "promoting":
        return envelope(project, "blocked", "job_commit_in_progress", data), 3
    code = "job_cancelled" if job["status"] == "cancelled" else "job_terminal"
    if before["status"] == "cancel_requested" and job["status"] == "cancelled":
        code = "job_cancelled"
    return envelope(project, "ok", code, data), 0


def resume(project_value, job_id, expected_tools_root=None):
    project = direct_project(project_value)
    try:
        job = bound_job(project, job_id)
    except LookupError:
        return envelope(project, "error", "job_not_found"), 2
    if expected_tools_root is not None:
        expected = Path(expected_tools_root)
        if expected.is_symlink() or not expected.is_dir():
            raise ValueError("tools root")
        if expected.resolve(strict=True) != Path(job["tools_root"]).resolve(strict=True):
            return envelope(project, "blocked", "job_tools_mismatch"), 3
    if job["status"] in portable_jobs.ACTIVE:
        return envelope(project, "ok", "job_running", public_job(project, job)), 0
    if job["status"] == "succeeded":
        return envelope(project, "ok", "job_terminal", public_job(project, job)), 0
    if not can_resume(project, job):
        return (
            envelope(
                project,
                "blocked",
                "job_resume_revision_changed",
                public_job(project, job),
            ),
            3,
        )
    try:
        resumed = portable_jobs.resume(project, job_id)
    except ValueError:
        return (
            envelope(project, "blocked", "job_resume_blocked", public_job(project, job)),
            3,
        )
    return envelope(project, "ok", "job_resumed", public_job(project, resumed)), 0


def main(argv):
    action = argv[1] if len(argv) > 1 else ""
    valid = (
        action in {"status", "cancel"} and len(argv) == 4
    ) or (action == "logs" and len(argv) in {4, 5}) or (
        action == "resume" and len(argv) in {4, 5}
    )
    if not valid:
        response, exit_code = envelope(None, "error", "invalid_input"), 2
    else:
        try:
            if action == "status":
                response, exit_code = status(argv[2], argv[3])
            elif action == "logs":
                limit = int(argv[4]) if len(argv) == 5 else DEFAULT_LOG_BYTES
                response, exit_code = logs(argv[2], argv[3], limit)
            elif action == "cancel":
                response, exit_code = cancel(argv[2], argv[3])
            else:
                response, exit_code = resume(
                    argv[2], argv[3], argv[4] if len(argv) == 5 else None
                )
        except (OSError, TypeError, ValueError):
            response, exit_code = envelope(
                Path(argv[2]), "error", "invalid_input"
            ), 2
    print(json.dumps(response, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
