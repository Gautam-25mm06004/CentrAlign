"""E-commerce integration adapter: ticket/order facts from trusted records and outcome
verification. Used only by the checker/verifier, never by the agent."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from operator_core import trusted
from shared.actions import fmt_inr

COMPANY = "ecommerce"
POLICY = yaml.safe_load((Path(__file__).parent / "policies.yaml").read_text(encoding="utf-8"))
TERMINAL = {"resolved", "closed_denied", "waiting_on_customer", "escalated"}


def scope() -> list[str]:
    snap = trusted.snapshot(COMPANY)
    return sorted(t["id"] for t in snap["tickets"] if t["status"] == "open")


def _lines(snap, customer_id):
    orders = {o["id"]: o for o in snap["orders"] if o["customer_id"] == customer_id}
    return [dict(line, delivered_at=orders[line["order_id"]]["delivered_at"]) for line in snap["order_lines"]
            if line["order_id"] in orders]


def facts(case_id: str, snap: dict | None = None) -> tuple[dict, dict]:
    snap = snap or trusted.snapshot(COMPANY)
    t = next((x for x in snap["tickets"] if x["id"] == case_id), None)
    if not t:
        return {}, {}
    f = {"requested_remedy": "replacement" if t["request_type"] == "replacement" else "refund"}
    exp: dict = {}
    lines = _lines(snap, t["customer_id"])
    candidates = []
    if t["linked_order_id"]:
        candidates = [ln for ln in lines if ln["order_id"] == t["linked_order_id"]]
    else:
        text = " ".join(m["body"] for m in snap["messages"] if m["ticket_id"] == case_id)
        ids = set(re.findall(r"\b([OL]-\d+)\b", text.upper()))
        candidates = [ln for ln in lines if ln["id"] in ids or ln["order_id"] in ids]
        if not candidates and len(lines) == 1:
            candidates = lines
    # Deterministic extraction cannot interpret free text such as "the yoga mat I ordered":
    # identity is covered only by an explicit reference or a single possible line.
    f["order_identified"] = len(candidates) == 1
    if len(candidates) != 1:
        return f, exp
    line = candidates[0]
    product = next(p for p in snap["products"] if p["id"] == line["product_id"])
    refunded = sum(r["amount"] for r in snap["refunds"] if r["order_line_id"] == line["id"])
    f["category_returnable"] = product["category"] not in POLICY.get("non_returnable_categories", [])
    # exact elapsed time (seconds / 86400), never rounded before the policy comparison; 7 days + 1 s is > 7
    f["days_since_delivery"] = (t["created_at"] - line["delivered_at"]) / 86400
    f["refundable_remaining_paise"] = line["paid_amount"] - refunded
    f["refund_amount_paise"] = line["paid_amount"] - refunded
    f["stock_available"] = product["stock"]
    exp["order_line_id"] = line["id"]
    exp["amount"] = f"{(line['paid_amount'] - refunded) / 100:.2f}"
    exp["quantity"] = str(line["qty"])
    return f, exp


def verify(case_id: str, writes: list[dict], before: dict, after: dict) -> dict:
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    committed = [w for w in writes if w["status"] == "committed"]
    t_after = next(x for x in after["tickets"] if x["id"] == case_id)
    new = lambda table: [r for r in after[table] if r["ticket_id"] == case_id and  # noqa: E731
                         r["id"] not in {b["id"] for b in before[table]}]
    refunds, repl, replies = new("refunds"), new("replacements"), new("replies")
    if not committed:
        check("ticket was handled", False, "no authorized write committed for this ticket")
        return {"status": "not_handled", "checks": checks, "disposition": None}
    auth_refunds = [w["fields"] for w in committed if w["action_type"] == "ecom.refund"]
    auth_repl = [w["fields"] for w in committed if w["action_type"] == "ecom.replace"]
    auth_replies = [w["fields"] for w in committed if w["action_type"] == "ecom.reply"]
    check("refund count matches authorization", len(refunds) == len(auth_refunds),
          f"{len(refunds)} saved, {len(auth_refunds)} authorized")
    for a in auth_refunds:
        check(f"refund of {fmt_inr(int(a['amount']))} on {a['order_line_id']} saved",
              any(r["order_line_id"] == a["order_line_id"] and int(r["amount"]) == int(a["amount"]) for r in refunds))
    check("replacement count matches authorization", len(repl) == len(auth_repl),
          f"{len(repl)} saved, {len(auth_repl)} authorized")
    lines = {ln["id"]: ln for ln in after["order_lines"]}
    for a in auth_repl:
        line = lines.get(a["order_line_id"])
        r = next((x for x in repl if x["order_line_id"] == a["order_line_id"]), None)
        ok = r is not None and line is not None and r["product_id"] == line["product_id"] \
            and int(r["quantity"]) == int(a["quantity"])
        check(f"replacement of {a['quantity']} x line {a['order_line_id']} saved with the line's product", ok,
              f"saved {r and (r['order_line_id'], r['product_id'], r['quantity'])}")
        if line is not None:
            # Stock is shared: several tickets may legitimately replace the same product in one run. The product's
            # stock must fall by exactly the total of all new replacements of that product (this one included).
            pid = line["product_id"]
            b_stock = next(p["stock"] for p in before["products"] if p["id"] == pid)
            a_stock = next(p["stock"] for p in after["products"] if p["id"] == pid)
            before_ids = {b["id"] for b in before["replacements"]}
            total_new = sum(int(x["quantity"]) for x in after["replacements"]
                            if x["id"] not in before_ids and x["product_id"] == pid)
            check("product stock reduced by all new replacements of it", b_stock - a_stock == total_new,
                  f"stock {b_stock} -> {a_stock}; new replacements of {pid}: {total_new}")
    check("customer reply sent", len(replies) >= 1 and len(auth_replies) >= 1, f"{len(replies)} reply(ies)")
    for a in auth_replies:
        check("reply text and status match authorization",
              any(" ".join(r["body"].split()) == a["body"] and r["new_status"] == a["new_status"] for r in replies))
    if auth_replies:
        check("ticket status updated", t_after["status"] == auth_replies[-1]["new_status"],
              f"ticket status {t_after['status']}")
    # obligations: a refund/replacement without a reply leaves the ticket unresolved
    if (auth_refunds or auth_repl) and not auth_replies:
        check("required reply sent", False, "refund/replacement done but reply outstanding")
    ok = all(c["ok"] for c in checks)
    disp = t_after["status"]
    return {"status": "verified" if ok else "failed", "checks": checks, "disposition": disp}


def unexpected_effects(before: dict, after: dict, cases_with_writes: set[str]) -> list[str]:
    out = []
    replaced = {}
    for r in after["replacements"]:
        if r["id"] not in {b["id"] for b in before["replacements"]}:
            replaced[r["product_id"]] = replaced.get(r["product_id"], 0) + int(r["quantity"])
    for p in after["products"]:
        b = next((x for x in before["products"] if x["id"] == p["id"]), None)
        if b and b["stock"] - p["stock"] != replaced.get(p["id"], 0):
            out.append(f"stock of {p['id']} changed by {b['stock'] - p['stock']} without matching replacements")
    for table in ("refunds", "replacements", "replies"):
        ids = {b["id"] for b in before[table]}
        for r in after[table]:
            if r["id"] not in ids and r["ticket_id"] not in cases_with_writes:
                out.append(f"unexpected {table[:-1]} on {r['ticket_id']}")
    return out
