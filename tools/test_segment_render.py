#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

import segment_plan
import segment_render
from test_segment_plan import plan_for, write_srt


class SegmentRenderContractTest(unittest.TestCase):
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
            scenes.append(
                {
                    "scene_id": f"s{index + 1}",
                    "start_seconds": start,
                    "end_seconds": end,
                    "visual_events": [
                        {
                            "event_id": f"e{index + 1}",
                            "start_seconds": start,
                            "end_seconds": end,
                        }
                    ],
                }
            )
        (self.project / "storyboard-final-timed.json").write_text(
            json.dumps({"schema": "haru.storyboard_timed.v1", "scenes": scenes}),
            encoding="utf-8",
        )
        self.plan = plan_for(self.project)
        self.write_plan()
        remotion = self.project / "remotion"
        remotion.mkdir()
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "composition": "Demo",
                    "remotion_dir": "remotion",
                }
            ),
            encoding="utf-8",
        )
        (self.project / ".hvp").mkdir()
        self.expected_record = None
        self.original_stream_profile = segment_render.probe_stream_profile
        segment_render.probe_stream_profile = self.fake_stream_profile

    def tearDown(self):
        segment_render.probe_stream_profile = self.original_stream_profile
        self.temporary.cleanup()

    def write_plan(self):
        (self.project / segment_plan.PLAN_PATH).write_text(
            json.dumps(self.plan), encoding="utf-8"
        )

    def refresh_input_bindings(self):
        inputs = {
            name: {
                "path": relative,
                "sha256": segment_render.sha256(self.project / relative),
            }
            for name, relative in segment_plan.INPUT_PATHS.items()
        }
        self.plan["inputs"] = inputs
        self.plan["render_input_sha256"] = segment_plan._render_input_digest(inputs)
        self.write_plan()

    def edit_storyboard_segment(self, ordinal):
        path = self.project / segment_plan.INPUT_PATHS["storyboard"]
        storyboard = json.loads(path.read_text(encoding="utf-8"))
        storyboard["scenes"][ordinal - 1]["visual_direction"] = f"revision-{ordinal}"
        path.write_text(json.dumps(storyboard), encoding="utf-8")
        self.refresh_input_bindings()

    def move_first_boundary(self):
        storyboard_path = self.project / segment_plan.INPUT_PATHS["storyboard"]
        storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))
        storyboard["scenes"][0]["end_seconds"] = 11
        storyboard["scenes"][0]["visual_events"][0]["end_seconds"] = 11
        storyboard["scenes"][1]["start_seconds"] = 11
        storyboard["scenes"][1]["visual_events"][0]["start_seconds"] = 11
        storyboard_path.write_text(json.dumps(storyboard), encoding="utf-8")
        (self.project / "narration-final.srt").write_text(
            "1\n00:00:00,000 --> 00:00:11,000\ncue 1\n\n"
            "2\n00:00:11,000 --> 00:00:20,000\ncue 2\n\n"
            "3\n00:00:20,000 --> 00:00:30,000\ncue 3\n\n"
            "4\n00:00:30,000 --> 00:00:40,000\ncue 4\n",
            encoding="utf-8",
        )
        self.plan["segments"][0]["end"]["seconds"] = 11
        self.plan["segments"][0]["selector"]["end_frame"] = 330
        self.plan["segments"][1]["start"]["seconds"] = 11
        self.plan["segments"][1]["selector"]["start_frame"] = 330
        self.refresh_input_bindings()

    def fake_renderer(self, remotion, composition, output, selector, home):
        self.assertEqual(composition, "Demo")
        self.assertEqual(selector, self.expected_record["selector"])
        output.write_bytes(f"{self.expected_record['segment_id']}-segment".encode())

    def fake_evidence(self, project, record, video, duration):
        root = segment_render.prepare_review_dir(project, record["segment_id"])
        names = {
            "head": "head.jpg",
            "tail": "tail.jpg",
            "authored_transition": "authored-transition.jpg",
            "caption_window": "caption-window.jpg",
            "waveform_window": "waveform.png",
        }
        samples = []
        for kind, name in names.items():
            asset = root / name
            asset.write_bytes(f"{record['segment_id']}-{kind}".encode())
            sample = {
                "kind": kind,
                "seconds": 0.0,
                "path": str(segment_render.review_dir(record["segment_id"]) / name),
                "sha256": segment_render.sha256(asset),
                "bytes": asset.stat().st_size,
            }
            if kind == "waveform_window":
                sample["duration_seconds"] = duration
            samples.append(sample)
        return samples

    @staticmethod
    def fake_stream_profile(_path):
        return segment_render.normalize_stream_profile(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                        "pix_fmt": "yuv420p",
                        "frame_rate": "30/1",
                        "time_base": "1/15360",
                    },
                    {
                        "codec_type": "audio",
                        "codec_name": "aac",
                        "sample_rate": 48000,
                        "channels": 2,
                        "channel_layout": "stereo",
                        "sample_fmt": "fltp",
                        "time_base": "1/48000",
                    },
                ]
            }
        )

    def render(self):
        current = segment_render.current_segment(self.project)
        self.expected_record = current["record"]
        original_probe = segment_render.probe_duration
        original_decode = segment_render.decode_clean
        segment_render.probe_duration = lambda _path: 10.0
        segment_render.decode_clean = lambda _path: True
        try:
            return segment_render.render_current_segment(
                self.project,
                renderer=self.fake_renderer,
                evidence_generator=self.fake_evidence,
            )
        finally:
            segment_render.probe_duration = original_probe
            segment_render.decode_clean = original_decode

    def review(self, verdict="pass", notes="segment holds"):
        return segment_render.review_current_segment(
            self.project, "harvey", verdict, notes
        )

    def lifecycle(self):
        return segment_render.apply_lifecycle(
            segment_plan.validate(self.project), self.project
        )

    def approve_next(self):
        rendered = self.render()
        reviewed = self.review()
        return rendered, reviewed, self.lifecycle()

    def test_render_binds_current_definition_dependency_and_exact_output(self):
        receipt = self.render()
        video = self.project / "output/segments/01-qi.mp4"
        current = segment_plan.validate(self.project)["segments"][0]
        self.assertEqual(receipt["schema"], segment_render.RENDER_SCHEMA)
        self.assertEqual(receipt["segment_id"], "qi")
        self.assertEqual(receipt["output"], segment_plan.OUTPUTS["qi"])
        self.assertEqual(receipt["video_sha256"], segment_render.sha256(video))
        self.assertEqual(receipt["bytes"], video.stat().st_size)
        self.assertEqual(receipt["definition_sha256"], current["definition_sha256"])
        self.assertEqual(receipt["dependency_sha256"], current["dependency_sha256"])
        evidence = json.loads(
            (self.project / "quality-review/segments/qi/evidence.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(evidence["dependency_sha256"], current["dependency_sha256"])
        status = self.lifecycle()
        self.assertEqual(
            [item["status"] for item in status["segments"]],
            ["review_pending", "planned", "planned", "planned"],
        )
        self.assertEqual(status["next_actionable_segment"], "qi")

    def test_tail_evidence_stays_two_frames_inside_the_encoded_duration(self):
        record = segment_plan.validate(self.project)["segments"][0]
        offsets = segment_render.evidence_offsets(self.project, record, 10.0)
        self.assertAlmostEqual(offsets["tail"], 10.0 - (2 / 30))

    def test_four_segments_advance_only_after_sequential_current_approvals(self):
        for index, segment_id in enumerate(segment_plan.SEGMENTS):
            self.assertEqual(self.lifecycle()["next_actionable_segment"], segment_id)
            rendered, reviewed, status = self.approve_next()
            self.assertEqual(rendered["segment_id"], segment_id)
            self.assertEqual(reviewed["segment_id"], segment_id)
            expected_next = (
                segment_plan.SEGMENTS[index + 1]
                if index + 1 < len(segment_plan.SEGMENTS)
                else None
            )
            self.assertEqual(status["next_actionable_segment"], expected_next)
        self.assertEqual(
            [item["status"] for item in status["segments"]],
            ["approved", "approved", "approved", "approved"],
        )
        self.assertFalse(any(item["actionable"] for item in status["segments"]))
        with self.assertRaises(segment_render.SegmentError) as raised:
            segment_render.current_segment(self.project)
        self.assertEqual(raised.exception.code, "segment_not_actionable")

    def test_changes_requested_keeps_same_segment_actionable_for_rerender(self):
        self.render()
        first_review = self.review("changes_requested", "tighten the hook")
        self.assertEqual(first_review["segment_id"], "qi")
        status = self.lifecycle()
        self.assertEqual(status["next_actionable_segment"], "qi")
        self.assertEqual(status["segments"][0]["status"], "changes_requested")
        second_render = self.render()
        self.assertEqual(second_render["segment_id"], "qi")
        self.assertFalse(
            (self.project / "quality-review/segments/qi/review.json").exists()
        )
        self.assertEqual(self.lifecycle()["segments"][0]["status"], "review_pending")

    def test_noncanonical_review_timestamp_never_approves(self):
        self.render()
        self.review()
        review_path = self.project / "quality-review/segments/qi/review.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["reviewed_at"] = "2026-08-13T00:00:00Z"
        review_path.write_text(json.dumps(review), encoding="utf-8")
        status = self.lifecycle()
        self.assertEqual(status["segments"][0]["status"], "review_pending")
        self.assertEqual(status["next_actionable_segment"], "qi")

    def test_local_edit_stales_only_its_approval(self):
        for _segment in segment_plan.SEGMENTS:
            self.approve_next()
        before = self.lifecycle()
        self.edit_storyboard_segment(3)
        after = self.lifecycle()
        self.assertEqual(
            [item["status"] for item in after["segments"]],
            ["approved", "approved", "planned", "approved"],
        )
        self.assertEqual(after["next_actionable_segment"], "zhuan")
        self.assertEqual(
            before["segments"][0]["dependency_sha256"],
            after["segments"][0]["dependency_sha256"],
        )
        self.assertNotEqual(
            before["segments"][2]["dependency_sha256"],
            after["segments"][2]["dependency_sha256"],
        )

    def test_srt_text_edit_stales_only_its_owner(self):
        for _segment in segment_plan.SEGMENTS:
            self.approve_next()
        (self.project / "narration-final.srt").write_text(
            "1\n00:00:00,000 --> 00:00:10,000\ncue 1\n\n"
            "2\n00:00:10,000 --> 00:00:20,000\nrevised cue 2\n\n"
            "3\n00:00:20,000 --> 00:00:30,000\ncue 3\n\n"
            "4\n00:00:30,000 --> 00:00:40,000\ncue 4\n",
            encoding="utf-8",
        )
        self.refresh_input_bindings()
        status = self.lifecycle()
        self.assertEqual(
            [item["status"] for item in status["segments"]],
            ["approved", "planned", "approved", "approved"],
        )
        self.assertEqual(status["next_actionable_segment"], "cheng")

    def test_global_edit_stales_all_four_approvals(self):
        for _segment in segment_plan.SEGMENTS:
            self.approve_next()
        (self.project / "narration-final.mp3").write_bytes(b"new narration")
        self.refresh_input_bindings()
        status = self.lifecycle()
        self.assertEqual(
            [item["status"] for item in status["segments"]],
            ["planned", "planned", "planned", "planned"],
        )
        self.assertEqual(status["next_actionable_segment"], "qi")

    def test_unowned_storyboard_authority_stales_all_four_approvals(self):
        for _segment in segment_plan.SEGMENTS:
            self.approve_next()
        storyboard_path = self.project / segment_plan.INPUT_PATHS["storyboard"]
        storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))
        storyboard["global_visual_direction"] = "replace palette across all acts"
        storyboard_path.write_text(json.dumps(storyboard), encoding="utf-8")
        self.refresh_input_bindings()
        status = self.lifecycle()
        self.assertEqual(
            [item["status"] for item in status["segments"]],
            ["planned", "planned", "planned", "planned"],
        )
        self.assertEqual(status["next_actionable_segment"], "qi")

    def test_boundary_timing_edit_stales_all_four_approvals(self):
        for _segment in segment_plan.SEGMENTS:
            self.approve_next()
        self.move_first_boundary()
        status = self.lifecycle()
        self.assertEqual(
            [item["status"] for item in status["segments"]],
            ["planned", "planned", "planned", "planned"],
        )
        self.assertEqual(status["next_actionable_segment"], "qi")

    def test_later_unapproved_local_edit_preserves_earlier_approval(self):
        self.approve_next()
        before = self.lifecycle()
        self.edit_storyboard_segment(4)
        after = self.lifecycle()
        self.assertEqual(after["segments"][0]["status"], "approved")
        self.assertEqual(after["next_actionable_segment"], "cheng")
        self.assertEqual(
            before["segments"][0]["dependency_sha256"],
            after["segments"][0]["dependency_sha256"],
        )

    def test_stale_or_tampered_receipts_never_advance_or_review(self):
        receipt = self.render()
        render_path = self.project / "quality-review/segments/qi/render.json"
        receipt["dependency_sha256"] = "0" * 64
        render_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.assertEqual(self.lifecycle()["segments"][0]["status"], "planned")
        with self.assertRaises(segment_render.SegmentError) as raised:
            self.review()
        self.assertEqual(raised.exception.code, "segment_stale")

        receipt = self.render()
        evidence_path = self.project / "quality-review/segments/qi/evidence.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["samples"].pop()
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        receipt["evidence"]["sha256"] = segment_render.sha256(evidence_path)
        render_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.assertEqual(self.lifecycle()["segments"][0]["status"], "planned")

    def test_stale_video_bytes_cannot_be_reviewed(self):
        self.render()
        (self.project / segment_plan.OUTPUTS["qi"]).write_bytes(b"tampered")
        with self.assertRaises(segment_render.SegmentError) as raised:
            self.review()
        self.assertEqual(raised.exception.code, "segment_stale")

    def test_tampered_stream_profile_cannot_be_reviewed(self):
        receipt = self.render()
        receipt["stream_profile"]["streams"][0]["width"] = 1280
        render_path = self.project / "quality-review/segments/qi/render.json"
        render_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaises(segment_render.SegmentError) as raised:
            self.review()
        self.assertEqual(raised.exception.code, "segment_stale")

    def test_non_finite_render_duration_cannot_be_reviewed(self):
        receipt = self.render()
        receipt["duration_seconds"] = float("inf")
        render_path = self.project / "quality-review/segments/qi/render.json"
        render_path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaises(segment_render.SegmentError) as raised:
            self.review()
        self.assertEqual(raised.exception.code, "segment_stale")

    def test_video_only_segment_profile_is_rejected(self):
        profile = self.fake_stream_profile(None)
        profile["streams"].pop()
        with self.assertRaises(segment_render.SegmentError) as raised:
            segment_render.normalize_stream_profile(profile)
        self.assertEqual(raised.exception.code, "segment_unmeasurable")
        profile = self.fake_stream_profile(None)
        profile["streams"].append({"codec_type": "subtitle", "codec_name": "mov_text"})
        with self.assertRaises(segment_render.SegmentError) as extra_stream:
            segment_render.normalize_stream_profile(profile)
        self.assertEqual(extra_stream.exception.code, "segment_unmeasurable")

    def test_missing_plan_cannot_render(self):
        (self.project / segment_plan.PLAN_PATH).unlink()
        with self.assertRaises(segment_render.SegmentError) as raised:
            self.render()
        self.assertEqual(raised.exception.code, "segment_plan_invalid")


if __name__ == "__main__":
    unittest.main()
