import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import local_delivery  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LocalDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.project = self.root / "projects" / "demo"
        self.output = self.project / "output"
        self.output.mkdir(parents=True)
        self.delivery = self.root / "delivery"
        self.delivery.mkdir()
        self.video = self.output / "final.mp4"
        ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
        result = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x90:rate=10",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                "-t",
                "1",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                "-shortest",
                str(self.video),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            self.fail(result.stderr.decode())
        self.narration = self.project / "narration-final.mp3"
        self.narration.write_bytes(b"imported narration")
        (self.project / "narration-final.srt").write_text(
            "1\n00:00:00,000 --> 00:00:00,800\nhello\n",
            encoding="utf-8",
        )
        (self.project / "sources.md").write_text(
            "# Sources\n- https://example.test/source\n", encoding="utf-8"
        )
        (self.project / "claims.json").write_text(
            json.dumps(
                {"claims": [{"id": "C1", "source_url": "https://example.test/source"}]}
            ),
            encoding="utf-8",
        )
        (self.project / "project-contract.json").write_text(
            json.dumps(
                {"schema": "haru.project_contract.v1", "lane_contract": "manual.v1"}
            ),
            encoding="utf-8",
        )
        self.write_receipt()

    def tearDown(self):
        self.directory.cleanup()

    def write_receipt(self, **changes):
        marker = {
            "schema": "haru.render_result.v1",
            "status": "render_complete",
            "project": self.project.name,
            "output": "output/final.mp4",
            "video_sha256": digest(self.video),
            "bytes": self.video.stat().st_size,
            "duration_seconds": 1.0,
            "loudness_lufs": -14.0,
            "true_peak_dbfs": -1.0,
            "loudness_range_lu": 2.0,
            "narration_sha256": digest(self.narration),
            "mix": {
                "schema": "haru.final_mix.v1",
                "method": "ffmpeg_loudnorm_two_pass",
                "normalization_type": "linear",
                "input_sha256": "a" * 64,
                "audio_mix": None,
                "target": {
                    "integrated_lufs": -14.0,
                    "true_peak_dbfs": -1.0,
                    "loudness_range_lu": 5.0,
                },
            },
        }
        marker.update(changes)
        Path(str(self.video) + ".render-result").write_text(
            json.dumps(marker), encoding="utf-8"
        )

    def test_real_media_is_technical_ready_without_claiming_human_checks(self):
        result = local_delivery.technical_status(self.project)
        self.assertEqual(result["status"], "technical_ready")
        self.assertEqual(result["full_decode"], "pass")
        self.assertEqual(result["subtitles"]["status"], "pass")
        self.assertFalse(result["publication_ready"])
        self.assertFalse(result["human_approval"])
        self.assertEqual(
            {item["status"] for item in result["content_checks"]}, {"unperformed"}
        )

    def test_tampered_video_and_changed_source_invalidate_the_render_binding(self):
        self.video.write_bytes(self.video.read_bytes() + b"tamper")
        tampered = local_delivery.technical_status(self.project)
        self.assertIn(
            "render_receipt_invalid", {item["code"] for item in tampered["blockers"]}
        )

        self.write_receipt()
        self.narration.write_bytes(b"changed narration")
        stale = local_delivery.technical_status(self.project)
        self.assertIn(
            "render_receipt_invalid", {item["code"] for item in stale["blockers"]}
        )

    def test_invalid_receipt_and_subtitle_ranges_are_blockers(self):
        Path(str(self.video) + ".render-result").write_text(
            "not-json", encoding="utf-8"
        )
        invalid = local_delivery.technical_status(self.project)
        self.assertIn(
            "render_receipt_invalid", {item["code"] for item in invalid["blockers"]}
        )

        self.write_receipt()
        (self.project / "narration-final.srt").write_text(
            "1\n00:00:00,900 --> 00:00:02,000\noutside\n", encoding="utf-8"
        )
        subtitle = local_delivery.technical_status(self.project)
        self.assertIn(
            "subtitle_invalid", {item["code"] for item in subtitle["blockers"]}
        )

    def test_export_is_typed_atomic_immutable_and_idempotent(self):
        exported = local_delivery.export_delivery(
            self.project, destination=self.delivery, idempotency_key="export-1"
        )
        bundle = Path(exported["bundle_path"])
        self.assertTrue((bundle / "video/final.mp4").is_file())
        self.assertTrue((bundle / "captions/narration-final.srt").is_file())
        self.assertTrue((bundle / "sources/sources.md").is_file())
        self.assertTrue((bundle / "qa/technical-status.json").is_file())
        manifest = json.loads((bundle / "artifact-manifest.json").read_text())
        self.assertEqual(manifest["schema"], local_delivery.BUNDLE_SCHEMA)
        self.assertFalse(manifest["publication_ready"])
        self.assertNotIn("credentials", json.dumps(manifest).lower())

        replay = local_delivery.export_delivery(
            self.project, destination=self.delivery, idempotency_key="export-1"
        )
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["bundle_id"], exported["bundle_id"])
        self.assertFalse(list(bundle.parent.glob(".*.staging-*")))

    def test_path_traversal_and_conflicting_target_are_refused(self):
        with self.assertRaises(local_delivery.DeliveryError) as traversal:
            local_delivery.export_delivery(
                self.project, destination=self.delivery, idempotency_key="../escape"
            )
        self.assertEqual(traversal.exception.code, "invalid_input")

        exported = local_delivery.export_delivery(
            self.project, destination=self.delivery, idempotency_key="first"
        )
        bundle = Path(exported["bundle_path"])
        (bundle / "video/final.mp4").write_bytes(b"changed")
        with self.assertRaises(local_delivery.DeliveryError) as conflict:
            local_delivery.export_delivery(
                self.project, destination=self.delivery, idempotency_key="second"
            )
        self.assertEqual(conflict.exception.code, "delivery_target_conflict")

    def test_copy_failure_cleans_staging_and_publishes_no_bundle(self):
        with mock.patch.object(
            local_delivery, "_copy", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                local_delivery.export_delivery(
                    self.project, destination=self.delivery, idempotency_key="failure"
                )
        project_destination = self.delivery / self.project.name
        self.assertFalse(
            project_destination.exists() and any(project_destination.iterdir())
        )
        self.assertFalse(
            (self.project / ".hvp/local-delivery/exports/failure.json").exists()
        )

    def test_source_change_during_export_is_refused_and_cleaned(self):
        original_copy = local_delivery._copy

        def changing_copy(source, destination):
            original_copy(source, destination)
            if destination.name == "final.mp4":
                self.narration.write_bytes(b"changed during export")

        with mock.patch.object(local_delivery, "_copy", side_effect=changing_copy):
            with self.assertRaises(local_delivery.DeliveryError) as changed:
                local_delivery.export_delivery(
                    self.project,
                    destination=self.delivery,
                    idempotency_key="source-change",
                )
        self.assertEqual(changed.exception.code, "delivery_source_changed")
        project_destination = self.delivery / self.project.name
        self.assertFalse(
            project_destination.exists() and any(project_destination.iterdir())
        )
        self.assertFalse(
            (self.project / ".hvp/local-delivery/exports/source-change.json").exists()
        )

    def test_destination_must_be_configured_and_absolute(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(local_delivery.DeliveryError) as unconfigured:
                local_delivery.export_delivery(
                    self.project, destination=None, idempotency_key="no-root"
                )
        self.assertEqual(
            unconfigured.exception.code, "delivery_destination_unconfigured"
        )
        with self.assertRaises(local_delivery.DeliveryError) as relative:
            local_delivery.export_delivery(
                self.project, destination=Path("relative"), idempotency_key="relative"
            )
        self.assertEqual(relative.exception.code, "delivery_destination_unconfigured")


if __name__ == "__main__":
    unittest.main()
