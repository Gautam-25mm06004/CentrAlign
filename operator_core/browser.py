"""Controlled browser: bounded observations with stale-safe element refs, generic actions,
and a context-wide request guard that enforces destinations and protected writes."""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlparse

from playwright.sync_api import sync_playwright

from shared import capability
from shared.config import app_origin, load_secrets

OBSERVE_JS = r"""
(obsN) => {
  document.querySelectorAll('[data-op-ref]').forEach(e => e.removeAttribute('data-op-ref'));
  const visible = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const labelOf = e => {
    if (e.getAttribute('aria-label')) return e.getAttribute('aria-label');
    if (e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) return l.innerText.trim(); }
    const pl = e.closest('label'); if (pl) return pl.innerText.trim();
    if (e.placeholder) return e.placeholder;
    if (e.tagName === 'A' || e.tagName === 'BUTTON') return e.innerText.trim();
    return e.name || '';
  };
  const els = [...document.querySelectorAll('a[href],button,input,select,textarea')]
    .filter(e => e.type !== 'hidden' && visible(e));
  const out = []; let i = 0;
  for (const e of els) {
    i++; const ref = `${obsN}.${i}`; e.setAttribute('data-op-ref', ref);
    const kind = e.tagName === 'A' ? 'link' : (e.tagName === 'BUTTON' || e.type === 'submit') ? 'button'
      : e.tagName === 'SELECT' ? 'select' : e.tagName === 'TEXTAREA' ? 'textarea' : 'textbox';
    const item = {ref, kind, name: e.name || '', label: labelOf(e).replace(/\s+/g, ' ').slice(0, 90)};
    if (kind === 'link') item.href = e.getAttribute('href');
    if (kind === 'textbox' || kind === 'textarea') item.value = e.type === 'password' ? (e.value ? '(hidden)' : '') : e.value;
    if (kind === 'select') { item.value = e.value;
      item.options = [...e.options].filter(o => o.value).map(o => `${o.value}: ${o.text.trim()}`); }
    const form = e.closest('form');
    if (form) item.form = (form.querySelector('h3,h2')?.innerText || form.getAttribute('action') || '').trim();
    out.push(item);
  }
  const tables = [...document.querySelectorAll('table')].map(t =>
    [...t.rows].map(r => [...r.cells].map(c => c.innerText.trim().replace(/\s+/g, ' ')).join(' | ')));
  const main = document.querySelector('main') || document.body;
  const clone = main.cloneNode(true);
  clone.querySelectorAll('table,form,style,script').forEach(n => n.remove());
  const text = clone.innerText.replace(/\n\s*\n+/g, '\n').trim();
  return {url: location.pathname + location.search, title: document.title, text, tables, elements: out};
}
"""

MAX_ELEMENTS = 60
MAX_ROWS = 20
MAX_TEXT = 1500


class ToolError(Exception):
    pass


class Browser:
    def __init__(self, profile: dict, run_dir: Path, gate, headless: bool = True):
        self.profile = profile
        self.company = profile["app"]
        self.origin = app_origin(self.company)
        self.run_dir = run_dir
        self.gate = gate  # authorization gate (operator_core.authz.Gate)
        self.headless = headless
        self.obs_n = 0
        self.last_obs: dict | None = None
        self.notices: list[str] = []
        self.shot_n = 0
        self.protected = [(r["method"].upper(), re.compile(r["pattern"]), r["action"])
                          for r in profile["protected_routes"]]

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.ctx = self._browser.new_context(service_workers="block", viewport={"width": 1280, "height": 900})
        self.ctx.route("**/*", self._route)  # registered before any page exists; covers new tabs
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(15000)
        self.page.goto(self.origin + self.profile["start_path"])

    def close(self):
        try:
            self._browser.close()
            self._pw.stop()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ request guard
    def _fulfill_block(self, route, message: str, status: int = 403):
        route.fulfill(status=status, content_type="text/html",
                      body=f"<html><body><h1>Blocked by operator gate</h1><p id='gate-msg'>{message}</p></body></html>")

    def _route(self, route, request):
        url = urlparse(request.url)
        if url.scheme in ("data", "blob", "about"):
            return route.continue_()
        origin = f"{url.scheme}://{url.netloc}"
        if origin != self.origin:
            self.notices.append(f"Blocked request to non-allowed destination {origin}")
            return route.abort("blockedbyclient")
        path = url.path
        if any(path.startswith(p) for p in self.profile.get("blocked_path_prefixes", [])):
            self.notices.append(f"Blocked request to restricted path {path}")
            return self._fulfill_block(route, "This path is not available to the operator.")
        headers = {k: v for k, v in request.headers.items() if k.lower() != capability.HEADER.lower()}
        method = request.method.upper()
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            if path == self.profile["login"]["path"] and method == "POST":
                return route.continue_(headers=headers)
            for m, pat, action in self.protected:
                if m == method and pat.match(path):
                    return self.gate.guard_request(route, request, action, path, headers)
            self.notices.append(f"Blocked unmapped state-changing request {method} {path}")
            return self._fulfill_block(route, f"Unmapped write route {method} {path} is not supported.")
        if method != "GET":
            return self._fulfill_block(route, f"Method {method} not supported.")
        return route.continue_(headers=headers)

    # ------------------------------------------------------------ observation
    def observe(self, page_no: int = 1) -> str:
        self.obs_n += 1
        try:
            self.page.wait_for_load_state("load", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        data = self.page.evaluate(OBSERVE_JS, self.obs_n)
        self.last_obs = data
        self._screenshot_latest()
        return self.format_obs(data, page_no)

    def format_obs(self, d: dict, page_no: int = 1) -> str:
        lines = [f"[observation {self.obs_n}] {d['title']} — URL {d['url']}"]
        if d["url"].startswith("/login") or "Session expired" in d["text"]:
            lines.append("NOTICE: you are not signed in (session missing/expired). Call sign_in.")
        text = d["text"]
        if text:
            lines.append("Text: " + text[:MAX_TEXT] + (" …[text truncated]" if len(text) > MAX_TEXT else ""))
        start = (page_no - 1) * MAX_ROWS
        for ti, rows in enumerate(d["tables"]):
            if not rows:
                continue
            body = rows[1:]
            shown = body[start:start + MAX_ROWS]
            lines.append(f"Table {ti + 1}: {rows[0]}")
            lines += [f"  {r}" for r in shown]
            if len(body) > start + MAX_ROWS:
                lines.append(f"  …{len(body) - start - MAX_ROWS} more rows (call observe with page={page_no + 1})")
        els = d["elements"]
        e_start = (page_no - 1) * MAX_ELEMENTS
        lines.append("Elements:")
        for e in els[e_start:e_start + MAX_ELEMENTS]:
            s = f"  [{e['ref']}] {e['kind']} \"{e['label']}\""
            if e.get("form"):
                s += f" (form: {e['form']})"
            if e.get("href"):
                s += f" -> {e['href']}"
            if "value" in e and e["value"] not in (None, ""):
                s += f" value={e['value']!r}"
            if e.get("options"):
                opts = e["options"]
                s += " options=[" + "; ".join(opts[:20]) + (" …" if len(opts) > 20 else "") + "]"
            lines.append(s)
        if len(els) > e_start + MAX_ELEMENTS:
            lines.append(f"  …{len(els) - e_start - MAX_ELEMENTS} more elements (observe page={page_no + 1})")
        return "\n".join(lines)

    def _screenshot_latest(self):
        try:
            self.page.screenshot(path=str(self.run_dir / "latest.jpg"), type="jpeg", quality=60)
        except Exception:  # noqa: BLE001
            pass

    def screenshot(self, label: str) -> str:
        self.shot_n += 1
        shots = self.run_dir / "shots"
        shots.mkdir(exist_ok=True)
        name = f"{self.shot_n:03d}-{re.sub(r'[^a-z0-9]+', '-', label.lower())[:40]}.png"
        try:
            self.page.screenshot(path=str(shots / name), full_page=True)
        except Exception:  # noqa: BLE001
            return ""
        return f"shots/{name}"

    # ------------------------------------------------------------ actions
    def _loc(self, ref: str):
        m = re.fullmatch(r"(\d+)\.(\d+)", str(ref).strip())
        if not m:
            raise ToolError(f"Invalid element ref {ref!r}; use refs like '{self.obs_n}.3' from the latest observation.")
        if int(m.group(1)) != self.obs_n:
            raise ToolError(f"Stale ref {ref}: it belongs to observation {m.group(1)}, the current observation is "
                            f"{self.obs_n}. Use refs from the latest observation.")
        loc = self.page.locator(f'[data-op-ref="{ref}"]')
        if loc.count() != 1:
            raise ToolError(f"Element {ref} is no longer on the page. Observe again.")
        return loc

    def check_path(self, path: str) -> str:
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise ToolError("Give a path within the company application, e.g. /vendors")
        p = urlparse(path).path
        allowed = self.profile["allowed_paths"]
        if not any(p == a or (a != "/" and p.startswith(a + "/")) or (a != "/" and p.startswith(a + "?"))
                   for a in allowed):
            raise ToolError(f"Path {p} is outside the allowed application areas: {allowed}")
        return path

    def open(self, path: str) -> str:
        self.page.goto(self.origin + self.check_path(path))
        return self.observe()

    def click(self, ref: str) -> str:
        loc = self._loc(ref)
        self.gate.last_write = None
        loc.click()
        time.sleep(0.2)
        try:
            self.page.wait_for_load_state("load", timeout=10000)
        except Exception:  # noqa: BLE001
            pass
        return self.observe()

    def fill(self, fields: list[dict]) -> str:
        done = []
        for f in fields:
            loc = self._loc(f["ref"])
            tag = loc.evaluate("e => e.tagName")
            val = "" if f.get("value") is None else str(f["value"])
            if tag == "SELECT":
                opts = loc.evaluate("e => [...e.options].map(o => [o.value, o.text.trim()])")
                choice = next((v for v, t in opts if v == val), None) or \
                    next((v for v, t in opts if t == val), None) or \
                    next((v for v, t in opts if val and val.lower() in t.lower()), None)
                if choice is None:
                    raise ToolError(f"Option {val!r} not found for {f['ref']}. Options: {[v for v, _ in opts if v]}")
                loc.select_option(choice)
                done.append(f"{f['ref']}={choice}")
            else:
                loc.fill(val)
                done.append(f"{f['ref']} filled")
        self._screenshot_latest()
        return "Filled: " + ", ".join(done) + " (not submitted). Refs remain valid until the next observation."

    def sign_in(self) -> str:
        u = urlparse(self.page.url)
        if u.path.startswith(self.profile["login"]["path"]):
            nxt = dict(parse_qsl(u.query)).get("next") or self.profile["start_path"]
        else:
            nxt = u.path + (f"?{u.query}" if u.query else "")
        self.page.goto(f"{self.origin}{self.profile['login']['path']}?next={quote(nxt, safe='')}")
        creds = load_secrets()["credentials"][self.company]
        self.page.fill(f"input[name={self.profile['login']['username_field']}]", creds["username"])
        self.page.fill(f"input[name={self.profile['login']['password_field']}]", creds["password"])
        self.page.click("button[type=submit]")
        self.page.wait_for_load_state("load")
        return "Signed in with the credential provider (credentials not shown).\n" + self.observe()

    def pop_notices(self) -> list[str]:
        n, self.notices = self.notices, []
        return n
