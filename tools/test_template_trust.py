import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import template_trust


ROOT = Path(__file__).resolve().parents[1]


class TemplateTrustTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project = self.root / "project"
        self.remotion = self.project / "remotion"
        (self.remotion / "src").mkdir(parents=True)
        (self.remotion / "public").mkdir()
        (self.remotion / "package.json").write_text('{"scripts":{"render":"remotion"}}')
        (self.remotion / "package-lock.json").write_text('{"lockfileVersion":3}')
        (self.remotion / "remotion.config.ts").write_text("export default {};")
        (self.remotion / "tsconfig.json").write_text('{"compilerOptions":{}}')
        (self.remotion / "src/index.ts").write_text("export const value = 1;")
        (self.remotion / "src/content.json").write_text('{"title":"one"}')
        (self.remotion / "public/image.bin").write_bytes(b"media one")
        self.write_plan(trusted=True)

    def tearDown(self):
        self.directory.cleanup()

    def write_plan(self, **extra):
        plan = {
            "schema": "haru.render_plan.v1",
            "engine": "remotion",
            "remotion_dir": "remotion",
            "composition": "Test",
            "output": "output/final.mp4",
            "expected_duration": 1,
            **extra,
        }
        (self.project / "render_plan.json").write_text(json.dumps(plan))

    def test_caller_trusted_flag_cannot_authorize_custom_code(self):
        with self.assertRaisesRegex(template_trust.TrustError, "template_untrusted"):
            template_trust.authorize(self.project)
        self.assertFalse((self.project / ".hvp").exists())

    def test_local_owner_approval_allows_data_edits_only(self):
        approved = template_trust.trust(self.project)
        self.assertEqual(approved["mode"], "local_approval")
        ledger = self.project / template_trust.LEDGER
        self.assertEqual(ledger.stat().st_mode & 0o777, 0o600)

        (self.remotion / "src/content.json").write_text('{"title":"two"}')
        (self.remotion / "public/image.bin").write_bytes(b"media two")
        current = template_trust.authorize(self.project)
        self.assertEqual(current["template_digest"], approved["template_digest"])

    def test_local_wrapper_records_owner_approval(self):
        result = subprocess.run(
            [ROOT / "scripts/trust-template", self.project],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result)
        response = json.loads(result.stdout)
        self.assertEqual(response["code"], "template_trusted")
        self.assertEqual(response["data"]["mode"], "local_approval")
        self.assertEqual(template_trust.authorize(self.project)["mode"], "local_approval")

    def test_code_lock_config_and_new_file_each_invalidate_approval(self):
        original = {
            "src/index.ts": (self.remotion / "src/index.ts").read_bytes(),
            "package-lock.json": (self.remotion / "package-lock.json").read_bytes(),
            "remotion.config.ts": (self.remotion / "remotion.config.ts").read_bytes(),
        }
        for relative, replacement in (
            ("src/index.ts", b"export const value = 2;"),
            ("package-lock.json", b'{"lockfileVersion":3,"changed":true}'),
            ("remotion.config.ts", b"export default {codec: 'h264'};"),
            ("src/new-scene.tsx", b"export const NewScene = () => null;"),
            ("public/runtime.js", b"globalThis.injected = true;"),
        ):
            with self.subTest(relative=relative):
                for restore, payload in original.items():
                    (self.remotion / restore).write_bytes(payload)
                try:
                    (self.remotion / "src/new-scene.tsx").unlink()
                except FileNotFoundError:
                    pass
                template_trust.trust(self.project)
                path = self.remotion / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(replacement)
                with self.assertRaisesRegex(
                    template_trust.TrustError, "template_untrusted"
                ):
                    template_trust.authorize(self.project)

    def test_symlink_escape_is_refused_before_approval(self):
        outside = self.root / "outside.ts"
        outside.write_text("export const escaped = true;")
        (self.remotion / "src/escaped.ts").symlink_to(outside)
        with self.assertRaisesRegex(template_trust.TrustError, "symlink"):
            template_trust.trust(self.project)

    def test_bundled_narrated_code_is_recognized_without_user_ledger(self):
        shutil.rmtree(self.remotion)
        shutil.copytree(ROOT / "templates/narrated/remotion", self.remotion)
        authorization = template_trust.authorize(self.project)
        self.assertEqual(authorization["mode"], "bundled")
        self.assertFalse((self.project / ".hvp").exists())


if __name__ == "__main__":
    unittest.main()
