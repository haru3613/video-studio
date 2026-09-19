import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
from tools import youtube_analytics as analytics


class Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return self.data


class YouTubeAnalyticsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        (self.repo / "jobs/prompts").mkdir(parents=True)
        (self.repo / "jobs/prompts/retro.md").write_text("capture only\n")
        self.token = root / "youtube-token.json"
        self.token.write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "type": "oauth_access_token",
                    "access_token": "secret-token",
                    "scopes": sorted(analytics.SCOPES),
                }
            )
        )
        self.token.chmod(0o600)

    def tearDown(self):
        self.temporary.cleanup()

    def arguments(self):
        return argparse.Namespace(
            video_id="dQw4w9WgXcQ",
            output_id="dQw4w9WgXcQ",
            start_date="2026-07-01",
            end_date="2026-07-28",
            oauth_token_file=str(self.token),
            retros_root="retros",
            policy_file="jobs/prompts/retro.md",
            repo_root=self.repo,
        )

    def payload(self):
        return {
            "kind": "youtubeAnalytics#resultTable",
            "columnHeaders": [
                {
                    "name": name,
                    "columnType": "METRIC",
                    "dataType": "FLOAT",
                }
                for name in analytics.METRICS
            ],
            "rows": [[100, 250, 150, 62.5, 10, 2, 3, 4, 1]],
        }

    def feed_payload(self):
        return b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>aaaaaaaaaaa</yt:videoId>
    <title>Oldest due</title>
    <published>2026-07-01T12:00:00Z</published>
  </entry>
  <entry>
    <yt:videoId>bbbbbbbbbbb</yt:videoId>
    <title>Newer due</title>
    <published>2026-07-10T12:00:00Z</published>
  </entry>
  <entry>
    <yt:videoId>ccccccccccc</yt:videoId>
    <title>Too new</title>
    <published>2026-07-27T12:00:00Z</published>
  </entry>
</feed>"""

    def test_exact_module_entrypoint_loads(self):
        completed = subprocess.run(
            [sys.executable, "-m", "tools.youtube_analytics", "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_query_is_authorized_get_for_exact_video(self):
        raw = json.dumps(self.payload()).encode()
        with mock.patch.object(
            analytics.urllib.request, "urlopen", return_value=Response(raw)
        ) as opened:
            payload, returned_raw = analytics.query(
                "secret-token", "dQw4w9WgXcQ", "2026-07-01", "2026-07-28"
            )

        request = opened.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(query["ids"], ["channel==MINE"])
        self.assertEqual(query["filters"], ["video==dQw4w9WgXcQ"])
        self.assertEqual(payload, self.payload())
        self.assertEqual(returned_raw, raw)

    def test_feed_is_read_only_and_parses_typed_entries(self):
        raw = self.feed_payload()
        with mock.patch.object(
            analytics.urllib.request, "urlopen", return_value=Response(raw)
        ) as opened:
            entries, returned_raw = analytics.feed(
                "UCaaaaaaaaaaaaaaaaaaaaaa"
            )

        request = opened.call_args.args[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(request.method, "GET")
        self.assertEqual(
            request.get_header("User-agent"), "video-studio/0.1"
        )
        self.assertEqual(
            query["channel_id"], ["UCaaaaaaaaaaaaaaaaaaaaaa"]
        )
        self.assertEqual(entries[0]["video_id"], "aaaaaaaaaaa")
        self.assertEqual(
            entries[0]["published_at"],
            analytics.parse_timestamp("2026-07-01T12:00:00Z"),
        )
        self.assertEqual(returned_raw, raw)

    def test_success_writes_versioned_retro_and_digest_bound_receipt(self):
        raw = json.dumps(self.payload()).encode()
        with mock.patch.object(
            analytics, "query", return_value=(self.payload(), raw)
        ):
            result = analytics.fetch_retro(self.arguments(), self.repo)

        retro = self.repo / "retros/dQw4w9WgXcQ.md"
        receipt_path = self.repo / "retros/dQw4w9WgXcQ.receipt.json"
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(result["code"], "retro_written")
        self.assertIn("schema: haru.youtube_retro.v1", retro.read_text())
        self.assertEqual(receipt["status"], "captured_not_interpreted")
        self.assertEqual(receipt["retro"], analytics.digest(retro))
        self.assertNotIn("secret-token", retro.read_text() + receipt_path.read_text())

    def test_due_retro_selects_oldest_missing_video_and_binds_artifacts(self):
        feed_raw = self.feed_payload()
        analytics_raw = json.dumps(self.payload()).encode()
        arguments = argparse.Namespace(
            channel_id="UCaaaaaaaaaaaaaaaaaaaaaa",
            run_id="retro-2026-07-28",
            as_of_date="2026-07-28",
            oauth_token_file=str(self.token),
            min_age_days=3,
            max_age_days=60,
            retros_root="retros",
            run_receipts_root=".hvp/retro-runs",
            policy_file="jobs/prompts/retro.md",
            repo_root=self.repo,
        )
        with mock.patch.object(
            analytics,
            "feed",
            return_value=(
                [
                    {
                        "video_id": "aaaaaaaaaaa",
                        "title": "Oldest due",
                        "published_at": analytics.parse_timestamp("2026-07-01T12:00:00Z"),
                    },
                    {
                        "video_id": "bbbbbbbbbbb",
                        "title": "Newer due",
                        "published_at": analytics.parse_timestamp("2026-07-10T12:00:00Z"),
                    },
                    {
                        "video_id": "ccccccccccc",
                        "title": "Too new",
                        "published_at": analytics.parse_timestamp("2026-07-27T12:00:00Z"),
                    },
                ],
                feed_raw,
            ),
        ), mock.patch.object(
            analytics, "query", return_value=(self.payload(), analytics_raw)
        ):
            result = analytics.run_due_retro(arguments, self.repo)

        run_receipt = json.loads(
            (self.repo / ".hvp/retro-runs/retro-2026-07-28.json").read_text()
        )
        self.assertEqual(result["code"], "retro_written")
        self.assertEqual(run_receipt["selected"]["video_id"], "aaaaaaaaaaa")
        self.assertEqual(
            run_receipt["selected"]["retro"],
            analytics.digest(self.repo / "retros/aaaaaaaaaaa.md"),
        )

        run_receipt["status"] = "running"
        run_receipt.pop("code")
        run_receipt["selected"] = {
            key: run_receipt["selected"][key]
            for key in ("video_id", "title", "published_at")
        }
        (self.repo / ".hvp/retro-runs/retro-2026-07-28.json").write_text(
            json.dumps(run_receipt)
        )
        with mock.patch.object(analytics, "feed") as fetched_feed, mock.patch.object(
            analytics, "query"
        ) as queried:
            resumed = analytics.run_due_retro(arguments, self.repo)

        self.assertEqual(resumed["code"], "retro_written")
        fetched_feed.assert_not_called()
        queried.assert_not_called()

    def test_due_retro_no_pending_is_a_versioned_successful_noop(self):
        entries = [
            {
                "video_id": "aaaaaaaaaaa",
                "title": "Already done",
                "published_at": analytics.parse_timestamp("2026-07-01T12:00:00Z"),
            }
        ]
        retros = self.repo / "retros"
        retros.mkdir()
        retro = retros / "aaaaaaaaaaa.md"
        retro.write_text("done")
        (retros / "aaaaaaaaaaa.receipt.json").write_text(
            json.dumps(
                {
                    "format_version": 1,
                    "schema": "haru.youtube_retro_receipt.v1",
                    "status": "captured_not_interpreted",
                    "video_id": "aaaaaaaaaaa",
                    "retro": analytics.digest(retro),
                }
            )
        )
        arguments = argparse.Namespace(
            channel_id="UCaaaaaaaaaaaaaaaaaaaaaa",
            run_id="retro-2026-07-28",
            as_of_date="2026-07-28",
            oauth_token_file=str(self.token),
            min_age_days=3,
            max_age_days=60,
            retros_root="retros",
            run_receipts_root=".hvp/retro-runs",
            policy_file="jobs/prompts/retro.md",
            repo_root=self.repo,
        )
        with mock.patch.object(
            analytics, "feed", return_value=(entries, self.feed_payload())
        ), mock.patch.object(analytics, "query") as queried:
            result = analytics.run_due_retro(arguments, self.repo)

        self.assertEqual(result["code"], "no_pending_retros")
        queried.assert_not_called()
        receipt = json.loads(
            (self.repo / ".hvp/retro-runs/retro-2026-07-28.json").read_text()
        )
        self.assertIsNone(receipt["selected"])
        self.assertEqual(receipt["artifacts"], [])
        receipt["artifacts"] = [analytics.digest(retro)]
        (self.repo / ".hvp/retro-runs/retro-2026-07-28.json").write_text(
            json.dumps(receipt)
        )
        with self.assertRaises(analytics.RetroError) as corrupted:
            analytics.run_due_retro(arguments, self.repo)
        self.assertEqual(corrupted.exception.code, "retro_write_failed")

    def test_due_selection_rejects_corrupt_or_symlinked_completed_pairs(self):
        entry = {
            "video_id": "aaaaaaaaaaa",
            "title": "Corrupt pair",
            "published_at": analytics.parse_timestamp("2026-07-01T12:00:00Z"),
        }
        retros = self.repo / "retros"
        retros.mkdir()
        retro = retros / "aaaaaaaaaaa.md"
        learning = retros / "aaaaaaaaaaa.receipt.json"
        retro.write_text("done")
        learning.write_text("{}")

        with self.assertRaises(analytics.RetroError) as corrupt:
            analytics.select_due(
                [entry],
                retros,
                analytics.parse_timestamp("2026-07-28T00:00:00Z"),
                3,
                60,
            )
        self.assertEqual(corrupt.exception.code, "retro_write_failed")

        retro.unlink()
        learning.unlink()
        outside = Path(self.temporary.name) / "outside.md"
        outside.write_text("done")
        retro.symlink_to(outside)
        learning.write_text("{}")
        with self.assertRaises(analytics.RetroError) as symlinked:
            analytics.completed_retro_pair(retros, "aaaaaaaaaaa")
        self.assertEqual(symlinked.exception.code, "retro_write_failed")

    def test_auth_quota_and_api_failures_are_not_success(self):
        cases = [
            (401, b"{}", "blocked", "youtube_auth_required"),
            (
                403,
                b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}',
                "blocked",
                "youtube_quota_exhausted",
            ),
            (400, b'{"error":{"errors":[{"reason":"badRequest"}]}}', "error", "youtube_api_failed"),
        ]
        for status, body, expected_status, code in cases:
            with self.subTest(status=status, code=code):
                failure = urllib.error.HTTPError(
                    analytics.ENDPOINT, status, "failure", {}, io.BytesIO(body)
                )
                with mock.patch.object(
                    analytics.urllib.request, "urlopen", side_effect=failure
                ):
                    with self.assertRaises(analytics.RetroError) as raised:
                        analytics.query(
                            "secret-token",
                            "dQw4w9WgXcQ",
                            "2026-07-01",
                            "2026-07-28",
                        )
                self.assertEqual(raised.exception.status, expected_status)
                self.assertEqual(raised.exception.code, code)

    def test_non_numeric_metric_response_is_rejected(self):
        payload = self.payload()
        payload["rows"][0][0] = "one hundred"

        with self.assertRaises(analytics.RetroError) as raised:
            analytics.metrics_from(payload)

        self.assertEqual(raised.exception.code, "youtube_api_failed")

    def test_token_contract_must_be_external_private_and_not_a_symlink(self):
        inside = self.repo / "token.json"
        inside.write_bytes(self.token.read_bytes())
        inside.chmod(0o600)
        link = self.token.parent / "token-link.json"
        link.symlink_to(self.token)
        for value in (inside, link):
            with self.subTest(value=value):
                with self.assertRaises(analytics.RetroError) as raised:
                    analytics.load_token(str(value), self.repo)
                self.assertEqual(raised.exception.status, "blocked")
                self.assertEqual(raised.exception.code, "youtube_auth_required")

    def test_write_failure_cannot_report_success(self):
        raw = json.dumps(self.payload()).encode()
        output = io.StringIO()
        with mock.patch.object(
            analytics, "query", return_value=(self.payload(), raw)
        ), mock.patch.object(analytics, "atomic_write", side_effect=OSError), contextlib.redirect_stdout(output):
            exit_code = analytics.main([
                "fetch-retro",
                "--video-id", "dQw4w9WgXcQ",
                "--output-id", "dQw4w9WgXcQ",
                "--start-date", "2026-07-01",
                "--end-date", "2026-07-28",
                "--oauth-token-file", str(self.token),
                "--repo-root", str(self.repo),
            ])

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(output.getvalue())["code"], "retro_write_failed")


if __name__ == "__main__":
    unittest.main()
