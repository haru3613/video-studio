#!/usr/bin/env python3
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import agent_status
import segment_plan


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_srt(path):
    rows = []
    for index in range(4):
        rows.append(
            f"{index + 1}\n00:00:{index * 10:02d},000 --> 00:00:{(index + 1) * 10:02d},000\ncue {index + 1}"
        )
    path.write_text("\n\n".join(rows) + "\n", encoding="utf-8")


def plan_for(project):
    inputs = {
        name: {"path": relative, "sha256": digest(project / relative)}
        for name, relative in segment_plan.INPUT_PATHS.items()
    }
    segments = []
    for ordinal, segment_id in enumerate(segment_plan.SEGMENTS, start=1):
        start, end = (ordinal - 1) * 10, ordinal * 10
        scene_id, event_id = f"s{ordinal}", f"e{ordinal}"
        segments.append(
            {
                "segment_id": segment_id,
                "ordinal": ordinal,
                "narrative_role": f"act-{ordinal}",
                "start": {"scene_id": scene_id, "event_id": event_id, "cue_index": ordinal, "seconds": start},
                "end": {"scene_id": scene_id, "event_id": event_id, "cue_index": ordinal, "seconds": end},
                "scene_ids": [scene_id],
                "event_ids": [event_id],
                "selector": {"kind": "frame_range.v1", "fps": 30, "start_frame": start * 30, "end_frame": end * 30},
                "output": segment_plan.OUTPUTS[segment_id],
            }
        )
    return {
        "schema": segment_plan.SCHEMA,
        "project": project.name,
        "inputs": inputs,
        "render_input_sha256": segment_plan._render_input_digest(inputs),
        "segments": segments,
        "assembly": {"order": list(segment_plan.SEGMENTS), "transition_policy": "cut.v1", "audio_policy": "premix.v1"},
    }


class SegmentPlanContractTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "demo"
        self.project.mkdir()
        (self.project / "narration-final.mp3").write_bytes(b"narration")
        write_srt(self.project / "narration-final.srt")
        (self.project / "editorial-contract.json").write_text("{}\n", encoding="utf-8")
        scenes = []
        for index in range(4):
            start, end = index * 10, (index + 1) * 10
            scenes.append({"scene_id": f"s{index + 1}", "start_seconds": start, "end_seconds": end, "visual_events": [{"event_id": f"e{index + 1}", "start_seconds": start, "end_seconds": end}]})
        (self.project / "storyboard-final-timed.json").write_text(json.dumps({"schema": "haru.storyboard_timed.v1", "scenes": scenes}), encoding="utf-8")
        self.plan = plan_for(self.project)

    def tearDown(self):
        self.temporary.cleanup()

    def write_plan(self):
        (self.project / segment_plan.PLAN_PATH).write_text(json.dumps(self.plan), encoding="utf-8")

    def assert_problem(self, text):
        self.write_plan()
        result = segment_plan.validate(self.project)
        self.assertEqual(result["mode"], "invalid_segment_plan")
        self.assertTrue(any(text in problem for problem in result["problems"]), result["problems"])

    def test_valid_plan_has_one_actionable_first_segment(self):
        self.write_plan()
        result = segment_plan.validate(self.project)
        self.assertEqual(result["mode"], "segmented")
        self.assertEqual(result["next_actionable_segment"], "qi")
        self.assertEqual([item["segment_id"] for item in result["segments"]], list(segment_plan.SEGMENTS))
        self.assertEqual([item["actionable"] for item in result["segments"]], [True, False, False, False])
        self.assertTrue(all(item["status"] == "planned" and item["review_receipt"] is None for item in result["segments"]))
        self.assertTrue(
            all(
                segment_plan.canonical_layout.valid_sha256(item["dependency_sha256"])
                and item["current_digest"] == item["dependency_sha256"]
                for item in result["segments"]
            )
        )
        self.assertTrue(
            segment_plan.canonical_layout.valid_sha256(
                result["global_dependency_sha256"]
            )
        )

    def test_canonical_digest_matches_cross_runtime_golden_vector(self):
        value = {
            "z": [None, True, False, -1, 2**63, 0.00001, "雪"],
            "a": {"opacity": 1e-5, "half": 0.15},
            "overflow": 2**64,
        }
        self.assertEqual(
            segment_plan._canonical_digest(value),
            "12a96cd79d958826256e03a27b661315dc460d46913e67e2607754f020dc2cb8",
        )

    def test_frame_index_uses_ties_to_even(self):
        self.assertEqual(segment_plan._frame_index(0.15, 30), 4)
        self.assertEqual(segment_plan._frame_index(0.25, 30), 8)

    def test_boolean_ordinal_fails_closed(self):
        self.plan["segments"][0]["ordinal"] = True
        self.assert_problem("ordinal")

    def test_wrong_ids_or_order_fails_closed(self):
        self.plan["segments"][0]["segment_id"] = "cheng"
        self.assert_problem("segment_id")

    def test_reordered_segments_fail_closed(self):
        self.plan["segments"][0], self.plan["segments"][1] = self.plan["segments"][1], self.plan["segments"][0]
        self.assert_problem("segment_id")

    def test_gap_fails_closed(self):
        self.plan["segments"][1]["start"]["seconds"] = 11
        self.plan["segments"][1]["selector"]["start_frame"] = 330
        self.assert_problem("shared scene/event/SRT boundary")

    def test_overlap_fails_closed(self):
        self.plan["segments"][1]["start"]["seconds"] = 9
        self.plan["segments"][1]["selector"]["start_frame"] = 270
        self.assert_problem("shared scene/event/SRT boundary")

    def test_duplicate_scene_or_event_fails_closed(self):
        self.plan["segments"][1]["scene_ids"] = ["s02", "s02"]
        self.assert_problem("scene ownership")
        self.plan = plan_for(self.project)
        self.plan["segments"][1]["event_ids"] = ["e02", "e02"]
        self.assert_problem("event ownership")

    def test_empty_scene_ids_cannot_be_absorbed_by_a_later_act(self):
        self.plan["segments"][1]["scene_ids"] = []
        self.plan["segments"][1]["event_ids"] = []
        self.plan["segments"][2]["scene_ids"] = ["s2", "s3"]
        self.plan["segments"][2]["event_ids"] = ["e2", "e3"]
        self.plan["segments"][2]["start"] = {
            "scene_id": "s2",
            "event_id": "e2",
            "cue_index": 2,
            "seconds": 10,
        }
        self.plan["segments"][2]["selector"]["start_frame"] = 300
        self.write_plan()
        result = segment_plan.validate(self.project)
        self.assertEqual(result["mode"], "invalid_segment_plan")
        self.assertEqual(result["segments"], [])
        self.assertIsNone(result["next_actionable_segment"])
        self.assertTrue(
            any("non-empty" in problem for problem in result["problems"]),
            result["problems"],
        )
    def test_illegal_boundary_fails_closed(self):
        self.plan["segments"][1]["start"]["cue_index"] = 1
        self.assert_problem("does not identify")

    def test_digest_mismatch_fails_closed(self):
        self.plan["inputs"]["srt"]["sha256"] = "0" * 64
        self.assert_problem("does not match current narration-final.srt")

    def test_srt_cue_crossing_a_segment_boundary_fails_closed(self):
        (self.project / "narration-final.srt").write_text(
            "1\n00:00:00,000 --> 00:00:15,000\ncrosses boundary\n\n"
            "2\n00:00:15,000 --> 00:00:20,000\ncue 2\n\n"
            "3\n00:00:20,000 --> 00:00:30,000\ncue 3\n\n"
            "4\n00:00:30,000 --> 00:00:40,000\ncue 4\n",
            encoding="utf-8",
        )
        self.plan = plan_for(self.project)
        self.assert_problem("boundary")

    def test_unknown_storyboard_schema_fails_closed(self):
        storyboard_path = self.project / segment_plan.INPUT_PATHS["storyboard"]
        storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))
        storyboard["schema"] = "wrong.storyboard.schema"
        storyboard_path.write_text(json.dumps(storyboard), encoding="utf-8")
        self.plan = plan_for(self.project)
        self.assert_problem("storyboard-final-timed.json schema")

    def test_invalid_unicode_fails_closed_before_digesting(self):
        storyboard_path = self.project / segment_plan.INPUT_PATHS["storyboard"]
        storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))
        storyboard["global_visual_direction"] = "\ud800"
        storyboard_path.write_text(
            json.dumps(storyboard, ensure_ascii=True),
            encoding="utf-8",
        )
        self.plan = plan_for(self.project)
        self.assert_problem("unsupported canonical JSON")

    def test_status_and_artifact_manifest_agree(self):
        self.write_plan()
        status, manifest = agent_status.build(self.project, self.root)
        self.assertEqual(status["segment_mode"], "segmented")
        self.assertEqual(status["next_actionable_segment"], "qi")
        self.assertEqual(manifest["canonical"]["segment_plan"]["path"], "demo/segment-plan.json")
        self.assertEqual(manifest["canonical"]["segment_plan"]["sha256"], digest(self.project / segment_plan.PLAN_PATH))
        self.assertEqual(status["segments"], segment_plan.validate(self.project)["segments"])

    def test_absent_plan_is_explicitly_legacy_and_has_no_segment_approval(self):
        result = segment_plan.validate(self.project)
        self.assertEqual(result["mode"], "legacy_non_segmented")
        self.assertEqual(result["segments"], [])
        self.assertIsNone(result["next_actionable_segment"])
        status, manifest = agent_status.build(self.project, self.root)
        self.assertEqual(status["segment_mode"], "legacy_non_segmented")
        self.assertEqual(status["segments"], [])
        self.assertIsNone(status["next_actionable_segment"])
        self.assertIsNone(manifest["canonical"]["segment_plan"])


if __name__ == "__main__":
    unittest.main()
