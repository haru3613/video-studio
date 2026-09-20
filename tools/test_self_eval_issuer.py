#!/usr/bin/env python3
"""Signed operator self-eval issuer and authority regressions."""

from __future__ import annotations

import datetime as dt
import base64
import json
import os
import stat
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import publish_attestation_fixture
import render_self_eval as engine
import self_eval_authority as authority
import self_eval_fixture
import self_eval_issuer as issuer
from test_render_self_eval import write_project

WRAPPER = Path(__file__).resolve().parents[1] / "scripts/self-eval-issuer"


def stat_mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


class SelfEvalIssuerCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=str(Path(tempfile.gettempdir()).resolve()))
        self.root = Path(self.temporary.name)
        self.project = write_project(self.root / "source", name="signed-operator")
        self.state = self.root / "state"
        self.attestations = self.root / "attestations"
        self.signer = publish_attestation_fixture.generate_signer()
        # Run subprocess requests against an isolated installation with a test
        # public key, never the repository's intentionally unconfigured policy.
        installation = self.root / "installation"
        (installation / "tools").mkdir(parents=True)
        (installation / "scripts").mkdir()
        (installation / "pipeline").mkdir()
        source = Path(__file__).resolve().parents[1]
        for module in (source / "tools").glob("*.py"):
            shutil.copy2(module, installation / "tools" / module.name)
        self.wrapper = installation / "scripts/self-eval-issuer"
        shutil.copy2(WRAPPER, self.wrapper)
        policy = json.loads((source / "pipeline/runtime-manifest.json").read_text())
        policy["publish_approval_signer"] = self.signer["pin"]
        (installation / "pipeline/runtime-manifest.json").write_text(json.dumps(policy))
        self.environment = mock.patch.dict(
            os.environ,
            {
                authority.STATE_ROOT_ENV: str(self.state),
                authority.ATTESTATION_ROOT_ENV: str(self.attestations),
            },
        )
        self.environment.start()
        self.pin = mock.patch.object(
            authority, "load_signer_pin", return_value=self.signer["pin"]
        )
        self.pin.start()
        with engine.Snapshot(self.project) as snapshot:
            self_eval_fixture._start_pending(self.project, snapshot, 1)

    def tearDown(self):
        self.pin.stop()
        self.environment.stop()
        self.temporary.cleanup()

    def _write_signed_leaf(self, request, *, overrides=None):
        immutable = dict(request["attestation"])
        if overrides:
            immutable.update(overrides)
        signature = publish_attestation_fixture._sign(
            authority.canonical_signed_statement(immutable),
            self.signer["private_key"],
        )
        value = {
            **immutable,
            "signature_base64": base64.b64encode(signature).decode("ascii"),
            "consumed_at": None,
            "consumed_project_id": None,
            "consumed_intent_sha256": None,
        }
        self.attestations.mkdir(mode=0o700, parents=True, exist_ok=True)
        suffix = immutable["attestation_ref"].split(":", 1)[1]
        path = self.attestations / f"{suffix}.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
        return immutable["attestation_ref"], path

    def test_signed_unavailability_then_signed_human_pass(self):
        unavailable = issuer.prepare_request(
            self.project,
            issuer.unavailable_review(
                reviewed_by="Harvey",
                reason="No vision provider is configured in this local runtime.",
            ),
        )
        unavailable_ref, _ = self._write_signed_leaf(unavailable)
        pending = engine.record_review(
            self.project, unavailable["review_input"], unavailable_ref
        )
        self.assertEqual(pending["status"], engine.STATUS_NEEDS_HUMAN)
        unavailable_receipt = json.loads(
            (
                self.project
                / engine.attempt_dir(1)
                / "vision-unavailable.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            unavailable_receipt["authority"]["schema"],
            authority.VISION_ATTESTATION_SCHEMA_V2,
        )
        with self.assertRaisesRegex(ValueError, "already records vision unavailability"):
            issuer.prepare_request(
                self.project,
                issuer.unavailable_review(
                    reviewed_by="Harvey",
                    reason="No vision provider is configured in this local runtime.",
                ),
            )

        human = issuer.prepare_request(
            self.project,
            issuer.human_review(
                reviewed_by="Harvey",
                verdict="pass",
                notes="Reviewed the complete evidence set and final mix.",
            ),
        )
        human_ref, _ = self._write_signed_leaf(human)
        sealed = engine.record_review(self.project, human["review_input"], human_ref)
        self.assertEqual(sealed["status"], engine.STATUS_PASS)
        self.assertEqual(
            json.loads(
                (self.project / engine.CURRENT_REVIEW_PATH).read_text(encoding="utf-8")
            )["authority"]["schema"],
            authority.HUMAN_ATTESTATION_SCHEMA_V2,
        )
        self.assertIsNotNone(engine.current_pass(self.project))

    def test_interrupted_signed_v2_review_recovers_consumed_authority(self):
        request = issuer.prepare_request(
            self.project,
            issuer.unavailable_review(
                reviewed_by="Harvey",
                reason="No vision provider is configured in this local runtime.",
            ),
        )
        reference, _ = self._write_signed_leaf(request)
        original = authority.write_leaf_at

        def crash_before_generation(directory_fd, name, payload, *, exclusive=True):
            if name == "00000002.json":
                raise OSError("fixture crash after signed review promotion")
            return original(directory_fd, name, payload, exclusive=exclusive)

        with mock.patch.object(
            authority, "write_leaf_at", side_effect=crash_before_generation
        ):
            with self.assertRaisesRegex(OSError, "fixture crash"):
                engine.record_review(
                    self.project, request["review_input"], reference
                )
        recovered = engine.record_review(
            self.project, request["review_input"], reference
        )
        self.assertEqual(recovered["status"], engine.STATUS_NEEDS_HUMAN)
        self.assertEqual(
            len(list(self.state.rglob("generations/*.json"))),
            2,
        )

    def test_signed_statement_matches_cross_language_golden(self):
        immutable = {
            "schema": authority.VISION_ATTESTATION_SCHEMA_V2,
            "action": authority.VISION_UNAVAILABLE_ACTION,
            "generation": 1,
        }
        self.assertEqual(
            authority.canonical_signed_statement(immutable),
            b'{"action":"declare_vision_unavailable","attestation":{"action":"declare_vision_unavailable","generation":1,"schema":"haru.self_eval_vision_attestation.v2"},"schema":"haru.self_eval_authorization_statement.v1"}',
        )

    def test_operator_request_cannot_mint_a_model_verdict(self):
        current = engine.validate_current(self.project)
        forged = {
            "reviewer_kind": "vision",
            "verdict": "pass",
            "reviewed_by": "operator",
            "provider": "operator-declared",
            "model": "not-configured",
            "capability": "vision_provider_configuration.v1",
            "notes": "No provider configured.",
            "findings": [],
        }
        self.assertEqual(current["status"], engine.STATUS_NEEDS_HUMAN)
        with self.assertRaisesRegex(ValueError, "only declare"):
            issuer.prepare_request(self.project, forged)

    def test_isolated_cli_request_verifies_with_native_helper_when_provided(self):
        output = self.root / "operator-request.json"
        command = [
            str(self.wrapper),
            "prepare-unavailable",
            str(self.project),
            "--reviewed-by",
            "Harvey",
            "--reason",
            "No vision provider is configured in this local runtime.",
            "--output",
            str(output),
        ]
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(stat_mode(output), 0o600)
        request = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(request["schema"], issuer.REQUEST_SCHEMA)

        native = os.environ.get("HARU_SELF_EVAL_NATIVE_VERIFIER")
        if not native:
            return
        verified = subprocess.run(
            [native, "self-eval-verify", str(output)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr.decode("utf-8"))
        payload = json.loads(verified.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["attestation_ref"], request["attestation"]["attestation_ref"]
        )

    def test_python_fractional_human_fail_request_verifies_natively_when_provided(self):
        current = engine.validate_current(self.project)
        unavailable = {
            "reviewer_kind": "vision",
            "verdict": "unavailable",
            "reviewed_by": "legacy-external-issuer",
            "provider": "legacy-provider",
            "model": "legacy-model",
            "capability": "legacy-vision.v1",
            "notes": "Provider was unavailable before the operator signer existed.",
            "findings": [],
        }
        reference = self_eval_fixture._mint_vision(
            self.project,
            authority.canonical_digest(
                engine._vision_intent(self.project, current, unavailable)
            ),
        )
        engine.record_review(self.project, unavailable, reference)
        findings = self.root / "findings.json"
        findings.write_text(
            json.dumps(
                [
                    {
                        "timestamp_seconds": 1.25,
                        "boundary_id": "boundary-1",
                        "category": "visual_discontinuity",
                        "severity": "fail",
                        "message": "Visible discontinuity at the cut.",
                    }
                ]
            ),
            encoding="utf-8",
        )
        output = self.root / "human-fail-request.json"
        result = subprocess.run(
            [
                str(self.wrapper),
                "prepare-human-review",
                str(self.project),
                "--reviewed-by",
                "Harvey",
                "--verdict",
                "fail",
                "--notes",
                "Reviewed the full evidence and found a visible discontinuity.",
                "--findings-json",
                str(findings),
                "--output",
                str(output),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        request = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            request["review_intent"]["findings"][0]["timestamp_seconds"], 1.25
        )
        native = os.environ.get("HARU_SELF_EVAL_NATIVE_VERIFIER")
        if native:
            verified = subprocess.run(
                [native, "self-eval-verify", str(output)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )
            self.assertEqual(
                verified.returncode, 0, verified.stderr.decode("utf-8")
            )

    def test_human_request_requires_current_unavailability(self):
        review = issuer.human_review(reviewed_by="Harvey", verdict="pass")
        with self.assertRaisesRegex(ValueError, "vision-unavailable"):
            issuer.prepare_request(self.project, review)

    def test_human_v2_request_accepts_valid_legacy_external_unavailability(self):
        current = engine.validate_current(self.project)
        unavailable = {
            "reviewer_kind": "vision",
            "verdict": "unavailable",
            "reviewed_by": "legacy-external-issuer",
            "provider": "legacy-provider",
            "model": "legacy-model",
            "capability": "legacy-vision.v1",
            "notes": "Provider was unavailable before the operator signer existed.",
            "findings": [],
        }
        digest = authority.canonical_digest(
            engine._vision_intent(self.project, current, unavailable)
        )
        reference = self_eval_fixture._mint_vision(self.project, digest)
        engine.record_review(self.project, unavailable, reference)
        request = issuer.prepare_request(
            self.project,
            issuer.human_review(reviewed_by="Harvey", verdict="pass"),
        )
        self.assertEqual(
            request["attestation"]["schema"],
            authority.HUMAN_ATTESTATION_SCHEMA_V2,
        )

    def test_invalid_signature_and_signed_root_mismatch_fail_closed(self):
        request = issuer.prepare_request(
            self.project,
            issuer.unavailable_review(
                reviewed_by="Harvey",
                reason="No vision provider is configured in this local runtime.",
            ),
        )
        reference, path = self._write_signed_leaf(request)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["signature_base64"] = "MAMCAQE="
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "signature"):
            engine.record_review(self.project, request["review_input"], reference)

        path.unlink()
        reference, _ = self._write_signed_leaf(
            request, overrides={"project_root_sha256": "f" * 64}
        )
        with self.assertRaisesRegex(ValueError, "project root"):
            engine.record_review(self.project, request["review_input"], reference)

    def test_expired_signed_leaf_is_not_consumed(self):
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
        request = issuer.prepare_request(
            self.project,
            issuer.unavailable_review(
                reviewed_by="Harvey",
                reason="No vision provider is configured in this local runtime.",
            ),
            now=old,
        )
        reference, _ = self._write_signed_leaf(request)
        with self.assertRaisesRegex(ValueError, "validity interval"):
            engine.record_review(self.project, request["review_input"], reference)

    def test_consumed_signed_leaf_cannot_downgrade_to_unsigned_v1(self):
        request = issuer.prepare_request(
            self.project,
            issuer.unavailable_review(
                reviewed_by="Harvey",
                reason="No vision provider is configured in this local runtime.",
            ),
        )
        reference, path = self._write_signed_leaf(request)
        engine.record_review(self.project, request["review_input"], reference)
        receipt = json.loads(
            (self.project / engine.attempt_dir(1) / "vision-unavailable.json").read_text()
        )
        signed_authority = receipt["authority"]
        consumed = json.loads(path.read_text())
        legacy = {
            key: consumed[key]
            for key in authority.ATTESTATION_KEYS
        }
        legacy["schema"] = authority.VISION_ATTESTATION_SCHEMA
        path.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertFalse(
            authority.verify_consumed(
                signed_authority, self.project.name, self.project
            )
        )
        with self.assertRaisesRegex(ValueError, "protected authority"):
            engine.validate_current(self.project)


if __name__ == "__main__":
    unittest.main()
