#!/usr/bin/env python3
"""Prepare operator-only self-eval attestation signing requests.

This module never mints an attestation and never records a review. It validates
the current protected self-eval state, binds an exact review intent, and emits a
short-lived request for the native user-presence signer.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import sys
import uuid
from pathlib import Path

TOOLS_ROOT = Path(__file__).resolve().parent
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

import render_self_eval as engine
import self_eval_authority as authority

REQUEST_SCHEMA = "haru.self_eval_operator_signing_request.v1"
VISION_CONFIGURATION_CAPABILITY = "vision_provider_configuration.v1"
VISION_CONFIGURATION_PROVIDER = "operator-declared"
VISION_CONFIGURATION_MODEL = "not-configured"
REQUEST_LIFETIME_SECONDS = 300


class IssuerError(ValueError):
    """A signing request cannot safely be prepared."""


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise IssuerError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _read_findings(path):
    if path is None:
        return []
    try:
        payload = Path(path).read_bytes()
    except OSError as error:
        raise IssuerError("findings file is unavailable") from error
    if len(payload) > 1_048_576:
        raise IssuerError("findings file exceeds 1 MiB")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError, IssuerError) as error:
        raise IssuerError("findings file is malformed") from error
    if not isinstance(value, list):
        raise IssuerError("findings file must contain a JSON array")
    return value


def _project(project) -> Path:
    try:
        return engine._project_dir(project)
    except (OSError, ValueError) as error:
        raise IssuerError(str(error)) from error


def unavailable_review(*, reviewed_by: str, reason: str) -> dict:
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise IssuerError("reviewed_by is required")
    if not isinstance(reason, str) or len(reason.strip()) < 12:
        raise IssuerError("unavailability reason must contain at least 12 characters")
    return {
        "reviewer_kind": "vision",
        "verdict": "unavailable",
        "reviewed_by": reviewed_by.strip(),
        "provider": VISION_CONFIGURATION_PROVIDER,
        "model": VISION_CONFIGURATION_MODEL,
        "capability": VISION_CONFIGURATION_CAPABILITY,
        "notes": reason.strip(),
        "findings": [],
    }


def human_review(
    *, reviewed_by: str, verdict: str, notes: str = "", findings=None
) -> dict:
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise IssuerError("reviewed_by is required")
    value = {
        "reviewer_kind": "human_fallback",
        "verdict": verdict,
        "reviewed_by": reviewed_by.strip(),
        "provider": "human-operator",
        "model": "none",
        "capability": engine.HUMAN_CAPABILITY,
        "notes": notes.strip() if isinstance(notes, str) else notes,
        "findings": [] if findings is None else findings,
    }
    try:
        return engine._validate_review_input(value)
    except ValueError as error:
        raise IssuerError(str(error)) from error


def _current_unavailable_ref(project: Path, result: dict) -> dict:
    relative = f"{engine.attempt_dir(result['attempt'])}/vision-unavailable.json"
    try:
        reference = engine._project_ref(project, relative)
        unavailable = engine._load_ref(project, reference)
    except (OSError, ValueError) as error:
        raise IssuerError(
            "human fallback requires the current valid vision-unavailable receipt"
        ) from error
    if not engine._valid_unavailable(project, unavailable, result):
        raise IssuerError(
            "human fallback requires the current valid vision-unavailable receipt"
        )
    return reference


def _generation(project: Path, schema: str) -> int:
    try:
        with authority.locked(project, project.name) as namespace:
            chain = authority.validated_chain(namespace)
            return authority.max_consumed_generation(chain, schema) + 1
    except ValueError as error:
        raise IssuerError(str(error)) from error


def prepare_request(project, review_input, *, now=None) -> dict:
    directory = _project(project)
    try:
        result = engine.validate_current(directory)
        review_input = engine._validate_review_input(review_input)
    except ValueError as error:
        raise IssuerError(str(error)) from error
    if result["status"] != engine.STATUS_NEEDS_HUMAN:
        raise IssuerError("only a current clean evaluation pending review can be signed")

    if review_input["reviewer_kind"] == "vision":
        if (
            review_input["verdict"] != "unavailable"
            or review_input["provider"] != VISION_CONFIGURATION_PROVIDER
            or review_input["model"] != VISION_CONFIGURATION_MODEL
            or review_input["capability"] != VISION_CONFIGURATION_CAPABILITY
            or len(review_input["notes"].strip()) < 12
        ):
            raise IssuerError(
                "operator vision requests may only declare an explicitly explained "
                "missing provider configuration"
            )
        try:
            engine._project_ref(
                directory,
                f"{engine.attempt_dir(result['attempt'])}/vision-unavailable.json",
            )
        except ValueError:
            pass
        else:
            raise IssuerError(
                "the current attempt already records vision unavailability; "
                "prepare a human fallback review"
            )
        action = authority.VISION_UNAVAILABLE_ACTION
        signed_schema = authority.VISION_ATTESTATION_SCHEMA_V2
        semantic_schema = authority.VISION_ATTESTATION_SCHEMA
        intent = engine._vision_intent(directory, result, review_input)
    elif review_input["reviewer_kind"] == "human_fallback":
        unavailable_ref = _current_unavailable_ref(directory, result)
        action = authority.HUMAN_REVIEW_ACTION
        signed_schema = authority.HUMAN_ATTESTATION_SCHEMA_V2
        semantic_schema = authority.HUMAN_ATTESTATION_SCHEMA
        intent = engine._human_intent(
            directory, result, review_input, unavailable_ref
        )
    else:
        raise IssuerError("operator signer supports only unavailability and human fallback")

    current_result = engine._project_ref(directory, engine.RESULT_PATH)
    intent_sha256 = authority.canonical_digest(intent)
    issued = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    issued = issued.replace(microsecond=0)
    expires = issued + dt.timedelta(seconds=REQUEST_LIFETIME_SECONDS)
    try:
        pin = authority._validate_signer_pin(authority.load_signer_pin())[0]
    except ValueError as error:
        raise IssuerError(str(error)) from error
    reference = f"self-eval-attestation:{uuid.uuid4()}"
    immutable = {
        "schema": signed_schema,
        "attestation_ref": reference,
        "action": action,
        "project_id": directory.name,
        "project_root_sha256": authority.project_path_sha256(directory),
        "review_intent_sha256": intent_sha256,
        "self_eval_result": current_result,
        "nonce": secrets.token_hex(32),
        "generation": _generation(directory, semantic_schema),
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "key_id": pin["key_id"],
        "signature_algorithm": authority.SIGNATURE_ALGORITHM,
    }
    return {
        "schema": REQUEST_SCHEMA,
        "project_root": str(directory),
        "action": action,
        "review_input": review_input,
        "review_intent": intent,
        "review_intent_sha256": intent_sha256,
        "attestation": immutable,
    }


def write_request(path, request) -> None:
    output = Path(path)
    if not output.is_absolute():
        raise IssuerError("output path must be absolute")
    payload = authority.canonical_bytes(request) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(output, flags, 0o600)
    except OSError as error:
        raise IssuerError("output file must be a new direct file") from error
    keep = False
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
        keep = True
    finally:
        os.close(fd)
        if not keep:
            try:
                output.unlink()
            except OSError:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    unavailable = subparsers.add_parser(
        "prepare-unavailable",
        help="prepare an operator declaration that no vision provider is configured",
    )
    unavailable.add_argument("project")
    unavailable.add_argument("--reviewed-by", required=True)
    unavailable.add_argument("--reason", required=True)
    unavailable.add_argument("--output", required=True)

    human = subparsers.add_parser(
        "prepare-human-review",
        help="prepare a human fallback verdict after valid vision unavailability",
    )
    human.add_argument("project")
    human.add_argument("--reviewed-by", required=True)
    human.add_argument("--verdict", choices=("pass", "fail"), required=True)
    human.add_argument("--notes", default="")
    human.add_argument("--findings-json")
    human.add_argument("--output", required=True)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare-unavailable":
            review = unavailable_review(
                reviewed_by=args.reviewed_by, reason=args.reason
            )
        else:
            review = human_review(
                reviewed_by=args.reviewed_by,
                verdict=args.verdict,
                notes=args.notes,
                findings=_read_findings(args.findings_json),
            )
        request = prepare_request(args.project, review)
        write_request(args.output, request)
    except (IssuerError, ValueError) as error:
        print(f"self-eval-issuer: {error}", file=sys.stderr)
        return 1
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
