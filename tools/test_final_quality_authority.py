#!/usr/bin/env python3
import hashlib
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import final_quality_authority as authority


class FinalQualityAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "projects/demo"
        self.project.mkdir(parents=True)
        self.state = self.root / "state"
        self.environment = mock.patch.dict(
            os.environ,
            {authority.authority.STATE_ROOT_ENV: str(self.state)},
        )
        self.environment.start()
        self.inputs, self.outputs = self.write_refs(self.project)

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    @staticmethod
    def ref(project, relative, content):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }

    def write_refs(self, project):
        inputs = {
            name: self.ref(project, relative, f"input:{name}".encode())
            for name, relative in authority.INPUT_PATHS.items()
        }
        outputs = {
            name: self.ref(project, relative, f"output:{name}".encode())
            for name, relative in authority.OUTPUT_PATHS.items()
        }
        return inputs, outputs

    def proof_path(self, project=None):
        project = project or self.project
        return (
            self.state
            / authority.authority.project_path_sha256(project)
            / authority.DIRECTORY
            / authority.LEAF
        )

    def test_missing_proof_fails_without_creating_state(self):
        self.assertFalse(authority.validate(self.project, self.inputs, self.outputs))
        self.assertFalse(self.state.exists())

    def test_recorded_current_proof_validates_with_owner_only_storage(self):
        proof = authority._record(self.project, self.inputs, self.outputs)

        self.assertEqual(proof["schema"], authority.SCHEMA)
        self.assertTrue(authority.validate(self.project, self.inputs, self.outputs))
        proof_path = self.proof_path()
        self.assertEqual(stat.S_IMODE(proof_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(proof_path.parent.stat().st_mode), 0o700)

    def test_tampered_input_or_output_and_stale_refs_fail(self):
        authority._record(self.project, self.inputs, self.outputs)

        (self.project / authority.INPUT_PATHS["render_result"]).write_bytes(
            b"changed input"
        )
        self.assertFalse(authority.validate(self.project, self.inputs, self.outputs))

        self.inputs, self.outputs = self.write_refs(self.project)
        authority._record(self.project, self.inputs, self.outputs)
        (self.project / authority.OUTPUT_PATHS["review"]).write_bytes(
            b"rolled back project receipt"
        )
        self.assertFalse(authority.validate(self.project, self.inputs, self.outputs))

    def test_copied_project_path_cannot_reuse_proof_and_validation_is_read_only(self):
        authority._record(self.project, self.inputs, self.outputs)
        copied = self.root / "copied/demo"
        shutil.copytree(self.project, copied)
        copied_inputs, copied_outputs = self.write_refs(copied)
        copied_namespace = self.state / authority.authority.project_path_sha256(copied)

        self.assertFalse(authority.validate(copied, copied_inputs, copied_outputs))
        self.assertFalse(copied_namespace.exists())

    def test_symlinked_state_root_or_project_output_fails_closed(self):
        actual_state = self.root / "actual-state"
        actual_state.mkdir(mode=0o700)
        linked_state = self.root / "linked-state"
        linked_state.symlink_to(actual_state, target_is_directory=True)
        with mock.patch.dict(
            os.environ,
            {authority.authority.STATE_ROOT_ENV: str(linked_state)},
        ):
            self.assertFalse(
                authority.validate(self.project, self.inputs, self.outputs)
            )
            with self.assertRaises(authority.FinalQualityAuthorityError):
                authority._record(self.project, self.inputs, self.outputs)

        os.environ[authority.authority.STATE_ROOT_ENV] = str(self.state)
        authority._record(self.project, self.inputs, self.outputs)
        review = self.project / authority.OUTPUT_PATHS["review"]
        target = self.project / "replacement-review.json"
        target.write_bytes(review.read_bytes())
        review.unlink()
        review.symlink_to(target)
        self.assertFalse(authority.validate(self.project, self.inputs, self.outputs))

    def test_malformed_or_wrong_project_proof_fails_closed(self):
        authority._record(self.project, self.inputs, self.outputs)
        proof_path = self.proof_path()
        proof_path.write_text("{}", encoding="utf-8")
        proof_path.chmod(0o600)

        self.assertFalse(authority.validate(self.project, self.inputs, self.outputs))


if __name__ == "__main__":
    unittest.main()
