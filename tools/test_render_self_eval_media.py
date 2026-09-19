#!/usr/bin/env python3
"""Runtime-generated ffmpeg fixtures for the production evaluator path."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import render_self_eval as engine
import self_eval_authority as authority
import self_eval_fixture
from test_render_self_eval import refresh_marker, write_project


FFMPEG = shutil.which("ffmpeg")


def ffmpeg(*arguments, cwd=None):
    subprocess.run(
        [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *arguments],
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def good_media(target: Path):
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:s=320x180:r=30:d=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=3",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(target),
    )


@unittest.skipUnless(FFMPEG, "ffmpeg is required for runtime media checks")
class RuntimeMediaCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = write_project(self.root, name="media")
        self.environment = mock.patch.dict(
            os.environ,
            {
                authority.STATE_ROOT_ENV: str(self.root / "state"),
                authority.ATTESTATION_ROOT_ENV: str(self.root / "attestations"),
            },
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    @property
    def video(self):
        return self.project / "output/final.mp4"

    def prepare(self, producer=good_media):
        producer(self.video)
        refresh_marker(self.project)

    def finding_categories(self, result):
        return {finding["category"] for finding in result["findings"]}

    def set_authored_duration(self, duration):
        storyboard_path = self.project / "storyboard-final-timed.json"
        storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))
        storyboard["duration_seconds"] = duration
        storyboard_path.write_text(json.dumps(storyboard), encoding="utf-8")
        marker_path = self.project / "output/final.mp4.render-result"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["duration_seconds"] = duration
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

    def test_clean_synthetic_media_reaches_needs_human_with_all_evidence(self):
        self.prepare()
        result = engine.evaluate(self.project)
        self.assertEqual(result["status"], "needs_human")
        index = json.loads(
            (self.project / result["evidence_index"]["path"]).read_text()
        )
        self.assertTrue(index["entries"])
        self.assertFalse(
            [entry for entry in index["entries"] if entry["availability"] != "available"]
        )
        for kind in ("filmstrip", "waveform", "labels", "composite"):
            self.assertIn(kind, {entry["kind"] for entry in index["entries"]})

    def test_audio_postroll_uses_the_last_decoded_video_frame_for_tail_evidence(self):
        def audio_postroll(target):
            ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:r=30:d=3",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=3.2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(target),
            )

        self.prepare(audio_postroll)
        self.set_authored_duration(3.2)
        result = engine.evaluate(self.project)
        self.assertEqual(result["status"], "needs_human")
        plan = json.loads(
            (self.project / result["boundary_plan"]["path"]).read_text()
        )
        tail = next(window for window in plan["windows"] if window["window_id"] == "edge-tail")
        self.assertEqual(tail["sample_times_seconds"][-1], 2.966667)

    def test_video_ending_before_authored_visual_tail_still_fails(self):
        def truncated_video(target):
            ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:r=30:d=2.7",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=3.2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(target),
            )

        self.prepare(truncated_video)
        self.set_authored_duration(3.2)
        result = engine.evaluate(self.project)
        self.assertEqual(result["status"], "fail")
        self.assertIn("video_tail_truncated", self.finding_categories(result))

    def test_missing_audio_seals_stream_profile_failure(self):
        def missing_audio(target):
            ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:r=30:d=3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(target),
            )

        self.prepare(missing_audio)
        result = engine.evaluate(self.project)
        self.assertEqual(result["status"], "fail")
        self.assertIn("stream_profile_invalid", self.finding_categories(result))

    def test_first_two_seconds_black_seal_invalid_edge_black(self):
        def edge_black(target):
            ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x180:r=30:d=2",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:r=30:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=3",
                "-filter_complex",
                "[0:v][1:v]concat=n=2:v=1:a=0[v]",
                "-map",
                "[v]",
                "-map",
                "2:a:0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(target),
            )

        self.prepare(edge_black)
        result = engine.evaluate(self.project)
        self.assertEqual(result["status"], "fail")
        self.assertIn("invalid_edge_black", self.finding_categories(result))

    def test_one_frame_black_at_authored_boundary_seals_black_flash(self):
        def exact_boundary_black(target):
            ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:r=30:d=3",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=3",
                "-vf",
                "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(t,1.483333,1.516667)'",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(target),
            )

        self.prepare(exact_boundary_black)
        result = engine.evaluate(self.project)
        self.assertEqual(result["status"], "fail")
        self.assertIn("black_flash", self.finding_categories(result))

    def test_vfr_dropped_frame_seals_frame_gap(self):
        def dropped(target):
            source = target.with_name("source.mp4")
            good_media(source)
            ffmpeg(
                "-i",
                str(source),
                "-vf",
                "select='not(eq(n,45))'",
                "-fps_mode",
                "vfr",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                str(target),
            )

        self.prepare(dropped)
        result = engine.evaluate(self.project)
        self.assertEqual(result["status"], "fail")
        self.assertIn("frame_gap", self.finding_categories(result))

    def test_pcm_step_at_authored_boundary_seals_audio_discontinuity(self):
        def pcm_jump(target):
            ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180:r=30:d=3",
                "-f",
                "lavfi",
                "-i",
                "aevalsrc=if(lt(t\\,1.5)\\,-0.9\\,0.9):s=48000:d=3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "pcm_s16le",
                "-shortest",
                str(target),
            )

        self.prepare(pcm_jump)
        result = engine.evaluate(self.project)
        self.assertEqual(result["status"], "fail")
        self.assertIn("audio_discontinuity", self.finding_categories(result))

    def test_protected_vision_failure_seals_overlay_conflict(self):
        self.prepare()
        current = engine.evaluate(self.project)
        review = {
            "reviewer_kind": "vision",
            "verdict": "fail",
            "reviewed_by": "vision-runtime",
            "provider": "fixture-provider",
            "model": "fixture-model",
            "capability": "whole_window_structural_review.v1",
            "notes": "overlay masks the boundary subject",
            "findings": [
                {
                    "timestamp_seconds": 1.5,
                    "boundary_id": "authored-001",
                    "category": "overlay_conflict",
                    "severity": "fail",
                    "message": "overlay obscures the authored transition",
                }
            ],
        }
        intent = engine._vision_intent(self.project, current, review)
        ref = self_eval_fixture._mint_attestation(
            self.project,
            authority.canonical_digest(intent),
            authority.VISION_ATTESTATION_SCHEMA,
        )
        result = engine.record_review(self.project, review, ref)
        self.assertEqual(result["status"], "fail")
        self.assertIn("overlay_conflict", self.finding_categories(result))

    def test_vision_unavailable_requires_protected_human_fallback(self):
        self.prepare()
        current = engine.evaluate(self.project)
        unavailable = {
            "reviewer_kind": "vision",
            "verdict": "unavailable",
            "reviewed_by": "vision-runtime",
            "provider": "fixture-provider",
            "model": "fixture-model",
            "capability": "whole_window_structural_review.v1",
            "notes": "provider unavailable",
            "findings": [],
        }
        vision_intent = engine._vision_intent(self.project, current, unavailable)
        vision_ref = self_eval_fixture._mint_attestation(
            self.project,
            authority.canonical_digest(vision_intent),
            authority.VISION_ATTESTATION_SCHEMA,
        )
        current = engine.record_review(self.project, unavailable, vision_ref)
        self.assertEqual(current["status"], "needs_human")
        unavailable_ref = engine._project_ref(
            self.project,
            f"{engine.attempt_dir(1)}/vision-unavailable.json",
        )
        human = {
            "reviewer_kind": "human_fallback",
            "verdict": "pass",
            "reviewed_by": "human-reviewer",
            "provider": "",
            "model": "",
            "capability": engine.HUMAN_CAPABILITY,
            "notes": "manual structural inspection passed",
            "findings": [],
        }
        human_intent = engine._human_intent(
            self.project, current, human, unavailable_ref
        )
        human_ref = self_eval_fixture._mint_attestation(
            self.project,
            authority.canonical_digest(human_intent),
            authority.HUMAN_ATTESTATION_SCHEMA,
        )
        result = engine.record_review(self.project, human, human_ref)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["review"]["path"], f"{engine.attempt_dir(1)}/review.json")


if __name__ == "__main__":
    unittest.main()
