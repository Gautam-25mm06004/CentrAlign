"""Shared plumbing for the sandbox business apps.

Provides: sessions + CSRF, login pages, the protected-write validator (capability +
idempotency + atomic save), the trusted read-only API guard and test-only fault switches.
The business workflows themselves live in each app; nothing here performs work for the agent.
"""
from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import time
from functools import wraps
from html import escape
from urllib.parse import quote
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from shared import capability
from shared.config import load_secrets

SESSION_COOKIE = "sbx_session"


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


IDEMPOTENCY_DDL = """
CREATE TABLE IF NOT EXISTS idempotency (
    idem_key TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Faults:
    """Test-only failure switches, controlled by the evaluation harness with the fault key."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.slow_ms = 0
        self.error_before_commit = 0
        self.drop_after_commit = 0
        self.expire_sessions_after = -1
        self.rename_labels = False
        self.corrupt_next_save = 0

    def as_dict(self):
        return dict(self.__dict__)


class SandboxApp:
    def __init__(self, company: str, origin: str, db_path: Path, title: str):
        self.company = company
        self.origin = origin
        self.db_path = db_path
        self.title = title
        self.sessions: dict[str, dict] = {}
        self.faults = Faults()
        self._request_count = 0

    # ---------- infrastructure ----------
    @property
    def secrets(self) -> dict:
        return load_secrets()

    def db(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def session(self, request: Request) -> dict | None:
        sid = request.cookies.get(SESSION_COOKIE)
        return self.sessions.get(sid) if sid else None

    def tick(self):
        """Called on each authenticated request; may expire sessions (fault switch)."""
        self._request_count += 1
        if self.faults.expire_sessions_after > 0:
            self.faults.expire_sessions_after -= 1
            if self.faults.expire_sessions_after == 0:
                self.sessions.clear()
                self.faults.expire_sessions_after = -1

    def slow(self):
        if self.faults.slow_ms:
            time.sleep(self.faults.slow_ms / 1000)

    # ---------- login ----------
    def login_page(self, request: Request, error: str = "") -> HTMLResponse:
        nxt = request.query_params.get("next", "/")
        return HTMLResponse(LOGIN_HTML.format(title=self.title, error=error, next=escape(nxt),
                                              css=self.login_css()))

    def login_css(self) -> str:
        return ""

    async def login_submit(self, request: Request):
        form = await request.form()
        creds = self.secrets["credentials"][self.company]
        ok = hmac.compare_digest(str(form.get("username", "")), creds["username"]) and \
            hmac.compare_digest(str(form.get("password", "")), creds["password"])
        if not ok:
            return self.login_page(request, error="Invalid username or password")
        sid = secrets.token_urlsafe(16)
        self.sessions[sid] = {"user": creds["username"], "csrf": secrets.token_urlsafe(16),
                              "created": time.time()}
        nxt = str(form.get("next") or "/")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(SESSION_COOKIE, sid, httponly=True, samesite="lax")
        return resp

    def logout(self, request: Request):
        sid = request.cookies.get(SESSION_COOKIE)
        self.sessions.pop(sid, None)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(SESSION_COOKIE)
        return resp

    def require_login(self, fn):
        @wraps(fn)
        async def wrapper(request: Request):
            self.slow()
            sess = self.session(request)
            if not sess:
                path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                if request.method != "GET":
                    return HTMLResponse(SESSION_EXPIRED_HTML, status_code=401)
                return RedirectResponse(f"/login?next={quote(path, safe='')}", status_code=303)
            self.tick()
            if not self.session(request):
                return HTMLResponse(SESSION_EXPIRED_HTML, status_code=401)
            request.state.sess = sess
            return await fn(request)
        return wrapper

    # ---------- trusted read-only API ----------
    def require_trusted(self, fn):
        @wraps(fn)
        async def wrapper(request: Request):
            key = request.headers.get("X-Trusted-Key", "")
            if not hmac.compare_digest(key, self.secrets["trusted_key"]):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            return await fn(request)
        return wrapper

    # ---------- faults ----------
    async def faults_endpoint(self, request: Request):
        if not hmac.compare_digest(request.headers.get("X-Fault-Key", ""), self.secrets["fault_key"]):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if request.method == "POST":
            body = await request.json()
            if body.get("reset"):
                self.faults.reset()
                self.sessions.clear()
            for k, v in body.items():
                if k != "reset" and k in self.faults.__dict__:
                    setattr(self.faults, k, v)
        return JSONResponse(self.faults.as_dict())

    # ---------- protected writes ----------
    async def protected_write(self, request: Request, *, action: str, fields: dict, commit):
        """Validate capability + CSRF + idempotency, then atomically commit.

        `commit(conn) -> dict` performs the business write inside the open transaction and
        returns a result dict. It must raise BusinessReject for live-state violations.
        """
        sess = request.state.sess
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf_token", "")), sess["csrf"]):
            return self.result_page("Rejected", "Form expired (CSRF check failed). Reload the page.", 403)
        try:
            cap = capability.verify(
                self.secrets["capability_key"], request.headers.get(capability.HEADER),
                audience=self.origin, method=request.method, route=request.url.path,
                action=action, fields=fields)
        except capability.CapabilityError as e:
            return self.result_page("Rejected by authorization check", str(e), 403)
        digest = capability.payload_digest(fields)
        conn = self.db()
        try:
            prior = conn.execute("SELECT * FROM idempotency WHERE idem_key=?", (cap["idem"],)).fetchone()
            if prior:
                if prior["digest"] != digest or prior["action"] != action:
                    return self.result_page("Rejected", "Idempotency key reused with different data.", 409)
                res = json.loads(prior["result_json"])
                return self.result_page("Already recorded", res.get("message", "This operation was already saved."),
                                        200, res)
            if self.faults.error_before_commit > 0:
                self.faults.error_before_commit -= 1
                return self.result_page("Service unavailable", "Temporary error. Nothing was saved.", 503)
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = commit(conn)
                conn.execute("INSERT INTO idempotency VALUES (?,?,?,?,?)",
                             (cap["idem"], action, digest, json.dumps(result), time.time()))
                conn.execute("COMMIT")
            except BusinessReject as e:
                conn.execute("ROLLBACK")
                return self.result_page("Rejected", str(e), 422)
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        if self.faults.drop_after_commit > 0:
            self.faults.drop_after_commit -= 1
            return HTMLResponse("<h1>502 Bad Gateway</h1>", status_code=502)
        return self.result_page("Saved", result.get("message", "Saved."), 200, result)

    def result_page(self, heading: str, message: str, status: int, result: dict | None = None):
        ref = (result or {}).get("ref", "")
        html = RESULT_HTML.format(heading=heading, message=message,
                                  ref=f"<p>Reference: <strong id='result-ref'>{ref}</strong></p>" if ref else "",
                                  status_class="ok" if status < 300 else "err")
        return HTMLResponse(html, status_code=status)


class BusinessReject(Exception):
    pass


LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Sign in — {title}</title>
<style>body{{font-family:system-ui,sans-serif;background:#f3f4f6;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}}
form{{background:#fff;padding:32px;border-radius:10px;box-shadow:0 2px 12px #0001;width:320px}}
label{{display:block;margin:12px 0 4px;font-size:14px}}input{{width:100%;padding:8px;box-sizing:border-box}}
button{{margin-top:18px;width:100%;padding:10px;border:0;border-radius:6px;background:#1f2937;color:#fff}}
.err{{color:#b91c1c;font-size:14px}}{css}</style></head>
<body><form method="post" action="/login"><h2>{title}</h2><p class="err">{error}</p>
<input type="hidden" name="next" value="{next}">
<label for="u">Username</label><input id="u" name="username" autocomplete="username">
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password">
<button type="submit">Sign in</button></form></body></html>"""

SESSION_EXPIRED_HTML = """<!doctype html><html><body><h1>Session expired</h1>
<p>Your session has expired. Please <a href="/login">sign in</a> again.</p></body></html>"""

RESULT_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>{heading}</title>
<style>body{{font-family:system-ui,sans-serif;padding:40px}}.ok{{color:#166534}}.err{{color:#b91c1c}}</style></head>
<body><h1 class="{status_class}">{heading}</h1><p>{message}</p>{ref}<p><a href="/">Back to home</a></p></body></html>"""
