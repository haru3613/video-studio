#!/usr/bin/env python3
import json
import tempfile
import unittest
import unittest.mock
import uuid
import wave
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.trustedhost import TrustedHostMiddleware

import review_api
import review_store


def wav(path: Path, seconds: float = 1.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * int(8000 * seconds))


def app_for(roots, storage):
    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
    review_api.register_review_routes(app, roots, storage)
    return app


def fixture(tmp):
    base = Path(tmp)
    studio = base / "studio"
    media = base / "media"
    project = studio / "episode"
    other = media / "comparison"
    (project / "authoring").mkdir(parents=True)
    (project / "output").mkdir()
    (other / "output").mkdir(parents=True)
    wav(project / "narration-final.mp3")
    wav(other / "output" / "final.mp4", 1.5)
    (project / "output" / "cover.png").write_bytes(b"cover-v1")
    manifest = {
        "schema": "haru.review_package.v1",
        "title": "測試影片",
        "assets": [
            {
                "id": "audio-current",
                "kind": "audio",
                "label": "目前旁白",
                "path": "narration-final.mp3",
                "role": "current",
            },
            {
                "id": "video-old",
                "kind": "video",
                "label": "前一版",
                "source": "media",
                "project": "comparison",
                "path": "output/final.mp4",
                "role": "previous",
                "preview_seconds": 1,
            },
            {
                "id": "cover-current",
                "kind": "cover",
                "label": "封面",
                "path": "output/cover.png",
                "role": "current",
            },
        ],
        "changes": [
            {"title": "聲音已調整", "description": "先聽開場。", "asset_id": "audio-current"}
        ],
        "chapters": [{"title": "開場", "seconds": 0}],
    }
    (project / "authoring" / "review-package.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return {"studio": studio, "media": media}, project


def write_headers(token, **extra):
    return {
        "Origin": "http://localhost",
        "X-Haru-Review-CSRF": token,
        "Content-Type": "application/json",
        **extra,
    }


class ReviewHubHttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.roots, self.project = fixture(self.temp.name)
        self.storage = Path(self.temp.name) / "state"
        self.client = TestClient(app_for(self.roots, self.storage), base_url="http://localhost")
        self.review = self.client.get("/api/review/studio/episode").json()
        self.token = self.review["csrf_token"]

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def payload(self, asset_id="audio-current", timestamp=0.25, client_id=None, body="開場語氣可以更自然"):
        asset = next(item for item in self.review["assets"] if item["id"] == asset_id)
        return {
            "client_id": client_id or str(uuid.uuid4()),
            "package_id": self.review["package_id"],
            "asset_id": asset_id,
            "asset_sha256": asset["sha256"],
            "timestamp_seconds": timestamp,
            "body": body,
        }

    def post(self, payload, headers=None):
        return self.client.post(
            "/api/review/studio/episode/comments",
            json=payload,
            headers=headers or write_headers(self.token),
        )

    def test_audio_comparison_accepts_an_explicit_mp4_container(self):
        manifest_path = self.project / "authoring/review-package.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["assets"][1]["kind"] = "audio"
        manifest["assets"][1]["label"] = "A/B from a rendered MP4"
        manifest_path.write_text(json.dumps(manifest))
        response = self.client.get("/api/review/studio/episode")
        self.assertEqual(response.status_code, 200, response.text)
        audio = next(item for item in response.json()["assets"] if item["id"] == "video-old")
        self.assertEqual(audio["kind"], "audio")
        self.assertGreater(audio["duration_seconds"], 0)
        self.assertEqual(self.client.get(audio["url"]).status_code, 200)

    def test_package_shape_range_and_cross_project_asset(self):
        self.assertEqual(self.review["project"]["title"], "測試影片")
        self.assertEqual(self.review["chapters"], [{"title": "開場", "seconds": 0.0}])
        self.assertEqual(len(self.review["assets"]), 3)
        old = next(item for item in self.review["assets"] if item["id"] == "video-old")
        self.assertEqual(old["source"], "media")
        self.assertAlmostEqual(old["duration_seconds"], 1.5, places=1)
        response = self.client.get(old["url"], headers={"Range": "bytes=0-9"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(len(response.content), 10)
        self.assertEqual(response.headers["content-range"].split("/")[0], "bytes 0-9")

    def test_comment_survives_restart_resolves_and_exports(self):
        created = self.post(self.payload())
        self.assertEqual(created.status_code, 201, created.text)
        comment = created.json()["comment"]
        self.assertEqual(comment["status"], "open")
        self.assertNotIn("is_current", comment)

        self.client.close()
        self.client = TestClient(app_for(self.roots, self.storage), base_url="http://localhost")
        restarted = self.client.get("/api/review/studio/episode").json()
        self.assertEqual(len(restarted["comments"]), 1)
        self.assertTrue(restarted["comments"][0]["is_current"])
        self.assertTrue(restarted["comments"][0]["asset_available"])

        patched = self.client.patch(
            f"/api/review/studio/episode/comments/{comment['id']}",
            json={"status": "resolved"},
            headers=write_headers(restarted["csrf_token"]),
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json()["comment"]["status"], "resolved")
        exported = self.client.get("/api/review/studio/episode/export")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("開場語氣可以更自然", exported.text)
        self.assertIn("Status: resolved", exported.text)
        self.assertIn("attachment", exported.headers["content-disposition"])

    def test_duplicate_retry_is_idempotent_and_changed_payload_conflicts(self):
        payload = self.payload()
        first = self.post(payload)
        second = self.post(payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["comment"]["id"], second.json()["comment"]["id"])
        payload["body"] = "different"
        conflict = self.post(payload)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(self.client.get("/api/review/studio/episode").json()["comments"]), 1)

    def test_identical_retry_remains_idempotent_after_package_changes(self):
        payload = self.payload()
        first = self.post(payload)
        self.assertEqual(first.status_code, 201)
        package = json.loads((self.project / "authoring" / "review-package.json").read_text())
        package["changes"].append({"title": "新摘要", "description": "package changed"})
        (self.project / "authoring" / "review-package.json").write_text(json.dumps(package), encoding="utf-8")
        retry = self.post(payload)
        self.assertEqual(retry.status_code, 201, retry.text)
        self.assertEqual(retry.json()["comment"]["id"], first.json()["comment"]["id"])

    def test_identical_retry_remains_idempotent_after_media_changes(self):
        payload = self.payload()
        first = self.post(payload)
        self.assertEqual(first.status_code, 201)
        wav(self.project / "narration-final.mp3", 2)
        retry = self.post(payload)
        self.assertEqual(retry.status_code, 201, retry.text)
        self.assertEqual(retry.json()["comment"]["id"], first.json()["comment"]["id"])

    def test_comment_stays_current_when_only_another_asset_changes(self):
        created = self.post(self.payload())
        self.assertEqual(created.status_code, 201)
        (self.project / "output" / "cover.png").write_bytes(b"cover-v2")
        current = self.client.get("/api/review/studio/episode").json()
        self.assertNotEqual(current["package_id"], self.review["package_id"])
        self.assertTrue(current["comments"][0]["is_current"])

    def test_stale_package_and_media_conflict_preserve_existing_snapshot(self):
        payload = self.payload()
        created = self.post(payload)
        self.assertEqual(created.status_code, 201)
        snapshot = created.json()["comment"]["asset"]

        wav(self.project / "narration-final.mp3", 2)
        stale = self.post(self.payload(client_id=str(uuid.uuid4())))
        self.assertEqual(stale.status_code, 409)
        current = self.client.get("/api/review/studio/episode").json()
        comment = current["comments"][0]
        self.assertEqual(comment["asset"]["sha256"], snapshot["sha256"])
        self.assertFalse(comment["is_current"])
        self.assertFalse(comment["asset_available"])
        historical = self.client.get(snapshot["url"])
        self.assertEqual(historical.status_code, 409)
        (self.project / "narration-final.mp3").unlink()
        self.assertEqual(self.client.get(snapshot["url"]).status_code, 404)
        unknown = self.client.get(
            "/api/review/studio/episode/asset/audio-current?sha256=" + "0" * 64
        )
        self.assertEqual(unknown.status_code, 404)

    def test_package_changed_after_initial_build_is_rejected_inside_locked_append(self):
        payload = self.payload()
        package_path = self.project / "authoring" / "review-package.json"
        package = json.loads(package_path.read_text())
        original_update = review_store.ReviewStore.update

        def change_before_lock(store, mutator):
            package["changes"].append(
                {"title": "concurrent edit", "description": "changed before lock"}
            )
            package_path.write_text(json.dumps(package), encoding="utf-8")
            return original_update(store, mutator)

        with unittest.mock.patch.object(
            review_api.ReviewStore, "update", new=change_before_lock
        ):
            response = self.post(payload)
        self.assertEqual(response.status_code, 409, response.text)
        fresh = self.client.get("/api/review/studio/episode").json()
        self.assertEqual(fresh["comments"], [])

    def test_cover_timestamp_null_and_invalid_timestamps(self):
        finite_payload = self.payload()
        raw = json.dumps(finite_payload).replace("0.25", "NaN")
        nonfinite = self.client.post(
            "/api/review/studio/episode/comments",
            content=raw,
            headers=write_headers(self.token),
        )
        self.assertEqual(nonfinite.status_code, 400)
        self.assertEqual(self.post(self.payload(timestamp=-0.1)).status_code, 422)
        self.assertEqual(self.post(self.payload(timestamp=99)).status_code, 422)
        cover = self.payload(asset_id="cover-current", timestamp=None)
        self.assertEqual(self.post(cover).status_code, 201)
        bad_cover = self.payload(asset_id="cover-current", timestamp=0)
        self.assertEqual(self.post(bad_cover).status_code, 422)

    def test_empty_asset_is_visible_but_cannot_receive_comment(self):
        (self.project / "output" / "cover.png").write_bytes(b"")
        current = self.client.get("/api/review/studio/episode").json()
        cover = next(item for item in current["assets"] if item["id"] == "cover-current")
        self.assertIsNone(cover["sha256"])
        self.assertIsNone(cover["url"])
        self.assertTrue(any("missing or empty" in warning for warning in current["warnings"]))
        payload = {
            "client_id": str(uuid.uuid4()),
            "package_id": current["package_id"],
            "asset_id": "cover-current",
            "asset_sha256": None,
            "timestamp_seconds": None,
            "body": "empty",
        }
        self.assertEqual(self.post(payload).status_code, 409)

    def test_write_security_and_exact_bounded_json_contract(self):
        payload = self.payload()
        self.assertEqual(self.post(payload, {"Content-Type": "application/json"}).status_code, 403)
        self.assertEqual(
            self.post(payload, write_headers(self.token, Origin="http://evil.example")).status_code,
            403,
        )
        self.assertEqual(self.post(payload, write_headers("wrong")).status_code, 403)
        form = self.client.post(
            "/api/review/studio/episode/comments",
            content=b"x=1",
            headers={"Origin": "http://localhost", "X-Haru-Review-CSRF": self.token},
        )
        self.assertEqual(form.status_code, 415)
        extra = self.payload()
        extra["path"] = "/tmp/write-here"
        self.assertEqual(self.post(extra).status_code, 422)
        huge = self.payload(body="x" * 5001)
        self.assertEqual(self.post(huge).status_code, 422)
        self.assertEqual(
            self.client.post(
                "/api/review/studio/episode/comments",
                content=b"{" + b" " * review_api.MAX_WRITE_BYTES + b"}",
                headers=write_headers(self.token),
            ).status_code,
            413,
        )
        self.assertEqual(
            self.client.get("/api/review/studio/episode", headers={"Host": "evil.example"}).status_code,
            400,
        )

    def test_malicious_manifest_paths_symlinks_and_hidden_lease_are_denied(self):
        outside = Path(self.temp.name) / "secret.mp3"
        outside.write_bytes(b"secret")
        package_path = self.project / "authoring" / "review-package.json"
        base = json.loads(package_path.read_text())
        for malicious in ("../secret.mp3", "/tmp/secret.mp3", ".hvp/lease.json"):
            package = dict(base)
            package["assets"] = [{"id": "leak", "kind": "audio", "path": malicious}]
            package_path.write_text(json.dumps(package), encoding="utf-8")
            response = self.client.get("/api/review/studio/episode")
            self.assertEqual(response.status_code, 422, malicious)
            self.assertNotIn("secret", response.text)

        link = self.project / "linked.mp3"
        link.symlink_to(outside)
        package = dict(base)
        package["assets"] = [{"id": "leak", "kind": "audio", "path": "linked.mp3"}]
        package_path.write_text(json.dumps(package), encoding="utf-8")
        self.assertEqual(self.client.get("/api/review/studio/episode").status_code, 422)

        allowed = self.project / ".hvp" / "staging" / "narration-candidates" / "candidate.mp3"
        wav(allowed)
        package["assets"] = [
            {"id": "candidate", "kind": "audio", "path": ".hvp/staging/narration-candidates/candidate.mp3"}
        ]
        package_path.write_text(json.dumps(package), encoding="utf-8")
        review = self.client.get("/api/review/studio/episode")
        self.assertEqual(review.status_code, 200, review.text)
        self.assertIsNotNone(review.json()["assets"][0]["sha256"])


class FallbackManifestTest(unittest.TestCase):
    def test_only_nonempty_canonical_assets_and_metadata_are_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "studio"
            project = root / "fallback"
            (project / "output").mkdir(parents=True)
            (project / "authoring").mkdir()
            wav(project / "narration-final.mp3")
            (project / "output" / "final.mp4").write_bytes(b"")
            (project / "publish-metadata.json").write_text(json.dumps({"title": "Fallback title"}))
            (project / "authoring" / "chapters.json").write_text(
                json.dumps({"chapters": [{"title": "One", "seconds": 1}]})
            )
            client = TestClient(
                app_for({"studio": root, "media": Path(tmp) / "media"}, Path(tmp) / "state"),
                base_url="http://localhost",
            )
            review = client.get("/api/review/studio/fallback").json()
            self.assertEqual(review["project"]["title"], "Fallback title")
            self.assertEqual([asset["id"] for asset in review["assets"]], ["audio-current"])
            self.assertEqual(review["chapters"], [{"title": "One", "seconds": 1.0}])


class ReviewStoreSafetyTest(unittest.TestCase):
    def test_read_does_not_create_storage_and_symlinked_bucket_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            state = Path(tmp) / "state"
            store = review_store.ReviewStore(project, state)
            self.assertEqual(store.read_comments(), [])
            self.assertFalse(state.exists())

            target = Path(tmp) / "target"
            target.mkdir()
            state.mkdir()
            store.directory.symlink_to(target, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                store.read_comments()

    def test_symlinked_feedback_and_lock_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            state = Path(tmp) / "state"
            store = review_store.ReviewStore(project, state)
            store.directory.mkdir(parents=True)
            outside = Path(tmp) / "outside"
            outside.write_text('{"schema":"haru.review_feedback.v1","comments":[]}')
            store.path.symlink_to(outside)
            with self.assertRaises(RuntimeError):
                store.read_comments()
            store.path.unlink()
            store.lock_path.unlink()
            store.lock_path.symlink_to(outside)
            with self.assertRaises(RuntimeError):
                store.update(lambda comments: None)

    def test_configured_storage_root_symlink_is_rejected_but_macos_var_alias_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            linked_state = Path(tmp) / "state"
            linked_state.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                review_store.ReviewStore(project, linked_state)

            # tempfile paths on macOS normally begin with the standard /var ->
            # /private/var alias; that OS-owned alias must remain usable.
            normal = review_store.ReviewStore(project, Path(tmp) / "normal-state")
            self.assertEqual(normal.read_comments(), [])


class CorruptFeedbackTest(unittest.TestCase):
    def test_get_post_and_patch_do_not_clobber_corrupt_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots, project = fixture(tmp)
            state = Path(tmp) / "state"
            store = review_store.ReviewStore(project, state)
            store.directory.mkdir(parents=True)
            corrupt = b"{ definitely not valid JSON"
            store.path.write_bytes(corrupt)
            app = app_for(roots, state)
            client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)

            self.assertEqual(client.get("/api/review/studio/episode").status_code, 500)
            # The server token is intentionally unknowable after GET fails; use
            # the token returned by route registration to reach storage reads.
            isolated = FastAPI()
            token = review_api.register_review_routes(isolated, roots, state)
            isolated_client = TestClient(
                isolated, base_url="http://localhost", raise_server_exceptions=False
            )
            package, _ = review_api._build_review(
                "studio", "episode", roots, token, None, include_comments=False
            )
            asset = package["assets"][0]
            post_payload = {
                "client_id": str(uuid.uuid4()),
                "package_id": package["package_id"],
                "asset_id": asset["id"],
                "asset_sha256": asset["sha256"],
                "timestamp_seconds": 0,
                "body": "must not overwrite",
            }
            headers = write_headers(token)
            self.assertEqual(
                isolated_client.post(
                    "/api/review/studio/episode/comments", json=post_payload, headers=headers
                ).status_code,
                500,
            )
            self.assertEqual(
                isolated_client.patch(
                    f"/api/review/studio/episode/comments/{uuid.uuid4()}",
                    json={"status": "resolved"},
                    headers=headers,
                ).status_code,
                500,
            )
            self.assertEqual(store.path.read_bytes(), corrupt)


if __name__ == "__main__":
    unittest.main()
