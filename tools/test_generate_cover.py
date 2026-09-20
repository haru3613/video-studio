import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from runtime_test_fixture import RuntimePolicyCase
from unittest import mock
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_cover
from test_agent_status import make_ready_project


def png(width=1280, height=720):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00\x00\x00" * width) * height))
        + chunk(b"IEND", b"")
    )


class GenerateCoverTest(RuntimePolicyCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "studio"
        self.workspace.mkdir()
        self.project = make_ready_project(self.workspace, "cover-project")
        self.tools = root / "media-tools"
        (self.tools / "cover").mkdir(parents=True)
        (self.tools / "cover/make_cover.py").write_text("# fixture\n")
        staging = self.project / ".hvp/staging"
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "cover-spec.json").write_text(
            json.dumps(
                {
                    "schema": "haru.cover_spec.v1",
                    "kicker": "小羊卡牌社事件",
                    "top": "他開了一家店",
                    "bottom": "然後合法走完**每一步**",
                    "subtitle": "六道防線，沒有一道攔得住",
                    "expression": "serious",
                    "accent": "amber",
                }
            )
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_generates_promotes_and_rebinds_the_cover(self):
        def render(arguments, **_kwargs):
            out = Path(arguments[arguments.index("--out") + 1])
            out.write_bytes(png())
            return mock.Mock(returncode=0)

        with mock.patch.object(generate_cover.subprocess, "run", side_effect=render):
            result = generate_cover.generate(self.project, self.tools)

        cover = self.project / "output/cover.png"
        digest = hashlib.sha256(cover.read_bytes()).hexdigest()
        self.assertEqual(result["output_sha256"], digest)
        self.assertEqual(result["status"], "complete")
        receipt = json.loads(
            (self.project / ".hvp/producer-receipts/output__cover.png.json").read_text()
        )
        self.assertEqual(receipt["output_sha256"], digest)
        manifest = json.loads((self.project / "artifact_manifest.json").read_text())
        self.assertEqual(manifest["canonical"]["cover"]["bytes"], cover.stat().st_size)

    def test_rejects_unknown_spec_fields_before_rendering(self):
        spec = self.project / ".hvp/staging/cover-spec.json"
        value = json.loads(spec.read_text())
        value["command"] = "unexpected"
        spec.write_text(json.dumps(value))
        with mock.patch.object(generate_cover.subprocess, "run") as runner:
            with self.assertRaisesRegex(generate_cover.CoverError, "cover_spec_invalid"):
                generate_cover.generate(self.project, self.tools)
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
