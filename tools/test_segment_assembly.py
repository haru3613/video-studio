#!/usr/bin/env python3
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

import render_contract
import segment_assembly
import render_project
import segment_plan
import segment_render
import render_project_worker
import test_segment_render


class SegmentAssemblyTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_segment_render.SegmentRenderContractTest(
            "test_render_binds_current_definition_dependency_and_exact_output"
        )
        self.fixture.setUp()
        self.project = self.fixture.project

    def tearDown(self):
        self.fixture.tearDown()

    def approve_all(self):
        for _ in segment_plan.SEGMENTS:
            self.fixture.approve_next()

    @staticmethod
    def profile(duration=10.0, width=1920, video_codec="h264"):
        return {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": video_codec,
                    "time_base": "1/15360",
                    "width": width,
                    "height": 1080,
                    "pix_fmt": "yuv420p",
                    "r_frame_rate": "30/1",
                    "avg_frame_rate": "30/1",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "time_base": "1/48000",
                    "sample_rate": "48000",
                    "channels": 2,
                    "channel_layout": "stereo",
                    "sample_fmt": "fltp",
                },
            ],
            "duration_seconds": duration,
        }

    def assembled(
        self,
        incompatible=False,
        decode=True,
        timestamps=True,
        output_duration=40.0,
        video_codec="h264",
    ):
        source_names = {Path(value).name for value in segment_plan.OUTPUTS.values()}

        def probe(path, runner=None):
            width = 1280 if incompatible and path.name == "02-cheng.mp4" else 1920
            duration = 10.0 if path.name in source_names else output_duration
            return self.profile(duration, width, video_codec)

        def run(command, runner=None):
            Path(command[-1]).write_bytes(b"assembled-premix")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            mock.patch.object(segment_assembly, "probe_media", side_effect=probe),
            mock.patch.object(segment_assembly, "decode_clean", return_value=decode),
            mock.patch.object(
                segment_assembly,
                "timestamp_evidence",
                return_value={
                    "packet_dts_monotonic": timestamps,
                    "frame_pts_monotonic": timestamps,
                },
            ),
            mock.patch.object(segment_assembly, "_run", side_effect=run),
        ):
            return segment_assembly.assemble(self.project)

    def test_all_approved_compatible_segments_use_fixed_concat_copy(self):
        self.approve_all()
        receipt = self.assembled()
        self.assertEqual(receipt["method"], segment_assembly.METHOD_COPY)
        self.assertEqual(
            [item["segment_id"] for item in receipt["segments"]],
            list(segment_plan.SEGMENTS),
        )
        self.assertIsNotNone(segment_assembly.current_receipt(self.project))

    def test_compatible_non_h264_segments_use_current_concat_copy(self):
        segment_render.probe_stream_profile = lambda _path: (
            segment_render.normalize_stream_profile(self.profile(video_codec="mpeg4"))
        )
        self.approve_all()
        receipt = self.assembled(video_codec="mpeg4")
        self.assertEqual(receipt["method"], segment_assembly.METHOD_COPY)
        self.assertTrue(segment_assembly.output_stream_profile_current(receipt))
        self.assertIsNotNone(segment_assembly.current_receipt(self.project))

    def test_current_assembly_is_idempotent_and_stale_receipt_is_replaced(self):
        self.approve_all()
        original = self.assembled()
        with mock.patch.object(
            segment_assembly, "_run", side_effect=AssertionError("must not rerun")
        ):
            self.assertEqual(segment_assembly.assemble(self.project), original)
        receipt_path = self.project / segment_assembly.RECEIPT_PATH
        stale = json.loads(receipt_path.read_text())
        stale["method"] = segment_assembly.METHOD_REENCODE
        receipt_path.write_text(json.dumps(stale))
        self.assertIsNone(segment_assembly.current_receipt(self.project))
        replacement = self.assembled()
        self.assertEqual(replacement["method"], segment_assembly.METHOD_COPY)
        self.assertIsNotNone(segment_assembly.current_receipt(self.project))

    def test_coordinated_profile_and_method_tamper_is_not_current(self):
        self.approve_all()
        self.assembled()
        receipt_path = self.project / segment_assembly.RECEIPT_PATH
        tampered = json.loads(receipt_path.read_text())
        tampered["source_stream_profiles"][1]["profile"]["streams"][0]["width"] = 1280
        tampered["method"] = segment_assembly.METHOD_REENCODE
        receipt_path.write_text(json.dumps(tampered))
        self.assertIsNone(segment_assembly.current_receipt(self.project))
        replacement = self.assembled()
        self.assertEqual(replacement["method"], segment_assembly.METHOD_COPY)

    def test_malformed_nested_receipt_values_fail_closed(self):
        self.approve_all()
        receipt = self.assembled()
        malformed_profiles = json.loads(json.dumps(receipt))
        for entry in malformed_profiles["source_stream_profiles"]:
            entry["profile"] = {"streams": []}
        self.assertFalse(
            segment_assembly.receipt_shape_ok(self.project, malformed_profiles)
        )
        receipt_path = self.project / segment_assembly.RECEIPT_PATH
        receipt_path.write_text(json.dumps(malformed_profiles))
        self.assertIsNone(segment_assembly.current_receipt(self.project))
        self.assertIsNotNone(self.assembled())
        malformed_evidence = json.loads(json.dumps(receipt))
        malformed_evidence["decode_evidence"] = []
        self.assertFalse(
            segment_assembly.receipt_shape_ok(self.project, malformed_evidence)
        )
        receipt_path.write_text(json.dumps(malformed_evidence))
        self.assertIsNone(segment_assembly.current_receipt(self.project))
        self.assertIsNotNone(self.assembled())
        malformed_segments = json.loads(json.dumps(receipt))
        malformed_segments["segments"][0].pop("video_sha256")
        self.assertFalse(
            segment_assembly.receipt_shape_ok(
                self.project, malformed_segments, require_output=False
            )
        )
        malformed_source_receipts = json.loads(json.dumps(receipt))
        malformed_source_receipts["source_receipts"] = [None, None, None, None]
        receipt_path.write_text(json.dumps(malformed_source_receipts))
        marker = {
            "mix": {"input_sha256": receipt["output_sha256"]},
            "assembly": {
                "schema": segment_assembly.SCHEMA,
                "path": segment_assembly.RECEIPT_PATH,
                "sha256": segment_assembly.sha256(receipt_path),
            },
        }
        self.assertFalse(
            segment_assembly.receipt_shape_ok(
                self.project, malformed_source_receipts, require_output=False
            )
        )
        self.assertEqual(
            render_contract.assembly_binding_state(self.project, marker), "invalid"
        )
        self.assertIsNotNone(self.assembled())
        malformed_output_profile = json.loads(json.dumps(receipt))
        malformed_output_profile["output_stream_profile"] = {}
        receipt_path.write_text(json.dumps(malformed_output_profile))
        self.assertIsNone(segment_assembly.current_receipt(self.project))
        self.assertIsNotNone(self.assembled())
        nested_output_profile = json.loads(json.dumps(receipt))
        nested_output_profile["output_stream_profile"]["streams"][0]["extra"] = True
        receipt_path.write_text(json.dumps(nested_output_profile))
        self.assertIsNone(segment_assembly.current_receipt(self.project))
        self.assertIsNotNone(self.assembled())
        top_level_output_profile = json.loads(json.dumps(receipt))
        top_level_output_profile["output_stream_profile"]["unexpected"] = True
        receipt_path.write_text(json.dumps(top_level_output_profile))
        self.assertFalse(
            segment_assembly.receipt_shape_ok(
                self.project, top_level_output_profile, require_output=False
            )
        )
        self.assertIsNone(segment_assembly.current_receipt(self.project))
        self.assertIsNotNone(self.assembled())
        wrong_tolerance = json.loads(json.dumps(receipt))
        wrong_tolerance["decode_evidence"]["frame_tolerance_seconds"] = 0.5
        receipt_path.write_text(json.dumps(wrong_tolerance))
        self.assertIsNone(segment_assembly.current_receipt(self.project))
        self.assertIsNotNone(self.assembled())
        non_finite_duration = json.loads(json.dumps(receipt))
        non_finite_duration["duration_seconds"] = float("inf")
        non_finite_duration["output_stream_profile"]["duration_seconds"] = float("inf")
        receipt_path.write_text(json.dumps(non_finite_duration))
        self.assertIsNone(segment_assembly.current_receipt(self.project))
        self.assertIsNotNone(self.assembled())

    def test_incompatible_streams_force_fixed_single_thread_reencode(self):
        segment_render.probe_stream_profile = lambda path: (
            segment_render.normalize_stream_profile(
                self.profile(width=1280 if path.name == "02-cheng.mp4" else 1920)
            )
        )
        self.approve_all()
        receipt = self.assembled(incompatible=True)
        self.assertEqual(receipt["method"], segment_assembly.METHOD_REENCODE)
        command = segment_assembly.assembly_command(
            Path("list"),
            Path("out"),
            receipt["method"],
            receipt["target_profile"],
            [Path(f"segment-{index}.mp4") for index in range(4)],
        )
        self.assertIn("libx264", command)
        self.assertEqual(command[command.index("-threads") + 1], "1")
        self.assertIn("-map_metadata", command)
        self.assertNotIn("concat", command[: command.index("-filter_complex")])
        self.assertIn(
            "concat=n=4:v=1:a=1",
            command[command.index("-filter_complex") + 1],
        )

    def test_missing_or_unapproved_segment_blocks(self):
        with self.assertRaises(segment_assembly.AssemblyError) as raised:
            segment_assembly.capture_approved_snapshot(self.project)
        self.assertEqual(raised.exception.code, "segments_not_approved")
        self.approve_all()
        (self.project / segment_plan.OUTPUTS["zhuan"]).unlink()
        with self.assertRaises(segment_assembly.AssemblyError):
            segment_assembly.capture_approved_snapshot(self.project)

    def test_reordered_or_unknown_policy_plan_blocks(self):
        self.fixture.plan["segments"].reverse()
        self.fixture.write_plan()
        with self.assertRaises(segment_assembly.AssemblyError):
            segment_assembly.capture_approved_snapshot(self.project)
        self.fixture.plan["segments"].reverse()
        self.fixture.plan["assembly"]["transition_policy"] = "crossfade.v1"
        self.fixture.write_plan()
        self.assertEqual(
            segment_plan.validate(self.project)["mode"], "invalid_segment_plan"
        )

    def test_replaced_or_stale_segment_blocks(self):
        self.approve_all()
        video = self.project / segment_plan.OUTPUTS["qi"]
        video.write_bytes(b"replacement")
        with self.assertRaises(segment_assembly.AssemblyError):
            segment_assembly.capture_approved_snapshot(self.project)

    def test_stale_review_receipt_blocks(self):
        self.approve_all()
        review = self.project / segment_render.review_dir("cheng") / "review.json"
        value = json.loads(review.read_text())
        value["video_sha256"] = "0" * 64
        review.write_text(json.dumps(value))
        with self.assertRaises(segment_assembly.AssemblyError):
            segment_assembly.capture_approved_snapshot(self.project)

    def test_decode_duration_and_timestamp_fail_closed(self):
        self.approve_all()
        with self.assertRaises(segment_assembly.AssemblyError) as decode:
            self.assembled(decode=False)
        self.assertEqual(decode.exception.code, "assembly_decode_failed")
        self.assertFalse((self.project / segment_assembly.OUTPUT_PATH).exists())
        with self.assertRaises(segment_assembly.AssemblyError) as duration:
            self.assembled(output_duration=39.0)
        self.assertEqual(duration.exception.code, "assembly_duration_failed")
        with self.assertRaises(segment_assembly.AssemblyError) as timestamp:
            self.assembled(timestamps=False)
        self.assertEqual(timestamp.exception.code, "assembly_timestamp_failed")

    def test_output_and_receipt_tamper_fail_currentity(self):
        self.approve_all()
        self.assembled()
        output = self.project / segment_assembly.OUTPUT_PATH
        output.write_bytes(output.read_bytes() + b"tamper")
        self.assertIsNone(segment_assembly.current_receipt(self.project))
        output.unlink()
        (self.project / segment_assembly.RECEIPT_PATH).unlink()
        self.assembled()
        receipt_path = self.project / segment_assembly.RECEIPT_PATH
        value = json.loads(receipt_path.read_text())
        value["method"] = "other"
        receipt_path.write_text(json.dumps(value))
        self.assertIsNone(segment_assembly.current_receipt(self.project))

    def test_final_result_requires_exact_current_assembly_binding(self):
        self.approve_all()
        receipt = self.assembled()
        receipt_path = self.project / segment_assembly.RECEIPT_PATH
        marker = {
            "mix": {"input_sha256": receipt["output_sha256"]},
            "assembly": {
                "schema": segment_assembly.SCHEMA,
                "path": segment_assembly.RECEIPT_PATH,
                "sha256": segment_assembly.sha256(receipt_path),
            },
        }
        self.assertEqual(
            render_contract.assembly_binding_state(self.project, marker), "current"
        )
        marker["assembly"]["sha256"] = "0" * 64
        self.assertEqual(
            render_contract.assembly_binding_state(self.project, marker), "invalid"
        )
        marker.pop("assembly")
        self.assertFalse(render_contract.describes_current_inputs(self.project, marker))

    def test_reassembly_archives_old_binding_and_final_retirement_preserves_current_assembly(
        self,
    ):
        self.approve_all()
        original = self.assembled()
        receipt_path = self.project / segment_assembly.RECEIPT_PATH
        original_sha256 = segment_assembly.sha256(receipt_path)
        marker = {
            "mix": {"input_sha256": original["output_sha256"]},
            "assembly": {
                "schema": segment_assembly.SCHEMA,
                "path": segment_assembly.RECEIPT_PATH,
                "sha256": original_sha256,
            },
        }
        review_path = self.project / "quality-review/segments/he/review.json"
        review = json.loads(review_path.read_text())
        review["notes"] = "same approved segment, refreshed review note"
        review_path.write_text(json.dumps(review))
        self.assertIsNone(segment_assembly.current_receipt(self.project))

        self.assembled()
        archived = receipt_path.with_name(
            f"{receipt_path.name}.superseded-{original_sha256[:12]}"
        )
        self.assertEqual(segment_assembly.sha256(archived), original_sha256)
        self.assertEqual(
            render_contract.assembly_binding_state(self.project, marker), "stale"
        )

        final = self.project / "output/final.mp4"
        final.write_bytes(b"old final")
        marker.update(
            {
                "schema": render_contract.RENDER_SCHEMA,
                "status": "render_complete",
                "render_input_revision": render_contract.render_input_revision(
                    self.project
                ),
                "project": self.project.name,
                "output": "output/final.mp4",
                "video_sha256": segment_assembly.sha256(final),
                "bytes": final.stat().st_size,
                "duration_seconds": 40.0,
                "loudness_lufs": -14.0,
                "true_peak_dbfs": -1.1,
                "loudness_range_lu": 1.0,
            }
        )
        marker["mix"].update(
            {
                "schema": render_contract.MIX_SCHEMA,
                "method": "ffmpeg_loudnorm_two_pass",
                "normalization_type": "linear",
                "target": {
                    "integrated_lufs": -14.0,
                    "true_peak_dbfs": -1.0,
                    "loudness_range_lu": 4.0,
                },
            }
        )
        self.assertTrue(
            render_contract.superseded_final_result(self.project, marker, final)
        )
        marker_path = self.project / "output/final.mp4.render-result"
        marker_path.write_text(json.dumps(marker))
        (self.project / "output/final.mp4.render.log").write_text("old log")
        render_project.retire_superseded(self.project, marker, final, marker_path)
        self.assertTrue((self.project / segment_assembly.OUTPUT_PATH).is_file())
        self.assertTrue(receipt_path.is_file())

    def test_symlinked_receipt_parent_is_never_read_or_retired(self):
        self.approve_all()
        receipt = self.assembled()
        receipt_path = self.project / segment_assembly.RECEIPT_PATH
        receipt_sha256 = segment_assembly.sha256(receipt_path)
        marker = {
            "assembly": {
                "schema": segment_assembly.SCHEMA,
                "path": segment_assembly.RECEIPT_PATH,
                "sha256": receipt_sha256,
            },
            "mix": {"input_sha256": receipt["output_sha256"]},
        }
        segments_parent = receipt_path.parent
        external_parent = self.project.parent / "external-segments"
        shutil.move(segments_parent, external_parent)
        os.symlink(external_parent, segments_parent, target_is_directory=True)
        with self.assertRaises(segment_assembly.AssemblyError) as unsafe_assembly:
            self.assembled()
        self.assertEqual(unsafe_assembly.exception.code, "assembly_output_invalid")

        self.assertEqual(
            render_contract.assembly_binding_state(self.project, marker), "invalid"
        )
        final = self.project / "output/final.mp4"
        final.write_bytes(b"old final")
        marker_path = self.project / "output/final.mp4.render-result"
        marker_path.write_text(json.dumps(marker))
        with self.assertRaisesRegex(
            ValueError, "unsafe superseded render (path|parent)"
        ):
            render_project.retire_superseded(self.project, marker, final, marker_path)
        self.assertTrue(final.is_file())
        self.assertTrue((external_parent / "assembly.json").is_file())

    def test_receipt_parent_swap_cannot_redirect_assembly_archival(self):
        self.approve_all()
        self.assembled()
        receipt_path = self.project / segment_assembly.RECEIPT_PATH
        stale = json.loads(receipt_path.read_text())
        stale["method"] = segment_assembly.METHOD_REENCODE
        receipt_path.write_text(json.dumps(stale))
        receipt_parent = receipt_path.parent
        detached_parent = self.project.parent / "detached-segments"
        external_parent = self.project.parent / "malicious-segments"
        external_parent.mkdir()
        external_receipt = external_parent / "assembly.json"
        external_receipt.write_text("external sentinel")
        original_capture = segment_assembly.capture_approved_snapshot
        calls = 0

        def swap_after_last_snapshot(project):
            nonlocal calls
            snapshot = original_capture(project)
            calls += 1
            if calls == 5:
                shutil.move(receipt_parent, detached_parent)
                os.symlink(external_parent, receipt_parent, target_is_directory=True)
            return snapshot

        try:
            with mock.patch.object(
                segment_assembly,
                "capture_approved_snapshot",
                side_effect=swap_after_last_snapshot,
            ):
                replacement = self.assembled()
            self.assertEqual(replacement["method"], segment_assembly.METHOD_COPY)
            self.assertEqual(external_receipt.read_text(), "external sentinel")
            self.assertEqual(
                json.loads((detached_parent / "assembly.json").read_text())["method"],
                segment_assembly.METHOD_COPY,
            )
        finally:
            if receipt_parent.is_symlink():
                receipt_parent.unlink()
            if detached_parent.exists():
                shutil.move(detached_parent, receipt_parent)

    def test_output_parent_swap_cannot_redirect_final_retirement(self):
        output = self.project / "output/final.mp4"
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(b"project final")
        marker_path = Path(str(output) + ".render-result")
        marker_path.write_text("{}")
        marker = {"narration_sha256": "a" * 64}
        detached_output = self.project.parent / "detached-output"
        external_output = self.project.parent / "malicious-output"
        external_output.mkdir()
        external_final = external_output / "final.mp4"
        external_final.write_bytes(b"external sentinel")
        original_replace = os.replace
        swapped = False

        def swap_before_first_retirement(source, target, **kwargs):
            nonlocal swapped
            if not swapped:
                shutil.move(output.parent, detached_output)
                os.symlink(external_output, output.parent, target_is_directory=True)
                swapped = True
            return original_replace(source, target, **kwargs)

        try:
            with mock.patch.object(
                render_project.os, "replace", side_effect=swap_before_first_retirement
            ):
                render_project.retire_superseded(
                    self.project, marker, output, marker_path
                )
            self.assertEqual(external_final.read_bytes(), b"external sentinel")
            self.assertTrue(
                (detached_output / f"final.mp4.superseded-{'a' * 12}").is_file()
            )
        finally:
            if output.parent.is_symlink():
                output.parent.unlink()
            if detached_output.exists():
                shutil.move(detached_output, output.parent)

    def test_legacy_project_accepts_only_legacy_marker(self):
        (self.project / segment_plan.PLAN_PATH).unlink()
        self.assertEqual(render_contract.assembly_binding_state(self.project, {}), "current")
        self.assertFalse(
            render_contract.describes_current_inputs(self.project, {"assembly": {}})
        )

    def test_segmented_worker_reuses_approved_assembly_then_mix_once(self):
        self.approve_all()
        self.assembled()
        tools_root = self.fixture.root / "media-tools"
        renderer = tools_root / "video/render_and_verify.sh"
        renderer.parent.mkdir(parents=True)
        renderer.write_text("#!/bin/sh\nexit 0\n")
        renderer.chmod(0o700)
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "Demo",
                    "output": "output/final.mp4",
                    "expected_duration": 40,
                    "concurrency": 1,
                    "skip_pronunciation_gate": True,
                }
            )
        )
        mix_calls = []

        def run(command, **kwargs):
            if Path(command[1]).name == "segment_assembly.py":
                self.fail("current approved assembly must not be rerun")
            mix_calls.append(command)
            output = Path(command[2])
            output.write_bytes(b"whole-program-final")
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            premix_digest = segment_assembly.sha256(Path(command[1]))
            value = {
                "schema": "haru.final_mix.v1",
                "status": "mix_complete",
                "method": "ffmpeg_loudnorm_two_pass",
                "normalization_type": "linear",
                "input_sha256": premix_digest,
                "sha256": digest,
                "bytes": output.stat().st_size,
                "duration_seconds": 40.0,
                "loudness_lufs": -14.0,
                "true_peak_dbfs": -1.1,
                "loudness_range_lu": 1.0,
                "target": {
                    "integrated_lufs": -14.0,
                    "true_peak_dbfs": -1.0,
                    "loudness_range_lu": 4.0,
                },
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

        stdout = io.StringIO()
        with (
            mock.patch.object(render_project_worker.subprocess, "run", side_effect=run),
            contextlib.redirect_stdout(stdout),
        ):
            code = render_project_worker.main(
                ["worker", str(self.project), str(tools_root)]
            )
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertEqual(len(mix_calls), 1)
        marker = json.loads(
            (self.project / "output/final.mp4.render-result").read_text()
        )
        self.assertEqual(
            marker["assembly"]["sha256"],
            segment_assembly.sha256(self.project / segment_assembly.RECEIPT_PATH),
        )
        self.assertTrue((self.project / segment_assembly.OUTPUT_PATH).exists())

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required"
    )
    def test_real_tiny_media_concat_copy_smoke(self):
        media = self.project / "tiny.mp4"
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
                "color=c=black:s=160x90:r=30:d=0.25",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=stereo",
                "-t",
                "0.25",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(media),
            ],
            check=True,
        )
        profile = segment_assembly.probe_media(media)
        self.assertEqual(profile["streams"][0]["codec_name"], "h264")
        self.assertEqual(
            segment_assembly.derive_target(profile)["video"]["frame_rate"], "30/1"
        )


if __name__ == "__main__":
    unittest.main()
