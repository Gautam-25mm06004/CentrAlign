"""Proposal handling, independent checking, human resolution and exact-action authorization.

Flow for every business decision:
  Gemini proposal (persisted first) -> independent checker -> comparison
  -> hard denial | human resolution (disagreement / not covered) | approval -> AuthorizedAction(s)
  -> browser request guard attaches a signed capability only to a matching submission.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import parse_qsl

from operator_core import checker, store, trusted
from shared import capability
from shared.actions import FieldError, business_fields_from_form, normalize, paise_to_str
from shared.config import load_secrets

AUTH_TTL_S = 1800
TOKEN_TTL_S = 120


class Human:
    """Durable human decisions. 'console' waits for a person; 'auto:*' answers as a labelled test operator."""

    def __init__(self, run_id: str, policy: str = "console", poll_s: float = 1.0, timeout_s: float = 3600):
        self.run_id, self.policy, self.poll_s, self.timeout_s = run_id, policy, poll_s, timeout_s

    def ask(self, kind: str, case_id: str | None, request: dict) -> dict:
        did = store.add_decision(self.run_id, case_id, kind, request)
        store.emit(self.run_id, "decision_requested", {"decision_id": did, "kind": kind, "request": request}, case_id)
        prev = store.get_run(self.run_id)["status"]
        store.update_run(self.run_id, status="waiting_for_approval" if kind == "approval" else "waiting_for_input")
        if self.policy.startswith("auto"):
            store.resolve_decision(did, auto_answer(self.policy, kind, request))
        start = time.time()
        while True:
            d = store.get_decision(did)
            if d["status"] == "resolved":
                break
            if time.time() - start > self.timeout_s:
                store.resolve_decision(did, {"choice": "stop", "reason": "no human response before timeout",
                                             "actor": "runtime"})
            time.sleep(self.poll_s)
        if store.get_run(self.run_id)["status"] != "cancel_requested":
            store.update_run(self.run_id, status=prev if prev not in ("waiting_for_approval", "waiting_for_input")
                             else "running")
        resp = d["response"]
        store.emit(self.run_id, "decision_resolved", {"decision_id": did, "kind": kind, "response": resp},
                   case_id, actor=resp.get("actor", "human"))
        return resp


def auto_answer(policy: str, kind: str, request: dict) -> dict:
    """Test-operator answers used by the evaluator and offline demos (always labelled as such)."""
    opts = dict(p.split("=", 1) for p in policy.split(":", 1)[1].split(",") if "=" in p) if ":" in policy else {}
    base = {"actor": "test-operator (automatic)"}
    if kind == "approval":
        return {**base, "choice": opts.get("approval", "approve"), "reason": "evaluation policy"}
    if kind == "disagreement":
        choice = opts.get("disagreement", "follow_rules")
        if choice == "follow_rules" and not request.get("rules_option_available"):
            choice = "follow_gemini"
        return {**base, "choice": choice, "reason": "evaluation policy"}
    if kind == "question":
        return {**base, "answer": opts.get("answer", "I don't know. Please follow the documented company policy.")}
    if kind == "scope":  # an unenforceable request: the safe default is recommendations only
        return {**base, "choice": opts.get("scope", "read_only"), "reason": "evaluation policy"}
    return {**base, "choice": "ack"}


class Gate:
    def __init__(self, run_id: str, company: dict, human: Human, agent_source: str = "gemini"):
        self.run_id = run_id
        self.agent_source = agent_source  # 'gemini' for live runs, 'scripted' for offline tests
        self.company = company          # loaded company bundle (profile, policy, adapter)
        self.profile = company["profile"]
        self.policy = company["policy"]
        self.adapter = company["adapter"]
        self.human = human
        self.origin = company["origin"]
        self.last_write: dict | None = None
        self.policy_hash = hashlib.sha256(json.dumps(self.policy, sort_keys=True, default=str).encode()).hexdigest()[:16]

    # ------------------------------------------------------------ durable run/case state
    @property
    def stopped_cases(self) -> set[str]:
        """Persisted: survives a worker restart. A new proposal never reopens a stopped case."""
        return store.stopped_cases(self.run_id)

    def _run_status(self) -> str:
        return (store.get_run(self.run_id) or {}).get("status", "")

    def _read_only(self) -> bool:
        return bool((store.get_run(self.run_id) or {}).get("read_only"))

    def stop_case(self, case_id: str, actor: str, reason: str):
        """Record the stop and revoke every outstanding permission for the case."""
        store.set_case_control(self.run_id, case_id, "stopped", actor, reason)
        revoked = self.revoke(case_id=case_id, why=f"case stopped by {actor}")
        store.emit(self.run_id, "case_stopped", {"actor": actor, "reason": reason, "revoked": revoked}, case_id)

    def revoke(self, case_id: str | None = None, why: str = "") -> int:
        n = 0
        for a in store.authorized(self.run_id, case_id, statuses=("authorized",)):
            store.update_authorized(a["id"], status="revoked", result={"message": why})
            n += 1
        return n

    def _basis(self, facts: dict, expected: dict) -> dict:
        return {"policy": self.policy_hash, "facts": facts, "expected": expected}

    def basis_changes(self, case_id: str, basis: dict) -> list[str]:
        """Re-read the source records now and list what changed since the decision was authorized."""
        try:
            facts, expected = self.adapter.facts(case_id)
        except Exception as e:  # noqa: BLE001
            return [f"records could not be re-read ({e})"]
        now = self._basis(facts, expected)
        out = []
        if now["policy"] != basis.get("policy"):
            out.append("company policy changed")
        for part in ("facts", "expected"):
            old, new = basis.get(part) or {}, now[part]
            for k in sorted(set(old) | set(new)):
                if old.get(k) != new.get(k):
                    out.append(f"{k}: {old.get(k)!r} -> {new.get(k)!r}")
        return out

    def reconcile_case(self, case_id: str | None = None) -> list[dict]:
        """Resolve submitted/uncertain writes through the trusted idempotency record.
        A failed lookup is NOT proof of absence: the write stays 'uncertain'."""
        out = []
        for a in store.authorized(self.run_id, case_id, statuses=("submitted", "uncertain")):
            try:
                rec = trusted.idempotency(self.profile["app"], a["idem_key"])
            except Exception as e:  # noqa: BLE001
                store.update_authorized(a["id"], status="uncertain")
                out.append({"authorization": a["id"], "result": "unknown", "error": str(e)[:200]})
                continue
            if rec:
                store.update_authorized(a["id"], status="committed",
                                        result={"reconciled": True, "message": "committed (confirmed by reconciliation)"})
                out.append({"authorization": a["id"], "result": "committed"})
            else:
                store.update_authorized(a["id"], status="authorized")
                out.append({"authorization": a["id"], "result": "not_saved"})
        return out

    # ------------------------------------------------------------ proposals
    def propose(self, case_id: str, action: str, values: dict, evidence: list, rationale: str,
                scope: list[str], source: str | None = None) -> str:
        source = source or self.agent_source
        if self._run_status() in ("cancel_requested", "cancelled"):
            return "RUN CANCELLED by the operator: no further decisions or changes are permitted. Stop now."
        if case_id not in scope:
            return f"ERROR: {case_id!r} is not in this task's scope. Cases in scope: {scope}"
        if case_id in self.stopped_cases:
            return (f"CASE STOPPED: a human stopped work on {case_id}. Do not change it. Only a human can reopen it "
                    f"from the console. Continue with the other cases.")
        actions = self.profile["actions"]
        if action not in actions:
            return f"ERROR: unknown action {action!r}. Allowed: {list(actions)}"
        missing = [v for v in actions[action]["values"] if v != "reason" and values.get(v) in (None, "")]
        if missing:
            return f"ERROR: missing values for {action}: {missing}"
        # an earlier write whose outcome is unknown blocks any change of intent for this case
        self.reconcile_case(case_id)
        if store.authorized(self.run_id, case_id, statuses=("uncertain",)):
            return (f"BLOCKED: the outcome of an earlier write for {case_id} is still unknown (reconciliation "
                    f"failed). No new decision can be made for this case until it is resolved; continue with "
                    f"other cases and try again later.")
        # 1. persist the agent's initial proposal BEFORE any checker feedback
        prop = store.add_proposal(self.run_id, case_id, source, action, values, rationale, evidence)
        store.emit(self.run_id, "proposal", {"proposal_id": prop["id"], "version": prop["version"], "action": action,
                                             "values": values, "rationale": rationale, "source": source},
                   case_id, actor=source)
        # 2. independent check from source documents and trusted records
        try:
            facts, expected = self.adapter.facts(case_id)
            chk = checker.evaluate(self.policy, facts)
        except Exception as e:  # noqa: BLE001
            facts, expected = {}, {}
            chk = {"status": "not_covered", "allowed_actions": [], "fired": [], "unevaluated":
                   [{"rule": "checker_error", "missing": [str(e)], "effect": "error"}], "denied_actions": [],
                   "hard_denied_actions": [], "approval_rules": [], "facts": {}, "outcome": None,
                   "policy_version": self.policy.get("version")}
        chk["expected_values"] = expected
        cmp_ = checker.compare(prop, chk, expected, self.profile)
        # internal consistency of the proposed values (e.g. a posting must reconcile arithmetically)
        problems = self._value_problems(action, values)
        if problems:
            cmp_["differences"].append({"field": "consistency", "gemini": "; ".join(problems),
                                        "rules": "proposed values must be internally consistent"})
            if cmp_["status"] == "agree":
                cmp_["status"] = "disagree"
        store.update_proposal(prop["id"], checker=chk, comparison=cmp_, status="checked")
        store.emit(self.run_id, "comparison", {"proposal_id": prop["id"], "comparison": cmp_,
                                               "rules_outcome": chk.get("allowed_actions"),
                                               "fired": chk.get("fired"), "expected_values": expected},
                   case_id, actor="checker")
        # read-only request: record the decision as a recommendation; nothing can be authorized
        if self._read_only():
            store.update_proposal(prop["id"], status="recommendation")
            store.emit(self.run_id, "recommendation", {"proposal_id": prop["id"], "action": action,
                                                       "rules_outcome": chk.get("allowed_actions"),
                                                       "comparison": cmp_["status"]}, case_id)
            return (f"READ-ONLY TASK: recorded '{action}' for {case_id} as a recommendation "
                    f"(rules: {chk.get('allowed_actions') or 'not covered'}, comparison: {cmp_['status']}). "
                    f"No changes are permitted in this task; do not submit any form. Continue with the next case.")
        # 3. hard restrictions: never overridable from the console
        if action in chk.get("hard_denied_actions", []):
            store.update_proposal(prop["id"], status="denied")
            msgs = "; ".join(r["message"] for r in chk["fired"] if r["effect"] == "hard")
            store.emit(self.run_id, "hard_denial", {"proposal_id": prop["id"], "message": msgs}, case_id)
            return (f"DENIED by hard policy: {msgs}. This cannot be overridden. "
                    f"Permitted outcome per policy: {chk['allowed_actions']}. Re-check and propose again.")
        chosen = {"action": action, "values": values, "source": source, "proposal": prop}
        # 4. disagreement or uncovered decision -> human resolution
        if cmp_["status"] != "agree":
            resp = self.human.ask("disagreement", case_id, self._disagreement_request(prop, chk, cmp_))
            chosen = self._apply_resolution(resp, prop, chk, case_id)
            if isinstance(chosen, str):
                return chosen
        # 5. approval for the chosen action and values
        appr = checker.approval_needed(self.policy, chk.get("facts", {}), chosen["action"], chosen["values"],
                                       self.profile.get("approval_value_facts", {}))
        if appr:
            resp = self.human.ask("approval", case_id, {
                "proposal_id": chosen["proposal"]["id"], "source": chosen["source"], "action": chosen["action"],
                "values": chosen["values"], "rules": appr, "rationale": chosen["proposal"]["rationale"],
                "evidence": chosen["proposal"]["evidence"]})
            if resp.get("choice") != "approve":
                store.update_proposal(chosen["proposal"]["id"], status="approval_rejected")
                self.stop_case(case_id, resp.get("actor", "human"), f"approval rejected: {resp.get('reason', '')}")
                return (f"Approval REJECTED by {resp.get('actor', 'human')}: {resp.get('reason', '')}. "
                        f"Do not perform this action. Leave case {case_id} as is and continue with other cases.")
        if self._run_status() in ("cancel_requested", "cancelled"):
            return "RUN CANCELLED by the operator while this decision was pending: nothing was authorized."
        return self._authorize(case_id, chosen, self._basis(chk.get("facts", {}), expected))

    def _value_problems(self, action: str, values: dict) -> list[str]:
        fn = getattr(self.adapter, "check_values", None)
        try:
            return list(fn(action, values)) if fn else []
        except Exception as e:  # noqa: BLE001
            return [f"values could not be checked: {e}"]

    def _disagreement_request(self, prop, chk, cmp_):
        rules_ok = chk["status"] == "determined" and len(chk["allowed_actions"]) == 1
        return {"proposal_id": prop["id"], "gemini": {"action": prop["action"], "values": prop["values"],
                                                      "rationale": prop["rationale"], "evidence": prop["evidence"]},
                "rules": {"status": chk["status"], "outcome": chk["allowed_actions"],
                          "expected_values": chk.get("expected_values", {}), "fired": chk["fired"],
                          "gaps": chk["unevaluated"], "policy_version": chk.get("policy_version")},
                "comparison": cmp_, "rules_option_available": rules_ok,
                "hard_denied_actions": chk.get("hard_denied_actions", []),
                "allowed_custom_actions": list(self.profile["actions"])}

    def _apply_resolution(self, resp, prop, chk, case_id):
        choice = resp.get("choice")
        actor = resp.get("actor", "human")
        if choice == "follow_gemini":
            return {"action": prop["action"], "values": prop["values"], "source": prop["source"], "proposal": prop}
        if choice == "follow_rules":
            if not (chk["status"] == "determined" and len(chk["allowed_actions"]) == 1):
                return "ERROR: rules have no single determined outcome; the human must choose another option."
            action = chk["allowed_actions"][0]
            values = dict(prop["values"])
            if action != prop["action"]:
                # free-text written for a different action (e.g. a refund reply) must not be reused
                keep = set(self.profile.get("compare_values", {})) | {"reason"}
                keep |= {k for k in self.profile["actions"][action]["values"] if k in self.profile.get("compare_values", {})}
                values = {k: v for k, v in values.items() if k in keep or not _is_text_value(self.profile, k)}
            values.update({k: v for k, v in chk.get("expected_values", {}).items()
                           if k in self.profile["actions"][action]["values"]})
            missing = [v for v in self.profile["actions"][action]["values"] if v != "reason" and not values.get(v)]
            if missing:
                store.update_proposal(prop["id"], status="superseded")
                return (f"HUMAN DECISION: follow the rules outcome '{action}' for {case_id}. Your proposal was not "
                        f"accepted. Gather the missing values {missing} (for example a reply to the customer) and "
                        f"propose '{action}' again.")
            p2 = store.add_proposal(self.run_id, case_id, "rules", action, values,
                                    f"Rules outcome selected by {actor}: {resp.get('reason', '')}", prop["evidence"])
            store.update_proposal(p2["id"], checker=chk, status="checked")
            store.update_proposal(prop["id"], status="superseded")
            return {"action": action, "values": values, "source": "rules", "proposal": p2}
        if choice == "custom":
            c = resp.get("custom") or {}
            action, values = c.get("action"), {**prop["values"], **(c.get("values") or {})}
            if action not in self.profile["actions"]:
                return "ERROR: human custom decision used an unknown action."
            if action in chk.get("hard_denied_actions", []):
                return "DENIED: the custom decision violates a hard policy restriction."
            p2 = store.add_proposal(self.run_id, case_id, "human", action, values,
                                    f"Custom decision by {actor}: {resp.get('reason', '')}", prop["evidence"])
            store.update_proposal(p2["id"], checker=chk, status="checked")
            store.update_proposal(prop["id"], status="superseded")
            return {"action": action, "values": values, "source": "human", "proposal": p2}
        store.update_proposal(prop["id"], status="stopped")
        self.stop_case(case_id, actor, resp.get("reason", "stopped"))
        return (f"HUMAN DECISION: stop work on {case_id} ({resp.get('reason', '')}). Do not change this case. "
                f"Continue with the remaining cases.")

    # ------------------------------------------------------------ authorization
    def _render(self, template: str, values: dict, case_id: str) -> str:
        return re.sub(r"\{(\w+)\}", lambda m: str(case_id if m.group(1) == "case_id" else values.get(m.group(1), "")),
                      str(template))

    def _authorize(self, case_id: str, chosen: dict, basis: dict | None = None) -> str:
        spec = self.profile["actions"][chosen["action"]]
        prop = chosen["proposal"]
        problems = self._value_problems(chosen["action"], chosen["values"])
        if problems:
            store.update_proposal(prop["id"], status="rejected_inconsistent")
            return ("ERROR: the decided values are not internally consistent and cannot be authorized: "
                    + "; ".join(problems) + ". Re-check the source and propose again.")
        lines = []
        # a newer decision for this case replaces any earlier writes that were authorized but never performed
        for old in store.authorized(self.run_id, case_id, statuses=("authorized",)):
            store.update_authorized(old["id"], status="superseded")
        for i, w in enumerate(spec["writes"], 1):
            raw = {k: self._render(v, chosen["values"], case_id) for k, v in w["fields"].items()}
            try:
                fields = normalize(w["action"], raw)
            except FieldError as e:
                return f"ERROR: proposal values are invalid for {w['action']}: {e}. Propose again with valid values."
            route = self._render(w["route"], chosen["values"], case_id)
            digest = capability.payload_digest(fields)
            idem = hashlib.sha256(f"{self.run_id}|{case_id}|{w['action']}|{digest}".encode()).hexdigest()[:32]
            done_before = next((a for a in store.authorized(self.run_id, case_id, statuses=("committed",))
                                if a["idem_key"] == idem), None)
            aid = store.add_authorized(self.run_id, case_id, prop["id"], i, w["action"], w["method"], route,
                                       fields, digest, idem, AUTH_TTL_S, basis=basis)
            if done_before:
                # identical write already saved earlier in this run: record it as done, never ask for it again
                store.update_authorized(aid, status="committed", result={"message": "identical write already saved",
                                                                         "same_as": done_before["id"]})
            shown = {k: (paise_to_str(int(v)) if capability_type(w["action"], k) == "money" else v)
                     for k, v in fields.items()}
            if done_before:
                lines.append(f"  {i}. ALREADY SAVED earlier ({w['action']} on {route}); do NOT submit it again.")
                continue
            lines.append(f"  {i}. {w['method']} {route} [{w['action']}] with " +
                         ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in shown.items()) + f"  (authorization {aid})")
        store.update_proposal(prop["id"], status="authorized")
        store.emit(self.run_id, "authorized", {"proposal_id": prop["id"], "action": chosen["action"],
                                               "source": chosen["source"], "values": chosen["values"]}, case_id)
        extra = " Also fill the 'reason / notes' field with a short explanation." if "reason" in spec["values"] else ""
        src = {"rules": "the RULES outcome chosen by the human",
               "human": "a CUSTOM human decision"}.get(chosen["source"], "your proposal")
        return (f"AUTHORIZED ({src}): action '{chosen['action']}' for {case_id}. Perform these writes now through "
                f"the application forms, in order, with exactly these values (amounts in rupees):\n" + "\n".join(lines)
                + f"\nAny other submission will be blocked.{extra} Check each result page.")

    # ------------------------------------------------------------ browser request guard
    def guard_request(self, route, request, action_type: str, path: str, headers: dict):
        items = parse_qsl(request.post_data or "", keep_blank_values=True)
        block = lambda msg: self._block(route, action_type, path, msg)  # noqa: E731
        if self._run_status() in ("cancel_requested", "cancelled"):
            self.revoke(why="run cancelled")
            return block("The run was cancelled by the operator. No further changes are permitted.")
        if self._read_only():
            return block("This task is read-only: no changes are permitted.")
        try:
            fields = business_fields_from_form(action_type, items)
        except FieldError as e:
            return block(f"Submission has invalid business fields: {e}")
        digest = capability.payload_digest(fields)
        cands = [a for a in store.authorized(self.run_id, statuses=("authorized", "uncertain"))
                 if a["action_type"] == action_type and a["route"] == path]
        match = next((a for a in cands if a["digest"] == digest), None)
        if not match:
            if not cands:
                return block(f"No authorization exists for {action_type} on {path}. Propose a decision first.")
            near = cands[0]
            diffs = [f"{k}: submitted {fields.get(k)!r}, authorized {v!r}" for k, v in near["fields"].items()
                     if fields.get(k) != v]
            return block("Submitted values differ from the authorized action: " + "; ".join(diffs))
        if match["expires_at"] < time.time():
            store.update_authorized(match["id"], status="expired")
            return block("Authorization expired. Propose the decision again.")
        if match["case_id"] in self.stopped_cases:
            self.revoke(match["case_id"], "case stopped")
            return block(f"A human stopped work on {match['case_id']}. This write is no longer permitted.")
        siblings = [a for a in store.authorized(self.run_id, match["case_id"]) if a["proposal_id"] == match["proposal_id"]]
        earlier = [a for a in siblings if a["seq"] < match["seq"] and a["status"] != "committed"]
        if earlier:
            return block(f"Writes must be performed in order: first {earlier[0]['method']} {earlier[0]['route']} "
                         f"[{earlier[0]['action_type']}] ({earlier[0]['status']}).")
        # before the decision's FIRST effect, re-read the records it was based on; a changed basis needs a new decision
        if match["status"] == "authorized" and match.get("basis") and \
                not any(a["status"] == "committed" for a in siblings):
            changed = self.basis_changes(match["case_id"], match["basis"])
            if changed:
                for a in siblings:
                    if a["status"] == "authorized":
                        store.update_authorized(a["id"], status="stale", result={"message": "; ".join(changed)[:500]})
                store.emit(self.run_id, "authorization_stale", {"authorization": match["id"], "changes": changed},
                           match["case_id"])
                return block("The records this decision was based on changed after it was authorized ("
                             + "; ".join(changed[:6]) + "). Re-check the case and propose the decision again.")
        token = capability.issue(load_secrets()["capability_key"], audience=self.origin, method=request.method,
                                 route=path, action=action_type, case_id=match["case_id"], digest=digest,
                                 run_id=self.run_id, idempotency_key=match["idem_key"], ttl_s=TOKEN_TTL_S)
        headers = dict(headers)
        headers[capability.HEADER] = token
        store.update_authorized(match["id"], status="submitted")
        store.emit(self.run_id, "write_submitted", {"authorization": match["id"], "action": action_type,
                                                    "route": path}, match["case_id"])
        try:
            resp = route.fetch(headers=headers, max_redirects=0, max_retries=0, timeout=20000)  # milliseconds
        except Exception as e:  # noqa: BLE001
            return self._uncertain(route, match, f"connection error: {e}")
        status = resp.status
        body = resp.text()
        msg = _page_message(body)
        if status < 300:
            ref = re.search(r"id='result-ref'>([^<]+)<", body)
            store.update_authorized(match["id"], status="committed", result={"http": status, "message": msg,
                                                                            "ref": ref.group(1) if ref else None})
            self.last_write = {"status": "committed", "message": msg, "authorization": match["id"]}
            store.emit(self.run_id, "write_committed", {"authorization": match["id"], "message": msg},
                       match["case_id"])
        elif status >= 500:
            return self._uncertain(route, match, f"HTTP {status}", resp)
        else:
            # rejected by the app (session, business rule, validation) => nothing written
            new = "authorized" if status in (401, 403) else "rejected"
            store.update_authorized(match["id"], status=new, result={"http": status, "message": msg})
            self.last_write = {"status": "rejected", "message": f"HTTP {status}: {msg}", "authorization": match["id"]}
            store.emit(self.run_id, "write_rejected", {"authorization": match["id"], "http": status, "message": msg},
                       match["case_id"])
        route.fulfill(response=resp)

    def _uncertain(self, route, match, why: str, resp=None):
        store.update_authorized(match["id"], status="uncertain")
        store.emit(self.run_id, "write_uncertain", {"authorization": match["id"], "why": why}, match["case_id"])
        try:
            rec = trusted.idempotency(self.profile["app"], match["idem_key"])
        except Exception as e:  # noqa: BLE001 - unknown is not "absent": keep the write uncertain
            text = ("The application returned an error and the outcome could not be confirmed (reconciliation "
                    f"failed: {str(e)[:120]}). Treat this write as UNKNOWN: do not change this case's decision. "
                    "Resubmitting the same authorized values is safe (it cannot create a duplicate).")
            self.last_write = {"status": "unknown_after_reconciliation", "message": text}
            store.emit(self.run_id, "write_reconciled", {"authorization": match["id"], "result": "unknown"},
                       match["case_id"])
            route.fulfill(status=503, content_type="text/html",
                          body=f"<html><body><h1>Submission result unknown ({why})</h1><p>{text}</p></body></html>")
            return
        if rec:
            store.update_authorized(match["id"], status="committed",
                                    result={"reconciled": True, "message": "committed (confirmed by reconciliation)"})
            text = ("The application returned an error, but reconciliation against trusted records confirms the "
                    "write WAS saved exactly once. Do NOT submit it again.")
            self.last_write = {"status": "committed_after_reconciliation", "message": text}
        else:
            store.update_authorized(match["id"], status="authorized")
            text = ("The application returned an error and reconciliation confirms nothing was saved. "
                    "It is safe to submit the same authorized values again.")
            self.last_write = {"status": "not_saved_after_reconciliation", "message": text}
        store.emit(self.run_id, "write_reconciled", {"authorization": match["id"], "result": self.last_write["status"]},
                   match["case_id"])
        route.fulfill(status=200 if rec else 503, content_type="text/html",
                      body=f"<html><body><h1>Submission result uncertain ({why})</h1><p>{text}</p></body></html>")

    def _block(self, route, action_type, path, msg):
        self.last_write = {"status": "blocked", "message": msg}
        store.emit(self.run_id, "write_blocked", {"action": action_type, "route": path, "message": msg})
        route.fulfill(status=403, content_type="text/html",
                      body=f"<html><body><h1>Blocked by operator gate</h1><p id='gate-msg'>{msg}</p></body></html>")


def _is_text_value(profile: dict, name: str) -> bool:
    """A value that ends up in a 'text' business field (e.g. reply_text -> body)."""
    from shared.actions import ACTION_SCHEMAS
    for spec in profile["actions"].values():
        for w in spec["writes"]:
            for field, tmpl in w["fields"].items():
                if f"{{{name}}}" in str(tmpl) and ACTION_SCHEMAS[w["action"]].get(field) == "text" \
                        and field not in ("invoice_no", "grade", "source_doc"):
                    return True
    return False


def capability_type(action: str, field: str) -> str:
    from shared.actions import ACTION_SCHEMAS
    return ACTION_SCHEMAS[action].get(field, "text")


def _page_message(html: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>.*?<p>(.*?)</p>", html, re.S)
    return re.sub(r"<[^>]+>", "", f"{m.group(1)}: {m.group(2)}") if m else html[:200]
