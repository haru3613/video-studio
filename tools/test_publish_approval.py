#!/usr/bin/env python3
"""Focused contracts for v3 self-eval/review-bound publish approval."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from runtime_test_fixture import RuntimePolicyCase
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_status  # noqa: E402
import approval_attestation  # noqa: E402
import publish_approval  # noqa: E402
import publish_attestation_fixture  # noqa: E402
import visual_qa_sample  # noqa: E402
from test_agent_status import make_ready_project, seal_self_eval, record_final_quality_fixture  # noqa: E402


class Args:
    def __init__(self, **kw):
        self.workspace = kw.pop("workspace")
        self.project = kw.pop("project")
        self.attestation_ref = kw.pop("attestation_ref", None)
        self.override_reason = kw.pop("override_reason", "")
        self.video_id = kw.pop("video_id", None)
        self.uploaded_at = kw.pop("uploaded_at", None)
        self.visibility = kw.pop("visibility", agent_status.PUBLISH_VISIBILITY)
        # Legacy authority assertions intentionally survive only as inert test
        # attributes; cmd_approve must not read them.
        self.approved_by = kw.pop("approved_by", "attacker")
        self.channel_id = kw.pop("channel_id", "UCaaaaaaaaaaaaaaaaaaaaaa")
        self.reapprove = kw.pop("reapprove", True)
        for key, value in kw.items():
            setattr(self, key, value)


class PublishApprovalV3Test(RuntimePolicyCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = make_ready_project(self.root)
        self.attestations = self.root / "protected-attestations"
        self.attestations.mkdir(mode=0o700)
        self.attestations.chmod(0o700)
        self.signer = publish_attestation_fixture.generate_signer()
        self.env = mock.patch.dict(
            os.environ,
            {approval_attestation.ROOT_ENV: str(self.attestations)},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.pin = mock.patch.object(
            approval_attestation,
            "load_signer_pin",
            return_value=self.signer["pin"],
        )
        self.pin.start()
        self.addCleanup(self.pin.stop)
        self.args = {"workspace": self.root, "project": str(self.project)}

    def tearDown(self):
        self._tmp.cleanup()

    def status(self):
        return agent_status.build(self.project, self.root)[0]

    def receipt(self):
        return json.loads((self.project / "publish" / "publish-approval.json").read_text())

    def attest(self, ref, *, project=None, generation=1, reason=None):
        project = project or self.project
        label = ref.rsplit(":", 1)[-1]
        ref = publish_attestation_fixture.attestation_ref(label)
        status, _ = agent_status.build(project, self.root)
        contract = json.loads((project / "project-contract.json").read_text())
        nonce = hashlib.sha256(f"nonce-{label}".encode("utf-8")).hexdigest()
        warnings = sorted(
            warning
            for warning in status["warnings"]
            if warning != agent_status.STALE_APPROVAL_WARNING
        )
        refs = status["approval_intent_refs"]
        intent = agent_status.approval_intent(
            project.name,
            status["canonical_artifacts"],
            contract,
            warnings,
            reason,
            ref,
            nonce,
            generation,
            render_self_eval_ref=refs["render_self_eval"],
            visual_qa_review_ref=refs["visual_qa_review"],
        )
        self.assertIsNotNone(intent, "fixture must form a complete v3 approval intent")
        value = publish_attestation_fixture.signed_leaf(
            ref,
            project.name,
            project,
            agent_status.approval_intent_sha256(intent),
            nonce,
            generation,
            signer=self.signer,
        )
        publish_attestation_fixture.write_leaf(self.attestations, value)
        return ref

    def approve(self, ref, **kw):
        return publish_approval.cmd_approve(
            Args(**{**self.args, "attestation_ref": ref, **kw})
        )

    def test_missing_target_is_read_only_and_cannot_approve(self):
        contract_path = self.project / "project-contract.json"
        contract = json.loads(contract_path.read_text())
        del contract["publish_target"]
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        self.assertEqual(self.status()["stages"]["publish_target"]["status"], "missing")
        self.assertEqual(self.approve("attestation:missing"), 1)
        self.assertFalse((self.project / "publish" / "publish-approval.json").exists())

    def test_malformed_target_is_read_only_and_cannot_approve(self):
        contract_path = self.project / "project-contract.json"
        contract = json.loads(contract_path.read_text())
        contract["publish_target"] = {"youtube_channel_id": "not-a-channel"}
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        self.assertEqual(self.status()["stages"]["publish_target"]["status"], "missing")
        self.assertEqual(self.approve("attestation:malformed"), 1)
        self.assertFalse((self.project / "publish" / "publish-approval.json").exists())

    def test_target_change_stales_prior_pack_and_approval_without_erasing_history(self):
        ref = self.attest("attestation:target-v1")
        self.assertEqual(self.approve(ref), 0)
        contract_path = self.project / "project-contract.json"
        contract = json.loads(contract_path.read_text())
        del contract["publish_target"]
        contract_path.write_text(json.dumps(contract), encoding="utf-8")
        status = self.status()
        self.assertEqual(status["stages"]["publish_target"]["status"], "missing")
        self.assertEqual(status["stages"]["publish_pack"]["status"], "missing")
        self.assertEqual(status["publish_approval"]["state"], "stale")
        self.assertTrue((self.project / "publish" / "publish-approval.json").exists())

    def test_one_time_attestation_approves_then_replay_is_refused(self):
        ref = self.attest("attestation:one")
        self.assertEqual(self.approve(ref), 0)
        receipt = self.receipt()
        self.assertEqual(self.status()["publish_approval"]["state"], "valid")
        self.assertTrue(receipt["approval_intent_sha256"])
        self.assertEqual(receipt["channel_id"], "UCaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(receipt["schema"], "haru.publish_approval.v3")
        self.assertEqual(
            receipt["render_self_eval"],
            self.status()["approval_intent_refs"]["render_self_eval"],
        )
        self.assertEqual(
            receipt["visual_qa_review"],
            self.status()["approval_intent_refs"]["visual_qa_review"],
        )
        value = json.loads(
            (self.attestations / f"{ref.split(':', 1)[1]}.json").read_text()
        )
        self.assertEqual(value["consumed_intent_sha256"], receipt["approval_intent_sha256"])
        self.assertEqual(self.approve(ref), 1)

    def test_consumed_receipt_copied_to_same_basename_in_other_workspace_is_stale(self):
        ref = self.attest("attestation:workspace-bound")
        self.assertEqual(self.approve(ref), 0)
        receipt = self.receipt()
        other_project = self.root / "other-workspace/projects" / self.project.name
        (other_project / "publish").mkdir(parents=True)
        shutil.copy2(
            self.project / "publish/publish-approval.json",
            other_project / "publish/publish-approval.json",
        )
        contract = {
            "publish_target": {
                "youtube_channel_id": receipt["channel_id"],
            },
            "runtime_contract": receipt["runtime_contract"],
        }
        canonical = {
            "final_video": {
                "sha256": receipt["final_sha256"],
                "bytes": receipt["final_bytes"],
            },
            "publish_metadata": {"sha256": receipt["metadata_sha256"]},
            "cover": {"sha256": receipt["cover_sha256"]},
        }
        state = agent_status.read_publish_approval(
            other_project,
            canonical,
            contract,
            receipt["warnings_acknowledged"],
            receipt["render_self_eval"],
            receipt["visual_qa_review"],
        )
        self.assertEqual(state["state"], "stale")
        self.assertEqual(
            state["note"],
            "the protected human attestation is absent, stale, or not consumed",
        )

    def test_stale_or_wrong_project_attestation_is_refused(self):
        ref = self.attest("attestation:stale")
        metadata = self.project / "publish-metadata.json"
        value = json.loads(metadata.read_text())
        value["title"] = "Changed title remains valid metadata"
        metadata.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.approve(ref), 1)

        other = make_ready_project(self.root, "other")
        other_ref = self.attest("attestation:wrong-project", project=self.project)
        other_args = Args(workspace=self.root, project=str(other), attestation_ref=other_ref)
        self.assertEqual(publish_approval.cmd_approve(other_args), 1)

    def test_every_bound_intent_field_invalidates_validity(self):
        ref = self.attest("attestation:bound")
        self.assertEqual(self.approve(ref), 0)
        original = self.receipt()
        mutations = {
            "project_id": "another-project",
            "final_sha256": "0" * 64,
            "final_bytes": original["final_bytes"] + 1,
            "metadata_sha256": "1" * 64,
            "cover_sha256": "2" * 64,
            "channel_id": "UCbbbbbbbbbbbbbbbbbbbbbb",
            "visibility": "private",
            "warnings_acknowledged": ["new warning"],
            "override_reason": "a materially different override reason",
            "runtime_contract": {"schema": "haru.project_runtime_contract.v1"},
            "render_self_eval": {
                "path": original["render_self_eval"]["path"],
                "sha256": "3" * 64,
                "bytes": original["render_self_eval"]["bytes"],
            },
            "visual_qa_review": {
                "path": original["visual_qa_review"]["path"],
                "sha256": "4" * 64,
                "bytes": original["visual_qa_review"]["bytes"],
            },
            "generation": original["generation"] + 1,
            "attestation_ref": "attestation:other",
            "nonce": "another-nonce",
            "approval_intent_sha256": "f" * 64,
        }
        receipt_path = self.project / "publish" / "publish-approval.json"
        for field, replacement in mutations.items():
            with self.subTest(field=field):
                candidate = dict(original)
                candidate[field] = replacement
                receipt_path.write_text(json.dumps(candidate), encoding="utf-8")
                self.assertEqual(self.status()["publish_approval"]["state"], "stale")
        receipt_path.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(self.status()["publish_approval"]["state"], "valid")

    def test_reapproval_requires_a_fresh_attestation_and_new_generation(self):
        first = self.attest("attestation:first", generation=1)
        self.assertEqual(self.approve(first), 0)
        second = self.attest("attestation:second", generation=2)
        self.assertEqual(self.approve(second), 0)
        receipt = self.receipt()
        self.assertEqual(receipt["generation"], 2)
        self.assertEqual(receipt["attestation_ref"], second)
        self.assertTrue((self.project / "publish" / "approval-history").is_dir())
        self.assertEqual(self.approve(first), 1)

    def test_old_v2_receipt_is_unknown_and_cannot_authorize_publish(self):
        ref = self.attest("attestation:old-schema")
        self.assertEqual(self.approve(ref), 0)
        receipt_path = self.project / "publish" / "publish-approval.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["schema"] = "haru.publish_approval.v2"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        status = self.status()

        self.assertEqual(status["publish_approval"]["state"], "absent")
        self.assertEqual(
            status["publish_approval"]["note"],
            "approval receipt has an unknown schema",
        )
        self.assertEqual(
            status["overall_status"], "ready_for_human_upload_approval"
        )

    def test_replacing_current_visual_review_stales_approval_until_reapproval(self):
        """Approval P bound to review A cannot survive replacement review B."""
        first = self.attest("attestation:review-a", generation=1)
        self.assertEqual(self.approve(first), 0)
        approval_a = self.receipt()
        review_a = approval_a["visual_qa_review"]
        self.assertEqual(self.status()["overall_status"], "publish_approved")

        visual_qa_sample.record_review(
            self.project,
            reviewed_by="replacement-reviewer",
            verdict="pass",
            notes="Replacement review B inspected the current complete sample.",
        )

        stale = self.status()
        review_b = stale["approval_intent_refs"]["visual_qa_review"]
        self.assertNotEqual(review_a, review_b)
        self.assertEqual(stale["publish_approval"]["state"], "stale")
        self.assertEqual(
            stale["overall_status"], "in_progress"
        )
        self.assertEqual(
            self.receipt(),
            approval_a,
            "replacing HVP-21 evidence must not rewrite approval P into looking current",
        )

        self.assertIn("qa_not_passed", {b["code"] for b in stale["blocker_details"]})
        # A replacement human verdict also invalidates the prior machine QA
        # binding. The fixture producer must issue current evidence again.
        record_final_quality_fixture(self.project)
        self.assertEqual(self.status()["overall_status"], "ready_for_human_upload_approval")
        second = self.attest("attestation:review-b", generation=2)
        self.assertEqual(self.approve(second), 0)
        current = self.status()
        self.assertEqual(current["overall_status"], "publish_approved")
        self.assertEqual(self.receipt()["visual_qa_review"], review_b)
        self.assertEqual(self.receipt()["generation"], 2)

    def test_a_new_self_eval_pass_alone_never_resurrects_old_human_receipts(self):
        """Pass B invalidates HVP-21 A and HVP-28 P transitively."""
        first = self.attest("attestation:self-eval-a", generation=1)
        self.assertEqual(self.approve(first), 0)
        approval_a = self.receipt()
        self.assertEqual(self.status()["overall_status"], "publish_approved")

        # SRT bytes are one of the fixed self-eval identity inputs. The render
        # bytes do not change, isolating the transitive binding this test names.
        srt = self.project / "narration-final.srt"
        srt.write_text(srt.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        current = seal_self_eval(self.project, self.root, attempt=2)
        self.assertEqual(current["attempt"], 2)

        status = self.status()
        self.assertEqual(status["stages"]["render_self_eval"]["status"], "pass")
        self.assertNotEqual(
            status["approval_intent_refs"]["render_self_eval"],
            approval_a["render_self_eval"],
        )
        self.assertIsNone(
            status["approval_intent_refs"]["visual_qa_review"],
            "the old HVP-21 review must not become current merely because pass B exists",
        )
        self.assertEqual(status["publish_approval"]["state"], "stale")
        self.assertEqual(status["overall_status"], "in_progress")
        self.assertEqual(
            self.receipt(),
            approval_a,
            "pass B must not mutate approval P into binding evidence it postdates",
        )

    def test_caller_asserted_authority_is_rejected_or_inert(self):
        self.assertEqual(
            self.approve(
                "attestation:not-issued",
                approved_by="harvey",
                channel_id="UCaaaaaaaaaaaaaaaaaaaaaa",
                reapprove=True,
            ),
            1,
        )
        with mock.patch.object(
            sys,
            "argv",
            [
                "hvp-approve",
                "approve",
                str(self.project),
                "--attestation-ref",
                "attestation:unused",
                "--approved-by",
                "harvey",
            ],
        ):
            with self.assertRaises(SystemExit) as exit_code:
                publish_approval.main()
        self.assertEqual(exit_code.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
