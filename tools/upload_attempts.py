#!/usr/bin/env python3
"""Durable, fail-closed state for one YouTube publish intent.

This module owns no HTTP. Callers must durably enter an unknown state before
crossing a remote mutation seam, then record only authenticated provider
outcomes. Resumable session URLs live in the protected external credential
store and never in project state.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path

import youtube_credentials

SCHEMA = "haru.youtube_upload_attempt.v1"
SESSION_SCHEMA = "haru.youtube_upload_session.v1"
SHA256 = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
CALLER_KEY = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
PUBLIC_STATES = frozenset(
    {
        "prepared",
        "session_creation_unknown",
        "session_created",
        "remote_outcome_unknown",
        "uploading",
        "reconciliation_required",
        "video_uploaded",
        "complete",
        "aborted",
    }
)


class AttemptError(Exception):
    """A stable error code with no state, secret, or provider diagnostics."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str):
    raise AttemptError(code)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def publish_intent_id(approval_intent_sha256: str) -> str:
    if not isinstance(approval_intent_sha256, str):
        _fail("youtube_upload_intent_invalid")
    match = SHA256.fullmatch(approval_intent_sha256)
    if match is None:
        _fail("youtube_upload_intent_invalid")
    return _sha256_text(match.group(1))


def _canonical_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _fsync_directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_public_json(path: Path, value: dict):
    if path.is_symlink():
        _fail("youtube_upload_state_unsafe")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except AttemptError:
        raise
    except OSError:
        _fail("youtube_upload_state_unavailable")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _read_public(path: Path) -> dict:
    try:
        if path.is_symlink():
            _fail("youtube_upload_state_unsafe")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                _fail("youtube_upload_state_unsafe")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read()
        finally:
            os.close(descriptor)
        value = json.loads(raw.decode("utf-8"))
    except AttemptError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _fail("youtube_upload_state_invalid")
    if not isinstance(value, dict):
        _fail("youtube_upload_state_invalid")
    return value


def _session_directory(channel_id: str, *, create: bool) -> int:
    reference = f"youtube:{channel_id}"
    channel_fd = youtube_credentials._open_store_directory(reference, create=create)
    try:
        try:
            os.mkdir("sessions", 0o700, dir_fd=channel_fd)
            os.fsync(channel_fd)
        except FileExistsError:
            pass
        except FileNotFoundError:
            if not create:
                _fail("youtube_upload_session_unavailable")
        try:
            descriptor = os.open(
                "sessions",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=channel_fd,
            )
        except OSError:
            _fail("youtube_upload_session_unsafe")
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or (metadata.st_mode & 0o777) != 0o700
        ):
            os.close(descriptor)
            _fail("youtube_upload_session_unsafe")
        return descriptor
    finally:
        os.close(channel_fd)


def _session_name(intent_id: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", intent_id) is None:
        _fail("youtube_upload_intent_invalid")
    return f"{intent_id}.json"


def _valid_session_url(value: str) -> bool:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(value)
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "https"
        and isinstance(parsed.hostname, str)
        and (
            parsed.hostname == "www.googleapis.com"
            or parsed.hostname.endswith(".googleapis.com")
        )
    )


def _write_session(channel_id: str, intent_id: str, session_url: str, request_sha256: str):
    if not _valid_session_url(session_url):
        _fail("youtube_upload_session_invalid")
    directory_fd = _session_directory(channel_id, create=True)
    temporary = f".session.{uuid.uuid4().hex}.tmp"
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        value = {
            "schema": SESSION_SCHEMA,
            "publish_intent_id": intent_id,
            "request_sha256": request_sha256,
            "session_url": session_url,
        }
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or (metadata.st_mode & 0o777) != 0o600
            or metadata.st_nlink != 1
        ):
            _fail("youtube_upload_session_unsafe")
        os.replace(
            temporary,
            _session_name(intent_id),
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except AttemptError:
        raise
    except OSError:
        _fail("youtube_upload_session_unavailable")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        os.close(directory_fd)


def _read_session(channel_id: str, intent_id: str, request_sha256: str) -> str | None:
    try:
        directory_fd = _session_directory(channel_id, create=False)
    except (AttemptError, youtube_credentials.CredentialError) as error:
        code = getattr(error, "code", "")
        if code in {"youtube_upload_session_unavailable", "youtube_credential_unavailable"}:
            return None
        _fail("youtube_upload_session_unsafe")
    try:
        try:
            descriptor = os.open(
                _session_name(intent_id), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
        except FileNotFoundError:
            return None
        except OSError:
            _fail("youtube_upload_session_unsafe")
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or (metadata.st_mode & 0o777) != 0o600
                or metadata.st_nlink != 1
            ):
                _fail("youtube_upload_session_unsafe")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                value = json.loads(handle.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            _fail("youtube_upload_session_invalid")
        finally:
            os.close(descriptor)
        if (
            not isinstance(value, dict)
            or value.get("schema") != SESSION_SCHEMA
            or value.get("publish_intent_id") != intent_id
            or value.get("request_sha256") != request_sha256
            or not _valid_session_url(value.get("session_url"))
        ):
            _fail("youtube_upload_session_invalid")
        return value["session_url"]
    finally:
        os.close(directory_fd)


def _delete_session(channel_id: str, intent_id: str):
    try:
        directory_fd = _session_directory(channel_id, create=False)
    except (AttemptError, youtube_credentials.CredentialError) as error:
        code = getattr(error, "code", "")
        if code in {"youtube_upload_session_unavailable", "youtube_credential_unavailable"}:
            return
        _fail("youtube_upload_session_unsafe")
    try:
        try:
            os.unlink(_session_name(intent_id), dir_fd=directory_fd)
            os.fsync(directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            _fail("youtube_upload_session_unavailable")
    finally:
        os.close(directory_fd)


class UploadAttemptStore:
    def __init__(self, project: Path):
        project = Path(project)
        if not project.is_absolute() or project.is_symlink() or not project.is_dir():
            _fail("youtube_upload_state_unsafe")
        self.project = project.resolve()
        self.root = self.project / ".hvp" / "youtube-upload-attempts"
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            _fail("youtube_upload_state_unsafe")
        self.lock_path = self.root / ".lock"

    def _locked(self):
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        try:
            descriptor = os.open(self.lock_path, flags, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                os.close(descriptor)
                _fail("youtube_upload_state_unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return descriptor
        except AttemptError:
            raise
        except OSError:
            _fail("youtube_upload_state_unavailable")

    @staticmethod
    def _unlock(descriptor: int):
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    def _path(self, intent_id: str) -> Path:
        return self.root / _session_name(intent_id)

    def _validate(self, value: dict, intent_id: str) -> dict:
        if (
            value.get("schema") != SCHEMA
            or value.get("project") != self.project.name
            or value.get("publish_intent_id") != intent_id
            or value.get("state") not in PUBLIC_STATES
            or not SHA256.fullmatch(value.get("approval_intent_sha256", ""))
            or not SHA256.fullmatch(value.get("request_sha256", ""))
            or not CHANNEL_ID.fullmatch(value.get("target_channel_id", ""))
            or value.get("visibility") != "unlisted"
            or not isinstance(value.get("session_post_issued"), bool)
            or not isinstance(value.get("media_put_issued"), bool)
            or not isinstance(value.get("bytes_confirmed"), int)
            or isinstance(value.get("bytes_confirmed"), bool)
            or value.get("bytes_confirmed", -1) < 0
            or not isinstance(value.get("attempt_generation"), int)
            or value.get("attempt_generation", 0) < 1
            or not isinstance(value.get("caller_keys"), list)
            or not all(CALLER_KEY.fullmatch(key or "") for key in value["caller_keys"])
            or not isinstance(value.get("audit"), list)
        ):
            _fail("youtube_upload_state_invalid")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if "session_url" in value or "googleapis.com/upload/" in encoded:
            _fail("youtube_upload_state_unsafe")
        return value

    def _load(self, intent_id: str) -> dict:
        return self._validate(_read_public(self._path(intent_id)), intent_id)

    def _save(self, value: dict):
        value["updated_at"] = _now()
        self._validate(value, value["publish_intent_id"])
        _atomic_public_json(self._path(value["publish_intent_id"]), value)

    @staticmethod
    def _audit(value: dict, event: str, evidence=None):
        entry = {"event": event, "at": _now()}
        if evidence is not None:
            entry["evidence_sha256"] = _canonical_digest(evidence)
        value["audit"].append(entry)

    def prepare(
        self,
        *,
        approval_intent_sha256: str,
        request_sha256: str,
        target_channel_id: str,
        visibility: str,
        caller_key: str,
    ) -> dict:
        intent_id = publish_intent_id(approval_intent_sha256)
        if (
            SHA256.fullmatch(request_sha256 or "") is None
            or CHANNEL_ID.fullmatch(target_channel_id or "") is None
            or visibility != "unlisted"
            or CALLER_KEY.fullmatch(caller_key or "") is None
        ):
            _fail("youtube_upload_intent_invalid")
        lock = self._locked()
        try:
            path = self._path(intent_id)
            if path.exists():
                value = self._load(intent_id)
                immutable = (
                    value["approval_intent_sha256"] == approval_intent_sha256
                    and value["request_sha256"] == request_sha256
                    and value["target_channel_id"] == target_channel_id
                    and value["visibility"] == visibility
                )
                if not immutable:
                    _fail("youtube_upload_intent_conflict")
                if caller_key not in value["caller_keys"]:
                    value["caller_keys"].append(caller_key)
                    self._audit(value, "caller_joined")
                    self._save(value)
                return value
            value = {
                "schema": SCHEMA,
                "project": self.project.name,
                "publish_intent_id": intent_id,
                "approval_intent_sha256": approval_intent_sha256,
                "request_sha256": request_sha256,
                "target_channel_id": target_channel_id,
                "visibility": visibility,
                "state": "prepared",
                "attempt_generation": 1,
                "caller_keys": [caller_key],
                "session_post_issued": False,
                "media_put_issued": False,
                "bytes_confirmed": 0,
                "video_id": None,
                "remote_channel_id": None,
                "created_at": _now(),
                "updated_at": _now(),
                "audit": [],
            }
            self._audit(value, "prepared")
            self._save(value)
            return value
        finally:
            self._unlock(lock)

    def get(self, approval_intent_sha256: str) -> dict:
        intent_id = publish_intent_id(approval_intent_sha256)
        lock = self._locked()
        try:
            return self._load(intent_id)
        finally:
            self._unlock(lock)

    def before_session_post(self, approval_intent_sha256: str) -> dict:
        intent_id = publish_intent_id(approval_intent_sha256)
        lock = self._locked()
        try:
            value = self._load(intent_id)
            if value["state"] != "prepared" or value["session_post_issued"]:
                _fail("youtube_upload_reconciliation_required")
            value["state"] = "session_creation_unknown"
            value["session_post_issued"] = True
            self._audit(value, "session_post_issued")
            self._save(value)
            return value
        finally:
            self._unlock(lock)

    def record_session(
        self, approval_intent_sha256: str, session_url: str
    ) -> dict:
        intent_id = publish_intent_id(approval_intent_sha256)
        lock = self._locked()
        try:
            value = self._load(intent_id)
            if value["state"] != "session_creation_unknown":
                _fail("youtube_upload_transition_invalid")
            _write_session(
                value["target_channel_id"], intent_id, session_url, value["request_sha256"]
            )
            value["state"] = "session_created"
            self._audit(value, "session_recorded")
            self._save(value)
            return value
        finally:
            self._unlock(lock)

    def session(self, approval_intent_sha256: str) -> str | None:
        intent_id = publish_intent_id(approval_intent_sha256)
        lock = self._locked()
        try:
            value = self._load(intent_id)
            return _read_session(
                value["target_channel_id"], intent_id, value["request_sha256"]
            )
        finally:
            self._unlock(lock)

    def recover_recorded_session(self, approval_intent_sha256: str) -> dict:
        intent_id = publish_intent_id(approval_intent_sha256)
        lock = self._locked()
        try:
            value = self._load(intent_id)
            if value["state"] != "session_creation_unknown":
                return value
            if _read_session(value["target_channel_id"], intent_id, value["request_sha256"]) is None:
                _fail("youtube_upload_reconciliation_required")
            value["state"] = "session_created"
            self._audit(value, "session_record_recovered")
            self._save(value)
            return value
        finally:
            self._unlock(lock)

    def before_put(
        self,
        approval_intent_sha256: str,
        *,
        offset: int,
        body_bearing: bool = True,
    ) -> dict:
        intent_id = publish_intent_id(approval_intent_sha256)
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(body_bearing, bool)
        ):
            _fail("youtube_upload_transition_invalid")
        lock = self._locked()
        try:
            value = self._load(intent_id)
            if value["state"] not in {"session_created", "uploading"}:
                _fail("youtube_upload_reconciliation_required")
            if offset != value["bytes_confirmed"]:
                _fail("youtube_upload_transition_invalid")
            value["state"] = "remote_outcome_unknown"
            value["media_put_issued"] = value["media_put_issued"] or body_bearing
            self._audit(
                value,
                "media_put_issued" if body_bearing else "completion_query_issued",
                {"offset": offset},
            )
            self._save(value)
            return value
        finally:
            self._unlock(lock)

    def record_progress(self, approval_intent_sha256: str, accepted: int) -> dict:
        intent_id = publish_intent_id(approval_intent_sha256)
        lock = self._locked()
        try:
            value = self._load(intent_id)
            if (
                value["state"] != "remote_outcome_unknown"
                or not isinstance(accepted, int)
                or isinstance(accepted, bool)
                or accepted < value["bytes_confirmed"]
            ):
                _fail("youtube_upload_transition_invalid")
            if accepted > value["bytes_confirmed"]:
                value["bytes_confirmed"] = accepted
            value["state"] = "uploading"
            self._audit(value, "progress_confirmed", {"accepted": accepted})
            self._save(value)
            return value
        finally:
            self._unlock(lock)

    def record_video(
        self, approval_intent_sha256: str, *, video_id: str, channel_id: str
    ) -> dict:
        intent_id = publish_intent_id(approval_intent_sha256)
        lock = self._locked()
        try:
            value = self._load(intent_id)
            if (
                value["state"] != "remote_outcome_unknown"
                or VIDEO_ID.fullmatch(video_id or "") is None
                or channel_id != value["target_channel_id"]
            ):
                _fail("youtube_upload_remote_mismatch")
            value["state"] = "video_uploaded"
            value["video_id"] = video_id
            value["remote_channel_id"] = channel_id
            self._audit(value, "video_candidate_recorded")
            self._save(value)
            return value
        finally:
            self._unlock(lock)

    def require_reconciliation(self, approval_intent_sha256: str, reason: str) -> dict:
        intent_id = publish_intent_id(approval_intent_sha256)
        lock = self._locked()
        try:
            value = self._load(intent_id)
            if value["state"] in {"complete", "aborted"}:
                _fail("youtube_upload_transition_invalid")
            value["state"] = "reconciliation_required"
            self._audit(value, "reconciliation_required", {"reason": str(reason)})
            self._save(value)
            return value
        finally:
            self._unlock(lock)

    def session_expired(self, approval_intent_sha256: str) -> dict:
        intent_id = publish_intent_id(approval_intent_sha256)
        lock = self._locked()
        try:
            value = self._load(intent_id)
            if value["media_put_issued"]:
                value["state"] = "reconciliation_required"
                self._audit(value, "expired_after_media_put")
            elif value["state"] in {"session_created", "remote_outcome_unknown", "uploading"}:
                value["state"] = "prepared"
                value["session_post_issued"] = False
                value["attempt_generation"] += 1
                self._audit(value, "expired_before_media_put")
                self._save(value)
                _delete_session(value["target_channel_id"], intent_id)
            else:
                _fail("youtube_upload_transition_invalid")
            if value["state"] == "reconciliation_required":
                self._save(value)
            return value
        finally:
            self._unlock(lock)

    def authorize_restart(self, approval_intent_sha256: str, evidence: dict) -> dict:
        intent_id = publish_intent_id(approval_intent_sha256)
        lock = self._locked()
        try:
            value = self._load(intent_id)
            if (
                value["state"] != "reconciliation_required"
                or not isinstance(evidence, dict)
                or evidence.get("authenticated") is not True
                or evidence.get("authoritative_absence") is not True
                or evidence.get("channel_id") != value["target_channel_id"]
                or not isinstance(evidence.get("checked_at"), str)
            ):
                _fail("youtube_upload_reconciliation_required")
            value["state"] = "prepared"
            value["session_post_issued"] = False
            value["media_put_issued"] = False
            value["bytes_confirmed"] = 0
            value["video_id"] = None
            value["remote_channel_id"] = None
            value["attempt_generation"] += 1
            self._audit(value, "restart_authorized", evidence)
            self._save(value)
            _delete_session(value["target_channel_id"], intent_id)
            return value
        finally:
            self._unlock(lock)

    def complete(self, approval_intent_sha256: str, readback: dict) -> dict:
        intent_id = publish_intent_id(approval_intent_sha256)
        lock = self._locked()
        try:
            value = self._load(intent_id)
            if not isinstance(readback, dict):
                _fail("youtube_upload_remote_mismatch")
            discovered_video = readback.get("video_id")
            expected_video = value["video_id"] or discovered_video
            if (
                value["state"] not in {"video_uploaded", "reconciliation_required"}
                or readback.get("authenticated") is not True
                or VIDEO_ID.fullmatch(discovered_video or "") is None
                or discovered_video != expected_video
                or readback.get("channel_id") != value["target_channel_id"]
                or readback.get("privacy_status") != "unlisted"
                or readback.get("matches_approval") is not True
                or readback.get("thumbnail_matches") is not True
            ):
                _fail("youtube_upload_remote_mismatch")
            value["video_id"] = discovered_video
            value["remote_channel_id"] = readback["channel_id"]
            value["state"] = "complete"
            self._audit(value, "remote_readback_confirmed", readback)
            self._save(value)
            _delete_session(value["target_channel_id"], intent_id)
            return value
        finally:
            self._unlock(lock)
