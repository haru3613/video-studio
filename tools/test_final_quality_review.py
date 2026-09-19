#!/usr/bin/env python3
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from runtime_test_fixture import RuntimePolicyCase
from pathlib import Path
from unittest import mock

import agent_status
import final_quality_authority
import final_quality_review
import self_eval_fixture
from test_agent_status import make_ready_project, write_visual_qa_receipts


FFMPEG_AVAILABLE = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@unittest.skipUnless(FFMPEG_AVAILABLE, "ffmpeg and ffprobe are required")
class FinalQualityReviewTest(RuntimePolicyCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.saved_environment = {
            name: os.environ.get(name)
            for name in (
                "HARU_VIDEO_STUDIO_SELF_EVAL_STATE_ROOT",
                "HARU_VIDEO_STUDIO_SELF_EVAL_ATTESTATION_ROOT",
            )
        }

    def tearDown(self):
        for name, value in self.saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.temporary.cleanup()

    def project(
        self,
        source="testsrc2=size=64x36:rate=5:duration=60",
        slug="demo",
        duration=60,
    ):
        project = make_ready_project(self.root, slug=slug)
        video = project / "output/final.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                source,
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=8000:duration={duration}",
                "-c:v",
                "mpeg4",
                "-q:v",
                "8",
                "-c:a",
                "aac",
                "-shortest",
                str(video),
            ],
            check=True,
        )
        marker_path = Path(str(video) + ".render-result")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["video_sha256"] = hashlib.sha256(video.read_bytes()).hexdigest()
        marker["bytes"] = video.stat().st_size
        marker["duration_seconds"] = duration
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        self_eval_fixture.seal(project, status="pass", attempt=2)
        write_visual_qa_receipts(project, video)
        return project

    def seed_fake_current_receipts(self, project):
        _video, _marker, _visual, inputs = final_quality_review.current_inputs(project)
        video_sha = inputs["final_video"]["sha256"]
        prep = {
            "schema": final_quality_review.PREP_SCHEMA,
            "project": project.name,
            "created_at": "2026-01-01T00:00:00+00:00",
            "video": str(final_quality_review.VIDEO_PATH),
            "video_sha256": video_sha,
            "inputs": inputs,
            "metadata": {"duration": 60},
            "audio": {"has_audio": True, "clipping": False, "too_quiet": False},
            "mechanical_evidence": {
                "decode": {"full_decode_clean": True},
                "black": {"intervals": []},
                "static": {"intervals": []},
            },
        }
        review = {
            "schema": final_quality_review.REVIEW_SCHEMA,
            "project": project.name,
            "reviewed_at": "2026-01-01T00:00:00+00:00",
            "video": str(final_quality_review.VIDEO_PATH),
            "video_sha256": video_sha,
            "inputs": inputs,
            "publish_readiness": "ship",
            "critical_issues": [],
            "warnings": [],
            "checks": [
                {"name": name, "status": "pass", "evidence": {}}
                for name in final_quality_review.REQUIRED_CHECKS
            ],
        }
        (project / final_quality_review.PREP_PATH).write_text(json.dumps(prep))
        (project / final_quality_review.REVIEW_PATH).write_text(json.dumps(review))

    def test_produces_real_mechanical_evidence_and_bound_receipts(self):
        project = self.project()

        value = final_quality_review.produce(project)

        self.assertEqual(value["schema"], "haru.final_quality_review.v1")
        self.assertEqual(value["status"], "complete")
        self.assertFalse(value["reused"])
        prep = json.loads((project / final_quality_review.PREP_PATH).read_text())
        review = json.loads((project / final_quality_review.REVIEW_PATH).read_text())
        self.assertEqual(prep["video_sha256"], value["video_sha256"])
        self.assertEqual(prep["metadata"]["duration"], 60)
        self.assertTrue(prep["audio"]["has_audio"])
        self.assertFalse(prep["audio"]["clipping"])
        self.assertFalse(prep["audio"]["too_quiet"])
        self.assertTrue(prep["mechanical_evidence"]["decode"]["full_decode_clean"])
        self.assertEqual(prep["mechanical_evidence"]["black"]["intervals"], [])
        self.assertEqual(prep["mechanical_evidence"]["static"]["intervals"], [])
        self.assertEqual(
            {item["name"] for item in review["checks"]},
            set(final_quality_review.REQUIRED_CHECKS),
        )
        visual = next(
            item for item in review["checks"] if item["name"] == "visual_spot_check"
        )
        self.assertEqual(visual["evidence"]["reviewed_by"], "fixture")
        self.assertEqual(review["inputs"], value["inputs"])
        self.assertTrue(
            final_quality_authority.validate(
                project, value["inputs"], value["artifacts"]
            )
        )
        status, _artifacts = agent_status.build(project, self.root)
        self.assertEqual(status["stages"]["qa"]["status"], "pass")
        self.assertEqual(status["stages"]["duration"]["status"], "pass")
        self.assertEqual(status["stages"]["loudness"]["status"], "pass")
        for name, reference in value["artifacts"].items():
            path = project / reference["path"]
            self.assertEqual(name, path.stem)
            self.assertEqual(
                reference["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )

    def test_repeat_still_executes_mechanical_checks(self):
        project = self.project()
        first = final_quality_review.produce(project)
        real_checks = final_quality_review.media_checks
        calls = []

        def checked_again(ffmpeg, video):
            calls.append(video)
            return real_checks(ffmpeg, video)

        with mock.patch.object(final_quality_review, "media_checks", checked_again):
            second = final_quality_review.produce(project)

        self.assertEqual(calls, [project / "output/final.mp4"])
        self.assertFalse(second["reused"])
        self.assertEqual(first["video_sha256"], second["video_sha256"])

    def test_fabricated_current_receipts_cannot_bypass_checks(self):
        project = self.project()
        self.seed_fake_current_receipts(project)
        prep = project / final_quality_review.PREP_PATH
        previous = prep.read_bytes()

        with mock.patch.object(
            final_quality_review,
            "media_checks",
            side_effect=final_quality_review.QualityReviewError(
                "deterministic_check_failed", "mechanical evidence is unavailable"
            ),
        ):
            with self.assertRaises(final_quality_review.QualityReviewError) as raised:
                final_quality_review.produce(project)

        self.assertEqual(raised.exception.code, "deterministic_check_failed")
        self.assertEqual(prep.read_bytes(), previous)

    def test_new_bound_v1_supersedes_old_v2_hold_but_not_newer_hold(self):
        project = self.project()
        video = project / "output/final.mp4"
        video_sha = hashlib.sha256(video.read_bytes()).hexdigest()
        hold_dir = project / "quality-review/final-v2"
        hold_dir.mkdir(parents=True)
        hold_path = hold_dir / "review.json"
        hold = {
            "schema": "haru.quality_review.v1",
            "project": project.name,
            "video": "output/final.mp4",
            "video_sha256": video_sha,
            "publish_readiness": "hold",
            "critical_issues": ["human hold"],
            "warnings": [],
            "checks": [],
        }
        hold_path.write_text(json.dumps(hold), encoding="utf-8")
        os.utime(hold_path, (1, 1))

        final_quality_review.produce(project)
        status, _artifacts = agent_status.build(project, self.root)

        self.assertEqual(status["stages"]["qa"]["status"], "pass")
        self.assertEqual(status["stages"]["duration"]["status"], "pass")
        self.assertEqual(status["stages"]["loudness"]["status"], "pass")

        proof_path = (
            final_quality_authority.authority.state_root()
            / final_quality_authority.authority.project_path_sha256(project)
            / final_quality_authority.DIRECTORY
            / final_quality_authority.LEAF
        )
        proof_before_hold = proof_path.read_bytes()
        hold_path.write_text(json.dumps(hold), encoding="utf-8")
        status, _artifacts = agent_status.build(project, self.root)

        self.assertNotEqual(status["stages"]["qa"]["status"], "pass")
        with self.assertRaises(final_quality_review.QualityReviewError) as raised:
            final_quality_review.produce(project)
        self.assertEqual(raised.exception.code, "newer_human_hold")
        self.assertEqual(proof_path.read_bytes(), proof_before_hold)

        # A machine receipt touched/re-emitted after the human hold must not
        # erase that decision. Only a newer actual HVP-21 verdict can do so.
        os.utime(project / final_quality_review.REVIEW_PATH, None)
        status, _ = agent_status.build(project, self.root)
        self.assertNotEqual(status["stages"]["qa"]["status"], "pass")
        with self.assertRaises(final_quality_review.QualityReviewError) as raised:
            final_quality_review.produce(project)
        self.assertEqual(raised.exception.code, "newer_human_hold")

        visual_review = project / "quality-review/visual-sampling/visual-qa-review.json"
        old_verdict = visual_review.read_bytes()
        os.utime(visual_review, None)
        self.assertEqual(visual_review.read_bytes(), old_verdict)
        with self.assertRaises(final_quality_review.QualityReviewError) as raised:
            final_quality_review.produce(project)
        self.assertEqual(raised.exception.code, "newer_human_hold")

        write_visual_qa_receipts(project, video)
        verdict = json.loads(visual_review.read_text())
        verdict["reviewed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        verdict["notes"] = "Fixture records a new actual verdict after resolving the hold."
        visual_review.write_text(json.dumps(verdict))
        self.assertGreater(
            visual_review.stat().st_mtime_ns, hold_path.stat().st_mtime_ns
        )
        final_quality_review.produce(project)
        status, _artifacts = agent_status.build(project, self.root)
        self.assertEqual(status["stages"]["qa"]["status"], "pass")

    def test_fixed_cli_prints_the_mcp_result_contract(self):
        project = self.project()
        isolated_environment = {
            "PATH": "/usr/bin:/bin",
            "HARU_VIDEO_STUDIO_SELF_EVAL_STATE_ROOT": os.environ[
                "HARU_VIDEO_STUDIO_SELF_EVAL_STATE_ROOT"
            ],
            "HARU_VIDEO_STUDIO_SELF_EVAL_ATTESTATION_ROOT": os.environ[
                "HARU_VIDEO_STUDIO_SELF_EVAL_ATTESTATION_ROOT"
            ],
        }

        completed = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                "-S",
                str(Path(final_quality_review.__file__)),
                str(project),
            ],
            capture_output=True,
            text=True,
            env=isolated_environment,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        value = json.loads(completed.stdout)
        self.assertEqual(value["schema"], "haru.final_quality_review.v1")
        self.assertEqual(value["status"], "complete")
        self.assertEqual(value["project"], project.name)
        self.assertFalse(value["reused"])
        self.assertEqual(
            set(value["artifacts"]),
            {"prep", "review"},
        )

    def test_missing_human_visual_review_never_passes(self):
        project = self.project()
        (project / "quality-review/visual-sampling/visual-qa-review.json").unlink()

        with self.assertRaises(final_quality_review.QualityReviewError) as raised:
            final_quality_review.produce(project)

        self.assertEqual(raised.exception.code, "review_missing")

    def test_stale_final_video_bytes_never_pass(self):
        project = self.project()
        with (project / "output/final.mp4").open("ab") as handle:
            handle.write(b"changed")

        with self.assertRaises(final_quality_review.QualityReviewError) as raised:
            final_quality_review.produce(project)

        self.assertEqual(raised.exception.code, "render_not_current")

    def test_input_change_during_scan_is_rejected_before_promotion(self):
        project = self.project()
        prep = project / final_quality_review.PREP_PATH
        previous = prep.read_bytes()
        real_checks = final_quality_review.media_checks

        def scan_then_change(ffmpeg, video):
            evidence = real_checks(ffmpeg, video)
            with video.open("ab") as handle:
                handle.write(b"changed-during-scan")
            return evidence

        with mock.patch.object(final_quality_review, "media_checks", scan_then_change):
            with self.assertRaises(final_quality_review.QualityReviewError) as raised:
                final_quality_review.produce(project)

        self.assertEqual(raised.exception.code, "render_not_current")
        self.assertEqual(prep.read_bytes(), previous)

    def test_black_and_long_static_video_fail_closed(self):
        for source, expected, slug, duration in (
            (
                "color=c=black:size=64x36:rate=5:duration=60",
                "black_frame_detected",
                "black-demo",
                60,
            ),
            (
                "color=c=red:size=64x36:rate=5:duration=61",
                "static_frame_detected",
                "static-demo",
                61,
            ),
        ):
            with self.subTest(expected=expected):
                project = self.project(source, slug=slug, duration=duration)
                self.seed_fake_current_receipts(project)
                with self.assertRaises(
                    final_quality_review.QualityReviewError
                ) as raised:
                    final_quality_review.produce(project)
                self.assertEqual(raised.exception.code, expected)


if __name__ == "__main__":
    unittest.main()
