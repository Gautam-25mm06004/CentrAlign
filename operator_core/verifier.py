"""Independent completion verification: reads trusted records and compares them with the original task scope and
the authorized decisions — never with the model's own claims.

A case is verified only when every write of its CURRENT accepted decision was saved (an expired, rejected, stale or
revoked obligation is not "done"), and the adapter's field-and-relationship checks pass against the database.
"""
from __future__ import annotations

from operator_core import store, trusted

FAILED_OBLIGATION = ("expired", "rejected", "stale", "revoked")
OPEN_OBLIGATION = ("authorized", "submitted", "uncertain")


def _current_decision_writes(run_id: str, case: str) -> list[dict]:
    """Writes of the latest authorized proposal for the case (its obligations)."""
    props = [p for p in store.proposals(run_id, case) if p["status"] == "authorized"]
    if not props:
        return []
    pid = props[-1]["id"]
    return [w for w in store.authorized(run_id, case) if w["proposal_id"] == pid]


def verify_run(run_id: str, company: dict, stopped_cases: set[str] | None = None) -> dict:
    run = store.get_run(run_id)
    scope = store.unj(run["scope_json"]) or {}
    cases, before = scope.get("cases", []), scope.get("before", {})
    after = trusted.snapshot(company["profile"]["app"])
    if not cases:
        return _verify_empty_scope(run_id, company, scope, before, after)
    if run.get("read_only"):
        return _verify_read_only(run_id, company, cases, before, after, scope)
    pending = {d["case_id"] for d in store.decisions(run_id, status="pending")}
    stopped = set(stopped_cases or set()) | store.stopped_cases(run_id)
    results = {}
    with_writes = set()
    for case in cases:
        writes = [w for w in store.authorized(run_id, case)
                  if w["status"] not in ("superseded", "revoked") and not (w["result"] or {}).get("same_as")]
        if any(w["status"] == "committed" for w in writes):
            with_writes.add(case)
        r = company["adapter"].verify(case, writes, before, after)
        if r["status"] == "not_handled":
            if case in pending:
                r["status"] = "waiting"
            elif case in stopped:
                r["status"] = "stopped_by_human"
        obligations = _current_decision_writes(run_id, case)
        failed = [w for w in obligations if w["status"] in FAILED_OBLIGATION]
        unfinished = [w for w in obligations if w["status"] in OPEN_OBLIGATION]
        if (failed or unfinished) and r["status"] in ("verified", "failed", "not_handled") and case not in stopped:
            r["checks"].append({"check": "every write of the accepted decision was saved", "ok": False,
                                "detail": ", ".join(f"{w['action_type']} {w['status']}" for w in failed + unfinished)})
            if r["status"] == "verified":
                r["status"] = "partial"
        decided = [p for p in store.proposals(run_id, case) if p["status"] == "authorized"]
        if decided:
            r["decision_source"] = decided[-1]["source"]
            r["action"] = decided[-1]["action"]
        results[case] = r
        store.save_verification(run_id, case, r)
    unexpected = company["adapter"].unexpected_effects(before, after, with_writes)
    statuses = [r["status"] for r in results.values()]
    if unexpected or "failed" in statuses:
        overall = "failed"
    elif all(s in ("verified", "stopped_by_human") for s in statuses):
        overall = "completed"
    else:
        overall = "partially_completed"
    if overall == "completed" and scope.get("unresolved"):  # the user asked for a case that could not be done
        overall = "partially_completed"
    out = {"overall": overall, "cases": results, "unexpected_effects": unexpected}
    if scope.get("unresolved"):
        out["unresolved_requested"] = scope["unresolved"]
    store.emit(run_id, "verification", out, actor="verifier")
    return out


def _verify_empty_scope(run_id: str, company: dict, scope: dict, before: dict, after: dict) -> dict:
    """No valid case in scope. A valid empty queue is complete; a request naming missing or ambiguous cases is not."""
    changes = diff_records(before, after) if before else []
    issues = scope.get("issues") or []
    if changes:
        overall = "failed"
    elif issues:
        overall = "needs_clarification"
    else:
        overall = "completed"
    out = {"overall": overall, "cases": {}, "unexpected_effects": changes,
           "note": "; ".join(issues) if issues else "No pending work was found."}
    if scope.get("unresolved"):
        out["unresolved_requested"] = scope["unresolved"]
    store.emit(run_id, "verification", out, actor="verifier")
    return out


def diff_records(before: dict, after: dict) -> list[str]:
    """Every added, deleted or changed business record between two trusted snapshots (keyed by id).
    Valid for this single-worker sandbox; a shared multi-user system would need per-change attribution."""
    out = []
    for table in sorted(set(before) | set(after)):
        b_rows, a_rows = before.get(table), after.get(table)
        if not isinstance(b_rows, list) or not isinstance(a_rows, list):
            if b_rows != a_rows:
                out.append(f"{table} changed")
            continue
        b = {str(r.get("id")): r for r in b_rows}
        a = {str(r.get("id")): r for r in a_rows}
        for rid in sorted(set(a) - set(b)):
            out.append(f"{table}: record {rid} added")
        for rid in sorted(set(b) - set(a)):
            out.append(f"{table}: record {rid} deleted")
        for rid in sorted(set(a) & set(b)):
            changed = sorted(k for k in set(a[rid]) | set(b[rid]) if a[rid].get(k) != b[rid].get(k))
            if changed:
                out.append(f"{table}: record {rid} changed ({', '.join(changed)})")
    return out


def _verify_read_only(run_id: str, company: dict, cases: list[str], before: dict, after: dict,
                      scope: dict | None = None) -> dict:
    """A read-only task is complete when every case has a recorded recommendation and no business record changed:
    the gate prevents writes, and this independently checks that the records stayed exactly as they were."""
    results = {}
    for case in cases:
        recs = [p for p in store.proposals(run_id, case) if p["status"] == "recommendation"]
        ok = bool(recs)
        results[case] = {"status": "reported" if ok else "not_handled", "disposition": recs[-1]["action"] if ok else None,
                         "action": recs[-1]["action"] if ok else None, "decision_source": recs[-1]["source"] if ok else None,
                         "checks": [{"check": "recommendation recorded (read-only task)", "ok": ok, "detail": ""}]}
        store.save_verification(run_id, case, results[case])
    unexpected = company["adapter"].unexpected_effects(before, after, set())
    changes = diff_records(before, after)
    changed = bool(unexpected or changes) or any(w["status"] == "committed" for w in store.authorized(run_id))
    overall = "failed" if changed else ("completed" if all(r["status"] == "reported" for r in results.values())
                                        else "partially_completed")
    if overall == "completed" and (scope or {}).get("unresolved"):
        overall = "partially_completed"
    out = {"overall": overall, "cases": results, "unexpected_effects": sorted(set(unexpected) | set(changes)),
           "read_only": True}
    if (scope or {}).get("unresolved"):
        out["unresolved_requested"] = scope["unresolved"]
    store.emit(run_id, "verification", out, actor="verifier")
    return out


def outstanding(result: dict) -> list[str]:
    return [c for c, r in result["cases"].items() if r["status"] in ("not_handled", "partial", "failed")]
