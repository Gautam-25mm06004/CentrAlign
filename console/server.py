"""Operator console backend (Starlette). Serves the single-page console and a small API over the
persisted run state. Starts runs as separate worker processes; streams events with SSE.

Run: python -m console.server   ->  http://127.0.0.1:8100
"""
from __future__ import annotations

import sys as _sys

for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import re

from shared.config import COMPANIES, DATA, PORTS, ROOT, RUNS, load_env_file

load_env_file()  # before the runtime modules are imported

from operator_core import store  # noqa: E402
from operator_core.company import available, load as load_company  # noqa: E402
from operator_core.llm import pacific_day_start  # noqa: E402

store.init()
RUN_ID = re.compile(r"^run-[0-9a-f]{8}$")
STATIC = Path(__file__).parent / "static"
ACTIVE: dict[str, subprocess.Popen] = {}
TASKS = {c: load_company(c)["profile"].get("default_task", "") for c in available()}


def _spawn(args: list[str]) -> subprocess.Popen:
    env = dict(os.environ)
    return subprocess.Popen([sys.executable, *args], cwd=str(ROOT), env=env,
                            stdout=open(DATA / "worker.log", "a", encoding="utf-8"), stderr=subprocess.STDOUT)


def _active_run() -> str | None:
    for rid, p in list(ACTIVE.items()):
        if p.poll() is None:
            return rid
        ACTIVE.pop(rid, None)
    return None


async def index(request):
    return FileResponse(STATIC / "index.html")


async def overview(request):
    comps = []
    for cid in sorted(available(), key=lambda c: c != "steel"):
        c = load_company(cid)
        p = c["profile"]
        comps.append({"id": cid, "name": p["name"], "actions": {k: v["description"] for k, v in p["actions"].items()},
                      "rules": [{"id": r["id"], "effect": r["effect"], "message": r.get("message", "")}
                                for r in c["policy"]["rules"]],
                      "policy_version": c["policy"].get("version"), "handbook": c["handbook"],
                      "default_task": TASKS.get(cid, ""), "protected_routes": p["protected_routes"]})
    calls_today = len(store.calls(since=pacific_day_start()))
    return JSONResponse({
        "companies": comps,
        "gemini": {"configured": bool(os.environ.get("GEMINI_API_KEY") and os.environ.get("GEMINI_MODEL")),
                   "model": os.environ.get("GEMINI_MODEL", ""), "calls_today": calls_today,
                   "rpd": os.environ.get("GEMINI_RPD"),
                   "daily_protection": bool(os.environ.get("GEMINI_RPD")),
                   "run_cap": int(os.environ.get("MAX_MODEL_CALLS_PER_RUN", 90))},
        "pending_decisions": len(store.decisions(status="pending")),
        "active_run": _active_run(),
    })


async def runs(request):
    if request.method == "POST":
        body = await request.json()
        if _active_run():
            return JSONResponse({"error": "One run is already active. Wait for it to finish or cancel it."}, 409)
        company = body["company"]
        engine = body.get("engine", "gemini")
        if engine == "gemini" and not (os.environ.get("GEMINI_API_KEY") and os.environ.get("GEMINI_MODEL")):
            return JSONResponse({"error": "Gemini is not configured. Add GEMINI_API_KEY and GEMINI_MODEL to .env."}, 400)
        if body.get("reset", True):
            from scripts.seed import seed
            seed(company)
        from operator_core import trusted
        trusted.set_faults(company, {"reset": True})
        faults = {k: v for k, v in (body.get("faults") or {}).items() if v}
        if faults:
            trusted.set_faults(company, faults)
        rid = store.create_run(company, body.get("task") or TASKS[company],
                               os.environ.get("GEMINI_MODEL", "") if engine == "gemini" else "scripted-offline",
                               engine, body.get("human", "console"), label=body.get("label", ""), faults=faults,
                               read_only=True if body.get("read_only") else None)
        ACTIVE[rid] = _spawn(["-m", "scripts.run", "--run-id", rid])
        return JSONResponse({"run_id": rid})
    rows = store.list_runs(100)
    for r in rows:
        r.pop("scope_json", None)
        r["summary"] = store.unj(r.pop("summary_json"))
        r["plan"] = store.unj(r.pop("plan_json"))
        r["calls"] = len(store.calls(r["id"]))
    return JSONResponse(rows)


async def run_detail(request):
    rid = request.path_params["rid"]
    run = store.get_run(rid)
    if not run:
        return JSONResponse({"error": "not found"}, 404)
    scope = store.unj(run.pop("scope_json")) or {}
    run["plan"] = store.unj(run.pop("plan_json"))
    run["summary"] = store.unj(run.pop("summary_json"))
    calls = store.calls(rid)
    return JSONResponse({
        "run": run, "cases": scope.get("cases", []), "proposals": store.proposals(rid),
        "decisions": store.decisions(rid), "writes": store.authorized(rid), "facts": store.facts(rid),
        "verifications": store.verifications(rid), "stopped_cases": sorted(store.stopped_cases(rid)),
        "usage": {"calls": len(calls), "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
                  "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
                  "thinking_tokens": sum(c["thinking_tokens"] or 0 for c in calls)},
        "alive": rid in ACTIVE and ACTIVE[rid].poll() is None,
    }, headers={"Cache-Control": "no-store"})


async def run_events(request):
    rid = request.path_params["rid"]
    after = int(request.headers.get("last-event-id") or request.query_params.get("after", 0))

    async def gen():
        nonlocal after
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            evs = store.events(rid, after)
            for e in evs:
                after = e["seq"]
                yield f"id: {e['seq']}\nevent: ev\ndata: {json.dumps(e, default=str)}\n\n"
            run = store.get_run(rid)
            idle = 0 if evs else idle + 1
            if idle % 20 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.6)
            if run and run["status"] in ("completed", "partially_completed", "failed", "cancelled",
                                         "waiting_for_quota", "needs_clarification") and not evs and idle > 3:
                yield "event: end\ndata: {}\n\n"
                break
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


async def run_file(request):
    """Serve a run's artifacts only: the run id must be a well-formed id of an existing run, its folder must be a
    direct child of RUNS, and the resolved file must stay inside that folder (checked after decoding and resolving
    symlinks)."""
    rid = request.path_params["rid"]
    rel = request.path_params["path"]
    if not RUN_ID.match(rid) or not store.get_run(rid):
        return Response(status_code=404)
    root = RUNS.resolve()
    base = (root / rid).resolve()
    if base.parent != root:
        return Response(status_code=404)
    p = (base / rel).resolve()
    if base not in p.parents or not p.is_file():
        return Response(status_code=404)
    return FileResponse(p, headers={"Cache-Control": "no-store"})


async def run_cancel(request):
    rid = request.path_params["rid"]
    store.update_run(rid, status="cancel_requested")
    for d in store.decisions(rid, status="pending"):
        store.resolve_decision(d["id"], {"choice": "stop", "actor": "operator", "reason": "run cancelled"})
    return JSONResponse({"ok": True})


async def run_resume(request):
    rid = request.path_params["rid"]
    if _active_run():
        return JSONResponse({"error": "Another run is active."}, 409)
    run = store.get_run(rid)
    if run["status"] != "waiting_for_quota":
        return JSONResponse({"error": "Only runs paused for Gemini capacity can be resumed."}, 400)
    for d in store.decisions(rid, status="pending"):
        if d["kind"] == "quota_handoff":
            store.resolve_decision(d["id"], {"choice": "resume_with_gemini", "actor": "operator"})
    store.update_run(rid, status="queued", mode="gemini")
    ACTIVE[rid] = _spawn(["-m", "scripts.run", "--run-id", rid])
    return JSONResponse({"ok": True})


async def case_reopen(request):
    """A human deliberately reopens a stopped case (recorded). A new model proposal can never do this by itself."""
    rid, case = request.path_params["rid"], request.path_params["case"]
    body = await request.json()
    if not (body.get("reason") or "").strip():
        return JSONResponse({"error": "A reason is required to reopen a case."}, 400)
    if case not in store.stopped_cases(rid):
        return JSONResponse({"error": "This case is not stopped."}, 409)
    store.set_case_control(rid, case, "reopened", "operator (console)", body["reason"])
    store.emit(rid, "case_reopened", {"actor": "operator (console)", "reason": body["reason"]}, case)
    return JSONResponse({"ok": True})


async def decisions(request):
    status = request.query_params.get("status")
    rows = store.decisions(status=status)
    runs_ = {r["id"]: r for r in store.list_runs(200)}
    for d in rows:
        r = runs_.get(d["run_id"]) or {}
        d["run_company"], d["run_engine"] = r.get("company"), r.get("model_kind")
    return JSONResponse(rows, headers={"Cache-Control": "no-store"})


async def decide(request):
    did = request.path_params["did"]
    body = await request.json()
    d = store.get_decision(did)
    if not d or d["status"] != "pending":
        return JSONResponse({"error": "This decision is no longer pending."}, 409)
    choice = body.get("choice")
    req = d["request"]
    if d["kind"] == "disagreement":
        if choice not in ("follow_gemini", "follow_rules", "custom", "stop"):
            return JSONResponse({"error": "invalid choice"}, 400)
        if choice in ("follow_gemini", "follow_rules", "custom") and not (body.get("reason") or "").strip():
            return JSONResponse({"error": "A reason is required for this decision."}, 400)
        if choice == "follow_gemini" and req["gemini"]["action"] in req.get("hard_denied_actions", []):
            return JSONResponse({"error": "That action is blocked by a hard policy rule and cannot be approved."}, 400)
        if choice == "follow_rules" and not req.get("rules_option_available"):
            return JSONResponse({"error": "The rules have no single outcome for this case."}, 400)
    if d["kind"] == "approval" and choice not in ("approve", "reject"):
        return JSONResponse({"error": "invalid choice"}, 400)
    if d["kind"] == "scope" and choice not in ("proceed", "read_only", "stop"):
        return JSONResponse({"error": "invalid choice"}, 400)
    if d["kind"] == "scope" and choice == "proceed" and not (body.get("reason") or "").strip():
        return JSONResponse({"error": "A reason is required to proceed with changes."}, 400)
    resp = {k: v for k, v in body.items() if k in ("choice", "reason", "answer", "custom")}
    resp["actor"] = "operator (console)"
    ok = store.resolve_decision(did, resp)
    return JSONResponse({"ok": ok})


async def evals(request):
    if request.method == "POST":
        body = await request.json()
        if _active_run():
            return JSONResponse({"error": "A run is active."}, 409)
        args = ["-m", "evaluation.run_eval", "--suite", body.get("suite", "offline"),
                "--engine", body.get("engine", "scripted")]
        if body.get("engine") == "gemini":
            if not os.environ.get("GEMINI_API_KEY"):
                return JSONResponse({"error": "Gemini is not configured."}, 400)
            args += ["--max-calls", str(int(body.get("max_calls", 120)))]
        ACTIVE["eval"] = _spawn(args)
        return JSONResponse({"ok": True})
    f = DATA / "eval_results.json"
    spec = yaml.safe_load((ROOT / "evaluation" / "scenarios.yaml").read_text(encoding="utf-8"))
    return JSONResponse({"results": json.loads(f.read_text(encoding="utf-8")) if f.exists() else [],
                         "scenarios": spec["scenarios"], "suites": spec["suites"],
                         "running": "eval" in ACTIVE and ACTIVE["eval"].poll() is None})


app = Starlette(routes=[
    Route("/", index),
    Route("/api/overview", overview),
    Route("/api/runs", runs, methods=["GET", "POST"]),
    Route("/api/runs/{rid}", run_detail),
    Route("/api/runs/{rid}/events", run_events),
    Route("/api/runs/{rid}/cancel", run_cancel, methods=["POST"]),
    Route("/api/runs/{rid}/resume", run_resume, methods=["POST"]),
    Route("/api/runs/{rid}/cases/{case}/reopen", case_reopen, methods=["POST"]),
    Route("/runs/{rid}/{path:path}", run_file),
    Route("/api/decisions", decisions),
    Route("/api/decisions/{did}", decide, methods=["POST"]),
    Route("/api/evals", evals, methods=["GET", "POST"]),
    Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
])

if __name__ == "__main__":
    print(f"Operator console: http://127.0.0.1:{PORTS['console']}")
    uvicorn.run(app, host="127.0.0.1", port=PORTS["console"], log_level="warning")
