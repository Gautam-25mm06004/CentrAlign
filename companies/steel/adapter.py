"""Steel integration adapter: deterministic invoice extraction, trusted-record facts and
outcome verification. Used only by the checker/verifier, never by the agent."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber

from operator_core import trusted
from shared.actions import fmt_inr, money_to_paise

INBOX = Path(__file__).parent / "inbox"
COMPANY = "steel"

PATTERNS = {
    "gstin": r"GSTIN:\s*([0-9A-Z]{15})",
    "invoice_no": r"Invoice No:\s*(\S+)",
    "po_no": r"PO Reference:\s*(PO-\d+)",
    "taxable": r"Taxable Value:\s*([\d,]+\.\d{2})",
    "gst": r"GST @ 18%:\s*([\d,]+\.\d{2})",
    "total": r"Invoice Total:\s*([\d,]+\.\d{2})",
    "line": r"Amount \(INR\)\n(.+?)\s+(\d+\.\d{3})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})",
}


def norm_invoice_no(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())


def scope() -> list[str]:
    return sorted(p.name for p in INBOX.glob("*.pdf"))


def extract(case_id: str) -> dict:
    path = INBOX / case_id
    if not path.exists():
        return {}
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:  # noqa: BLE001 - unsupported/corrupt document => no facts
        return {}
    out = {}
    for k, pat in PATTERNS.items():
        m = re.search(pat, text)
        if not m:
            continue
        if k == "line":
            out["grade"], out["quantity_t"] = m.group(1).strip(), m.group(2)
            out["rate_paise"] = money_to_paise(m.group(3))
            out["line_amount_paise"] = money_to_paise(m.group(4))
        elif k in ("taxable", "gst", "total"):
            out[f"{k}_paise"] = money_to_paise(m.group(1))
        else:
            out[k] = m.group(1)
    return out


def facts(case_id: str, snap: dict | None = None) -> tuple[dict, dict]:
    """Return (facts, expected_values). Missing facts stay absent so rules report coverage gaps."""
    snap = snap or trusted.snapshot(COMPANY)
    doc = extract(case_id)
    f: dict = {}
    exp: dict = {}
    if "total_paise" in doc:
        f["total_paise"] = doc["total_paise"]
        exp["total"] = f"{doc['total_paise'] / 100:.2f}"
    if "invoice_no" in doc:
        exp["invoice_no"] = doc["invoice_no"]
    if "grade" in doc:
        exp["grade"] = doc["grade"]
    if "taxable_paise" in doc:
        exp["subtotal"] = f"{doc['taxable_paise'] / 100:.2f}"
    if "gst_paise" in doc:
        exp["tax"] = f"{doc['gst_paise'] / 100:.2f}"
    vendor = next((v for v in snap["vendors"] if v["gstin"] == doc.get("gstin")), None) if doc.get("gstin") else None
    if vendor:
        f["vendor_status"] = vendor["status"]
        exp["vendor_id"] = vendor["id"]
        if "invoice_no" in doc:
            f["already_posted"] = any(
                p["vendor_id"] == vendor["id"] and p["invoice_no_norm"] == norm_invoice_no(doc["invoice_no"])
                and p["decision"] == "post" and p["source_doc"] != case_id  # this document's own record is not a duplicate
                for p in snap["processed_invoices"])
    if "po_no" in doc:
        po = next((p for p in snap["purchase_orders"] if p["id"] == doc["po_no"]), None)
        f["po_found"] = po is not None
        exp["po_no"] = doc["po_no"]
        if po and vendor:
            f["po_belongs_to_vendor"] = po["vendor_id"] == vendor["id"]
        if po and "grade" in doc:
            f["grade_matches"] = " ".join(po["grade"].lower().split()) == " ".join(doc["grade"].lower().split())
        if po and "quantity_t" in doc:
            try:
                q_po, q_inv = Decimal(po["tonnes"]), Decimal(doc["quantity_t"])
                f["quantity_deviation_pct"] = float(abs(q_inv - q_po) / q_po * 100) if q_po else None
            except InvalidOperation:
                pass
            exp["quantity_t"] = doc["quantity_t"]
        if po and "rate_paise" in doc:
            f["rate_matches"] = po["rate"] == doc["rate_paise"]
            exp["rate"] = f"{doc['rate_paise'] / 100:.2f}"
    if all(k in doc for k in ("quantity_t", "rate_paise", "taxable_paise", "gst_paise", "total_paise")):
        sub = int((Decimal(doc["quantity_t"]) * doc["rate_paise"]).to_integral_value())
        gst = int((Decimal(doc["taxable_paise"]) * Decimal("0.18")).to_integral_value())
        f["arithmetic_ok"] = (abs(sub - doc["taxable_paise"]) <= 100 and abs(gst - doc["gst_paise"]) <= 100
                              and doc["taxable_paise"] + doc["gst_paise"] == doc["total_paise"])
    return f, exp


GST_RATE = Decimal("0.18")


def invoice_arithmetic_problems(qty: str, rate_p: int, sub_p: int, tax_p: int, total_p: int) -> list[str]:
    """Quantity x rate = taxable value and 18% GST (each within one rupee); taxable + GST = total exactly."""
    out = []
    expect_sub = int((Decimal(qty) * rate_p).to_integral_value())
    if abs(expect_sub - sub_p) > 100:
        out.append(f"taxable value {fmt_inr(sub_p)} is not quantity x rate ({fmt_inr(expect_sub)})")
    expect_tax = int((Decimal(sub_p) * GST_RATE).to_integral_value())
    if abs(expect_tax - tax_p) > 100:
        out.append(f"GST {fmt_inr(tax_p)} is not 18% of the taxable value ({fmt_inr(expect_tax)})")
    if sub_p + tax_p != total_p:
        out.append(f"total {fmt_inr(total_p)} is not taxable value + GST ({fmt_inr(sub_p + tax_p)})")
    return out


def check_values(action: str, values: dict) -> list[str]:
    """Internal consistency of proposed values. A posting must reconcile arithmetically; holds and rejections
    record the invoice as printed (which may be exactly why it is held)."""
    if action != "post":
        return []
    from shared.actions import FieldError, normalize_field
    try:
        qty = normalize_field("qty3", values.get("quantity_t"))
        rate, sub, tax, total = (int(normalize_field("money", values.get(k))) for k in ("rate", "subtotal", "tax", "total"))
    except FieldError as e:
        return [str(e)]
    return invoice_arithmetic_problems(qty, rate, sub, tax, total)


def verify(case_id: str, writes: list[dict], before: dict, after: dict) -> dict:
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    before_ids = {p["id"] for p in before["processed_invoices"]}
    new_rows = [p for p in after["processed_invoices"] if p["id"] not in before_ids and p["source_doc"] == case_id]
    committed = [w for w in writes if w["status"] == "committed"]
    if not committed:
        check("a decision was recorded", bool(new_rows), "no authorized write committed for this invoice")
        return {"status": "not_handled" if not new_rows else "failed", "checks": checks, "disposition": None}
    w = committed[-1]["fields"]
    check("exactly one record saved for this document", len(new_rows) == 1, f"{len(new_rows)} new record(s)")
    if new_rows:
        r = new_rows[-1]
        for k, rk in (("vendor_id", "vendor_id"), ("invoice_no", "invoice_no"), ("po_no", "po_no"),
                      ("decision", "decision")):
            check(f"saved {k} matches authorized value", str(r[rk]) == str(w[k]), f"saved {r[rk]!r}, authorized {w[k]!r}")
        for k in ("total", "subtotal", "tax", "rate"):
            check(f"saved {k} matches authorized value", int(r[k]) == int(w[k]),
                  f"saved {fmt_inr(int(r[k]))}, authorized {fmt_inr(int(w[k]))}")
        from shared.actions import FieldError, normalize_field
        try:
            q_ok = normalize_field("qty3", r["quantity_t"]) == w["quantity_t"]
        except FieldError:
            q_ok = False
        check("saved quantity matches authorized value", q_ok, f"saved {r['quantity_t']}, authorized {w['quantity_t']}")
        check("saved grade matches authorized value", " ".join(str(r["grade"]).split()) == w["grade"],
              f"saved {r['grade']!r}, authorized {w['grade']!r}")
        check("record linked to this source document", r["source_doc"] == case_id == w["source_doc"],
              f"saved {r['source_doc']!r}")
        if w["decision"] == "post":
            probs = invoice_arithmetic_problems(normalize_field("qty3", r["quantity_t"]) if q_ok else w["quantity_t"],
                                                int(r["rate"]), int(r["subtotal"]), int(r["tax"]), int(r["total"]))
            check("posted record reconciles arithmetically", not probs, "; ".join(probs))
        ledger_new = [lg for lg in after["ledger"] if lg["processed_id"] == r["id"]]
        if w["decision"] == "post":
            check("exactly one ledger posting", len(ledger_new) == 1, f"{len(ledger_new)} posting(s)")
            if ledger_new:
                check("ledger amount equals authorized total", int(ledger_new[0]["amount"]) == int(w["total"]),
                      f"ledger {fmt_inr(int(ledger_new[0]['amount']))}, authorized {fmt_inr(int(w['total']))}")
        else:
            check("no ledger posting for a non-posted invoice", not ledger_new)
    ok = all(c["ok"] for c in checks)
    return {"status": "verified" if ok else "failed", "checks": checks, "disposition": w["decision"]}


def unexpected_effects(before: dict, after: dict, cases_with_writes: set[str]) -> list[str]:
    before_ids = {p["id"] for p in before["processed_invoices"]}
    out = [f"unexpected processed invoice {p['id']} for {p['source_doc']}" for p in after["processed_invoices"]
           if p["id"] not in before_ids and p["source_doc"] not in cases_with_writes]
    if len(after["ledger"]) - len(before["ledger"]) > sum(
            1 for p in after["processed_invoices"] if p["id"] not in before_ids and p["decision"] == "post"):
        out.append("more ledger postings than posted invoices")
    changed_vendors = [v["id"] for v in after["vendors"] if v not in before["vendors"]]
    if changed_vendors:
        out.append(f"vendor records changed: {changed_vendors}")
    return out
