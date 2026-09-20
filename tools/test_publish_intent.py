#!/usr/bin/env python3
"""Read-only publish approval signing-request contracts."""
import contextlib
import datetime as dt
import hashlib
import io
import json
import sys
import tempfile
import unittest
from runtime_test_fixture import RuntimePolicyCase
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_status  # noqa: E402
import publish_approval  # noqa: E402
from test_agent_status import make_ready_project  # noqa: E402


class Args:
    def __init__(self, workspace, project, override_reason=""):
        self.workspace = workspace
        self.project = project
        self.override_reason = override_reason


class PublishIntentTest(RuntimePolicyCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = make_ready_project(self.root)
        self.pin = {
            "algorithm": "ecdsa-p256-sha256",
            "key_id": "test-p256-key",
            "public_key_x963_base64": "BAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        }
        self.args = Args(self.root, str(self.project))

    def tearDown(self):
        self._tmp.cleanup()

    def prepare(self, args=None):
        output = io.StringIO()
        with mock.patch.object(
            publish_approval.approval_attestation,
            "load_signer_pin",
            return_value=self.pin,
        ), contextlib.redirect_stdout(output):
            code = publish_approval.cmd_prepare(args or self.args)
        return code, json.loads(output.getvalue())

    def files(self):
        return {
            path.relative_to(self.project).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.project.rglob("*")
            if path.is_file()
        }

    def test_ready_project_produces_exact_short_lived_request_without_writes(self):
        before = self.files()
        code, request = self.prepare()
        after = self.files()

        self.assertEqual(code, 0)
        self.assertEqual(after, before)
        self.assertEqual(request["schema"], publish_approval.SIGNING_REQUEST_SCHEMA)
        self.assertEqual(request["project_root"], str(self.project.resolve()))
        self.assertEqual(
            request["intent_sha256"],
            agent_status.approval_intent_sha256(request["intent"]),
        )
        status = agent_status.build(self.project, self.root)[0]
        self.assertEqual(
            request["intent"]["render_self_eval"],
            status["approval_intent_refs"]["render_self_eval"],
        )
        self.assertEqual(
            request["intent"]["visual_qa_review"],
            status["approval_intent_refs"]["visual_qa_review"],
        )
        attestation = request["attestation"]
        self.assertEqual(
            set(attestation),
            {
                "schema",
                "attestation_ref",
                "project_id",
                "project_root_sha256",
                "approval_intent_sha256",
                "nonce",
                "generation",
                "issued_at",
                "expires_at",
                "channel_id",
                "visibility",
                "key_id",
                "signature_algorithm",
            },
        )
        self.assertEqual(attestation["approval_intent_sha256"], request["intent_sha256"])
        self.assertEqual(
            attestation["project_root_sha256"],
            hashlib.sha256(str(self.project.resolve()).encode("utf-8")).hexdigest(),
        )
        self.assertEqual(attestation["attestation_ref"], request["intent"]["attestation_ref"])
        self.assertEqual(attestation["nonce"], request["intent"]["nonce"])
        self.assertEqual(attestation["generation"], request["intent"]["generation"])
        self.assertEqual(len(attestation["nonce"]), 64)
        int(attestation["nonce"], 16)
        issued = dt.datetime.fromisoformat(attestation["issued_at"])
        expires = dt.datetime.fromisoformat(attestation["expires_at"])
        self.assertEqual(expires - issued, dt.timedelta(seconds=300))
        self.assertEqual(attestation["channel_id"], request["intent"]["channel_id"])
        self.assertEqual(attestation["visibility"], "unlisted")
        self.assertEqual(attestation["key_id"], self.pin["key_id"])

    def test_each_request_has_fresh_nonce_and_attestation_ref(self):
        _, first = self.prepare()
        _, second = self.prepare()
        self.assertNotEqual(first["attestation"]["nonce"], second["attestation"]["nonce"])
        self.assertNotEqual(
            first["attestation"]["attestation_ref"],
            second["attestation"]["attestation_ref"],
        )

    def test_project_change_makes_the_prepared_intent_stale(self):
        _, prepared = self.prepare()
        metadata = self.project / "publish-metadata.json"
        value = json.loads(metadata.read_text(encoding="utf-8"))
        value["title"] = "Changed after the signing request was prepared"
        metadata.write_text(json.dumps(value), encoding="utf-8")

        _, current = self.prepare()
        self.assertNotEqual(prepared["intent_sha256"], current["intent_sha256"])
        self.assertNotEqual(
            prepared["intent"]["metadata_sha256"],
            current["intent"]["metadata_sha256"],
        )

    def test_same_project_name_in_another_workspace_has_a_distinct_root_binding(self):
        _, first = self.prepare()
        other_root = self.root / "other-workspace"
        other_project = make_ready_project(other_root, self.project.name)
        _, second = self.prepare(Args(other_root, str(other_project)))
        self.assertEqual(first["intent"]["project_id"], second["intent"]["project_id"])
        self.assertNotEqual(
            first["attestation"]["project_root_sha256"],
            second["attestation"]["project_root_sha256"],
        )

    def test_next_generation_comes_from_prior_approval(self):
        path = self.project / "publish" / publish_approval.RECEIPT_NAME
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"generation": 8}), encoding="utf-8")
        code, request = self.prepare()
        self.assertEqual(code, 0)
        self.assertEqual(request["attestation"]["generation"], 9)

        path.write_text(json.dumps({"generation": True}), encoding="utf-8")
        code, result = self.prepare()
        self.assertEqual(code, 1)
        self.assertEqual(result["code"], "prior_generation_invalid")

        path.write_text("not-json", encoding="utf-8")
        code, result = self.prepare()
        self.assertEqual(code, 1)
        self.assertEqual(result["code"], "prior_generation_invalid")

    def test_not_ready_and_missing_pin_are_blocked_without_writes(self):
        (self.project / "output" / "final.mp4").unlink()
        before = self.files()
        code, result = self.prepare()
        self.assertEqual(code, 1)
        self.assertEqual(result["code"], "not_ready")
        self.assertEqual(self.files(), before)

        ready = make_ready_project(self.root, "missing-pin")
        output = io.StringIO()
        with mock.patch.object(
            publish_approval.approval_attestation,
            "load_signer_pin",
            side_effect=ValueError("issuer_not_enrolled"),
        ), contextlib.redirect_stdout(output):
            code = publish_approval.cmd_prepare(Args(self.root, str(ready)))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "issuer_not_enrolled")
        self.assertFalse((ready / "publish" / publish_approval.RECEIPT_NAME).exists())

    def test_legacy_caller_identity_and_credential_text_are_rejected(self):
        with mock.patch.object(
            sys,
            "argv",
            ["hvp-approve", "prepare", str(self.project), "--approved-by", "harvey"],
        ):
            with self.assertRaises(SystemExit) as exit_code:
                publish_approval.main()
        self.assertEqual(exit_code.exception.code, 2)

        code, result = self.prepare(
            Args(self.root, str(self.project), "Bearer secret-value that must not pass")
        )
        self.assertEqual(code, 1)
        self.assertEqual(result["code"], "credential_in_override_reason")


if __name__ == "__main__":
    unittest.main()
