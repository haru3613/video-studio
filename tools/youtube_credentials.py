#!/usr/bin/env python3
"""Protected, channel-bound OAuth credentials for the YouTube upload runtime.

This module deliberately owns neither publication nor video uploads.  It only
resolves a logical credential reference, refreshes it when necessary, and
proves the authenticated channel with ``channels.list(mine=true)``.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import http.server
import json
import os
import re
import secrets
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from pathlib import Path

CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
REFERENCE_PREFIX = "youtube:"
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
CHANNEL_LOOKUP_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
REQUIRED_SCOPES = frozenset((UPLOAD_SCOPE, CHANNEL_LOOKUP_SCOPE))
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels?part=id&mine=true"
CREDENTIAL_STORE_ROOT = (
    Path.home() / ".local/state/video-studio/secrets/youtube"
)


class CredentialError(Exception):
    """A stable error that intentionally carries no provider or filesystem data."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _redacted_provider_error(_value):
    """Discard every provider value, including recursively nested diagnostics."""
    return {"provider_error": "redacted"}


def _fail(code):
    raise CredentialError(code)


def _utc_now():
    return dt.datetime.now(dt.timezone.utc)


def _parse_expiry(value):
    if not isinstance(value, str):
        _fail("youtube_credential_invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("youtube_credential_invalid")
    if parsed.tzinfo is None:
        _fail("youtube_credential_invalid")
    return parsed.astimezone(dt.timezone.utc)


def _format_expiry(seconds):
    return (_utc_now() + dt.timedelta(seconds=seconds)).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")


def _channel_from_reference(reference):
    if not isinstance(reference, str) or not reference.startswith(REFERENCE_PREFIX):
        _fail("youtube_credential_reference_invalid")
    channel_id = reference[len(REFERENCE_PREFIX) :]
    if not CHANNEL_ID.fullmatch(channel_id):
        _fail("youtube_credential_reference_invalid")
    return channel_id


def credential_path(reference, store_root=None):
    """Return a path for display-free internal use; callers must not serialize it."""
    channel_id = _channel_from_reference(reference)
    root = CREDENTIAL_STORE_ROOT if store_root is None else Path(store_root)
    return root / channel_id / "credential.json"


def _is_protected_component(path):
    # The named store begins at ``secrets``.  Its directories must all be 0700.
    return path.name in {"secrets", "youtube"} or (
        path.parent.name == "youtube" and CHANNEL_ID.fullmatch(path.name) is not None
    )


def _open_store_directory(reference, *, create=False):
    """Open the channel directory through no-follow descriptors only.

    The returned descriptor owns the entire path walk.  This prevents a rename
    or symlink swap from changing what the subsequent credential read touches.
    """
    channel_id = _channel_from_reference(reference)
    root = Path(CREDENTIAL_STORE_ROOT)
    if not root.is_absolute():
        _fail("youtube_credential_store_unsafe")
    target = root / channel_id
    components = target.parts
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    current = Path("/")
    try:
        for component in components[1:]:
            current = current / component
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                if not create:
                    _fail("youtube_credential_unavailable")
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fd,
                    )
                except OSError:
                    _fail("youtube_credential_store_unsafe")
            except OSError:
                _fail("youtube_credential_store_unsafe")
            os.close(fd)
            fd = child
            try:
                metadata = os.fstat(fd)
            except OSError:
                _fail("youtube_credential_store_unsafe")
            if not stat.S_ISDIR(metadata.st_mode):
                _fail("youtube_credential_store_unsafe")
            if _is_protected_component(current) and (
                metadata.st_uid != os.getuid()
                or (metadata.st_mode & 0o777) != 0o700
            ):
                _fail("youtube_credential_store_unsafe")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _validate_private_file(fd):
    try:
        metadata = os.fstat(fd)
    except OSError:
        _fail("youtube_credential_store_unsafe")
    if (
        metadata.st_uid != os.getuid()
        or not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_mode & 0o777) != 0o600
        or metadata.st_nlink != 1
    ):
        _fail("youtube_credential_store_unsafe")


def _read_credential(directory_fd):
    try:
        credential_fd = os.open(
            "credential.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
        )
    except FileNotFoundError:
        _fail("youtube_credential_unavailable")
    except OSError:
        _fail("youtube_credential_store_unsafe")
    try:
        _validate_private_file(credential_fd)
        with os.fdopen(credential_fd, "rb", closefd=False) as handle:
            raw = handle.read()
    except CredentialError:
        raise
    except OSError:
        _fail("youtube_credential_unavailable")
    finally:
        os.close(credential_fd)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("youtube_credential_invalid")
    if not isinstance(value, dict):
        _fail("youtube_credential_invalid")
    return value


def _validate_scopes(scopes):
    if (
        not isinstance(scopes, list)
        or len(scopes) != len(REQUIRED_SCOPES)
        or not all(isinstance(scope, str) for scope in scopes)
        or set(scopes) != REQUIRED_SCOPES
    ):
        _fail("youtube_credential_scope_invalid")


def _validate_credential(value, expected_channel_id):
    if not isinstance(expected_channel_id, str) or not CHANNEL_ID.fullmatch(expected_channel_id):
        _fail("youtube_credential_reference_invalid")
    if (
        value.get("format_version") != 1
        or value.get("type") != "youtube_oauth_refresh_credential"
        or value.get("channel_id") != expected_channel_id
    ):
        _fail("youtube_credential_invalid")
    _validate_scopes(value.get("scopes"))
    for key in ("client_id", "client_secret", "refresh_token", "access_token"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            _fail("youtube_credential_invalid")
    expiry = _parse_expiry(value.get("expires_at"))
    return expiry


def _open_lock(directory_fd):
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    try:
        fd = os.open("credential.lock", flags, 0o600, dir_fd=directory_fd)
    except OSError:
        _fail("youtube_credential_store_unsafe")
    try:
        _validate_private_file(fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _request(transport, method, url, *, headers=None, body=None):
    try:
        if transport is None:
            return _http_request(method, url, headers=headers, body=body)
        if callable(transport):
            return transport(method, url, headers=headers, body=body)
        return transport.request(method, url, headers=headers, body=body)
    except CredentialError:
        raise
    except Exception:
        _fail("youtube_credential_provider_unavailable")


def _http_request(method, url, *, headers=None, body=None):
    request = urllib.request.Request(
        url, data=body, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read()
    except (OSError, urllib.error.URLError):
        _fail("youtube_credential_provider_unavailable")


def _json_response(status, body, *, error_code):
    if not isinstance(status, int) or not isinstance(body, (bytes, bytearray)):
        _fail(error_code)
    if not 200 <= status < 300:
        _fail(error_code)
    try:
        value = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(error_code)
    if not isinstance(value, dict):
        _fail(error_code)
    return value


def _refresh_credential(value, transport):
    body = urllib.parse.urlencode(
        {
            "client_id": value["client_id"],
            "client_secret": value["client_secret"],
            "refresh_token": value["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode()
    try:
        status, _headers, response_body = _request(
            transport,
            "POST",
            TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=body,
        )
    except CredentialError:
        raise
    if status == 400:
        # OAuth's invalid_grant is intentionally not surfaced; invalid/revoked
        # refresh state always has the same operator-facing remedy.
        _fail("youtube_credential_revoked")
    response = _json_response(status, response_body, error_code="youtube_credential_refresh_failed")
    access_token = response.get("access_token")
    expires_in = response.get("expires_in")
    if (
        not isinstance(access_token, str)
        or not access_token.strip()
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or expires_in <= 0
    ):
        _fail("youtube_credential_refresh_failed")
    returned_scope = response.get("scope")
    if returned_scope is not None:
        if not isinstance(returned_scope, str):
            _fail("youtube_credential_scope_invalid")
        _validate_scopes(returned_scope.split())
    refreshed = dict(value)
    rotated_refresh_token = response.get("refresh_token")
    if rotated_refresh_token is not None:
        if not isinstance(rotated_refresh_token, str) or not rotated_refresh_token.strip():
            _fail("youtube_credential_refresh_failed")
        refreshed["refresh_token"] = rotated_refresh_token
    refreshed["access_token"] = access_token
    refreshed["expires_at"] = _format_expiry(expires_in)
    return refreshed


def _atomic_replace(directory_fd, value):
    temporary = f".credential.{uuid.uuid4().hex}.tmp"
    fd = None
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(fd, 0o600)
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _validate_private_file(fd)
        os.replace(
            temporary,
            "credential.json",
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except CredentialError:
        raise
    except OSError:
        _fail("youtube_credential_refresh_failed")
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _authenticated_channel(access_token, expected_channel_id, transport):
    status, _headers, body = _request(
        transport,
        "GET",
        CHANNELS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if status in {401, 403}:
        _fail("youtube_credential_revoked")
    response = _json_response(status, body, error_code="youtube_credential_channel_lookup_failed")
    items = response.get("items")
    if not isinstance(items, list):
        _fail("youtube_credential_channel_lookup_failed")
    channel_ids = [item.get("id") for item in items if isinstance(item, dict)]
    if channel_ids != [expected_channel_id]:
        _fail("youtube_credential_channel_mismatch")


def resolve_and_refresh(reference, expected_channel_id, transport=None):
    """Resolve one logical credential reference to a verified access token.

    The token is never persisted or logged by this function's callers.  Any
    refresh happens under an exclusive lock and atomically replaces the leaf
    only after the new value has been fsynced.
    """
    channel_id = _channel_from_reference(reference)
    if channel_id != expected_channel_id:
        _fail("youtube_credential_channel_mismatch")
    directory_fd = _open_store_directory(reference)
    try:
        lock_fd = _open_lock(directory_fd)
        try:
            credential = _read_credential(directory_fd)
            expiry = _validate_credential(credential, expected_channel_id)
            if expiry <= _utc_now() + dt.timedelta(seconds=60):
                credential = _refresh_credential(credential, transport)
                _atomic_replace(directory_fd, credential)
            access_token = credential["access_token"]
            _authenticated_channel(access_token, expected_channel_id, transport)
            return access_token
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    finally:
        os.close(directory_fd)


def _load_client_config(path):
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _fail("youtube_credential_client_config_invalid")
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        _fail("youtube_credential_client_config_invalid")
    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret")
    auth_uri = installed.get("auth_uri", AUTHORIZE_URL)
    token_uri = installed.get("token_uri", TOKEN_URL)
    if (
        not isinstance(client_id, str)
        or not client_id
        or not isinstance(client_secret, str)
        or not client_secret
        # Google still puts this legacy URL in downloaded Desktop-client JSON.
        # Authorization itself always uses our fixed v2 URL and PKCE below.
        or auth_uri not in (AUTHORIZE_URL, "https://accounts.google.com/o/oauth2/auth")
        or token_uri != TOKEN_URL
    ):
        _fail("youtube_credential_client_config_invalid")
    return client_id, client_secret


def _pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _receive_authorization_code(authorization_url, state):
    received = {}
    event = threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - HTTP handler API
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            received["code"] = query.get("code", [None])[0]
            received["state"] = query.get("state", [None])[0]
            received["error"] = query.get("error", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorization received. You may close this tab.")
            event.set()

        def log_message(self, _format, *_args):
            return

    with http.server.HTTPServer(("127.0.0.1", 0), Callback) as server:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2/callback"
        url = authorization_url(redirect_uri)
        print("Opening the Google authorization page in your default browser.")
        print("Complete consent in that browser. This command never uploads a video.")
        webbrowser.open(url, new=1)
        deadline = time.monotonic() + 300
        while not event.is_set() and time.monotonic() < deadline:
            server.timeout = 1
            server.handle_request()
    if received.get("state") != state or not isinstance(received.get("code"), str):
        _fail("youtube_credential_authorization_failed")
    return received["code"], redirect_uri


def provision(expected_channel_id, client_config, transport=None):
    """Perform browser-mediated desktop OAuth and save a channel-bound credential."""
    if not isinstance(expected_channel_id, str) or not CHANNEL_ID.fullmatch(expected_channel_id):
        _fail("youtube_credential_reference_invalid")
    client_id, client_secret = _load_client_config(client_config)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()

    def authorization_url(redirect_uri):
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(sorted(REQUIRED_SCOPES)),
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )

    code, redirect_uri = _receive_authorization_code(authorization_url, state)
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
    ).encode()
    status, _headers, response_body = _request(
        transport,
        "POST",
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=body,
    )
    response = _json_response(status, response_body, error_code="youtube_credential_authorization_failed")
    access_token = response.get("access_token")
    refresh_token = response.get("refresh_token")
    expires_in = response.get("expires_in")
    scope = response.get("scope")
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(refresh_token, str)
        or not refresh_token
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or expires_in <= 0
        or not isinstance(scope, str)
    ):
        _fail("youtube_credential_authorization_failed")
    _validate_scopes(scope.split())
    _authenticated_channel(access_token, expected_channel_id, transport)
    credential = {
        "format_version": 1,
        "type": "youtube_oauth_refresh_credential",
        "channel_id": expected_channel_id,
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "access_token": access_token,
        "expires_at": _format_expiry(expires_in),
        "scopes": sorted(REQUIRED_SCOPES),
    }
    reference = REFERENCE_PREFIX + expected_channel_id
    directory_fd = _open_store_directory(reference, create=True)
    try:
        lock_fd = _open_lock(directory_fd)
        try:
            _atomic_replace(directory_fd, credential)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    finally:
        os.close(directory_fd)


def _canonical_channel_id(repo_root):
    try:
        manifest = json.loads(
            (Path(repo_root) / "pipeline/runtime-manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _fail("youtube_credential_canonical_channel_invalid")
    channel_id = manifest.get("youtube_channel_id") if isinstance(manifest, dict) else None
    if not isinstance(channel_id, str) or not CHANNEL_ID.fullmatch(channel_id):
        _fail("youtube_credential_canonical_channel_invalid")
    return channel_id


def main(argv=None):
    parser = argparse.ArgumentParser(description="Provision the protected YouTube OAuth credential.")
    parser.add_argument("provision", nargs="?", choices=("provision",))
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--client-config")
    args = parser.parse_args(argv)
    if args.provision != "provision":
        parser.error("the provision command is required")
    client_config = args.client_config or os.environ.get("YOUTUBE_OAUTH_CLIENT_FILE")
    if not client_config:
        _fail("youtube_credential_client_config_invalid")
    provision(_canonical_channel_id(args.repo_root), client_config)
    print("YouTube credential provisioned and channel verified.")


if __name__ == "__main__":
    try:
        main()
    except CredentialError as error:
        print(error.code, file=os.sys.stderr)
        raise SystemExit(1)
