"""Protected one-time human publish attestation state.

Attestations are deliberately external to a project tree: project writers can
prepare an intent, but cannot mint or replay a human approval. An attestation
issuer writes a mode-0600 JSON leaf under the mode-0700 state root; this module
only verifies and atomically consumes that leaf.
"""
from __future__ import annotations

import datetime as dt
import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

SCHEMA = "haru.publish_attestation.v2"
STATEMENT_SCHEMA = "haru.publish_authorization_statement.v1"
SIGNATURE_ALGORITHM = "ecdsa-p256-sha256"
PUBLISH_ACTION = "youtube.upload.unlisted"
PUBLISH_VISIBILITY = "unlisted"
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
RECONCILE_SCHEMA = "haru.upload_reconcile_attestation.v1"
RECONCILE_ACTION = "authorize_restart"

REF = re.compile(r"^attestation:([A-Za-z0-9][A-Za-z0-9._-]{0,127})$")
PUBLISH_REF = re.compile(
    r"^attestation:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
LOWER_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
ROOT_ENV = "HARU_VIDEO_STUDIO_ATTESTATION_ROOT"
OPENSSL = "/usr/bin/openssl"
RUNTIME_MANIFEST = Path(__file__).resolve().parents[1] / "pipeline/runtime-manifest.json"

IMMUTABLE_KEYS = frozenset(
    {
        "schema",
        "attestation_ref",
        "project_id",
        "project_root_sha256",
        "approval_intent_sha256",
        "nonce",
        "generation",
        "issued_at",
        "expires_at",
        "channel_id",
        "visibility",
        "key_id",
        "signature_algorithm",
    }
)
CONSUMED_KEYS = frozenset(
    {"signature_base64", "consumed_at", "consumed_project_id", "consumed_intent_sha256"}
)
LEAF_KEYS = IMMUTABLE_KEYS | CONSUMED_KEYS
PIN_KEYS = frozenset({"algorithm", "key_id", "public_key_x963_base64"})


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _runtime_manifest():
    try:
        value = json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("runtime signer policy is unavailable") from error
    if not isinstance(value, dict) or value.get("schema") != "haru.runtime_manifest.v1":
        raise ValueError("runtime signer policy is malformed")
    return value


def _decode_base64(value, description):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{description} is malformed")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as error:
        raise ValueError(f"{description} is malformed") from error


def _validate_signer_pin(value):
    if value is None:
        raise ValueError("issuer_not_enrolled")
    if not isinstance(value, dict) or set(value) != PIN_KEYS:
        raise ValueError("publish approval signer pin is malformed")
    if value.get("algorithm") != SIGNATURE_ALGORITHM:
        raise ValueError("publish approval signer pin is malformed")
    key_id = value.get("key_id")
    if not isinstance(key_id, str) or LOWER_HEX_64.fullmatch(key_id) is None:
        raise ValueError("publish approval signer pin is malformed")
    public_key = _decode_base64(
        value.get("public_key_x963_base64"), "publish approval signer pin"
    )
    if len(public_key) != 65 or public_key[0] != 0x04:
        raise ValueError("publish approval signer pin is malformed")
    if hashlib.sha256(public_key).hexdigest() != key_id:
        raise ValueError("publish approval signer pin is malformed")
    return dict(value)


def load_signer_pin():
    """Load the source-fingerprinted publish signer pin.

    There is deliberately no environment or project-local override: enrollment
    becomes authority only after the exported public key is committed into and
    promoted with the runtime manifest.
    """
    manifest = _runtime_manifest()
    if manifest.get("publish_approval_signer") is None:
        raise ValueError("issuer_not_enrolled")
    return _validate_signer_pin(manifest["publish_approval_signer"])


def canonical_channel_id():
    channel = _runtime_manifest().get("youtube_channel_id")
    if not isinstance(channel, str) or CHANNEL_ID.fullmatch(channel) is None:
        raise ValueError("runtime publish target is not configured")
    return channel


def _publish_policy():
    manifest = _runtime_manifest()
    channel_id = manifest.get("youtube_channel_id")
    if not isinstance(channel_id, str) or CHANNEL_ID.fullmatch(channel_id) is None:
        raise ValueError("runtime publish target is malformed")
    # Keep this call patchable by test fixtures without creating a production
    # environment bypass. Production always resolves it from the fixed manifest.
    return _validate_signer_pin(load_signer_pin()), channel_id


def state_root():
    configured = os.environ.get(ROOT_ENV)
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local/state/video-studio/publish-attestations"
    )


def _path_for(ref):
    match = REF.fullmatch(ref) if isinstance(ref, str) else None
    if not match:
        raise ValueError("attestation_ref is malformed")
    root = state_root()
    if not root.is_absolute():
        raise ValueError("protected attestation root must be absolute")
    try:
        root_stat = root.lstat()
    except OSError as error:
        raise ValueError("protected attestation store is unavailable") from error
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid != os.getuid()
        or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise ValueError("protected attestation store permissions are invalid")
    path = root / f"{match.group(1)}.json"
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("attestation_ref escapes protected state") from error
    return path


def _open(ref, flags):
    path = _path_for(ref)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags | nofollow)
    except OSError as error:
        raise ValueError("protected attestation is unavailable") from error
    file_stat = os.fstat(fd)
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != os.getuid()
        or stat.S_IMODE(file_stat.st_mode) != 0o600
    ):
        os.close(fd)
        raise ValueError("protected attestation permissions are invalid")
    return fd


def _open_publish(ref, flags):
    fd = _open(ref, flags)
    if os.fstat(fd).st_nlink != 1:
        os.close(fd)
        raise ValueError("protected publish attestation links are invalid")
    return fd


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("protected attestation contains duplicate keys")
        value[key] = item
    return value


def _read(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        block = os.read(fd, 65536)
        if not block:
            break
        chunks.append(block)
        if sum(map(len, chunks)) > 65536:
            raise ValueError("protected attestation is too large")
    try:
        value = json.loads(
            b"".join(chunks).decode("utf-8"), object_pairs_hook=_strict_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("protected attestation is malformed") from error
    if not isinstance(value, dict):
        raise ValueError("protected attestation is malformed")
    return value


def _parse_utc_timestamp(value, field):
    if not isinstance(value, str) or UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError(f"publish attestation {field} is malformed")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"publish attestation {field} is malformed") from error
    if parsed.tzinfo != dt.timezone.utc or parsed.microsecond:
        raise ValueError(f"publish attestation {field} is malformed")
    return parsed


def _immutable_attestation(value, ref):
    if not isinstance(value, dict) or set(value) != LEAF_KEYS:
        raise ValueError("protected publish attestation is malformed")
    if (
        value.get("schema") != SCHEMA
        or not isinstance(value.get("attestation_ref"), str)
        or PUBLISH_REF.fullmatch(value["attestation_ref"]) is None
        or value["attestation_ref"] != ref
        or not isinstance(value.get("project_id"), str)
        or not value["project_id"]
        or not isinstance(value.get("project_root_sha256"), str)
        or LOWER_HEX_64.fullmatch(value["project_root_sha256"]) is None
        or not isinstance(value.get("approval_intent_sha256"), str)
        or LOWER_HEX_64.fullmatch(value["approval_intent_sha256"]) is None
        or not isinstance(value.get("nonce"), str)
        or LOWER_HEX_64.fullmatch(value["nonce"]) is None
        or not isinstance(value.get("generation"), int)
        or isinstance(value.get("generation"), bool)
        or value["generation"] < 1
        or value.get("channel_id") != canonical_channel_id()
        or value.get("visibility") != PUBLISH_VISIBILITY
        or not isinstance(value.get("key_id"), str)
        or LOWER_HEX_64.fullmatch(value["key_id"]) is None
        or value.get("signature_algorithm") != SIGNATURE_ALGORITHM
    ):
        raise ValueError("protected publish attestation is malformed")
    issued_at = _parse_utc_timestamp(value.get("issued_at"), "issued_at")
    expires_at = _parse_utc_timestamp(value.get("expires_at"), "expires_at")
    if expires_at <= issued_at or expires_at - issued_at > dt.timedelta(seconds=300):
        raise ValueError("protected publish attestation lifetime is malformed")
    immutable = {key: value[key] for key in IMMUTABLE_KEYS}
    return immutable, issued_at, expires_at


def canonical_statement(immutable):
    """Return the exact bytes signed by the native approval issuer."""
    statement = {
        "schema": STATEMENT_SCHEMA,
        "action": PUBLISH_ACTION,
        "attestation": immutable,
    }
    return json.dumps(
        statement,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _spki_der(public_key):
    # RFC 5480 SubjectPublicKeyInfo for id-ecPublicKey + prime256v1, followed by
    # the uncompressed 65-byte X9.63 point pinned in the runtime manifest.
    return bytes.fromhex(
        "3059"
        "3013"
        "06072a8648ce3d0201"
        "06082a8648ce3d030107"
        "034200"
    ) + public_key


def _write_temp(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify_signature(value, ref):
    immutable, issued_at, expires_at = _immutable_attestation(value, ref)
    pin, channel_id = _publish_policy()
    if immutable["key_id"] != pin["key_id"]:
        raise ValueError("issuer_not_enrolled")
    if immutable["channel_id"] != channel_id:
        raise ValueError("publish attestation target is invalid")
    public_key = _decode_base64(
        pin["public_key_x963_base64"], "publish approval signer pin"
    )
    signature = _decode_base64(
        value.get("signature_base64"), "publish attestation signature"
    )
    if not 64 <= len(signature) <= 80:
        raise ValueError("publish attestation signature is malformed")
    with tempfile.TemporaryDirectory(prefix="hvp-publish-verify-") as directory:
        temporary = Path(directory)
        public_path = temporary / "public.der"
        signature_path = temporary / "signature.der"
        statement_path = temporary / "statement.json"
        _write_temp(public_path, _spki_der(public_key))
        _write_temp(signature_path, signature)
        _write_temp(statement_path, canonical_statement(immutable))
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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5,
                env={
                    "HOME": str(temporary),
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin",
                },
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError("publish attestation signature verification unavailable") from error
    if result.returncode != 0:
        raise ValueError("publish attestation signature is invalid")
    return immutable, issued_at, expires_at


def _validate_unconsumed(value, issued_at, expires_at):
    if any(
        value.get(field) is not None
        for field in ("consumed_at", "consumed_project_id", "consumed_intent_sha256")
    ):
        raise ValueError("attestation has already been consumed")
    now = dt.datetime.now(dt.timezone.utc)
    if now < issued_at or now >= expires_at:
        raise ValueError("publish attestation is expired or not yet valid")


def _project_root_sha256(project_root):
    try:
        resolved = Path(project_root).resolve()
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("publish attestation project root is malformed") from error
    if not resolved.is_absolute():
        raise ValueError("publish attestation project root is malformed")
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()


def _matches(
    value,
    ref,
    project_id,
    project_root_sha256,
    intent_sha256,
    nonce,
    generation,
):
    return (
        value.get("schema") == SCHEMA
        and value.get("attestation_ref") == ref
        and value.get("project_id") == project_id
        and value.get("project_root_sha256") == project_root_sha256
        and value.get("approval_intent_sha256") == intent_sha256
        and value.get("nonce") == nonce
        and value.get("generation") == generation
    )


def is_consumed(
    ref, project_id, intent_sha256, nonce, generation, *, project_root
):
    """Return whether this exact human decision was consumed once for this intent."""
    fd = _open_publish(ref, os.O_RDONLY)
    try:
        value = _read(fd)
    finally:
        os.close(fd)
    _immutable, issued_at, expires_at = _verify_signature(value, ref)
    project_root_sha256 = _project_root_sha256(project_root)
    try:
        consumed_at = _parse_utc_timestamp(value.get("consumed_at"), "consumed_at")
    except ValueError:
        return False
    return bool(
        _matches(
            value,
            ref,
            project_id,
            project_root_sha256,
            intent_sha256,
            nonce,
            generation,
        )
        and issued_at <= consumed_at < expires_at
        and consumed_at <= dt.datetime.now(dt.timezone.utc)
        and value.get("consumed_project_id") == project_id
        and value.get("consumed_intent_sha256") == intent_sha256
    )


def consume(ref, project_id, intent_sha256, nonce, generation, *, project_root):
    """Atomically consume an external attestation for one exact approval intent."""
    fd = _open_publish(ref, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        value = _read(fd)
        _immutable, issued_at, expires_at = _verify_signature(value, ref)
        project_root_sha256 = _project_root_sha256(project_root)
        if not _matches(
            value,
            ref,
            project_id,
            project_root_sha256,
            intent_sha256,
            nonce,
            generation,
        ):
            raise ValueError("attestation does not bind this project approval intent")
        _validate_unconsumed(value, issued_at, expires_at)
        consumed_at = now_iso()
        if _parse_utc_timestamp(consumed_at, "consumed_at") >= expires_at:
            raise ValueError("publish attestation is expired or not yet valid")
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
        os.write(fd, encoded)
        os.fsync(fd)
        return value
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def consume_reconcile(ref, project_id, publish_intent_id, attempt_generation):
    """Atomically consume an attempt-bound restart attestation.

    A publish-approval leaf cannot satisfy this: the schema, bound intent id,
    and action are distinct, so a leftover approval cannot authorize a restart.
    """
    if (
        not isinstance(project_id, str)
        or not project_id
        or not isinstance(publish_intent_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", publish_intent_id) is None
        or not isinstance(attempt_generation, int)
        or isinstance(attempt_generation, bool)
        or attempt_generation < 1
    ):
        raise ValueError("reconcile attestation binding is malformed")
    fd = _open(ref, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        value = _read(fd)
        if (
            value.get("schema") != RECONCILE_SCHEMA
            or value.get("attestation_ref") != ref
            or value.get("project_id") != project_id
            or value.get("publish_intent_id") != publish_intent_id
            or value.get("attempt_generation") != attempt_generation
            or value.get("action") != RECONCILE_ACTION
        ):
            raise ValueError("attestation does not bind this upload attempt")
        if value.get("consumed_at") is not None:
            raise ValueError("attestation has already been consumed")
        value.update(
            {
                "consumed_at": now_iso(),
                "consumed_project_id": project_id,
                "consumed_publish_intent_id": publish_intent_id,
                "consumed_attempt_generation": attempt_generation,
            }
        )
        encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, encoded)
        os.fsync(fd)
        return value
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
