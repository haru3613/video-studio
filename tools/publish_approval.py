#!/usr/bin/env python3
"""Digest-bound, one-time human approval for the canonical YouTube target.

Approval has no caller-supplied identity or destination. The project contract
names the target, the promoted runtime confirms it, and a separately protected
one-time human attestation authorizes one exact full intent. This tool never
opens a network session or starts an upload.

Since HVP-33 the intent is v3: it additionally binds the current render
self-evaluation pass and the current HVP-21 v2 visual review, each by exact
`{path,sha256,bytes}`. That is what stops an approval from being resurrected —
replacing the visual review over unchanged final bytes changes the intent
digest, so the old approval reads stale until a fresh higher-generation
attestation approves the new evidence.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_status  # noqa: E402
import approval_attestation  # noqa: E402

SCHEMA = agent_status.SCHEMA_PUBLISH_APPROVAL
SIGNING_REQUEST_SCHEMA = "haru.publish_approval_signing_request.v1"
ATTESTATION_SCHEMA = "haru.publish_attestation.v2"
RECEIPT_NAME = "publish-approval.json"
INVALIDATION_SCHEMA = "haru.publish_dependent_state_invalidation.v1"
MIN_REASON_CHARS = 12
SUBSTRING_MARKERS = ("bearer ", "-----begin")
TOKEN_MARKERS = ("xox", "xapp-", "sk-", "ghp_", "gho_", "aiza", "ya29.")
CREDENTIAL_MARKERS = SUBSTRING_MARKERS + TOKEN_MARKERS


def credential_like(value) -> bool:
    """True when free-form override text appears to contain a credential."""
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    if any(marker in lowered for marker in SUBSTRING_MARKERS):
        return True
    return any(token.startswith(TOKEN_MARKERS) for token in re.split(r"[\s,;]+", lowered))


def scan_for_credentials(receipt) -> list:
    found = []
    for key, value in receipt.items():
        if credential_like(value):
            found.append(key)
        elif isinstance(value, list) and any(credential_like(v) for v in value):
            found.append(key)
    return sorted(found)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def receipt_path(project: Path) -> Path:
    return project / "publish" / RECEIPT_NAME


def read_receipt(project: Path):
    path = receipt_path(project)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def resolve_project(project_arg: str, workspace: Path) -> Path:
    candidate = Path(project_arg)
    if os.sep in project_arg or project_arg.startswith("."):
        if candidate.is_dir():
            return candidate.resolve()
    return (workspace.resolve() / "projects" / project_arg).resolve()


def current_final(project: Path, workspace: Path):
    status, _artifacts = agent_status.build(project, workspace)
    final = (status.get("canonical_artifacts") or {}).get("final_video") or {}
    return status, final


def _fail(code: str, message: str, **extra):
    payload = {"schema": SCHEMA, "ok": False, "code": code, "message": message}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1


def _ok(**fields):
    payload = {"schema": SCHEMA, "ok": True}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _project_contract(project):
    path = agent_status.project_file(project / "project-contract.json", project)
    return agent_status.read_json(path) if path else None


def _attestation_values(ref):
    """Read a protected attestation only to obtain its already-bound nonce/generation."""
    fd = approval_attestation._open(ref, os.O_RDONLY)
    try:
        return approval_attestation._read(fd)
    finally:
        os.close(fd)


def _write_project_json(project, path, value):
    if (
        agent_status.canonical_layout.direct_path(path.parent, project) is None
        or agent_status.canonical_layout.direct_path(path, project) is None
    ):
        raise ValueError(f"{path} is not a direct project path")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _archive_invalidation(project, existing, replacement_intent):
    """Keep target-change evidence without deleting upload-attempt audit state."""
    if not isinstance(existing, dict):
        return
    previous = existing.get("approval_intent_sha256")
    if not isinstance(previous, str) or not previous:
        previous = hashlib.sha256(
            json.dumps(existing, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    record = {
        "schema": INVALIDATION_SCHEMA,
        "invalidated_at": now_iso(),
        "previous_approval_intent_sha256": previous,
        "replacement_approval_intent_sha256": replacement_intent,
        "reason": "a canonical publish intent field changed; prior pack, approval, and upload attempts remain audit-only",
    }
    path = project / "publish" / "approval-history" / f"{previous}.invalidation.json"
    _write_project_json(project, path, record)


def cmd_approve(args) -> int:
    workspace = Path(args.workspace).resolve()
    project = resolve_project(args.project, workspace)
    if not project.is_dir():
        return _fail("project_missing", f"no project at {project}")

    status, final = current_final(project, workspace)
    contract = _project_contract(project)
    target, target_error = agent_status.canonical_publish_target(contract)
    if target is None:
        return _fail("publish_target_invalid", target_error)
    if status.get("overall_status") not in {
        "ready_for_human_upload_approval",
        "publish_approved",
    }:
        return _fail(
            "not_ready",
            f"overall_status is {status.get('overall_status')!r}; approval requires ready_for_human_upload_approval",
            blockers=status.get("blockers", []),
        )
    if not final.get("sha256"):
        return _fail("final_video_missing", "no canonical final video to approve")

    # The two v3 evidence refs come from the status reader, which is the same
    # code that decided they are current. Deriving them here instead would be a
    # second opinion the approval could be recorded against and then immediately
    # read as stale.
    intent_refs = status.get("approval_intent_refs") or {}
    self_eval_ref = intent_refs.get("render_self_eval")
    review_ref = intent_refs.get("visual_qa_review")
    if not agent_status.valid_ref(self_eval_ref):
        return _fail(
            "self_eval_not_current",
            "no current render self-evaluation pass to bind; approval requires the deterministic gate first",
        )
    if not agent_status.valid_ref(review_ref):
        return _fail(
            "visual_review_not_current",
            "no current visual QA review to bind; approval must postdate the human verdict on this render",
        )

    warnings = sorted(
        warning
        for warning in (status.get("warnings") or [])
        if warning != agent_status.STALE_APPROVAL_WARNING
    )
    reason = (getattr(args, "override_reason", "") or "").strip()
    if reason and credential_like(reason):
        return _fail(
            "credential_in_override_reason",
            "refusing override reason because it looks like a credential",
        )
    if warnings and len(reason) < MIN_REASON_CHARS:
        return _fail(
            "override_reason_required",
            f"{len(warnings)} warning(s) outstanding; --override-reason of at least {MIN_REASON_CHARS} characters is required to approve",
            warnings=warnings,
        )
    if not warnings:
        reason = None

    existing = read_receipt(project)
    ref = getattr(args, "attestation_ref", None)
    try:
        attestation = _attestation_values(ref)
    except (OSError, ValueError) as error:
        return _fail("attestation_unavailable", str(error))
    nonce = attestation.get("nonce") if isinstance(attestation, dict) else None
    generation = attestation.get("generation") if isinstance(attestation, dict) else None
    prior_generation = existing.get("generation") if isinstance(existing, dict) else None
    if (
        isinstance(prior_generation, int)
        and not isinstance(prior_generation, bool)
        and (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation <= prior_generation
        )
    ):
        return _fail(
            "generation_not_fresh",
            "reapproval requires an attestation with a generation greater than the prior approval",
        )
    intent = agent_status.approval_intent(
        project.name,
        status.get("canonical_artifacts") or {},
        contract,
        warnings,
        reason,
        ref,
        nonce,
        generation,
        render_self_eval_ref=self_eval_ref,
        visual_qa_review_ref=review_ref,
    )
    if intent is None:
        return _fail(
            "approval_intent_invalid",
            "canonical artifacts, project target, evaluator/runtime contract, render self-evaluation, or visual review cannot form a full approval intent",
        )
    intent_sha256 = agent_status.approval_intent_sha256(intent)
    receipt = {
        "schema": SCHEMA,
        "project": project.name,
        "project_id": project.name,
        "approved_at": now_iso(),
        "approval_intent_sha256": intent_sha256,
        "warnings_acknowledged": warnings,
        "override_reason": reason,
        "final_path": final.get("path"),
        "final_sha256": intent["final_sha256"],
        "final_bytes": intent["final_bytes"],
        "metadata_sha256": intent["metadata_sha256"],
        "cover_sha256": intent["cover_sha256"],
        "channel_id": intent["channel_id"],
        "visibility": agent_status.PUBLISH_VISIBILITY,
        "runtime_contract": intent["runtime_contract"],
        "render_self_eval": intent["render_self_eval"],
        "visual_qa_review": intent["visual_qa_review"],
        "generation": intent["generation"],
        "attestation_ref": ref,
        "nonce": nonce,
        "video_id": None,
        "uploaded_at": None,
    }
    if existing and existing.get("video_id"):
        receipt["supersedes"] = {
            "approval_intent_sha256": existing.get("approval_intent_sha256"),
            "video_id": existing.get("video_id"),
            "uploaded_at": existing.get("uploaded_at"),
        }
    leaked = scan_for_credentials(receipt)
    if leaked:
        return _fail("credential_in_receipt", f"refusing to write a receipt: {leaked} looks like a credential")

    path = receipt_path(project)
    if (
        agent_status.canonical_layout.direct_path(path.parent, project) is None
        or agent_status.canonical_layout.direct_path(path, project) is None
    ):
        return _fail("receipt_not_in_project", f"{path} is not a direct path inside the project")
    try:
        # Consumption happens before the repo write. A crash can leave a stale
        # receipt, but can never leave a reusable human decision.
        approval_attestation.consume(
            ref,
            project.name,
            intent_sha256,
            nonce,
            generation,
            project_root=project,
        )
        _archive_invalidation(project, existing, intent_sha256)
        _write_project_json(project, path, receipt)
    except (OSError, ValueError) as error:
        if str(error) == "issuer_not_enrolled":
            return _fail("issuer_not_enrolled", str(error))
        return _fail("approval_write_failed", str(error))
    if agent_status.project_file(path, project) is None:
        return _fail("receipt_not_in_project", f"{path} is not a plain file inside the project; approval not recorded")
    return _ok(
        state="publish_approved",
        receipt=str(path.relative_to(project)),
        final_sha256=intent["final_sha256"],
        approval_intent_sha256=intent_sha256,
        channel_id=target["youtube_channel_id"],
        warnings_acknowledged=len(warnings),
    )


def cmd_prepare(args) -> int:
    """Build an exact, short-lived signing request without recording approval."""
    workspace = Path(args.workspace).resolve()
    project = resolve_project(args.project, workspace)
    if not project.is_dir():
        return _fail("project_missing", f"no project at {project}")

    status, final = current_final(project, workspace)
    contract = _project_contract(project)
    target, target_error = agent_status.canonical_publish_target(contract)
    if target is None:
        return _fail("publish_target_invalid", target_error)
    if status.get("overall_status") not in {
        "ready_for_human_upload_approval",
        "publish_approved",
    }:
        return _fail(
            "not_ready",
            f"overall_status is {status.get('overall_status')!r}; approval requires ready_for_human_upload_approval",
            blockers=status.get("blockers", []),
        )
    if not final.get("sha256"):
        return _fail("final_video_missing", "no canonical final video to approve")

    intent_refs = status.get("approval_intent_refs") or {}
    self_eval_ref = intent_refs.get("render_self_eval")
    review_ref = intent_refs.get("visual_qa_review")
    if not agent_status.valid_ref(self_eval_ref):
        return _fail(
            "self_eval_not_current",
            "no current render self-evaluation pass to bind; approval requires the deterministic gate first",
        )
    if not agent_status.valid_ref(review_ref):
        return _fail(
            "visual_review_not_current",
            "no current visual QA review to bind; approval must postdate the human verdict on this render",
        )

    warnings = sorted(
        warning
        for warning in (status.get("warnings") or [])
        if warning != agent_status.STALE_APPROVAL_WARNING
    )
    reason = (getattr(args, "override_reason", "") or "").strip()
    if reason and credential_like(reason):
        return _fail(
            "credential_in_override_reason",
            "refusing override reason because it looks like a credential",
        )
    if warnings and len(reason) < MIN_REASON_CHARS:
        return _fail(
            "override_reason_required",
            f"{len(warnings)} warning(s) outstanding; --override-reason of at least {MIN_REASON_CHARS} characters is required to approve",
            warnings=warnings,
        )
    if not warnings:
        reason = None

    existing_path = receipt_path(project)
    existing = read_receipt(project)
    if existing is None:
        if os.path.lexists(existing_path):
            return _fail(
                "prior_generation_invalid",
                "the prior approval receipt is malformed; refusing to issue a signing request",
            )
        generation = 1
    else:
        prior_generation = existing.get("generation")
        if (
            not isinstance(prior_generation, int)
            or isinstance(prior_generation, bool)
            or prior_generation < 1
        ):
            return _fail(
                "prior_generation_invalid",
                "the prior approval generation is malformed; refusing to issue a signing request",
            )
        generation = prior_generation + 1

    try:
        pin = approval_attestation.load_signer_pin()
    except ValueError as error:
        code = "issuer_not_enrolled" if str(error) == "issuer_not_enrolled" else "signer_pin_invalid"
        return _fail(code, str(error))
    except OSError as error:
        return _fail("issuer_not_enrolled", str(error))

    attestation_ref = f"attestation:{uuid.uuid4()}"
    nonce = secrets.token_hex(32)
    intent = agent_status.approval_intent(
        project.name,
        status.get("canonical_artifacts") or {},
        contract,
        warnings,
        reason,
        attestation_ref,
        nonce,
        generation,
        render_self_eval_ref=self_eval_ref,
        visual_qa_review_ref=review_ref,
    )
    if intent is None:
        return _fail(
            "approval_intent_invalid",
            "canonical artifacts, project target, evaluator/runtime contract, render self-evaluation, or visual review cannot form a full approval intent",
        )
    intent_sha256 = agent_status.approval_intent_sha256(intent)
    issued = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    immutable = {
        "schema": ATTESTATION_SCHEMA,
        "attestation_ref": attestation_ref,
        "project_id": project.name,
        "project_root_sha256": hashlib.sha256(
            str(project.resolve()).encode("utf-8")
        ).hexdigest(),
        "approval_intent_sha256": intent_sha256,
        "nonce": nonce,
        "generation": generation,
        "issued_at": issued.isoformat(),
        "expires_at": (issued + dt.timedelta(seconds=300)).isoformat(),
        "channel_id": target["youtube_channel_id"],
        "visibility": agent_status.PUBLISH_VISIBILITY,
        "key_id": pin["key_id"],
        "signature_algorithm": "ecdsa-p256-sha256",
    }
    request = {
        "schema": SIGNING_REQUEST_SCHEMA,
        "project_root": str(project.resolve()),
        "intent": intent,
        "intent_sha256": intent_sha256,
        "attestation": immutable,
    }
    print(json.dumps(request, ensure_ascii=False, indent=2))
    return 0


def cmd_record_upload(args) -> int:
    """Record an already-completed external upload; this command does not upload."""
    workspace = Path(args.workspace).resolve()
    project = resolve_project(args.project, workspace)
    receipt = read_receipt(project)
    if receipt is None:
        return _fail("no_approval", "no approval receipt; approve before recording an upload")
    status, final = current_final(project, workspace)
    if status.get("overall_status") != "publish_approved":
        return _fail("approval_not_current", "the full approval intent is no longer current")
    if final.get("sha256") != receipt.get("final_sha256"):
        return _fail("sha_mismatch", "the final video changed after approval")
    if getattr(args, "visibility", agent_status.PUBLISH_VISIBILITY) != agent_status.PUBLISH_VISIBILITY:
        return _fail("visibility_invalid", "a canonical approval authorizes unlisted only")
    if receipt.get("video_id") and receipt["video_id"] != args.video_id:
        return _fail("already_uploaded", "this approval already records a different video id", video_id=receipt["video_id"])
    receipt["video_id"] = args.video_id
    receipt["uploaded_at"] = getattr(args, "uploaded_at", None) or now_iso()
    receipt["visibility"] = agent_status.PUBLISH_VISIBILITY
    try:
        _write_project_json(project, receipt_path(project), receipt)
    except (OSError, ValueError) as error:
        return _fail("receipt_not_in_project", str(error))
    return _ok(video_id=args.video_id, uploaded_at=receipt["uploaded_at"])


def evaluate(project: Path, workspace: Path):
    status, _final = current_final(project, workspace)
    approval = status.get("publish_approval") or {"state": "absent"}
    return {
        "approved": status.get("overall_status") == "publish_approved",
        "overall_status": status.get("overall_status"),
        "blockers": status.get("blockers", []),
        **approval,
    }


def cmd_check(args) -> int:
    workspace = Path(args.workspace).resolve()
    project = resolve_project(args.project, workspace)
    result = evaluate(project, workspace)
    payload = {"schema": SCHEMA, "ok": bool(result.get("approved"))}
    payload.update(result)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.get("approved") else 1


def main() -> int:
    default_workspace = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Record and check one-time human publish approval.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("approve", help="Consume a human attestation for the current canonical publish intent")
    p.add_argument("project")
    p.add_argument("--workspace", default=default_workspace, type=Path)
    p.add_argument("--attestation-ref", required=True, help="One-time protected human attestation reference")
    p.add_argument("--override-reason", default="", help="Required when non-stale warnings are outstanding")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("prepare", help="Prepare a short-lived publish approval signing request")
    p.add_argument("project")
    p.add_argument("--workspace", default=default_workspace, type=Path)
    p.add_argument("--override-reason", default="", help="Required when non-stale warnings are outstanding")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("record-upload", help="Record an externally completed unlisted upload")
    p.add_argument("project")
    p.add_argument("--workspace", default=default_workspace, type=Path)
    p.add_argument("--video-id", required=True)
    p.add_argument("--uploaded-at", default=None)
    p.add_argument("--visibility", default=agent_status.PUBLISH_VISIBILITY, choices=[agent_status.PUBLISH_VISIBILITY])
    p.set_defaults(func=cmd_record_upload)

    p = sub.add_parser("check", help="Report approval state; exit non-zero when invalid")
    p.add_argument("project")
    p.add_argument("--workspace", default=default_workspace, type=Path)
    p.set_defaults(func=cmd_check)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
