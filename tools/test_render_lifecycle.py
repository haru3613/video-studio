import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import render_project
import template_trust


class RenderLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project = self.root / "project"
        self.tools = self.root / "tools"
        self.remotion = self.project / "remotion"
        (self.remotion / "node_modules/.bin").mkdir(parents=True)
        (self.project / "output").mkdir()
        (self.tools / "video").mkdir(parents=True)
        (self.remotion / "package.json").write_text("{}")
        (self.remotion / "package-lock.json").write_text("{}")
        (self.remotion / "remotion.config.ts").write_text("export default {};")
        (self.remotion / "src").mkdir()
        (self.remotion / "src/index.ts").write_text("export const fixture = true;")
        (self.remotion / "node_modules/.bin/remotion").write_text("binary")
        (self.tools / "video/render_and_verify.sh").write_text("renderer")
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "Episode",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "concurrency": 1,
                    "skip_pronunciation_gate": True,
                }
            )
        )
        template_trust.trust(self.project)

    def tearDown(self):
        self.directory.cleanup()

    @mock.patch.object(render_project.portable_jobs, "submit")
    def test_first_call_starts_portable_worker_and_later_call_resumes_from_marker(
        self, submit
    ):
        submit.return_value = (
            {
                "job_id": "job-1",
                "status": "running",
                "epoch": 1,
                "revision": "a" * 64,
                "pid": 123,
                "created_at": "2026-07-30T00:00:00+00:00",
            },
            True,
        )
        response, exit_code = render_project.run(self.project, self.tools)

        self.assertEqual(exit_code, 0)
        self.assertEqual(response["code"], "render_started")
        self.assertEqual(response["data"]["status"], "running")
        submit.assert_called_once()
        receipt = response["data"]
        self.assertEqual(receipt["schema"], "haru.render_job.v2")
        self.assertEqual(receipt["launcher"], "portable-python")

        video = self.project / "output/final.mp4"
        video.write_bytes(b"real final bytes")
        digest = hashlib.sha256(video.read_bytes()).hexdigest()
        marker = {
            "schema": "haru.render_result.v1",
            "status": "render_complete",
            "project": self.project.name,
            "output": "output/final.mp4",
            "video_sha256": digest,
            "bytes": video.stat().st_size,
            "duration_seconds": 1,
            "loudness_lufs": -14,
            "true_peak_dbfs": -1,
            "loudness_range_lu": 5,
            "mix": {
                "schema": "haru.final_mix.v1",
                "method": "ffmpeg_loudnorm_two_pass",
                "normalization_type": "linear",
                "input_sha256": "a" * 64,
                "target": {
                    "integrated_lufs": -14.0,
                    "true_peak_dbfs": -1.0,
                    "loudness_range_lu": 5,
                },
            },
        }
        (self.project / "output/final.mp4.render-result").write_text(
            json.dumps(marker)
        )

        resumed, resumed_exit = render_project.run(self.project, self.tools)

        self.assertEqual(resumed_exit, 0)
        self.assertEqual(resumed["code"], "render_complete")
        self.assertEqual(resumed["data"]["video_sha256"], digest)

    @mock.patch.object(render_project.portable_jobs, "get_job")
    def test_disappeared_portable_worker_without_marker_fails_closed(self, get_job):
        get_job.return_value = {
            "job_id": "job-1",
            "status": "interrupted",
            "epoch": 1,
            "revision": "a" * 64,
            "pid": 123,
            "created_at": "2026-07-30T00:00:00+00:00",
        }

        response, exit_code = render_project.run(self.project, self.tools)

        self.assertEqual(exit_code, 4)
        self.assertEqual(response["code"], "render_aborted")

    def test_cleanup_never_uses_project_receipt_to_remove_an_external_file(self):
        outside = self.root / "outside.plist"
        outside.write_text("keep")
        state = self.project / ".hvp"
        state.mkdir(exist_ok=True)
        (state / "render-job.json").write_text("{}")

        render_project.cleanup_job(
            self.project,
            {
                "label": "com.apple.Finder",
                "plist": str(outside),
            },
        )

        self.assertEqual(outside.read_text(), "keep")

    def test_cleanup_refuses_symlinked_render_jobs_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        state = self.project / ".hvp"
        state.mkdir(exist_ok=True)
        (state / "render-jobs").symlink_to(outside, target_is_directory=True)
        victim = outside / "victim"
        victim.write_text("keep")
        job = {"schema": "malformed"}

        render_project.cleanup_job(self.project.resolve(), job)

        self.assertEqual(victim.read_text(), "keep")

    def test_terminal_result_under_symlinked_output_is_never_accepted(self):
        (self.project / "output").rmdir()
        outside = self.root / "outside-output"
        outside.mkdir()
        (self.project / "output").symlink_to(outside, target_is_directory=True)
        video = outside / "final.mp4"
        video.write_bytes(b"external bytes")
        digest = hashlib.sha256(video.read_bytes()).hexdigest()
        (outside / "final.mp4.render-result").write_text(
            json.dumps(
                {
                    "schema": "haru.render_result.v1",
                    "status": "render_complete",
                    "project": self.project.name,
                    "output": "output/final.mp4",
                    "video_sha256": digest,
                    "bytes": video.stat().st_size,
                    "duration_seconds": 1,
                    "loudness_lufs": -14,
                    "true_peak_dbfs": -1,
                    "loudness_range_lu": 5,
                    "mix": {
                        "schema": "haru.final_mix.v1",
                        "method": "ffmpeg_loudnorm_two_pass",
                        "normalization_type": "linear",
                        "input_sha256": "a" * 64,
                        "target": {
                            "integrated_lufs": -14.0,
                            "true_peak_dbfs": -1.0,
                            "loudness_range_lu": 5,
                        },
                    },
                }
            )
        )

        response, exit_code = render_project.run(self.project, self.tools)

        self.assertNotEqual(exit_code, 0)
        self.assertNotEqual(response["code"], "render_complete")

    def test_cleanup_refuses_symlinked_state_directory(self):
        outside = self.root / "outside-state"
        outside.mkdir()
        victim = outside / "render-job.json"
        victim.write_text("keep")
        shutil.rmtree(self.project / ".hvp")
        (self.project / ".hvp").symlink_to(outside, target_is_directory=True)

        render_project.cleanup_job(self.project.resolve(), None)

        self.assertEqual(victim.read_text(), "keep")

    @mock.patch.object(render_project.portable_jobs, "submit")
    def test_invalid_plan_is_rejected_before_worker_submission(self, submit):
        plan = json.loads((self.project / "render_plan.json").read_text())
        plan["remotion_dir"] = "../outside"
        plan["composition"] = "../bad"
        (self.project / "render_plan.json").write_text(json.dumps(plan))

        with self.assertRaises(ValueError):
            render_project.run(self.project, self.tools)

        submit.assert_not_called()
        self.assertFalse((self.project / ".hvp/render-job.json").exists())

    def test_malformed_existing_receipts_are_rejected_not_restarted(self):
        marker = self.project / "output/final.mp4.render-result"
        marker.write_text("{broken")

        response, exit_code = render_project.run(self.project, self.tools)

        self.assertEqual(exit_code, 4)
        self.assertEqual(response["code"], "render_result_invalid")
        marker.write_text("{}")
        response, exit_code = render_project.run(self.project, self.tools)
        self.assertEqual((exit_code, response["code"]), (4, "render_result_invalid"))
        marker.unlink()
        state = self.project / ".hvp"
        state.mkdir(exist_ok=True)
        (state / "render-job.json").write_text("{broken")

        response, exit_code = render_project.run(self.project, self.tools)

        self.assertEqual(exit_code, 4)
        self.assertEqual(response["code"], "render_job_invalid")
        (state / "render-job.json").write_text("{}")
        response, exit_code = render_project.run(self.project, self.tools)
        self.assertEqual((exit_code, response["code"]), (4, "render_job_invalid"))


if __name__ == "__main__":
    unittest.main()


class SupersededRenderTest(unittest.TestCase):
    """A re-take must not need a hand-cleared output directory.

    A complete, coherent render of a version of the project that no longer
    exists is the normal aftermath of re-taking the narration. Telling it apart
    from a malformed marker is what lets the runner do the only sane thing --
    render again -- while still stopping on the unexplained case.
    """

    def _project(self, root):
        project = Path(root) / "demo"
        (project / "output").mkdir(parents=True)
        (project / ".hvp").mkdir(parents=True)
        video = project / "output/final.mp4"
        video.write_bytes(b"a real render")
        narration = project / "narration-final.mp3"
        narration.write_bytes(b"the take it was rendered from")
        marker = {
            "schema": "haru.render_result.v1",
            "status": "render_complete",
            "project": "demo",
            "output": "output/final.mp4",
            "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
            "bytes": video.stat().st_size,
            "narration_sha256": hashlib.sha256(narration.read_bytes()).hexdigest(),
            "duration_seconds": 10.0,
            "loudness_lufs": -14.0,
            "true_peak_dbfs": -2.0,
            "loudness_range_lu": 3.2,
            "mix": {
                "schema": "haru.final_mix.v1",
                "method": "ffmpeg_loudnorm_two_pass",
                "normalization_type": "dynamic",
                "input_sha256": "a" * 64,
                "target": {"integrated_lufs": -14.0, "true_peak_dbfs": -1.0,
                           "loudness_range_lu": 3.8},
            },
        }
        (project / "output/final.mp4.render-result").write_text(
            json.dumps(marker), encoding="utf-8")
        # The previous run's byproducts, which the worker also refuses to start
        # on top of.
        (project / "output/final.mp4.render.log").write_text("previous log", encoding="utf-8")
        (project / "output/final.pre-loudnorm.mp4").write_bytes(b"premix")
        return project, narration, marker

    def test_a_superseded_render_is_preserved_until_replacement_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, narration, marker = self._project(tmp)
            narration.write_bytes(b"the take approved after it")

            with mock.patch.object(render_project, "start_job",
                                   return_value={"label": "job"}) as start:
                payload, code = render_project.run(str(project), str(project))

            self.assertEqual(code, 0)
            self.assertEqual(payload["code"], "render_started")
            self.assertEqual(start.call_count, 1)
            stamp = marker["narration_sha256"][:12]
            # A failed retry must not destroy the only playable version.  The
            # supervisor snapshots around it and promotion retires it only
            # after the new candidate passes the real worker gates.
            self.assertTrue(payload["data"]["previous_final_preserved"])
            self.assertTrue((project / "output/final.mp4").is_file())
            self.assertTrue((project / "output/final.mp4.render-result").is_file())
            self.assertFalse(
                (project / f"output/final.mp4.superseded-{stamp}").exists()
            )

    def test_a_current_render_is_still_reported_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            project, _, _ = self._project(tmp)
            with mock.patch.object(render_project, "start_job") as start:
                payload, code = render_project.run(str(project), str(project))
            self.assertEqual(code, 0)
            self.assertEqual(payload["code"], "render_complete")
            start.assert_not_called()

    def test_a_malformed_marker_still_stops_rather_than_restarting(self):
        # Unexplained is not the same as superseded; only one of them is a
        # reason to overwrite a render.
        with tempfile.TemporaryDirectory() as tmp:
            project, _, marker = self._project(tmp)
            del marker["mix"]
            (project / "output/final.mp4.render-result").write_text(
                json.dumps(marker), encoding="utf-8")

            with mock.patch.object(render_project, "start_job") as start:
                payload, code = render_project.run(str(project), str(project))
            self.assertEqual(payload["code"], "render_result_invalid")
            self.assertNotEqual(code, 0)
            start.assert_not_called()
            self.assertTrue((project / "output/final.mp4").is_file())
