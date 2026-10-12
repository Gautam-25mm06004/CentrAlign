"""The agent loop: Goal -> Understand -> Plan -> Execute -> Observe -> Adapt -> Verify -> Complete.

One worker, one active run. All state is persisted so the console can follow along and
reports can be rebuilt. The model chooses every action; the runtime only executes tools,
enforces boundaries and records evidence.
"""
from __future__ import annotations

import json
import os
import time
import traceback

from operator_core import report, store, verifier
from operator_core.authz import Gate, Human
from operator_core.browser import Browser, ToolError
from operator_core.company import load as load_company
from operator_core.context import PromptTooLarge, build_user_prompt, est_tokens, system_prompt
from operator_core.llm import GeminiModel, ModelError, QuotaExhausted
from operator_core.taskscope import interpret, request_mode
from operator_core.tools import BARRIER_TOOLS, TOOL_NAMES, TOOLS
from shared.config import RUNS

MAX_CALLS_PER_TURN = 3
MAX_INVALID_TURNS = 4


def limits() -> tuple[int, int]:
    """Read at run time so every launcher (console worker, CLI, evaluator) applies the configured .env limits."""
    return int(os.environ.get("MAX_TOOL_STEPS", 160)), int(os.environ.get("MAX_RUN_SECONDS", 3600))


class Runner:
    def __init__(self, run_id: str, model=None, headless: bool = True):
        self.run_id = run_id
        self.run = store.get_run(run_id)
        self.company = load_company(self.run["company"])
        self.run_dir = RUNS / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.human = Human(run_id, self.run["human_policy"])
        self.gate = Gate(run_id, self.company, self.human, agent_source=self.run["model_kind"])
        self.browser = Browser(self.company["profile"], self.run_dir, self.gate, headless=headless)
        self.model = model or GeminiModel(run_id, self.run["model"] or None)
        self.steps: list[dict] = []
        self.latest_obs = ""
        self.completion_requests = 0
        self.finished = False

    # ------------------------------------------------------------ helpers
    def scope(self) -> list[str]:
        return (store.unj(store.get_run(self.run_id)["scope_json"]) or {}).get("cases", [])

    def record(self, tool, args, result, actor="gemini"):
        n = len(self.steps) + 1
        step = {"n": n, "tool": tool, "args": args, "result": result}
        self.steps.append(step)
        store.emit(self.run_id, "step", {"n": n, "tool": tool, "args": args, "result": result[:6000],
                                         "obs": self.browser.obs_n}, case_id=args.get("case_id"), actor=actor)

    def shot(self, label: str, case_id=None):
        path = self.browser.screenshot(label)
        if path:
            store.emit(self.run_id, "screenshot", {"label": label, "path": path}, case_id)

    def cancelled(self) -> bool:
        return store.get_run(self.run_id)["status"] in ("cancel_requested", "cancelled")

    def _save_active_time(self):
        store.update_run(self.run_id, active_s=self.active_before + (time.time() - self.started))

    # ------------------------------------------------------------ main
    def execute(self):
        store.init()
        self.started = time.time()
        self.active_before = float(store.get_run(self.run_id).get("active_s") or 0)  # preserved across restarts
        max_steps, max_seconds = limits()
        try:
            self._setup()
            system = system_prompt(self.company)
            overhead = est_tokens(system) + est_tokens(json.dumps(TOOLS))
            invalid = 0
            while not self.finished:
                if self.cancelled():
                    self._finish("cancelled", "Cancelled by the operator")
                    break
                active = self.active_before + (time.time() - self.started)
                if len(self.steps) >= max_steps or active > max_seconds:
                    self._finish("budget", f"Run budget reached ({len(self.steps)} steps, {int(active)} s active)")
                    break
                self._save_active_time()
                budget = self.model.budget.status() if hasattr(self.model, "budget") else {}
                try:
                    prompt = build_user_prompt(store.get_run(self.run_id), self.steps, self.latest_obs, budget,
                                               overhead_tokens=overhead)
                except PromptTooLarge as e:
                    store.emit(self.run_id, "prompt_too_large", {"error": str(e)})
                    self._finish("failed", f"Request could not fit the context ceiling: {e}")
                    break
                try:
                    turn = self.model.generate(system, prompt, TOOLS)
                except QuotaExhausted as q:
                    self._handoff(q)
                    break
                except ModelError as e:
                    invalid += 1
                    store.emit(self.run_id, "model_error", {"error": str(e)})
                    if invalid >= MAX_INVALID_TURNS:
                        self._finish("failed", f"Model errors: {e}")
                        break
                    continue
                if self.cancelled():  # cancelled while the model was thinking: its returned actions are not run
                    store.emit(self.run_id, "calls_discarded", {"reason": "run cancelled during the model call",
                                                                "calls": [c["name"] for c in turn.calls]})
                    self._finish("cancelled", "Cancelled by the operator")
                    break
                if turn.text:
                    store.emit(self.run_id, "model_text", {"text": turn.text[:1000]}, actor="gemini")
                if not turn.calls:
                    invalid += 1
                    self.record("(no tool call)", {}, f"Your response contained no function call "
                                f"(finish reason {turn.finish}). You must call one of the tools.", actor="runtime")
                    if invalid >= MAX_INVALID_TURNS:
                        self._finish("failed", "Model repeatedly returned no tool call")
                    continue
                invalid = 0
                for call in turn.calls[:MAX_CALLS_PER_TURN]:
                    if self.cancelled():
                        self._finish("cancelled", "Cancelled by the operator")
                        break
                    self._dispatch(call["name"], call.get("args") or {})
                    if self.finished or call["name"] in BARRIER_TOOLS:
                        break
        except Exception as e:  # noqa: BLE001
            store.emit(self.run_id, "runtime_error", {"error": str(e), "trace": traceback.format_exc()[-3000:]})
            if not self.finished:
                self._finish("failed", f"Runtime error: {e}")
        finally:
            try:
                self._save_active_time()
            except Exception:  # noqa: BLE001
                pass
            self.browser.close()
            report.build(self.run_id)

    def _setup(self):
        from operator_core import trusted
        run = store.get_run(self.run_id)
        if not run["scope_json"]:
            candidates = self.company["adapter"].scope()
            s = interpret(run["task"], candidates)
            cases, note = s["cases"], s["note"]
            before = trusted.snapshot(self.company["profile"]["app"])
            issues = list(s["issues"])
            if s["unresolved"]:
                issues.append(f"requested case(s) not open or not found: {', '.join(s['unresolved'])}")
            mode, why = request_mode(run["task"])
            if mode == "unclear" and not run.get("read_only"):
                issues.append(why)
            scope = {"cases": cases, "before": before, "note": note, "unresolved": s["unresolved"], "issues": issues}
            store.update_run(self.run_id, scope=scope)
            store.emit(self.run_id, "scope_captured", {"cases": cases, "note": note, "issues": issues,
                                                       "read_only": bool(run.get("read_only"))})
        run = store.get_run(self.run_id)
        scope = store.unj(run["scope_json"]) or {}
        if not scope.get("cases"):
            # nothing valid to work on: an empty queue is complete, a missing/ambiguous request is not
            if scope.get("issues"):
                self._finish("needs_clarification", "Nothing was done: " + "; ".join(scope["issues"]))
            else:
                verifier.verify_run(self.run_id, self.company)
                self._finish("completed", "No pending work was found.")
            return
        if scope.get("issues") and not run.get("read_only") and not scope.get("clarified"):
            for d in store.decisions(self.run_id, status="pending"):  # a question the previous worker never got answered
                if d["kind"] == "scope":
                    store.resolve_decision(d["id"], {"choice": "interrupted", "actor": "runtime",
                                                     "reason": "worker restarted; asked again"})
            if not self._confirm_scope(run["task"], scope["cases"], scope["issues"], scope):
                return
        # resuming: restore the compact step history from the event log
        for e in store.events(self.run_id, types=("step",)):
            p = e["payload"]
            self.steps.append({"n": p["n"], "tool": p["tool"], "args": p["args"], "result": p["result"]})
        if self.steps:
            self._recover()
        store.update_run(self.run_id, status="running")
        store.emit(self.run_id, "run_started", {"company": self.company["id"], "task": run["task"],
                                                "engine": run["model_kind"], "model": run["model"]})
        self.browser.start()
        self.latest_obs = self.browser.observe()
        if hasattr(self.model, "attach"):
            self.model.attach(self)

    def _confirm_scope(self, task: str, cases: list[str], issues: list[str], scope: dict) -> bool:
        """The request cannot be enforced exactly as written. Ask before any write: proceed with the listed cases,
        give recommendations only (read-only), or stop. Never silently widen permission to write."""
        resp = self.human.ask("scope", None, {"task": task, "cases": cases, "issues": issues,
                                              "options": ["proceed", "read_only", "stop"]})
        choice = resp.get("choice")
        scope["clarified"] = {"choice": choice, "actor": resp.get("actor", "human"), "reason": resp.get("reason", "")}
        store.update_run(self.run_id, scope=scope)
        if choice == "proceed":
            return True
        if choice == "read_only":
            store.update_run(self.run_id, read_only=1)
            store.emit(self.run_id, "scope_clarified", {"choice": choice, "read_only": True})
            return True
        self._finish("needs_clarification", "Stopped before any change: " + "; ".join(issues))
        return False

    # ------------------------------------------------------------ tools
    def _recover(self):
        """Before the model acts again after a restart: reconcile every write that may have been dispatched,
        close human decisions the dead worker was waiting for, and tell the model the recovered state."""
        rec = self.gate.reconcile_case()
        interrupted = []
        for d in store.decisions(self.run_id, status="pending"):
            if d["kind"] == "quota_handoff":
                continue
            store.resolve_decision(d["id"], {"choice": "interrupted", "actor": "runtime",
                                             "reason": "worker restarted before an answer; the decision must be asked again"})
            pid = (d["request"] or {}).get("proposal_id")
            if pid:
                store.update_proposal(pid, status="interrupted")
            interrupted.append(d["case_id"])
        stopped = sorted(self.gate.stopped_cases)
        summary = {"restored_steps": len(self.steps), "reconciled": rec, "interrupted_decisions": interrupted,
                   "stopped_cases": stopped}
        store.emit(self.run_id, "run_resumed", summary)
        lines = ["RECOVERY after a restart (from durable records, not memory):"]
        for r in rec:
            lines.append(f"- write {r['authorization']}: {r['result']}")
        if interrupted:
            lines.append(f"- human decisions interrupted for {interrupted}: propose those cases again")
        if stopped:
            lines.append(f"- cases stopped by a human (do not touch): {stopped}")
        lines.append("Check CASE STATUS before acting.")
        self.record("(recovery)", {}, "\n".join(lines), actor="runtime")

    def _dispatch(self, name: str, args: dict):
        if name not in TOOL_NAMES:
            self.record(name, args, f"ERROR: unknown tool {name}. Available: {sorted(TOOL_NAMES)}")
            return
        try:
            result = getattr(self, f"t_{name}")(**args)
        except ToolError as e:
            result = f"ERROR: {e}"
        except TypeError as e:
            result = f"ERROR: invalid arguments for {name}: {e}"
        except Exception as e:  # noqa: BLE001 - surface tool failures to the model, keep running
            result = f"ERROR: {type(e).__name__}: {e}"
            try:
                self.latest_obs = self.browser.observe()
                result += "\n" + self.latest_obs
            except Exception:  # noqa: BLE001
                pass
        # loop guard: the same navigation repeated recently means the model is not making progress
        if name in ("open_page", "observe", "read_document"):
            sig = (name, json.dumps(args, sort_keys=True))
            recent = [(st["tool"], json.dumps(st["args"], sort_keys=True)) for st in self.steps[-8:]]
            if recent.count(sig) >= 2:
                result = (f"LOOP WARNING: you already did {name}({args}) {recent.count(sig)} times in the last 8 steps. "
                          "Use what you saw, note() the facts, and move to a decision or the next case.\n" + result)
                store.emit(self.run_id, "loop_detected", {"tool": name, "args": args})
        notices = self.browser.pop_notices()
        if notices:
            result += "\nGATE NOTICES: " + "; ".join(notices)
        self.record(name, args, result)

    def t_set_plan(self, steps, success_criteria):
        store.update_run(self.run_id, plan={"steps": steps, "success_criteria": success_criteria})
        store.emit(self.run_id, "plan", {"steps": steps, "success_criteria": success_criteria}, actor="gemini")
        return "Plan recorded."

    def _page_result(self, obs: str) -> str:
        self.latest_obs = obs
        w = self.gate.last_write
        prefix = ""
        if w:
            prefix = f"WRITE RESULT: {w['status']} — {w['message']}\n"
            auth = next((a for a in store.authorized(self.run_id) if a["id"] == w.get("authorization")), None)
            if auth:
                left = store.authorized(self.run_id, auth["case_id"], statuses=("authorized", "uncertain"))
                prefix += (f"STILL TO DO for {auth['case_id']}: " + "; ".join(
                    f"{a['method']} {a['route']} [{a['action_type']}]" for a in left)
                           + ". Perform it now with the authorized values.\n") if left else \
                    f"All authorized writes for {auth['case_id']} are saved.\n"
            self.shot(f"write {w['status']}")
            self.gate.last_write = None
        return prefix + obs

    def t_open_page(self, path):
        return self._page_result(self.browser.open(path))

    def t_observe(self, page=1):
        return self._page_result(self.browser.observe(int(page or 1)))

    def t_click(self, ref):
        return self._page_result(self.browser.click(ref))

    def t_fill(self, fields):
        return self.browser.fill(fields)

    def t_sign_in(self):
        return self._page_result(self.browser.sign_in())

    def _files_dir(self):
        d = self.company["files_dir"]
        if not d:
            raise ToolError("This company has no document inbox.")
        return d

    def t_list_files(self):
        d = self._files_dir()
        files = sorted(p.name for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
        return f"{len(files)} document(s) in the inbox: " + ", ".join(files)

    def t_read_document(self, name):
        d = self._files_dir()
        p = (d / name).resolve()
        if p.parent != d.resolve() or not p.exists() or p.suffix.lower() != ".pdf":
            raise ToolError(f"No document named {name!r} in the inbox.")
        import pdfplumber
        with pdfplumber.open(p) as pdf:
            text = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
        if not text.strip():
            return f"{name}: no extractable text (unsupported scan). Ask a human or hold."
        return f"Document {name} (untrusted supplier content):\n{text[:4000]}"

    def t_note(self, key, value, source, case_id=None):
        store.add_fact(self.run_id, case_id, key, value, source)
        return "Noted."

    def t_propose_decision(self, case_id, action, values, rationale, evidence=None):
        vals = {}
        for v in values or []:
            if isinstance(v, dict) and "name" in v:
                vals[str(v["name"])] = "" if v.get("value") is None else str(v["value"])
        self.shot(f"before decision {case_id}", case_id)
        res = self.gate.propose(case_id, action, vals, evidence or [], rationale, self.scope())
        if store.get_run(self.run_id)["status"] != "cancel_requested":
            store.update_run(self.run_id, status="running")
        return res

    def t_ask_human(self, question, case_id=None):
        resp = self.human.ask("question", case_id, {"question": question})
        if resp.get("choice") == "stop":
            return "The human did not answer (timeout/stop). Do not guess; leave this case unresolved."
        return f"Human answer ({resp.get('actor', 'human')}): {resp.get('answer', '')}"

    def t_request_completion(self, summary):
        self.completion_requests += 1
        res = verifier.verify_run(self.run_id, self.company, self.gate.stopped_cases)
        left = verifier.outstanding(res)
        lines = [f"{c}: {r['status']}" + (" — " + "; ".join(ch["check"] + " (" + ch.get("detail", "") + ")"
                                                              for ch in r["checks"] if not ch["ok"])
                                          if r["status"] != "verified" else "")
                 for c, r in res["cases"].items()]
        if left and self.completion_requests < 3:
            return ("NOT COMPLETE. Independent verification:\n" + "\n".join(lines) +
                    f"\nUnexpected effects: {res['unexpected_effects'] or 'none'}. Handle the outstanding cases "
                    f"{left} and request completion again.")
        self.shot("final state")
        store.update_run(self.run_id, summary={"agent_summary": summary, "verification": res["overall"]})
        self._finish(res["overall"], summary)
        return "Verification result: " + res["overall"] + "\n" + "\n".join(lines)

    # ------------------------------------------------------------ endings
    def _finish(self, status: str, note: str):
        if self.finished:
            return
        self.finished = True
        if status == "cancelled":
            revoked = self.gate.revoke(why="run cancelled")
            rec = self.gate.reconcile_case()  # report the real outcome of anything already dispatched
            store.emit(self.run_id, "cancellation_cleanup", {"revoked": revoked, "reconciled": rec})
            try:
                verifier.verify_run(self.run_id, self.company, self.gate.stopped_cases)
            except Exception as e:  # noqa: BLE001
                store.emit(self.run_id, "runtime_error", {"error": f"verification failed: {e}"})
        if status in ("budget", "failed", "waiting_for_quota", "needs_clarification"):
            try:
                res = verifier.verify_run(self.run_id, self.company, self.gate.stopped_cases)
                if status == "budget":
                    status = res["overall"] if res["overall"] != "completed" else "completed"
            except Exception as e:  # noqa: BLE001
                store.emit(self.run_id, "runtime_error", {"error": f"verification failed: {e}"})
        store.update_run(self.run_id, status=status)
        store.emit(self.run_id, "run_finished", {"status": status, "note": note})

    def _handoff(self, q: QuotaExhausted):
        """Free Gemini capacity ended. No further model calls. Reconcile in-flight writes; then the labelled rules
        executor completes the cases the rules fully cover (including unfinished obligations of decisions already
        accepted), through the same gate, browser forms, idempotency and verifier. Cases the rules do not cover are
        queued for a human as NOT executed."""
        from operator_core.fallback import RulesExecutor
        store.emit(self.run_id, "model_capacity_unavailable", {"kind": q.kind, "detail": q.detail})
        self.gate.reconcile_case()
        res = verifier.verify_run(self.run_id, self.company, self.gate.stopped_cases)
        remaining = [c for c, r in res["cases"].items() if r["status"] not in ("verified", "stopped_by_human")]
        executed, left = [], [{"case": c, "why": "rules executor disabled"} for c in remaining]
        if remaining and os.environ.get("QUOTA_FALLBACK", "rules") == "rules" and not store.get_run(self.run_id).get("read_only"):
            store.update_run(self.run_id, mode="rules_fallback")
            out = RulesExecutor(self).run(remaining)
            executed, left = out["executed"], out["left_for_human"]
        payload = {"reason": q.kind, "detail": q.detail[:300], "executed_by_rules": executed,
                   "recommendations": [{"case": p["case"], "rules_outcome": p.get("rules") or ([p["action"]] if p.get("action") else []),
                                        "why": p.get("why", ""), "label": "left for a human — not executed"}
                                       for p in left]}
        store.emit(self.run_id, "quota_handoff", payload)
        if left:
            store.add_decision(self.run_id, None, "quota_handoff", payload)
            store.update_run(self.run_id, mode="paused_no_model")
            self._finish("waiting_for_quota", f"Gemini capacity unavailable ({q.kind}); rules executor completed "
                                              f"{len(executed)} case(s); {len(left)} left for a human")
        else:
            res = verifier.verify_run(self.run_id, self.company, self.gate.stopped_cases)
            self.shot("final state")
            store.update_run(self.run_id, summary={"agent_summary": f"Gemini capacity ended ({q.kind}); the rules "
                                                   f"executor completed {len(executed)} case(s).",
                                                   "verification": res["overall"]})
            self._finish(res["overall"], f"Completed by the rules executor after Gemini capacity ended ({q.kind})")


def run(run_id: str, model=None, headless: bool = True):
    Runner(run_id, model=model, headless=headless).execute()
    return store.get_run(run_id)


def dumps(o) -> str:
    return json.dumps(o, indent=2, default=str)
