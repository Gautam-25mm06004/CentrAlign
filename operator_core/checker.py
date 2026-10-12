"""Independent deterministic checker: a small typed rule engine plus comparison.

Facts come from company adapters that read source documents and trusted records
directly; the model's extracted values are never used as checker input.
"""
from __future__ import annotations

from shared.actions import FieldError, normalize_field

OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "in": lambda a, b: a in b,
    "is_true": lambda a, b: a is True,
    "is_false": lambda a, b: a is False,
}


def eval_cond(cond: dict, facts: dict) -> tuple[bool | None, list[str]]:
    """Returns (result, missing_facts). None means the condition could not be evaluated."""
    if "all" in cond:
        results = [eval_cond(c, facts) for c in cond["all"]]
        missing = [m for _, ms in results for m in ms]
        if any(r is False for r, _ in results):
            return False, []
        if any(r is None for r, _ in results):
            return None, missing
        return True, []
    if "any" in cond:
        results = [eval_cond(c, facts) for c in cond["any"]]
        if any(r is True for r, _ in results):
            return True, []
        missing = [m for _, ms in results for m in ms]
        return (None, missing) if any(r is None for r, _ in results) else (False, [])
    name = cond["fact"]
    if name not in facts or facts[name] is None:
        return None, [name]
    op = cond["op"]
    if op not in OPS:
        raise ValueError(f"unsupported rule operator {op}")
    return bool(OPS[op](facts[name], cond.get("value"))), []


def evaluate(policy: dict, facts: dict) -> dict:
    fired, unevaluated = [], []
    for rule in policy["rules"]:
        res, missing = eval_cond(rule["when"], facts)
        if res is None:
            unevaluated.append({"rule": rule["id"], "missing": missing, "effect": rule["effect"]})
        elif res:
            fired.append(rule)
    denied = sorted({a for r in fired for a in r.get("deny_actions", [])})
    by = lambda eff: [r for r in fired if r["effect"] == eff]  # noqa: E731
    terminal = by("hard") or by("clarify")
    blocking_gaps = [u for u in unevaluated if u["effect"] != "approval"]
    status = "determined"
    if terminal:
        outcomes = sorted({r["outcome"] for r in terminal})
    elif blocking_gaps:
        status, outcomes = "not_covered", []
    elif by("deny"):
        outcomes = sorted({r["outcome"] for r in by("deny")})
    elif by("hold"):
        outcomes = [policy.get("hold_outcome", "hold")]
    elif by("substitute"):
        outcomes = sorted({r["outcome"] for r in by("substitute")})
    else:
        if "default_outcome" in policy:
            outcomes = [policy["default_outcome"]]
        else:
            v = facts.get(policy.get("default_outcome_from_fact", ""))
            if v is None:
                status, outcomes = "not_covered", []
            else:
                outcomes = ["replace" if v == "replacement" else str(v)]
    if len(outcomes) > 1:
        conflict = True
    else:
        conflict = False
    approvals = [r for r in fired if r["effect"] == "approval"]
    return {
        "status": status,
        "policy_version": policy.get("version"),
        "allowed_actions": outcomes,
        "outcome": outcomes[0] if len(outcomes) == 1 else None,
        "conflict": conflict,
        "fired": [{"id": r["id"], "effect": r["effect"], "message": r.get("message", "")} for r in fired],
        "unevaluated": unevaluated,
        "denied_actions": denied,
        "hard_denied_actions": sorted({a for r in by("hard") for a in r.get("deny_actions", [])}),
        "approval_rules": [{"id": r["id"], "applies_to": r.get("applies_to", []), "message": r.get("message", "")}
                           for r in approvals],
        "facts": facts,
    }


def approval_needed(policy: dict, facts: dict, action: str, values: dict, value_facts: dict) -> list[dict]:
    """Evaluate approval rules for the action actually chosen, using the chosen values."""
    f = dict(facts)
    for vname, spec in (value_facts or {}).items():
        # "value: fact" means a money value (paise); "value: {fact, type}" declares int / number / money
        fname, ftype = (spec, "money") if isinstance(spec, str) else (spec["fact"], spec.get("type", "money"))
        if vname in values and values[vname] not in (None, ""):
            try:
                if ftype == "money":
                    f[fname] = int(normalize_field("money", values[vname]))
                elif ftype == "int":
                    f[fname] = int(normalize_field("int", values[vname]))
                else:
                    f[fname] = float(str(values[vname]).replace(",", ""))
            except (FieldError, ValueError):
                pass
    out = []
    for rule in policy["rules"]:
        if rule["effect"] != "approval" or action not in rule.get("applies_to", []):
            continue
        res, _ = eval_cond(rule["when"], f)
        if res:
            out.append({"id": rule["id"], "message": rule.get("message", "")})
    return out


def compare(proposal: dict, checker: dict, expected_values: dict, profile: dict) -> dict:
    """Compare Gemini's initial proposal with the checker's independent result."""
    if checker["status"] != "determined":
        return {"status": "not_covered", "action_match": None, "differences": [],
                "gaps": checker["unevaluated"]}
    action = proposal["action"]
    action_ok = action in checker["allowed_actions"]
    diffs = []
    relevant = set(profile["actions"].get(action, {}).get("values", {}))
    for name, ftype in profile.get("compare_values", {}).items():
        if name not in relevant or name not in expected_values or expected_values[name] is None:
            continue
        exp = expected_values[name]
        got = proposal["values"].get(name)
        try:
            g = normalize_field(ftype, got) if got not in (None, "") else None
            e = normalize_field(ftype, exp)
        except FieldError:
            g, e = str(got), str(exp)
        if g != e:
            diffs.append({"field": name, "gemini": got, "rules": exp})
    if not action_ok:
        diffs.insert(0, {"field": "action", "gemini": action, "rules": " or ".join(checker["allowed_actions"])})
    return {"status": "agree" if action_ok and not diffs else "disagree", "action_match": action_ok,
            "differences": diffs, "gaps": checker["unevaluated"]}
