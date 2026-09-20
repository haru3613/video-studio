import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from runtime_test_fixture import RuntimePolicyCase

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_status
from test_agent_status import make_ready_project


ROOT = Path(__file__).resolve().parents[1]
IMPORTER = ROOT / "scripts/import-openmontage-reference"
ROLL_TYPES = {
    "a_roll_full",
    "b_roll_pure",
    "b_roll_with_presenter_pip",
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReferenceAnalysisTest(RuntimePolicyCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "studio"
        self.project = make_ready_project(self.workspace, "reference-project")
        self.openmontage = self.root / "OpenMontage"
        self.analysis = (
            self.openmontage
            / "projects/reference-ockl98zqb/artifacts/reference-analysis"
        )
        self.analysis.mkdir(parents=True)
        self._write_openmontage_fixture()
        (self.project / "reference-videos.json").write_text(
            json.dumps(
                {
                    "schema": "haru.reference_videos.v1",
                    "references": [
                        {
                            "id": "ockl98zqb",
                            "selected": True,
                            "source_url": "https://www.youtube.com/watch?v=OcKl98ZQbMQ",
                            "analysis": {
                                "provider": "openmontage",
                                "root_env": "OPENMONTAGE_ROOT",
                                "relative_path": "projects/reference-ockl98zqb/artifacts/reference-analysis",
                            },
                            "classification_reviewed_by": "codex",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _write_openmontage_fixture(self):
        curated = {
            "schema": "haru.reference_video_analysis.v1",
            "source": {
                "url": "https://www.youtube.com/watch?v=OcKl98ZQbMQ",
                "video_id": "OcKl98ZQbMQ",
                "title": "AI capital map",
                "channel": "reference channel",
                "duration_seconds": 1167.994,
                "metadata_snapshot_date": "2026-07-29",
            },
            "analysis_provenance": {
                "openmontage_revision": "c36e412",
                "interval_sample_seconds": 5,
                "interval_sample_count": 234,
                "transcription": {
                    "engine": "local Whisper",
                    "segments": 791,
                    "quality_note": "Proper nouns may be incorrect.",
                },
            },
            "editing_metrics": {
                "detected_visual_segments": 300,
                "average_segment_seconds": 3.89,
                "median_segment_seconds": 2.7,
                "p75_segment_seconds": 4.99,
                "overall_cuts_per_minute": 15.41,
            },
            "roll_mix_estimate": {
                "method": "Manual classification of one frame every five seconds.",
                "confidence": "moderate",
                "expected_error_percentage_points": 5,
                "categories": [
                    {
                        "id": "a_roll_full",
                        "sample_count": 76,
                        "estimated_share_percent": 32.5,
                    },
                    {
                        "id": "b_roll_pure",
                        "sample_count": 68,
                        "estimated_share_percent": 29.1,
                    },
                    {
                        "id": "b_roll_with_presenter_pip",
                        "sample_count": 90,
                        "estimated_share_percent": 38.5,
                    },
                ],
            },
            "chapters": [
                {
                    "start_seconds": 0,
                    "end_seconds": 61,
                    "label": "Hook",
                    "cuts_per_minute": 26.6,
                },
                {
                    "start_seconds": 61,
                    "end_seconds": 1168,
                    "label": "Body",
                    "cuts_per_minute": 14.9,
                },
            ],
            "five_aspect_shot_groups": [
                {
                    "id": "presenter_anchor",
                    "subject": "Presenter",
                    "subject_motion": "Small gestures",
                    "scene": "Studio",
                    "spatial_framing": "Medium shot",
                    "camera": "Locked",
                    "editorial_function": "Trust reset",
                }
            ],
            "reusable_workflow_rules": [
                "Use the fastest cut density in the first minute.",
                "Pair important claims with visible evidence.",
            ],
            "known_limitations": [
                "OpenMontage's URL analyzer rejected this 1168-second video at its 600-second limit, so the local file was analyzed.",
                "All 300 motion_type values are unknown with flow_variance -1.",
                "Roll shares are five-second sample estimates.",
            ],
        }
        (self.analysis / "haru_reference_analysis.json").write_text(
            json.dumps(curated), encoding="utf-8"
        )
        scenes = [
            {
                "index": index,
                "start_seconds": index * 3.89,
                "end_seconds": (index + 1) * 3.89,
            }
            for index in range(300)
        ]
        (self.analysis / "scenes.json").write_text(
            json.dumps({"scenes": scenes}), encoding="utf-8"
        )
        brief_scenes = [
            {"scene_index": index, "motion_type": "unknown", "flow_variance": -1}
            for index in range(300)
        ]
        (self.analysis / "video_analysis_brief.json").write_text(
            json.dumps(
                {
                    "version": "1.0",
                    "source": {"duration_seconds": 1167.994},
                    "structure_analysis": {
                        "total_scenes": 300,
                        "scenes": brief_scenes,
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.analysis / "reference_audio.json").write_text(
            json.dumps(
                {
                    "language": "zh",
                    "segments": [{"start": 0, "end": 1, "text": "fixture"}],
                    "text": "fixture",
                }
            ),
            encoding="utf-8",
        )
        (self.analysis / "reference_video.mp4").write_bytes(b"large media stays external")

    def invoke(self, *extra):
        environment = {
            **os.environ,
            "OPENMONTAGE_ROOT": str(self.openmontage),
        }
        return subprocess.run(
            [IMPORTER, self.project, *extra],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def test_long_reference_import_is_small_digest_bound_and_preserves_limits(self):
        result = self.invoke()

        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.project / "reference-analysis.json"
        analysis = json.loads(output.read_text())
        self.assertEqual(analysis["schema"], "haru.reference_video_analysis.v1")
        self.assertEqual(analysis["reference_id"], "ockl98zqb")
        self.assertLess(output.stat().st_size, 100_000)
        self.assertEqual(
            {item["roll_type"] for item in analysis["roll_mix"]["categories"]},
            ROLL_TYPES,
        )
        self.assertTrue(analysis["methodology"]["long_video_local_fallback"])
        self.assertEqual(
            analysis["methodology"]["motion_classification"]["unknown_count"],
            300,
        )
        self.assertEqual(
            {item["name"] for item in analysis["evidence"]["artifacts"]},
            {
                "haru_reference_analysis.json",
                "scenes.json",
                "video_analysis_brief.json",
                "reference_audio.json",
            },
        )
        for item in analysis["evidence"]["artifacts"]:
            source = self.analysis / item["name"]
            self.assertEqual(item["sha256"], sha256(source))
            self.assertEqual(item["bytes"], source.stat().st_size)
        self.assertFalse((self.project / "reference_video.mp4").exists())
        self.assertFalse((self.project / "reference_audio.json").exists())

    def test_reference_gate_is_optional_but_a_declared_reference_fails_closed(self):
        (self.project / "reference-videos.json").unlink()
        status, _ = agent_status.build(self.project, self.workspace)
        self.assertNotIn("reference_analysis", status["required_stages"])
        self.assertEqual(status["blockers"], [])

        self.setUp_reference_declaration_only()
        status, _ = agent_status.build(self.project, self.workspace)
        self.assertEqual(status["stages"]["reference_analysis"]["status"], "missing")
        self.assertIn(
            "reference_analysis_not_passed",
            {item["code"] for item in status["blocker_details"]},
        )

    def setUp_reference_declaration_only(self):
        (self.project / "reference-videos.json").write_text(
            json.dumps(
                {
                    "schema": "haru.reference_videos.v1",
                    "references": [
                        {
                            "id": "ockl98zqb",
                            "selected": True,
                            "source_url": "https://www.youtube.com/watch?v=OcKl98ZQbMQ",
                            "analysis": {
                                "provider": "openmontage",
                                "root_env": "OPENMONTAGE_ROOT",
                                "relative_path": "projects/reference-ockl98zqb/artifacts/reference-analysis",
                            },
                            "classification_reviewed_by": "codex",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_adoption_and_storyboard_rule_references_pass_the_optional_gate(self):
        self.assertEqual(self.invoke().returncode, 0)
        analysis = json.loads((self.project / "reference-analysis.json").read_text())
        rules = [rule["id"] for rule in analysis["editing_rules"]]
        (self.project / "reference-adoption.json").write_text(
            json.dumps(
                {
                    "schema": "haru.reference_adoption.v1",
                    "reference_id": "ockl98zqb",
                    "adopted_rules": [
                        {"rule_id": rules[0], "reason": "Use faster hook pacing."}
                    ],
                    "rejected_rules": [
                        {"rule_id": rules[1], "reason": "No financial evidence in this story."}
                    ],
                }
            ),
            encoding="utf-8",
        )
        storyboard_path = self.project / "storyboard-final-timed.json"
        storyboard = json.loads(storyboard_path.read_text())
        storyboard["scenes"][0]["reference_rule_ids"] = [rules[0]]
        storyboard_path.write_text(json.dumps(storyboard), encoding="utf-8")

        status, artifacts = agent_status.build(self.project, self.workspace)

        self.assertEqual(status["stages"]["reference_analysis"]["status"], "pass")
        self.assertNotIn(
            "reference_analysis_not_passed",
            {item["code"] for item in status["blocker_details"]},
        )
        self.assertEqual(
            artifacts["canonical"]["reference_analysis"]["sha256"],
            sha256(self.project / "reference-analysis.json"),
        )

    def test_storyboard_cannot_claim_a_rejected_reference_rule(self):
        self.assertEqual(self.invoke().returncode, 0)
        analysis = json.loads((self.project / "reference-analysis.json").read_text())
        rules = [rule["id"] for rule in analysis["editing_rules"]]
        (self.project / "reference-adoption.json").write_text(
            json.dumps(
                {
                    "schema": "haru.reference_adoption.v1",
                    "reference_id": "ockl98zqb",
                    "adopted_rules": [
                        {"rule_id": rules[0], "reason": "Use faster hook pacing."}
                    ],
                    "rejected_rules": [
                        {"rule_id": rules[1], "reason": "Not suitable."}
                    ],
                }
            ),
            encoding="utf-8",
        )
        storyboard_path = self.project / "storyboard-final-timed.json"
        storyboard = json.loads(storyboard_path.read_text())
        storyboard["scenes"][0]["reference_rule_ids"] = [rules[1]]
        storyboard_path.write_text(json.dumps(storyboard), encoding="utf-8")

        status, _ = agent_status.build(self.project, self.workspace)

        self.assertEqual(status["stages"]["reference_analysis"]["status"], "warn")
        self.assertIn(
            "storyboard cites a reference rule that was not adopted",
            status["stages"]["reference_analysis"]["warnings"],
        )

    def test_analysis_locator_cannot_escape_openmontage_root(self):
        declaration = json.loads((self.project / "reference-videos.json").read_text())
        declaration["references"][0]["analysis"]["relative_path"] = "../outside"
        (self.project / "reference-videos.json").write_text(
            json.dumps(declaration), encoding="utf-8"
        )

        result = self.invoke()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.project / "reference-analysis.json").exists())


if __name__ == "__main__":
    unittest.main()
