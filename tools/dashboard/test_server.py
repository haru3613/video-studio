#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

try:
    import server
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:
    if exc.name in {"fastapi", "starlette", "httpx"}:
        server = None
        TestClient = None
    else:
        raise


@unittest.skipUnless(server is not None, "fastapi not installed")
class ScanTest(unittest.TestCase):
    def test_buckets_exclusions_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "demo"
            (proj / "assets" / "story").mkdir(parents=True)
            (proj / "node_modules" / "x").mkdir(parents=True)
            (proj / "output").mkdir()

            (proj / "assets" / "story" / "s1.png").write_bytes(b"png")
            (proj / "output" / "final.mp4").write_bytes(b"vid")
            (proj / "narration.txt").write_text("hi", encoding="utf-8")
            (proj / "voice.mp3").write_bytes(b"mp3")
            (proj / ".DS_Store").write_bytes(b"junk")
            (proj / "node_modules" / "x" / "bad.png").write_bytes(b"no")
            (proj / "some.blend").write_bytes(b"ignored ext")

            buckets, truncated = server.scan_files(proj)

            self.assertFalse(truncated)
            self.assertEqual([f["path"] for f in buckets["images"]], ["assets/story/s1.png"])
            self.assertEqual([f["path"] for f in buckets["videos"]], ["output/final.mp4"])
            self.assertEqual([f["path"] for f in buckets["docs"]], ["narration.txt"])
            self.assertEqual([f["path"] for f in buckets["audio"]], ["voice.mp3"])
            self.assertEqual(buckets["images"][0]["size"], 3)

    def test_bucket_cap_truncates(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "demo"
            proj.mkdir()
            for i in range(5):
                (proj / f"img{i}.png").write_bytes(b"x")
            old = server.BUCKET_CAP
            server.BUCKET_CAP = 2
            try:
                buckets, truncated = server.scan_files(proj)
            finally:
                server.BUCKET_CAP = old
            self.assertTrue(truncated)
            self.assertEqual(len(buckets["images"]), 2)

    def test_scan_survives_unstatable_entry(self):
        import unittest.mock
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "demo"
            proj.mkdir()
            (proj / "ok.png").write_bytes(b"x")
            (proj / "gone.png").write_bytes(b"x")

            real_scandir = server.os.scandir

            def flaky_scandir(d):
                entries = list(real_scandir(d))
                for e in entries:
                    if e.name == "gone.png":
                        patched = unittest.mock.MagicMock(wraps=e)
                        patched.name = e.name
                        patched.stat.side_effect = FileNotFoundError()
                        patched.is_dir.return_value = False
                        patched.is_file.return_value = True
                        yield patched
                    else:
                        yield e

            with unittest.mock.patch.object(server.os, "scandir", flaky_scandir):
                buckets, truncated = server.scan_files(proj)
            self.assertEqual([f["path"] for f in buckets["images"]], ["ok.png"])

    def test_read_json_none_on_missing_or_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(server.read_json(Path(tmp) / "nope.json"))
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertIsNone(server.read_json(bad))
            # Test non-UTF-8 file (UnicodeDecodeError)
            non_utf8 = Path(tmp) / "non_utf8.json"
            non_utf8.write_bytes(b"\xff\xfe\x00\x01garbage")
            self.assertIsNone(server.read_json(non_utf8))
            good = Path(tmp) / "ok.json"
            good.write_text(json.dumps({"a": 1}), encoding="utf-8")
            self.assertEqual(server.read_json(good), {"a": 1})


def make_fixture(tmp):
    """studio root with one healthy + one corrupt-status project; media root with one legacy project."""
    studio = Path(tmp) / "studio-projects"
    media = Path(tmp) / "media-projects"
    (studio / "good-proj" / "assets").mkdir(parents=True)
    (studio / "bad-proj").mkdir(parents=True)
    (studio / "_manifests").mkdir()
    (media / "legacy-proj" / "output").mkdir(parents=True)

    (studio / "good-proj" / "pipeline_status.json").write_text(
        json.dumps(
            {
                "schema": "haru.pipeline_status.v1",
                "overall_status": "parked_awaiting_tts",
                "generated_at": "2026-07-06T13:55:00+00:00",
                "updated_by": "codex",
                "blockers": ["waiting quota"],
                "next_actions": ["run tts"],
                "stages": {"proposal": {"status": "pass", "files": []}},
            }
        ),
        encoding="utf-8",
    )
    (studio / "good-proj" / "assets" / "cover.png").write_bytes(b"png")
    (studio / "good-proj" / "script.md").write_text("# s", encoding="utf-8")
    (studio / "bad-proj" / "pipeline_status.json").write_text("{broken", encoding="utf-8")
    (media / "legacy-proj" / "output" / "final.mp4").write_bytes(b"vid")
    (media / "legacy-proj" / "notes.md").write_text("n", encoding="utf-8")
    return {"studio": studio, "media": media}


@unittest.skipUnless(server is not None, "fastapi not installed")
class ListDetailTest(unittest.TestCase):
    def test_list_projects_shapes_and_corrupt_status_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = make_fixture(tmp)
            result = server.list_projects(roots)

            names = {p["name"] for p in result["projects"]}
            self.assertEqual(names, {"good-proj", "bad-proj", "legacy-proj"})
            self.assertEqual(result["warnings"], [])

            good = next(p for p in result["projects"] if p["name"] == "good-proj")
            self.assertEqual(good["source"], "studio")
            self.assertEqual(good["overall_status"], "parked_awaiting_tts")
            self.assertEqual(good["updated_at"], "2026-07-06T13:55:00+00:00")
            self.assertEqual(good["thumbnail"], "/media/studio/good-proj/assets/cover.png")
            self.assertEqual(good["counts"], {"videos": 0, "images": 1, "audio": 0, "docs": 2})

            bad = next(p for p in result["projects"] if p["name"] == "bad-proj")
            self.assertIsNone(bad["overall_status"])
            self.assertTrue(bad["updated_at"])  # falls back to dir mtime, still sortable

            legacy = next(p for p in result["projects"] if p["name"] == "legacy-proj")
            self.assertIsNone(legacy["overall_status"])
            self.assertEqual(legacy["counts"]["videos"], 1)

    def test_list_projects_survives_numeric_generated_at(self):
        """A valid-JSON manifest with a numeric generated_at must fall back to
        mtime instead of TypeError-ing the whole-list sort."""
        with tempfile.TemporaryDirectory() as tmp:
            studio = Path(tmp) / "studio-projects"
            (studio / "drifted-proj").mkdir(parents=True)
            (studio / "normal-proj").mkdir(parents=True)
            (studio / "drifted-proj" / "pipeline_status.json").write_text(
                json.dumps({"schema": "haru.pipeline_status.v1", "generated_at": 1751800000}),
                encoding="utf-8",
            )
            roots = {"studio": studio, "media": Path(tmp) / "media-projects"}

            result = server.list_projects(roots)  # must not TypeError in the sort

            names = {p["name"] for p in result["projects"]}
            self.assertEqual(names, {"drifted-proj", "normal-proj"})
            drifted = next(p for p in result["projects"] if p["name"] == "drifted-proj")
            self.assertIsInstance(drifted["updated_at"], str)

    def test_thumbnail_url_percent_encoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = Path(tmp) / "studio-projects"
            proj = studio / "weird-proj"
            proj.mkdir(parents=True)
            (proj / "we ird #1.png").write_bytes(b"png")
            roots = {"studio": studio, "media": Path(tmp) / "media-projects"}

            result = server.list_projects(roots)

            card = next(p for p in result["projects"] if p["name"] == "weird-proj")
            self.assertIn("we%20ird%20%231.png", card["thumbnail"])
            self.assertTrue(card["thumbnail"].startswith("/media/studio/"))

    def test_missing_root_warns_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = make_fixture(tmp)
            roots["media"] = Path(tmp) / "does-not-exist"
            result = server.list_projects(roots)
            self.assertEqual(len(result["warnings"]), 1)
            self.assertIn("does-not-exist", result["warnings"][0])
            self.assertEqual({p["name"] for p in result["projects"]}, {"good-proj", "bad-proj"})

    def test_project_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = make_fixture(tmp)
            d = server.project_detail("studio", "good-proj", roots)
            self.assertEqual(d["pipeline"]["overall_status"], "parked_awaiting_tts")
            self.assertEqual(d["files"]["images"][0]["path"], "assets/cover.png")
            self.assertIsNone(server.project_detail("studio", "nope", roots))
            self.assertIsNone(server.project_detail("weird-source", "good-proj", roots))
            self.assertIsNone(server.project_detail("studio", "../escape", roots))

    def test_resolve_media_path_blocks_traversal_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = make_fixture(tmp)
            secret = Path(tmp) / "secret.txt"
            secret.write_text("s", encoding="utf-8")

            ok = server.resolve_media_path("studio", "good-proj/assets/cover.png", roots)
            self.assertIsNotNone(ok)
            self.assertIsNone(server.resolve_media_path("studio", "../secret.txt", roots))
            self.assertIsNone(server.resolve_media_path("studio", "good-proj/../../secret.txt", roots))
            self.assertIsNone(server.resolve_media_path("nope", "x", roots))
            self.assertIsNone(server.resolve_media_path("studio", "good-proj", roots))  # dir, not file

            link = roots["studio"] / "good-proj" / "leak.png"
            link.symlink_to(secret)
            self.assertIsNone(server.resolve_media_path("studio", "good-proj/leak.png", roots))

            # embedded null byte raises ValueError in Path.resolve(), not OSError
            self.assertIsNone(server.resolve_media_path("studio", "\x00x", roots))


@unittest.skipUnless(server is not None, "fastapi not installed")
class ApiTest(unittest.TestCase):
    def test_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = make_fixture(tmp)
            client = TestClient(server.create_app(roots, require_session=False), base_url="http://localhost")

            r = client.get("/api/projects")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(len(r.json()["projects"]), 3)

            r = client.get("/api/projects/studio/good-proj")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["pipeline"]["overall_status"], "parked_awaiting_tts")

            self.assertEqual(client.get("/api/projects/studio/nope").status_code, 404)

            r = client.get("/media/studio/good-proj/assets/cover.png")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.content, b"png")

            # encoded traversal reaches the handler as ../secret.txt and must 404
            self.assertEqual(client.get("/media/studio/%2e%2e/secret.txt").status_code, 404)

    def test_index_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = make_fixture(tmp)
            client = TestClient(server.create_app(roots, require_session=False), base_url="http://localhost")
            r = client.get("/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("Video Studio", r.text)


@unittest.skipUnless(server is not None, "fastapi not installed")
class ProgressTest(unittest.TestCase):
    def _proj(self, tmp, lease=None, approval=None):
        proj = Path(tmp) / "demo"
        (proj / ".hvp").mkdir(parents=True)
        if lease is not None:
            (proj / ".hvp" / "lease.json").write_text(json.dumps(lease), encoding="utf-8")
        if approval is not None:
            (proj / "publish").mkdir()
            (proj / "publish" / "publish-approval.json").write_text(json.dumps(approval), encoding="utf-8")
        return proj

    def test_lease_active_only_until_it_expires(self):
        import time

        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._proj(tmp, lease={"owner": "codex", "expires_at": now + 600})
            self.assertTrue(server.read_lease(proj)["active"])
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._proj(tmp, lease={"owner": "codex", "expires_at": now - 600})
            self.assertFalse(server.read_lease(proj)["active"])

    def test_stale_lock_file_is_not_a_running_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._proj(tmp)
            (proj / ".hvp" / "lease.lock").write_bytes(b"")
            self.assertIsNone(server.read_lease(proj))

    def test_upload_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._proj(tmp, approval={"video_id": "abc123", "visibility": "public"})
            info = server.upload_info(proj, {"overall_status": "publish_approved"})
            self.assertEqual(info["state"], "uploaded")
            self.assertEqual(info["url"], "https://youtu.be/abc123")
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._proj(tmp, approval={"video_id": None})
            status = {"overall_status": "ready_for_human_upload_approval"}
            self.assertEqual(server.upload_info(proj, status)["state"], "awaiting_approval")
        with tempfile.TemporaryDirectory() as tmp:
            proj = self._proj(tmp)
            self.assertEqual(server.upload_info(proj, None)["state"], "not_ready")

    def test_stage_progress_reports_first_unpassed_gate(self):
        status = {"stages": {"tts": {"status": "pass"}, "render": {"status": "missing"}, "qa": {"status": "warn"}}}
        self.assertEqual(server.stage_progress(status), {"done": 1, "total": 3, "current": "render"})
        self.assertIsNone(server.stage_progress(None))
        self.assertIsNone(server.stage_progress({"stages": {}}))

    def test_stage_progress_ignores_stages_outside_required(self):
        """`upload` is permanently requires_harvey; counting it would pin a
        finished project at 2/3 and name a blocker that never clears."""
        status = {
            "stages": {"tts": {"status": "pass"}, "render": {"status": "pass"},
                       "upload": {"status": "requires_harvey"}},
            "required_stages": ["tts", "render"],
        }
        self.assertEqual(server.stage_progress(status), {"done": 2, "total": 2, "current": None})


@unittest.skipUnless(server is not None, "fastapi not installed")
class MediaExposureTest(unittest.TestCase):
    def test_media_refuses_dotfiles_so_the_lease_token_stays_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = make_fixture(tmp)
            proj = roots["studio"] / "demo"
            (proj / ".hvp").mkdir(parents=True, exist_ok=True)
            (proj / ".hvp" / "lease.json").write_text('{"token": "SECRET-TOKEN"}', encoding="utf-8")
            client = TestClient(server.create_app(roots, require_session=False), base_url="http://localhost")
            r = client.get("/media/studio/demo/.hvp/lease.json")
            self.assertEqual(r.status_code, 404)
            self.assertNotIn("SECRET-TOKEN", r.text)
            self.assertIsNone(server.resolve_media_path("studio", "demo/.hvp/lease.json", roots))

    def test_symlink_cannot_launder_a_dotfile(self):
        """A non-dot name pointing into .hvp must not smuggle the lease token
        out; the dot check runs on the resolved path for exactly this reason."""
        with tempfile.TemporaryDirectory() as tmp:
            roots = make_fixture(tmp)
            proj = roots["studio"] / "demo"
            (proj / ".hvp").mkdir(parents=True, exist_ok=True)
            (proj / ".hvp" / "lease.json").write_text('{"token": "SECRET-TOKEN"}', encoding="utf-8")
            (proj / "notes.json").symlink_to(proj / ".hvp" / "lease.json")
            (proj / "hv").symlink_to(proj / ".hvp", target_is_directory=True)

            client = TestClient(server.create_app(roots, require_session=False), base_url="http://localhost")
            for url in ("/media/studio/demo/notes.json", "/media/studio/demo/hv/lease.json"):
                r = client.get(url)
                self.assertEqual(r.status_code, 404, url)
                self.assertNotIn("SECRET-TOKEN", r.text)

    def test_media_refuses_unbucketed_extensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = make_fixture(tmp)
            (roots["studio"] / "demo").mkdir(parents=True, exist_ok=True)
            (roots["studio"] / "demo" / "client_secret.pem").write_text("KEY", encoding="utf-8")
            client = TestClient(server.create_app(roots, require_session=False), base_url="http://localhost")
            r = client.get("/media/studio/demo/client_secret.pem")
            self.assertEqual(r.status_code, 404)
            self.assertNotIn("KEY", r.text)

    def test_rejects_foreign_host_header(self):
        """127.0.0.1 binding alone does not stop DNS rebinding."""
        with tempfile.TemporaryDirectory() as tmp:
            client = TestClient(server.create_app(make_fixture(tmp), require_session=False), base_url="http://localhost")
            self.assertEqual(client.get("/api/projects").status_code, 200)
            self.assertEqual(client.get("/api/projects", headers={"Host": "evil.example"}).status_code, 400)


@unittest.skipUnless(server is not None, "fastapi not installed")
class OverviewApiTest(unittest.TestCase):
    def test_grouped_endpoint_preserves_original_inventory_and_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            roots = {"studio": base / "studio", "media": base / "media"}
            for root in roots.values():
                root.mkdir()
            for name in ("film", "preview", "new-project"):
                (roots["studio"] / name).mkdir()
            (roots["media"] / "film").mkdir()
            members = [{"source": "studio", "name": name, "label": name} for name in ("film", "preview")]
            catalog = base / "catalog.json"
            catalog.write_text(json.dumps({"schema": "haru.dashboard_catalog.v1", "groups": [{
                "id": "film", "title": "中文影片", "type": "video", "collection": "解說",
                "primary": members[0], "members": members, "cover": None, "reason": "版本合併"
            }]}), encoding="utf-8")
            client = TestClient(server.create_app(roots, overview_catalog_path=catalog, require_session=False), base_url="http://localhost")
            overview = client.get("/api/overview")
            self.assertEqual(overview.status_code, 200)
            self.assertEqual(overview.json()["counts"], {"video": 1, "library": 0, "experiment": 0, "unclassified": 2})
            self.assertEqual(len(client.get("/api/projects").json()["projects"]), 4)
            self.assertEqual(client.get("/api/projects/studio/preview").status_code, 200)
            self.assertEqual(client.get("/overview/overview.js").status_code, 200)
            self.assertEqual(client.get("/overview/overview.css").status_code, 200)
            self.assertEqual(client.get("/overview/server.py").status_code, 404)
            self.assertEqual(client.post("/api/overview", json={}).status_code, 405)
            self.assertEqual(client.get("/api/overview", headers={"Host": "evil.example"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
