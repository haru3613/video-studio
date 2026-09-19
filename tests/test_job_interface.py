import json
import os
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import job_interface
import portable_jobs
import render_project
import template_trust


ROOT = Path(__file__).resolve().parents[1]


class MissingJobReadTest(unittest.TestCase):
    def test_read_of_missing_job_does_not_create_project_state(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            response, code = job_interface.status(project, "0" * 32)
            self.assertEqual((code, response["code"]), (2, "job_not_found"))
            self.assertFalse((project / ".hvp").exists())


class JobInterfaceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project = self.root / "project"
        self.tools = self.root / "tools"
        remotion = self.project / "remotion"
        (remotion / "node_modules/.bin").mkdir(parents=True)
        (self.project / "output").mkdir()
        (self.tools / "video").mkdir(parents=True)
        (remotion / "package.json").write_text("{}", encoding="utf-8")
        (remotion / "package-lock.json").write_text("{}", encoding="utf-8")
        (remotion / "remotion.config.ts").write_text(
            "export default {};", encoding="utf-8"
        )
        (remotion / "src").mkdir()
        (remotion / "src/index.ts").write_text(
            "export const fixture = true;", encoding="utf-8"
        )
        binary = remotion / "node_modules/.bin/remotion"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        renderer = self.tools / "video/render_and_verify.sh"
        renderer.write_text("#!/bin/sh\nsleep 30\nexit 9\n", encoding="utf-8")
        renderer.chmod(renderer.stat().st_mode | stat.S_IXUSR)
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "JobInterfaceTest",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "concurrency": 1,
                    "skip_pronunciation_gate": True,
                }
            ),
            encoding="utf-8",
        )
        template_trust.trust(self.project)
        response, code = render_project.run(self.project, self.tools)
        self.assertEqual(code, 0, response)
        self.job_id = response["data"]["job_id"]
        self.wait_for({"running"})

    def tearDown(self):
        try:
            job = portable_jobs.get_job(self.project, self.job_id)
            if job and job["status"] in portable_jobs.ACTIVE:
                portable_jobs.cancel(self.project, self.job_id)
            elif job and portable_jobs._same_process(job):
                os.killpg(job["process_group"], signal.SIGTERM)
        except (OSError, ValueError):
            pass
        self.directory.cleanup()

    def wait_for(self, states, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = portable_jobs.get_job(self.project, self.job_id)
            if job and job["status"] in states:
                return job
            time.sleep(0.03)
        self.fail(f"job did not reach {states}: {job}")

    def test_status_is_typed_and_hides_process_and_filesystem_authority(self):
        response, code = job_interface.status(self.project, self.job_id)
        self.assertEqual((code, response["code"]), (0, "job_status"))
        data = response["data"]
        self.assertEqual(data["job_id"], self.job_id)
        self.assertEqual(data["status"], "running")
        for private in (
            "pid",
            "pid_token",
            "process_group",
            "worker",
            "tools_root",
            "snapshot_root",
            "candidate_root",
        ):
            self.assertNotIn(private, data)

    def test_logs_are_bounded_tailed_and_redacted(self):
        path = self.project / "output/.staging" / self.job_id / "worker.log"
        with path.open("ab") as handle:
            handle.write(b"x" * 70000)
            handle.write(b"\nOPENAI_API_KEY=sk-testsecrettoken123456\n")
            handle.write(b"Authorization: Bearer abc.def.ghi\n")
            handle.write(b"https://user:password@example.test/path\n")

        response, code = job_interface.logs(self.project, self.job_id, 1024)
        self.assertEqual((code, response["code"]), (0, "job_logs"))
        data = response["data"]
        self.assertTrue(data["truncated"])
        self.assertTrue(data["redacted"])
        self.assertLessEqual(data["returned_bytes"], 1024)
        self.assertNotIn("testsecrettoken", data["text"])
        self.assertNotIn("abc.def.ghi", data["text"])
        self.assertNotIn("user:password", data["text"])

    def test_invalid_job_id_cannot_cancel_an_owned_worker(self):
        with self.assertRaises(ValueError):
            job_interface.cancel(self.project, "../" + self.job_id)
        job = portable_jobs.get_job(self.project, self.job_id)
        self.assertEqual(job["status"], "running")
        self.assertTrue(portable_jobs._same_process(job))

    def test_status_projects_a_dead_worker_without_mutating_the_database(self):
        job = portable_jobs.get_job(self.project, self.job_id, refresh=False)
        os.killpg(job["process_group"], signal.SIGKILL)
        portable_jobs._CHILDREN[job["pid"]].wait(timeout=5)

        response, code = job_interface.status(self.project, self.job_id)
        self.assertEqual((code, response["code"]), (0, "job_status"))
        self.assertEqual(response["data"]["status"], "interrupted")
        connection = portable_jobs._connect(self.project)
        try:
            stored = connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (self.job_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(stored, "running")

    def test_cancel_is_idempotent_and_resume_checks_the_configured_tools_root(self):
        response, code = job_interface.cancel(self.project, self.job_id)
        self.assertEqual((code, response["code"]), (0, "job_cancelled"))
        repeated, repeated_code = job_interface.cancel(self.project, self.job_id)
        self.assertEqual((repeated_code, repeated["code"]), (0, "job_cancelled"))

        other_tools = self.root / "other-tools"
        other_tools.mkdir()
        blocked, blocked_code = job_interface.resume(
            self.project, self.job_id, other_tools
        )
        self.assertEqual((blocked_code, blocked["code"]), (3, "job_tools_mismatch"))
        resumed, resumed_code = job_interface.resume(
            self.project, self.job_id, self.tools
        )
        self.assertEqual((resumed_code, resumed["code"]), (0, "job_resumed"))
        self.assertEqual(resumed["data"]["epoch"], 2)
        self.wait_for({"running"})

    def test_fixed_wrapper_rejects_unknown_actions_and_returns_json(self):
        invalid = subprocess.run(
            [ROOT / "scripts/render-job", "exec", str(self.project), self.job_id],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(json.loads(invalid.stdout)["code"], "invalid_input")

        status = subprocess.run(
            [ROOT / "scripts/render-job", "status", str(self.project), self.job_id],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(status.returncode, 0, status)
        self.assertEqual(json.loads(status.stdout)["code"], "job_status")


if __name__ == "__main__":
    unittest.main()
