#!/usr/bin/env python3
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import visual_qa_sample
from test_agent_status import seal_self_eval


def write_png(path, width=1920, height=1080):
    import struct
    import zlib

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\0" + b"\0" * (width * 3) for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def storyboard(project):
    return {
        "schema": "haru.storyboard_timed.v1",
        "project": project,
        "srt": "narration-final.srt",
        "scenes": [
            {
                "scene_id": "s01",
                "section": "cold-open",
                "start_seconds": 0,
                "end_seconds": 5,
                "visual_intent": "ConceptCard：cold open",
                "card": ["cold"],
            },
            {
                "scene_id": "s02",
                "section": "body",
                "start_seconds": 5,
                "end_seconds": 10,
                "visual_intent": "ComparePanel：motif",
                "card": ["body"],
                "motif": True,
            },
            {
                "scene_id": "s03",
                "section": "leopard-tail",
                "start_seconds": 10,
                "end_seconds": 15,
                "visual_intent": "callback",
            },
        ],
    }


def require_editorial_profile(project):
    """Put the fixture on the lane where editorial is an identity input."""
    (project / "project-contract.json").write_text(
        json.dumps(
            {
                "schema": "haru.project_contract.v1",
                "lane_contract": "social_issue_longform.v1",
                "production_profile": "host_longform.v1",
                "publish_target": {
                    "youtube_channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa"
                },
                "runtime_contract": {
                    "schema": "haru.project_runtime_contract.v1",
                    "runtime": "haru.runtime.v1",
                    "evaluator": "haru.evaluator.v1",
                    "artifact": "haru.artifact.v1",
                },
            }
        )
    )


SRT = """1
00:00:01,000 --> 00:00:02,000
cold

2
00:00:06,000 --> 00:00:07,000
body

3
00:00:12,000 --> 00:00:13,000
tail
"""


class VisualQaSampleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = (Path(self._tmp.name) / "demo").resolve()
        (self.project / "output").mkdir(parents=True)
        (self.project / "project-contract.json").write_text(
            json.dumps(
                {
                    "schema": "haru.project_contract.v1",
                    "lane_contract": "manual.v1",
                    "production_profile": None,
                }
            ),
            encoding="utf-8",
        )
        video = self.project / "output" / "final.mp4"
        video.write_bytes(b"final-video")
        digest = hashlib.sha256(video.read_bytes()).hexdigest()
        Path(str(video) + ".render-result").write_text(
            json.dumps(
                {
                    "schema": "haru.render_result.v1",
                    "status": "pass",
                    "project": "demo",
                    "output": "output/final.mp4",
                    "video_sha256": digest,
                    "bytes": video.stat().st_size,
                    "duration_seconds": 15,
                    "loudness_lufs": -14,
                    "true_peak_dbfs": -1,
                    "loudness_range_lu": 5,
                    "mix": {
                        "schema": "haru.final_mix.v1",
                        "method": "ffmpeg_loudnorm_two_pass",
                        "normalization_type": "linear",
                        "input_sha256": "1" * 64,
                        "target": {
                            "integrated_lufs": -14.0,
                            "true_peak_dbfs": -1.0,
                            "loudness_range_lu": 5,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.project / "storyboard-final-timed.json").write_text(
            json.dumps(storyboard("demo")), encoding="utf-8"
        )
        (self.project / "storyboard-final-timed-validation.json").write_text(
            json.dumps(
                {
                    "schema": "haru.storyboard_validation.v1",
                    "ok": True,
                    "checks": [{"name": "every_scene_matched", "status": "pass"}],
                }
            ),
            encoding="utf-8",
        )
        (self.project / "narration-final.srt").write_text(SRT, encoding="utf-8")
        # The whole-video sampler is downstream of HVP-33 and must refuse even a
        # perfectly shaped render marker until the external ledger anchors a
        # current self-eval pass for these exact bytes.
        seal_self_eval(self.project, Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def generate_sample(self):
        def fake_contact_sheet(_ffmpeg, _video, _frames, output):
            write_png(output)

        with mock.patch.object(
            visual_qa_sample, "extract_contact_sheet", fake_contact_sheet
        ):
            return visual_qa_sample.generate(self.project, ffmpeg="ffmpeg")

    def test_plan_samples_inside_cues_and_pairs_open_with_tail(self):
        frames = visual_qa_sample.plan_samples(
            storyboard("demo")["scenes"],
            visual_qa_sample.parse_srt(SRT),
            columns=4,
        )

        self.assertEqual([frame["scene_id"] for frame in frames], ["s01", "s03", "s02"])
        self.assertEqual(
            [frame["timestamp_seconds"] for frame in frames], [1.5, 12.5, 6.5]
        )
        self.assertEqual(frames[0]["coordinate"], {"row": 0, "column": 0})
        self.assertEqual(frames[1]["coordinate"], {"row": 0, "column": 1})
        self.assertIn("cold_open_callback", frames[0]["reasons"])
        self.assertIn("cold_open_callback", frames[1]["reasons"])
        self.assertIn("motif", frames[2]["reasons"])
        self.assertIn("card_type:ComparePanel", frames[2]["reasons"])

    def test_plan_samples_adds_one_frame_for_each_unique_editorial_asset(self):
        shots = [
            {
                "event_id": "open-mina",
                "start_seconds": 1,
                "end_seconds": 2,
                "composition": "aroll_full",
                "asset_path": "assets/mina.mp4",
                "asset_sha256": "a" * 64,
            },
            {
                "event_id": "body-broll",
                "start_seconds": 6,
                "end_seconds": 7,
                "composition": "broll_full",
                "asset_path": "assets/server.mp4",
                "asset_sha256": "b" * 64,
            },
            {
                "event_id": "tail-broll-repeat",
                "start_seconds": 12,
                "end_seconds": 13,
                "composition": "broll_pip",
                "asset_path": "assets/server.mp4",
                "asset_sha256": "b" * 64,
                "presenter_asset_path": "assets/mina-listening.mp4",
                "presenter_asset_sha256": "c" * 64,
            },
        ]

        frames = visual_qa_sample.plan_samples(
            storyboard("demo")["scenes"],
            visual_qa_sample.parse_srt(SRT),
            columns=4,
            editorial_shots=shots,
        )

        asset_frames = [frame for frame in frames if "asset" in frame["reasons"]]
        self.assertEqual(
            [frame["asset_sha256"] for frame in asset_frames],
            ["a" * 64, "b" * 64, "c" * 64],
        )
        self.assertEqual(
            [frame["event_id"] for frame in asset_frames],
            ["open-mina", "body-broll", "tail-broll-repeat"],
        )

    def test_current_inputs_prefers_canonical_and_rejects_a_legacy_only_final(self):
        """Discovery is not authority.

        The pre-HVP-33 sampler could fall back to a revisioned legacy video.
        HVP-33 intentionally keeps discovery for diagnostics, then refuses the
        fallback: the self-eval gate is defined on exactly output/final.mp4 and a
        human review must never cover bytes it did not evaluate.
        """
        canonical = visual_qa_sample.current_inputs(self.project)
        self.assertEqual(
            canonical["paths"]["final_video"], self.project / "output/final.mp4"
        )

        video = self.project / "output/final.mp4"
        marker = Path(str(video) + ".render-result")
        legacy = self.project / "output/demo-final-v1.mp4"
        legacy_marker = Path(str(legacy) + ".render-result")
        legacy.write_bytes(video.read_bytes())
        marker_data = json.loads(marker.read_text())
        marker_data["output"] = "output/demo-final-v1.mp4"
        legacy_marker.write_text(json.dumps(marker_data))
        review_dir = self.project / "quality-review/final-v1"
        review_dir.mkdir(parents=True)
        (review_dir / "review.json").write_text(
            json.dumps({"video": "output/demo-final-v1.mp4"})
        )

        preferred = visual_qa_sample.current_inputs(self.project.resolve())
        self.assertEqual(
            preferred["paths"]["final_video"], video.resolve()
        )

        video.unlink()
        marker.unlink()

        with self.assertRaises(visual_qa_sample.SelfEvalNotCurrent):
            visual_qa_sample.current_inputs(self.project.resolve())
        with self.assertRaises(visual_qa_sample.SelfEvalNotCurrent):
            visual_qa_sample.current_inputs(
                self.project.resolve(), expected_video_path=legacy
            )

    def test_current_inputs_binds_optional_editorial_contract_to_render(self):
        # Move the fixture onto the profile where editorial is authoritative;
        # an editorial file in the legacy/manual lane is intentionally refused
        # by self-eval rather than treated as an optional extra input.
        require_editorial_profile(self.project)
        contract = self.project / "editorial-contract.json"
        contract.write_text(
            json.dumps(
                {
                    "schema": "haru.editorial_contract.v1",
                    "project": "demo",
                    "shots": [],
                }
            )
        )
        marker = self.project / "output/final.mp4.render-result"
        value = json.loads(marker.read_text())
        value["editorial_contract_sha256"] = hashlib.sha256(contract.read_bytes()).hexdigest()
        marker.write_text(json.dumps(value))
        seal_self_eval(self.project, Path(self._tmp.name), attempt=2)

        inputs = visual_qa_sample.current_inputs(self.project)

        self.assertEqual(
            inputs["digests"]["editorial_contract"]["sha256"],
            value["editorial_contract_sha256"],
        )
        self.assertEqual(inputs["editorial_contract"]["shots"], [])

    def test_sample_needs_a_human_digest_bound_verdict(self):
        def fake_contact_sheet(_ffmpeg, _video, frames, output):
            self.assertEqual(len(frames), 3)
            write_png(output)

        with mock.patch.object(
            visual_qa_sample, "extract_contact_sheet", fake_contact_sheet
        ):
            receipt = visual_qa_sample.generate(self.project, ffmpeg="ffmpeg")

        self.assertEqual(receipt["schema"], "haru.visual_qa_sample.v2")
        self.assertTrue(visual_qa_sample.validate_sample(self.project)["ok"])
        self.assertFalse(visual_qa_sample.validate_review(self.project)["ok"])

        visual_qa_sample.record_review(
            self.project,
            reviewed_by="harvey",
            verdict="pass",
            notes="Checked every sampled frame; no production notes are visible.",
        )
        self.assertTrue(visual_qa_sample.validate_review(self.project)["ok"])

        sheet = (
            self.project
            / "quality-review"
            / "visual-sampling"
            / "visual-qa-contact-sheet.png"
        )
        sheet.write_bytes(sheet.read_bytes() + b"tampered")
        self.assertFalse(visual_qa_sample.validate_review(self.project)["ok"])

    def test_whole_video_sampling_refuses_when_the_external_pass_anchor_is_gone(self):
        """A render marker plus a project-tree pass receipt is not authority."""
        shutil.rmtree(Path(self._tmp.name) / ".self-eval-state")

        with self.assertRaises(visual_qa_sample.SelfEvalNotCurrent):
            self.generate_sample()

        result = visual_qa_sample.validate_sample(self.project)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "self_eval_not_current")

    def test_old_v1_sample_and_review_receipts_are_rejected(self):
        """The schema cutover is clean; no compatibility reader survives."""
        self.generate_sample()
        sample_path = (
            self.project / visual_qa_sample.OUTPUT_DIR / visual_qa_sample.SAMPLE_NAME
        )
        sample = json.loads(sample_path.read_text(encoding="utf-8"))
        sample["schema"] = "haru.visual_qa_sample.v1"
        sample_path.write_text(json.dumps(sample), encoding="utf-8")
        self.assertEqual(
            visual_qa_sample.validate_sample(self.project)["code"],
            "sample_receipt_invalid",
        )

        # Restore the v2 sample and record a valid v2 review, then prove the old
        # review schema is rejected independently of the sample.
        self.generate_sample()
        visual_qa_sample.record_review(
            self.project,
            reviewed_by="harvey",
            verdict="pass",
            notes="Checked the complete v2 contact sheet.",
        )
        review_path = (
            self.project / visual_qa_sample.OUTPUT_DIR / visual_qa_sample.REVIEW_NAME
        )
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["schema"] = "haru.visual_qa_review.v1"
        review_path.write_text(json.dumps(review), encoding="utf-8")
        self.assertEqual(
            visual_qa_sample.validate_review(self.project)["code"],
            "review_invalid",
        )

    def test_unknown_v2_review_keys_are_rejected(self):
        """Exact means exact; an extra authority-looking field is not inert."""
        self.generate_sample()
        visual_qa_sample.record_review(
            self.project,
            reviewed_by="harvey",
            verdict="pass",
            notes="Checked the complete v2 contact sheet.",
        )
        review_path = (
            self.project / visual_qa_sample.OUTPUT_DIR / visual_qa_sample.REVIEW_NAME
        )
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["approved_by"] = "caller-asserted-authority"
        review_path.write_text(json.dumps(review), encoding="utf-8")

        self.assertEqual(
            visual_qa_sample.validate_review(self.project)["code"],
            "review_invalid",
        )

    def test_a_new_self_eval_pass_never_resurrects_the_old_human_review(self):
        """Pass B is not evidence that a review of pass A still applies."""
        self.generate_sample()
        old_review = visual_qa_sample.record_review(
            self.project,
            reviewed_by="harvey",
            verdict="pass",
            notes="Reviewed the first current self-eval pass.",
        )
        old_ref = old_review["render_self_eval"]
        self.assertTrue(visual_qa_sample.validate_review(self.project)["ok"])

        # SRT text is one of the fixed identity inputs. The timings stay valid,
        # so the only meaningful change here is which immutable attempt is
        # current; the engine seals it as attempt 2.
        srt = self.project / "narration-final.srt"
        srt.write_text(SRT.replace("body", "body changed"), encoding="utf-8")
        current = seal_self_eval(self.project, Path(self._tmp.name), attempt=2)
        self.assertEqual(current["attempt"], 2)

        result = visual_qa_sample.validate_review(self.project)
        self.assertFalse(result["ok"])
        self.assertNotEqual(
            old_ref,
            {
                "path": "quality-review/render-self-eval/render-self-eval.json",
                "sha256": hashlib.sha256(
                    (
                        self.project
                        / "quality-review/render-self-eval/render-self-eval.json"
                    ).read_bytes()
                ).hexdigest(),
                "bytes": (
                    self.project
                    / "quality-review/render-self-eval/render-self-eval.json"
                ).stat().st_size,
            },
        )
        self.assertEqual(
            old_review,
            json.loads(
                (
                    self.project
                    / visual_qa_sample.OUTPUT_DIR
                    / visual_qa_sample.REVIEW_NAME
                ).read_text(encoding="utf-8")
            ),
            "a new pass must not rewrite the old HVP-21 receipt into looking current",
        )

    def test_sample_binds_editorial_contract_and_covers_unique_assets(self):
        require_editorial_profile(self.project)
        contract = self.project / "editorial-contract.json"
        contract.write_text(
            json.dumps(
                {
                    "schema": "haru.editorial_contract.v1",
                    "project": "demo",
                    "shots": [
                        {"event_id": "open", "start_seconds": 1, "end_seconds": 2, "composition": "aroll_full", "asset_path": "assets/mina.mp4", "asset_sha256": "a" * 64},
                        {"event_id": "body", "start_seconds": 6, "end_seconds": 7, "composition": "broll_full", "asset_path": "assets/server.mp4", "asset_sha256": "b" * 64},
                        {"event_id": "tail", "start_seconds": 12, "end_seconds": 13, "composition": "broll_pip", "asset_path": "assets/server.mp4", "asset_sha256": "b" * 64},
                    ],
                }
            )
        )
        marker = self.project / "output/final.mp4.render-result"
        value = json.loads(marker.read_text())
        value["editorial_contract_sha256"] = hashlib.sha256(contract.read_bytes()).hexdigest()
        marker.write_text(json.dumps(value))
        seal_self_eval(self.project, Path(self._tmp.name), attempt=2)

        def fake_contact_sheet(_ffmpeg, _video, frames, output):
            self.assertEqual(len(frames), 5)
            write_png(output)

        with mock.patch.object(visual_qa_sample, "extract_contact_sheet", fake_contact_sheet):
            receipt = visual_qa_sample.generate(self.project, ffmpeg="ffmpeg")

        self.assertEqual(
            len([frame for frame in receipt["frames"] if "asset" in frame["reasons"]]), 2
        )
        self.assertTrue(visual_qa_sample.validate_sample(self.project)["ok"])
        contract.write_text(contract.read_text() + "\n")
        self.assertFalse(visual_qa_sample.validate_sample(self.project)["ok"])

    def test_scene_without_a_cue_fails_closed(self):
        cues = visual_qa_sample.parse_srt(SRT)
        scenes = storyboard("demo")["scenes"]
        scenes[1]["start_seconds"] = 8
        scenes[1]["end_seconds"] = 9

        with self.assertRaisesRegex(ValueError, "cue-covered"):
            visual_qa_sample.plan_samples(scenes, cues, columns=4)

    def test_zero_length_punctuation_cue_is_ignored(self):
        cues = visual_qa_sample.parse_srt(
            SRT + "\n\n4\n00:00:15,000 --> 00:00:15,000\n。\n"
        )

        self.assertEqual([cue["index"] for cue in cues], [1, 2, 3])

    def test_real_motion_graphic_template_is_the_card_type(self):
        scenes = storyboard("demo")["scenes"]
        scenes[1].pop("card")
        scenes[1]["source"] = "compare_panel"
        scenes[1]["motion_graphic"] = {"template": "compare_panel"}

        frames = visual_qa_sample.plan_samples(
            scenes,
            visual_qa_sample.parse_srt(SRT),
            visual_motif="declared motif",
        )
        body = next(frame for frame in frames if frame["scene_id"] == "s02")

        self.assertEqual(body["card_type"], "compare_panel")
        self.assertIn("card_type:compare_panel", body["reasons"])
        self.assertIn("motif", body["reasons"])

    def test_declared_motif_without_marked_scenes_fails_closed(self):
        scenes = storyboard("demo")["scenes"]
        scenes[1].pop("motif")

        with self.assertRaisesRegex(ValueError, "marks no motif scenes"):
            visual_qa_sample.plan_samples(
                scenes,
                visual_qa_sample.parse_srt(SRT),
                visual_motif="treadmill",
            )


if __name__ == "__main__":
    unittest.main()
