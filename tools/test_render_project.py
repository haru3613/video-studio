import json
import hashlib
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

import template_trust

from test_agent_status import write_mina_editorial_fixture


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools/render_project_worker.py"


class RenderProjectTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.root = root
        self.project = root / "project"
        self.remotion = self.project / "remotion"
        self.output = self.project / "output"
        self.tools = root / "tools"
        (self.remotion / "node_modules/.bin").mkdir(parents=True)
        self.output.mkdir()
        (self.tools / "video").mkdir(parents=True)
        (self.remotion / "package.json").write_text("{}")
        (self.remotion / "package-lock.json").write_text("{}")
        (self.remotion / "remotion.config.ts").write_text("export default {};")
        (self.remotion / "src").mkdir()
        (self.remotion / "src/index.ts").write_text("export const fixture = true;")
        binary = self.remotion / "node_modules/.bin/remotion"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        renderer = self.tools / "video/render_and_verify.sh"
        renderer.write_text(
            """#!/bin/sh
case "$1" in
  --verify-only|--loudness-gate-only) exit 0 ;;
esac
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i color=c=black:s=160x90:r=30:d=1 \
  -f lavfi -i sine=frequency=440:sample_rate=48000:duration=1 \
  -filter:a volume=0.02 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$3"
printf '%s\\n' "$@" > "$3.args"
"""
        )
        renderer.chmod(renderer.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.directory.cleanup()

    def invoke(self):
        try:
            template_trust.trust(self.project)
        except template_trust.TrustError:
            # Invalid-path cases must reach the worker and be rejected there;
            # valid synthetic templates are explicitly approved above.
            pass
        return subprocess.run(
            [RUNNER, self.project, self.tools],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_fixed_remotion_plan_produces_a_digest_bound_marker(self):
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "HvpSmoke",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "concurrency": 1,
                    "skip_pronunciation_gate": True,
                }
            )
        )

        result = self.invoke()

        self.assertEqual(result.returncode, 0, result)
        response = json.loads(result.stdout)
        marker = json.loads((self.output / "final.mp4.render-result").read_text())
        self.assertEqual(response["code"], "render_complete")
        self.assertEqual(marker["status"], "render_complete")
        self.assertEqual(marker["video_sha256"], response["data"]["video_sha256"])
        self.assertEqual(marker["mix"]["method"], "ffmpeg_loudnorm_two_pass")
        self.assertGreaterEqual(marker["loudness_lufs"], -15)
        self.assertLessEqual(marker["loudness_lufs"], -13)
        self.assertFalse((self.output / "final.pre-loudnorm.mp4").exists())
        self.assertEqual(self.invoke().returncode, 3)

    def test_verified_narration_uses_its_digest_bound_stamp_in_isolated_worker(self):
        narration = self.project / "narration-final.mp3"
        narration.write_bytes(b"verified narration")
        digest = hashlib.sha256(narration.read_bytes()).hexdigest()
        (self.project / "narration-final.mp3.pron-ok.json").write_text(
            json.dumps({"sha256": digest, "warnings": []})
        )
        (self.project / "narration.txt").write_text("verified text")
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "HvpSmoke",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "concurrency": 1,
                    "narration": "narration-final.mp3",
                    "narration_text": "narration.txt",
                }
            )
        )

        result = self.invoke()

        self.assertEqual(result.returncode, 0, result)
        marker = json.loads((self.output / "final.mp4.render-result").read_text())
        self.assertEqual(marker["narration_sha256"], digest)
        self.assertIn(
            "--skip-pronunciation-gate",
            (self.output / "final.pre-loudnorm.mp4.args").read_text(),
        )

    def test_a_stale_renderer_narration_copy_blocks_the_render(self):
        """The failure a human caught by watching a preview, which no gate caught.

        Remotion reads narration through staticFile(), which resolves under
        remotion/public/ and nowhere else. Promoting a new take replaces the
        canonical file and leaves that copy behind, so the render comes out as
        the new cut carrying the PREVIOUS narration -- and every other check
        here passes, because every other check reads the canonical file.
        """
        narration = self.project / "narration-final.mp3"
        narration.write_bytes(b"the take that was approved")
        digest = hashlib.sha256(narration.read_bytes()).hexdigest()
        (self.project / "narration-final.mp3.pron-ok.json").write_text(
            json.dumps({"sha256": digest, "warnings": []})
        )
        (self.project / "narration.txt").write_text("text")
        static_copy = self.project / "remotion/public/narration-final.mp3"
        static_copy.parent.mkdir(parents=True, exist_ok=True)
        static_copy.write_bytes(b"the take before it")
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "HvpSmoke",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "concurrency": 1,
                    "narration": "narration-final.mp3",
                    "narration_text": "narration.txt",
                }
            )
        )

        self.assertNotEqual(self.invoke().returncode, 0)
        self.assertFalse((self.output / "final.mp4.render-result").exists())

        static_copy.write_bytes(narration.read_bytes())
        self.assertEqual(self.invoke().returncode, 0)

    def test_audio_mix_plan_is_applied_by_the_render_runner(self):
        bgm = self.project / "audio/bgm.wav"
        effect = self.project / "audio/effect.wav"
        bgm.parent.mkdir()
        for path, frequency in ((bgm, 220), (effect, 880)):
            subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration=1",
                    str(path),
                ],
                check=True,
            )
        (self.project / "render_plan.json").write_text(json.dumps({
            "schema": "haru.render_plan.v1",
            "engine": "remotion",
            "remotion_dir": "remotion",
            "composition": "HvpSmoke",
            "output": "output/final.mp4",
            "expected_duration": 1,
            "concurrency": 1,
            "skip_pronunciation_gate": True,
            "audio_mix": {
                "schema": "haru.audio_mix.v1",
                "background_music": {"path": "audio/bgm.wav", "gain_db": -30},
                "sound_effects": [{"path": "audio/effect.wav", "start_seconds": 0.25}],
            },
        }))

        result = self.invoke()

        self.assertEqual(result.returncode, 0, result)
        marker = json.loads((self.output / "final.mp4.render-result").read_text())
        self.assertEqual(marker["mix"]["audio_mix"]["schema"], "haru.audio_mix.v1")

    def test_failed_final_verification_does_not_leave_final_bytes(self):
        renderer = self.tools / "video/render_and_verify.sh"
        renderer.write_text(
            """#!/bin/sh
case "$1" in
  --verify-only|--loudness-gate-only) exit 1 ;;
esac
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i color=c=black:s=160x90:r=30:d=1 \
  -f lavfi -i sine=frequency=440:sample_rate=48000:duration=1 \
  -filter:a volume=0.02 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest "$3"
"""
        )
        renderer.chmod(renderer.stat().st_mode | stat.S_IXUSR)
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "HvpSmoke",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "concurrency": 1,
                    "skip_pronunciation_gate": True,
                }
            )
        )

        result = self.invoke()

        self.assertEqual(result.returncode, 4)
        self.assertFalse((self.output / "final.mp4").exists())
        self.assertTrue((self.output / "final.pre-loudnorm.mp4").exists())

    def test_unprovenanced_premix_cannot_skip_rendering(self):
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "HvpSmoke",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "concurrency": 1,
                    "skip_pronunciation_gate": True,
                }
            )
        )
        premix = self.output / "final.pre-loudnorm.mp4"
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
                "color=c=black:s=160x90:r=30:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=1",
                "-filter:a",
                "volume=0.02",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                premix,
            ],
            check=True,
        )

        result = self.invoke()

        self.assertEqual(result.returncode, 3, result)
        self.assertFalse((self.output / "final.mp4").exists())
        self.assertTrue(premix.is_file())

    def test_flattened_lra_mix_receipt_is_removed_and_reported_as_json(self):
        copied_repo = self.root / "repo"
        (copied_repo / "scripts").mkdir(parents=True)
        (copied_repo / "tools").mkdir()
        copied_runner = copied_repo / "tools/render_project_worker.py"
        shutil.copy2(RUNNER, copied_runner)
        shutil.copy2(
            ROOT / "tools/render_contract.py",
            copied_repo / "tools/render_contract.py",
        )
        shutil.copy2(
            ROOT / "tools/editorial_contract.py",
            copied_repo / "tools/editorial_contract.py",
        )
        shutil.copy2(
            ROOT / "tools/pronunciation_workflow.py",
            copied_repo / "tools/pronunciation_workflow.py",
        )
        for name in (
            "canonical_layout.py",
            "segment_assembly.py",
            "segment_plan.py",
            "segment_render.py",
            "template_trust.py",
        ):
            shutil.copy2(ROOT / "tools" / name, copied_repo / "tools" / name)
        mixer = copied_repo / "tools/mix_final.py"
        mixer.write_text(
            """#!/usr/bin/python3 -I
import hashlib
import json
import shutil
import sys

source, output = sys.argv[1:3]
shutil.copyfile(source, output)
payload = open(output, "rb").read()
print(json.dumps({
    "schema": "haru.final_mix.v1",
    "status": "mix_complete",
    "method": "ffmpeg_loudnorm_two_pass",
    "normalization_type": "dynamic",
    "input_sha256": hashlib.sha256(open(source, "rb").read()).hexdigest(),
    "sha256": hashlib.sha256(payload).hexdigest(),
    "bytes": len(payload),
    "duration_seconds": 1,
    "loudness_lufs": -14,
    "true_peak_dbfs": -1,
    "loudness_range_lu": 0,
    "target": {
        "integrated_lufs": -14.0,
        "true_peak_dbfs": -1.0,
        "loudness_range_lu": 20,
    },
}))
"""
        )
        mixer.chmod(mixer.stat().st_mode | stat.S_IXUSR)
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "HvpSmoke",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "concurrency": 1,
                    "skip_pronunciation_gate": True,
                }
            )
        )

        template_trust.trust(self.project)

        result = subprocess.run(
            [copied_runner, self.project, self.tools],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 4, result)
        self.assertEqual(json.loads(result.stdout)["code"], "mix_failed")
        self.assertFalse((self.output / "final.mp4").exists())

    def test_paths_outside_the_project_fail_before_the_renderer_runs(self):
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "../outside",
                    "composition": "HvpSmoke",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "skip_pronunciation_gate": True,
                }
            )
        )

        result = self.invoke()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["code"], "invalid_input")
        self.assertFalse((self.output / "final.mp4").exists())

    def test_unapproved_or_changed_template_is_blocked_before_render(self):
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "HvpSmoke",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "skip_pronunciation_gate": True,
                    "trusted": True,
                }
            )
        )
        unapproved = subprocess.run(
            [RUNNER, "--validate", self.project, self.tools],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(unapproved.returncode, 2, unapproved)
        self.assertEqual(json.loads(unapproved.stdout)["code"], "invalid_input")

        template_trust.trust(self.project)
        (self.remotion / "src/index.ts").write_text("export const changed = true;")
        changed = subprocess.run(
            [RUNNER, "--validate", self.project, self.tools],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(changed.returncode, 2, changed)
        self.assertEqual(json.loads(changed.stdout)["code"], "invalid_input")
        self.assertFalse((self.output / "final.mp4").exists())

    def test_g2p_plan_without_current_human_review_blocks_render(self):
        (self.project / "narration.txt").write_text("銀行行動")
        (self.project / "pronunciation-plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.pronunciation_plan.v1",
                    "source": {
                        "sha256": hashlib.sha256("銀行行動".encode()).hexdigest()
                    },
                }
            )
        )
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "HvpSmoke",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "skip_pronunciation_gate": True,
                }
            )
        )

        result = self.invoke()

        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["code"], "pronunciation_review_stale")
        self.assertFalse((self.output / "final.mp4").exists())

    def test_social_longform_render_is_blocked_without_required_profile(self):
        (self.project / "project-contract.json").write_text(
            json.dumps(
                {
                    "schema": "haru.project_contract.v1",
                    "lane_contract": "social_issue_longform.v1",
                }
            )
        )
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "HvpSmoke",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "concurrency": 1,
                    "skip_pronunciation_gate": True,
                }
            )
        )

        result = self.invoke()

        self.assertEqual(result.returncode, 3, result)
        response = json.loads(result.stdout)
        self.assertEqual(response["code"], "editorial_contract_failed")
        self.assertFalse((self.output / "final.mp4").exists())

    def test_every_presenter_lane_is_gated_at_render(self):
        """A lane pinned to a presenter must reach the editorial gate.

        The worker used to carry its own list of gated lanes. Adding a lane and
        forgetting that list let a project render with no editorial validation at
        all, and nothing failed — the gap this test exists to close. Driving the
        assertion off `LANE_PROFILES` means a fourth lane cannot be added without
        either being gated or breaking this.
        """
        import editorial_contract

        pinned = [
            lane for lane, profile in editorial_contract.LANE_PROFILES.items()
            if profile is not None
        ]
        self.assertTrue(pinned, "no presenter lanes declared")

        for lane in pinned:
            with self.subTest(lane=lane):
                (self.project / "project-contract.json").write_text(
                    json.dumps(
                        {
                            "schema": "haru.project_contract.v1",
                            "lane_contract": lane,
                            # Deliberately no production_profile: the gate must fire on
                            # the lane, never on the project having declared something.
                        }
                    )
                )
                (self.project / "render_plan.json").write_text(
                    json.dumps(
                        {
                            "schema": "haru.render_plan.v1",
                            "engine": "remotion",
                            "remotion_dir": "remotion",
                            "composition": "HvpSmoke",
                            "output": "output/final.mp4",
                            "expected_duration": 1,
                            "concurrency": 1,
                            "skip_pronunciation_gate": True,
                        }
                    )
                )

                result = self.invoke()

                self.assertEqual(result.returncode, 3, result)
                self.assertEqual(
                    json.loads(result.stdout)["code"], "editorial_contract_failed"
                )
                self.assertFalse((self.output / "final.mp4").exists())

    def test_social_longform_render_and_result_bind_the_editorial_contract(self):
        (self.project / "project-contract.json").write_text(
            json.dumps(
                {
                    "schema": "haru.project_contract.v1",
                    "lane_contract": "social_issue_longform.v1",
                    "production_profile": "mina_longform.v1",
                }
            )
        )
        (self.project / "storyboard-final-timed.json").write_text(
            json.dumps(
                {
                    "schema": "haru.storyboard_timed.v1",
                    "visual_timeline_contract": "cue_driven.v1",
                    "audio_duration_seconds": 60,
                    "scenes": [
                        {
                            "visual_events": [
                                {"event_id": "host-open", "start_seconds": 0, "end_seconds": 10.8},
                                {"event_id": "evidence", "start_seconds": 10.8, "end_seconds": 30},
                                {"event_id": "evidence-pip", "start_seconds": 30, "end_seconds": 42},
                                {"event_id": "system-motion", "start_seconds": 42, "end_seconds": 60},
                            ]
                        }
                    ],
                }
            )
        )
        write_mina_editorial_fixture(self.project)
        editorial_digest = hashlib.sha256(
            (self.project / "editorial-contract.json").read_bytes()
        ).hexdigest()
        (self.project / "render_plan.json").write_text(
            json.dumps(
                {
                    "schema": "haru.render_plan.v1",
                    "engine": "remotion",
                    "remotion_dir": "remotion",
                    "composition": "HvpSmoke",
                    "output": "output/final.mp4",
                    "expected_duration": 1,
                    "concurrency": 1,
                    "skip_pronunciation_gate": True,
                    "editorial_contract": "editorial-contract.json",
                    "editorial_contract_sha256": editorial_digest,
                }
            )
        )

        result = self.invoke()

        self.assertEqual(result.returncode, 0, result)
        marker = json.loads((self.output / "final.mp4.render-result").read_text())
        self.assertEqual(marker["editorial_contract_sha256"], editorial_digest)


if __name__ == "__main__":
    unittest.main()
