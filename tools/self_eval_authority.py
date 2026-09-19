#!/usr/bin/env python3
"""External protected authority for the render self-evaluation gate.

A project tree is writable by everything that touches the project, so a receipt
stored only inside it proves nothing: anyone able to write `quality-review/`
could fabricate an internally consistent pass, or restore an older tree that
once passed. This module keeps the deciding state outside the project:

* a monotonic generation ledger, owner-only, hash-chained, with a pointer to the
  latest record. The project JSON is an audit mirror; the ledger is authority.
* one-time review attestations, minted by an external issuer and consumed here,
  so `reviewer_kind`/`provider`/`model` strings stay metadata and cannot mint a
  human or vision decision.

Everything project-shaped is a callback: this module never learns the project
layout, and the engine never learns how authority is stored.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

STATE_ROOT_ENV = "HARU_VIDEO_STUDIO_SELF_EVAL_STATE_ROOT"
ATTESTATION_ROOT_ENV = "HARU_VIDEO_STUDIO_SELF_EVAL_ATTESTATION_ROOT"

TRANSACTION_SCHEMA = "haru.render_self_eval_transaction.v1"
GENERATION_SCHEMA = "haru.render_self_eval_ledger_generation.v1"
POINTER_SCHEMA = "haru.render_self_eval_ledger_current.v1"
VISION_ATTESTATION_SCHEMA = "haru.self_eval_vision_attestation.v1"
HUMAN_ATTESTATION_SCHEMA = "haru.self_eval_human_attestation.v1"
VISION_ATTESTATION_SCHEMA_V2 = "haru.self_eval_vision_attestation.v2"
HUMAN_ATTESTATION_SCHEMA_V2 = "haru.self_eval_human_attestation.v2"
SIGNED_STATEMENT_SCHEMA = "haru.self_eval_authorization_statement.v1"
VISION_UNAVAILABLE_ACTION = "declare_vision_unavailable"
HUMAN_REVIEW_ACTION = "record_human_fallback_review"
SIGNATURE_ALGORITHM = "ecdsa-p256-sha256"
OPENSSL = "/usr/bin/openssl"

ATTESTATION_REF = re.compile(
    r"^self-eval-attestation:([A-Za-z0-9][A-Za-z0-9._-]{0,127})$"
)
GENERATION_NAME = re.compile(r"^[0-9]{8}\.json$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SIGNED_ATTESTATION_REF = re.compile(
    r"^self-eval-attestation:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

TRANSITIONS = (
    "evaluate_pending",
    "evaluate_fail",
    "vision_unavailable",
    "vision_pass",
    "vision_fail",
    "human_pass",
    "human_fail",
)
EVALUATE_TRANSITIONS = ("evaluate_pending", "evaluate_fail")

PENDING_KEYS = {
    "schema",
    "project_id",
    "project_path_sha256",
    "transaction_id",
    "transition",
    "generation",
    "predecessor",
    "attempt",
    "attempt_identity",
    "expected_project_refs",
    "retirement",
    "prepared_at",
}
GENERATION_KEYS = {
    "schema",
    "project_id",
    "project_path_sha256",
    "generation",
    "transition",
    "transaction_id",
    "predecessor",
    "attempt",
    "attempt_identity",
    "project_refs",
    "authority",
    "recorded_at",
}
POINTER_KEYS = {
    "schema",
    "project_id",
    "project_path_sha256",
    "generation",
    "record_sha256",
}
ATTESTATION_KEYS = {
    "schema",
    "attestation_ref",
    "project_id",
    "review_intent_sha256",
    "nonce",
    "generation",
    "issued_at",
    "consumed_at",
    "consumed_project_id",
    "consumed_intent_sha256",
}
SIGNED_ATTESTATION_IMMUTABLE_KEYS = {
    "schema",
    "attestation_ref",
    "action",
    "project_id",
    "project_root_sha256",
    "review_intent_sha256",
    "self_eval_result",
    "nonce",
    "generation",
    "issued_at",
    "expires_at",
    "key_id",
    "signature_algorithm",
}
SIGNED_ATTESTATION_KEYS = SIGNED_ATTESTATION_IMMUTABLE_KEYS | {
    "signature_base64",
    "consumed_at",
    "consumed_project_id",
    "consumed_intent_sha256",
}
AUTHORITY_KEYS = {
    "schema",
    "attestation_ref",
    "review_intent_sha256",
    "nonce",
    "generation",
    "consumed_at",
}

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
CHUNK = 1024 * 1024


class AuthorityError(ValueError):
    """Any refusal of external authority. Always fail closed."""


# --------------------------------------------------------------------------
# canonical bytes
# --------------------------------------------------------------------------


def canonical_bytes(value) -> bytes:
    """Sorted, minified, UTF-8, no trailing newline. Non-finite numbers fail."""
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise AuthorityError("value is not canonical JSON") from error
    return text.encode("utf-8")


def canonical_digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def is_sha256(value) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def is_ref(value) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == {"path", "sha256", "bytes"}
        and isinstance(value.get("path"), str)
        and value["path"]
        and is_sha256(value.get("sha256"))
        and isinstance(value.get("bytes"), int)
        and not isinstance(value["bytes"], bool)
        and value["bytes"] >= 0
    )


def sorted_refs(refs) -> list:
    return sorted(refs, key=lambda ref: ref["path"])


# --------------------------------------------------------------------------
# project file descriptors: never a caller path, never a symlink component
# --------------------------------------------------------------------------


def relative_parts(relative) -> tuple:
    text = relative if isinstance(relative, str) else str(relative)
    if not text or text.startswith("/") or "\\" in text:
        raise AuthorityError(f"project path must be relative: {text!r}")
    parts = PurePosixPath(text).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise AuthorityError(f"project path escapes the project: {text!r}")
    return parts


def open_project_fd(project) -> int:
    """Open the already-resolved project root without following an ABA symlink."""
    root = os.path.abspath(os.fspath(project))
    try:
        return os.open(root, DIR_FLAGS)
    except OSError as error:
        raise AuthorityError("project directory is unavailable") from error


def open_dir_at(dir_fd: int, name: str) -> int:
    try:
        return os.open(name, DIR_FLAGS, dir_fd=dir_fd)
    except OSError as error:
        raise AuthorityError(f"directory is unavailable: {name}") from error


def open_project_dir_fd(project, relative: str = "") -> int:
    descriptor = open_project_fd(project)
    if not relative:
        return descriptor
    try:
        for part in relative_parts(relative):
            child = open_dir_at(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise




def open_relative_dir_fd(project_fd: int, relative: str = "") -> int:
    descriptor = os.dup(project_fd)
    if not relative:
        return descriptor
    try:
        for part in relative_parts(relative):
            child = open_dir_at(descriptor, part)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_relative_file_fd(project_fd: int, relative: str) -> int:
    parts = relative_parts(relative)
    parent = (
        str(PurePosixPath(*parts[:-1])) if len(parts) > 1 else ""
    )
    descriptor = open_relative_dir_fd(project_fd, parent)
    try:
        return open_file_at(descriptor, parts[-1])
    finally:
        os.close(descriptor)
def open_file_at(dir_fd: int, name: str) -> int:
    try:
        descriptor = os.open(name, os.O_RDONLY | NOFOLLOW, dir_fd=dir_fd)
    except OSError as error:
        raise AuthorityError(f"file is unavailable: {name}") from error
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise AuthorityError(f"not a regular file: {name}")
    return descriptor


def open_protected_file_at(dir_fd: int, name: str) -> int:
    descriptor = open_file_at(dir_fd, name)
    info = os.fstat(descriptor)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(descriptor)
        raise AuthorityError(f"protected leaf permissions are invalid: {name}")
    return descriptor


def open_project_file_fd(project, relative: str) -> int:
    parts = relative_parts(relative)
    descriptor = open_project_dir_fd(project, str(PurePosixPath(*parts[:-1])) if len(parts) > 1 else "")
    try:
        return open_file_at(descriptor, parts[-1])
    finally:
        os.close(descriptor)


def read_fd(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        block = os.read(fd, CHUNK)
        if not block:
            break
        chunks.append(block)
    payload = b"".join(chunks)
    if os.fstat(fd).st_size != len(payload):
        raise AuthorityError("file changed while being read")
    return payload


def hash_fd(fd: int) -> tuple:
    """Digest and size of the bytes this descriptor holds, not of a pathname."""
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while True:
        block = os.read(fd, CHUNK)
        if not block:
            break
        digest.update(block)
        total += len(block)
    if os.fstat(fd).st_size != total:
        raise AuthorityError("file changed while being hashed")
    return digest.hexdigest(), total


def ref_at(dir_fd: int, name: str, path: str) -> dict:
    fd = open_file_at(dir_fd, name)
    try:
        digest, size = hash_fd(fd)
    finally:
        os.close(fd)
    return {"path": path, "sha256": digest, "bytes": size}


def verify_refs(project, refs) -> str:
    """Return "match", "absent", or "mismatch" for a whole ref set."""
    present = 0
    matched = 0
    for ref in refs:
        try:
            fd = open_project_file_fd(project, ref["path"])
        except AuthorityError:
            continue
        try:
            digest, size = hash_fd(fd)
        except AuthorityError:
            present += 1
            continue
        finally:
            os.close(fd)
        present += 1
        if digest == ref["sha256"] and size == ref["bytes"]:
            matched += 1
    if matched == len(refs):
        return "match"
    if present == 0:
        return "absent"
    return "mismatch"


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to protected state")
        view = view[written:]


def write_leaf_at(dir_fd: int, name: str, payload: bytes, *, exclusive: bool = True) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(dir_fd)


def atomic_leaf_at(dir_fd: int, name: str, payload: bytes) -> None:
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    fsync_dir(dir_fd)


def fsync_dir(dir_fd: int) -> None:
    try:
        os.fsync(dir_fd)
    except OSError:
        # Directory fsync is unsupported on some filesystems; the rename is
        # still ordered, and refusing here would block every write.
        pass


# --------------------------------------------------------------------------
# external roots
# --------------------------------------------------------------------------


def state_root() -> Path:
    configured = os.environ.get(STATE_ROOT_ENV)
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local/state/video-studio/self-eval"
    )


def attestation_root() -> Path:
    configured = os.environ.get(ATTESTATION_ROOT_ENV)
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local/state/video-studio/self-eval-attestations"
    )


def project_path_sha256(project) -> str:
    # Engine entrypoints resolve once, then all later opens are O_NOFOLLOW.
    # Re-resolving here after an attacker swaps the pathname would select a
    # different authority namespace for the same in-flight transaction.
    resolved_at_entry = os.path.abspath(os.fspath(project))
    return hashlib.sha256(os.fsencode(resolved_at_entry)).hexdigest()


def _owner_only_dir(path: Path, *, create: bool) -> None:
    if create and not path.exists():
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as error:
        raise AuthorityError(f"protected state is unavailable: {path.name}") from error
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise AuthorityError(f"protected state permissions are invalid: {path.name}")


class Namespace:
    """Held descriptors for one project's protected ledger, under flock."""

    __slots__ = ("project", "project_id", "path_sha256", "path", "fd", "pending_fd", "generations_fd", "lock_fd")

    def __init__(self, project, project_id: str):
        self.project = Path(project)
        self.project_id = project_id
        self.path_sha256 = project_path_sha256(project)
        root = state_root()
        if not root.is_absolute():
            raise AuthorityError("protected state root must be absolute")
        _owner_only_dir(root, create=True)
        self.path = root / self.path_sha256
        _owner_only_dir(self.path, create=True)
        _owner_only_dir(self.path / "pending", create=True)
        _owner_only_dir(self.path / "generations", create=True)
        self.fd = os.open(self.path, DIR_FLAGS)
        self.pending_fd = -1
        self.generations_fd = -1
        self.lock_fd = -1
        try:
            self.pending_fd = open_dir_at(self.fd, "pending")
            self.generations_fd = open_dir_at(self.fd, "generations")
            self.lock_fd = os.open(
                "lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=self.fd,
            )
            lock_info = os.fstat(self.lock_fd)
            if (
                not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_uid != os.getuid()
                or stat.S_IMODE(lock_info.st_mode) != 0o600
            ):
                raise AuthorityError("protected ledger lock permissions are invalid")
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        for name in ("lock_fd", "generations_fd", "pending_fd", "fd"):
            fd = getattr(self, name)
            if fd is not None and fd >= 0:
                if name == "lock_fd":
                    with contextlib.suppress(OSError):
                        fcntl.flock(fd, fcntl.LOCK_UN)
                with contextlib.suppress(OSError):
                    os.close(fd)
                setattr(self, name, -1)


@contextlib.contextmanager
def locked(project, project_id: str):
    namespace = Namespace(project, project_id)
    try:
        yield namespace
    finally:
        namespace.close()


# --------------------------------------------------------------------------
# chain
# --------------------------------------------------------------------------


class Chain:
    __slots__ = ("records", "digests")

    def __init__(self, records, digests):
        self.records = records
        self.digests = digests

    @property
    def generation(self) -> int:
        return len(self.records)

    @property
    def latest(self):
        return self.records[-1] if self.records else None

    @property
    def latest_sha256(self):
        return self.digests[-1] if self.digests else None

    def predecessor(self):
        if not self.records:
            return None
        return {"generation": self.generation, "sha256": self.latest_sha256}


def _valid_record(value, namespace: Namespace, generation: int) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == GENERATION_KEYS
        and value.get("schema") == GENERATION_SCHEMA
        and value.get("project_id") == namespace.project_id
        and value.get("project_path_sha256") == namespace.path_sha256
        and value.get("generation") == generation
        and value.get("transition") in TRANSITIONS
        and is_sha256(value.get("transaction_id"))
        and isinstance(value.get("attempt"), int)
        and not isinstance(value["attempt"], bool)
        and value["attempt"] >= 1
        and is_sha256(value.get("attempt_identity"))
        and isinstance(value.get("project_refs"), list)
        and value["project_refs"]
        and all(is_ref(ref) for ref in value["project_refs"])
        and value["project_refs"] == sorted_refs(value["project_refs"])
        and isinstance(value.get("recorded_at"), str)
    )


def validated_chain(namespace: Namespace) -> Chain:
    """Read every retained generation and recompute every digest."""
    names = sorted(
        name
        for name in os.listdir(namespace.generations_fd)
        if GENERATION_NAME.fullmatch(name)
    )
    records = []
    digests = []
    for index, name in enumerate(names, 1):
        fd = open_protected_file_at(namespace.generations_fd, name)
        try:
            payload = read_fd(fd)
        finally:
            os.close(fd)
        if int(name[:8]) != index:
            raise AuthorityError("protected ledger has a generation gap")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AuthorityError("protected ledger record is malformed") from error
        if not _valid_record(value, namespace, index):
            raise AuthorityError("protected ledger record is malformed")
        if canonical_bytes(value) != payload:
            raise AuthorityError("protected ledger record is not canonical")
        expected_predecessor = (
            None if index == 1 else {"generation": index - 1, "sha256": digests[-1]}
        )
        if value["predecessor"] != expected_predecessor:
            raise AuthorityError("protected ledger predecessor chain is broken")
        authority = value["authority"]
        if value["transition"] in EVALUATE_TRANSITIONS:
            if authority is not None:
                raise AuthorityError("evaluate generation must carry no authority")
        elif not _valid_authority(authority):
            raise AuthorityError("review generation authority is malformed")
        records.append(value)
        digests.append(hashlib.sha256(payload).hexdigest())
    chain = Chain(records, digests)
    _repair_pointer(namespace, chain)
    return chain


def _pointer(namespace: Namespace):
    try:
        os.stat("current.json", dir_fd=namespace.fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    fd = open_protected_file_at(namespace.fd, "current.json")
    try:
        payload = read_fd(fd)
    finally:
        os.close(fd)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuthorityError("protected ledger pointer is malformed") from error
    if not isinstance(value, dict) or set(value) != POINTER_KEYS:
        raise AuthorityError("protected ledger pointer is malformed")
    return value


def _repair_pointer(namespace: Namespace, chain: Chain) -> None:
    pointer = _pointer(namespace)
    if chain.latest is None:
        if pointer is not None:
            raise AuthorityError("protected ledger pointer has no record")
        return
    expected = {
        "schema": POINTER_SCHEMA,
        "project_id": namespace.project_id,
        "project_path_sha256": namespace.path_sha256,
        "generation": chain.generation,
        "record_sha256": chain.latest_sha256,
    }
    if pointer == expected:
        return
    if pointer is not None and (
        pointer.get("schema") != POINTER_SCHEMA
        or pointer.get("project_id") != namespace.project_id
        or pointer.get("project_path_sha256") != namespace.path_sha256
        or not isinstance(pointer.get("generation"), int)
        or isinstance(pointer["generation"], bool)
        or pointer["generation"] > chain.generation
        or pointer["generation"] < 1
        or pointer.get("record_sha256") != chain.digests[pointer["generation"] - 1]
    ):
        raise AuthorityError("protected ledger pointer does not match the chain")
    # A crash between the immutable record and the pointer leaves the chain
    # valid and the pointer behind. The chain is authority, so completing the
    # pointer is recovery, not a state change.
    atomic_leaf_at(namespace.fd, "current.json", canonical_bytes(expected))


def latest_record(project, project_id: str):
    with locked(project, project_id) as namespace:
        return validated_chain(namespace).latest


# --------------------------------------------------------------------------
# attestations
# --------------------------------------------------------------------------


def is_vision_attestation_schema(value) -> bool:
    return value in (VISION_ATTESTATION_SCHEMA, VISION_ATTESTATION_SCHEMA_V2)


def is_human_attestation_schema(value) -> bool:
    return value in (HUMAN_ATTESTATION_SCHEMA, HUMAN_ATTESTATION_SCHEMA_V2)


def _semantic_attestation_schema(value):
    if is_vision_attestation_schema(value):
        return VISION_ATTESTATION_SCHEMA
    if is_human_attestation_schema(value):
        return HUMAN_ATTESTATION_SCHEMA
    return None


def same_attestation_family(left, right) -> bool:
    family = _semantic_attestation_schema(left)
    return family is not None and family == _semantic_attestation_schema(right)


def _valid_authority(value) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == AUTHORITY_KEYS
        and _semantic_attestation_schema(value.get("schema")) is not None
        and isinstance(value.get("attestation_ref"), str)
        and ATTESTATION_REF.fullmatch(value["attestation_ref"])
        and is_sha256(value.get("review_intent_sha256"))
        and isinstance(value.get("nonce"), str)
        and value["nonce"]
        and isinstance(value.get("generation"), int)
        and not isinstance(value["generation"], bool)
        and value["generation"] >= 1
        and isinstance(value.get("consumed_at"), str)
        and value["consumed_at"]
    )


def attestation_path(ref) -> Path:
    match = ATTESTATION_REF.fullmatch(ref) if isinstance(ref, str) else None
    if not match:
        raise AuthorityError("attestation_ref is malformed")
    root = attestation_root()
    if not root.is_absolute():
        raise AuthorityError("protected attestation root must be absolute")
    _owner_only_dir(root, create=False)
    path = root / f"{match.group(1)}.json"
    try:
        path.relative_to(root)
    except ValueError as error:
        raise AuthorityError("attestation_ref escapes protected state") from error
    return path


def _open_attestation(ref, flags: int) -> int:
    path = attestation_path(ref)
    try:
        fd = os.open(path, flags | NOFOLLOW)
    except OSError as error:
        raise AuthorityError("protected attestation is unavailable") from error
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        os.close(fd)
        raise AuthorityError("protected attestation permissions are invalid")
    return fd


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise AuthorityError("protected attestation contains duplicate keys")
        value[key] = item
    return value


def _parse_utc_timestamp(value, name):
    if not isinstance(value, str) or UTC_TIMESTAMP.fullmatch(value) is None:
        raise AuthorityError(f"protected attestation {name} is malformed")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as error:
        raise AuthorityError(f"protected attestation {name} is malformed") from error
    if parsed.tzinfo != dt.timezone.utc or parsed.microsecond:
        raise AuthorityError(f"protected attestation {name} is malformed")
    return parsed


def _decode_base64(value, description):
    if not isinstance(value, str) or not value:
        raise AuthorityError(f"{description} is malformed")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as error:
        raise AuthorityError(f"{description} is malformed") from error


def load_signer_pin():
    """Use the reviewed, source-fingerprinted publish signer pin."""
    try:
        import approval_attestation

        return approval_attestation.load_signer_pin()
    except (ImportError, ValueError) as error:
        raise AuthorityError("self-eval operator issuer is not enrolled") from error


def _validate_signer_pin(value):
    if not isinstance(value, dict) or set(value) != {
        "algorithm",
        "key_id",
        "public_key_x963_base64",
    }:
        raise AuthorityError("self-eval operator signer pin is malformed")
    if value.get("algorithm") != SIGNATURE_ALGORITHM or not is_sha256(
        value.get("key_id")
    ):
        raise AuthorityError("self-eval operator signer pin is malformed")
    public_key = _decode_base64(
        value.get("public_key_x963_base64"), "self-eval operator signer pin"
    )
    if (
        len(public_key) != 65
        or public_key[0] != 0x04
        or hashlib.sha256(public_key).hexdigest() != value["key_id"]
    ):
        raise AuthorityError("self-eval operator signer pin is malformed")
    return dict(value), public_key


def _spki_der(public_key):
    return bytes.fromhex(
        "3059"
        "3013"
        "06072a8648ce3d0201"
        "06082a8648ce3d030107"
        "034200"
    ) + public_key


def canonical_signed_statement(immutable):
    return canonical_bytes(
        {
            "schema": SIGNED_STATEMENT_SCHEMA,
            "action": immutable["action"],
            "attestation": immutable,
        }
    )


def _write_private_file(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify_p256_signature(statement, signature, public_key):
    with tempfile.TemporaryDirectory(prefix="hvp-self-eval-verify-") as directory:
        root = Path(directory)
        public_path = root / "public.der"
        signature_path = root / "signature.der"
        statement_path = root / "statement.json"
        _write_private_file(public_path, _spki_der(public_key))
        _write_private_file(signature_path, signature)
        _write_private_file(statement_path, statement)
        try:
            result = subprocess.run(
                [
                    OPENSSL,
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(public_path),
                    "-keyform",
                    "DER",
                    "-signature",
                    str(signature_path),
                    str(statement_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
                env={
                    "HOME": tempfile.gettempdir(),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin",
                },
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise AuthorityError("self-eval attestation signature could not be verified") from error
    return result.returncode == 0


def _validate_signed_attestation(value, schema: str, ref: str) -> None:
    expected_action = (
        VISION_UNAVAILABLE_ACTION
        if schema == VISION_ATTESTATION_SCHEMA_V2
        else HUMAN_REVIEW_ACTION
    )
    if (
        set(value) != SIGNED_ATTESTATION_KEYS
        or value.get("schema") != schema
        or value.get("attestation_ref") != ref
        or SIGNED_ATTESTATION_REF.fullmatch(ref) is None
        or value.get("action") != expected_action
        or not isinstance(value.get("project_id"), str)
        or not value["project_id"]
        or not is_sha256(value.get("project_root_sha256"))
        or not is_sha256(value.get("review_intent_sha256"))
        or not is_ref(value.get("self_eval_result"))
        or not isinstance(value.get("nonce"), str)
        or SHA256.fullmatch(value["nonce"]) is None
        or not isinstance(value.get("generation"), int)
        or isinstance(value["generation"], bool)
        or value["generation"] < 1
        or not is_sha256(value.get("key_id"))
        or value.get("signature_algorithm") != SIGNATURE_ALGORITHM
    ):
        raise AuthorityError("protected signed attestation is malformed")
    issued_at = _parse_utc_timestamp(value.get("issued_at"), "issued_at")
    expires_at = _parse_utc_timestamp(value.get("expires_at"), "expires_at")
    if expires_at <= issued_at or expires_at - issued_at > dt.timedelta(seconds=300):
        raise AuthorityError("protected signed attestation lifetime is malformed")
    consumed = (
        value.get("consumed_at"),
        value.get("consumed_project_id"),
        value.get("consumed_intent_sha256"),
    )
    if consumed != (None, None, None):
        if (
            not isinstance(consumed[0], str)
            or not consumed[0]
            or not isinstance(consumed[1], str)
            or not consumed[1]
            or not is_sha256(consumed[2])
        ):
            raise AuthorityError("protected signed attestation consumption is malformed")
        consumed_at = _parse_utc_timestamp(consumed[0], "consumed_at")
        if consumed_at < issued_at or consumed_at >= expires_at:
            raise AuthorityError("protected signed attestation was consumed outside its validity interval")
    pin, public_key = _validate_signer_pin(load_signer_pin())
    if value["key_id"] != pin["key_id"]:
        raise AuthorityError("self-eval operator issuer is not enrolled")
    immutable = {key: value[key] for key in SIGNED_ATTESTATION_IMMUTABLE_KEYS}
    signature = _decode_base64(
        value.get("signature_base64"), "self-eval attestation signature"
    )
    if not 64 <= len(signature) <= 80 or not _verify_p256_signature(
        canonical_signed_statement(immutable), signature, public_key
    ):
        raise AuthorityError("self-eval attestation signature is invalid")


def _read_attestation(fd: int, schema: str, ref: str) -> dict:
    try:
        value = json.loads(
            read_fd(fd).decode("utf-8"), object_pairs_hook=_strict_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError, AuthorityError) as error:
        raise AuthorityError("protected attestation is malformed") from error
    semantic_schema = _semantic_attestation_schema(schema)
    signed_schema = {
        VISION_ATTESTATION_SCHEMA: VISION_ATTESTATION_SCHEMA_V2,
        HUMAN_ATTESTATION_SCHEMA: HUMAN_ATTESTATION_SCHEMA_V2,
    }.get(semantic_schema)
    if isinstance(value, dict) and value.get("schema") == signed_schema:
        _validate_signed_attestation(value, signed_schema, ref)
        return value
    if (
        not isinstance(value, dict)
        or set(value) != ATTESTATION_KEYS
        or value.get("schema") != semantic_schema
        or value.get("attestation_ref") != ref
        or not isinstance(value.get("project_id"), str)
        or not value["project_id"]
        or not is_sha256(value.get("review_intent_sha256"))
        or not isinstance(value.get("nonce"), str)
        or not value["nonce"]
        or not isinstance(value.get("generation"), int)
        or isinstance(value["generation"], bool)
        or value["generation"] < 1
        or not isinstance(value.get("issued_at"), str)
        or not value["issued_at"]
    ):
        raise AuthorityError("protected attestation is malformed")
    return value


def max_consumed_generation(chain: Chain, schema: str) -> int:
    greatest = 0
    for record in chain.records:
        authority = record.get("authority")
        if (
            isinstance(authority, dict)
            and _semantic_attestation_schema(authority.get("schema"))
            == _semantic_attestation_schema(schema)
        ):
            greatest = max(greatest, authority["generation"])
    return greatest


def consume(chain: Chain, schema: str, ref: str, project_id: str, intent_sha256: str) -> dict:
    """Atomically consume one external review attestation, once, forever."""
    semantic_schema = _semantic_attestation_schema(schema)
    if semantic_schema is None:
        raise AuthorityError("unknown attestation schema")
    if not is_sha256(intent_sha256):
        raise AuthorityError("review intent digest is malformed")
    fd = _open_attestation(ref, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        value = _read_attestation(fd, schema, ref)
        if value["project_id"] != project_id:
            raise AuthorityError("attestation does not bind this project")
        if value["review_intent_sha256"] != intent_sha256:
            raise AuthorityError("attestation does not bind this review intent")
        signed_consumed_at = None
        if value["schema"] in (
            VISION_ATTESTATION_SCHEMA_V2,
            HUMAN_ATTESTATION_SCHEMA_V2,
        ):
            expected_path_sha256 = (
                chain.latest.get("project_path_sha256") if chain.latest else None
            )
            if value["project_root_sha256"] != expected_path_sha256:
                raise AuthorityError("attestation does not bind this project root")
            if not chain.latest or value["self_eval_result"] not in chain.latest[
                "project_refs"
            ]:
                raise AuthorityError(
                    "attestation does not bind the current self-eval result"
                )
            current = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
            issued_at = _parse_utc_timestamp(value["issued_at"], "issued_at")
            expires_at = _parse_utc_timestamp(value["expires_at"], "expires_at")
            if current < issued_at or current >= expires_at:
                raise AuthorityError("signed attestation is outside its validity interval")
            signed_consumed_at = current.isoformat()
        if (
            value["consumed_at"] is not None
            or value["consumed_project_id"] is not None
            or value["consumed_intent_sha256"] is not None
        ):
            raise AuthorityError("attestation has already been consumed")
        if value["generation"] <= max_consumed_generation(chain, semantic_schema):
            raise AuthorityError("attestation generation is not monotonic")
        consumed_at = signed_consumed_at or now_iso()
        value.update(
            {
                "consumed_at": consumed_at,
                "consumed_project_id": project_id,
                "consumed_intent_sha256": intent_sha256,
            }
        )
        encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        _write_all(fd, encoded)
        os.fsync(fd)
        return {
            "schema": value["schema"],
            "attestation_ref": ref,
            "review_intent_sha256": intent_sha256,
            "nonce": value["nonce"],
            "generation": value["generation"],
            "consumed_at": consumed_at,
        }
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def verify_consumed(authority, project_id: str, project=None) -> bool:
    """Re-open the leaf: a stored authority object is not self-proving."""
    if not _valid_authority(authority):
        return False
    try:
        fd = _open_attestation(authority["attestation_ref"], os.O_RDONLY)
    except AuthorityError:
        return False
    try:
        value = _read_attestation(fd, authority["schema"], authority["attestation_ref"])
    except AuthorityError:
        return False
    finally:
        os.close(fd)
    return bool(
        value["schema"] == authority["schema"]
        and value["project_id"] == project_id
        and value["review_intent_sha256"] == authority["review_intent_sha256"]
        and value["nonce"] == authority["nonce"]
        and value["generation"] == authority["generation"]
        and value["consumed_at"] == authority["consumed_at"]
        and value["consumed_project_id"] == project_id
        and value["consumed_intent_sha256"] == authority["review_intent_sha256"]
        and (
            value["schema"]
            not in (VISION_ATTESTATION_SCHEMA_V2, HUMAN_ATTESTATION_SCHEMA_V2)
            or (
                project is not None
                and value["project_root_sha256"] == project_path_sha256(project)
            )
        )
    )


# --------------------------------------------------------------------------
# transactions
# --------------------------------------------------------------------------


def _pending_names(namespace: Namespace) -> list:
    return sorted(
        name
        for name in os.listdir(namespace.pending_fd)
        if name.endswith(".json") and not name.startswith(".")
    )


def _valid_pending(value, namespace: Namespace) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) != PENDING_KEYS
        or value.get("schema") != TRANSACTION_SCHEMA
        or value.get("project_id") != namespace.project_id
        or value.get("project_path_sha256") != namespace.path_sha256
        or value.get("transition") not in TRANSITIONS
        or not isinstance(value.get("generation"), int)
        or isinstance(value["generation"], bool)
        or value["generation"] < 1
        or not isinstance(value.get("attempt"), int)
        or isinstance(value["attempt"], bool)
        or value["attempt"] < 1
        or not is_sha256(value.get("attempt_identity"))
        or not isinstance(value.get("expected_project_refs"), list)
        or not value["expected_project_refs"]
        or not all(is_ref(ref) for ref in value["expected_project_refs"])
        or value["expected_project_refs"] != sorted_refs(value["expected_project_refs"])
        or not isinstance(value.get("prepared_at"), str)
    ):
        return False
    predecessor = value["predecessor"]
    if value["generation"] == 1:
        if predecessor is not None or value["retirement"] is None:
            return False
    else:
        if (
            not isinstance(predecessor, dict)
            or set(predecessor) != {"generation", "sha256"}
            or predecessor["generation"] != value["generation"] - 1
            or not is_sha256(predecessor.get("sha256"))
            or value["retirement"] is not None
        ):
            return False
    if not is_sha256(value.get("transaction_id")):
        return False
    return value["transaction_id"] == transaction_id(value)


def transaction_id(pending: dict) -> str:
    preimage = {
        key: pending[key]
        for key in PENDING_KEYS
        if key not in ("transaction_id", "prepared_at")
    }
    return canonical_digest(preimage)


def prepare(
    namespace: Namespace,
    chain: Chain,
    *,
    transition: str,
    attempt: int,
    attempt_identity: str,
    expected_refs: list,
    retirement=None,
) -> dict:
    """Persist the externally authoritative intent before any project mutation."""
    if transition not in TRANSITIONS:
        raise AuthorityError("unknown transition")
    generation = chain.generation + 1
    if generation == 1 and retirement is None:
        raise AuthorityError("generation 1 must carry the retirement plan")
    if generation != 1 and retirement is not None:
        raise AuthorityError("retirement is only prepared at generation 1")
    pending = {
        "schema": TRANSACTION_SCHEMA,
        "project_id": namespace.project_id,
        "project_path_sha256": namespace.path_sha256,
        "transaction_id": "",
        "transition": transition,
        "generation": generation,
        "predecessor": chain.predecessor(),
        "attempt": attempt,
        "attempt_identity": attempt_identity,
        "expected_project_refs": sorted_refs(expected_refs),
        "retirement": retirement,
        "prepared_at": now_iso(),
    }
    pending["transaction_id"] = transaction_id(pending)
    if not _valid_pending(pending, namespace):
        raise AuthorityError("prepared transaction is malformed")
    name = f"{pending['transaction_id']}.json"
    payload = canonical_bytes(pending)
    try:
        write_leaf_at(namespace.pending_fd, name, payload, exclusive=True)
    except FileExistsError:
        fd = open_protected_file_at(namespace.pending_fd, name)
        try:
            if read_fd(fd) != payload:
                raise AuthorityError("pending transaction bytes differ")
        finally:
            os.close(fd)
    return pending


def _record(namespace: Namespace, pending: dict, authority) -> dict:
    return {
        "schema": GENERATION_SCHEMA,
        "project_id": namespace.project_id,
        "project_path_sha256": namespace.path_sha256,
        "generation": pending["generation"],
        "transition": pending["transition"],
        "transaction_id": pending["transaction_id"],
        "predecessor": pending["predecessor"],
        "attempt": pending["attempt"],
        "attempt_identity": pending["attempt_identity"],
        "project_refs": pending["expected_project_refs"],
        "authority": authority,
        "recorded_at": now_iso(),
    }


def commit(
    namespace: Namespace,
    chain: Chain,
    pending: dict,
    *,
    authority=None,
    retire=None,
    promote=None,
) -> dict:
    """Steps 3-8 of the commit protocol; steps 1-2 are the caller's."""
    if pending["generation"] != chain.generation + 1:
        raise AuthorityError("transaction generation is stale")
    if pending["transition"] in EVALUATE_TRANSITIONS:
        if authority is not None:
            raise AuthorityError("evaluate transitions carry no authority")
    elif not _valid_authority(authority):
        raise AuthorityError("review transitions require consumed authority")
    if pending["retirement"] is not None and retire is not None:
        retire(pending["retirement"])
    if promote is not None:
        promote()
    if verify_refs(namespace.project, pending["expected_project_refs"]) != "match":
        raise AuthorityError("promoted project refs do not match the transaction")
    record = _record(namespace, pending, authority)
    payload = canonical_bytes(record)
    name = f"{record['generation']:08d}.json"
    try:
        write_leaf_at(namespace.generations_fd, name, payload, exclusive=True)
    except FileExistsError as error:
        fd = open_protected_file_at(namespace.generations_fd, name)
        try:
            if read_fd(fd) != payload:
                raise AuthorityError("generation record already exists") from error
        finally:
            os.close(fd)
    atomic_leaf_at(
        namespace.fd,
        "current.json",
        canonical_bytes(
            {
                "schema": POINTER_SCHEMA,
                "project_id": namespace.project_id,
                "project_path_sha256": namespace.path_sha256,
                "generation": record["generation"],
                "record_sha256": hashlib.sha256(payload).hexdigest(),
            }
        ),
    )
    _discard_pending(namespace, pending["transaction_id"])
    return record


def _discard_pending(namespace: Namespace, identifier: str) -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        os.unlink(f"{identifier}.json", dir_fd=namespace.pending_fd)
    fsync_dir(namespace.pending_fd)


def recover(
    namespace: Namespace,
    chain: Chain,
    *,
    retire=None,
    quarantine=None,
    recovered_authority=None,
) -> Chain:
    """Finish or discard every pending transaction. Never self-mint authority."""
    committed = {record["transaction_id"] for record in chain.records}
    for name in _pending_names(namespace):
        fd = open_protected_file_at(namespace.pending_fd, name)
        try:
            payload = read_fd(fd)
        finally:
            os.close(fd)
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = None
        if not isinstance(value, dict) or not _valid_pending(value, namespace):
            _discard_pending(namespace, name[: -len(".json")])
            continue
        if value["transaction_id"] in committed or value["generation"] <= chain.generation:
            _discard_pending(namespace, value["transaction_id"])
            continue
        if canonical_bytes(value) != payload:
            _discard_pending(namespace, value["transaction_id"])
            continue
        state = verify_refs(namespace.project, value["expected_project_refs"])
        if state == "absent":
            _discard_pending(namespace, value["transaction_id"])
            continue
        if state == "mismatch":
            if quarantine is not None:
                quarantine(value["transaction_id"], value["expected_project_refs"])
            _discard_pending(namespace, value["transaction_id"])
            continue
        recovered = None
        if value["transition"] not in EVALUATE_TRANSITIONS:
            if recovered_authority is not None:
                recovered = recovered_authority(value)
            if not _valid_authority(recovered):
                # The protected leaf is consumed, but no trustworthy authority
                # can be reconstructed from the exact promoted receipt. Never
                # guess: quarantine the bytes and require a fresh attestation.
                if quarantine is not None:
                    quarantine(
                        value["transaction_id"], value["expected_project_refs"]
                    )
                _discard_pending(namespace, value["transaction_id"])
                continue
        if value["generation"] != chain.generation + 1:
            _discard_pending(namespace, value["transaction_id"])
            continue
        commit(
            namespace,
            chain,
            value,
            authority=recovered,
            retire=(
                (lambda plan: retire(plan, value["transaction_id"]))
                if retire is not None
                else None
            ),
            promote=None,
        )
        chain = validated_chain(namespace)
        committed = {record["transaction_id"] for record in chain.records}
    return chain
