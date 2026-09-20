from __future__ import annotations

import multiprocessing
import io
import json
import os
import stat
import urllib.error
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_narration_with_srt as generator
from providers import (
    AlignmentUnit,
    ProviderConfirmedFailure,
    ProviderError,
    ProviderSubmissionUnknown,
    SynthesisResult,
)
from spend_journal import BudgetExceeded, SpendJournal, SubmissionBlocked
from providers.elevenlabs import ElevenLabsProvider


class SimulatedCrash(BaseException):
    pass


class FakeProvider:
    name = "elevenlabs"

    def __init__(self, marker: Path | None = None):
        self.calls = 0
        self.history = {}
        self.failure = None
        self.marker = marker

    def credits_for(self, text, model):
        return 5

    def synthesize(self, text, **_kwargs):
        self.calls += 1
        if self.marker is not None:
            with self.marker.open("a", encoding="utf-8") as handle:
                handle.write("submitted\n")
        if self.failure is not None:
            failure = self.failure
            self.failure = None
            raise failure
        request_id = f"req-{self.calls}"
        self.history[request_id] = {
            "request_id": request_id,
            "voice_id": _kwargs["voice"],
            "model_id": _kwargs["model"],
            "text": text,
            "character_cost": 5,
        }
        units = [
            AlignmentUnit(character, index * 0.1, (index + 1) * 0.1)
            for index, character in enumerate(text)
        ]
        return SynthesisResult(
            audio=b"fake-mp3-bytes",
            units=units,
            granularity="char",
            metadata={
                "request_id": request_id,
                "history_item_id": f"history-{self.calls}",
                "character_cost": 5,
            },
        )

    def history_costs(self, request_ids):
        return {
            request_id: self.history[request_id]["character_cost"]
            for request_id in request_ids
            if request_id in self.history
        }

    def history_records(self, request_ids):
        return {
            request_id: self.history[request_id]
            for request_id in request_ids
            if request_id in self.history
        }


def arguments(root: Path, *extra: str):
    return generator.build_parser().parse_args(
        [
            "--text",
            "測試。",
            "--out-base",
            str(root / "take"),
            "--voice",
            "synthetic-voice",
            "--max-credits",
            "20",
            "--spend-journal",
            str(root / "spend.sqlite3"),
            *extra,
        ]
    )


def parent_arguments(root: Path, text: str, output_name: str):
    return generator.build_parser().parse_args(
        [
            "--text",
            text,
            "--out-base",
            str(root / output_name),
            "--voice",
            "synthetic-voice",
            "--spend-journal",
            str(root / "shared-spend.sqlite3"),
            "--force-budget",
        ]
    )


def concurrent_reservation(path: str, base_key: str, queue):
    journal = SpendJournal(Path(path))
    try:
        with journal.locked():
            attempt = journal.reserve(
                base_key=base_key,
                provider="test",
                model="test",
                request_payload_sha256=base_key,
                estimated_credits=4,
                output_base=f"/tmp/{base_key}",
                budget_limit=5,
                retake=False,
            )
            journal.mark_submitting(attempt.attempt_id)
            time.sleep(0.1)
            journal.mark_submitted(
                attempt.attempt_id,
                provider_request_id=f"req-{base_key}",
                provider_status="ok",
            )
            journal.mark_succeeded(attempt.attempt_id, actual_credits=4)
        queue.put("succeeded")
    except BudgetExceeded:
        queue.put("budget_exceeded")
    except BaseException as error:
        queue.put(f"error:{type(error).__name__}:{error}")


def crash_execution(root: str, stage: str):
    directory = Path(root)
    provider = FakeProvider(directory / "provider-calls.log")

    def crash(current, _attempt):
        if current == stage:
            os._exit(77)

    generator.execute(
        arguments(directory),
        provider=provider,
        stage_hook=crash,
    )


def bounded_parent_generation(root: str, text: str, output_name: str, queue):
    directory = Path(root)
    os.environ["VIDEO_STUDIO_TTS_MAX_CREDITS"] = "5"
    provider = FakeProvider(directory / "bounded-provider-calls.log")
    try:
        generator.execute(
            parent_arguments(directory, text, output_name),
            provider=provider,
        )
        queue.put("succeeded")
    except BudgetExceeded:
        queue.put("budget_exceeded")
    except BaseException as error:
        queue.put(f"error:{type(error).__name__}:{error}")


class SpendJournalTest(unittest.TestCase):
    def test_force_budget_requires_cap_and_uses_lower_operator_limit(self):
        with mock.patch.dict(
            os.environ,
            {"VIDEO_STUDIO_TTS_MAX_CREDITS": "5"},
        ):
            self.assertEqual(generator._read_limit(10, True), 5)
            self.assertEqual(generator._read_limit(None, True), 5)
        with mock.patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "not an unbounded-spend"):
                generator._read_limit(None, True)
        with mock.patch.dict(
            os.environ,
            {"VIDEO_STUDIO_TTS_MAX_CREDITS": "invalid"},
        ):
            with self.assertRaisesRegex(ValueError, "positive integer"):
                generator._read_limit(10, False)

    def test_parent_force_budget_argv_concurrent_sections_share_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("fork")
            queue = context.Queue()
            processes = [
                context.Process(
                    target=bounded_parent_generation,
                    args=(directory, text, output_name, queue),
                )
                for text, output_name in (
                    ("第一段。", "section-001"),
                    ("第二段。", "section-002"),
                )
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            outcomes = sorted(queue.get(timeout=2) for _ in processes)
            self.assertEqual(outcomes, ["budget_exceeded", "succeeded"])
            calls = (
                Path(directory) / "bounded-provider-calls.log"
            ).read_text().splitlines()
            self.assertEqual(calls, ["submitted"])
            with SpendJournal(
                Path(directory) / "shared-spend.sqlite3"
            ).locked() as journal:
                self.assertEqual(journal.used_credits(), 5)
                self.assertEqual(len(journal.dump()["attempts"]), 1)

    def test_real_process_crash_before_submit_is_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("fork")
            process = context.Process(
                target=crash_execution,
                args=(directory, "reserved"),
            )
            process.start()
            process.join(5)
            self.assertEqual(process.exitcode, 77)
            root = Path(directory)
            self.assertFalse((root / "provider-calls.log").exists())
            journal = SpendJournal(root / "spend.sqlite3")
            with journal.locked():
                self.assertEqual(journal.dump()["attempts"][0]["status"], "prepared")
            self.assertEqual(
                stat.S_IMODE((root / "spend.sqlite3").stat().st_mode),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE((root / "spend.sqlite3.lock").stat().st_mode),
                0o600,
            )
            provider = FakeProvider(root / "provider-calls.log")
            generator.execute(arguments(root), provider=provider)
            self.assertEqual(
                (root / "provider-calls.log").read_text().splitlines(),
                ["submitted"],
            )

    def test_real_process_crash_after_provider_acceptance_blocks_resend(self):
        with tempfile.TemporaryDirectory() as directory:
            context = multiprocessing.get_context("fork")
            process = context.Process(
                target=crash_execution,
                args=(directory, "provider_returned"),
            )
            process.start()
            process.join(5)
            self.assertEqual(process.exitcode, 77)
            root = Path(directory)
            self.assertEqual(
                (root / "provider-calls.log").read_text().splitlines(),
                ["submitted"],
            )
            provider = FakeProvider(root / "provider-calls.log")
            with self.assertRaisesRegex(SubmissionBlocked, "submission_unknown"):
                generator.execute(
                    arguments(root, "--retake", "--force-budget"),
                    provider=provider,
                )
            self.assertEqual(
                (root / "provider-calls.log").read_text().splitlines(),
                ["submitted"],
            )

    def test_provider_classifies_http_rejection_as_proven_precharge_failure(self):
        response = urllib.error.HTTPError(
            "https://provider.invalid",
            400,
            "bad request",
            {},
            io.BytesIO(b"invalid voice"),
        )
        with mock.patch(
            "providers.elevenlabs.load_key",
            return_value="fake",
        ), mock.patch(
            "providers.elevenlabs.urllib.request.urlopen",
            side_effect=response,
        ):
            with self.assertRaises(ProviderConfirmedFailure) as caught:
                ElevenLabsProvider().synthesize(
                    "test",
                    voice="voice",
                    model="eleven_v3",
                    stability=0.3,
                    speed=1.0,
                )
        self.assertEqual(caught.exception.proof, "provider_http_rejection:400")

    def test_provider_classifies_timeout_as_submission_unknown(self):
        with mock.patch(
            "providers.elevenlabs.load_key",
            return_value="fake",
        ), mock.patch(
            "providers.elevenlabs.urllib.request.urlopen",
            side_effect=TimeoutError("read timed out"),
        ):
            with self.assertRaises(ProviderSubmissionUnknown):
                ElevenLabsProvider().synthesize(
                    "test",
                    voice="voice",
                    model="eleven_v3",
                    stability=0.3,
                    speed=1.0,
                )

    def test_timeout_like_4xx_does_not_release_reservation(self):
        for status in (408, 409):
            response = urllib.error.HTTPError(
                "https://provider.invalid",
                status,
                "ambiguous",
                {},
                io.BytesIO(b"ambiguous"),
            )
            with self.subTest(status=status), mock.patch(
                "providers.elevenlabs.load_key",
                return_value="fake",
            ), mock.patch(
                "providers.elevenlabs.urllib.request.urlopen",
                side_effect=response,
            ):
                with self.assertRaises(ProviderSubmissionUnknown):
                    ElevenLabsProvider().synthesize(
                        "test",
                        voice="voice",
                        model="eleven_v3",
                        stability=0.3,
                        speed=1.0,
                    )

    def test_crash_before_submit_reuses_reservation_without_duplicate_spend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider()

            def crash(stage, _attempt):
                if stage == "reserved":
                    raise SimulatedCrash()

            with self.assertRaises(SimulatedCrash):
                generator.execute(
                    arguments(root),
                    provider=provider,
                    stage_hook=crash,
                )
            self.assertEqual(provider.calls, 0)
            journal = SpendJournal(root / "spend.sqlite3")
            with journal.locked():
                self.assertEqual(journal.dump()["attempts"][0]["status"], "prepared")

            message = generator.execute(arguments(root), provider=provider)
            self.assertIn("OK wrote", message)
            self.assertEqual(provider.calls, 1)
            with journal.locked():
                self.assertEqual(journal.used_credits(), 5)
                self.assertEqual(len(journal.dump()["attempts"]), 1)

    def test_crash_after_provider_acceptance_becomes_unknown_and_never_resends(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider()

            def crash(stage, _attempt):
                if stage == "provider_returned":
                    raise SimulatedCrash()

            with self.assertRaises(SimulatedCrash):
                generator.execute(
                    arguments(root),
                    provider=provider,
                    stage_hook=crash,
                )
            self.assertEqual(provider.calls, 1)

            retry = arguments(root, "--retake", "--force-budget")
            with self.assertRaisesRegex(SubmissionBlocked, "submission_unknown"):
                generator.execute(retry, provider=provider)
            self.assertEqual(provider.calls, 1)
            journal = SpendJournal(root / "spend.sqlite3")
            with journal.locked():
                self.assertEqual(journal.latest(
                    generator.take_hash(
                        "synthetic-voice",
                        generator.MODEL,
                        generator.DEFAULT_STABILITY,
                        generator.SPEED,
                        "測試。",
                        provider="elevenlabs",
                        request_context={
                            "stream": False,
                            "previous_request_ids": [],
                            "next_request_ids": [],
                            "previous_text": None,
                            "next_text": None,
                            "seed": None,
                        },
                    )
                ).status, "submission_unknown")

    def test_receipt_written_before_crash_recovers_without_resubmission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider()

            def crash(stage, _attempt):
                if stage == "receipt_written":
                    raise SimulatedCrash()

            with self.assertRaises(SimulatedCrash):
                generator.execute(
                    arguments(root),
                    provider=provider,
                    stage_hook=crash,
                )
            self.assertEqual(provider.calls, 1)
            message = generator.execute(arguments(root), provider=provider)
            self.assertIn("SKIP unchanged", message)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(
                SpendJournal(root / "spend.sqlite3").latest(
                    generator._load_take(root / "take.take.json")["hash"]
                ).status,
                "succeeded",
            )

    def test_unknown_requires_provider_history_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider()
            provider.history["provider-known-id"] = {
                "request_id": "provider-known-id",
                "voice_id": "synthetic-voice",
                "model_id": generator.MODEL,
                "text": "測試。",
                "character_cost": 5,
            }
            provider.failure = ProviderSubmissionUnknown(
                "timeout after upload",
                provider_request_id=None,
            )
            with self.assertRaises(ProviderSubmissionUnknown):
                generator.execute(arguments(root), provider=provider)
            self.assertEqual(provider.calls, 1)

            message = generator.execute(
                arguments(
                    root,
                    "--reconcile-request-id",
                    "provider-known-id",
                ),
                provider=provider,
            )
            self.assertIn("RECONCILED", message)
            self.assertEqual(provider.calls, 1)
            with self.assertRaisesRegex(SubmissionBlocked, "--retake"):
                generator.execute(arguments(root), provider=provider)
            self.assertEqual(provider.calls, 1)

            retake = arguments(root, "--retake", "--max-credits", "10")
            self.assertIn("OK wrote", generator.execute(retake, provider=provider))
            self.assertEqual(provider.calls, 2)
            with SpendJournal(root / "spend.sqlite3").locked() as journal:
                self.assertEqual(journal.used_credits(), 10)

    def test_unproven_request_id_cannot_release_or_reconcile_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider()
            provider.failure = ProviderSubmissionUnknown("ambiguous timeout")
            with self.assertRaises(ProviderSubmissionUnknown):
                generator.execute(arguments(root), provider=provider)
            with self.assertRaisesRegex(SubmissionBlocked, "did not prove"):
                generator.execute(
                    arguments(root, "--reconcile-request-id", "invented-id"),
                    provider=provider,
                )
            with self.assertRaises(SubmissionBlocked):
                generator.execute(
                    arguments(root, "--retake", "--force-budget"),
                    provider=provider,
                )
            self.assertEqual(provider.calls, 1)

    def test_unrelated_provider_history_id_cannot_reconcile_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider()
            provider.failure = ProviderSubmissionUnknown("ambiguous timeout")
            provider.history["unrelated-id"] = {
                "request_id": "unrelated-id",
                "voice_id": "another-voice",
                "model_id": generator.MODEL,
                "text": "different text",
                "character_cost": 5,
            }
            with self.assertRaises(ProviderSubmissionUnknown):
                generator.execute(arguments(root), provider=provider)
            with self.assertRaisesRegex(
                SubmissionBlocked,
                "does not match text, voice, and model",
            ):
                generator.execute(
                    arguments(
                        root,
                        "--reconcile-request-id",
                        "unrelated-id",
                    ),
                    provider=provider,
                )
            self.assertEqual(provider.calls, 1)

    def test_confirmed_precharge_failure_releases_only_with_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider()
            provider.failure = ProviderConfirmedFailure(
                "HTTP 400",
                proof="provider_http_rejection:400",
            )
            with self.assertRaises(ProviderConfirmedFailure):
                generator.execute(arguments(root), provider=provider)
            journal = SpendJournal(root / "spend.sqlite3")
            with journal.locked():
                self.assertEqual(journal.used_credits(), 0)
                latest = journal.dump()["attempts"][-1]
                self.assertEqual(latest["status"], "confirmed_failed")
                self.assertEqual(
                    latest["failure_proof"],
                    "provider_http_rejection:400",
                )

            self.assertIn(
                "OK wrote",
                generator.execute(
                    arguments(root, "--retake"),
                    provider=provider,
                ),
            )
            self.assertEqual(provider.calls, 2)

    def test_cache_consumes_zero_and_explicit_retakes_cost_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider()
            args = arguments(root, "--max-credits", "10")
            generator.execute(args, provider=provider)
            self.assertEqual(provider.calls, 1)
            self.assertIn(
                "SKIP unchanged",
                generator.execute(args, provider=provider),
            )
            self.assertEqual(provider.calls, 1)

            generator.execute(
                arguments(root, "--retake", "--max-credits", "10"),
                provider=provider,
            )
            self.assertEqual(provider.calls, 2)
            with self.assertRaises(BudgetExceeded):
                generator.execute(
                    arguments(root, "--retake", "--max-credits", "10"),
                    provider=provider,
                )
            self.assertEqual(provider.calls, 2)
            journal = SpendJournal(root / "spend.sqlite3")
            with journal.locked():
                self.assertEqual(journal.used_credits(), 10)
                self.assertEqual(len(journal.dump()["attempts"]), 2)

    def test_provider_affecting_settings_change_idempotency_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider()
            generator.execute(
                arguments(root, "--seed", "1", "--max-credits", "10"),
                provider=provider,
            )
            first_hash = json.loads(
                (root / "take.take.json").read_text()
            )["hash"]
            generator.execute(
                arguments(root, "--seed", "2", "--max-credits", "10"),
                provider=provider,
            )
            second_hash = json.loads(
                (root / "take.take.json").read_text()
            )["hash"]
            self.assertNotEqual(first_hash, second_hash)
            self.assertEqual(provider.calls, 2)
            with SpendJournal(root / "spend.sqlite3").locked() as journal:
                self.assertEqual(journal.used_credits(), 10)
                self.assertEqual(len(journal.dump()["attempts"]), 2)

    def test_legacy_completed_cache_is_imported_without_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeProvider()
            legacy_hash = generator.legacy_take_hash(
                "synthetic-voice",
                generator.MODEL,
                generator.DEFAULT_STABILITY,
                generator.SPEED,
                "測試。",
            )
            (root / "take.mp3").write_bytes(b"legacy-audio")
            (root / "take.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n測試。\n",
                encoding="utf-8",
            )
            (root / "take.take.json").write_text(
                json.dumps(
                    {
                        "hash": legacy_hash,
                        "voice": "synthetic-voice",
                        "model": generator.MODEL,
                        "request_id": "legacy-request-id",
                        "credits": 5,
                    }
                ),
                encoding="utf-8",
            )
            message = generator.execute(arguments(root), provider=provider)
            self.assertIn("SKIP unchanged", message)
            self.assertEqual(provider.calls, 0)
            receipt = json.loads((root / "take.take.json").read_text())
            self.assertEqual(receipt["spend_status"], "succeeded")
            self.assertNotEqual(receipt["hash"], legacy_hash)

    def test_concurrent_reservations_cannot_cross_budget_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            journal_path = str(Path(directory) / "spend.sqlite3")
            context = multiprocessing.get_context("fork")
            queue = context.Queue()
            processes = [
                context.Process(
                    target=concurrent_reservation,
                    args=(journal_path, key, queue),
                )
                for key in ("a" * 64, "b" * 64)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            outcomes = sorted(queue.get(timeout=2) for _ in processes)
            self.assertEqual(outcomes, ["budget_exceeded", "succeeded"])
            journal = SpendJournal(Path(journal_path))
            with journal.locked():
                self.assertEqual(journal.used_credits(), 4)


if __name__ == "__main__":
    unittest.main()
