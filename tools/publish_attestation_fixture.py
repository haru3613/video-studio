"""Test-only P-256 publish-attestation issuer.

Every software key is generated into ephemeral test state. It has no runtime
authority: production trusts only the Secure Enclave public key pinned in the
source-fingerprinted runtime manifest.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import approval_attestation


def _run(arguments, *, input_bytes=None):
    result = subprocess.run(
        arguments,
        input=input_bytes,
        stdin=subprocess.DEVNULL if input_bytes is None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5,
        env={
            "HOME": tempfile.gettempdir(),
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        },
    )
    if result.returncode != 0:
        raise RuntimeError("ephemeral test signer generation failed")
    return result.stdout


def generate_signer():
    """Create an unpersisted software P-256 key for one test fixture."""
    private_key = _run(
        [approval_attestation.OPENSSL, "ecparam", "-name", "prime256v1", "-genkey", "-noout"]
    )
    public_der = _run(
        [
            approval_attestation.OPENSSL,
            "ec",
            "-pubout",
            "-conv_form",
            "uncompressed",
            "-outform",
            "DER",
        ],
        input_bytes=private_key,
    )
    public_key = public_der[-65:]
    if len(public_key) != 65 or public_key[0] != 0x04:
        raise RuntimeError("ephemeral test signer public key is malformed")
    return {
        "private_key": private_key,
        "pin": {
            "algorithm": approval_attestation.SIGNATURE_ALGORITHM,
            "key_id": hashlib.sha256(public_key).hexdigest(),
            "public_key_x963_base64": base64.b64encode(public_key).decode("ascii"),
        },
    }


def attestation_ref(label="fixture"):
    del label
    return f"attestation:{uuid.uuid4()}"


def _write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
    finally:
        os.close(fd)


def _sign(statement, private_key):
    with tempfile.TemporaryDirectory(prefix="hvp-test-sign-") as directory:
        root = Path(directory)
        key_path = root / "test-private.pem"
        statement_path = root / "statement.json"
        signature_path = root / "signature.der"
        _write_private(key_path, private_key)
        _write_private(statement_path, statement)
        result = subprocess.run(
            [
                approval_attestation.OPENSSL,
                "dgst",
                "-sha256",
                "-sign",
                str(key_path),
                "-out",
                str(signature_path),
                str(statement_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
            env={
                "HOME": str(root),
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
        )
        if result.returncode != 0:
            raise RuntimeError("test publish attestation signing failed")
        return signature_path.read_bytes()


def signed_leaf(
    ref,
    project_id,
    project_root,
    approval_intent_sha256,
    nonce,
    generation,
    *,
    signer,
    issued_at=None,
    expires_at=None,
    immutable_overrides=None,
    private_key=None,
    consumed_at=None,
    consumed_project_id=None,
    consumed_intent_sha256=None,
):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    issued_at = issued_at or now.isoformat()
    expires_at = expires_at or (now + dt.timedelta(minutes=5)).isoformat()
    immutable = {
        "schema": approval_attestation.SCHEMA,
        "attestation_ref": ref,
        "project_id": project_id,
        "project_root_sha256": hashlib.sha256(
            str(Path(project_root).resolve()).encode("utf-8")
        ).hexdigest(),
        "approval_intent_sha256": approval_intent_sha256,
        "nonce": nonce,
        "generation": generation,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "channel_id": approval_attestation.canonical_channel_id(),
        "visibility": approval_attestation.PUBLISH_VISIBILITY,
        "key_id": signer["pin"]["key_id"],
        "signature_algorithm": approval_attestation.SIGNATURE_ALGORITHM,
    }
    if immutable_overrides:
        immutable.update(immutable_overrides)
    signature = _sign(
        approval_attestation.canonical_statement(immutable),
        private_key or signer["private_key"],
    )
    return {
        **immutable,
        "signature_base64": base64.b64encode(signature).decode("ascii"),
        "consumed_at": consumed_at,
        "consumed_project_id": consumed_project_id,
        "consumed_intent_sha256": consumed_intent_sha256,
    }


def write_leaf(root, value):
    ref = value["attestation_ref"]
    path = Path(root) / f"{ref.split(':', 1)[1]}.json"
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    return path
