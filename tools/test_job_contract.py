import json
import datetime as dt
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("job_contract.py")


class JobContractTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        (self.repo / "jobs").mkdir()
        (self.repo / "input.txt").write_text("input\n")
        self.job_script = self.repo / "job.py"
        self.job_script.write_text(
            "import json, pathlib, sys\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "path.parent.mkdir(parents=True, exist_ok=True)\n"
            "path.write_text(json.dumps({'limits_ok': sys.argv[2] == 'true'}))\n"
        )
        self.notify_script = self.repo / "notify.py"
        self.notify_script.write_text(
            "import json, pathlib, sys\n"
            "counter = pathlib.Path(sys.argv[1])\n"
            "mode, target, message = sys.argv[2:]\n"
            "counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else '1')\n"
            "if mode == 'fail': raise SystemExit(1)\n"
            "if not pathlib.Path(message).is_file(): raise SystemExit(2)\n"
            "print(json.dumps({'message_id': 'msg-1', 'target': target}))\n"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def contract(self, *, gate=True, target="slack:#finance", primary="ok", fallback=False):
        counter = self.repo / "delivery-count"
        notification = {
            "target": target,
            "message_file": "runtime/report-{run_id}.json",
            "argv": [
                sys.executable,
                str(self.notify_script),
                str(counter),
                primary,
                "{target}",
                "{message_file}",
            ],
        }
        if fallback:
            notification["fallback"] = {
                "target": "slack:#backup",
                "argv": [
                    sys.executable,
                    str(self.notify_script),
                    str(counter),
                    "ok",
                    "{target}",
                    "{message_file}",
                ],
            }
        return {
            "format_version": 1,
            "id": "finance-report",
            "target_runtime": "any",
            "enabled": True,
            "schedule": {"max_age_seconds": 3600},
            "command": {
                "argv": [
                    sys.executable,
                    str(self.job_script),
                    "runtime/report-{run_id}.json",
                    "true" if gate else "false",
                ],
                "cwd": ".",
            },
            "inputs": ["input.txt"],
            "outputs": [{"path": "runtime/report-{run_id}.json", "min_bytes": 1}],
            "gates": [
                {
                    "kind": "json_equals",
                    "path": "runtime/report-{run_id}.json",
                    "field": "limits_ok",
                    "equals": True,
                }
            ],
            "notification": notification,
            "failure_notification": {
                "target": "slack:#ops",
                "argv": [
                    sys.executable,
                    str(self.notify_script),
                    str(self.repo / "failure-count"),
                    "ok",
                    "{target}",
                    "{message_file}",
                ],
            },
            "retry": {"max_attempts": 1},
        }

    def write_contract(self, contract):
        path = self.repo / "jobs/test.json"
        path.write_text(json.dumps(contract))
        return "jobs/test.json"

    def execute(self, *arguments, env=None):
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                *arguments,
                "--repo-root",
                str(self.repo),
            ],
            capture_output=True,
            text=True,
            env=env,
        )

    def result(self, completed):
        return json.loads(completed.stdout)

    def test_success_requires_gate_and_delivery_and_deduplicates_same_run(self):
        contract = self.write_contract(self.contract())

        first = self.execute("run", contract, "--run-id", "run-1")
        duplicate = self.execute("run", contract, "--run-id", "run-1")

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(duplicate.returncode, 0, duplicate.stdout + duplicate.stderr)
        self.assertEqual(self.result(first)["delivery"]["status"], "delivered")
        self.assertEqual(
            self.result(first)["finished_at"], self.result(duplicate)["finished_at"]
        )
        self.assertEqual((self.repo / "delivery-count").read_text(), "1")

        (self.repo / "input.txt").write_text("changed\n")
        conflict = self.execute("run", contract, "--run-id", "run-1")
        self.assertEqual(conflict.returncode, 2)
        self.assertEqual(self.result(conflict)["error"], "run_id_conflict")
        self.assertEqual((self.repo / "delivery-count").read_text(), "1")

    def test_same_run_rejects_changed_rendered_command_arguments(self):
        contract_value = self.contract()
        contract_value["command"]["argv"].append("{env:VIDEO_ID}")
        contract = self.write_contract(contract_value)

        first = self.execute(
            "run",
            contract,
            "--run-id",
            "bound",
            env={**os.environ, "VIDEO_ID": "AAAAAAAAAAA"},
        )
        conflict = self.execute(
            "run",
            contract,
            "--run-id",
            "bound",
            env={**os.environ, "VIDEO_ID": "BBBBBBBBBBB"},
        )

        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(conflict.returncode, 2)
        self.assertEqual(self.result(conflict)["error"], "run_id_conflict")
        self.assertEqual((self.repo / "delivery-count").read_text(), "1")

    def test_missing_target_fails_before_command(self):
        contract = self.write_contract(self.contract(target=""))

        completed = self.execute("run", contract, "--run-id", "run-2")

        self.assertEqual(completed.returncode, 3)
        self.assertEqual(self.result(completed)["error"], "notification_invalid")
        self.assertFalse((self.repo / "runtime/report-run-2.json").exists())

    def test_child_blocked_code_is_preserved_without_copying_stdout(self):
        self.job_script.write_text(
            "import json\n"
            "print(json.dumps({'status': 'blocked', 'code': 'youtube_auth_required'}))\n"
            "raise SystemExit(3)\n"
        )
        contract = self.write_contract(self.contract())

        completed = self.execute("run", contract, "--run-id", "blocked")
        receipt = self.result(completed)

        self.assertEqual(completed.returncode, 3)
        self.assertEqual(receipt["command"]["reported_status"], "blocked")
        self.assertEqual(
            receipt["command"]["reported_code"], "youtube_auth_required"
        )

    def test_unstructured_child_failure_gets_safe_machine_code(self):
        self.job_script.write_text(
            "import sys\n"
            "sys.stderr.write('private provider detail')\n"
            "raise SystemExit(1)\n"
        )
        contract = self.write_contract(self.contract())

        completed = self.execute("run", contract, "--run-id", "unstructured")
        receipt = self.result(completed)

        self.assertEqual(completed.returncode, 3)
        self.assertEqual(receipt["command"]["reported_status"], "error")
        self.assertEqual(
            receipt["command"]["reported_code"], "command_unstructured_failure"
        )
        self.assertNotIn("private provider detail", completed.stdout)

    def test_unstructured_execution_failures_get_safe_machine_code(self):
        cases = {
            "timeout": {
                "argv": [sys.executable, "-c", "import time; time.sleep(2)"],
                "timeout_seconds": 1,
            },
            "missing": {"argv": [str(self.repo / "missing-command")]},
        }
        for run_id, command in cases.items():
            with self.subTest(run_id=run_id):
                contract_value = self.contract()
                contract_value["command"] = command
                contract = self.write_contract(contract_value)

                completed = self.execute("run", contract, "--run-id", run_id)
                receipt = self.result(completed)

                self.assertEqual(completed.returncode, 3)
                self.assertEqual(receipt["command"]["reported_status"], "error")
                self.assertEqual(
                    receipt["command"]["reported_code"],
                    "command_unstructured_failure",
                )

    def test_failed_gate_alerts_once_for_repeated_run(self):
        contract = self.write_contract(self.contract(gate=False))

        first = self.execute("run", contract, "--run-id", "run-3")
        retry = self.execute("run", contract, "--run-id", "run-3")

        self.assertEqual(first.returncode, 3)
        self.assertEqual(retry.returncode, 3)
        self.assertEqual(self.result(first)["error"], "gate_failed")
        self.assertEqual((self.repo / "failure-count").read_text(), "1")

    def test_failed_delivery_uses_explicit_fallback_and_backfill_is_receipted(self):
        contract = self.write_contract(self.contract(primary="fail", fallback=True))

        completed = self.execute(
            "run",
            contract,
            "--run-id",
            "run-4",
            "--backfill",
            "run-original",
        )
        receipt = self.result(completed)

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(receipt["backfill_of"], "run-original")
        self.assertEqual(receipt["delivery"]["target"], "slack:#backup")
        self.assertEqual(len(receipt["delivery"]["attempts"]), 2)

    def test_watchdog_detects_output_mutation(self):
        contract = self.write_contract(self.contract())
        completed = self.execute("run", contract, "--run-id", "run-5")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        healthy = self.execute("watchdog", contract)
        self.assertEqual(healthy.returncode, 0, healthy.stderr)

        (self.repo / "runtime/report-run-5.json").write_text("{}")
        unhealthy = self.execute("watchdog", contract)

        self.assertEqual(unhealthy.returncode, 3)
        self.assertIn(
            "output_stale",
            [issue["code"] for issue in self.result(unhealthy)["issues"]],
        )

    def test_watchdog_alert_state_suppresses_until_recovery(self):
        contract = self.write_contract(self.contract())
        completed = self.execute("run", contract, "--run-id", "dedupe")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        receipt_path = next((self.repo / ".hvp/jobs/finance-report").glob("*.json"))
        receipt = json.loads(receipt_path.read_text())
        receipt["status"] = "failed"
        receipt_path.write_text(json.dumps(receipt))
        alert_state = self.repo / ".hvp/watchdog-alert-state.json"
        alert_state.write_text("[]")
        arguments = (
            "watchdog",
            contract,
            "--alert-state",
            ".hvp/watchdog-alert-state.json",
        )

        first = self.execute(*arguments)
        duplicate = self.execute(*arguments)
        receipt["status"] = "success"
        receipt_path.write_text(json.dumps(receipt))
        recovered = self.execute(*arguments)
        receipt["status"] = "failed"
        receipt_path.write_text(json.dumps(receipt))
        recurrence = self.execute(*arguments)

        self.assertEqual(first.returncode, 3)
        self.assertEqual(self.result(first)["alert_status"], "required")
        self.assertEqual(duplicate.returncode, 0)
        self.assertEqual(self.result(duplicate)["alert_status"], "suppressed")
        self.assertEqual(recovered.returncode, 0)
        self.assertEqual(self.result(recovered)["alert_status"], "clear")
        self.assertEqual(recurrence.returncode, 3)
        self.assertEqual(self.result(recurrence)["alert_status"], "required")

    def test_manifest_output_binds_nested_artifacts_for_dedup_and_watchdog(self):
        self.job_script.write_text(
            "import hashlib, json, pathlib, sys\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "path.parent.mkdir(parents=True, exist_ok=True)\n"
            "artifact = path.with_suffix('.artifact')\n"
            "artifact.write_text('stable')\n"
            "data = artifact.read_bytes()\n"
            "path.write_text(json.dumps({'limits_ok': True, 'artifacts': [{"
            "'path': str(artifact.resolve()), 'bytes': len(data), "
            "'sha256': 'sha256:' + hashlib.sha256(data).hexdigest()}]}))\n"
        )
        contract_value = self.contract()
        contract_value["outputs"][0]["manifest_digest_field"] = "artifacts"
        contract = self.write_contract(contract_value)

        completed = self.execute("run", contract, "--run-id", "manifest")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        (self.repo / "runtime/report-manifest.artifact").write_text("changed")

        duplicate = self.execute("run", contract, "--run-id", "manifest")
        unhealthy = self.execute("watchdog", contract)

        self.assertEqual(duplicate.returncode, 2)
        self.assertEqual(self.result(duplicate)["error"], "successful_output_stale")
        self.assertEqual(unhealthy.returncode, 3)
        self.assertIn(
            "output_stale",
            [issue["code"] for issue in self.result(unhealthy)["issues"]],
        )

    def test_watchdog_detects_a_stuck_running_receipt(self):
        contract_value = self.contract()
        contract_value["schedule"]["stuck_after_seconds"] = 1
        contract = self.write_contract(contract_value)
        completed = self.execute("run", contract, "--run-id", "run-6")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        receipt_path = next((self.repo / ".hvp/jobs/finance-report").glob("*.json"))
        receipt = json.loads(receipt_path.read_text())
        receipt["status"] = "running"
        receipt["finished_at"] = None
        receipt["started_at"] = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        ).isoformat()
        receipt_path.write_text(json.dumps(receipt))

        unhealthy = self.execute("watchdog", contract)

        self.assertEqual(unhealthy.returncode, 3)
        self.assertIn(
            "run_stuck",
            [issue["code"] for issue in self.result(unhealthy)["issues"]],
        )

    def test_watchdog_freshness_uses_artifact_completion_not_delivery_retry(self):
        contract_value = self.contract()
        contract_value["schedule"]["max_age_seconds"] = 1
        contract = self.write_contract(contract_value)
        completed = self.execute("run", contract, "--run-id", "artifact-age")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        receipt_path = next((self.repo / ".hvp/jobs/finance-report").glob("*.json"))
        receipt = json.loads(receipt_path.read_text())
        receipt["artifacts_completed_at"] = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
        ).isoformat()
        receipt_path.write_text(json.dumps(receipt))

        unhealthy = self.execute("watchdog", contract)

        self.assertEqual(unhealthy.returncode, 3)
        self.assertIn(
            "schedule_stale",
            [issue["code"] for issue in self.result(unhealthy)["issues"]],
        )

    def test_preexisting_unchanged_output_is_not_success(self):
        contract_value = self.contract()
        contract_value["command"]["argv"] = [sys.executable, "-c", "pass"]
        contract = self.write_contract(contract_value)
        output = self.repo / "runtime/report-stale.json"
        output.parent.mkdir()
        output.write_text('{"limits_ok": true}')

        completed = self.execute("run", contract, "--run-id", "stale")

        self.assertEqual(completed.returncode, 3)
        self.assertTrue(self.result(completed)["error"].startswith("output_stale:"))

    def test_indeterminate_delivery_is_not_resent(self):
        contract = self.write_contract(self.contract())
        completed = self.execute("run", contract, "--run-id", "crash")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        receipt_path = next((self.repo / ".hvp/jobs/finance-report").glob("*.json"))
        receipt = json.loads(receipt_path.read_text())
        receipt["status"] = "running"
        receipt["delivery"]["status"] = "in_flight"
        receipt_path.write_text(json.dumps(receipt))

        retry = self.execute("run", contract, "--run-id", "crash")

        self.assertEqual(retry.returncode, 3)
        self.assertEqual(self.result(retry)["error"], "delivery_indeterminate")
        self.assertEqual((self.repo / "delivery-count").read_text(), "1")

    def test_json_shape_gate_rejects_unstructured_output(self):
        contract_value = self.contract()
        contract_value["gates"] = [{
            "kind": "json_shape",
            "path": "runtime/report-{run_id}.json",
            "required": ["format_version", "candidates"],
            "arrays": {
                "candidates": {
                    "min_items": 3,
                    "max_items": 3,
                    "item_required": ["id", "hook"],
                }
            },
        }]
        contract = self.write_contract(contract_value)

        completed = self.execute("run", contract, "--run-id", "shape")

        self.assertEqual(completed.returncode, 3)
        self.assertEqual(self.result(completed)["error"], "gate_failed")

    def test_environment_values_are_rejected_in_artifact_paths(self):
        contract_value = self.contract()
        contract_value["outputs"][0]["path"] = "{env:TOP_SECRET}/report.json"
        contract = self.write_contract(contract_value)
        environment = {**os.environ, "TOP_SECRET": "must-not-leak"}

        completed = self.execute(
            "run", contract, "--run-id", "secret-path", env=environment
        )

        self.assertEqual(completed.returncode, 3)
        self.assertEqual(
            self.result(completed)["error"], "environment_not_allowed_in_path"
        )
        self.assertNotIn("must-not-leak", completed.stdout)

    def test_receipt_symlink_is_rejected(self):
        contract = self.write_contract(self.contract())
        outside = self.repo / "outside"
        outside.mkdir()
        (self.repo / ".hvp").symlink_to(outside, target_is_directory=True)

        completed = self.execute("run", contract, "--run-id", "symlink")

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(self.result(completed)["error"], "symlink_path_rejected")
        self.assertEqual(list(outside.iterdir()), [])

    def test_receipt_cannot_redirect_a_required_output(self):
        contract = self.write_contract(self.contract())
        completed = self.execute("run", contract, "--run-id", "redirect")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        receipt_path = next((self.repo / ".hvp/jobs/finance-report").glob("*.json"))
        receipt = json.loads(receipt_path.read_text())
        required_output = self.repo / "runtime/report-redirect.json"
        required_output.unlink()
        outside = self.repo.parent / f"{self.repo.name}-external.json"
        outside.write_text('{"limits_ok": true}')
        try:
            receipt["outputs"][0] = {
                "path": str(outside),
                "bytes": outside.stat().st_size,
                "sha256": "sha256:ignored",
                "mtime_ns": outside.stat().st_mtime_ns,
            }
            receipt_path.write_text(json.dumps(receipt))

            retry = self.execute("run", contract, "--run-id", "redirect")

            self.assertEqual(retry.returncode, 2)
            self.assertEqual(self.result(retry)["error"], "receipt_invalid")
            self.assertFalse(required_output.exists())
            self.assertEqual((self.repo / "delivery-count").read_text(), "1")
        finally:
            outside.unlink()

    def test_json_shape_rejects_null_topic_fields(self):
        self.job_script.write_text(
            "import json, pathlib, sys\n"
            "path = pathlib.Path(sys.argv[1])\n"
            "path.parent.mkdir(parents=True, exist_ok=True)\n"
            "candidate = {'id': None, 'hook': None, 'score': None}\n"
            "path.write_text(json.dumps({"
            "'decision_prompt': None, 'candidates': [candidate] * 3,"
            "'reject_bin': [{'id': None, 'reason': None}]}))\n"
        )
        contract_value = self.contract()
        contract_value["gates"] = [{
            "kind": "json_shape",
            "path": "runtime/report-{run_id}.json",
            "nonempty_strings": ["decision_prompt"],
            "arrays": {
                "candidates": {
                    "min_items": 3,
                    "max_items": 3,
                    "item_required": ["id", "hook", "score"],
                    "item_nonempty_strings": ["id", "hook"],
                    "item_number_ranges": {"score": [0, 12]},
                    "item_patterns": {"id": "^[a-z][a-z0-9-]{1,63}$"},
                    "unique_by": "id",
                },
                "reject_bin": {
                    "min_items": 1,
                    "item_required": ["id", "reason"],
                    "item_nonempty_strings": ["id", "reason"],
                },
            },
        }]
        contract = self.write_contract(contract_value)

        completed = self.execute("run", contract, "--run-id", "null-shape")

        self.assertEqual(completed.returncode, 3)
        self.assertEqual(self.result(completed)["error"], "gate_failed")

if __name__ == "__main__":
    unittest.main()
