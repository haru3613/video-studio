import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import youtube_credentials


CHANNEL = "UC" + "a" * 22
OTHER_CHANNEL = "UC" + "b" * 22
REFERENCE = f"youtube:{CHANNEL}"


class FakeTransport:
    def __init__(self, *, refresh=None, channels=None):
        self.refresh = refresh or (200, {}, b'{}')
        self.channels = channels or (200, {}, json.dumps({"items": [{"id": CHANNEL}]}).encode())
        self.calls = []

    def __call__(self, method, url, headers=None, body=None):
        self.calls.append((method, url, headers or {}, body))
        if url == youtube_credentials.TOKEN_URL:
            return self.refresh
        if url == youtube_credentials.CHANNELS_URL:
            return self.channels
        raise AssertionError((method, url))


class YouTubeCredentialStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Path(self.temporary.name) / "secrets/youtube"
        self.channel_dir = self.store / CHANNEL
        self.channel_dir.mkdir(parents=True)
        for directory in (self.store.parent, self.store, self.channel_dir):
            directory.chmod(0o700)
        self.credential = self.channel_dir / "credential.json"
        self.write_credential()
        self.store_patch = mock.patch.object(
            youtube_credentials, "CREDENTIAL_STORE_ROOT", self.store.resolve()
        )
        self.store_patch.start()

    def tearDown(self):
        self.store_patch.stop()
        self.temporary.cleanup()

    def write_credential(self, **updates):
        value = {
            "format_version": 1,
            "type": "youtube_oauth_refresh_credential",
            "channel_id": CHANNEL,
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token",
            "access_token": "access-token",
            "expires_at": "2099-01-01T00:00:00Z",
            "scopes": sorted(youtube_credentials.REQUIRED_SCOPES),
        }
        value.update(updates)
        self.credential.write_text(json.dumps(value), encoding="utf-8")
        self.credential.chmod(0o600)

    def resolve(self, transport=None):
        return youtube_credentials.resolve_and_refresh(REFERENCE, CHANNEL, transport)

    def assert_code(self, code, operation):
        with self.assertRaisesRegex(youtube_credentials.CredentialError, f"^{code}$"):
            operation()

    def test_healthy_credential_reads_through_protected_store(self):
        transport = FakeTransport()

        self.assertEqual(self.resolve(transport), "access-token")
        self.assertEqual([call[1] for call in transport.calls], [youtube_credentials.CHANNELS_URL])
        self.assertEqual(transport.calls[0][2]["Authorization"], "Bearer access-token")

    def test_google_downloaded_desktop_config_uses_fixed_v2_pkce_authorization(self):
        config = Path(self.temporary.name) / "desktop-client.json"
        config.write_text(json.dumps({"installed": {
            "client_id": "desktop-client-id",
            "client_secret": "desktop-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }}))
        transport = FakeTransport(refresh=(200, {}, json.dumps({
            "access_token": "provision-access",
            "refresh_token": "provision-refresh",
            "expires_in": 3600,
            "scope": " ".join(sorted(youtube_credentials.REQUIRED_SCOPES)),
        }).encode()))

        def authorize(make_url, state):
            redirect = "http://127.0.0.1:42500/oauth2/callback"
            url = urllib.parse.urlsplit(make_url(redirect))
            self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}",
                             "https://accounts.google.com/o/oauth2/v2/auth")
            query = urllib.parse.parse_qs(url.query)
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertEqual(query["state"], [state])
            self.assertEqual(query["redirect_uri"], [redirect])
            return "authorization-code", redirect

        with mock.patch.object(youtube_credentials, "_receive_authorization_code", side_effect=authorize):
            youtube_credentials.provision(CHANNEL, config, transport)
        self.assertEqual([call[1] for call in transport.calls],
                         [youtube_credentials.TOKEN_URL, youtube_credentials.CHANNELS_URL])
        self.assertEqual(json.loads(self.credential.read_text())["refresh_token"], "provision-refresh")

    def test_desktop_config_rejects_foreign_endpoints_before_authorization(self):
        config = Path(self.temporary.name) / "desktop-client.json"
        for auth_uri, token_uri in [
            ("https://accounts.google.com.evil.example/o/oauth2/auth", youtube_credentials.TOKEN_URL),
            (youtube_credentials.AUTHORIZE_URL, "https://evil.example/token"),
        ]:
            config.write_text(json.dumps({"installed": {
                "client_id": "desktop-client-id", "client_secret": "desktop-client-secret",
                "auth_uri": auth_uri, "token_uri": token_uri,
            }}))
            with mock.patch.object(youtube_credentials, "_receive_authorization_code") as authorize:
                self.assert_code("youtube_credential_client_config_invalid",
                                 lambda: youtube_credentials.provision(CHANNEL, config))
                authorize.assert_not_called()

    def test_expired_credential_refreshes_and_atomically_replaces_leaf(self):
        self.write_credential(expires_at="2000-01-01T00:00:00Z")
        transport = FakeTransport(
            refresh=(
                200,
                {},
                json.dumps(
                    {
                        "access_token": "new-access-token",
                        "expires_in": 3600,
                        "scope": " ".join(sorted(youtube_credentials.REQUIRED_SCOPES)),
                    }
                ).encode(),
            )
        )

        self.assertEqual(self.resolve(transport), "new-access-token")
        saved = json.loads(self.credential.read_text(encoding="utf-8"))
        self.assertEqual(saved["access_token"], "new-access-token")
        self.assertGreater(
            dt.datetime.fromisoformat(saved["expires_at"].replace("Z", "+00:00")),
            dt.datetime.now(dt.timezone.utc),
        )
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET"])
        self.assertEqual(self.credential.stat().st_mode & 0o777, 0o600)

    def test_refresh_token_rotation_replaces_the_stored_refresh_token(self):
        self.write_credential(expires_at="2000-01-01T00:00:00Z")
        transport = FakeTransport(
            refresh=(
                200,
                {},
                json.dumps(
                    {
                        "access_token": "new-access-token",
                        "refresh_token": "rotated-refresh-token",
                        "expires_in": 3600,
                        "scope": " ".join(sorted(youtube_credentials.REQUIRED_SCOPES)),
                    }
                ).encode(),
            )
        )

        self.assertEqual(self.resolve(transport), "new-access-token")
        saved = json.loads(self.credential.read_text(encoding="utf-8"))
        self.assertEqual(saved["refresh_token"], "rotated-refresh-token")

    def test_revoked_refresh_is_stable_and_redacted(self):
        self.write_credential(expires_at="2000-01-01T00:00:00Z")
        canary = "refresh-token-must-never-escape"
        transport = FakeTransport(
            refresh=(400, {}, json.dumps({"error": "invalid_grant", "detail": canary}).encode())
        )

        with self.assertRaises(youtube_credentials.CredentialError) as raised:
            self.resolve(transport)

        self.assertEqual(raised.exception.code, "youtube_credential_revoked")
        self.assertNotIn(canary, str(raised.exception))

    def test_changed_scopes_are_rejected_before_provider_calls(self):
        self.write_credential(scopes=[youtube_credentials.UPLOAD_SCOPE])
        transport = FakeTransport()

        self.assert_code("youtube_credential_scope_invalid", lambda: self.resolve(transport))
        self.assertEqual(transport.calls, [])

    def test_wrong_authenticated_channel_is_rejected(self):
        transport = FakeTransport(
            channels=(200, {}, json.dumps({"items": [{"id": OTHER_CHANNEL}]}).encode())
        )

        self.assert_code("youtube_credential_channel_mismatch", lambda: self.resolve(transport))

    def test_wrong_logical_reference_is_rejected_before_store_read(self):
        self.assert_code(
            "youtube_credential_channel_mismatch",
            lambda: youtube_credentials.resolve_and_refresh(
                f"youtube:{OTHER_CHANNEL}", CHANNEL, FakeTransport()
            ),
        )

    def test_unsafe_directory_mode_is_rejected(self):
        self.channel_dir.chmod(0o755)
        self.assert_code("youtube_credential_store_unsafe", self.resolve)

    def test_unsafe_leaf_mode_is_rejected(self):
        self.credential.chmod(0o640)
        self.assert_code("youtube_credential_store_unsafe", self.resolve)

    def test_wrong_owner_is_rejected(self):
        with mock.patch.object(youtube_credentials.os, "getuid", return_value=-1):
            self.assert_code("youtube_credential_store_unsafe", self.resolve)

    def test_symlink_leaf_is_rejected_without_following_it(self):
        target = self.channel_dir / "outside.json"
        target.write_text(self.credential.read_text(encoding="utf-8"), encoding="utf-8")
        target.chmod(0o600)
        self.credential.unlink()
        self.credential.symlink_to(target.name)

        self.assert_code("youtube_credential_store_unsafe", self.resolve)

    def test_hardlinked_leaf_is_rejected(self):
        os.link(self.credential, self.channel_dir / "credential-copy.json")

        self.assert_code("youtube_credential_store_unsafe", self.resolve)

    def test_atomic_refresh_failure_preserves_existing_credential(self):
        self.write_credential(expires_at="2000-01-01T00:00:00Z")
        original = self.credential.read_bytes()
        transport = FakeTransport(
            refresh=(200, {}, b'{"access_token":"new-token","expires_in":3600}')
        )
        with mock.patch.object(youtube_credentials.os, "replace", side_effect=OSError):
            self.assert_code("youtube_credential_refresh_failed", lambda: self.resolve(transport))

        self.assertEqual(self.credential.read_bytes(), original)

    def test_recursive_provider_redaction_discards_canaries(self):
        canary = "oauth-body-canary"
        redacted = youtube_credentials._redacted_provider_error(
            {"error": {"details": [{"nested": canary}]}, "token": canary}
        )

        self.assertNotIn(canary, json.dumps(redacted))
        self.assertEqual(redacted, {"provider_error": "redacted"})


if __name__ == "__main__":
    unittest.main()
