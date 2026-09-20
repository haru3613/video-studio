#!/usr/bin/env python3
"""Security-boundary tests for signed one-time publish approvals."""
import datetime as dt
import json
import os
import tempfile
import unittest
from runtime_test_fixture import RuntimePolicyCase
from pathlib import Path
from unittest import mock

import approval_attestation as authority
import publish_attestation_fixture as fixture


class SignedPublishAttestationTest(RuntimePolicyCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "attestations"
        self.root.mkdir(mode=0o700)
        self.root.chmod(0o700)
        self.environment = mock.patch.dict(
            os.environ, {authority.ROOT_ENV: str(self.root)}
        )
        self.signer = fixture.generate_signer()
        self.pin = mock.patch.object(
            authority, "load_signer_pin", return_value=self.signer["pin"]
        )
        self.environment.start()
        self.pin.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.pin.stop)
        self.ref = fixture.attestation_ref("valid")
        self.project_id = "demo"
        self.project_root = Path(self.temporary.name) / "workspace-a/projects/demo"
        self.project_root.mkdir(parents=True)
        self.intent_sha256 = "a" * 64
        self.nonce = "b" * 64
        self.generation = 1

    def tearDown(self):
        self.temporary.cleanup()

    def leaf(self, **kwargs):
        return fixture.signed_leaf(
            self.ref,
            self.project_id,
            self.project_root,
            self.intent_sha256,
            self.nonce,
            self.generation,
            signer=self.signer,
            **kwargs,
        )

    def write(self, value):
        fixture.write_leaf(self.root, value)

    def consume(self, **overrides):
        return authority.consume(
            self.ref,
            overrides.get("project_id", self.project_id),
            overrides.get("intent_sha256", self.intent_sha256),
            overrides.get("nonce", self.nonce),
            overrides.get("generation", self.generation),
            project_root=overrides.get("project_root", self.project_root),
        )

    def test_valid_signature_consumes_once_and_replay_is_rejected(self):
        self.write(self.leaf())
        consumed = self.consume()
        self.assertTrue(
            authority.is_consumed(
                self.ref,
                self.project_id,
                self.intent_sha256,
                self.nonce,
                self.generation,
                project_root=self.project_root,
            )
        )
        self.assertEqual(consumed["consumed_project_id"], self.project_id)
        with self.assertRaisesRegex(ValueError, "already been consumed"):
            self.consume()

    def test_tampered_signed_field_is_rejected_without_consumption(self):
        value = self.leaf()
        value["project_id"] = "tampered"
        self.write(value)
        with self.assertRaisesRegex(ValueError, "signature is invalid"):
            self.consume(project_id="tampered")
        self.assertIsNone(json.loads(next(self.root.iterdir()).read_text())["consumed_at"])

    def test_signature_from_wrong_key_is_rejected(self):
        wrong_signer = fixture.generate_signer()
        self.write(self.leaf(private_key=wrong_signer["private_key"]))
        with self.assertRaisesRegex(ValueError, "signature is invalid"):
            self.consume()

    def test_signed_wrong_project_and_wrong_intent_are_rejected(self):
        cases = (
            ("project", {"immutable_overrides": {"project_id": "other"}}),
            (
                "intent",
                {"immutable_overrides": {"approval_intent_sha256": "c" * 64}},
            ),
        )
        for label, arguments in cases:
            with self.subTest(label=label):
                self.write(self.leaf(**arguments))
                with self.assertRaisesRegex(ValueError, "does not bind"):
                    self.consume()
                next(self.root.iterdir()).unlink()

    def test_expired_leaf_cannot_be_consumed(self):
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.write(
            self.leaf(
                issued_at=(now - dt.timedelta(minutes=6)).isoformat(),
                expires_at=(now - dt.timedelta(minutes=1)).isoformat(),
            )
        )
        with self.assertRaisesRegex(ValueError, "expired"):
            self.consume()

    def test_consumed_signature_remains_valid_after_expiry(self):
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        issued_at = now - dt.timedelta(minutes=6)
        expires_at = now - dt.timedelta(minutes=1)
        consumed_at = issued_at + dt.timedelta(minutes=1)
        self.write(
            self.leaf(
                issued_at=issued_at.isoformat(),
                expires_at=expires_at.isoformat(),
                consumed_at=consumed_at.isoformat(),
                consumed_project_id=self.project_id,
                consumed_intent_sha256=self.intent_sha256,
            )
        )
        self.assertTrue(
            authority.is_consumed(
                self.ref,
                self.project_id,
                self.intent_sha256,
                self.nonce,
                self.generation,
                project_root=self.project_root,
            )
        )

    def test_invalid_consumption_time_is_not_current_authority(self):
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        issued_at = now - dt.timedelta(minutes=6)
        expires_at = now - dt.timedelta(minutes=1)
        self.write(
            self.leaf(
                issued_at=issued_at.isoformat(),
                expires_at=expires_at.isoformat(),
                consumed_at=expires_at.isoformat(),
                consumed_project_id=self.project_id,
                consumed_intent_sha256=self.intent_sha256,
            )
        )
        self.assertFalse(
            authority.is_consumed(
                self.ref,
                self.project_id,
                self.intent_sha256,
                self.nonce,
                self.generation,
                project_root=self.project_root,
            )
        )

    def test_future_consumption_time_is_not_current_authority(self):
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.write(
            self.leaf(
                issued_at=now.isoformat(),
                expires_at=(now + dt.timedelta(minutes=5)).isoformat(),
                consumed_at=(now + dt.timedelta(minutes=1)).isoformat(),
                consumed_project_id=self.project_id,
                consumed_intent_sha256=self.intent_sha256,
            )
        )
        self.assertFalse(
            authority.is_consumed(
                self.ref,
                self.project_id,
                self.intent_sha256,
                self.nonce,
                self.generation,
                project_root=self.project_root,
            )
        )

    def test_unsigned_v1_leaf_is_rejected(self):
        value = {
            "schema": "haru.publish_attestation.v1",
            "attestation_ref": self.ref,
            "project_id": self.project_id,
            "approval_intent_sha256": self.intent_sha256,
            "nonce": self.nonce,
            "generation": self.generation,
            "issued_at": "2026-09-09T00:00:00+00:00",
            "consumed_at": None,
        }
        self.write(value)
        with self.assertRaisesRegex(ValueError, "malformed"):
            self.consume()
        with self.assertRaisesRegex(ValueError, "malformed"):
            authority.is_consumed(
                self.ref,
                self.project_id,
                self.intent_sha256,
                self.nonce,
                self.generation,
                project_root=self.project_root,
            )

    def test_same_basename_in_another_workspace_cannot_consume_leaf(self):
        other_root = Path(self.temporary.name) / "workspace-b/projects/demo"
        other_root.mkdir(parents=True)
        self.write(self.leaf())
        with self.assertRaisesRegex(ValueError, "does not bind"):
            self.consume(project_root=other_root)
        value = json.loads(next(self.root.iterdir()).read_text())
        self.assertIsNone(value["consumed_at"])

    def test_non_string_digest_nonce_and_key_id_fail_as_malformed(self):
        for field, replacement in (
            ("approval_intent_sha256", None),
            ("approval_intent_sha256", 7),
            ("nonce", None),
            ("nonce", 7),
            ("key_id", None),
            ("key_id", 7),
        ):
            with self.subTest(field=field, replacement=replacement):
                value = self.leaf()
                value[field] = replacement
                self.write(value)
                with self.assertRaisesRegex(ValueError, "malformed"):
                    self.consume()
                next(self.root.iterdir()).unlink()

    def test_missing_and_unknown_pins_fail_closed(self):
        self.write(self.leaf())
        with mock.patch.object(authority, "load_signer_pin", side_effect=ValueError("issuer_not_enrolled")):
            with self.assertRaisesRegex(ValueError, "issuer_not_enrolled"):
                self.consume()
        unknown = fixture.generate_signer()["pin"]
        with mock.patch.object(authority, "load_signer_pin", return_value=unknown):
            with self.assertRaisesRegex(ValueError, "issuer_not_enrolled"):
                self.consume()

    def test_hardlinked_leaf_is_rejected(self):
        path = fixture.write_leaf(self.root, self.leaf())
        os.link(path, self.root / "second-link.json")
        with self.assertRaisesRegex(ValueError, "links are invalid"):
            self.consume()

    def test_manifest_has_no_test_or_environment_pin_bypass(self):
        self.pin.stop()
        manifest_pin = authority._runtime_manifest()["publish_approval_signer"]
        with mock.patch.dict(os.environ, {
            "HARU_VIDEO_STUDIO_PUBLISH_APPROVAL_SIGNER": json.dumps(self.signer["pin"]),
            "HARU_VIDEO_STUDIO_PUBLISH_APPROVAL_KEY_ID": self.signer["pin"]["key_id"],
        }):
            if manifest_pin is None:
                with self.assertRaisesRegex(ValueError, "issuer_not_enrolled"):
                    authority.load_signer_pin()
            else:
                self.assertEqual(authority.load_signer_pin(), manifest_pin)
                self.write(self.leaf())
                with self.assertRaisesRegex(ValueError, "issuer_not_enrolled"):
                    self.consume()
            with mock.patch.object(authority, "_runtime_manifest", return_value={"publish_approval_signer": None}):
                with self.assertRaisesRegex(ValueError, "issuer_not_enrolled"):
                    authority.load_signer_pin()


if __name__ == "__main__":
    unittest.main()
