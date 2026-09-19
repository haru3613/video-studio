"""Explicit, synthetic installed policy for legacy workflow regression tests.

Production ships without a channel or enrolled signer. Tests which exercise
publishing must supply their own policy; they must never use the maintainer's.
"""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CHANNEL = "UC" + "a" * 22


class RuntimePolicyCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        import agent_status
        import approval_attestation
        import youtube_upload

        policy = json.loads(
            (Path(__file__).resolve().parents[1] / "pipeline/runtime-manifest.json").read_text()
        )
        policy["youtube_channel_id"] = CHANNEL
        self.test_runtime_policy = policy
        for module, name, result in (
            (agent_status, "runtime_manifest", policy),
            (approval_attestation, "_runtime_manifest", policy),
            (youtube_upload, "canonical_channel_id", CHANNEL),
        ):
            patch = mock.patch.object(module, name, side_effect=lambda value=result: copy.deepcopy(value))
            patch.start()
            self.addCleanup(patch.stop)

    def installed_tool(self, name):
        """A real subprocess installation with the same isolated test policy."""
        if not hasattr(self, "_test_installation"):
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            self._test_installation = Path(temporary.name)
            (self._test_installation / "tools").mkdir()
            (self._test_installation / "pipeline").mkdir()
            for source in Path(__file__).parent.glob("*.py"):
                shutil.copy2(source, self._test_installation / "tools" / source.name)
            (self._test_installation / "pipeline/runtime-manifest.json").write_text(
                json.dumps(self.test_runtime_policy)
            )
        return self._test_installation / "tools" / name
