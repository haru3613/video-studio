from pathlib import Path

from fastapi.testclient import TestClient

import server
from session import SessionAuthority


def test_ui_authentication_binds_session_and_consumes_code(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    authority = SessionAuthority()
    code = authority.code
    app = server.create_app({"studio": root}, session_authority=authority)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/projects").status_code == 401
        assert client.post("/session", json={"code": code}, headers={"Origin": "https://evil.invalid"}).status_code == 403
        response = client.post("/session", json={"code": code}, headers={"Origin": "http://localhost"})
        assert response.status_code == 200
        assert "httponly" in response.headers["set-cookie"].lower()
        assert client.get("/api/projects").status_code == 200
        assert client.post("/session", json={"code": code}, headers={"Origin": "http://localhost"}).status_code == 401
        assert client.get("/api/projects", headers={"Host": "evil.invalid"}).status_code == 400


def test_ui_code_and_session_expire():
    now = [0.0]
    authority = SessionAuthority(clock=lambda: now[0])
    now[0] = 301
    assert authority.exchange(authority.code) is None
    authority = SessionAuthority(clock=lambda: now[0])
    token = authority.exchange(authority.code)
    assert authority.valid(token)
    now[0] += 8 * 3600
    assert not authority.valid(token)


def test_public_default_roots_do_not_scan_operator_private_media(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VIDEO_STUDIO_WORKSPACE", raising=False)
    monkeypatch.delenv("VIDEO_STUDIO_ARCHIVE_ROOT", raising=False)
    assert server.default_roots() == {"studio": tmp_path / "VideoStudio/projects"}
