#!/usr/bin/env python3
"""Focused unit and lifecycle checks for render_self_eval."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import render_self_eval as engine
import render_contract
import self_eval_fixture


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_marker(project: Path, *, duration=3.0) -> dict:
    video = project / "output/final.mp4"
    digest = sha256(video)
    return {
        "schema": "haru.render_result.v1",
        "status": "render_complete",
        "render_input_revision": render_contract.render_input_revision(project),
        "project": project.name,
        "output": "output/final.mp4",
        "video_sha256": digest,
        "bytes": video.stat().st_size,
        "duration_seconds": duration,
        "loudness_lufs": -14.0,
        "true_peak_dbfs": -1.2,
        "loudness_range_lu": 2.0,
        "mix": {
            "schema": "haru.final_mix.v1",
            "method": "ffmpeg_loudnorm_two_pass",
            "normalization_type": "linear",
            "input_sha256": "1" * 64,
            "audio_mix": {
                "schema": "haru.audio_mix.v1",
                "plan_sha256": "2" * 64,
                "background_music": {
                    "path": "assets/music.wav",
                    "sha256": "3" * 64,
                },
                "sound_effects": [],
                "ducking": "sidechaincompress.v1",
            },
            "target": {
                "integrated_lufs": -14.0,
                "true_peak_dbfs": -1.0,
                "loudness_range_lu": 2.0,
            },
        },
    }


def write_project(root: Path, name="fixture-project", *, video=b"video") -> Path:
    project = root / name
    (project / "output").mkdir(parents=True)
    (project / "output/final.mp4").write_bytes(video)
    (project / "project-contract.json").write_text(
        json.dumps(
            {
                "schema": "haru.project_contract.v1",
                "lane_contract": "manual.v1",
                "production_profile": None,
            }
        ),
        encoding="utf-8",
    )
    (project / "storyboard-final-timed.json").write_text(
        json.dumps(
            {
                "schema": "haru.storyboard_timed.v1",
                "project": project.name,
                "duration_seconds": 3.0,
                "fps": 30,
                "scenes": [
                    {
                        "scene_id": "open",
                        "start_seconds": 0.0,
                        "end_seconds": 1.5,
                        "visual_events": [
                            {
                                "event_id": "open-a",
                                "start_seconds": 0.0,
                                "end_seconds": 1.5,
                                "visual_state": "host",
                                "presenter_state": "talking",
                            }
                        ],
                    },
                    {
                        "scene_id": "tail",
                        "start_seconds": 1.5,
                        "end_seconds": 3.0,
                        "visual_events": [
                            {
                                "event_id": "tail-a",
                                "start_seconds": 1.5,
                                "end_seconds": 3.0,
                                "visual_state": "evidence",
                                "presenter_state": "hidden",
                            }
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (project / "storyboard-final-timed-validation.json").write_text(
        json.dumps(
            {
                "schema": "haru.storyboard_validation.v1",
                "ok": True,
                "checks": [{"status": "pass"}],
            }
        ),
        encoding="utf-8",
    )
    (project / "narration-final.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,500\nopen\n\n"
        "2\n00:00:01,500 --> 00:00:03,000\ntail\n",
        encoding="utf-8",
    )
    (project / "output/final.mp4.render-result").write_text(
        json.dumps(render_marker(project)), encoding="utf-8"
    )
    return project


def refresh_marker(project: Path) -> None:
    (project / "output/final.mp4.render-result").write_text(
        json.dumps(render_marker(project)), encoding="utf-8"
    )


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = write_project(self.root)
        self.environment = mock.patch.dict(
            os.environ,
            {
                engine.authority.STATE_ROOT_ENV: str(self.root / "state"),
                engine.authority.ATTESTATION_ROOT_ENV: str(self.root / "attestations"),
            },
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_identity_bytes_are_exact_fixed_order_and_absent_lane_authorities(self):
        value = json.loads(engine.identity_bytes(self.project))
        self.assertEqual(list(value), ["boundary_policy_sha256", "inputs", "schema"])
        self.assertEqual(value["schema"], engine.IDENTITY_SCHEMA)
        self.assertEqual(
            [entry["path"] for entry in value["inputs"]], list(engine.INPUT_ORDER)
        )
        self.assertEqual(
            value["inputs"][5], {"path": engine.EDITORIAL, "state": "absent"}
        )
        self.assertEqual(
            value["inputs"][6], {"path": engine.ASSEMBLY, "state": "absent"}
        )
        self.assertEqual(
            engine.authority.canonical_digest(value),
            hashlib.sha256(engine.identity_bytes(self.project)).hexdigest(),
        )

    def test_evidence_budget_counts_exact_bytes_and_never_overcommits(self):
        with mock.patch.dict(engine.POLICY, {"max_total_evidence_bytes": 10}):
            budget = engine.Budget()
            self.assertTrue(budget.admit(6))
            self.assertTrue(budget.admit(4))
            self.assertFalse(budget.admit(1))
            self.assertEqual(budget.total, 10)
            self.assertTrue(budget.exceeded)

    def test_fixture_pass_is_ledger_anchored_and_current_pass_is_single_read(self):
        result = self_eval_fixture.seal(self.project, status="pass", attempt=1)
        current = engine.current_pass(self.project)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(current["result"], result)
        self.assertEqual(current["ref"]["path"], engine.RESULT_PATH)
        result_path = self.project / engine.RESULT_PATH
        self.assertEqual(current["ref"]["sha256"], sha256(result_path))
        self.assertEqual(current["ref"]["bytes"], result_path.stat().st_size)

    def test_unchanged_seal_is_byte_stable_reuse(self):
        self_eval_fixture.seal(self.project, status="pass", attempt=1)
        path = self.project / engine.RESULT_PATH
        before = path.read_bytes()
        self_eval_fixture.seal(self.project, status="pass", attempt=1)
        self.assertEqual(path.read_bytes(), before)
        generations = list((self.root / "state").rglob("generations/*.json"))
        self.assertEqual(len(generations), 2)  # evaluate_pending + vision_pass

    def test_new_identity_allocates_next_immutable_ordinal(self):
        self_eval_fixture.seal(self.project, status="pass", attempt=1)
        (self.project / "output/final.mp4").write_bytes(b"second-video")
        refresh_marker(self.project)
        result = self_eval_fixture.seal(self.project, status="pass", attempt=2)
        self.assertEqual(result["attempt"], 2)
        self.assertTrue(
            (self.project / f"{engine.attempt_dir(1)}/outcome.json").is_file()
        )
        self.assertTrue(
            (self.project / f"{engine.attempt_dir(2)}/outcome.json").is_file()
        )

    def test_tail_sampling_policy_change_invalidates_old_pass_and_allocates_new_attempt(
        self,
    ):
        legacy_policy = copy.deepcopy(engine.POLICY)
        legacy_policy["algorithm"] = "haru.render_self_eval_policy.v1"
        legacy_policy["sampling"]["mode"] = "half_open_bin_center"
        legacy_policy["tool_provenance"]["algorithm"] = "haru.render_self_eval.v1"
        legacy_policy["sampling"].pop("tail_frame_strategy")
        legacy_policy.pop("video_tail_coverage")
        legacy_bytes = engine.authority.canonical_bytes(legacy_policy)
        legacy_sha = engine.authority.canonical_digest(legacy_policy)
        with (
            mock.patch.object(engine, "POLICY_BYTES", legacy_bytes),
            mock.patch.object(engine, "POLICY_SHA256", legacy_sha),
            mock.patch.object(engine, "ALGORITHM", "haru.render_self_eval.v1"),
        ):
            old = self_eval_fixture.seal(self.project, status="pass", attempt=1)
            self.assertEqual(old["attempt"], 1)
            self.assertIsNotNone(engine.current_pass_ref(self.project))

        self.assertIsNone(engine.current_pass_ref(self.project))
        current = engine.evaluate(self.project)
        self.assertEqual(current["attempt"], 2)
        self.assertNotEqual(current["attempt_identity"], old["attempt_identity"])
        self.assertTrue(
            (self.project / f"{engine.attempt_dir(1)}/outcome.json").is_file()
        )
        self.assertIsNone(engine.current_pass_ref(self.project))

    def test_fixture_synthesizes_failed_history_for_attempt_three(self):
        result = self_eval_fixture.seal(
            self.project,
            status="human_intervention_required",
            attempt=3,
        )
        self.assertEqual(result["attempt"], 3)
        self.assertEqual(result["status"], "human_intervention_required")
        for ordinal in (1, 2, 3):
            outcome = self.project / f"{engine.attempt_dir(ordinal)}/outcome.json"
            self.assertEqual(
                json.loads(outcome.read_text(encoding="utf-8"))["verdict"],
                "fail",
            )

    def test_three_reviewer_failures_require_human_and_no_fourth_attempt(self):
        self_eval_fixture.seal(self.project, status="fail", attempt=1)
        (self.project / "output/final.mp4").write_bytes(b"second")
        refresh_marker(self.project)
        self_eval_fixture.seal(self.project, status="fail", attempt=2)
        (self.project / "output/final.mp4").write_bytes(b"third")
        refresh_marker(self.project)
        result = self_eval_fixture.seal(
            self.project, status="human_intervention_required", attempt=3
        )
        self.assertEqual(result["status"], "human_intervention_required")
        (self.project / "output/final.mp4").write_bytes(b"fourth")
        refresh_marker(self.project)
        with self.assertRaisesRegex(ValueError, "next immutable ordinal is 4"):
            self_eval_fixture.seal(self.project, status="pass", attempt=3)

    def test_marker_change_stales_a_previously_anchored_pass(self):
        self_eval_fixture.seal(self.project, status="pass", attempt=1)
        marker = render_marker(self.project)
        marker["output"] = "output/not-final.mp4"
        (self.project / "output/final.mp4.render-result").write_text(
            json.dumps(marker), encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "no longer describes|marker"):
            engine.validate_current(self.project)
        self.assertIsNone(engine.current_pass_ref(self.project))

    def test_unexpected_manual_lane_editorial_authority_fails_identity(self):
        (self.project / "editorial-contract.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unexpected authority"):
            engine.identity_bytes(self.project)

    def test_nofollow_candidate_refuses_before_any_attempt(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"outside")
        candidate = self.project / "output/final.mp4"
        candidate.unlink()
        candidate.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "required identity input"):
            engine.identity_bytes(self.project)
        self.assertFalse((self.project / engine.ROOT).exists())

    def test_boundary_plan_keeps_authored_ids_and_srt_context(self):
        storyboard = json.loads(
            (self.project / "storyboard-final-timed.json").read_text()
        )
        cues = engine.parse_srt_labels(
            (self.project / "narration-final.srt").read_text()
        )
        windows = engine.plan_windows(storyboard, cues, [], 3.0)
        authored = [window for window in windows if window["kind"] == "authored"]
        self.assertEqual(len(authored), 1)
        self.assertIn("scene:tail", authored[0]["authored_ids"])
        self.assertEqual(len(authored[0]["sample_times_seconds"]), 10)
        self.assertEqual(
            [label["cue_index"] for label in authored[0]["srt_labels"]], [1, 2]
        )

    def test_detectors_report_gap_edge_black_and_pcm_jump(self):
        window = {
            "window_id": "authored-001",
            "start_seconds": 0.5,
            "end_seconds": 1.5,
            "boundary_seconds": 1.0,
        }
        gaps = engine.frame_gap_findings(window, [0.5, 0.533333, 0.65], 30.0)
        self.assertEqual(gaps[0]["category"], "frame_gap")
        edge = {
            "start_seconds": 0.0,
            "end_seconds": 2.0,
        }
        self.assertGreaterEqual(
            engine.edge_black_covered(
                [
                    {
                        "start_seconds": 0.0,
                        "end_seconds": 1.9,
                        "duration_seconds": 1.9,
                    }
                ],
                edge,
            ),
            0.9,
        )
        raw = self.root / "jump.raw"
        samples = [0] * 48000 + [32767] * 48000
        raw.write_bytes(
            b"".join(int(value).to_bytes(2, "little", signed=True) for value in samples)
        )
        jump = engine.audio_jump(raw, window)
        self.assertGreaterEqual(jump["value"], 0.5)


if __name__ == "__main__":
    unittest.main()
