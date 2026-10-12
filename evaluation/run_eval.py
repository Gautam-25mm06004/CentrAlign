"""Evaluation harness: fresh sandbox state per scenario, fault injection, a full operator run,
then assertions against trusted database state using hand-written expectations.

  python -m evaluation.run_eval --suite offline                 # scripted client, zero Gemini calls
  python -m evaluation.run_eval --suite live-smoke --engine gemini --max-calls 120
"""
from __future__ import annotations

import sys as _sys

for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import argparse
import json
import os
import time
from pathlib import Path

import yaml

from shared.config import DATA, load_env_file

load_env_file()  # before the runtime modules are imported

from operator_core import runner, store, trusted  # noqa: E402
from scripts.run import DEFAULT_TASKS  # noqa: E402
from scripts.seed import SEEDERS, seed  # noqa: E402

SPEC = yaml.safe_load((Path(__file__).parent / "scenarios.yaml").read_text(encoding="utf-8"))
RESULTS = DATA / "eval_results.json"


def wait_apps():
    for _ in range(30):
        try:
            for co in SEEDERS:
                trusted.snapshot(co)
            return
        except Exception:  # noqa: BLE001
            time.sleep(1)
    raise SystemExit("Sandbox apps are not running (start them with scripts/start_all).")


def prepare(sc: dict):
    spec = sc.get("seed", {})
    for co in SEEDERS:
        cases = spec.get(co)
        seed(co, None if cases in (None, "all") else cases)
    for co in SEEDERS:
        trusted.set_faults(co, {"reset": True})
        if sc.get("faults", {}).get(co):
            trusted.set_faults(co, sc["faults"][co])


def assert_state(sc: dict, rid: str) -> list[dict]:
    exp = sc["expect"]
    checks = []

    def ck(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    run = store.get_run(rid)
    ck("overall verification", run["status"] == exp["overall"], f"got {run['status']}, expected {exp['overall']}")
    if "steel" in exp:
        snap = trusted.snapshot("steel")
        for doc, e in exp["steel"].items():
            rows = [p for p in snap["processed_invoices"] if p["source_doc"] == doc]
            if e["decision"] == "none":
                ck(f"{doc}: nothing recorded", not rows, f"{len(rows)} record(s)")
            else:
                ck(f"{doc}: one record with decision {e['decision']}",
                   len(rows) == 1 and rows[0]["decision"] == e["decision"], f"{[r['decision'] for r in rows]}")
            ids = {r["id"] for r in rows}
            led = [lg for lg in snap["ledger"] if lg["processed_id"] in ids]
            ck(f"{doc}: {e['ledger']} ledger posting(s)", len(led) == e["ledger"], f"{len(led)}")
    if "ecommerce" in exp:
        snap = trusted.snapshot("ecommerce")
        for t, e in exp["ecommerce"].items():
            tk = next(x for x in snap["tickets"] if x["id"] == t)
            ck(f"{t}: status {e['status']}", tk["status"] == e["status"], tk["status"])
            amounts = sorted(r["amount"] for r in snap["refunds"] if r["ticket_id"] == t)
            ck(f"{t}: refunds {e['refunds']}", amounts == sorted(e["refunds"]), f"{amounts}")
            if e["status"] != "open":
                ck(f"{t}: customer got a reply", any(r["ticket_id"] == t for r in snap["replies"]))
    if "school" in exp:
        snap = trusted.snapshot("school")
        for aid, e in exp["school"].items():
            app = next(x for x in snap["applications"] if x["id"] == aid)
            ck(f"{aid}: status {e['status']}", app["status"] == e["status"], app["status"])
            en = [x for x in snap["enrolments"] if x["application_id"] == aid]
            if e.get("batch"):
                ok = len(en) == 1 and en[0]["batch_id"] == e["batch"] and en[0]["fee_payable"] == e["fee"] \
                    and en[0]["concession_pct"] == e.get("concession", 0)
                ck(f"{aid}: enrolled in {e['batch']} at {e['fee']} paise", ok, f"{[(x['batch_id'], x['fee_payable']) for x in en]}")
            else:
                ck(f"{aid}: not enrolled", not en, f"{len(en)} enrolment(s)")
            ck(f"{aid}: applicant messaged", any(m["application_id"] == aid for m in snap["messages"]))
    for case in exp.get("approvals_requested", []):
        ck(f"{case}: human approval requested",
           any(d["kind"] == "approval" and d["case_id"] == case for d in store.decisions(rid)))
    return checks


def run_scenario(sc: dict, engine: str, call_cap: int | None = None) -> dict:
    prepare(sc)
    label = f"eval:{sc['id']}"
    if engine == "scripted" and sc.get("mistakes"):
        label += " mistakes=" + ",".join(sc["mistakes"])
    rid = store.create_run(sc["company"], sc.get("task") or DEFAULT_TASKS[sc["company"]],
                           os.environ.get("GEMINI_MODEL", "") if engine == "gemini" else "scripted-offline",
                           engine, sc.get("human", "auto"), label=label, faults=sc.get("faults"),
                           read_only=sc.get("read_only"), call_cap=call_cap)
    model = None
    if engine == "scripted":
        from evaluation.scripted import ScriptedModel
        model = ScriptedModel(rid, set(sc.get("mistakes", [])))
    t0 = time.time()
    runner.run(rid, model=model)
    checks = assert_state(sc, rid)
    calls = store.calls(rid)
    return {"scenario": sc["id"], "covers": sc.get("covers", []), "run_id": rid, "engine": engine,
            "passed": all(c["ok"] for c in checks), "checks": checks, "final_status": store.get_run(rid)["status"],
            "model_calls": len(calls), "seconds": round(time.time() - t0, 1),
            "human_decisions": len(store.decisions(rid)), "at": time.time()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="offline")
    ap.add_argument("--scenario", nargs="*")
    ap.add_argument("--engine", choices=["scripted", "gemini"], default="scripted")
    ap.add_argument("--max-calls", type=int, help="total model-call allocation for this suite (live)")
    a = ap.parse_args()
    store.init()
    wait_apps()
    ids = a.scenario or SPEC["suites"][a.suite]
    scen = {s["id"]: s for s in SPEC["scenarios"]}
    results = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else []
    used = 0
    for sid in ids:
        if a.max_calls is not None and used >= a.max_calls:
            print(f"- {sid}: NOT RUN (suite call allocation {a.max_calls} reached)")
            results.append({"scenario": sid, "engine": a.engine, "passed": None, "final_status": "not_run",
                            "at": time.time()})
            continue
        # the suite allocation is enforced per attempt: a run may use at most what is left of it
        r = run_scenario(scen[sid], a.engine, None if a.max_calls is None else a.max_calls - used)
        used += r["model_calls"]
        results.append(r)
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"- {sid} [{a.engine}] {mark} · status={r['final_status']} · calls={r['model_calls']} · {r['seconds']}s")
        for c in r["checks"]:
            if not c["ok"]:
                print(f"    ✗ {c['check']} ({c['detail']})")
    RESULTS.write_text(json.dumps(results, indent=2), encoding="utf-8")
    batch = [r for r in results[-len(ids):] if r.get("passed") is not None]
    print(f"\n{sum(1 for r in batch if r['passed'])}/{len(batch)} scenarios passed ({a.engine}). "
          f"Results appended to {RESULTS}")


if __name__ == "__main__":
    main()
