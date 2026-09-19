"""Durable idempotency and spend reservations for synchronous TTS calls.

A process that reached the submitting state is ambiguous after restart. A retry
cannot prove whether the provider accepted the request, so the state becomes
submission_unknown and blocks until provider history proves a request ID.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
from typing import Iterator

SCHEMA_VERSION = 1
ACTIVE_STATUSES = frozenset(
    {"prepared", "submitting", "submission_unknown", "submitted"}
)
CHARGED_STATUSES = frozenset({"succeeded", "reconciled_spent"})
VALID_STATUSES = ACTIVE_STATUSES | CHARGED_STATUSES | frozenset(
    {"confirmed_failed"}
)


class JournalError(RuntimeError):
    pass


class BudgetExceeded(JournalError):
    pass


class SubmissionBlocked(JournalError):
    pass


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    base_key: str
    attempt_no: int
    status: str
    estimated_credits: int
    actual_credits: int | None
    provider_request_id: str | None
    provider_status: str | None
    failure_proof: str | None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class SpendJournal:
    def __init__(self, path: Path):
        expanded = Path(path).expanduser()
        self.path = (
            expanded
            if expanded.is_absolute()
            else Path.cwd() / expanded
        )
        self.lock_path = Path(str(self.path) + ".lock")

    @contextmanager
    def locked(self) -> Iterator["SpendJournal"]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
        except OSError as error:
            raise JournalError(f"cannot open TTS spend journal lock: {error}") from error
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            os.close(descriptor)
            raise JournalError(
                "TTS spend journal lock must be a current-user regular file"
            )
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._initialize()
            yield self
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(self.path, flags, 0o600)
            os.close(descriptor)
            metadata = self.path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise JournalError(
                "TTS spend journal must be a current-user regular file"
            )
        os.chmod(self.path, 0o600)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    attempt_id TEXT PRIMARY KEY,
                    base_key TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
                    status TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    request_payload_sha256 TEXT NOT NULL,
                    estimated_credits INTEGER NOT NULL CHECK (estimated_credits > 0),
                    actual_credits INTEGER,
                    output_base TEXT NOT NULL,
                    provider_request_id TEXT,
                    provider_status TEXT,
                    failure_proof TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(base_key, attempt_no)
                );
                CREATE INDEX IF NOT EXISTS attempts_base_key
                    ON attempts(base_key, attempt_no DESC);
                """
            )
            existing = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif existing["value"] != str(SCHEMA_VERSION):
                raise JournalError("unsupported TTS spend journal schema")
        directory = os.open(
            self.path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def latest(self, base_key: str) -> Attempt | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM attempts
                WHERE base_key=?
                ORDER BY attempt_no DESC
                LIMIT 1
                """,
                (base_key,),
            ).fetchone()
        return self._attempt(row) if row else None

    def get(self, attempt_id: str) -> Attempt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
        return self._attempt(row) if row else None

    def reserve(
        self,
        *,
        base_key: str,
        provider: str,
        model: str,
        request_payload_sha256: str,
        estimated_credits: int,
        output_base: str,
        budget_limit: int | None,
        retake: bool,
    ) -> Attempt:
        if estimated_credits <= 0:
            raise JournalError("estimated credits must be positive")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            latest_row = connection.execute(
                """
                SELECT * FROM attempts
                WHERE base_key=?
                ORDER BY attempt_no DESC
                LIMIT 1
                """,
                (base_key,),
            ).fetchone()
            latest = self._attempt(latest_row) if latest_row else None
            if latest_row is not None and (
                latest_row["provider"] != provider
                or latest_row["model"] != model
                or latest_row["request_payload_sha256"] != request_payload_sha256
                or latest_row["estimated_credits"] != estimated_credits
                or latest_row["output_base"] != output_base
            ):
                connection.rollback()
                raise JournalError("TTS idempotency key conflicts with stored request")
            if latest and latest.status == "submitting":
                timestamp = _now()
                connection.execute(
                    """
                    UPDATE attempts
                    SET status='submission_unknown',
                        provider_status='process_interrupted_during_submit',
                        failure_proof='no provider response was durably recorded',
                        updated_at=?
                    WHERE attempt_id=?
                    """,
                    (timestamp, latest.attempt_id),
                )
                connection.commit()
                raise SubmissionBlocked(
                    "submission_unknown: the previous process stopped after the "
                    "submit boundary; reconcile a provider request ID before retrying"
                )
            if latest and latest.status in {"submission_unknown", "submitted"}:
                connection.rollback()
                raise SubmissionBlocked(
                    f"{latest.status}: reconcile provider request "
                    f"{latest.provider_request_id or '(unknown)'} before retrying"
                )
            if latest and latest.status == "prepared":
                connection.commit()
                return latest
            if latest and latest.status == "succeeded" and not retake:
                connection.rollback()
                raise SubmissionBlocked(
                    "a succeeded request exists but its cache is unavailable; "
                    "restore the bound artifacts or use an explicit retake"
                )
            if (
                latest
                and latest.status in {"confirmed_failed", "reconciled_spent"}
                and not retake
            ):
                connection.rollback()
                raise SubmissionBlocked(
                    f"{latest.status}: a new paid attempt requires --retake"
                )

            used = self._used_credits(connection)
            if budget_limit is not None and used + estimated_credits > budget_limit:
                connection.rollback()
                raise BudgetExceeded(
                    f"TTS budget exceeded: journal usage {used} + reservation "
                    f"{estimated_credits} > cap {budget_limit}"
                )

            attempt_no = (latest.attempt_no + 1) if latest else 1
            attempt_id = f"{base_key[:32]}-{attempt_no:06d}-{secrets.token_hex(8)}"
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO attempts(
                    attempt_id, base_key, attempt_no, status, provider, model,
                    request_payload_sha256, estimated_credits, actual_credits,
                    output_base, provider_request_id, provider_status,
                    failure_proof, created_at, updated_at
                ) VALUES (?, ?, ?, 'prepared', ?, ?, ?, ?, NULL, ?, NULL, NULL,
                          NULL, ?, ?)
                """,
                (
                    attempt_id,
                    base_key,
                    attempt_no,
                    provider,
                    model,
                    request_payload_sha256,
                    estimated_credits,
                    output_base,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
            result = self.get(attempt_id)
            if result is None:
                raise JournalError("TTS spend reservation disappeared")
            return result
        finally:
            connection.close()

    def mark_submitting(self, attempt_id: str) -> Attempt:
        return self._transition(
            attempt_id,
            allowed={"prepared"},
            status="submitting",
            provider_status="request_body_ready",
        )

    def mark_submission_unknown(
        self,
        attempt_id: str,
        *,
        error: str,
        provider_request_id: str | None = None,
    ) -> Attempt:
        return self._transition(
            attempt_id,
            allowed={"submitting", "submitted", "submission_unknown"},
            status="submission_unknown",
            provider_request_id=provider_request_id,
            provider_status="submission_unknown",
            failure_proof=error,
        )

    def mark_confirmed_failure(self, attempt_id: str, *, proof: str) -> Attempt:
        if not proof.strip():
            raise JournalError("confirmed failure requires durable proof")
        return self._transition(
            attempt_id,
            allowed={"prepared", "submitting"},
            status="confirmed_failed",
            provider_status="rejected_before_charge",
            failure_proof=proof,
        )

    def mark_submitted(
        self,
        attempt_id: str,
        *,
        provider_request_id: str,
        provider_status: str,
    ) -> Attempt:
        if not provider_request_id.strip():
            raise JournalError("submitted request requires provider request ID")
        return self._transition(
            attempt_id,
            allowed={"submitting"},
            status="submitted",
            provider_request_id=provider_request_id,
            provider_status=provider_status,
        )

    def mark_succeeded(self, attempt_id: str, *, actual_credits: int) -> Attempt:
        if actual_credits < 0:
            raise JournalError("actual credits cannot be negative")
        return self._transition(
            attempt_id,
            allowed={"submitted", "succeeded"},
            status="succeeded",
            actual_credits=actual_credits,
            provider_status="succeeded",
            failure_proof=None,
        )

    def mark_reconciled_spent(
        self,
        attempt_id: str,
        *,
        provider_request_id: str,
        actual_credits: int,
        proof: str,
    ) -> Attempt:
        if not provider_request_id.strip() or actual_credits < 0 or not proof.strip():
            raise JournalError("reconciliation needs request ID, cost, and proof")
        return self._transition(
            attempt_id,
            allowed={"submitting", "submission_unknown", "submitted"},
            status="reconciled_spent",
            provider_request_id=provider_request_id,
            provider_status="accepted_confirmed_by_provider_history",
            actual_credits=actual_credits,
            failure_proof=proof,
        )

    def import_legacy_success(
        self,
        *,
        base_key: str,
        provider: str,
        model: str,
        request_payload_sha256: str,
        estimated_credits: int,
        output_base: str,
        provider_request_id: str,
        actual_credits: int,
    ) -> Attempt:
        existing = self.latest(base_key)
        if existing:
            return existing
        reserved = self.reserve(
            base_key=base_key,
            provider=provider,
            model=model,
            request_payload_sha256=request_payload_sha256,
            estimated_credits=estimated_credits,
            output_base=output_base,
            budget_limit=None,
            retake=False,
        )
        self.mark_submitting(reserved.attempt_id)
        self.mark_submitted(
            reserved.attempt_id,
            provider_request_id=provider_request_id,
            provider_status="legacy_receipt_import",
        )
        return self.mark_succeeded(
            reserved.attempt_id,
            actual_credits=actual_credits,
        )

    def used_credits(self) -> int:
        with self._connect() as connection:
            return self._used_credits(connection)

    def _used_credits(self, connection: sqlite3.Connection) -> int:
        rows = connection.execute(
            "SELECT status, estimated_credits, actual_credits FROM attempts"
        ).fetchall()
        total = 0
        for row in rows:
            if row["status"] in ACTIVE_STATUSES:
                total += int(row["estimated_credits"])
            elif row["status"] in CHARGED_STATUSES:
                total += int(
                    row["actual_credits"]
                    if row["actual_credits"] is not None
                    else row["estimated_credits"]
                )
        return total

    def _transition(
        self,
        attempt_id: str,
        *,
        allowed: set[str],
        status: str,
        provider_request_id: str | None = None,
        provider_status: str | None = None,
        actual_credits: int | None = None,
        failure_proof: str | None = None,
    ) -> Attempt:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise JournalError("TTS spend attempt was not found")
            current = self._attempt(row)
            if current.status == status:
                if (
                    actual_credits is not None
                    and current.actual_credits != actual_credits
                ):
                    connection.rollback()
                    raise JournalError(
                        "idempotent TTS transition changed actual credits"
                    )
                if (
                    provider_request_id is not None
                    and current.provider_request_id not in {
                        None,
                        provider_request_id,
                    }
                ):
                    connection.rollback()
                    raise JournalError(
                        "idempotent TTS transition changed provider request ID"
                    )
                connection.commit()
                return current
            if current.status not in allowed:
                connection.rollback()
                raise JournalError(
                    f"invalid TTS spend transition {current.status} -> {status}"
                )
            connection.execute(
                """
                UPDATE attempts SET
                    status=?,
                    provider_request_id=COALESCE(?, provider_request_id),
                    provider_status=COALESCE(?, provider_status),
                    actual_credits=COALESCE(?, actual_credits),
                    failure_proof=?,
                    updated_at=?
                WHERE attempt_id=?
                """,
                (
                    status,
                    provider_request_id,
                    provider_status,
                    actual_credits,
                    failure_proof,
                    _now(),
                    attempt_id,
                ),
            )
            connection.commit()
            result = self.get(attempt_id)
            if result is None:
                raise JournalError("TTS spend attempt disappeared")
            return result
        finally:
            connection.close()

    @staticmethod
    def _attempt(row: sqlite3.Row) -> Attempt:
        if row["status"] not in VALID_STATUSES:
            raise JournalError("TTS spend journal contains an unknown status")
        return Attempt(
            attempt_id=row["attempt_id"],
            base_key=row["base_key"],
            attempt_no=int(row["attempt_no"]),
            status=row["status"],
            estimated_credits=int(row["estimated_credits"]),
            actual_credits=(
                int(row["actual_credits"])
                if row["actual_credits"] is not None
                else None
            ),
            provider_request_id=row["provider_request_id"],
            provider_status=row["provider_status"],
            failure_proof=row["failure_proof"],
        )

    def dump(self) -> dict:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM attempts ORDER BY created_at, attempt_no"
            ).fetchall()
        return {
            "schema": "video_studio.tts_spend_journal.v1",
            "path": str(self.path),
            "used_credits": self.used_credits(),
            "attempts": [dict(row) for row in rows],
        }


def payload_digest(value: dict) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
