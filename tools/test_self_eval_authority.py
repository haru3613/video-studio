#!/usr/bin/env python3
"""Forgery, replay, rollback, nofollow, and crash recovery regressions."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import render_self_eval as engine
import self_eval_authority as authority
import self_eval_fixture
from test_render_self_eval import refresh_marker, write_project


class AuthorityCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = write_project(self.root / "source", name="same-name")
        self.state = self.root / "state"
        self.attestations = self.root / "attestations"
        self.environment = mock.patch.dict(
            os.environ,
            {
                authority.STATE_ROOT_ENV: str(self.state),
                authority.ATTESTATION_ROOT_ENV: str(self.attestations),
            },
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_project_tree_copy_cannot_copy_external_pass_authority(self):
        self_eval_fixture.seal(self.project, status="pass", attempt=1)
        copied = self.root / "copied" / self.project.name
        copied.parent.mkdir()
        shutil.copytree(self.project, copied)
        self.assertIsNone(engine.current_pass(copied))
        with self.assertRaisesRegex(ValueError, "anchor|ledger"):
            engine.validate_current(copied)

    def test_exact_project_result_rollback_is_rejected_by_latest_anchor(self):
        self_eval_fixture.seal(self.project, status="pass", attempt=1)
        old = (self.project / engine.RESULT_PATH).read_bytes()
        (self.project / "output/final.mp4").write_bytes(b"second")
        refresh_marker(self.project)
        self_eval_fixture.seal(self.project, status="pass", attempt=2)
        (self.project / engine.RESULT_PATH).write_bytes(old)
        with self.assertRaisesRegex(
            ValueError, "anchored|history|describes|does not match"
        ):
            engine.validate_current(self.project)

    def test_missing_or_modified_pointer_fails_closed(self):
        self_eval_fixture.seal(self.project, status="pass", attempt=1)
        pointer = next(self.state.rglob("current.json"))
        pointer.write_text("{}", encoding="utf-8")
        os.chmod(pointer, 0o600)
        with self.assertRaisesRegex(ValueError, "pointer"):
            engine.validate_current(self.project)

    def test_predecessor_break_fails_closed(self):
        self_eval_fixture.seal(self.project, status="pass", attempt=1)
        generation = next(self.state.rglob("generations/00000002.json"))
        value = json.loads(generation.read_text())
        value["predecessor"]["sha256"] = "0" * 64
        generation.write_bytes(authority.canonical_bytes(value))
        os.chmod(generation, 0o600)
        with self.assertRaisesRegex(ValueError, "predecessor"):
            engine.validate_current(self.project)

    def test_immutable_prior_attempt_substitution_breaks_ledger_history(self):
        self_eval_fixture.seal(self.project, status="fail", attempt=1)
        (self.project / "output/final.mp4").write_bytes(b"second-video")
        refresh_marker(self.project)
        self_eval_fixture.seal(self.project, status="fail", attempt=2)
        prior = self.project / f"{engine.attempt_dir(1)}/outcome.json"
        prior.write_bytes(prior.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "immutable attempt history"):
            engine.validate_current(self.project)

    def test_unanchored_internally_consistent_result_never_passes(self):
        result_path = self.project / engine.RESULT_PATH
        result_path.parent.mkdir(parents=True)
        result_path.write_text(
            json.dumps(
                {
                    "schema": engine.RESULT_SCHEMA,
                    "project": self.project.name,
                    "status": "pass",
                }
            ),
            encoding="utf-8",
        )
        self.assertIsNone(engine.current_pass_ref(self.project))

    def test_attestation_is_consumed_once_and_changed_intent_cannot_replay(self):
        with engine.Snapshot(self.project) as snapshot:
            self_eval_fixture._start_pending(self.project, snapshot, 1)
        current = engine.validate_current(self.project)
        review = {
            "reviewer_kind": "vision",
            "verdict": "pass",
            "reviewed_by": "fixture",
            "provider": "fixture",
            "model": "fixture",
            "capability": "vision.v1",
            "notes": "",
            "findings": [],
        }
        digest = authority.canonical_digest(
            engine._vision_intent(self.project, current, review)
        )
        ref = self_eval_fixture._mint_vision(self.project, digest)
        first = engine.record_review(self.project, review, ref)
        self.assertEqual(first["status"], "pass")
        self.assertEqual(engine.record_review(self.project, review, ref), first)
        changed = dict(review)
        changed["notes"] = "changed after attestation"
        with self.assertRaisesRegex(ValueError, "pending review|bind|attestation"):
            engine.record_review(self.project, changed, ref)

    def test_unbacked_vision_review_creates_no_receipt(self):
        with engine.Snapshot(self.project) as snapshot:
            self_eval_fixture._start_pending(self.project, snapshot, 1)
        current_path = self.project / engine.RESULT_PATH
        before = current_path.read_bytes()
        review = {
            "reviewer_kind": "vision",
            "verdict": "pass",
            "reviewed_by": "forged",
            "provider": "forged",
            "model": "forged",
            "capability": "vision.v1",
            "notes": "",
            "findings": [],
        }
        with self.assertRaisesRegex(ValueError, "unavailable"):
            engine.record_review(
                self.project,
                review,
                "self-eval-attestation:does-not-exist",
            )
        self.assertEqual(current_path.read_bytes(), before)
        self.assertFalse(
            (self.project / f"{engine.attempt_dir(1)}/review.json").exists()
        )
        self.assertFalse(
            (self.project / f"{engine.attempt_dir(1)}/outcome.json").exists()
        )
    def test_attestation_symlink_and_path_escape_are_rejected(self):
        self.attestations.mkdir(mode=0o700)
        outside = self.root / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        os.chmod(outside, 0o600)
        (self.attestations / "linked.json").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            authority.consume(
                authority.Chain([], []),
                authority.VISION_ATTESTATION_SCHEMA,
                "self-eval-attestation:linked",
                self.project.name,
                "1" * 64,
            )
        for malformed in (
            "linked",
            "self-eval-attestation:../linked",
            "self-eval-attestation:/absolute",
            "self-eval-attestation:",
        ):
            with self.assertRaisesRegex(ValueError, "malformed"):
                authority.attestation_path(malformed)

    def test_generation_leaf_symlink_fails_nofollow(self):
        self_eval_fixture.seal(self.project, status="pass", attempt=1)
        generation = next(self.state.rglob("generations/00000001.json"))
        saved = self.root / "saved-generation.json"
        generation.rename(saved)
        generation.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            engine.validate_current(self.project)

    def test_project_result_symlink_fails_nofollow_without_reading_target(self):
        self_eval_fixture.seal(self.project, status="pass", attempt=1)
        result = self.project / engine.RESULT_PATH
        outside = self.root / "outside-result.json"
        result.rename(outside)
        result.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            engine.validate_current(self.project)
        self.assertIsNone(engine.current_pass(self.project))

    def test_first_generation_retires_legacy_human_receipts_once(self):
        visual = self.project / "quality-review/visual-sampling"
        publish = self.project / "publish"
        visual.mkdir(parents=True)
        publish.mkdir()
        legacy = {
            visual / "visual-qa-sample.json": b'{"schema":"haru.visual_qa_sample.v1"}',
            visual / "visual-qa-contact-sheet.png": b"legacy-png",
            visual / "visual-qa-index.md": b"legacy-index",
            visual / "visual-qa-review.json": b'{"schema":"haru.visual_qa_review.v1"}',
            publish / "publish-approval.json": b'{"schema":"haru.publish_approval.v2"}',
        }
        for path, payload in legacy.items():
            path.write_bytes(payload)

        self_eval_fixture.seal(self.project, status="pass", attempt=1)

        retired = visual / "retired-pre-self-eval"
        self.assertTrue((retired / "retirement.json").is_file())
        self.assertTrue(
            (publish / "approval-history/self-eval-cutover.json").is_file()
        )
        for path, payload in legacy.items():
            self.assertFalse(path.exists())
            archive = (
                publish / "approval-history"
                if path.parent == publish
                else retired
            )
            matches = [
                candidate
                for candidate in archive.rglob("*")
                if candidate.is_file() and candidate.name != "retirement.json"
            ]
            self.assertTrue(
                any(candidate.read_bytes() == payload for candidate in matches),
                path.name,
            )

    def test_crash_after_project_promotion_recovers_prepared_evaluation(self):
        original = authority.write_leaf_at

        def crash_on_generation(directory_fd, name, payload, *, exclusive=True):
            if name == "00000001.json":
                raise OSError("fixture crash after project promotion")
            return original(
                directory_fd, name, payload, exclusive=exclusive
            )

        with engine.Snapshot(self.project) as snapshot:
            with mock.patch.object(
                authority, "write_leaf_at", side_effect=crash_on_generation
            ):
                with self.assertRaisesRegex(OSError, "fixture crash"):
                    self_eval_fixture._start_pending(self.project, snapshot, 1)
        self.assertTrue((self.project / engine.RESULT_PATH).is_file())
        recovered = engine.evaluate(self.project)
        self.assertEqual(recovered["status"], "needs_human")
        self.assertEqual(len(list(self.state.rglob("generations/*.json"))), 1)

    def test_crash_after_generation_repairs_missing_pointer(self):
        original = authority.atomic_leaf_at

        def crash_on_pointer(directory_fd, name, payload):
            if name == "current.json":
                raise OSError("fixture crash before pointer")
            return original(directory_fd, name, payload)

        with engine.Snapshot(self.project) as snapshot:
            with mock.patch.object(
                authority, "atomic_leaf_at", side_effect=crash_on_pointer
            ):
                with self.assertRaisesRegex(OSError, "fixture crash"):
                    self_eval_fixture._start_pending(self.project, snapshot, 1)
        self.assertFalse(any(self.state.rglob("current.json")))
        current = engine.validate_current(self.project)
        self.assertEqual(current["status"], "needs_human")
        self.assertTrue(any(self.state.rglob("current.json")))

    def test_project_path_aba_fails_before_promotion(self):
        moved = self.root / "moved-original"
        with engine.Snapshot(self.project) as snapshot:
            self.project.rename(moved)
            replacement = write_project(
                self.root / "source", name="same-name", video=b"replacement"
            )
            with self.assertRaisesRegex(ValueError, "project root changed"):
                self_eval_fixture._start_pending(self.project, snapshot, 1)
        self.assertFalse((moved / engine.RESULT_PATH).exists())
        self.assertFalse((replacement / engine.RESULT_PATH).exists())

if __name__ == "__main__":
    unittest.main()
