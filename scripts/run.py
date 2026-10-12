"""Start (or resume) one operator run from the command line.

  python -m scripts.run --company steel --task "Process the supplier invoices in the inbox"
  python -m scripts.run --company ecommerce --engine scripted --human auto     # offline mechanism test
  python -m scripts.run --run-id run-1234abcd                                   # execute an existing queued run
"""
from __future__ import annotations

import sys as _sys

for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import argparse
import os

from shared.config import load_env_file

load_env_file()  # before any runtime module is imported, so every configured limit applies

from operator_core import runner, store  # noqa: E402

def _default_tasks() -> dict:
    from operator_core.company import available, load
    return {c: load(c)["profile"].get("default_task", "") for c in available()}


DEFAULT_TASKS = _default_tasks()


def make_model(run: dict):
    if run["model_kind"] == "scripted":
        from evaluation.scripted import ScriptedModel
        mistakes = set(filter(None, (run.get("label") or "").split("mistakes=")[-1].split(";")[0].split(","))) \
            if "mistakes=" in (run.get("label") or "") else set()
        return ScriptedModel(run["id"], mistakes)
    return None  # GeminiModel created by the runner


def main():
    load_env_file()
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", choices=sorted(DEFAULT_TASKS))
    ap.add_argument("--task")
    ap.add_argument("--engine", choices=["gemini", "scripted"], default="gemini")
    ap.add_argument("--human", default="console", help="console | auto | auto:approval=approve,disagreement=follow_rules")
    ap.add_argument("--run-id")
    ap.add_argument("--label", default="")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--read-only", action="store_true", help="information only: no changes may be authorized")
    a = ap.parse_args()
    store.init()
    if a.run_id:
        rid = a.run_id
    else:
        if not a.company:
            ap.error("--company is required")
        rid = store.create_run(a.company, a.task or DEFAULT_TASKS[a.company],
                               os.environ.get("GEMINI_MODEL", "") if a.engine == "gemini" else "scripted-offline",
                               a.engine, a.human, label=a.label, read_only=True if a.read_only else None)
    run = store.get_run(rid)
    print(f"Run {rid}: {run['company']} · engine={run['model_kind']} · human={run['human_policy']}"
          f"{' · READ-ONLY' if run.get('read_only') else ''}")
    if run["model_kind"] == "gemini" and not os.environ.get("GEMINI_RPD"):
        print("WARNING: GEMINI_RPD is not set, so the daily free-tier protection is OFF.")
    final = runner.run(rid, model=make_model(run), headless=not a.headed)
    print(f"Finished with status: {final['status']}. Report: runs/{rid}/report.md")


if __name__ == "__main__":
    main()
