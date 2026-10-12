"""Deterministic evidence report (no model calls)."""
from __future__ import annotations

import json

from operator_core import store
from shared.config import RUNS


def build(run_id: str) -> dict:
    run = store.get_run(run_id)
    ver = next((e["payload"] for e in reversed(store.events(run_id, types=("verification",)))), None)
    props = store.proposals(run_id)
    decs = store.decisions(run_id)
    auths = store.authorized(run_id)
    calls = store.calls(run_id)
    shots = [e["payload"] for e in store.events(run_id, types=("screenshot",))]
    handoff = [e["payload"] for e in store.events(run_id, types=("quota_handoff",))]
    comparisons = []
    for p in props:
        if p["source"] in ("gemini", "scripted") and p["comparison"]:
            comparisons.append({"case": p["case_id"], "version": p["version"], "gemini_action": p["action"], "agent": p["source"],
                                "rules_outcome": (p["checker"] or {}).get("allowed_actions"),
                                "status": p["comparison"]["status"],
                                "differences": p["comparison"]["differences"]})
    usage = {"model_calls": len(calls), "ok_calls": sum(1 for c in calls if c["status"] == "ok"),
             "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
             "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
             "thinking_tokens": sum(c["thinking_tokens"] or 0 for c in calls)}
    rep = {"run": {k: run[k] for k in ("id", "company", "task", "status", "model", "model_kind", "human_policy",
                                        "label", "created_at", "updated_at")},
           "plan": store.unj(run["plan_json"]), "verification": ver, "comparisons": comparisons,
           "decisions": decs, "writes": auths, "usage": usage, "screenshots": shots, "handoff": handoff,
           "summary": store.unj(run["summary_json"])}
    d = RUNS / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    (d / "report.md").write_text(to_markdown(rep), encoding="utf-8")
    return rep


def to_markdown(rep: dict) -> str:
    r = rep["run"]
    out = [f"# Run report {r['id']}", "",
           f"- Company: {r['company']}", f"- Task: {r['task']}", f"- Final status: **{r['status']}**",
           f"- Decision engine: {r['model_kind']} ({r['model']})", f"- Human input: {r['human_policy']}",
           f"- Model calls: {rep['usage']['model_calls']} · input tokens {rep['usage']['input_tokens']} · "
           f"output {rep['usage']['output_tokens']} · thinking {rep['usage']['thinking_tokens']}", ""]
    if r["model_kind"] != "gemini":
        out += ["> This run used a scripted model client for offline testing. It exercises the real apps, gate,",
                "> checker and verifier but is NOT evidence of live model autonomy.", ""]
    if rep["plan"]:
        out += ["## Plan", *[f"1. {s}" for s in rep["plan"].get("steps", [])], "",
                "Success criteria:", *[f"- {s}" for s in rep["plan"].get("success_criteria", [])], ""]
    v = rep["verification"] or {"cases": {}, "overall": "not verified", "unexpected_effects": []}
    out += ["## Outcomes (independently verified)", "", "| Case | Action | Decided by | Verification | Final state |",
            "|---|---|---|---|---|"]
    for case, c in v["cases"].items():
        out.append(f"| {case} | {c.get('action', '—')} | {c.get('decision_source', '—')} | {c['status']} | "
                   f"{c.get('disposition') or '—'} |")
    out += ["", f"Overall verification: **{v['overall']}**. Unexpected effects: {v['unexpected_effects'] or 'none'}", ""]
    out += ["## Agent proposal vs independent rules (every model proposal, including re-proposals; v = attempt)", "", "| Case | v | Agent proposal | Rules | Result | Differences |",
            "|---|---|---|---|---|---|"]
    for c in rep["comparisons"]:
        diffs = "; ".join(f"{d['field']}: {d['gemini']} vs {d['rules']}" for d in c["differences"]) or "—"
        out.append(f"| {c['case']} | {c['version']} | {c['gemini_action']} | {', '.join(c['rules_outcome'] or ['—'])} "
                   f"| {c['status']} | {diffs} |")
    out += ["", "## Human decisions", ""]
    for d in rep["decisions"]:
        resp = d["response"] or {}
        out.append(f"- {d['case_id'] or '-'} · {d['kind']} · {d['status']} · "
                   f"{resp.get('choice') or resp.get('answer') or ''} by {resp.get('actor', '-')}"
                   f"{' — ' + resp.get('reason', '') if resp.get('reason') else ''}")
    out += ["", "## Writes", ""]
    for w in rep["writes"]:
        out.append(f"- {w['case_id']} · {w['action_type']} {w['route']} · **{w['status']}** · "
                   f"{(w['result'] or {}).get('message', '')}")
    if rep["handoff"]:
        out += ["", "## Gemini capacity handoff", ""]
        for h in rep["handoff"]:
            out.append(f"- {h.get('reason')}: {len(h.get('recommendations', []))} cases left with rule recommendations")
    out += ["", "## Verification checks", ""]
    for case, c in v["cases"].items():
        out.append(f"### {case} — {c['status']}")
        out += [f"- {'✅' if ch['ok'] else '❌'} {ch['check']} {('· ' + ch['detail']) if ch.get('detail') else ''}"
                for ch in c["checks"]]
    if rep["screenshots"]:
        out += ["", "## Evidence screenshots", ""] + [f"- {s['label']}: `{s['path']}`" for s in rep["screenshots"]]
    return "\n".join(out) + "\n"
