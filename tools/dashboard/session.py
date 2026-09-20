"""Owner-initiated loopback UI sessions; not publication authorization."""
from __future__ import annotations

import secrets
import time
from collections import deque

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

COOKIE = "video_studio_session"
LOGIN = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Video Studio · Sign in</title><style>
body{background:#161719;color:#eee;font:18px system-ui;margin:10vh auto;max-width:480px;padding:24px}
input,button{font:inherit;padding:12px;box-sizing:border-box;width:100%;margin:8px 0}
p{line-height:1.5;color:#bbb}</style><h1>Video Studio</h1>
<p>Enter the one-time code from your local UI code file.</p>
<form id="login"><label for="code">Session code</label><input id="code" type="password"
autocomplete="off" required><button>Open studio</button></form><p id="error" role="alert"></p>
<script>document.querySelector('form').onsubmit=async(e)=>{e.preventDefault();
const r=await fetch('/session',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({code:document.querySelector('#code').value})});
if(r.ok)location.replace('/');else document.querySelector('#error').textContent='Invalid or expired code. Restart the local UI to obtain a new code.';};</script></html>"""


class SessionAuthority:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.code = secrets.token_urlsafe(32)
        self.code_expires = clock() + 300
        self.sessions: dict[str, float] = {}
        self.attempts: deque[float] = deque()

    def exchange(self, supplied: str) -> str | None:
        now = self.clock()
        while self.attempts and self.attempts[0] <= now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= 5:
            return None
        self.attempts.append(now)
        if not self.code or now >= self.code_expires or not secrets.compare_digest(supplied, self.code):
            return None
        self.code = ""
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + 8 * 3600
        return token

    def valid(self, token: str | None) -> bool:
        now = self.clock()
        self.sessions = {key: deadline for key, deadline in self.sessions.items() if deadline > now}
        return bool(token and token in self.sessions)


class SessionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, authority: SessionAuthority):
        super().__init__(app)
        self.authority = authority

    async def dispatch(self, request: Request, call_next):
        if request.url.path not in {"/login", "/session"} and not self.authority.valid(request.cookies.get(COOKIE)):
            if request.url.path == "/":
                return RedirectResponse("/login", status_code=303)
            return JSONResponse({"error": "session_required"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response


def install_sessions(app, authority: SessionAuthority):
    app.add_middleware(SessionMiddleware, authority=authority)

    @app.get("/login")
    def login():
        return HTMLResponse(LOGIN)

    @app.post("/session")
    async def exchange(request: Request):
        expected_origin = str(request.base_url).rstrip("/")
        if request.headers.get("origin") != expected_origin:
            return JSONResponse({"error": "origin_forbidden"}, status_code=403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024:
                return JSONResponse({"error": "invalid_request"}, status_code=413)
        import json
        try:
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            value = None
        if not isinstance(value, dict) or set(value) != {"code"} or not isinstance(value["code"], str):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        token = authority.exchange(value["code"])
        if token is None:
            return JSONResponse({"error": "invalid_code"}, status_code=401)
        response = JSONResponse({"status": "authenticated"})
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=8 * 3600)
        return response
