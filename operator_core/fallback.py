"""Deterministic rules executor, used only after free model capacity has ended.

It continues the run without further model calls, through exactly the same controls as the model:
  rules outcome -> Gate.propose (source 'rules', saved and labelled) -> human approval when a rule requires it
  -> exact authorization -> the normal application form in the same controlled browser -> request guard and
  signed capability -> the app's own validation and idempotency -> independent verification.

It acts only where the rules give a single determined outcome and every value of that action can be filled from
trusted records or the company's declared message templates. Everything else is left for a human, explicitly listed
as not executed. Runs and decisions made this way are labelled 'rules executor' in events, reports and the console.
"""
from __future__ import annotations

import re

from operator_core import checker, store

ACTOR = "rules-executor"


class RulesExecutor:
    def __init__(self, runner):
        self.r = runner
        self.gate = runner.gate
        self.company = runner.company
        self.profile = self.company["profile"]

    # ------------------------------------------------------------------ plan
    def plan(self, cases: list[str]) -> list[dict]:
        """For each remaining case: perform remaining authorized writes, execute a covered rules outcome, or
        leave it for a human (with the reason)."""
        out = []
        for case in cases:
            left = [a for a in store.authorized(self.r.run_id, case, statuses=("authorized",))]
            if left:
                out.append({"case": case, "mode": "finish_authorized", "writes": left})
                continue
            try:
                facts, expected = self.company["adapter"].facts(case)
                chk = checker.evaluate(self.company["policy"], facts)
            except Exception as e:  # noqa: BLE001
                out.append({"case": case, "mode": "human", "why": f"rules could not evaluate the case: {e}"})
                continue
            if chk["status"] != "determined" or len(chk["allowed_actions"]) != 1:
                out.append({"case": case, "mode": "human", "rules": chk["allowed_actions"],
                            "why": "rules do not give a single outcome" if chk["status"] == "determined"
                            else "not covered by the rules: " + ", ".join(
                                f"{u['rule']} ({', '.join(u['missing'])})" for u in chk["unevaluated"])})
                continue
            action = chk["allowed_actions"][0]
            values, missing = self._values(case, action, expected)
            if missing:
                out.append({"case": case, "mode": "human", "rules": [action],
                            "why": f"values not available to the rules executor: {missing}"})
                continue
            out.append({"case": case, "mode": "execute", "action": action, "values": values, "fired": chk["fired"]})
        return out

    def _values(self, case: str, action: str, expected: dict) -> tuple[dict, list[str]]:
        spec = self.profile["actions"][action]
        templates = (self.profile.get("fallback_text") or {}).get(action, {})
        values = {k: v for k, v in expected.items() if k in spec["values"]}
        for name, tmpl in templates.items():
            if name in spec["values"] and not values.get(name):
                values[name] = re.sub(r"\{(\w+)\}", lambda m: str(case if m.group(1) == "case_id" else
                                                                values.get(m.group(1), expected.get(m.group(1), ""))),
                                      tmpl)
        missing = [k for k in spec["values"] if k != "reason" and not values.get(k)]
        return values, missing

    # ------------------------------------------------------------------ execution
    def run(self, cases: list[str]) -> dict:
        plan = self.plan(cases)
        store.emit(self.r.run_id, "rules_executor_started",
                   {"plan": [{k: v for k, v in p.items() if k != "writes"} for p in plan]}, actor=ACTOR)
        done, human = [], []
        for p in plan:
            if self.r.cancelled():
                break
            case = p["case"]
            if p["mode"] == "human":
                human.append(p)
                continue
            if p["mode"] == "execute":
                res = self.gate.propose(case, p["action"], p["values"], ["rules executor (model capacity ended)"],
                                        "Rules outcome executed deterministically after Gemini capacity ended: "
                                        + ", ".join(f["id"] for f in p["fired"]) if p["fired"] else
                                        "Rules default outcome executed deterministically after Gemini capacity ended.",
                                        self.r.scope(), source="rules")
                store.emit(self.r.run_id, "rules_executor_decision", {"action": p["action"], "result": res[:400]},
                           case, actor=ACTOR)
                if not res.startswith("AUTHORIZED"):
                    human.append({**p, "mode": "human", "why": res[:300]})
                    continue
            writes = store.authorized(self.r.run_id, case, statuses=("authorized",))
            ok = all(self._perform(w) for w in writes)
            (done if ok else human).append(p if ok else {**p, "mode": "human", "why": "a write did not complete"})
        return {"executed": [p["case"] for p in done], "left_for_human": human}

    def _form_page(self, w: dict) -> str:
        action_name = next(a for a, spec in self.profile["actions"].items()
                           if any(x["action"] == w["action_type"] for x in spec["writes"]))
        tmpl = next(x for x in self.profile["actions"][action_name]["writes"] if x["action"] == w["action_type"])
        return re.sub(r"\{case_id\}", w["case_id"], tmpl.get("form", w["route"]))

    def _perform(self, w: dict) -> bool:
        """Fill the application's own form with the authorized values and submit it (through the request guard)."""
        b = self.r.browser
        from shared.actions import paise_to_str
        from operator_core.authz import capability_type
        shown = {k: (paise_to_str(int(v)) if capability_type(w["action_type"], k) == "money" else v)
                 for k, v in w["fields"].items()}
        for _attempt in range(2):
            b.page.goto(b.origin + self._form_page(w))
            if b.page.url.split("?")[0].endswith(self.profile["login"]["path"]):
                b.sign_in()
                b.page.goto(b.origin + self._form_page(w))
            form = b.page.locator(f'form[action="{w["route"]}"]')
            if form.count() != 1:
                store.emit(self.r.run_id, "rules_executor_error", {"why": f"form for {w['route']} not found"},
                           w["case_id"], actor=ACTOR)
                return False
            for name, value in shown.items():
                el = form.locator(f'[name="{name}"]')
                if el.count() == 0:
                    continue
                kind = el.first.evaluate("e => e.tagName + ':' + (e.type || '')")
                if kind.endswith(":hidden"):
                    continue
                if kind.startswith("SELECT"):
                    el.first.select_option(str(value))
                else:
                    el.first.fill(str(value))
            reason = form.locator('[name="reason"]')
            if reason.count():
                reason.first.fill(f"Recorded by the rules executor ({w['fields'].get('decision', '')}) after model "
                                  f"capacity ended")
            self.gate.last_write = None
            form.locator('button[type="submit"]').first.click()
            try:
                b.page.wait_for_load_state("load", timeout=10000)
            except Exception:  # noqa: BLE001
                pass
            res = self.gate.last_write or {}
            store.emit(self.r.run_id, "rules_executor_write", {"authorization": w["id"], "result": res.get("status"),
                                                               "message": res.get("message", "")[:300]},
                       w["case_id"], actor=ACTOR)
            if res.get("status") in ("committed", "committed_after_reconciliation"):
                return True
            if res.get("status") == "rejected" and "HTTP 401" not in res.get("message", ""):
                return False
            if res.get("status") in ("blocked", "unknown_after_reconciliation"):
                return False
        return False
