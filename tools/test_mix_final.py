import json
import math
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIXER = ROOT / "tools/mix_final.py"


class MixFinalTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.source = root / "premix.mp4"
        self.output = root / "final.mp4"
        self.verifier = root / "render_and_verify.sh"
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
                "color=c=black:s=160x90:r=30:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=2",
                "-filter:a",
                "volume=0.02",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                self.source,
            ],
            check=True,
        )
        self.write_verifier("#!/bin/sh\nexit 0\n")

    def tearDown(self):
        self.directory.cleanup()

    def write_verifier(self, body):
        self.verifier.write_text(body)
        self.verifier.chmod(self.verifier.stat().st_mode | stat.S_IXUSR)

    def invoke(self, plan=None):
        command = [MIXER, self.source, self.output, "2", self.verifier]
        if plan:
            command.append(plan)
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_two_pass_mix_produces_verified_target_loudness(self):
        result = self.invoke()

        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["schema"], "haru.final_mix.v1")
        self.assertEqual(receipt["status"], "mix_complete")
        self.assertEqual(receipt["method"], "ffmpeg_loudnorm_two_pass")
        self.assertIn(receipt["normalization_type"], {"linear", "dynamic"})
        self.assertTrue(math.isfinite(receipt["loudness_lufs"]))
        self.assertGreaterEqual(receipt["loudness_lufs"], -15)
        self.assertLessEqual(receipt["loudness_lufs"], -13)
        self.assertLessEqual(receipt["true_peak_dbfs"], -1.0)
        self.assertEqual(receipt["target"]["encoder_true_peak_dbfs"], -3.0)
        self.assertEqual(receipt["sha256"], self.sha256(self.output))
        self.assertTrue(self.output.is_file())
        self.assertTrue(self.source.is_file())
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", self.output],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertAlmostEqual(float(probe.stdout), 2, delta=1)

    def test_failed_verification_never_promotes_final_bytes(self):
        self.write_verifier("#!/bin/sh\nexit 1\n")

        result = self.invoke()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_bgm_and_sound_effects_are_mixed_and_digest_bound(self):
        bgm = self.source.parent / "bgm.wav"
        effect = self.source.parent / "effect.wav"
        for path, frequency in ((bgm, 220), (effect, 880)):
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration=1",
                    str(path),
                ],
                check=True,
            )
        plan = self.source.parent / "render_plan.json"
        plan.write_text(json.dumps({
            "audio_mix": {
                "schema": "haru.audio_mix.v1",
                "background_music": {"path": "bgm.wav", "gain_db": -30},
                "sound_effects": [{"path": "effect.wav", "start_seconds": 0.5, "gain_db": -12}],
            }
        }))

        result = self.invoke(plan)

        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)["audio_mix"]
        self.assertEqual(receipt["schema"], "haru.audio_mix.v1")
        self.assertEqual(receipt["ducking"], "sidechaincompress.v1")
        self.assertEqual(receipt["background_music"]["path"], "bgm.wav")
        self.assertEqual(receipt["sound_effects"][0]["path"], "effect.wav")

    def test_audio_mix_rejects_paths_outside_the_project(self):
        plan = self.source.parent / "render_plan.json"
        plan.write_text(json.dumps({
            "audio_mix": {
                "schema": "haru.audio_mix.v1",
                "sound_effects": [{"path": "../effect.wav", "start_seconds": 0}],
            }
        }))

        result = self.invoke(plan)

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_source_change_during_verification_fails_closed(self):
        self.write_verifier(
            f"#!/bin/sh\nprintf tampered >> {shlex.quote(str(self.source))}\nexit 0\n"
        )

        result = self.invoke()

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_high_crest_source_uses_bounded_dynamic_fallback(self):
        self.source.unlink()
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
                "color=c=black:s=160x90:r=30:d=10",
                "-f",
                "lavfi",
                "-i",
                r"aevalsrc=0.02*sin(2*PI*440*t)+if(lt(mod(t\,1)\,0.005)\,0.8\,0):s=48000:d=10",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                self.source,
            ],
            check=True,
        )

        result = self.invoke()

        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["normalization_type"], "dynamic")
        self.assertLessEqual(
            receipt["target"]["loudness_range_lu"] - receipt["loudness_range_lu"],
            3,
        )

    @staticmethod
    def sha256(path):
        import hashlib

        return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
