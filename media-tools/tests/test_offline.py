from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
NARRATION = ROOT / "narration"
COVER = ROOT / "cover"
RENDERER = ROOT / "video" / "render_and_verify.sh"
sys.path.insert(0, str(NARRATION))
sys.path.insert(0, str(COVER))

import generate_sectioned_narration as sectioned
import make_cover
import stt_align
import zh_normalize
from providers import ProviderError
from providers import elevenlabs


class PureNarrationTest(unittest.TestCase):
    def test_zh_normalization_is_offline_and_idempotent(self):
        text = "2026年，0050漲了12.5%，價格是NT$20,000。"
        expected = "二零二六年，零零五零漲了百分之十二點五，價格是新台幣兩萬元。"
        self.assertEqual(zh_normalize.normalize_zh(text), expected)
        self.assertEqual(zh_normalize.normalize_zh(expected), expected)

    def test_section_split_preserves_text_and_bounds(self):
        source = "\n\n".join(
            [
                "第一段說明背景，內容足夠形成一個完整段落。",
                "第二段延續背景，並補充必要條件。",
                "第三段形成新的語意區塊，作為結尾。",
            ]
        )
        sections = sectioned.split_text(source, target_chars=30, max_chars=60)
        self.assertEqual(
            "\n\n".join(item["text"] for item in sections),
            source,
        )
        self.assertTrue(all(len(item["text"]) <= 60 for item in sections))
        self.assertEqual(
            [item["id"] for item in sections],
            [f"section-{index:03d}" for index in range(1, len(sections) + 1)],
        )

    def test_stt_retime_preserves_cues_and_uses_synthetic_words(self):
        source = (
            "1\n00:00:00,000 --> 00:00:01,000\n你好。\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n世界。\n"
        )
        words = [
            {"text": "你", "start": 0.20, "end": 0.35, "type": "word"},
            {"text": "好", "start": 0.36, "end": 0.55, "type": "word"},
            {"text": "。", "start": 0.56, "end": 0.70, "type": "spacing"},
            {"text": "世", "start": 1.25, "end": 1.45, "type": "word"},
            {"text": "界", "start": 1.46, "end": 1.70, "type": "word"},
        ]
        output, metrics = stt_align.retime(source, words, 2.0)
        cues = stt_align.parse_srt(output)
        self.assertEqual([item["text"] for item in cues], ["你好。", "世界。"])
        self.assertEqual(metrics["anchor_coverage"], 1.0)
        self.assertTrue(metrics["anchor_coverage_ok"])
        self.assertAlmostEqual(cues[0]["start"], 0.20)
        self.assertAlmostEqual(cues[1]["start"], 1.25)


class ConfigurationTest(unittest.TestCase):
    def test_provider_has_no_credential_path_default(self):
        with mock.patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            with self.assertRaisesRegex(ProviderError, "ELEVENLABS_API_KEY"):
                elevenlabs.load_key()

    def test_generator_fails_before_provider_call_when_voice_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable,
                    str(NARRATION / "generate_narration_with_srt.py"),
                    "--text",
                    "測試",
                    "--out-base",
                    str(Path(directory) / "take"),
                    "--max-credits",
                    "10",
                ],
                capture_output=True,
                text=True,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if key
                    not in {
                        "VIDEO_STUDIO_TTS_VOICE_ID",
                        "ELEVENLABS_API_KEY",
                        "ELEVENLABS_API_KEY_PATH",
                    }
                },
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("VIDEO_STUDIO_TTS_VOICE_ID", result.stderr)

    def test_g2p_fails_closed_without_optional_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "script.txt"
            source.write_text("銀行行動", encoding="utf-8")
            output = root / "plan.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(NARRATION / "g2p_plan.py"),
                    "--text-file",
                    str(source),
                    "--out",
                    str(output),
                ],
                capture_output=True,
                text=True,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith(("VIDEO_STUDIO_G2PW_", "HARU_G2PW_"))
                },
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stderr)["code"], "g2p_model_unavailable")
            self.assertFalse(output.exists())

    def test_g2p_accepts_explicit_external_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "fake" / "g2pw"
            package.mkdir(parents=True)
            package.joinpath("__init__.py").write_text(
                "__version__ = 'test'\n"
                "class G2PWConverter:\n"
                "    def __init__(self, **kwargs): self.chars = ['行']\n"
                "    def __call__(self, text):\n"
                "        readings={'銀':'ㄧㄣ2','行':'ㄏㄤ2','動':'ㄉㄨㄥ4'}\n"
                "        return [[readings[c] for c in text]]\n",
                encoding="utf-8",
            )
            model = root / "model"
            model.mkdir()
            model.joinpath("version").write_text("fixture\n", encoding="utf-8")
            model.joinpath("g2pw.onnx").write_bytes(b"fixture")
            bert = root / "bert"
            bert.mkdir()
            source = root / "script.txt"
            source.write_text("銀行行動", encoding="utf-8")
            output = root / "plan.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(NARRATION / "g2p_plan.py"),
                    "--text-file",
                    str(source),
                    "--model-dir",
                    str(model),
                    "--bert-model",
                    str(bert),
                    "--out",
                    str(output),
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(root / "fake")},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(plan["schema"], "haru.pronunciation_plan.v1")
            self.assertEqual(plan["source"]["characters"], 4)
            self.assertTrue(plan["summary"]["review_required"])


class CoverTest(unittest.TestCase):
    def test_template_fill_is_neutral_and_complete(self):
        template = (COVER / "cover_template.html").read_text(encoding="utf-8")
        html = make_cover.build_cover_html(
            template,
            accent="teal",
            subject_src="file:///tmp/subject.png",
            kicker="# Topic",
            top="Top",
            bottom="A **clear** point",
            subtitle="Subtitle",
        )
        for token in (
            "SUBJECT_SRC",
            "KICKER_HTML",
            "TOP_HTML",
            "BOTTOM_HTML",
            "SUBTITLE_HTML",
            "TOP_SIZE",
            "BOTTOM_SIZE",
            "--ACCENT",
        ):
            self.assertNotIn(token, html)
        self.assertIn('<span class="hl">clear</span>', html)

    def test_browser_detection_accepts_explicit_macos_or_linux_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            browser = Path(directory) / "chromium"
            browser.write_text("#!/bin/sh\n", encoding="utf-8")
            browser.chmod(browser.stat().st_mode | stat.S_IXUSR)
            self.assertEqual(make_cover.find_chromium(str(browser)), str(browser))

    def test_subject_assets_are_required(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(FileNotFoundError, "asset"):
                make_cover.resolve_subject("default", asset=None, asset_dir=None)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class RenderTest(unittest.TestCase):
    def make_video(self, path: Path, duration: float = 1.0) -> None:
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
                f"color=c=black:s=160x90:r=24:d={duration}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ],
            check=True,
        )

    def test_verify_only_checks_duration_and_full_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "fixture.mp4"
            self.make_video(video)
            passed = subprocess.run(
                [str(RENDERER), "--verify-only", str(video), "1"],
                capture_output=True,
                text=True,
            )
            failed = subprocess.run(
                [str(RENDERER), "--verify-only", str(video), "5"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assertNotEqual(failed.returncode, 0)

    def test_verify_only_rejects_truncated_container(self):
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "fixture.mp4"
            self.make_video(video)
            payload = video.read_bytes()
            video.write_bytes(payload[: len(payload) // 2])
            result = subprocess.run(
                [str(RENDERER), "--verify-only", str(video), "1"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_full_render_promotes_only_verified_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            output = root / "final.mp4"
            binaries = root / "bin"
            binaries.mkdir()
            fake_npx = binaries / "npx"
            ffmpeg = shutil.which("ffmpeg")
            fake_npx.write_text(
                "#!/bin/sh\n"
                f"exec {ffmpeg} -y -hide_banner -loglevel error "
                "-f lavfi -i color=c=black:s=160x90:r=24:d=1 "
                "-c:v libx264 -pix_fmt yuv420p \"$5\"\n",
                encoding="utf-8",
            )
            fake_npx.chmod(fake_npx.stat().st_mode | stat.S_IXUSR)
            env = {**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}"}
            passed = subprocess.run(
                [
                    str(RENDERER),
                    str(project),
                    "Demo",
                    str(output),
                    "1",
                    "1",
                    "--skip-pronunciation-gate",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assertTrue(output.is_file())
            self.assertFalse(Path(str(output).replace(".mp4", ".candidate.mp4")).exists())

    def test_failed_candidate_and_remux_never_reach_final_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            output = root / "final.mp4"
            binaries = root / "bin"
            binaries.mkdir()
            fake_npx = binaries / "npx"
            ffmpeg = shutil.which("ffmpeg")
            fake_npx.write_text(
                "#!/bin/sh\n"
                f"exec {ffmpeg} -y -hide_banner -loglevel error "
                "-f lavfi -i color=c=black:s=160x90:r=24:d=1 "
                "-c:v libx264 -pix_fmt yuv420p \"$5\"\n",
                encoding="utf-8",
            )
            fake_npx.chmod(fake_npx.stat().st_mode | stat.S_IXUSR)
            env = {**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}"}
            result = subprocess.run(
                [
                    str(RENDERER),
                    str(project),
                    "Demo",
                    str(output),
                    "5",
                    "1",
                    "--skip-pronunciation-gate",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())
            self.assertFalse(root.joinpath("final.candidate.mp4").exists())
            self.assertFalse(root.joinpath("final.remux-candidate.mp4").exists())


if __name__ == "__main__":
    unittest.main()
