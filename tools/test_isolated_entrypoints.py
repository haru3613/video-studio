#!/usr/bin/env python3
"""Every scripts/ entry point must import under `python3 -I`.

The wrappers in scripts/ run `/usr/bin/python3 -I -S`, and -I implies -P, so the
script's own directory is NOT placed on sys.path. A module that imports a
sibling therefore works under `python3 tools/thing.py` and fails through the
wrapper — which is exactly how a broken `make-publish-pack` shipped: the change
that made agent_status import canonical_layout was probed with a plain python3,
where the script directory is added automatically, and looked fine.

This runs each tool the way the wrappers do.
"""
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ENTRY_MODULES = [
    "agent_status.py",
    "make_publish_pack.py",
    "publish_approval.py",
    "canonical_layout.py",
    "render_project.py",
    "render_contract.py",
    "visual_qa_sample.py",
    "youtube_upload.py",
    "retime_visuals.py",
    "editorial_preview.py",
]


class IsolatedImportTest(unittest.TestCase):
    def test_entry_modules_import_under_isolated_python(self):
        for name in ENTRY_MODULES:
            with self.subTest(module=name):
                result = subprocess.run(
                    ["/usr/bin/python3", "-I", "-S", "-c",
                     f"import runpy, sys; sys.argv=['{name}','--help']; "
                     f"runpy.run_path('{TOOLS / name}', run_name='__main__')"],
                    capture_output=True, text=True,
                )
                self.assertNotIn(
                    "ModuleNotFoundError", result.stderr,
                    f"{name} cannot import under -I; a scripts/ wrapper will fail:\n"
                    f"{result.stderr[-400:]}",
                )


if __name__ == "__main__":
    unittest.main()
