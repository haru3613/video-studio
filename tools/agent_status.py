#!/usr/bin/env python3
"""Build agent-readable project status for Haru video projects.

This is intentionally file-based: Hermes/OpenClaw can resume from the project
folder without relying on chat history.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import struct
import sys
import zlib

from pathlib import Path

# scripts/ entry points run `/usr/bin/python3 -I`, and -I implies -P: the
# script's own directory is NOT put on sys.path. Importing a sibling module
# therefore fails there while working fine under a plain `python3 tools/...`,
# which is why this survived a probe run the wrong way.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical_layout  # noqa: E402
import editorial_contract  # noqa: E402
import final_quality_authority  # noqa: E402
import pronunciation_workflow  # noqa: E402
import render_contract  # noqa: E402
import render_self_eval  # noqa: E402
import segment_assembly  # noqa: E402
import segment_plan  # noqa: E402
import segment_render  # noqa: E402
import visual_qa_sample  # noqa: E402


SCHEMA_STATUS = "haru.pipeline_status.v1"
SCHEMA_ARTIFACTS = "haru.artifact_manifest.v1"
# HVP-33 bumped this to v3: the approval intent now also binds the current
# render self-evaluation pass and the current HVP-21 v2 visual review, so a
# v2 receipt no longer describes a complete intent and reads as absent.
SCHEMA_PUBLISH_APPROVAL = "haru.publish_approval.v3"
SCHEMA_PROJECT_CONTRACT = "haru.project_contract.v1"
PUBLISH_VISIBILITY = "unlisted"
YOUTUBE_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

# Source types describing a subject from INSIDE the world an episode is about --
# what the trade charges, what a participant did -- as opposed to statute,
# dataset, news and academic work, which describe it from outside. Both kinds
# pass citation review, so nothing noticed when a project collected only the
# outside kind: prepay-card-shop-fraud-taiwan-2026 shipped 33 claims, every one
# institutional, and never learned how its own subject priced itself.
DOMAIN_SOURCE_TYPES = frozenset(
    {
        "community",
        "marketplace",
        "participant",
        "practitioner",
        "price_listing",
        "trade_press",
    }
)

LONGFORM_QA_CHECKS = [
    "duration_and_decode_gate",
    "static_frame_gate",
    "black_frame_scan",
    "visual_spot_check",
    "visual_sampling",
]
LANE_CONTRACTS = {
    "social_issue_longform.v1": {
        "selection_policy": "topic_foundry",
        "required_qa_checks": LONGFORM_QA_CHECKS,
        "production_profile": editorial_contract.PROFILE,
    },
    "tech_longform.v1": {
        "selection_policy": "human_provenance",
        "required_qa_checks": LONGFORM_QA_CHECKS,
        "production_profile": "haru_tech.v1",
    },
    "manual.v1": {
        "selection_policy": "human_provenance",
        "required_qa_checks": LONGFORM_QA_CHECKS,
    },
}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except OSError as exc:
        return {"_error": f"cannot read json: {exc}"}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"_error": f"invalid json: {exc}"}


def rel(path, root):
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except (OSError, RuntimeError, ValueError):
        return str(path)


def file_info(path, root, with_sha=False):
    if not path or not path.exists():
        return None
    try:
        digest = stable_sha256(path) if with_sha else None
        stat = path.stat()
        info = {
            "path": rel(path, root),
            "bytes": stat.st_size,
            "mtime": dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        }
    except OSError:
        return None
    if with_sha:
        info["sha256"] = digest
    return info


# A stale approval is a *state*, not a content warning a human must justify.
# It is named so cmd_approve can tell the two apart; see docs/publish-approval.md.
STALE_APPROVAL_WARNING = (
    "publish approval is stale: the final video changed after it was "
    "approved; re-approve before uploading"
)


def runtime_manifest():
    """The promoted runtime is the only canonical channel/evaluator authority."""
    return read_json(Path(__file__).resolve().parents[1] / "pipeline" / "runtime-manifest.json")


def canonical_publish_target(project_contract):
    """Return the project target only when it is the promoted channel.

    The project contract names the desired target; the runtime manifest names
    the only channel this installation is allowed to publish to. Neither a CLI
    value nor a receipt can introduce another authority.
    """
    target = (
        project_contract.get("publish_target")
        if isinstance(project_contract, dict)
        else None
    )
    channel_id = target.get("youtube_channel_id") if isinstance(target, dict) else None
    manifest = runtime_manifest()
    canonical_channel = (
        manifest.get("youtube_channel_id") if isinstance(manifest, dict) else None
    )
    if not isinstance(channel_id, str) or not YOUTUBE_CHANNEL_ID.fullmatch(channel_id):
        return None, "project-contract publish_target.youtube_channel_id is missing or malformed"
    if (
        not isinstance(canonical_channel, str)
        or not YOUTUBE_CHANNEL_ID.fullmatch(canonical_channel)
        or channel_id != canonical_channel
    ):
        return None, "project-contract publish target does not match the canonical runtime channel"
    return {
        "youtube_channel_id": channel_id,
        "visibility": PUBLISH_VISIBILITY,
    }, None


def approval_runtime_contract(project_contract):
    """Return the project runtime/evaluator contract when it matches this runtime."""
    value = project_contract.get("runtime_contract") if isinstance(project_contract, dict) else None
    manifest = runtime_manifest()
    if not isinstance(value, dict) or not isinstance(manifest, dict):
        return None
    expected = {
        "schema": "haru.project_runtime_contract.v1",
        "runtime": manifest.get("runtime_contract"),
        "evaluator": manifest.get("evaluator_contract"),
        "artifact": manifest.get("artifact_contract"),
    }
    if any(
        not isinstance(expected[field], str) or not expected[field].strip()
        for field in ("runtime", "evaluator", "artifact")
    ):
        return None
    return expected if value == expected else None


def valid_ref(value):
    """An exact `{path,sha256,bytes}` reference to project bytes."""
    return bool(
        isinstance(value, dict)
        and set(value) == {"path", "sha256", "bytes"}
        and isinstance(value.get("path"), str)
        and value["path"]
        and isinstance(value.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
        and isinstance(value.get("bytes"), int)
        and not isinstance(value["bytes"], bool)
        and value["bytes"] > 0
    )


def approval_intent(
    project_id,
    canonical,
    project_contract,
    warnings_acknowledged,
    override_reason,
    attestation_ref,
    nonce,
    generation,
    *,
    render_self_eval_ref,
    visual_qa_review_ref,
):
    """Build the full, canonical approval intent or return ``None``.

    Kept in the status reader so approval validity is a recomputation, not a
    trust decision made when a receipt was first written.

    The two v3 refs are the HVP-33 anti-resurrection property. Both are supplied
    already validated — the self-eval ref only exists when the external ledger
    anchors a current pass, and the review ref only exists when the current v2
    human review validates against that same pass. Replacing either changes the
    intent digest, so an approval can never outlive the evidence it was given.
    """
    final = canonical.get("final_video") or {}
    metadata = canonical.get("publish_metadata") or {}
    cover = canonical.get("cover") or {}
    target, _target_error = canonical_publish_target(project_contract)
    runtime_contract = approval_runtime_contract(project_contract)
    if (
        not isinstance(project_id, str)
        or not project_id
        or not isinstance(final.get("sha256"), str)
        or not isinstance(final.get("bytes"), int)
        or isinstance(final.get("bytes"), bool)
        or not isinstance(metadata.get("sha256"), str)
        or not isinstance(cover.get("sha256"), str)
        or target is None
        or runtime_contract is None
        or not valid_ref(render_self_eval_ref)
        or not valid_ref(visual_qa_review_ref)
        or not isinstance(warnings_acknowledged, list)
        or not all(isinstance(warning, str) for warning in warnings_acknowledged)
        or override_reason is not None and not isinstance(override_reason, str)
        or not isinstance(attestation_ref, str)
        or not attestation_ref
        or not isinstance(nonce, str)
        or not nonce
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        return None
    return {
        "schema": SCHEMA_PUBLISH_APPROVAL,
        "project_id": project_id,
        "final_sha256": final["sha256"],
        "final_bytes": final["bytes"],
        "metadata_sha256": metadata["sha256"],
        "cover_sha256": cover["sha256"],
        "channel_id": target["youtube_channel_id"],
        "visibility": target["visibility"],
        "warnings_acknowledged": warnings_acknowledged,
        "override_reason": override_reason,
        "runtime_contract": runtime_contract,
        "render_self_eval": dict(render_self_eval_ref),
        "visual_qa_review": dict(visual_qa_review_ref),
        "generation": generation,
        "attestation_ref": attestation_ref,
        "nonce": nonce,
    }


def publish_target_sha256(target):
    if not isinstance(target, dict):
        return None
    return hashlib.sha256(
        json.dumps(target, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def approval_intent_sha256(intent):
    if not isinstance(intent, dict):
        return None
    return hashlib.sha256(
        json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def read_publish_approval(
    project,
    canonical,
    project_contract,
    warnings,
    render_self_eval_ref,
    visual_qa_review_ref,
):
    """Approval state for the current full publish intent.

    A v3 receipt is valid only when every field the human attested to is still
    current. The protected attestation is checked separately from repo state:
    copying a receipt cannot recreate a consumed human decision.

    The two refs arrive from the caller already validated. Passing them in rather
    than re-deriving them here is deliberate: the gate that decided the self-eval
    pass and the v2 review are current is the same one whose answer the approval
    is being compared against, so there is no second opinion to disagree with.
    """
    path = project_file(project / "publish" / "publish-approval.json", project)
    if path is None:
        return {"state": "absent"}
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "absent", "note": "approval receipt unreadable"}
    if not isinstance(receipt, dict):
        return {"state": "absent", "note": "approval receipt malformed"}
    if receipt.get("schema") != SCHEMA_PUBLISH_APPROVAL:
        return {"state": "absent", "note": "approval receipt has an unknown schema"}
    if receipt.get("project_id") != project.name:
        return {
            "state": "stale",
            "note": "approval project identity does not match the canonical project",
            "approval_intent_sha256": receipt.get("approval_intent_sha256"),
        }

    intent = approval_intent(
        project.name,
        canonical,
        project_contract,
        receipt.get("warnings_acknowledged"),
        receipt.get("override_reason"),
        receipt.get("attestation_ref"),
        receipt.get("nonce"),
        receipt.get("generation"),
        render_self_eval_ref=render_self_eval_ref,
        visual_qa_review_ref=visual_qa_review_ref,
    )
    current_warnings = sorted(
        warning for warning in warnings if warning != STALE_APPROVAL_WARNING
    )
    if intent is None:
        return {
            "state": "stale",
            "note": (
                "the canonical publish target, artifacts, runtime contract, "
                "render self-evaluation pass, or visual review no longer forms "
                "the approved intent"
            ),
            "approval_intent_sha256": receipt.get("approval_intent_sha256"),
        }
    if receipt.get("warnings_acknowledged") != current_warnings:
        return {
            "state": "stale",
            "note": "approval warnings no longer match the current publish intent",
            "approval_intent_sha256": receipt.get("approval_intent_sha256"),
        }
    for field in (
        "final_sha256",
        "final_bytes",
        "metadata_sha256",
        "cover_sha256",
        "channel_id",
        "visibility",
        "runtime_contract",
        # Both HVP-33 refs are compared field-by-field as well as inside the
        # intent digest, so the stale note names which evidence moved.
        "render_self_eval",
        "visual_qa_review",
        "generation",
        "attestation_ref",
        "nonce",
    ):
        if receipt.get(field) != intent[field]:
            return {
                "state": "stale",
                "note": f"approval {field} no longer matches the current publish intent",
                "approval_intent_sha256": receipt.get("approval_intent_sha256"),
            }
    expected_sha256 = approval_intent_sha256(intent)
    if receipt.get("approval_intent_sha256") != expected_sha256:
        return {
            "state": "stale",
            "note": "approval intent no longer matches canonical artifacts or target",
            "approval_intent_sha256": receipt.get("approval_intent_sha256"),
            "current_approval_intent_sha256": expected_sha256,
        }
    try:
        import approval_attestation

        attestation_ok = approval_attestation.is_consumed(
            receipt["attestation_ref"],
            project.name,
            expected_sha256,
            receipt["nonce"],
            receipt["generation"],
            project_root=project,
        )
    except (OSError, ValueError):
        attestation_ok = False
    if not attestation_ok:
        return {
            "state": "stale",
            "note": "the protected human attestation is absent, stale, or not consumed",
            "approval_intent_sha256": expected_sha256,
        }
    return {
        "state": "valid",
        "approved_at": receipt.get("approved_at"),
        "approval_intent_sha256": expected_sha256,
        "generation": receipt["generation"],
        "channel_id": intent["channel_id"],
        "visibility": intent["visibility"],
        "warnings_acknowledged": current_warnings,
        "override_reason": receipt.get("override_reason"),
        "render_self_eval": intent["render_self_eval"],
        "visual_qa_review": intent["visual_qa_review"],
        "video_id": receipt.get("video_id"),
        "uploaded_at": receipt.get("uploaded_at"),
    }


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_sha256(path):
    try:
        before = path.stat()
        digest = sha256(path)
        after = path.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            return None
        return digest
    except OSError:
        return None


def stage(status, files=None, warnings=None, notes=None):
    return {
        "status": status,
        "files": [f for f in (files or []) if f],
        "warnings": warnings or [],
        "notes": notes or [],
    }


def resolve_project(arg, workspace):
    raw = Path(arg).expanduser()
    if raw.exists():
        return raw.resolve()
    return (workspace / "projects" / arg).resolve()


def latest(paths):
    existing = [p for p in paths if p.exists()]
    return max(existing, key=lambda p: (p.stat().st_mtime, str(p))) if existing else None


def project_file(path, project, allow_empty=False):
    # Path containment is defined once, in canonical_layout.direct_path. This
    # used to carry its own copy of the walk while the checklist used plain
    # is_file(), so a symlinked artifact read as done there and blocked here.
    candidate = canonical_layout.direct_path(path, project)
    if candidate is None:
        return None
    try:
        if not candidate.is_file():
            return None
        if not (allow_empty or candidate.stat().st_size > 0):
            return None
        # A scaffolded stub is not work. Gates that only ask "does this file
        # exist" — proposal and publish_pack — went green on a fresh `hvp
        # scaffold`, because a placeholder is a non-empty file. Rejecting the
        # marker here closes it for every gate at once, including ones added
        # later; checking it per-gate would just wait for the next omission.
        with candidate.open("rb") as handle:
            head = handle.read(4096)
        if canonical_layout.TODO_MARKER.encode() in head:
            return None
        return candidate
    except (OSError, ValueError):
        return None


def project_files(paths, project, allow_empty=False):
    return [safe for path in paths if (safe := project_file(path, project, allow_empty))]


def direct_project_path_matches(candidate, actual, project):
    try:
        candidate_path = project_file(candidate, project)
        actual_path = project_file(actual, project)
        return (
            bool(candidate_path and actual_path)
            and candidate_path == actual_path
            and candidate_path.resolve() == actual_path.resolve()
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_passes(check):
    if check is True:
        return True
    if not isinstance(check, dict):
        return False
    status = str(check.get("status", "")).lower()
    if status in {"warn", "warning", "fail", "failed", "missing"}:
        return False
    if ("ok" in check and check["ok"] is not True) or (
        "passed" in check and check["passed"] is not True
    ):
        return False
    return status == "pass" or (not status and (check.get("ok") is True or check.get("passed") is True))


def checks_pass(data, required=None):
    checks = data.get("checks", []) if isinstance(data, dict) else []
    if not isinstance(checks, list) or not checks:
        return False
    if required is None:
        return all(check_passes(check) for check in checks)
    if not isinstance(required, list) or not required or not all(
        isinstance(name, str) and bool(name) and name.strip() == name for name in required
    ):
        return False
    return all(
        (matches := [check for check in checks if isinstance(check, dict) and check.get("name") == name])
        and all(check_passes(check) for check in matches)
        for name in set(required)
    )


def valid_receipt_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


def valid_project_slug(value):
    return (
        isinstance(value, str)
        and len(value) <= 64
        and re.fullmatch(r"[a-z](?:[a-z0-9-]*[a-z0-9])?", value) is not None
    )


def selection_summary(project):
    contract_path = project_file(project / "project-contract.json", project)
    contract = read_json(contract_path) if contract_path else None
    receipt_path = project_file(project / ".hvp" / "selection.json", project)
    receipt = read_json(receipt_path) if receipt_path else None
    lane_id = contract.get("lane_contract") if isinstance(contract, dict) else None
    lane = LANE_CONTRACTS.get(lane_id)
    files = ["project-contract.json"] if contract_path else []
    notes = []
    selected = False

    if not isinstance(contract, dict) or contract.get("schema") != SCHEMA_PROJECT_CONTRACT:
        notes.append("project-contract.json is missing or invalid")
    elif not lane:
        notes.append("lane_contract is missing or unsupported")
    elif lane["selection_policy"] == "topic_foundry":
        if receipt_path:
            files.append(".hvp/selection.json")
        selected = bool(
            isinstance(receipt, dict)
            and receipt.get("schema_version") == 1
            and receipt.get("project_slug") == project.name
            and valid_project_slug(receipt.get("project_slug"))
            and all(
                valid_receipt_id(receipt.get(field))
                for field in ("cron_run_id", "candidate_id", "chosen_by")
            )
            and isinstance(receipt.get("chosen_at"), int)
            and not isinstance(receipt.get("chosen_at"), bool)
            and receipt["chosen_at"] > 0
        )
        if not selected:
            notes.append("valid immutable Topic Foundry selection receipt is required")
    else:
        provenance = contract.get("selection")
        selected = bool(
            isinstance(provenance, dict)
            and isinstance(provenance.get("chosen_by"), str)
            and bool(provenance["chosen_by"].strip())
            and isinstance(provenance.get("chosen_at"), int)
            and not isinstance(provenance.get("chosen_at"), bool)
            and provenance["chosen_at"] > 0
        )
        if not selected:
            notes.append("manual lane requires explicit human chosen_by and chosen_at")

    return (
        lane_id,
        lane,
        stage("pass" if selected else "missing", files=files, notes=notes),
        contract_path,
        receipt
        if isinstance(receipt, dict)
        else contract.get("selection")
        if isinstance(contract, dict)
        else None,
    )


def duration_target(project):
    path = project_file(project / "issue_brief.md", project)
    if not path:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(
        r"(?im)(?:Duration target|Target duration|Length)\s*:\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*seconds?",
        text,
    )
    return (float(match.group(1)), float(match.group(2))) if match else None


def png_dimensions(path):
    try:
        with path.open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            dimensions = None
            image_spec = None
            image_data = bytearray()
            while True:
                raw_length = handle.read(4)
                if len(raw_length) != 4:
                    return None
                length = struct.unpack(">I", raw_length)[0]
                if length > 64 * 1024 * 1024:
                    return None
                kind = handle.read(4)
                data = handle.read(length)
                checksum = handle.read(4)
                if len(kind) != 4 or len(data) != length or len(checksum) != 4:
                    return None
                if zlib.crc32(kind + data) != struct.unpack(">I", checksum)[0]:
                    return None
                if kind == b"IHDR" and length == 13:
                    width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                        ">IIBBBBB", data
                    )
                    if compression != 0 or filter_method != 0 or interlace not in {0, 1}:
                        return None
                    dimensions = (width, height)
                    image_spec = (bit_depth, color_type, interlace)
                if kind == b"IDAT" and data:
                    image_data.extend(data)
                    if len(image_data) > 64 * 1024 * 1024:
                        return None
                if kind == b"IEND":
                    if dimensions != (1280, 720) or not image_spec or not image_data:
                        return None
                    bit_depth, color_type, interlace = image_spec
                    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
                    valid_depths = {
                        0: {1, 2, 4, 8, 16},
                        2: {8, 16},
                        3: {1, 2, 4, 8},
                        4: {8, 16},
                        6: {8, 16},
                    }
                    if not channels or bit_depth not in valid_depths[color_type]:
                        return None
                    passes = (
                        [(0, 0, 1, 1)]
                        if interlace == 0
                        else [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                              (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]
                    )
                    row_lengths = []
                    for x_start, y_start, x_step, y_step in passes:
                        pass_width = max(0, (dimensions[0] - x_start + x_step - 1) // x_step)
                        pass_height = max(0, (dimensions[1] - y_start + y_step - 1) // y_step)
                        if pass_width:
                            row_bytes = (pass_width * channels * bit_depth + 7) // 8
                            row_lengths.extend([row_bytes + 1] * pass_height)
                    expected_bytes = sum(row_lengths)
                    decoder = zlib.decompressobj()
                    decoded = decoder.decompress(bytes(image_data), expected_bytes + 1)
                    if not decoder.eof or len(decoded) != expected_bytes:
                        return None
                    offset = 0
                    for row_length in row_lengths:
                        if decoded[offset] > 4:
                            return None
                        offset += row_length
                    return dimensions
    except (OSError, struct.error, zlib.error):
        pass
    return None


def claim_summary(project):
    path = project_file(project / "claims.json", project)
    sources_path = project_file(project / "sources.md", project)
    if not path or not sources_path:
        return None, stage("missing", warnings=["claims.json or sources.md missing"])
    data = read_json(path)
    if not isinstance(data, dict):
        return None, stage("missing", warnings=["claims.json missing or invalid"])
    claims = data.get("claims")
    if not isinstance(claims, list):
        return None, stage("missing", warnings=["claims.json claims must be a list"])
    source_types = {}
    domain_claims = 0
    warnings = []
    for item in claims:
        if not isinstance(item, dict):
            warnings.append("claim entry is invalid")
            continue
        st = item.get("source_type", "missing")
        if not isinstance(st, str) or not st:
            st = "missing"
            warnings.append(f"{item.get('id', 'unknown')}: source_type is invalid")
        source_types[st] = source_types.get(st, 0) + 1
        if st in DOMAIN_SOURCE_TYPES:
            domain_claims += 1
        if "blocked" in st or "snippet" in st:
            warnings.append(f"{item.get('id', 'unknown')}: source still needs manual lock ({st})")
        if (
            not isinstance(item.get("id"), str)
            or not item["id"].strip()
            or not isinstance(item.get("source_name"), str)
            or not item["source_name"].strip()
        ):
            warnings.append(f"{item.get('id', 'unknown')}: source identity is incomplete")
        if st != "derived" and (
            not isinstance(item.get("source_url"), str) or not item["source_url"].strip()
        ):
            warnings.append(f"{item.get('id', 'unknown')}: source_url is missing")
    try:
        sources_text = sources_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        sources_text = ""
        warnings.append("sources.md cannot be read")
    if not sources_text.strip():
        warnings.append("sources.md is empty")
    status = "pass" if claims and not warnings else "warn" if claims else "missing"
    # Reported, not warned: every warning here becomes a pre-upload blocker, and
    # missing domain evidence is an editorial judgement rather than a broken
    # citation. Blocking on it would strand every in-flight project at once.
    notes = []
    if claims and not domain_claims:
        notes.append(
            "no claim carries domain evidence (source_type in "
            f"{sorted(DOMAIN_SOURCE_TYPES)}): this episode can only describe its "
            "subject from the outside"
        )
    return {
        "path": "claims.json",
        "count": len(claims),
        "domain_claims": domain_claims,
        "source_types": source_types,
    }, stage(status, files=["claims.json"], warnings=warnings, notes=notes)


def find_final_audio(project, *, canonical_only=False):
    candidates = [project / "narration-final.mp3"]
    if not canonical_only:
        candidates += [
            project / "narration-final-v3-yu-sectioned-v5.mp3",
            *sorted(project_files(project.glob("narration-final-v3-yu-sectioned*.mp3"), project), key=lambda p: p.name, reverse=True),
            *sorted(project_files(project.glob("narration-final*.mp3"), project), key=lambda p: p.stat().st_mtime, reverse=True),
        ]
    for mp3 in candidates:
        mp3 = project_file(mp3, project)
        if not mp3:
            continue
        pron_ok = project_file(Path(str(mp3) + ".pron-ok.json"), project)
        srt = project_file(mp3.with_suffix(".srt"), project)
        return mp3, srt, pron_ok
    return None, None, None


def final_quality_provenance(project, review_path):
    """Only the fixed runner's off-project record proves mechanical QA ran."""
    if review_path != project / "quality-review/final-v1/review.json":
        return False

    def refs(paths):
        result = {}
        for key, name in paths.items():
            path = project_file(project / name, project)
            if path is None:
                return None
            result[key] = {"path": name, "sha256": stable_sha256(path), "bytes": path.stat().st_size}
        return result

    try:
        inputs = refs({
            "final_video": "output/final.mp4",
            "render_result": "output/final.mp4.render-result",
            "render_self_eval": canonical_layout.RENDER_SELF_EVAL_RESULT,
            "visual_qa_review": "quality-review/visual-sampling/visual-qa-review.json",
        })
        outputs = refs({
            "prep": "quality-review/final-v1/prep.json",
            "review": "quality-review/final-v1/review.json",
        })
        return bool(inputs and outputs and final_quality_authority.validate(project, inputs, outputs))
    except (OSError, ValueError):
        return False


def find_final_video(project):
    return render_contract.find_final_video(project, project_file)


final_video_candidates = render_contract.final_video_candidates
final_revision = render_contract.final_revision
newer_final_candidate = render_contract.newer_final_candidate


def publish_pack(project):
    candidates = [
        project / "youtube-publish-pack.md",
        project / "publish" / "publish_pack.md",
        project / "publish_pack.md",
    ]
    return latest(
        path for candidate in candidates if (path := project_file(candidate, project))
    )


def publish_pack_binds_target(pack, target):
    """A pack generated for another channel is a read-only historical artifact."""
    fingerprint = publish_target_sha256(target)
    if not pack or not fingerprint:
        return False
    try:
        text = pack.read_text(encoding="utf-8")
    except OSError:
        return False
    return f"<!-- haru.publish_target_sha256: {fingerprint} -->" in text


def reference_analysis_summary(project):
    declaration_path = project_file(project / "reference-videos.json", project)
    if not declaration_path:
        return None
    analysis_path = project_file(project / "reference-analysis.json", project)
    adoption_path = project_file(project / "reference-adoption.json", project)
    declaration = read_json(declaration_path)
    analysis = read_json(analysis_path) if analysis_path else None
    adoption = read_json(adoption_path) if adoption_path else None
    receipt_path = project_file(
        project / ".hvp/producer-receipts/reference-analysis.json", project
    )
    receipt = read_json(receipt_path) if receipt_path else None
    warnings = []

    references = (
        declaration.get("references") if isinstance(declaration, dict) else None
    )
    selected = (
        [
            item
            for item in references
            if isinstance(item, dict) and item.get("selected") is True
        ]
        if isinstance(references, list)
        else []
    )
    selected_id = selected[0].get("id") if len(selected) == 1 else None
    rules = analysis.get("editing_rules") if isinstance(analysis, dict) else None
    rule_ids = (
        {
            item.get("id")
            for item in rules
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if isinstance(rules, list)
        else set()
    )
    evidence = analysis.get("evidence") if isinstance(analysis, dict) else None
    valid_analysis = bool(
        isinstance(declaration, dict)
        and declaration.get("schema") == "haru.reference_videos.v1"
        and len(selected) == 1
        and isinstance(selected_id, str)
        and isinstance(analysis, dict)
        and analysis.get("schema") == "haru.reference_video_analysis.v1"
        and analysis.get("reference_id") == selected_id
        and rule_ids
        and len(rule_ids) == len(rules)
        and isinstance(evidence, dict)
        and evidence.get("declaration_sha256") == stable_sha256(declaration_path)
        and isinstance(receipt, dict)
        and receipt.get("schema") == "haru.producer_receipt.v1"
        and receipt.get("source_sha256") == stable_sha256(declaration_path)
        and receipt.get("output_sha256") == stable_sha256(analysis_path)
    )

    adopted_items = (
        adoption.get("adopted_rules") if isinstance(adoption, dict) else None
    )
    rejected_items = (
        adoption.get("rejected_rules") if isinstance(adoption, dict) else None
    )
    adopted = (
        {item.get("rule_id") for item in adopted_items if isinstance(item, dict)}
        if isinstance(adopted_items, list)
        else set()
    )
    rejected = (
        {item.get("rule_id") for item in rejected_items if isinstance(item, dict)}
        if isinstance(rejected_items, list)
        else set()
    )
    decisions = [*(adopted_items or []), *(rejected_items or [])]
    valid_adoption = bool(
        valid_analysis
        and isinstance(adoption, dict)
        and adoption.get("schema") == "haru.reference_adoption.v1"
        and adoption.get("reference_id") == selected_id
        and isinstance(adopted_items, list)
        and isinstance(rejected_items, list)
        and adopted.isdisjoint(rejected)
        and adopted | rejected == rule_ids
        and len(decisions) == len(rule_ids)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("reason"), str)
            and bool(item["reason"].strip())
            for item in decisions
        )
    )

    storyboard_path = project_file(
        project / "storyboard-final-timed.json", project
    )
    storyboard = read_json(storyboard_path) if storyboard_path else None
    scenes = storyboard.get("scenes") if isinstance(storyboard, dict) else None
    cited = set()
    if isinstance(scenes, list):
        for scene in scenes:
            values = (
                scene.get("reference_rule_ids")
                if isinstance(scene, dict)
                else None
            )
            if values is None:
                continue
            if not isinstance(values, list) or not all(
                isinstance(value, str) for value in values
            ):
                warnings.append("storyboard reference_rule_ids must be a string list")
                continue
            cited.update(values)
    if cited - adopted:
        warnings.append(
            "storyboard cites a reference rule that was not adopted"
        )

    status = (
        "missing"
        if not (valid_analysis and valid_adoption)
        else "warn"
        if warnings
        else "pass"
    )
    return {
        "stage": stage(
            status,
            files=[
                rel(declaration_path, project),
                rel(analysis_path, project),
                rel(adoption_path, project),
                rel(receipt_path, project),
            ],
            warnings=warnings,
            notes=[
                {
                    "reference_id": selected_id,
                    "adopted_rule_ids": sorted(adopted),
                    "rejected_rule_ids": sorted(rejected),
                }
            ],
        ),
        "declaration": declaration_path,
        "analysis": analysis_path,
        "adoption": adoption_path,
    }


# Status strings the self-eval engine can report, in lifecycle order. Named here
# so a blocker can say which of the four a project is sitting in rather than
# reducing all of them to "not passed".
SELF_EVAL_NEXT_ACTIONS = {
    None: "run render self-evaluation for the current final render",
    render_self_eval.STATUS_NEEDS_HUMAN: (
        "record the render self-evaluation vision review"
    ),
    render_self_eval.STATUS_FAIL: (
        "fix the render inputs, render again, then reevaluate"
    ),
    render_self_eval.STATUS_HUMAN_INTERVENTION: (
        "escalate to a human: render self-evaluation has spent all three attempts"
    ),
}


def render_self_eval_summary(project):
    """The current render self-evaluation state, or why there is not one.

    Fail-closed by construction: the engine's `current_pass` validates the
    project projection against the external authority ledger, so a hand-written
    or rolled-back project tree with no anchor behind it arrives here as "no
    current pass" rather than as evidence.

    Only the pass branch produces a ref, and only that ref is ever bound into a
    publish approval. The non-pass branch re-reads the projection purely to name
    the state and quote the engine's own fixed next action; nothing downstream
    derives authority from it.
    """
    current = render_self_eval.current_pass(project)
    if current is not None:
        result = current["result"]
        return {
            "ok": True,
            "status": render_self_eval.STATUS_PASS,
            "code": "self_eval_passed",
            "message": None,
            "attempt": result.get("attempt"),
            "max_attempts": result.get("max_attempts"),
            "next_action": result.get("next_action"),
            "ref": dict(current["ref"]),
            "files": [render_self_eval.RESULT_PATH],
        }
    try:
        result = render_self_eval.validate_current(project)
    except ValueError as exc:
        return {
            "ok": False,
            "status": None,
            "code": "self_eval_unavailable",
            "message": str(exc),
            "attempt": None,
            "max_attempts": None,
            "next_action": SELF_EVAL_NEXT_ACTIONS[None],
            "ref": None,
            "files": [],
        }
    status = result.get("status")
    return {
        "ok": False,
        "status": status,
        "code": f"self_eval_{status}",
        "message": None,
        "attempt": result.get("attempt"),
        "max_attempts": result.get("max_attempts"),
        # The engine fixes one next action per status; quoting it keeps a single
        # source rather than a second table drifting beside it.
        "next_action": result.get("next_action")
        or SELF_EVAL_NEXT_ACTIONS.get(status, SELF_EVAL_NEXT_ACTIONS[None]),
        "ref": None,
        "files": [render_self_eval.RESULT_PATH],
    }


def build(project, workspace):
    generated_at = now_iso()
    slug = project.name
    warnings = []
    blockers = []
    next_actions = []

    lane_id, lane_contract, selection_stage, contract_path, selection = selection_summary(project)
    project_contract = read_json(contract_path, {}) if contract_path else {}
    if selection_stage["status"] != "pass":
        next_actions.append("record human topic selection before proposal")

    proposal_files = [
        path.name
        for path in [project / "script-proposal.md", project / "sources.md"]
        if project_file(path, project)
    ]
    proposal_status = "pass" if len(proposal_files) == 2 else "missing"

    claims, source_stage = claim_summary(project)
    warnings.extend(source_stage["warnings"])
    if source_stage["status"] == "missing":
        blockers.append("claims.json is missing or invalid")

    render_plan_path = project_file(project / "render_plan.json", project)
    render_plan = read_json(render_plan_path, {}) if render_plan_path else {}
    canonical_audio_required = isinstance(render_plan, dict) and (
        render_plan.get("narration") == "narration-final.mp3"
        or render_plan.get("skip_pronunciation_gate") is True
    )
    audio, srt, pron_ok = find_final_audio(project, canonical_only=canonical_audio_required)
    pron_data = read_json(pron_ok, {}) if pron_ok else {}
    raw_pron_warnings = pron_data.get("warnings") if isinstance(pron_data, dict) else None
    pron_warnings_valid = isinstance(raw_pron_warnings, list)
    pron_warnings = (raw_pron_warnings if pron_warnings_valid else
                     ["pronunciation warnings must be a list"] if pron_ok else [])
    pronunciation_status = str(pron_data.get("status", "")).lower() if isinstance(pron_data, dict) else ""
    pron_failed = (
        not isinstance(pron_data, dict)
        or "_error" in pron_data
        or bool(pronunciation_status and pronunciation_status not in {"pass", "passed", "ok"})
        or ("ok" in pron_data and pron_data["ok"] is not True)
        or ("passed" in pron_data and pron_data["passed"] is not True)
        or not pron_warnings_valid
        or bool(pron_warnings)
        or not pron_data.get("sha256")
        or not audio
        or pron_data["sha256"] != stable_sha256(audio)
    )
    g2p_review = pronunciation_workflow.validate_current_review(project)
    if g2p_review["required"] and not g2p_review["ok"]:
        pron_failed = True
        pron_warnings.append(g2p_review["code"])
    tts_status = (
        "missing"
        if not (audio and srt and pron_ok)
        else "warn"
        if pron_failed
        else "pass"
    )
    warnings.extend(str(item) for item in pron_warnings)
    if tts_status != "pass":
        if not audio and isinstance(render_plan, dict) and render_plan.get("skip_pronunciation_gate") is True:
            next_actions.append(
                "embedded audio does not satisfy narrated publication: use derive-narration "
                "in the source project, listen and promote-derived-narration, then retime-visuals and render-project"
            )
        else:
            next_actions.append("finish sectioned final TTS and pronunciation stamp")
    segment_summary = segment_plan.validate(project)
    if segment_summary["mode"] == "segmented":
        segment_summary = segment_render.apply_lifecycle(segment_summary, project)
    current_assembly = (
        segment_assembly.current_receipt(project)
        if segment_summary["mode"] == "segmented"
        else None
    )
    segment_status = (
        "pass"
        if segment_summary["mode"] == "segmented"
        else "missing"
        if segment_summary["mode"] == "invalid_segment_plan"
        else None
    )
    if segment_summary["mode"] == "invalid_segment_plan":
        next_actions.append("repair segment-plan.json before continuing segmented production")
    elif segment_summary["mode"] == "segmented":
        current_id = segment_summary.get("next_actionable_segment")
        current = next(
            (
                item
                for item in segment_summary["segments"]
                if item.get("segment_id") == current_id
            ),
            {},
        )
        if current.get("status") in {"planned", "render_pending", "changes_requested"}:
            next_actions.append(f"render the current {current_id} segment for review")
        elif current.get("status") == "review_pending":
            next_actions.append(f"record the human {current_id} segment review")
        elif current_id is None and current_assembly is None:
            next_actions.append(
                "run leased assemble-segments for the four approved segments"
            )


    storyboard_validation = project_file(project / "storyboard-final-timed-validation.json", project)
    storyboard_data = read_json(storyboard_validation, {}) if storyboard_validation else {}
    storyboard_ok = bool(
        isinstance(storyboard_data, dict)
        and storyboard_data.get("ok") is True
        and checks_pass(storyboard_data)
    )
    storyboard_status = "pass" if storyboard_ok else "missing"
    if not storyboard_ok:
        next_actions.append("run storyboard timing validation")

    required_profile = lane_contract.get("production_profile") if lane_contract else None
    actual_profile = project_contract.get("production_profile") if isinstance(project_contract, dict) else None
    if not lane_contract:
        editorial_validation = {
            "schema": "haru.editorial_contract_validation.v1",
            "profile": None,
            "ok": False,
            "problems": ["a valid lane contract is required before editorial validation"],
            "ratios": {},
        }
        preview_validation = editorial_validation
    elif required_profile and actual_profile != required_profile:
        editorial_validation = {
            "schema": "haru.editorial_contract_validation.v1",
            "profile": required_profile,
            "ok": False,
            "problems": [f"production_profile must be {required_profile}"],
            "ratios": {},
        }
        preview_validation = editorial_validation
    elif required_profile:
        editorial_validation = editorial_contract.validate_project(
            project, require_preview=False
        )
        preview_validation = editorial_contract.validate_project(
            project, require_preview=True
        )
    else:
        editorial_validation = {"ok": True, "problems": [], "ratios": {}}
        preview_validation = editorial_validation
    editorial_status = "pass" if editorial_validation["ok"] else "missing"
    preview_status = "pass" if preview_validation["ok"] else "missing"
    if editorial_status != "pass":
        next_actions.append(
            "complete digest-bound B-roll sourcing, Mina identity, and editorial diversity"
        )
    elif preview_status != "pass":
        next_actions.append("render and review the 60-90 second editorial preview")
    reference_summary = reference_analysis_summary(project)
    if reference_summary:
        warnings.extend(reference_summary["stage"]["warnings"])
    if reference_summary and reference_summary["stage"]["status"] != "pass":
        next_actions.append(
            "import the selected reference analysis and record rule adoption"
        )

    video, review_path, review_data = find_final_video(project)
    video_inside_project = bool(project_file(video, project))
    video = project_file(video, project)
    final_video_info = file_info(video, workspace, with_sha=True) if video else None
    actual_video_sha = final_video_info.get("sha256") if final_video_info else None
    render_result_path = project_file(Path(str(video) + ".render-result"), project) if video else None
    render_result = render_contract.parse_render_result(render_result_path)
    render_status = (
        "pass"
        if video
        and preview_status == "pass"
        and (
            not required_profile
            or render_result.get("editorial_contract_sha256")
            == sha256(project / "editorial-contract.json")
        )
        and render_contract.final_mix_passes(
            render_result, project, video, actual_video_sha
        )
        else "missing"
    )
    if render_status != "pass" and not (
        segment_summary["mode"] == "segmented" and current_assembly is None
    ):
        next_actions.append(
            "run render-project and write the final render-result marker"
        )

    # HVP-33 sits between render and whole-video QA: the deterministic gate reads
    # the finished bytes, and only its pass lets a human be asked to look. The
    # next action is suppressed until render passes for the same reason render's
    # own is suppressed while assembly is pending — telling a producer to
    # self-evaluate a render that does not exist yet is noise.
    self_eval = render_self_eval_summary(project)
    self_eval_status = "pass" if self_eval["ok"] else "missing"
    if not self_eval["ok"] and render_status == "pass":
        next_actions.append(self_eval["next_action"])

    qa_status = "missing"
    required_qa_checks = (
        lane_contract.get("required_qa_checks") if lane_contract else None
    )
    visual_sampling = visual_qa_sample.validate_review(
        project, actual_video_sha, video
    )
    review_qa_checks = (
        [name for name in required_qa_checks if name != "visual_sampling"]
        if isinstance(required_qa_checks, list)
        else None
    )
    if isinstance(review_data, dict):
        critical_issues = review_data.get("critical_issues")
        review_warnings = review_data.get("warnings")
        qa_status = (
            "pass"
            if review_data.get("publish_readiness") == "ship"
            and preview_status == "pass"
            and isinstance(critical_issues, list)
            and not critical_issues
            and isinstance(review_warnings, list)
            and not review_warnings
            and isinstance(required_qa_checks, list)
            and bool(required_qa_checks)
            and checks_pass(review_data, review_qa_checks)
            and visual_sampling.get("ok") is True
            else "warn"
        )
    final_quality_proof_ok = final_quality_provenance(project, review_path)
    if qa_status == "pass" and not final_quality_proof_ok:
        qa_status = "missing"

    if visual_sampling.get("ok") is not True and self_eval["ok"]:
        # While self-eval has not passed, sampling *cannot* succeed: it refuses
        # with `self_eval_not_current`. Emitting both actions would name the
        # dependent step as if it were available.
        next_actions.append(
            "run structure-aware visual sampling and record the human verdict"
        )
    if qa_status != "pass":
        if self_eval["ok"] and visual_sampling.get("ok") is True:
            visual_review_path = project_file(project / visual_qa_sample.OUTPUT_DIR / visual_qa_sample.REVIEW_NAME, project)
            selected_review = project_file(review_path, project)
            current_human_hold = bool(
                isinstance(review_data, dict)
                and review_data.get("publish_readiness") == "hold"
                and review_data.get("video_sha256") == actual_video_sha
                and selected_review and visual_review_path
                and selected_review.stat().st_mtime > render_contract.human_review_time(project, project_file)
            )
            next_actions.append(
                "record a fresh visual_qa review to resolve the current final QA hold"
                if current_human_hold else "run_next with runner final-quality-review"
            )
        else:
            next_actions.append("finish render self-evaluation and visual_qa sample/review before final-quality-review")

    cover = latest(
        path
        for candidate in project.glob("output/cover*.png")
        if (path := project_file(candidate, project))
    )
    cover_status = (
        "missing"
        if not cover
        else "pass"
        if png_dimensions(cover) == (1280, 720)
        else "warn"
    )
    if cover_status != "pass":
        next_actions.append("create 1280x720 cover")

    publish_metadata_path = project_file(project / "publish-metadata.json", project)
    publish_metadata = (
        read_json(publish_metadata_path) if publish_metadata_path else None
    )
    try:
        canonical_layout.validate_publish_metadata(publish_metadata, slug)
        publish_metadata_valid = True
    except ValueError:
        publish_metadata_valid = False
    publish_target, target_error = canonical_publish_target(project_contract)
    publish_target_status = "pass" if publish_target else "missing"
    if publish_target_status != "pass":
        next_actions.append(
            "set project-contract.json publish_target.youtube_channel_id to the canonical runtime channel"
        )
    pack = publish_pack(project)
    pack_status = (
        "pass"
        if pack
        and publish_metadata_valid
        and publish_pack_binds_target(pack, publish_target)
        else "missing"
    )
    if pack_status != "pass":
        next_actions.append(
            "complete publish-metadata.json and generate a target-bound youtube-publish-pack.md"
        )

    if source_stage["warnings"]:
        next_actions.append("resolve source-lock warnings before public upload")

    prep_path = project_file(review_path.with_name("prep.json"), project) if review_path else None
    prep_data = read_json(prep_path, {}) if prep_path else {}
    render_duration = render_result.get("duration_seconds")
    prep_metadata = prep_data.get("metadata") if isinstance(prep_data, dict) else None
    prep_duration = prep_metadata.get("duration") if isinstance(prep_metadata, dict) else None
    target = duration_target(project)
    duration_ok = (
        number(render_duration)
        and number(prep_duration)
        and abs(render_duration - prep_duration) <= 0.5
        and target is not None
        and target[0] <= render_duration <= target[1]
    )
    duration_status = "pass" if duration_ok else "missing"

    prep_audio = prep_data.get("audio") if isinstance(prep_data, dict) else None
    loudness_lufs = render_result.get("loudness_lufs")
    loudness_ok = (
        bool(video)
        and isinstance(prep_data, dict)
        and prep_data.get("video_sha256") == actual_video_sha
        and isinstance(prep_audio, dict)
        and prep_audio.get("has_audio") is True
        and prep_audio.get("clipping") is False
        and prep_audio.get("too_quiet") is False
        and number(loudness_lufs)
        and -16 <= loudness_lufs <= -12
    )
    loudness_status = "pass" if loudness_ok else "missing"

    video_unchanged = bool(
        video
        and actual_video_sha
        and actual_video_sha == stable_sha256(video)
    )
    video_binding_ok = bool(
        review_path
        and video_inside_project
        and actual_video_sha
        and video_unchanged
        and review_data.get("video_sha256") == actual_video_sha
    )
    video_binding_status = "pass" if video_binding_ok else "missing"
    manifest_path = project_file(project / "artifact_manifest.json", project)
    existing_manifest = read_json(manifest_path, {}) if manifest_path else {}
    manifest_canonical = existing_manifest.get("canonical") if isinstance(existing_manifest, dict) else None
    declared_video = manifest_canonical.get("final_video") if isinstance(manifest_canonical, dict) else None
    manifest_binding_ok = False
    if isinstance(declared_video, dict):
        declared_path_value = declared_video.get("path")
        if isinstance(declared_path_value, str) and declared_path_value:
            declared_path = Path(declared_path_value)
            if not declared_path.is_absolute():
                declared_path = workspace / declared_path
            manifest_binding_ok = bool(
                video
                and final_video_info
                and video_unchanged
                and existing_manifest.get("schema") == SCHEMA_ARTIFACTS
                and existing_manifest.get("project") == slug
                and direct_project_path_matches(declared_path, video, project)
                and declared_video.get("sha256") == actual_video_sha
                and isinstance(declared_video.get("bytes"), int)
                and not isinstance(declared_video.get("bytes"), bool)
                and declared_video.get("bytes") == final_video_info.get("bytes")
            )
    manifest_binding_status = "pass" if manifest_binding_ok else "missing"
    review_freshness_ok = bool(
        review_path
        and video
        and not any(
            Path(os.path.abspath(candidate)) != Path(os.path.abspath(video))
            and newer_final_candidate(candidate, video)
            for candidate in final_video_candidates(project)
        )
    )
    review_freshness_status = "pass" if review_freshness_ok else "missing"

    artifacts = {
        "schema": SCHEMA_ARTIFACTS,
        "project": slug,
        "generated_at": generated_at,
        "canonical": {
            "final_video": final_video_info,
            "render_result": rel(render_result_path, workspace) if render_result_path and render_result_path.exists() else None,
            "final_audio": file_info(audio, workspace) if audio else None,
            "final_srt": file_info(srt, workspace) if srt else None,
            "pronunciation_stamp": file_info(pron_ok, workspace) if pron_ok else None,
            "pronunciation_plan": file_info(
                project_file(project / "pronunciation-plan.json", project),
                workspace,
                with_sha=True,
            ),
            "pronunciation_probe": file_info(
                project_file(project / ".hvp/pronunciation-probes.json", project),
                workspace,
                with_sha=True,
            ),
            "pronunciation_review": file_info(
                project_file(project / ".hvp/pronunciation-review.json", project),
                workspace,
                with_sha=True,
            ),
            "segment_plan": file_info(
                project_file(project / segment_plan.PLAN_PATH, project),
                workspace,
                with_sha=True,
            )
            if segment_summary["present"]
            else None,
            "segment_assembly": file_info(
                project_file(project / segment_assembly.RECEIPT_PATH, project),
                workspace,
                with_sha=True,
            )
            if segment_summary["mode"] == "segmented"
            else None,
            "storyboard_validation": file_info(storyboard_validation, workspace) if storyboard_validation else None,
            "editorial_contract": file_info(
                project_file(project / "editorial-contract.json", project),
                workspace,
                with_sha=True,
            ),
            "editorial_preview_review": file_info(
                project_file(
                    project / "quality-review/editorial-preview/review.json",
                    project,
                ),
                workspace,
                with_sha=True,
            ),
            "reference_videos": file_info(
                reference_summary["declaration"], workspace, with_sha=True
            )
            if reference_summary
            else None,
            "reference_analysis": file_info(
                reference_summary["analysis"], workspace, with_sha=True
            )
            if reference_summary
            else None,
            "reference_adoption": file_info(
                reference_summary["adoption"], workspace, with_sha=True
            )
            if reference_summary
            else None,
            "quality_review": file_info(review_path, workspace) if review_path else None,
            # The self-eval receipt is listed unconditionally, like every other
            # canonical entry: `artifact_index` and the status gate must agree
            # about whether it exists, and a conditional entry would let the
            # manifest go quiet exactly when the gate is blocking.
            "render_self_eval": file_info(
                project_file(
                    project / canonical_layout.RENDER_SELF_EVAL_RESULT, project
                ),
                workspace,
                with_sha=True,
            ),
            "visual_qa_sample": file_info(
                project_file(
                    project / visual_qa_sample.OUTPUT_DIR / visual_qa_sample.SAMPLE_NAME,
                    project,
                ),
                workspace,
                with_sha=True,
            ),
            "visual_qa_review": file_info(
                project_file(
                    project / visual_qa_sample.OUTPUT_DIR / visual_qa_sample.REVIEW_NAME,
                    project,
                ),
                workspace,
                with_sha=True,
            ),
            "visual_qa_contact_sheet": file_info(
                project_file(
                    project / visual_qa_sample.OUTPUT_DIR / visual_qa_sample.SHEET_NAME,
                    project,
                ),
                workspace,
                with_sha=True,
            ),
            "cover": file_info(cover, workspace, with_sha=True) if cover else None,
            "publish_metadata": file_info(
                publish_metadata_path, workspace, with_sha=True
            )
            if publish_metadata_path
            else None,
            "publish_pack": file_info(pack, workspace, with_sha=True) if pack else None,
            "project_contract": file_info(contract_path, workspace, with_sha=True)
            if contract_path
            else None,
            "selection_receipt": file_info(
                project_file(project / ".hvp" / "selection.json", project), workspace
            ),
        },
        "inventory": {
            "render_outputs": len(list((project / "output").glob("*"))) if (project / "output").exists() else 0,
            "root_audio_files": len(list(project.glob("*.mp3"))) + len(list(project.glob("*.m4a"))) + len(list(project.glob("*.aiff"))),
            "quality_review_dirs": len([p for p in (project / "quality-review").glob("*") if p.is_dir()])
            if (project / "quality-review").exists()
            else 0,
            "has_remotion_node_modules": (project / "remotion" / "node_modules").exists(),
        },
    }

    stages = {
        "selection": selection_stage,
        "proposal": stage(proposal_status, files=proposal_files),
        "sources": source_stage,
        "tts": stage(
            tts_status,
            files=[
                rel(audio, project),
                rel(srt, project),
                rel(pron_ok, project),
                *g2p_review["files"],
            ],
            warnings=pron_warnings,
        ),
        "storyboard": stage(storyboard_status, files=[rel(storyboard_validation, project)]),
        "editorial": stage(
            editorial_status,
            files=["editorial-contract.json"] if editorial_status == "pass" else [],
            notes=[editorial_validation],
        ),
        "editorial_preview": stage(
            preview_status,
            files=["quality-review/editorial-preview/review.json"]
            if preview_status == "pass"
            else [],
            notes=[preview_validation],
        ),
        "render": stage(
            render_status,
            files=[
                rel(video, project),
                rel(render_result_path, project),
                segment_assembly.RECEIPT_PATH
                if segment_summary["mode"] == "segmented"
                and project_file(project / segment_assembly.RECEIPT_PATH, project)
                else None,
            ],
            notes=[render_result],
        ),
        "render_self_eval": stage(
            self_eval_status,
            files=self_eval["files"],
            notes=[self_eval],
        ),
        "qa": stage(
            qa_status,
            files=[
                rel(review_path, project),
                *(visual_sampling.get("files") or []),
            ],
            notes=[
                {
                    "lane_contract": lane_id,
                    "required_checks": required_qa_checks or [],
                },
                visual_sampling,
                {"final_quality_runner_proof": final_quality_proof_ok},
            ],
        ),
        "cover": stage(cover_status, files=[rel(cover, project)]),
        "publish_pack": stage(
            pack_status,
            files=[
                rel(publish_metadata_path, project),
                rel(pack, project),
            ],
        ),
        "publish_target": stage(
            publish_target_status,
            files=["project-contract.json"] if publish_target else [],
            notes=[{"target": publish_target, "error": target_error}],
        ),
        "duration": stage(
            duration_status,
            files=[rel(render_result_path, project), rel(prep_path, project)],
            notes=[{"render_seconds": render_duration, "prep_seconds": prep_duration, "target_seconds": target}],
        ),
        "loudness": stage(
            loudness_status,
            files=[rel(prep_path, project)],
            notes=[{"loudness_lufs": loudness_lufs}],
        ),
        "video_binding": stage(
            video_binding_status,
            files=[rel(video, project), rel(review_path, project)],
        ),
        "manifest_binding": stage(
            manifest_binding_status,
            files=["artifact_manifest.json"] if manifest_path else [],
        ),
        "review_freshness": stage(review_freshness_status),
        "upload": stage("requires_harvey", notes=["Public upload/schedule/metadata changes require Harvey confirmation."]),
    }
    if segment_status is not None:
        stages["segments"] = stage(
            segment_status,
            files=[segment_plan.PLAN_PATH]
            if project_file(project / segment_plan.PLAN_PATH, project)
            else [],
            notes=[
                {
                    "mode": segment_summary["mode"],
                    "problems": segment_summary["problems"],
                }
            ],
        )

    if reference_summary:
        stages["reference_analysis"] = reference_summary["stage"]

    required_statuses = {
        "selection": selection_stage["status"],
        "proposal": proposal_status,
        "sources": source_stage["status"],
        "tts": tts_status,
        "storyboard": storyboard_status,
        "editorial": editorial_status,
        "editorial_preview": preview_status,
        "render": render_status,
        "render_self_eval": self_eval_status,
        "qa": qa_status,
        "cover": cover_status,
        "publish_pack": pack_status,
        "publish_target": publish_target_status,
        "duration": duration_status,
        "loudness": loudness_status,
        "video_binding": video_binding_status,
        "manifest_binding": manifest_binding_status,
        "review_freshness": review_freshness_status,
    }
    if reference_summary:
        required_statuses["reference_analysis"] = reference_summary["stage"]["status"]
    if segment_status is not None:
        required_statuses["segments"] = segment_status

    # Name the artifact, not just the gate. A blocker that says "gate storyboard
    # is missing" tells a producer nothing about what to make; the canonical
    # declaration knows the filename, so say it.
    gate_artifacts = {}
    for artifact in canonical_layout.ARTIFACTS:
        gate_artifacts.setdefault(artifact.gate, []).append(artifact.path)
    gate_artifacts["reference_analysis"] = [
        "reference-videos.json",
        "reference-analysis.json",
        "reference-adoption.json",
        ".hvp/producer-receipts/reference-analysis.json",
    ]
    gate_artifacts["editorial"] = ["editorial-contract.json"]
    gate_artifacts["editorial_preview"] = [
        "quality-review/editorial-preview/review.json"
    ]
    gate_artifacts["publish_target"] = ["project-contract.json"]

    gate_artifacts["segments"] = [segment_plan.PLAN_PATH]

    blocker_details = []
    for name, value in required_statuses.items():
        if value == "pass":
            continue
        expected = gate_artifacts.get(name) or []
        if name == "selection" and lane_id == "social_issue_longform.v1":
            expected = [*expected, ".hvp/selection.json"]
        message = f"required gate {name} is {value}"
        if expected:
            message += f" (expected {', '.join(expected)})"
        blocker_details.append(
            {
                "code": f"{name}_not_passed",
                "stage": name,
                "message": message,
                "expected_artifacts": expected,
            }
        )
    if segment_summary["mode"] == "invalid_segment_plan":
        for problem in segment_summary["problems"]:
            blocker_details.append(
                {
                    "code": "segment_plan_invalid",
                    "stage": "segments",
                    "message": f"segment-plan.json: {problem}",
                    "expected_artifacts": [segment_plan.PLAN_PATH],
                }
            )
    if not self_eval["ok"]:
        # The generic `render_self_eval_not_passed` blocker above says the gate
        # is missing. This one says which of the four lifecycle states the
        # project is actually in, and — for an unanchored or tampered projection
        # — why the projection was refused, which is the difference between
        # "run the evaluator" and "your project tree is not the authority".
        blocker_details.append(
            {
                "code": self_eval["code"],
                "stage": "render_self_eval",
                "message": (
                    f"render self-evaluation: {self_eval['message']}"
                    if self_eval["message"]
                    else f"render self-evaluation is {self_eval['status']}"
                ),
                "expected_artifacts": [canonical_layout.RENDER_SELF_EVAL_RESULT],
            }
        )

    for detail in blocker_details:
        if detail["message"] not in blockers:
            blockers.append(detail["message"])
    overall = (
        "ready_for_human_upload_approval"
        if not blocker_details
        else "in_progress"
    )

    # An approval is recomputed from all publish intent fields: final bytes,
    # metadata, cover, canonical target, warnings, runtime contract, generation,
    # nonce, consumed external human attestation, and — since HVP-33 — the
    # current render self-evaluation pass and the current v2 visual review.
    #
    # Both refs are published on the status so `publish_approval.py` binds the
    # exact same bytes this reader validated, instead of re-deriving them and
    # possibly answering differently.
    approval_intent_refs = {
        "render_self_eval": self_eval["ref"],
        "visual_qa_review": (
            visual_sampling.get("review_ref") if visual_sampling.get("ok") else None
        ),
    }
    approval = read_publish_approval(
        project,
        artifacts["canonical"],
        project_contract,
        warnings,
        approval_intent_refs["render_self_eval"],
        approval_intent_refs["visual_qa_review"],
    )
    if approval.get("state") == "stale":
        warnings.append(STALE_APPROVAL_WARNING)
    if overall == "ready_for_human_upload_approval" and approval.get("state") == "valid":
        overall = "publish_approved"

    status = {
        "schema": SCHEMA_STATUS,
        "project": slug,
        "generated_at": generated_at,
        "updated_by": os.environ.get("HARU_AGENT_NAME", "codex"),
        "overall_status": overall,
        "lock": None,
        "blockers": blockers,
        "blocker_details": blocker_details,
        "warnings": warnings,
        "next_actions": next_actions,
        "stages": stages,
        "required_stages": list(required_statuses),
        "canonical_artifacts": artifacts["canonical"],
        "claims": claims,
        "publish_approval": approval,
        "approval_intent_refs": approval_intent_refs,
        "lane_contract": lane_id,
        "segment_mode": segment_summary["mode"],
        "segments": segment_summary["segments"],
        "next_actionable_segment": segment_summary["next_actionable_segment"],
        "selection": selection,
    }
    return status, artifacts


def main():
    parser = argparse.ArgumentParser(description="Build Hermes/OpenClaw-readable Haru project status.")
    parser.add_argument("project", help="Project slug under projects/ or a project path")
    parser.add_argument("--workspace", default=Path(__file__).resolve().parents[1], type=Path)
    parser.add_argument("--write", action="store_true", help="Write pipeline_status.json and artifact_manifest.json")
    parser.add_argument("--check", action="store_true", help="Exit non-zero on hard blockers")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    project = resolve_project(args.project, workspace)
    if not project.exists():
        raise SystemExit(f"project not found: {project}")

    status, artifacts = build(project, workspace)
    if args.write:
        (project / "pipeline_status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (project / "artifact_manifest.json").write_text(
            json.dumps(artifacts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    print(json.dumps(status, ensure_ascii=False, indent=2))
    if args.check and status["blockers"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
