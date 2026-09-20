import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from runtime_test_fixture import RuntimePolicyCase
import urllib.parse

from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_status
import approval_attestation
import youtube_upload
from test_agent_status import make_ready_project


RUNTIME_A = "sha256:" + "a" * 64
BINARY_A = "sha256:" + "b" * 64
RUNTIME_B = "sha256:" + "c" * 64
BINARY_B = "sha256:" + "d" * 64
CHANNEL = "UCaaaaaaaaaaaaaaaaaaaaaa"

class FakeYouTube:
    UPLOADS = "UU" + CHANNEL[2:]

    def __init__(self, interrupt_once=False, remote_videos=None):
        self.calls = []
        self.interrupt_once = interrupt_once
        self.interrupted = False
        self.remote_videos = list(remote_videos or [])

    def fixture_item(self, video_id="abcdefghijk"):
        return {
            "id": video_id,
            "snippet": {
                "channelId": CHANNEL,
                "title": "Fixture title",
                "description": "Fixture description.\n\nFixture source statement.\n\n#Fixture",
                "categoryId": "22",
                "tags": ["Fixture"],
                "thumbnails": {"default": {"url": "https://example.test/thumbnail"}},
            },
            "status": {
                "privacyStatus": "unlisted",
                "selfDeclaredMadeForKids": False,
            },
        }

    def __call__(self, method, url, headers=None, body=None, timeout=120):
        headers = headers or {}
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": {
                    key: value
                    for key, value in headers.items()
                    if key.lower() != "authorization"
                },
                "bytes": len(body or b""),
            }
        )
        if method == "POST" and "youtube/v3/videos" in url:
            return (
                200,
                {"Location": "https://www.googleapis.com/upload/secret-session"},
                b"",
            )
        if method == "PUT" and headers.get("Content-Range") == "bytes */5":
            return 308, {"Range": "bytes=0-1"}, b""
        if method == "PUT":
            if self.interrupt_once and not self.interrupted:
                self.interrupted = True
                raise youtube_upload.UploadError("youtube_upload_interrupted")
            return (
                200,
                {},
                json.dumps(
                    {
                        "id": "abcdefghijk",
                        "snippet": {"channelId": CHANNEL},
                    }
                ).encode(),
            )
        if method == "POST" and "thumbnails/set" in url:
            return 200, {}, b"{}"
        if method == "GET" and "youtube/v3/channels" in url:
            return (
                200,
                {},
                json.dumps(
                    {
                        "items": [
                            {
                                "id": CHANNEL,
                                "contentDetails": {
                                    "relatedPlaylists": {"uploads": self.UPLOADS}
                                },
                            }
                        ]
                    }
                ).encode(),
            )
        if method == "GET" and "youtube/v3/playlistItems" in url:
            return (
                200,
                {},
                json.dumps(
                    {
                        "items": [
                            {"contentDetails": {"videoId": item["id"]}}
                            for item in self.remote_videos
                        ]
                    }
                ).encode(),
            )
        if method == "GET" and "youtube/v3/videos" in url:
            parsed = urllib.parse.urlparse(url)
            requested = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            wanted = {part for part in requested.split(",") if part}
            catalog = self.remote_videos or [self.fixture_item()]
            items = [item for item in catalog if not wanted or item["id"] in wanted]
            return (200, {}, json.dumps({"items": items}).encode())
        raise AssertionError((method, url, headers))



class YouTubeUploadTest(RuntimePolicyCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.workspace = root / "studio"
        self.workspace.mkdir()
        self.project = make_ready_project(self.workspace, "upload-project")
        self.credential_reference = f"youtube:{CHANNEL}"

        status, _artifacts = agent_status.build(self.project, self.workspace)
        contract = json.loads((self.project / "project-contract.json").read_text())
        self.attestation_ref = "attestation:upload-test"
        self.nonce = "upload-test-nonce"
        self.generation = 1
        warnings = status.get("warnings") or []
        refs = status["approval_intent_refs"]
        intent = agent_status.approval_intent(
            self.project.name,
            status["canonical_artifacts"],
            contract,
            warnings,
            None,
            self.attestation_ref,
            self.nonce,
            self.generation,
            render_self_eval_ref=refs["render_self_eval"],
            visual_qa_review_ref=refs["visual_qa_review"],
        )
        self.assertIsNotNone(intent)
        self.approval_intent_sha256 = agent_status.approval_intent_sha256(intent)
        final = status["canonical_artifacts"]["final_video"]
        approval_dir = self.project / "publish"
        approval_dir.mkdir()
        (approval_dir / "publish-approval.json").write_text(
            json.dumps(
                {
                    "schema": agent_status.SCHEMA_PUBLISH_APPROVAL,
                    "project": self.project.name,
                    "project_id": self.project.name,
                    "approved_at": "2026-07-30T00:00:00+00:00",
                    "approval_intent_sha256": self.approval_intent_sha256,
                    "warnings_acknowledged": warnings,
                    "override_reason": None,
                    "final_path": final["path"],
                    "final_sha256": intent["final_sha256"],
                    "final_bytes": intent["final_bytes"],
                    "metadata_sha256": intent["metadata_sha256"],
                    "cover_sha256": intent["cover_sha256"],
                    "channel_id": intent["channel_id"],
                    "visibility": "unlisted",
                    "runtime_contract": intent["runtime_contract"],
                    "render_self_eval": intent["render_self_eval"],
                    "visual_qa_review": intent["visual_qa_review"],
                    "generation": self.generation,
                    "attestation_ref": self.attestation_ref,
                    "nonce": self.nonce,
                    "video_id": None,
                    "uploaded_at": None,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_upload(
        self,
        fake,
        key="upload-once",
        runtime_id=RUNTIME_A,
        runtime_binary_sha256=BINARY_A,
        credential_error=None,
    ):
        resolver = (
            mock.Mock(side_effect=youtube_upload.UploadError(credential_error))
            if credential_error
            else mock.Mock(return_value="secret-upload-token")
        )
        with (
            mock.patch.object(youtube_upload, "http_request", side_effect=fake),
            mock.patch.object(youtube_upload, "resolve_token", resolver),
            mock.patch.object(
                approval_attestation,
                "is_consumed",
                return_value=True,
            ),
        ):
            return youtube_upload.run(
                self.project,
                self.workspace,
                self.credential_reference,
                key,
                runtime_id,
                runtime_binary_sha256,
            )

    def run_reconcile(self, fake, key="reconcile-once", override=None):
        with (
            mock.patch.object(youtube_upload, "http_request", side_effect=fake),
            mock.patch.object(
                youtube_upload, "resolve_token", mock.Mock(return_value="secret-upload-token")
            ),
            mock.patch.object(approval_attestation, "is_consumed", return_value=True),
        ):
            return youtube_upload.reconcile(
                self.project,
                self.workspace,
                self.credential_reference,
                key,
                RUNTIME_A,
                BINARY_A,
                override,
            )

    def fence_interrupted_put(self):
        with self.assertRaisesRegex(
            youtube_upload.UploadError, "youtube_upload_interrupted"
        ):
            self.run_upload(FakeYouTube(interrupt_once=True), "ambiguous-put")

    def write_override(self, ref, intent_id, generation):
        root = Path(self.temporary.name) / "protected-attestations"
        root.mkdir(mode=0o700, exist_ok=True)
        root.chmod(0o700)
        path = root / f"{ref.split(':', 1)[1]}.json"
        path.write_text(
            json.dumps(
                {
                    "schema": approval_attestation.RECONCILE_SCHEMA,
                    "attestation_ref": ref,
                    "project_id": self.project.name,
                    "publish_intent_id": intent_id,
                    "attempt_generation": generation,
                    "action": approval_attestation.RECONCILE_ACTION,
                    "issued_at": "2026-08-13T00:00:00+00:00",
                    "consumed_at": None,
                }
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return root


    def test_preflight_failures_never_call_youtube(self):
        cases = ("missing_approval", "wrong_sha", "newer_render")
        for case in cases:
            with self.subTest(case=case):
                fake = FakeYouTube()
                approval = self.project / "publish/publish-approval.json"
                original = approval.read_bytes()
                newer = self.project / "output/newer-final-v2.mp4"
                if case == "missing_approval":
                    approval.unlink()
                elif case == "wrong_sha":
                    value = json.loads(approval.read_text())
                    value["final_sha256"] = "0" * 64
                    approval.write_text(json.dumps(value), encoding="utf-8")
                else:
                    newer.write_bytes(b"newer")
                with self.assertRaises(youtube_upload.UploadError):
                    self.run_upload(fake, f"blocked-{case}")
                self.assertEqual(fake.calls, [])
                if newer.exists():
                    newer.unlink()
                approval.write_bytes(original)

    def test_credential_failure_is_rejected_before_api(self):
        fake = FakeYouTube()
        with self.assertRaisesRegex(
            youtube_upload.UploadError, "youtube_upload_auth_required"
        ):
            self.run_upload(
                fake,
                credential_error="youtube_upload_auth_required",
            )
        self.assertEqual(fake.calls, [])

    def test_duplicate_key_reuses_completed_receipt_without_second_upload(self):
        fake = FakeYouTube()
        first = self.run_upload(fake)
        call_count = len(fake.calls)
        second = self.run_upload(fake)

        self.assertEqual(first["video_id"], "abcdefghijk")
        self.assertEqual(second, first)
        self.assertEqual(len(fake.calls), call_count)
        receipt = self.project / ".hvp/youtube-uploads/upload-once.json"
        receipt_text = receipt.read_text()
        self.assertNotIn("secret-upload-token", receipt_text)
        self.assertNotIn("secret-session", receipt_text)
        approval = json.loads(
            (self.project / "publish/publish-approval.json").read_text()
        )
        self.assertEqual(approval["video_id"], "abcdefghijk")
        self.assertEqual(approval["visibility"], "unlisted")

    def test_different_caller_key_reuses_one_completed_publish_intent(self):
        first_transport = FakeYouTube()
        first = self.run_upload(first_transport, "caller-a")
        second_transport = FakeYouTube()
        second = self.run_upload(second_transport, "caller-b")

        self.assertEqual(second["video_id"], first["video_id"])
        self.assertEqual(second_transport.calls, [])
        attempts = list(
            (self.project / ".hvp/youtube-upload-attempts").glob("[0-9a-f]*.json")
        )
        self.assertEqual(len(attempts), 1)
        state = json.loads(attempts[0].read_text())
        self.assertEqual(state["caller_keys"], ["caller-a", "caller-b"])

    def test_publish_intent_cannot_be_replayed_by_another_runtime(self):
        self.run_upload(FakeYouTube(), "runtime-a", RUNTIME_A, BINARY_A)
        fake = FakeYouTube()
        with self.assertRaisesRegex(
            youtube_upload.UploadError, "youtube_upload_intent_conflict"
        ):
            self.run_upload(fake, "runtime-b", RUNTIME_B, BINARY_B)
        self.assertEqual(fake.calls, [])

    def test_interrupted_put_requires_reconciliation_and_never_restarts(self):
        first_transport = FakeYouTube(interrupt_once=True)
        with self.assertRaisesRegex(
            youtube_upload.UploadError, "youtube_upload_interrupted"
        ):
            self.run_upload(first_transport, "ambiguous-put")

        second_transport = FakeYouTube()
        with self.assertRaisesRegex(
            youtube_upload.UploadError, "youtube_upload_reconciliation_required"
        ):
            self.run_upload(second_transport, "another-caller")
        self.assertEqual(second_transport.calls, [])
        attempts = list(
            (self.project / ".hvp/youtube-upload-attempts").glob("[0-9a-f]*.json")
        )
        state = json.loads(attempts[0].read_text())
        self.assertTrue(state["media_put_issued"])
        self.assertEqual(state["state"], "reconciliation_required")

    def test_receipts_and_result_contain_no_credentials(self):
        fake = FakeYouTube()
        result = self.run_upload(fake, "no-secrets")
        receipt = self.project / ".hvp/youtube-uploads/no-secrets.json"
        attempt = next(
            (self.project / ".hvp/youtube-upload-attempts").glob("[0-9a-f]*.json")
        )
        combined = json.dumps(result) + receipt.read_text() + attempt.read_text()

        self.assertNotIn("secret-upload-token", combined)
        self.assertNotIn("secret-session", combined)
        self.assertEqual(
            json.loads(receipt.read_text())["final_sha256"],
            hashlib.sha256(b"video").hexdigest(),
        )

    def test_thumbnail_only_updates_the_uploaded_video_once(self):
        approval_path = self.project / "publish/publish-approval.json"
        approval = json.loads(approval_path.read_text())
        approval.update(
            {
                "video_id": "abcdefghijk",
                "uploaded_at": "2026-08-05T00:00:00+00:00",
                "visibility": "unlisted",
            }
        )
        approval_path.write_text(json.dumps(approval), encoding="utf-8")
        fake = FakeYouTube()

        with (
            mock.patch.object(youtube_upload, "http_request", side_effect=fake),
            mock.patch.object(
                youtube_upload, "resolve_token", return_value="secret-upload-token"
            ),
            mock.patch.object(
                approval_attestation,
                "is_consumed",
                return_value=True,
            ),
        ):
            first = youtube_upload.replace_thumbnail(
                self.project,
                self.workspace,
                self.credential_reference,
                "thumbnail-once",
                "harvey",
                RUNTIME_A,
                BINARY_A,
            )
            second = youtube_upload.replace_thumbnail(
                self.project,
                self.workspace,
                self.credential_reference,
                "thumbnail-once",
                "harvey",
                RUNTIME_A,
                BINARY_A,
            )

        self.assertEqual(first, second)
        self.assertEqual(first["schema"], "haru.youtube_thumbnail.v1")
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("thumbnails/set", fake.calls[0]["url"])
        approval = json.loads(approval_path.read_text())
        self.assertEqual(approval["thumbnail_sha256"], first["cover_sha256"])
        self.assertEqual(approval["thumbnail_updated_by"], "harvey")
        receipt = self.project / ".hvp/youtube-thumbnails/thumbnail-once.json"
        self.assertTrue(receipt.is_file())
        self.assertNotIn("secret-upload-token", receipt.read_text())


    def test_interrupted_put_reconciles_a_matching_remote_video(self):
        self.fence_interrupted_put()
        fake = FakeYouTube(remote_videos=[FakeYouTube().fixture_item()])
        result = self.run_reconcile(fake)
        self.assertEqual(result["code"], "youtube_upload_reconciled")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["video_id"], "abcdefghijk")
        self.assertFalse(any("youtube/v3/videos?" in call["url"] and call["method"] == "POST" for call in fake.calls))
        self.assertTrue(any("playlistItems" in call["url"] for call in fake.calls))
        self.assertTrue(any("thumbnails/set" in call["url"] for call in fake.calls))
        attempt = json.loads(
            next((self.project / ".hvp/youtube-upload-attempts").glob("[0-9a-f]*.json")).read_text()
        )
        self.assertEqual(attempt["state"], "complete")
        self.assertEqual(attempt["video_id"], "abcdefghijk")
        approval = json.loads((self.project / "publish/publish-approval.json").read_text())
        self.assertEqual(approval["video_id"], "abcdefghijk")

    def test_bytes_confirmed_zero_is_not_absence_after_media_put(self):
        self.fence_interrupted_put()
        attempt = json.loads(
            next((self.project / ".hvp/youtube-upload-attempts").glob("[0-9a-f]*.json")).read_text()
        )
        self.assertEqual(attempt["bytes_confirmed"], 0)
        self.assertTrue(attempt["media_put_issued"])
        fake = FakeYouTube(remote_videos=[])
        with self.assertRaisesRegex(
            youtube_upload.UploadError, "youtube_upload_absence_unproved"
        ):
            self.run_reconcile(fake)
        self.assertTrue(any("playlistItems" in call["url"] for call in fake.calls))
        self.assertFalse(any(call["method"] == "POST" for call in fake.calls))
        state = json.loads(
            next((self.project / ".hvp/youtube-upload-attempts").glob("[0-9a-f]*.json")).read_text()
        )
        self.assertEqual(state["state"], "reconciliation_required")
        self.assertEqual(state["attempt_generation"], 1)

    def test_human_override_restarts_only_the_same_attempt(self):
        self.fence_interrupted_put()
        attempt_path = next((self.project / ".hvp/youtube-upload-attempts").glob("[0-9a-f]*.json"))
        attempt = json.loads(attempt_path.read_text())
        ref = "attestation:reconcile-restart"
        root = self.write_override(ref, attempt["publish_intent_id"], attempt["attempt_generation"])
        fake = FakeYouTube(remote_videos=[])
        with mock.patch.dict(
            __import__("os").environ,
            {approval_attestation.ROOT_ENV: str(root)},
        ):
            result = self.run_reconcile(fake, override=ref)
        self.assertEqual(result["code"], "youtube_upload_restart_authorized")
        self.assertEqual(result["status"], "prepared")
        self.assertTrue(result["override_consumed"])
        self.assertEqual(result["attempt_generation"], 2)
        restarted = json.loads(attempt_path.read_text())
        self.assertEqual(restarted["state"], "prepared")
        self.assertFalse(restarted["media_put_issued"])
        self.assertEqual(restarted["attempt_generation"], 2)
        leaf = json.loads((root / "reconcile-restart.json").read_text())
        self.assertIsNotNone(leaf["consumed_at"])
        with self.assertRaises(ValueError), mock.patch.dict(
            __import__("os").environ,
            {approval_attestation.ROOT_ENV: str(root)},
        ):
            approval_attestation.consume_reconcile(
                ref,
                self.project.name,
                attempt["publish_intent_id"],
                restarted["attempt_generation"],
            )
        self.fence_interrupted_put()
        with self.assertRaisesRegex(
            youtube_upload.UploadError, "youtube_upload_override_invalid"
        ), mock.patch.dict(
            __import__("os").environ,
            {approval_attestation.ROOT_ENV: str(root)},
        ):
            self.run_reconcile(FakeYouTube(remote_videos=[]), key="again", override=ref)


    def test_two_matching_remote_videos_fail_closed(self):
        self.fence_interrupted_put()
        fake = FakeYouTube(
            remote_videos=[
                FakeYouTube().fixture_item("abcdefghijk"),
                FakeYouTube().fixture_item("lmnopqrstuv"),
            ]
        )
        with self.assertRaisesRegex(
            youtube_upload.UploadError, "youtube_upload_remote_ambiguous"
        ):
            self.run_reconcile(fake)
        state = json.loads(
            next((self.project / ".hvp/youtube-upload-attempts").glob("[0-9a-f]*.json")).read_text()
        )
        self.assertEqual(state["state"], "reconciliation_required")
        self.assertIsNone(state["video_id"])

    def test_session_unknown_without_bytes_can_restart_from_empty_channel(self):
        store = __import__("upload_attempts").UploadAttemptStore(self.project)
        store.prepare(
            approval_intent_sha256=self.approval_intent_sha256,
            request_sha256="2" * 64,
            target_channel_id=CHANNEL,
            visibility="unlisted",
            caller_key="lost-session",
        )
        store.before_session_post(self.approval_intent_sha256)
        fake = FakeYouTube(remote_videos=[])
        result = self.run_reconcile(fake)
        self.assertEqual(result["code"], "youtube_upload_restart_authorized")
        self.assertFalse(result["override_consumed"])
        self.assertFalse(any(call["method"] == "POST" for call in fake.calls))

    def test_reconcile_never_emits_secrets(self):
        self.fence_interrupted_put()
        fake = FakeYouTube(remote_videos=[FakeYouTube().fixture_item()])
        result = self.run_reconcile(fake)
        attempt = next((self.project / ".hvp/youtube-upload-attempts").glob("[0-9a-f]*.json")).read_text()
        combined = json.dumps(result) + attempt
        self.assertNotIn("secret-upload-token", combined)
        self.assertNotIn("secret-session", combined)


if __name__ == "__main__":
    unittest.main()
