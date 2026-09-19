import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parent))


import upload_attempts
import youtube_credentials

CHANNEL = "UC" + "a" * 22
APPROVAL = "1" * 64
REQUEST = "2" * 64
SESSION = "https://www.googleapis.com/upload/secret-session-canary"

def _prepare_worker(project, caller_key, result):
    try:
        store = upload_attempts.UploadAttemptStore(Path(project))
        value = store.prepare(
            approval_intent_sha256=APPROVAL,
            request_sha256=REQUEST,
            target_channel_id=CHANNEL,
            visibility="unlisted",
            caller_key=caller_key,
        )
        result.put(("ok", value["publish_intent_id"]))
    except Exception as error:
        result.put(("error", type(error).__name__, str(error)))


class UploadAttemptStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name).resolve()
        self.project = root / "project"
        self.project.mkdir()
        self.secret_root = root / "secrets/youtube"
        self.store_patch = mock.patch.object(
            youtube_credentials, "CREDENTIAL_STORE_ROOT", self.secret_root
        )
        self.store_patch.start()
        self.store = upload_attempts.UploadAttemptStore(self.project)

    def tearDown(self):
        self.store_patch.stop()
        self.temporary.cleanup()

    def prepare(self, key="caller-a"):
        return self.store.prepare(
            approval_intent_sha256=APPROVAL,
            request_sha256=REQUEST,
            target_channel_id=CHANNEL,
            visibility="unlisted",
            caller_key=key,
        )

    def session_path(self):
        intent = upload_attempts.publish_intent_id(APPROVAL)
        return self.secret_root / CHANNEL / "sessions" / f"{intent}.json"

    def assert_code(self, code, operation):
        with self.assertRaisesRegex(upload_attempts.AttemptError, f"^{code}$"):
            operation()

    def test_caller_keys_share_one_publish_intent(self):
        first = self.prepare("caller-a")
        second = self.prepare("caller-b")
        self.assertEqual(first["publish_intent_id"], second["publish_intent_id"])
        self.assertEqual(second["caller_keys"], ["caller-a", "caller-b"])
        self.assertEqual(len(list(self.store.root.glob("[0-9a-f]*.json"))), 1)

    def test_concurrent_callers_create_exactly_one_attempt(self):
        context = multiprocessing.get_context("fork")
        result = context.Queue()
        processes = [
            context.Process(
                target=_prepare_worker,
                args=(str(self.project), f"caller-{index}", result),
            )
            for index in range(8)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        outcomes = [result.get(timeout=1) for _ in processes]
        self.assertEqual({outcome[0] for outcome in outcomes}, {"ok"})
        self.assertEqual(len({outcome[1] for outcome in outcomes}), 1)
        self.assertEqual(len(list(self.store.root.glob("[0-9a-f]*.json"))), 1)
        state = self.store.get(APPROVAL)
        self.assertEqual(len(state["caller_keys"]), 8)

    def test_session_post_unknown_is_durable_before_the_remote_seam(self):
        self.prepare()
        state = self.store.before_session_post(APPROVAL)
        durable = json.loads(self.store._path(state["publish_intent_id"]).read_text())
        self.assertEqual(durable["state"], "session_creation_unknown")
        self.assertTrue(durable["session_post_issued"])
        self.assertIsNone(self.store.session(APPROVAL))
        self.assert_code(
            "youtube_upload_reconciliation_required",
            lambda: self.store.before_session_post(APPROVAL),
        )

    def test_crash_after_secret_write_recovers_without_a_second_session_post(self):
        self.prepare()
        self.store.before_session_post(APPROVAL)
        value = self.store.get(APPROVAL)
        upload_attempts._write_session(
            CHANNEL, value["publish_intent_id"], SESSION, REQUEST
        )
        recovered = self.store.recover_recorded_session(APPROVAL)
        self.assertEqual(recovered["state"], "session_created")
        self.assertEqual(self.store.session(APPROVAL), SESSION)
        self.assertNotIn(SESSION, self.store._path(value["publish_intent_id"]).read_text())

    def test_first_put_unknown_keeps_fence_even_when_local_bytes_are_zero(self):
        self.prepare()
        self.store.before_session_post(APPROVAL)
        self.store.record_session(APPROVAL, SESSION)
        unknown = self.store.before_put(APPROVAL, offset=0)
        self.assertEqual(unknown["state"], "remote_outcome_unknown")
        self.assertEqual(unknown["bytes_confirmed"], 0)
        joined = self.prepare("other-key")
        self.assertEqual(joined["state"], "remote_outcome_unknown")
        self.assertTrue(joined["media_put_issued"])
        self.assert_code(
            "youtube_upload_reconciliation_required",
            lambda: self.store.before_put(APPROVAL, offset=0),
        )

    def test_final_put_unknown_does_not_restart_or_create_a_new_intent(self):
        self.prepare()
        self.store.before_session_post(APPROVAL)
        self.store.record_session(APPROVAL, SESSION)
        self.store.before_put(APPROVAL, offset=0)
        self.store.record_progress(APPROVAL, 4)
        unknown = self.store.before_put(APPROVAL, offset=4)
        self.assertEqual(unknown["state"], "remote_outcome_unknown")
        self.store.require_reconciliation(APPROVAL, "completion_response_lost")
        fenced = self.prepare("new-key")
        self.assertEqual(fenced["state"], "reconciliation_required")
        self.assertEqual(fenced["attempt_generation"], 1)

    def test_expired_session_before_media_put_can_restart(self):
        self.prepare()
        self.store.before_session_post(APPROVAL)
        self.store.record_session(APPROVAL, SESSION)
        restarted = self.store.session_expired(APPROVAL)
        self.assertEqual(restarted["state"], "prepared")
        self.assertEqual(restarted["attempt_generation"], 2)
        self.assertFalse(restarted["session_post_issued"])
        self.assertIsNone(self.store.session(APPROVAL))

    def test_expired_session_after_media_put_requires_reconciliation(self):
        self.prepare()
        self.store.before_session_post(APPROVAL)
        self.store.record_session(APPROVAL, SESSION)
        self.store.before_put(APPROVAL, offset=0)
        blocked = self.store.session_expired(APPROVAL)
        self.assertEqual(blocked["state"], "reconciliation_required")
        self.assertTrue(blocked["media_put_issued"])
        self.assert_code(
            "youtube_upload_reconciliation_required",
            lambda: self.store.before_session_post(APPROVAL),
        )

    def test_restart_requires_authenticated_authoritative_absence(self):
        self.prepare()
        self.store.before_session_post(APPROVAL)
        self.store.require_reconciliation(APPROVAL, "lost_location")
        for evidence in (
            {},
            {"authenticated": True, "authoritative_absence": False, "channel_id": CHANNEL, "checked_at": "now"},
            {"authenticated": True, "authoritative_absence": True, "channel_id": "UC" + "b" * 22, "checked_at": "now"},
        ):
            self.assert_code(
                "youtube_upload_reconciliation_required",
                lambda evidence=evidence: self.store.authorize_restart(APPROVAL, evidence),
            )
        restarted = self.store.authorize_restart(
            APPROVAL,
            {
                "authenticated": True,
                "authoritative_absence": True,
                "channel_id": CHANNEL,
                "checked_at": "2026-08-10T00:00:00+00:00",
                "search_window": "attempt-bound",
            },
        )
        self.assertEqual(restarted["state"], "prepared")
        self.assertEqual(restarted["attempt_generation"], 2)

    def test_completion_requires_exact_authenticated_unlisted_readback(self):
        self.prepare()
        self.store.before_session_post(APPROVAL)
        self.store.record_session(APPROVAL, SESSION)
        self.store.before_put(APPROVAL, offset=0)
        self.store.record_video(APPROVAL, video_id="abcdefghijk", channel_id=CHANNEL)
        good = {
            "authenticated": True,
            "video_id": "abcdefghijk",
            "channel_id": CHANNEL,
            "privacy_status": "unlisted",
            "matches_approval": True,
            "thumbnail_matches": True,
        }
        for field, wrong in (
            ("authenticated", False),
            ("video_id", "lmnopqrstuv"),
            ("channel_id", "UC" + "b" * 22),
            ("privacy_status", "private"),
            ("matches_approval", False),
            ("thumbnail_matches", False),
        ):
            bad = dict(good)
            bad[field] = wrong
            self.assert_code(
                "youtube_upload_remote_mismatch",
                lambda bad=bad: self.store.complete(APPROVAL, bad),
            )
        complete = self.store.complete(APPROVAL, good)
        self.assertEqual(complete["state"], "complete")
        self.assertFalse(self.session_path().exists())

    def test_illegal_transitions_fail_closed(self):
        self.prepare()
        self.assert_code(
            "youtube_upload_reconciliation_required",
            lambda: self.store.before_put(APPROVAL, offset=0),
        )
        self.assert_code(
            "youtube_upload_transition_invalid",
            lambda: self.store.record_progress(APPROVAL, 1),
        )
        self.assert_code(
            "youtube_upload_remote_mismatch",
            lambda: self.store.record_video(
                APPROVAL, video_id="abcdefghijk", channel_id=CHANNEL
            ),
        )

    def test_external_session_permissions_and_link_guards(self):
        self.prepare()
        self.store.before_session_post(APPROVAL)
        self.store.record_session(APPROVAL, SESSION)
        path = self.session_path()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

        linked = path.parent / "linked.json"
        os.link(path, linked)
        self.assert_code(
            "youtube_upload_session_unsafe", lambda: self.store.session(APPROVAL)
        )
        linked.unlink()

        path.unlink()
        target = path.parent / "target.json"
        target.write_text("{}")
        target.chmod(0o600)
        path.symlink_to(target.name)
        self.assert_code(
            "youtube_upload_session_unsafe", lambda: self.store.session(APPROVAL)
        )

    def test_secret_canary_never_appears_in_public_state_or_errors(self):
        self.prepare()
        self.store.before_session_post(APPROVAL)
        self.store.record_session(APPROVAL, SESSION)
        state = self.store.get(APPROVAL)
        public = self.store._path(state["publish_intent_id"]).read_text()
        self.assertNotIn("secret-session-canary", public)
        try:
            self.store.before_session_post(APPROVAL)
        except upload_attempts.AttemptError as error:
            self.assertNotIn("secret-session-canary", str(error))
        self.assertNotIn("secret-session-canary", json.dumps(state))


if __name__ == "__main__":
    unittest.main()
