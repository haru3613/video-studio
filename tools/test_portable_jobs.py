import json
import hashlib
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import portable_jobs
import render_project
import template_trust


class LinuxProcessTokenTest(unittest.TestCase):
    def test_proc_stat_parser_preserves_start_time_and_reports_state(self):
        prefix = "4321 (render worker (take 2))"
        fields_four_through_twenty_one = [str(value) for value in range(4, 22)]
        running = " ".join(
            [prefix, "S", *fields_four_through_twenty_one, "55534", "0"]
        )
        zombie = running.replace(f"{prefix} S ", f"{prefix} Z ", 1)

        self.assertEqual(
            portable_jobs._parse_linux_proc_stat(running), ("S", "55534")
        )
        self.assertEqual(
            portable_jobs._parse_linux_proc_stat(zombie), ("Z", "55534")
        )
        self.assertIsNone(portable_jobs._parse_linux_proc_stat("4321 malformed"))


class PortableRenderJobsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project = self.root / "project"
        self.tools = self.root / "tools"
        self.remotion = self.project / "remotion"
        (self.remotion / "node_modules/.bin").mkdir(parents=True)
        (self.project / "output").mkdir()
        (self.tools / "video").mkdir(parents=True)
        (self.remotion / "package.json").write_text("{}", encoding="utf-8")
        (self.remotion / "package-lock.json").write_text("{}", encoding="utf-8")
        (self.remotion / "remotion.config.ts").write_text(
            "export default {};", encoding="utf-8"
        )
        (self.remotion / "src").mkdir()
        (self.remotion / "src/index.ts").write_text(
            "export const fixture = true;", encoding="utf-8"
        )
        remotion = self.remotion / "node_modules/.bin/remotion"
        remotion.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        remotion.chmod(remotion.stat().st_mode | stat.S_IXUSR)
        self.renderer = self.tools / "video/render_and_verify.sh"
        self.renderer.write_text("#!/bin/sh\nsleep 30\nexit 9\n", encoding="utf-8")
        self.renderer.chmod(self.renderer.stat().st_mode | stat.S_IXUSR)
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "PortableJobTest",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "concurrency": 1,
                    "skip_pronunciation_gate": True,
                }
            ),
            encoding="utf-8",
        )
        template_trust.trust(self.project)
        self.jobs = []

    def tearDown(self):
        for job_id in self.jobs:
            try:
                job = portable_jobs.get_job(self.project, job_id)
                if job and job["status"] in portable_jobs.ACTIVE:
                    portable_jobs.cancel(self.project, job_id)
                elif job and portable_jobs._same_process(job):
                    os.killpg(job["process_group"], signal.SIGTERM)
            except (OSError, ValueError):
                pass
        self.directory.cleanup()

    def start(self):
        response, code = render_project.run(self.project, self.tools)
        self.assertEqual(code, 0, response)
        self.assertIn(response["code"], {"render_started", "render_running"})
        job_id = response["data"]["job_id"]
        if job_id not in self.jobs:
            self.jobs.append(job_id)
        return portable_jobs.get_job(self.project, job_id), response

    def wait_for(self, job_id, statuses, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = portable_jobs.get_job(self.project, job_id)
            if job and job["status"] in statuses:
                return job
            time.sleep(0.03)
        self.fail(f"job {job_id} did not reach {statuses}: {job}")

    def install_real_renderer(self):
        self.renderer.write_text(
            """#!/bin/sh
case "$1" in
  --verify-only|--loudness-gate-only) exit 0 ;;
esac
ffmpeg -y -hide_banner -loglevel error \\
  -f lavfi -i color=c=black:s=160x90:r=30:d=1 \\
  -f lavfi -i sine=frequency=440:sample_rate=48000:duration=1 \\
  -filter:a volume=0.02 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$3"
""",
            encoding="utf-8",
        )
        self.renderer.chmod(self.renderer.stat().st_mode | stat.S_IXUSR)

    def bind_narration(self, payload):
        narration = self.project / "narration-final.mp3"
        narration.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        (self.project / "narration-final.mp3.pron-ok.json").write_text(
            json.dumps({"sha256": digest, "warnings": []}), encoding="utf-8"
        )
        (self.project / "narration.txt").write_text("approved words", encoding="utf-8")
        static = self.remotion / "public/narration-final.mp3"
        static.parent.mkdir(exist_ok=True)
        static.write_bytes(payload)
        plan = json.loads((self.project / "render_plan.json").read_text())
        plan.pop("skip_pronunciation_gate", None)
        plan["narration"] = "narration-final.mp3"
        plan["narration_text"] = "narration.txt"
        (self.project / "render_plan.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )

    def test_client_exit_does_not_cancel_and_duplicate_start_is_idempotent(self):
        caller = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json,render_project,sys; "
                    "value,code=render_project.run(sys.argv[1],sys.argv[2]); "
                    "print(json.dumps(value)); raise SystemExit(code)"
                ),
                str(self.project),
                str(self.tools),
            ],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(caller.returncode, 0, caller)
        started = json.loads(caller.stdout)
        job_id = started["data"]["job_id"]
        self.jobs.append(job_id)

        job = self.wait_for(job_id, {"running"})
        self.assertTrue(
            Path(job["snapshot_root"])
            .resolve()
            .is_relative_to((self.project / "output/.staging").resolve())
        )
        again, code = render_project.run(self.project, self.tools)
        self.assertEqual(code, 0)
        self.assertEqual(again["code"], "render_running")
        self.assertEqual(again["data"]["job_id"], job_id)

    def test_cancel_records_intent_and_only_terminates_owned_process_group(self):
        job, _response = self.start()
        job = self.wait_for(job["job_id"], {"running"})
        unrelated = subprocess.Popen(
            ["/bin/sh", "-c", "sleep 30"], start_new_session=True
        )
        try:
            cancelled = portable_jobs.cancel(self.project, job["job_id"])
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertIsNone(unrelated.poll())
            self.assertFalse((self.project / "output/final.mp4").exists())
        finally:
            os.killpg(unrelated.pid, signal.SIGTERM)
            unrelated.wait(timeout=5)

    def test_crashed_worker_becomes_interrupted_and_resume_fences_old_epoch(self):
        job, _response = self.start()
        job = self.wait_for(job["job_id"], {"running"})
        old_epoch = job["epoch"]
        os.killpg(job["process_group"], signal.SIGKILL)
        interrupted = self.wait_for(job["job_id"], {"interrupted"})
        self.assertEqual(interrupted["epoch"], old_epoch)

        resumed = portable_jobs.resume(self.project, job["job_id"])
        resumed = self.wait_for(job["job_id"], {"running"})
        self.assertEqual(resumed["epoch"], old_epoch + 1)
        self.assertNotEqual(resumed["pid_token"], job["pid_token"])
        self.assertFalse(
            portable_jobs.promote_candidate(self.project, job["job_id"], old_epoch)
        )
        self.assertEqual(
            portable_jobs.get_job(self.project, job["job_id"])["status"], "running"
        )

    def test_project_change_blocks_stale_candidate_before_canonical_output(self):
        job, _response = self.start()
        job = self.wait_for(job["job_id"], {"running"})
        (self.project / "new-input.txt").write_text("revision two", encoding="utf-8")

        self.assertFalse(
            portable_jobs.promote_candidate(self.project, job["job_id"], job["epoch"])
        )
        fenced = portable_jobs.get_job(self.project, job["job_id"])
        self.assertEqual(fenced["status"], "interrupted")
        self.assertEqual(fenced["error_code"], "project_revision_changed")
        self.assertFalse((self.project / "output/final.mp4").exists())

    def test_generated_webpack_cache_is_not_a_mutated_render_input(self):
        self.install_real_renderer()
        source = self.renderer.read_text()
        source = source.replace(
            "ffmpeg -y -hide_banner",
            'test ! -e "$1/node_modules/.cache/untrusted-seed" || exit 45\n'
            'mkdir -p "$1/node_modules/.cache/webpack"\n'
            'printf generated > "$1/node_modules/.cache/webpack/index.pack"\n'
            "ffmpeg -y -hide_banner",
        )
        self.renderer.write_text(source)
        cache = self.remotion / "node_modules/.cache"
        cache.mkdir()
        (cache / "untrusted-seed").write_text(
            "do not reuse a project-supplied compiler cache"
        )
        job, _response = self.start()
        terminal = self.wait_for(
            job["job_id"], {"succeeded", "failed", "interrupted"}, timeout=15
        )
        self.assertEqual(terminal["status"], "succeeded", terminal.get("error_code"))
        self.assertTrue((self.project / "output/final.mp4").is_file())
        self.assertTrue((cache / "untrusted-seed").is_file())

    def test_visual_content_change_starts_a_new_render_instead_of_reusing_final(self):
        self.install_real_renderer()
        content = self.remotion / "src/content.json"
        content.write_text('{"title":"first"}', encoding="utf-8")
        first, _response = self.start()
        first = self.wait_for(
            first["job_id"], {"succeeded", "failed", "interrupted"}, timeout=15
        )
        self.assertEqual(first["status"], "succeeded", first.get("error_code"))
        marker_path = self.project / "output/final.mp4.render-result"
        first_marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(first_marker["render_input_revision"], first["revision"])

        reused, code = render_project.run(self.project, self.tools)
        self.assertEqual((code, reused["code"]), (0, "render_complete"))

        content.write_text('{"title":"changed"}', encoding="utf-8")
        restarted, code = render_project.run(self.project, self.tools)
        self.assertEqual((code, restarted["code"]), (0, "render_started"))
        self.assertTrue(restarted["data"]["previous_final_preserved"])
        second_id = restarted["data"]["job_id"]
        self.jobs.append(second_id)
        second = self.wait_for(
            second_id, {"succeeded", "failed", "interrupted"}, timeout=15
        )
        self.assertEqual(second["status"], "succeeded", second.get("error_code"))
        second_marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(second_marker["render_input_revision"], second["revision"])
        self.assertNotEqual(
            second_marker["render_input_revision"],
            first_marker["render_input_revision"],
        )

    def test_source_change_during_file_promotion_never_commits_success(self):
        job_id = "f" * 32
        revision = portable_jobs.project_revision(self.project)
        snapshot, snapshot_digest = portable_jobs._snapshot(self.project, job_id, 1)
        self.assertEqual(snapshot_digest, revision)
        candidate = snapshot / "output/final.mp4"
        candidate.write_bytes(b"candidate final")
        marker = {
            "schema": "haru.render_result.v1",
            "status": "render_complete",
            "render_input_revision": revision,
            "project": self.project.name,
            "output": "output/final.mp4",
            "video_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
            "bytes": candidate.stat().st_size,
            "duration_seconds": 1,
            "loudness_lufs": -14,
            "true_peak_dbfs": -1,
            "loudness_range_lu": 2,
            "mix": {
                "schema": "haru.final_mix.v1",
                "method": "ffmpeg_loudnorm_two_pass",
                "normalization_type": "linear",
                "input_sha256": "a" * 64,
                "target": {
                    "integrated_lufs": -14.0,
                    "true_peak_dbfs": -1.0,
                    "loudness_range_lu": 2,
                },
            },
        }
        Path(str(candidate) + ".render-result").write_text(
            json.dumps(marker), encoding="utf-8"
        )
        canonical = self.project / "output/final.mp4"
        canonical.write_bytes(b"previous final")
        previous_marker = dict(marker)
        previous_marker["video_sha256"] = hashlib.sha256(
            canonical.read_bytes()
        ).hexdigest()
        previous_marker["bytes"] = canonical.stat().st_size
        canonical_marker = Path(str(canonical) + ".render-result")
        canonical_marker.write_text(json.dumps(previous_marker), encoding="utf-8")
        connection = portable_jobs._connect(self.project)
        try:
            connection.execute(
                """INSERT INTO jobs
                   (job_id,kind,project,tools_root,worker,status,epoch,revision,
                    snapshot_digest,snapshot_root,candidate_root,created_at,updated_at)
                   VALUES (?,'render',?,?,?,'running',1,?,?,?,?,?,?)""",
                (
                    job_id,
                    str(self.project),
                    str(self.tools),
                    str(Path(portable_jobs.__file__).parent / "render_project_worker.py"),
                    revision,
                    snapshot_digest,
                    str(snapshot),
                    str(snapshot / "output"),
                    "2026-09-20T00:00:00+00:00",
                    "2026-09-20T00:00:00+00:00",
                ),
            )
        finally:
            connection.close()
        original_promote = portable_jobs._promote_files

        def mutate_after_move(*arguments):
            original_promote(*arguments)
            (self.remotion / "src/index.ts").write_text(
                "export const fixture = 'changed during promotion';",
                encoding="utf-8",
            )

        with mock.patch.object(
            portable_jobs, "_promote_files", side_effect=mutate_after_move
        ):
            self.assertFalse(portable_jobs.promote_candidate(self.project, job_id, 1))

        state = portable_jobs.get_job(self.project, job_id, refresh=False)
        self.assertEqual(state["status"], "interrupted")
        self.assertEqual(state["error_code"], "project_revision_changed")
        self.assertEqual(canonical.read_bytes(), b"previous final")
        restored_marker = json.loads(canonical_marker.read_text(encoding="utf-8"))
        self.assertEqual(restored_marker["video_sha256"], previous_marker["video_sha256"])
        self.assertFalse(
            render_project.render_contract.valid_final_result(
                self.project, restored_marker, canonical
            ),
            "restored prior bytes must not be presented as current after source change",
        )

    def test_interrupted_promotion_restores_the_previous_bytes(self):
        job, _response = self.start()
        job = self.wait_for(job["job_id"], {"running"})
        os.killpg(job["process_group"], signal.SIGKILL)
        portable_jobs._CHILDREN[job["pid"]].wait(timeout=5)

        output = self.project / "output/final.mp4"
        marker = self.project / "output/final.mp4.render-result"
        output.write_bytes(b"previous diagnostic bytes")
        marker.write_text('{"previous":true}\n', encoding="utf-8")
        backup_video = self.project / "output/final.mp4.superseded-recovery"
        backup_marker = (
            self.project / "output/final.mp4.render-result.superseded-recovery"
        )
        os.link(output, backup_video)
        backup_marker.write_bytes(marker.read_bytes())
        partial = self.project / "output/partial-candidate.mp4"
        partial.write_bytes(b"partial candidate bytes")
        os.replace(partial, output)
        portable_jobs._atomic_json(
            self.project / "output/.staging" / job["job_id"] / "promotion.json",
            {
                "schema": "video-studio.pending_promotion.v1",
                "job_id": job["job_id"],
                "epoch": job["epoch"],
                "candidate_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "backups": {
                    "final.mp4": str(backup_video),
                    "final.mp4.render-result": str(backup_marker),
                },
            },
        )
        connection = portable_jobs._connect(self.project)
        try:
            connection.execute(
                "UPDATE jobs SET status='promoting' WHERE job_id=?",
                (job["job_id"],),
            )
        finally:
            connection.close()

        recovered = portable_jobs.get_job(self.project, job["job_id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(output.read_bytes(), b"previous diagnostic bytes")
        self.assertEqual(marker.read_text(), '{"previous":true}\n')

    def test_custom_browser_is_forwarded_without_provider_secrets(self):
        browser = self.root / "custom-chromium"
        browser.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        browser.chmod(browser.stat().st_mode | stat.S_IXUSR)
        self.renderer.write_text(
            """#!/bin/sh
if [ "${OPENAI_API_KEY+x}" = x ] || [ "${PROVIDER_SECRET_SENTINEL+x}" = x ]; then
  exit 91
fi
printf '%s' "$VIDEO_STUDIO_CHROMIUM" > "$3.browser"
exit 9
""",
            encoding="utf-8",
        )
        self.renderer.chmod(self.renderer.stat().st_mode | stat.S_IXUSR)
        with mock.patch.dict(
            os.environ,
            {
                "VIDEO_STUDIO_CHROMIUM": str(browser),
                "OPENAI_API_KEY": "provider-secret-must-not-cross",
                "PROVIDER_SECRET_SENTINEL": "also-must-not-cross",
            },
            clear=False,
        ):
            job, _response = self.start()
        failed = self.wait_for(job["job_id"], {"failed"})
        observed = (
            Path(failed["snapshot_root"]) / "output/final.pre-loudnorm.mp4.browser"
        )
        self.assertEqual(observed.read_text(), str(browser.resolve()))

    def test_real_render_promotes_once_and_failed_retake_keeps_previous_final(self):
        self.install_real_renderer()
        self.bind_narration(b"approved take one")
        first, _response = self.start()
        first = self.wait_for(first["job_id"], {"succeeded"}, timeout=20)
        final = self.project / "output/final.mp4"
        marker = self.project / "output/final.mp4.render-result"
        self.assertTrue(final.is_file())
        self.assertTrue(marker.is_file())
        original_digest = hashlib.sha256(final.read_bytes()).hexdigest()
        self.assertEqual(
            json.loads(marker.read_text())["video_sha256"], original_digest
        )
        process = portable_jobs._CHILDREN.get(first["pid"])
        if process is not None:
            process.wait(timeout=5)
        connection = portable_jobs._connect(self.project)
        try:
            connection.execute(
                "UPDATE jobs SET status='promoting' WHERE job_id=?",
                (first["job_id"],),
            )
        finally:
            connection.close()
        recovered = portable_jobs.get_job(self.project, first["job_id"])
        self.assertEqual(recovered["status"], "succeeded")

        self.bind_narration(b"approved take two")
        self.renderer.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
        self.renderer.chmod(self.renderer.stat().st_mode | stat.S_IXUSR)
        response, code = render_project.run(self.project, self.tools)
        self.assertEqual(code, 0, response)
        retry_id = response["data"]["job_id"]
        self.jobs.append(retry_id)
        self.assertEqual(
            hashlib.sha256(final.read_bytes()).hexdigest(), original_digest
        )
        self.wait_for(retry_id, {"failed"}, timeout=10)
        self.assertEqual(
            hashlib.sha256(final.read_bytes()).hexdigest(), original_digest
        )
        self.assertEqual(
            json.loads(marker.read_text())["video_sha256"], original_digest
        )

        self.install_real_renderer()
        resumed = portable_jobs.resume(self.project, retry_id)
        self.assertEqual(resumed["epoch"], 2)
        self.wait_for(retry_id, {"succeeded"}, timeout=20)
        promoted = json.loads(marker.read_text())
        take_two_digest = hashlib.sha256(b"approved take two").hexdigest()
        self.assertEqual(promoted["narration_sha256"], take_two_digest)
        old_stamp = hashlib.sha256(b"approved take one").hexdigest()[:12]
        self.assertTrue(
            (self.project / f"output/final.mp4.superseded-{old_stamp}").is_file()
        )

    def test_revised_inputs_after_failed_retake_start_a_new_job_and_keep_final(self):
        """A failed snapshot stays diagnostic evidence; a revised take is new work."""
        self.install_real_renderer()
        self.bind_narration(b"approved take one")
        first, _response = self.start()
        first = self.wait_for(first["job_id"], {"succeeded"}, timeout=20)
        final = self.project / "output/final.mp4"
        original_digest = hashlib.sha256(final.read_bytes()).hexdigest()

        self.bind_narration(b"take two fails")
        self.renderer.write_text("#!/bin/sh\necho renderer failure >&2\nexit 9\n")
        self.renderer.chmod(self.renderer.stat().st_mode | stat.S_IXUSR)
        failed, _response = self.start()
        failed = self.wait_for(failed["job_id"], {"failed"}, timeout=10)
        self.assertEqual(hashlib.sha256(final.read_bytes()).hexdigest(), original_digest)

        self.bind_narration(b"take three also fails")
        retried, response = self.start()
        self.assertNotEqual(retried["job_id"], failed["job_id"])
        self.assertEqual(response["code"], "render_started")
        self.assertEqual(portable_jobs.get_job(self.project, failed["job_id"])["status"], "failed")
        self.assertFalse(
            portable_jobs.promote_candidate(self.project, failed["job_id"], failed["epoch"])
        )
        retried = self.wait_for(retried["job_id"], {"failed"}, timeout=10)
        self.assertEqual(retried["status"], "failed")
        self.assertEqual(hashlib.sha256(final.read_bytes()).hexdigest(), original_digest)


if __name__ == "__main__":
    unittest.main()
