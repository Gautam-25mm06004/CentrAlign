"""Persistent run state: runs, ordered events, proposals, human decisions, authorized
actions, facts, model-call ledger and verification results (SQLite, WAL).

The agent process and the console share this database. Everything needed to explain
or resume a run lives here; nothing is held only in memory.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from shared.config import DATA

DB_PATH = DATA / "operator.db"

DDL = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, company TEXT, task TEXT, status TEXT, mode TEXT, model TEXT,
  model_kind TEXT, human_policy TEXT, scope_json TEXT, plan_json TEXT, summary_json TEXT,
  created_at REAL, updated_at REAL, label TEXT, faults_json TEXT);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, case_id TEXT, type TEXT, actor TEXT,
  ts REAL, payload_json TEXT);
CREATE INDEX IF NOT EXISTS ev_run ON events(run_id, seq);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, run_id TEXT, case_id TEXT, version INTEGER, source TEXT, action TEXT,
  values_json TEXT, rationale TEXT, evidence_json TEXT, checker_json TEXT, comparison_json TEXT,
  status TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, run_id TEXT, case_id TEXT, kind TEXT, status TEXT, request_json TEXT,
  response_json TEXT, created_at REAL, resolved_at REAL);
CREATE TABLE IF NOT EXISTS authorized_actions (
  id TEXT PRIMARY KEY, run_id TEXT, case_id TEXT, proposal_id TEXT, seq INTEGER, action_type TEXT,
  method TEXT, route TEXT, fields_json TEXT, digest TEXT, idem_key TEXT, status TEXT,
  result_json TEXT, created_at REAL, expires_at REAL);
CREATE TABLE IF NOT EXISTS facts (
  run_id TEXT, case_id TEXT, key TEXT, value TEXT, source TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, status TEXT, kind TEXT,
  input_tokens INTEGER, output_tokens INTEGER, thinking_tokens INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS verifications (
  run_id TEXT, case_id TEXT, result_json TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS case_controls (
  run_id TEXT, case_id TEXT, state TEXT, actor TEXT, reason TEXT, ts REAL);
"""

# columns added after the first release (migrated in place on existing databases)
MIGRATIONS = {
    "runs": {"read_only": "INTEGER DEFAULT 0", "call_cap": "INTEGER", "active_s": "REAL DEFAULT 0"},
    "authorized_actions": {"basis_json": "TEXT"},
}


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    with db() as c:
        c.executescript(DDL)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


@contextmanager
def db():
    c = connect()
    try:
        yield c
    finally:
        c.close()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def j(x) -> str:
    return json.dumps(x, default=str)


def unj(s):
    return json.loads(s) if s else None


# ---------------- runs ----------------
def create_run(company: str, task: str, model: str, model_kind: str, human_policy: str, label: str = "",
               faults: dict | None = None, read_only: bool | None = None, call_cap: int | None = None) -> str:
    """read_only=None means: decide from the wording of the task (see operator_core.taskscope)."""
    from operator_core.taskscope import looks_read_only
    rid = new_id("run")
    now = time.time()
    ro = looks_read_only(task) if read_only is None else bool(read_only)
    with db() as c:
        c.execute("INSERT INTO runs (id, company, task, status, mode, model, model_kind, human_policy, created_at,"
                  " updated_at, label, faults_json, read_only, call_cap, active_s)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                  (rid, company, task, "queued", "gemini" if model_kind == "gemini" else model_kind, model,
                   model_kind, human_policy, now, now, label, j(faults or {}), int(ro), call_cap))
    return rid


def get_run(rid: str) -> dict | None:
    with db() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None


def update_run(rid: str, **kw):
    kw["updated_at"] = time.time()
    for k in ("scope", "plan", "summary"):
        if k in kw:
            kw[f"{k}_json"] = j(kw.pop(k))
    sets = ", ".join(f"{k}=?" for k in kw)
    with db() as c:
        c.execute(f"UPDATE runs SET {sets} WHERE id=?", (*kw.values(), rid))


def list_runs(limit=50):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]


# ---------------- events ----------------
def emit(rid: str, etype: str, payload: dict | None = None, case_id: str | None = None, actor: str = "runtime") -> int:
    with db() as c:
        cur = c.execute("INSERT INTO events (run_id, case_id, type, actor, ts, payload_json) VALUES (?,?,?,?,?,?)",
                        (rid, case_id, etype, actor, time.time(), j(payload or {})))
        return cur.lastrowid


def events(rid: str, after: int = 0, types: tuple[str, ...] | None = None) -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (rid, after)).fetchall()
    out = []
    for r in rows:
        if types and r["type"] not in types:
            continue
        d = dict(r)
        d["payload"] = unj(d.pop("payload_json"))
        out.append(d)
    return out


# ---------------- facts ----------------
def add_fact(rid, case_id, key, value, source):
    with db() as c:
        c.execute("INSERT INTO facts VALUES (?,?,?,?,?,?)", (rid, case_id, key, str(value), source, time.time()))


def facts(rid) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM facts WHERE run_id=? ORDER BY created_at", (rid,))]


# ---------------- proposals ----------------
def add_proposal(rid, case_id, source, action, values, rationale, evidence) -> dict:
    with db() as c:
        v = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM proposals WHERE run_id=? AND case_id=?",
                      (rid, case_id)).fetchone()[0]
        pid = new_id("prop")
        c.execute("INSERT INTO proposals (id, run_id, case_id, version, source, action, values_json, rationale,"
                  " evidence_json, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (pid, rid, case_id, v, source, action, j(values), rationale, j(evidence), "proposed", time.time()))
    return get_proposal(pid)


def update_proposal(pid, **kw):
    for k in ("checker", "comparison"):
        if k in kw:
            kw[f"{k}_json"] = j(kw.pop(k))
    sets = ", ".join(f"{k}=?" for k in kw)
    with db() as c:
        c.execute(f"UPDATE proposals SET {sets} WHERE id=?", (*kw.values(), pid))


def _prop(r):
    d = dict(r)
    for k in ("values", "evidence", "checker", "comparison"):
        d[k] = unj(d.pop(f"{k}_json"))
    return d


def get_proposal(pid):
    with db() as c:
        r = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    return _prop(r) if r else None


def proposals(rid, case_id=None):
    with db() as c:
        if case_id:
            rows = c.execute("SELECT * FROM proposals WHERE run_id=? AND case_id=? ORDER BY created_at", (rid, case_id))
        else:
            rows = c.execute("SELECT * FROM proposals WHERE run_id=? ORDER BY created_at", (rid,))
        return [_prop(r) for r in rows]


# ---------------- decisions ----------------
def add_decision(rid, case_id, kind, request) -> str:
    did = new_id("dec")
    with db() as c:
        c.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)",
                  (did, rid, case_id, kind, "pending", j(request), None, time.time(), None))
    return did


def resolve_decision(did, response) -> bool:
    with db() as c:
        cur = c.execute("UPDATE decisions SET status='resolved', response_json=?, resolved_at=? "
                        "WHERE id=? AND status='pending'", (j(response), time.time(), did))
        return cur.rowcount == 1  # duplicate submissions are ignored


def get_decision(did):
    with db() as c:
        r = c.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["request"] = unj(d.pop("request_json"))
    d["response"] = unj(d.pop("response_json"))
    return d


def decisions(rid=None, status=None):
    sql, args = "SELECT * FROM decisions WHERE 1=1", []
    if rid:
        sql += " AND run_id=?"
        args.append(rid)
    if status:
        sql += " AND status=?"
        args.append(status)
    with db() as c:
        rows = c.execute(sql + " ORDER BY created_at", args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["request"] = unj(d.pop("request_json"))
        d["response"] = unj(d.pop("response_json"))
        out.append(d)
    return out


# ---------------- authorized actions ----------------
def add_authorized(rid, case_id, proposal_id, seq, action_type, method, route, fields, digest, idem_key, ttl_s,
                   basis=None):
    aid = new_id("auth")
    now = time.time()
    with db() as c:
        c.execute("INSERT INTO authorized_actions (id, run_id, case_id, proposal_id, seq, action_type, method, route,"
                  " fields_json, digest, idem_key, status, result_json, created_at, expires_at, basis_json)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (aid, rid, case_id, proposal_id, seq, action_type, method, route, j(fields), digest, idem_key,
                   "authorized", None, now, now + ttl_s, j(basis) if basis is not None else None))
    return aid


def _auth(r):
    d = dict(r)
    d["fields"] = unj(d.pop("fields_json"))
    d["result"] = unj(d.pop("result_json"))
    d["basis"] = unj(d.pop("basis_json", None))
    return d


def authorized(rid, case_id=None, statuses=None):
    sql, args = "SELECT * FROM authorized_actions WHERE run_id=?", [rid]
    if case_id:
        sql += " AND case_id=?"
        args.append(case_id)
    with db() as c:
        rows = [_auth(r) for r in c.execute(sql + " ORDER BY created_at, seq", args)]
    if statuses:
        rows = [r for r in rows if r["status"] in statuses]
    return rows


def update_authorized(aid, **kw):
    if "result" in kw:
        kw["result_json"] = j(kw.pop("result"))
    sets = ", ".join(f"{k}=?" for k in kw)
    with db() as c:
        c.execute(f"UPDATE authorized_actions SET {sets} WHERE id=?", (*kw.values(), aid))


# ---------------- model call ledger ----------------
def record_call(rid, status, kind, inp=0, out=0, think=0, error=None) -> int:
    with db() as c:
        cur = c.execute("INSERT INTO model_calls (run_id, ts, status, kind, input_tokens, output_tokens,"
                        " thinking_tokens, error) VALUES (?,?,?,?,?,?,?,?)",
                        (rid, time.time(), status, kind, inp, out, think, error))
        return cur.lastrowid


def update_call(cid, **kw):
    sets = ", ".join(f"{k}=?" for k in kw)
    with db() as c:
        c.execute(f"UPDATE model_calls SET {sets} WHERE id=?", (*kw.values(), cid))


def calls(rid=None, since=None, provider_only=False):
    """provider_only: count only real model-provider requests (excludes the offline scripted client)."""
    sql, args = "SELECT * FROM model_calls WHERE 1=1", []
    if rid:
        sql += " AND run_id=?"
        args.append(rid)
    if since:
        sql += " AND ts>=?"
        args.append(since)
    if provider_only:
        sql += " AND kind != 'scripted'"
    with db() as c:
        return [dict(r) for r in c.execute(sql, args)]


def save_verification(rid, case_id, result):
    with db() as c:
        c.execute("DELETE FROM verifications WHERE run_id=? AND case_id=?", (rid, case_id))
        c.execute("INSERT INTO verifications VALUES (?,?,?,?)", (rid, case_id, j(result), time.time()))


def verifications(rid):
    with db() as c:
        return {r["case_id"]: unj(r["result_json"]) for r in
                c.execute("SELECT * FROM verifications WHERE run_id=?", (rid,))}


# ---------------- case controls (human stop / reopen, persisted) ----------------
def set_case_control(rid, case_id, state, actor, reason=""):
    with db() as c:
        c.execute("INSERT INTO case_controls VALUES (?,?,?,?,?,?)", (rid, case_id, state, actor, reason, time.time()))


def stopped_cases(rid) -> set[str]:
    """Cases whose latest control is 'stopped' (a later 'reopened' record lifts it)."""
    latest = {}
    with db() as c:
        for r in c.execute("SELECT * FROM case_controls WHERE run_id=? ORDER BY ts", (rid,)):
            latest[r["case_id"]] = r["state"]
    return {k for k, v in latest.items() if v == "stopped"}


def case_controls(rid) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM case_controls WHERE run_id=? ORDER BY ts", (rid,))]


def delete_run(rid: str):
    with db() as c:
        for t in ("runs", "events", "proposals", "decisions", "authorized_actions", "facts", "model_calls",
                  "verifications", "case_controls"):
            c.execute(f"DELETE FROM {t} WHERE {'id' if t == 'runs' else 'run_id'}=?", (rid,))
