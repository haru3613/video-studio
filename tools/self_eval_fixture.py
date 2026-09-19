"""Test-only builder for an *anchored* render-self-eval state.

Unit tests elsewhere in the repository use tiny placeholder MP4 bytes. They
cannot invoke ffmpeg, but they must not gain a way to fabricate a project-local
pass that production validation would accept. `seal` therefore constructs only
the deterministic evidence payload, then goes through the same protected
prepare/retire/promote/commit protocol as production. Terminal reviewer states
also mint an external leaf as the test's authority issuer, then consume it
through production `record_review`.

No production code imports this module.
"""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import stat
from pathlib import Path

import render_self_eval as engine
import self_eval_authority as authority

VALID_STATUSES = {
    engine.STATUS_NEEDS_HUMAN,
    engine.STATUS_PASS,
    engine.STATUS_FAIL,
    engine.STATUS_HUMAN_INTERVENTION,
}


def _tool() -> dict:
    return {
        "algorithm": engine.ALGORITHM,
        "ffmpeg_version": "fixture-no-execution",
        "ffprobe_version": "fixture-no-execution",
        "inspired_by": dict(engine.INSPIRED_BY),
    }


def _start_pending(project: Path, snapshot: engine.Snapshot, attempt: int) -> dict:
    """Create a clean evaluation and anchor its pending current projection."""
    directory = engine.attempt_dir(attempt)
    stager = engine.Stager(snapshot.stage)
    created = engine.now_iso()
    marker = engine.render_contract.parse_render_result(snapshot.path(engine.MARKER))
    duration = marker.get("duration_seconds")
    if not engine._number(duration) or duration <= 0:
        storyboard = snapshot.json(engine.STORYBOARD)
        duration = storyboard.get("duration_seconds")
    if not engine._number(duration) or duration <= 0:
        duration = 1.0
    storyboard = snapshot.json(engine.STORYBOARD)
    fps = storyboard.get("fps", 30)
    if not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0:
        fps = 30

    plan = {
        "schema": engine.PLAN_SCHEMA,
        "project": project.name,
        "attempt_identity": snapshot.identity,
        "inputs": snapshot.entries,
        "boundary_policy": engine._ref(engine.POLICY_PATH, engine.POLICY_BYTES),
        "duration_seconds": engine._round(duration),
        "fps": float(fps),
        "windows": [],
        "created_at": created,
    }
    plan_bytes = authority.canonical_bytes(plan)
    plan_ref = stager.add_bytes(
        f"{directory}/boundary-plan.json", "boundary-plan.json", plan_bytes
    )
    commands = {
        "schema": engine.COMMANDS_SCHEMA,
        "project": project.name,
        "attempt_identity": snapshot.identity,
        "commands": [],
    }
    commands_ref = stager.add_bytes(
        f"{directory}/commands.json",
        "commands.json",
        authority.canonical_bytes(commands),
    )
    facts = {
        "schema": engine.FACTS_SCHEMA,
        "project": project.name,
        "attempt_identity": snapshot.identity,
        "media": {
            "duration_seconds": engine._round(duration),
            "fps": float(fps),
            "video_streams": 1,
            "audio_streams": 1,
            "full_decode_clean": True,
            "frame_pts_monotonic": True,
            "packet_dts_monotonic": True,
        },
        "windows": [],
    }
    facts_ref = stager.add_bytes(
        f"{directory}/facts.json", "facts.json", authority.canonical_bytes(facts)
    )
    entries = [
        engine._entry_available(commands_ref["path"], "commands", commands_ref, None),
        engine._entry_available(facts_ref["path"], "facts", facts_ref, None),
    ]
    index = {
        "schema": engine.INDEX_SCHEMA,
        "project": project.name,
        "attempt_identity": snapshot.identity,
        "entries": entries,
        "total_available_bytes": sum(entry["bytes"] for entry in entries),
    }
    index_ref = stager.add_bytes(
        f"{directory}/evidence-index.json",
        "evidence-index.json",
        authority.canonical_bytes(index),
    )
    tool = _tool()
    evaluation = {
        "schema": engine.EVALUATION_SCHEMA,
        "project": project.name,
        "attempt": attempt,
        "attempt_identity": snapshot.identity,
        "inputs": snapshot.entries,
        "boundary_policy": engine._ref(engine.POLICY_PATH, engine.POLICY_BYTES),
        "boundary_plan": plan_ref,
        "commands": commands_ref,
        "facts": facts_ref,
        "evidence_index": index_ref,
        "tool": tool,
        "checks": [
            {
                "check_id": "fixture_structural_contract",
                "status": "pass",
                "scope": "fixture",
                "details": "schema-valid deterministic fixture evidence",
            }
        ],
        "findings": [],
        "status": "clean",
        "remediation": engine.remediation(engine.STATUS_NEEDS_HUMAN),
        "evaluated_at": created,
    }
    evaluation_ref = stager.add_bytes(
        f"{directory}/evaluation.json",
        "evaluation.json",
        authority.canonical_bytes(evaluation),
    )
    current_plan_ref = engine._ref(engine.CURRENT_PLAN_PATH, plan_bytes)
    result = engine.build_result(
        project,
        status=engine.STATUS_NEEDS_HUMAN,
        attempt=attempt,
        identity=snapshot.identity,
        inputs=snapshot.entries,
        plan_ref=current_plan_ref,
        evaluation_ref=evaluation_ref,
        index_ref=index_ref,
        review_ref=None,
        outcome_ref=None,
        tool=tool,
        findings=[],
    )
    result_bytes = authority.canonical_bytes(result)
    current = {
        "boundary-policy.json": engine.POLICY_BYTES,
        "boundary-plan.json": plan_bytes,
        "render-self-eval.json": result_bytes,
    }
    refs = list(stager.refs.values()) + [
        engine._ref(engine.POLICY_PATH, engine.POLICY_BYTES),
        current_plan_ref,
        engine._ref(engine.RESULT_PATH, result_bytes),
    ]

    namespace, chain = engine._open_transaction(project, snapshot.root_fd)
    try:
        pending = authority.prepare(
            namespace,
            chain,
            transition="evaluate_pending",
            attempt=attempt,
            attempt_identity=snapshot.identity,
            expected_refs=refs,
            retirement=(
                engine.retirement_plan(project, snapshot.root_fd)
                if chain.generation == 0
                else None
            ),
        )
        authority.commit(
            namespace,
            chain,
            pending,
            authority=None,
            retire=lambda plan: engine.retire(
                project,
                plan,
                pending["transaction_id"],
                snapshot.root_fd,
            ),
            promote=lambda: engine._promote(
                project,
                pending["transaction_id"],
                attempt,
                snapshot.stage,
                current,
                remove=("review.json",),
                project_fd=snapshot.root_fd,
            ),
        )
    finally:
        namespace.close()
    return engine.validate_current(project)


def _ensure_attestation_root() -> Path:
    root = authority.attestation_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if (
        root.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ValueError("fixture attestation root must be owner-only mode 0700")
    return root


def _next_attestation_generation(project: Path, schema: str) -> int:
    with authority.locked(project, project.name) as namespace:
        chain = authority.validated_chain(namespace)
        return authority.max_consumed_generation(chain, schema) + 1


def _mint_attestation(project: Path, intent_sha256: str, schema: str) -> str:
    if schema not in (
        authority.VISION_ATTESTATION_SCHEMA,
        authority.HUMAN_ATTESTATION_SCHEMA,
    ):
        raise ValueError("fixture attestation schema is invalid")
    root = _ensure_attestation_root()
    suffix = f"fixture-{secrets.token_hex(12)}"
    ref = f"self-eval-attestation:{suffix}"
    value = {
        "schema": schema,
        "attestation_ref": ref,
        "project_id": project.name,
        "review_intent_sha256": intent_sha256,
        "nonce": secrets.token_hex(24),
        "generation": _next_attestation_generation(project, schema),
        "issued_at": engine.now_iso(),
        "consumed_at": None,
        "consumed_project_id": None,
        "consumed_intent_sha256": None,
    }
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd = os.open(
        root / f"{suffix}.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    return ref


def _mint_vision(project: Path, intent_sha256: str) -> str:
    return _mint_attestation(
        project, intent_sha256, authority.VISION_ATTESTATION_SCHEMA
    )


def _seal_review(project: Path, status: str) -> dict:
    current = engine.validate_current(project)
    verdict = "pass" if status == engine.STATUS_PASS else "fail"
    findings = []
    if verdict == "fail":
        findings = [
            {
                "timestamp_seconds": 0.0,
                "boundary_id": "fixture",
                "category": "fixture_reviewer_failure",
                "severity": "fail",
                "message": "fixture reviewer sealed a structural failure",
            }
        ]
    review_input = {
        "reviewer_kind": "vision",
        "verdict": verdict,
        "reviewed_by": "self_eval_fixture",
        "provider": "fixture",
        "model": "fixture",
        "capability": "fixture_structural_review.v1",
        "notes": "test-only authority fixture",
        "findings": findings,
    }
    intent = engine._vision_intent(project, current, review_input)
    attestation_ref = _mint_vision(project, authority.canonical_digest(intent))
    return engine.record_review(project, review_input, attestation_ref)


def _advance_fixture_identity(project: Path, ordinal: int) -> None:
    """Make the next fixture candidate distinct while keeping its marker current."""
    video = project / engine.CANDIDATE
    payload = video.read_bytes() + f"|self-eval-attempt-{ordinal}|".encode("ascii")
    video.write_bytes(payload)
    marker_path = project / engine.MARKER
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["video_sha256"] = hashlib.sha256(payload).hexdigest()
    marker["bytes"] = len(payload)
    marker_path.write_bytes(authority.canonical_bytes(marker))


def _prepare_failed_history(project: Path) -> None:
    """Fill missing ordinals 1-2 through genuine failed review transactions."""
    with engine.Snapshot(project) as snapshot:
        namespace, chain = engine._open_transaction(project, snapshot.root_fd)
        try:
            history = engine._history(chain)
            next_attempt = (max(history) if history else 0) + 1
            latest_same = bool(
                chain.latest is not None
                and chain.latest["attempt_identity"] == snapshot.identity
            )
            latest_status = (
                engine.TRANSITION_STATUS[chain.latest["transition"]]
                if chain.latest is not None
                else None
            )
        finally:
            namespace.close()
    if next_attempt > engine.MAX_ATTEMPTS:
        return
    if latest_same:
        if latest_status != engine.STATUS_FAIL:
            raise ValueError(
                "fixture cannot synthesize failed history over a non-failed state"
            )
        _advance_fixture_identity(project, next_attempt)
    for ordinal in range(next_attempt, engine.MAX_ATTEMPTS):
        seal(project, status=engine.STATUS_FAIL, attempt=ordinal)
        _advance_fixture_identity(project, ordinal + 1)


def seal(project, *, status="pass", attempt=1) -> dict:
    """Seal one schema-valid state through the genuine protected transaction.

    Identity inputs, lane presence, segment mode, and next ordinal are derived
    by the production engine. `attempt` is an assertion, not a way to choose or
    rewind an ordinal. A second call after a real input change lands on ordinal
    2; unchanged inputs return the byte-identical current state.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"unsupported fixture status: {status}")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or not 1 <= attempt <= 3:
        raise ValueError("attempt must be an integer from 1 through 3")
    project = engine._project_dir(project)
    if (
        status == engine.STATUS_HUMAN_INTERVENTION
        and attempt == engine.MAX_ATTEMPTS
    ):
        _prepare_failed_history(project)
    with engine.Snapshot(project) as snapshot:
        namespace, chain = engine._open_transaction(project, snapshot.root_fd)
        try:
            history = engine._history(chain)
            unchanged = bool(
                chain.latest is not None
                and chain.latest["attempt_identity"] == snapshot.identity
            )
            next_attempt = (max(history) if history else 0) + 1
        finally:
            namespace.close()
        if unchanged:
            current = engine.validate_current(project)
            if current["attempt"] != attempt or current["status"] != status:
                raise ValueError("unchanged fixture identity is already sealed differently")
            return current
        if next_attempt != attempt:
            raise ValueError(
                f"fixture requested attempt {attempt}, but next immutable ordinal is {next_attempt}"
            )
        _start_pending(project, snapshot, attempt)

    if status == engine.STATUS_NEEDS_HUMAN:
        return engine.validate_current(project)
    requested = status
    if status == engine.STATUS_HUMAN_INTERVENTION:
        if attempt != engine.MAX_ATTEMPTS:
            raise ValueError("human_intervention_required is only attempt 3")
        requested = engine.STATUS_FAIL
    result = _seal_review(project, requested)
    if result["status"] != status:
        raise ValueError(
            f"fixture sealed {result['status']!r}, expected requested {status!r}"
        )
    return result
