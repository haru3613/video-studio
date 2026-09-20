#!/usr/bin/env python3
"""Run and monitor portable, receipt-based scheduled jobs."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
TOKEN = re.compile(r"\{env:([A-Z][A-Z0-9_]*)\}")


class JobError(Exception):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def file_digest(path: Path, *, include_mtime: bool = False) -> dict[str, Any]:
    data = path.read_bytes()
    digest = {
        "path": str(path),
        "bytes": len(data),
        "sha256": sha256(data),
    }
    if include_mtime:
        digest["mtime_ns"] = path.stat().st_mtime_ns
    return digest


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        temporary = Path(tmp.name)
    os.replace(temporary, path)


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode() + b"\n")


def render(value: str, context: dict[str, str]) -> str:
    for key, replacement in context.items():
        value = value.replace(f"{{{key}}}", replacement)

    def environment(match: re.Match[str]) -> str:
        name = match.group(1)
        if not os.environ.get(name):
            raise JobError(f"missing_environment:{name}")
        return os.environ[name]

    return TOKEN.sub(environment, value)


def repo_path(repo: Path, value: str, context: dict[str, str]) -> Path:
    if TOKEN.search(value):
        raise JobError("environment_not_allowed_in_path")
    unresolved = repo / render(value, context)
    try:
        relative = unresolved.relative_to(repo)
    except ValueError as error:
        raise JobError("path_outside_repo") from error
    current = repo
    for part in relative.parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise JobError("symlink_path_rejected")
    path = unresolved.resolve(strict=False)
    try:
        path.relative_to(repo)
    except ValueError as error:
        raise JobError("path_outside_repo") from error
    return path


def load_contract(repo: Path, value: str) -> tuple[dict[str, Any], Path, str]:
    path = repo_path(repo, value, {})
    if path.is_symlink() or not path.is_file():
        raise JobError("contract_missing")
    raw = path.read_bytes()
    try:
        contract = json.loads(raw)
    except json.JSONDecodeError as error:
        raise JobError("contract_invalid_json") from error
    if not isinstance(contract, dict):
        raise JobError("contract_invalid")
    command = contract.get("command")
    schedule = contract.get("schedule")
    outputs = contract.get("outputs")
    if (
        contract.get("format_version") != 1
        or not ID.fullmatch(str(contract.get("id", "")))
        or not isinstance(contract.get("target_runtime"), str)
        or not contract["target_runtime"].strip()
        or not isinstance(contract.get("enabled"), bool)
        or not isinstance(schedule, dict)
        or not isinstance(schedule.get("max_age_seconds"), int)
        or schedule["max_age_seconds"] < 1
        or not isinstance(command, dict)
        or not isinstance(command.get("argv"), list)
        or not command["argv"]
        or not all(isinstance(item, str) and item for item in command["argv"])
        or not isinstance(contract.get("inputs"), list)
        or not isinstance(outputs, list)
        or not outputs
        or not all(
            isinstance(output, dict)
            and isinstance(output.get("path"), str)
            and output["path"]
            and isinstance(output.get("min_bytes", 1), int)
            and output.get("min_bytes", 1) >= 1
            and (
                output.get("manifest_digest_field") is None
                or (
                    isinstance(output["manifest_digest_field"], str)
                    and bool(output["manifest_digest_field"].strip())
                )
            )
            for output in outputs
        )
        or not isinstance(contract.get("gates", []), list)
        or not all(isinstance(gate, dict) for gate in contract.get("gates", []))
    ):
        raise JobError("contract_invalid")
    return contract, path, sha256(raw)


def check_notification(
    notification: dict[str, Any] | None, *, require_message_file: bool = False
) -> None:
    if notification is None:
        return
    if (
        not isinstance(notification, dict)
        or not isinstance(notification.get("target"), str)
        or not notification["target"].strip()
        or not isinstance(notification.get("argv"), list)
        or not notification["argv"]
        or not all(isinstance(item, str) and item for item in notification["argv"])
        or (
            require_message_file
            and (
                not isinstance(notification.get("message_file"), str)
                or not notification["message_file"]
            )
        )
    ):
        raise JobError("notification_invalid")


def input_digests(repo: Path, contract: dict[str, Any], context: dict[str, str]) -> list[dict[str, Any]]:
    inputs = []
    for value in contract["inputs"]:
        if not isinstance(value, str):
            raise JobError("contract_invalid")
        path = repo_path(repo, value, context)
        if path.is_symlink() or not path.is_file():
            raise JobError(f"input_missing:{value}")
        inputs.append(file_digest(path))
    return inputs


def wip_gate(repo: Path, contract: dict[str, Any], context: dict[str, str]) -> dict[str, Any] | None:
    definition = contract.get("wip")
    if definition is None:
        return None
    if not isinstance(definition, dict):
        raise JobError("wip_state_invalid")
    try:
        path = repo_path(repo, definition["path"], context)
        state = json.loads(path.read_text())
        active = state[definition["field"]]
        limit = int(definition["max_active"])
    except (JobError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise JobError("wip_state_invalid") from error
    if not isinstance(active, list) or limit < 1:
        raise JobError("wip_state_invalid")
    result = {"kind": "wip_below", "active": len(active), "max_active": limit}
    if len(active) >= limit:
        raise JobError("wip_limit_reached")
    return result


def command_argv(
    repo: Path, contract: dict[str, Any], context: dict[str, str]
) -> list[str]:
    command = contract["command"]
    prompt = ""
    if command.get("prompt_path"):
        prompt_path = repo_path(repo, command["prompt_path"], context)
        if prompt_path.is_symlink() or not prompt_path.is_file():
            raise JobError("prompt_missing")
        prompt = render(prompt_path.read_text(), context)
    return [
        prompt if item == "{prompt}" else render(item, context)
        for item in command["argv"]
    ]


def run_command(
    repo: Path, contract: dict[str, Any], context: dict[str, str]
) -> tuple[dict[str, Any], bytes]:
    command = contract["command"]
    argv = command_argv(repo, contract, context)
    cwd = repo_path(repo, command.get("cwd", "."), context)
    if not cwd.is_dir() or cwd.is_symlink():
        raise JobError("command_cwd_invalid")
    try:
        timeout = int(command.get("timeout_seconds", 3600))
        if timeout < 1:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise JobError("command_timeout_invalid") from error
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        return {
            "status": "failed",
            "exit_code": None,
            "timed_out": True,
            "reported_status": "error",
            "reported_code": "command_unstructured_failure",
            "argv_digest": sha256(b"\0".join(item.encode() for item in argv)),
            "stdout_sha256": sha256(stdout),
            "stderr_sha256": sha256(stderr),
        }, stdout
    except OSError:
        return {
            "status": "failed",
            "exit_code": None,
            "timed_out": False,
            "reported_status": "error",
            "reported_code": "command_unstructured_failure",
            "argv_digest": sha256(b"\0".join(item.encode() for item in argv)),
            "stdout_sha256": sha256(b""),
            "stderr_sha256": sha256(b""),
        }, b""
    result = {
        "status": "ok" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "argv_digest": sha256(b"\0".join(item.encode() for item in argv)),
        "stdout_sha256": sha256(completed.stdout),
        "stderr_sha256": sha256(completed.stderr),
    }
    try:
        reported = json.loads(completed.stdout)
        if (
            isinstance(reported, dict)
            and reported.get("status") in {"ok", "blocked", "error"}
            and ID.fullmatch(str(reported.get("code", "")))
        ):
            result["reported_status"] = reported["status"]
            result["reported_code"] = reported["code"]
    except json.JSONDecodeError:
        pass
    if result["status"] == "failed" and "reported_status" not in result:
        result["reported_status"] = "error"
        result["reported_code"] = "command_unstructured_failure"
    capture = command.get("capture_stdout")
    if completed.returncode == 0 and capture:
        if not completed.stdout.strip():
            raise JobError("command_output_empty")
        atomic_write(repo_path(repo, capture, context), completed.stdout)
    return result, completed.stdout


def manifest_digests_match(repo: Path, path: Path, field: str) -> bool:
    try:
        value: Any = json.loads(path.read_text())
        for part in field.split("."):
            value = value[part]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise JobError("output_manifest_invalid") from error
    if not isinstance(value, list):
        raise JobError("output_manifest_invalid")
    seen = set()
    for expected in value:
        if (
            not isinstance(expected, dict)
            or set(expected) != {"path", "bytes", "sha256"}
            or not isinstance(expected["path"], str)
            or not isinstance(expected["bytes"], int)
            or expected["bytes"] < 0
            or not isinstance(expected["sha256"], str)
            or expected["path"] in seen
        ):
            raise JobError("output_manifest_invalid")
        seen.add(expected["path"])
        artifact = repo_path(repo, expected["path"], {})
        if artifact.is_symlink() or not artifact.is_file():
            return False
        if file_digest(artifact) != expected:
            return False
    return True


def validate_outputs(
    repo: Path,
    contract: dict[str, Any],
    context: dict[str, str],
    before: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    outputs = []
    for definition in contract["outputs"]:
        path = repo_path(repo, definition["path"], context)
        if path.is_symlink() or not path.is_file():
            raise JobError(f"output_missing:{definition['path']}")
        digest = file_digest(path, include_mtime=True)
        if digest["bytes"] < definition.get("min_bytes", 1):
            raise JobError(f"output_empty:{definition['path']}")
        if before.get(str(path)) == digest:
            raise JobError(f"output_stale:{definition['path']}")
        manifest_field = definition.get("manifest_digest_field")
        if manifest_field and not manifest_digests_match(repo, path, manifest_field):
            raise JobError(f"output_manifest_stale:{definition['path']}")
        outputs.append(digest)
    return outputs


def output_snapshot(
    repo: Path, contract: dict[str, Any], context: dict[str, str]
) -> dict[str, dict[str, Any]]:
    snapshots = {}
    for definition in contract["outputs"]:
        path = repo_path(repo, definition["path"], context)
        if path.is_file() and not path.is_symlink():
            snapshots[str(path)] = file_digest(path, include_mtime=True)
    return snapshots


def evaluate_gates(
    repo: Path, contract: dict[str, Any], context: dict[str, str]
) -> list[dict[str, Any]]:
    results = []
    for gate in contract.get("gates", []):
        if not isinstance(gate.get("path"), str):
            raise JobError("gate_input_invalid")
        kind = gate.get("kind")
        path = repo_path(repo, gate.get("path", ""), context)
        if kind == "file_nonempty":
            passed = path.is_file() and path.stat().st_size > 0
            actual: Any = path.stat().st_size if passed else None
        elif kind == "json_equals":
            try:
                actual = json.loads(path.read_text())
                for part in gate["field"].split("."):
                    actual = actual[part]
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise JobError("gate_input_invalid") from error
            expected = gate.get("equals")
            if isinstance(expected, str):
                expected = render(expected, context)
            passed = actual == expected
        elif kind == "json_shape":
            try:
                actual = json.loads(path.read_text())
                required = gate.get("required", [])
                arrays = gate.get("arrays", {})
                equals = gate.get("equals", {})
                passed = isinstance(actual, dict) and all(key in actual for key in required)
                passed = passed and all(
                    isinstance(actual.get(key), str) and bool(actual[key].strip())
                    for key in gate.get("nonempty_strings", [])
                )
                for key, expected in equals.items():
                    if isinstance(expected, str):
                        expected = render(expected, context)
                    passed = passed and actual.get(key) == expected
                for key, shape in arrays.items():
                    value = actual.get(key)
                    passed = (
                        passed
                        and isinstance(value, list)
                        and len(value) >= shape.get("min_items", 0)
                        and len(value) <= shape.get("max_items", len(value))
                        and all(
                            isinstance(item, dict)
                            and all(field in item for field in shape.get("item_required", []))
                            and all(
                                isinstance(item.get(field), str)
                                and bool(item[field].strip())
                                for field in shape.get("item_nonempty_strings", [])
                            )
                            and all(
                                isinstance(item.get(field), (int, float))
                                and not isinstance(item[field], bool)
                                and bounds[0] <= item[field] <= bounds[1]
                                for field, bounds in shape.get("item_number_ranges", {}).items()
                            )
                            and all(
                                isinstance(item.get(field), str)
                                and re.fullmatch(pattern, item[field]) is not None
                                for field, pattern in shape.get("item_patterns", {}).items()
                            )
                            for item in value
                        )
                    )
                    unique_by = shape.get("unique_by")
                    if unique_by and isinstance(value, list):
                        values = [item.get(unique_by) for item in value if isinstance(item, dict)]
                        passed = passed and len(values) == len(set(values))
            except (OSError, IndexError, TypeError, json.JSONDecodeError, re.error) as error:
                raise JobError("gate_input_invalid") from error
        else:
            raise JobError("gate_kind_invalid")
        results.append({"kind": kind, "path": str(path), "passed": passed, "actual": actual})
        if not passed:
            raise JobError("gate_failed")
    return results


def delivery_attempt(
    notification: dict[str, Any],
    message_file: Path,
    context: dict[str, str],
    kind: str,
) -> dict[str, Any]:
    target = notification["target"]
    argv = [
        render(item, {**context, "target": target, "message_file": str(message_file)})
        for item in notification["argv"]
    ]
    try:
        timeout = int(notification.get("timeout_seconds", 30))
        if timeout < 1:
            raise ValueError
    except (TypeError, ValueError):
        return {
            "kind": kind,
            "target": target,
            "delivered": False,
            "message_id": None,
            "exit_code": None,
            "error": "notification_timeout_invalid",
            "attempted_at": now(),
        }
    try:
        completed = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "kind": kind,
            "target": target,
            "delivered": False,
            "message_id": None,
            "exit_code": None,
            "attempted_at": now(),
        }
    message_id = None
    if completed.returncode == 0:
        try:
            message_id = json.loads(completed.stdout).get("message_id")
        except (AttributeError, json.JSONDecodeError):
            pass
    delivered = completed.returncode == 0 and bool(message_id)
    return {
        "kind": kind,
        "target": target,
        "delivered": delivered,
        "message_id": message_id,
        "exit_code": completed.returncode,
        "attempted_at": now(),
    }


def deliver(
    notification: dict[str, Any],
    message_file: Path,
    context: dict[str, str],
    kind: str,
    max_attempts: int,
    event_key: str,
    persist: Any,
) -> dict[str, Any]:
    attempts = []
    for _ in range(max_attempts):
        delivery_key = sha256(
            f"{event_key}\0{kind}\0{notification['target']}\0{len(attempts)}".encode()
        )
        attempts.append(
            {
                "kind": kind,
                "target": notification["target"],
                "status": "in_flight",
                "delivery_key": delivery_key,
                "attempted_at": now(),
            }
        )
        persist(
            {
                "status": "in_flight",
                "target": notification["target"],
                "attempts": attempts,
            }
        )
        attempt = delivery_attempt(notification, message_file, context, kind)
        attempt["delivery_key"] = delivery_key
        attempts[-1] = attempt
        if attempt["delivered"]:
            result = {"status": "delivered", "target": attempt["target"], "attempts": attempts}
            persist(result)
            return result
        persist({"status": "failed", "target": attempt["target"], "attempts": attempts})
    fallback = notification.get("fallback")
    if fallback:
        if not isinstance(fallback, dict):
            raise JobError("notification_invalid")
        check_notification(fallback)
        delivery_key = sha256(
            f"{event_key}\0{kind}\0{fallback['target']}\0{len(attempts)}".encode()
        )
        attempts.append(
            {
                "kind": kind,
                "target": fallback["target"],
                "status": "in_flight",
                "delivery_key": delivery_key,
                "attempted_at": now(),
            }
        )
        persist(
            {
                "status": "in_flight",
                "target": fallback["target"],
                "attempts": attempts,
            }
        )
        attempt = delivery_attempt(fallback, message_file, context, kind)
        attempt["delivery_key"] = delivery_key
        attempts[-1] = attempt
        if attempt["delivered"]:
            result = {"status": "delivered", "target": attempt["target"], "attempts": attempts}
            persist(result)
            return result
    result = {"status": "failed", "target": notification["target"], "attempts": attempts}
    persist(result)
    return result


def outputs_match(
    repo: Path,
    contract: dict[str, Any],
    context: dict[str, str],
    outputs: list[dict[str, Any]],
) -> bool:
    if not isinstance(outputs, list) or len(outputs) != len(contract["outputs"]):
        raise JobError("receipt_invalid")
    expected = {
        str(repo_path(repo, definition["path"], context))
        for definition in contract["outputs"]
    }
    if {output.get("path") for output in outputs if isinstance(output, dict)} != expected:
        raise JobError("receipt_invalid")
    for output in outputs:
        path = Path(output["path"])
        if path.is_symlink():
            raise JobError("receipt_invalid")
        if not path.is_file() or file_digest(
            path, include_mtime="mtime_ns" in output
        ) != output:
            return False
        definition = next(
            item
            for item in contract["outputs"]
            if str(repo_path(repo, item["path"], context)) == output["path"]
        )
        manifest_field = definition.get("manifest_digest_field")
        if manifest_field and not manifest_digests_match(repo, path, manifest_field):
            return False
    return True


def run_job(repo: Path, contract_value: str, run_id: str, backfill: str | None) -> int:
    contract, _, contract_digest = load_contract(repo, contract_value)
    if not contract["enabled"]:
        raise JobError("job_disabled")
    if not RUN_ID.fullmatch(run_id):
        raise JobError("run_id_invalid")
    if backfill and not RUN_ID.fullmatch(backfill):
        raise JobError("backfill_id_invalid")
    context = {"run_id": run_id}
    if backfill:
        context["backfill"] = backfill
    job_id = contract["id"]
    event_key = hashlib.sha256(f"{job_id}\0{run_id}\0{backfill or ''}".encode()).hexdigest()
    receipt_dir = repo_path(repo, f".hvp/jobs/{job_id}", context)
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = receipt_dir / f"{event_key}.json"
    lock = (receipt_dir / ".lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        existing = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
    except json.JSONDecodeError as error:
        raise JobError("receipt_invalid") from error
    input_error = None
    try:
        current_inputs = input_digests(repo, contract, context)
    except JobError as error:
        current_inputs = []
        input_error = error
    if existing and input_error:
        raise JobError("run_id_conflict")
    if existing and (
        existing.get("contract_digest") != contract_digest
        or existing.get("inputs") != current_inputs
    ):
        raise JobError("run_id_conflict")
    if existing and existing.get("command") is not None:
        argv_digest = sha256(
            b"\0".join(item.encode() for item in command_argv(repo, contract, context))
        )
        if existing["command"].get("argv_digest") != argv_digest:
            raise JobError("run_id_conflict")
    existing_delivery = (existing or {}).get("delivery") or {}
    if (
        existing
        and existing.get("command") is None
        and (
            existing.get("status") == "success"
            or existing_delivery.get("status") == "delivered"
        )
    ):
        raise JobError("receipt_invalid")
    if existing_delivery.get("status") == "in_flight":
        blocked = {**existing, "error": "delivery_indeterminate"}
        print(json.dumps(blocked, ensure_ascii=False))
        return 3
    existing_outputs_ok = (
        outputs_match(repo, contract, context, existing.get("outputs", []))
        if existing and existing.get("outputs")
        else False
    )
    if existing and existing.get("status") == "success":
        if not existing_outputs_ok:
            raise JobError("successful_output_stale")
        print(json.dumps(existing, ensure_ascii=False))
        return 0
    if existing and existing_delivery.get("status") == "delivered":
        if not existing_outputs_ok:
            raise JobError("successful_output_stale")
        existing["status"] = "success"
        existing["finished_at"] = (
            existing.get("finished_at")
            or existing.get("artifacts_completed_at")
            or existing["started_at"]
        )
        atomic_json(receipt_path, existing)
        print(json.dumps(existing, ensure_ascii=False))
        return 0

    started = now()
    receipt: dict[str, Any] = {
        "format_version": 1,
        "job_id": job_id,
        "run_id": run_id,
        "event_key": event_key,
        "backfill_of": backfill,
        "contract_digest": contract_digest,
        "started_at": started,
        "finished_at": None,
        "artifacts_completed_at": None,
        "status": "running",
        "command": None,
        "inputs": current_inputs,
        "outputs": [],
        "gates": [],
        "delivery": None,
        "failure_alert": existing.get("failure_alert") if existing else None,
    }
    atomic_json(receipt_path, receipt)
    failure_candidate = contract.get("failure_notification")
    failure = None
    try:
        check_notification(failure_candidate)
        failure = failure_candidate
        check_notification(contract.get("notification"), require_message_file=True)
        if input_error:
            raise input_error
        wip = wip_gate(repo, contract, context)
        if wip:
            receipt["gates"].append(wip)
        if (
            existing
            and existing.get("error") == "delivery_failed"
            and (existing.get("command") or {}).get("status") == "ok"
            and outputs_match(repo, contract, context, existing.get("outputs", []))
        ):
            receipt["command"] = existing["command"]
            receipt["outputs"] = existing["outputs"]
            receipt["gates"] = existing["gates"]
        else:
            before = output_snapshot(repo, contract, context)
            receipt["command"], _ = run_command(repo, contract, context)
            if receipt["command"]["status"] != "ok":
                raise JobError("command_failed")
            receipt["outputs"] = validate_outputs(repo, contract, context, before)
            receipt["gates"].extend(evaluate_gates(repo, contract, context))
        receipt["artifacts_completed_at"] = (
            (existing or {}).get("artifacts_completed_at") or now()
        )
        atomic_json(receipt_path, receipt)
        def persist_delivery(value: dict[str, Any]) -> None:
            receipt["delivery"] = value
            atomic_json(receipt_path, receipt)

        notification = contract.get("notification")
        if notification:
            message_file = repo_path(repo, notification["message_file"], context)
            try:
                max_attempts = int(contract.get("retry", {}).get("max_attempts", 1))
                if max_attempts < 1:
                    raise ValueError
            except (AttributeError, TypeError, ValueError) as error:
                raise JobError("retry_invalid") from error
            receipt["delivery"] = deliver(
                notification,
                message_file,
                context,
                "success",
                max_attempts,
                event_key,
                persist_delivery,
            )
            if receipt["delivery"]["status"] != "delivered":
                raise JobError("delivery_failed")
        receipt["status"] = "success"
        exit_code = 0
    except JobError as error:
        receipt["status"] = "failed"
        receipt["error"] = str(error)
        already_alerted = (
            receipt["failure_alert"]
            and receipt["failure_alert"].get("status") in {"delivered", "in_flight"}
        )
        if failure and not already_alerted:
            message = receipt_dir / f"{event_key}.failure.txt"
            atomic_write(message, f"{job_id} {run_id} failed: {error}\n".encode())
            def persist_failure(value: dict[str, Any]) -> None:
                receipt["failure_alert"] = value
                atomic_json(receipt_path, receipt)

            receipt["failure_alert"] = deliver(
                failure,
                message,
                context,
                "failure",
                1,
                event_key,
                persist_failure,
            )
        exit_code = 3
    receipt["finished_at"] = now()
    atomic_json(receipt_path, receipt)
    print(json.dumps(receipt, ensure_ascii=False))
    return exit_code


def watchdog(
    repo: Path, contract_values: list[str], alert_state_value: str | None = None
) -> int:
    checked_at = dt.datetime.now(dt.timezone.utc)
    issues = []
    jobs = []
    for value in contract_values:
        contract, _, _ = load_contract(repo, value)
        if not contract["enabled"]:
            continue
        job_id = contract["id"]
        try:
            check_notification(contract.get("notification"), require_message_file=True)
            definition = contract.get("wip")
            if definition:
                state = json.loads(repo_path(repo, definition["path"], {}).read_text())
                if len(state[definition["field"]]) >= int(definition["max_active"]):
                    issues.append({"job_id": job_id, "code": "wip_limit_reached"})
            receipt_dir = repo_path(repo, f".hvp/jobs/{job_id}", {})
            receipts = list(receipt_dir.glob("*.json"))
            if any(path.is_symlink() for path in receipts):
                raise JobError("symlink_path_rejected")
            if not receipts:
                issues.append({"job_id": job_id, "code": "receipt_missing"})
                continue
            latest = max(
                (json.loads(path.read_text()) for path in receipts),
                key=lambda receipt: receipt.get("finished_at") or receipt["started_at"],
            )
            jobs.append({"job_id": job_id, "last_status": latest.get("status")})
            status = latest.get("status")
            if status == "running":
                issues.append({"job_id": job_id, "code": "last_run_running"})
                started = dt.datetime.fromisoformat(latest["started_at"])
                stuck_after = int(
                    contract["schedule"].get(
                        "stuck_after_seconds",
                        contract["schedule"]["max_age_seconds"],
                    )
                )
                if (checked_at - started).total_seconds() > stuck_after:
                    issues.append({"job_id": job_id, "code": "run_stuck"})
            elif status != "success":
                issues.append({"job_id": job_id, "code": "last_run_failed"})
            if (
                status == "success"
                and contract.get("notification")
                and (latest.get("delivery") or {}).get("status") != "delivered"
            ):
                issues.append({"job_id": job_id, "code": "delivery_missing"})
            receipt_context = {"run_id": latest["run_id"]}
            if latest.get("backfill_of"):
                receipt_context["backfill"] = latest["backfill_of"]
            if status == "success" and not outputs_match(
                repo, contract, receipt_context, latest.get("outputs", [])
            ):
                issues.append({"job_id": job_id, "code": "output_stale"})
            event_time = dt.datetime.fromisoformat(
                latest.get("artifacts_completed_at")
                or latest.get("finished_at")
                or latest["started_at"]
            )
            max_age = int(contract["schedule"]["max_age_seconds"])
            if (checked_at - event_time).total_seconds() > max_age:
                issues.append({"job_id": job_id, "code": "schedule_stale"})
        except (JobError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            issues.append({"job_id": job_id, "code": "watchdog_input_invalid"})
    result = {
        "format_version": 1,
        "status": "healthy" if not issues else "unhealthy",
        "checked_at": checked_at.isoformat(),
        "jobs": jobs,
        "issues": issues,
    }
    if alert_state_value:
        alert_state_path = repo_path(repo, alert_state_value, {})
        try:
            previous = json.loads(alert_state_path.read_text())
        except (OSError, json.JSONDecodeError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        issue_digest = sha256(
            json.dumps(issues, sort_keys=True, separators=(",", ":")).encode()
        )
        if issues:
            result["alert_status"] = (
                "suppressed"
                if previous.get("status") == "unhealthy"
                and previous.get("issue_digest") == issue_digest
                else "required"
            )
        else:
            result["alert_status"] = "clear"
        atomic_json(
            alert_state_path,
            {
                "format_version": 1,
                "status": result["status"],
                "issue_digest": issue_digest if issues else None,
            },
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not issues or result.get("alert_status") == "suppressed" else 3


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    subcommands = root.add_subparsers(dest="action", required=True)
    run = subcommands.add_parser("run")
    run.add_argument("contract")
    run.add_argument("--run-id", required=True)
    run.add_argument("--backfill")
    run.add_argument("--repo-root", type=Path)
    watch = subcommands.add_parser("watchdog")
    watch.add_argument("contracts", nargs="+")
    watch.add_argument("--alert-state")
    watch.add_argument("--repo-root", type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    repo = (arguments.repo_root or Path(__file__).resolve().parent.parent).resolve()
    try:
        if arguments.action == "run":
            return run_job(repo, arguments.contract, arguments.run_id, arguments.backfill)
        return watchdog(repo, arguments.contracts, arguments.alert_state)
    except JobError as error:
        print(json.dumps({"format_version": 1, "status": "error", "error": str(error)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
