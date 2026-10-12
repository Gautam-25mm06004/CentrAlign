"""Steel trading company — accounts app (sandbox).

Vendors, purchase orders, supplier invoice recording (post / hold / reject) and ledger.
Run: python -m apps.steel.app
"""
from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from apps.common import BusinessReject, SandboxApp
from shared.actions import FieldError, business_fields_from_form, fmt_inr, paise_to_str
from shared.config import PORTS, app_db, app_origin

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["inr"] = fmt_inr
APP = SandboxApp("steel", app_origin("steel"), app_db("steel"), "Steel Trading — Accounts")

LABELS = {
    False: {"invoice_no": "Supplier invoice number", "po_no": "Purchase order", "quantity_t": "Quantity (MT)",
            "rate": "Rate (₹ per MT)", "total": "Invoice total (₹)", "decision": "Decision"},
    # alternative wording used by the 'rename_labels' generalization fault
    True: {"invoice_no": "Vendor bill no.", "po_no": "PO reference", "quantity_t": "Billed tonnage",
           "rate": "Unit price per tonne", "total": "Gross amount payable", "decision": "Processing outcome"},
}


def render(request: Request, name: str, **ctx):
    ctx.setdefault("user", getattr(request.state, "sess", {}).get("user"))
    ctx["labels"] = LABELS[bool(APP.faults.rename_labels)]
    return TEMPLATES.TemplateResponse(request, name, ctx)


@APP.require_login
async def home(request: Request):
    conn = APP.db()
    stats = {
        "open_pos": conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE status='open'").fetchone()[0],
        "processed": conn.execute("SELECT COUNT(*) FROM processed_invoices").fetchone()[0],
        "ledger": conn.execute("SELECT COALESCE(SUM(amount),0) FROM ledger").fetchone()[0],
    }
    conn.close()
    return render(request, "home.html", stats=stats)


@APP.require_login
async def vendors(request: Request):
    q = request.query_params.get("q", "").strip()
    conn = APP.db()
    rows = conn.execute(
        "SELECT * FROM vendors WHERE ?='' OR name LIKE ? OR gstin LIKE ? OR id LIKE ? ORDER BY id",
        (q, f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    conn.close()
    return render(request, "vendors.html", rows=rows, q=q)


@APP.require_login
async def purchase_orders(request: Request):
    q = request.query_params.get("q", "").strip()
    conn = APP.db()
    rows = conn.execute(
        """SELECT p.*, v.name vendor_name FROM purchase_orders p JOIN vendors v ON v.id=p.vendor_id
           WHERE ?='' OR p.id LIKE ? OR v.name LIKE ? OR p.vendor_id LIKE ? ORDER BY p.id""",
        (q, f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    conn.close()
    return render(request, "pos.html", rows=rows, q=q)


@APP.require_login
async def processed(request: Request):
    q = request.query_params.get("q", "").strip()
    conn = APP.db()
    rows = conn.execute(
        """SELECT i.*, v.name vendor_name FROM processed_invoices i JOIN vendors v ON v.id=i.vendor_id
           WHERE ?='' OR i.invoice_no LIKE ? OR v.name LIKE ? OR i.vendor_id LIKE ? ORDER BY i.id DESC""",
        (q, f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
    conn.close()
    return render(request, "processed.html", rows=rows, q=q)


@APP.require_login
async def ledger(request: Request):
    conn = APP.db()
    rows = conn.execute(
        """SELECT l.*, i.invoice_no, i.vendor_id FROM ledger l JOIN processed_invoices i ON i.id=l.processed_id
           ORDER BY l.id DESC""").fetchall()
    conn.close()
    return render(request, "ledger.html", rows=rows)


@APP.require_login
async def invoice_form(request: Request):
    conn = APP.db()
    vendors_ = conn.execute("SELECT * FROM vendors ORDER BY name").fetchall()
    pos = conn.execute("SELECT * FROM purchase_orders ORDER BY id").fetchall()
    conn.close()
    return render(request, "invoice_form.html", vendors=vendors_, pos=pos,
                  csrf=request.state.sess["csrf"])


def norm_invoice_no(s: str) -> str:
    return "".join(ch for ch in s.upper() if ch.isalnum())


@APP.require_login
async def invoice_submit(request: Request):
    form = await request.form()
    try:
        fields = business_fields_from_form("steel.record_invoice", form.multi_items())
    except FieldError as e:
        return APP.result_page("Rejected", f"Invalid form: {e}", 422)
    reason = " ".join(str(form.get("reason", "")).split())

    def commit(conn):
        v = conn.execute("SELECT * FROM vendors WHERE id=?", (fields["vendor_id"],)).fetchone()
        if not v:
            raise BusinessReject("Unknown vendor")
        po = conn.execute("SELECT * FROM purchase_orders WHERE id=?", (fields["po_no"],)).fetchone()
        decision = fields["decision"]
        norm = norm_invoice_no(fields["invoice_no"])
        if decision == "post":
            if v["status"] != "active":
                raise BusinessReject("Vendor is blocked; posting is not allowed")
            if not po or po["vendor_id"] != v["id"]:
                raise BusinessReject("Purchase order not found for this vendor")
            dup = conn.execute(
                "SELECT 1 FROM processed_invoices WHERE vendor_id=? AND invoice_no_norm=? AND decision='post'",
                (v["id"], norm)).fetchone()
            if dup:
                raise BusinessReject("This supplier invoice was already posted")
            # three-way match and arithmetic, checked inside the save transaction against the current PO
            qty, rate, sub, tax, total_ = (Decimal(fields["quantity_t"]), int(fields["rate"]), int(fields["subtotal"]),
                                           int(fields["tax"]), int(fields["total"]))
            if " ".join(po["grade"].split()).casefold() != " ".join(fields["grade"].split()).casefold():
                raise BusinessReject(f"Grade differs from {po['id']} ({po['grade']})")
            if rate != po["rate"]:
                raise BusinessReject(f"Rate differs from {po['id']} ({fmt_inr(po['rate'])} per MT)")
            po_qty = Decimal(po["tonnes"])
            if po_qty <= 0 or abs(qty - po_qty) / po_qty * 100 > 2:
                raise BusinessReject(f"Quantity differs from {po['id']} ({po['tonnes']} MT) by more than 2%")
            if abs(int((qty * rate).to_integral_value()) - sub) > 100 or \
                    abs(int((Decimal(sub) * Decimal("0.18")).to_integral_value()) - tax) > 100 or sub + tax != total_:
                raise BusinessReject("Invoice amounts do not reconcile (quantity x rate, 18% GST, total)")
        if decision in ("hold", "reject_duplicate", "reject_blocked") and not reason:
            raise BusinessReject("A reason is required for holds and rejections")
        total = int(fields["total"])
        if APP.faults.corrupt_next_save and decision == "post":
            APP.faults.corrupt_next_save -= 1
            total += 10000  # deliberately wrong saved value (verifier test)
        cur = conn.execute(
            """INSERT INTO processed_invoices (source_doc, vendor_id, invoice_no, invoice_no_norm, po_no, grade,
               quantity_t, rate, subtotal, tax, total, decision, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fields["source_doc"], v["id"], fields["invoice_no"], norm, fields["po_no"], fields["grade"],
             fields["quantity_t"], int(fields["rate"]), int(fields["subtotal"]), int(fields["tax"]), total,
             decision, reason, time.time()))
        pid = cur.lastrowid
        ref = f"PI-{pid:05d}"
        msg = {"post": "Invoice posted to ledger.", "hold": "Invoice held for review.",
               "reject_duplicate": "Invoice rejected as duplicate.",
               "reject_blocked": "Invoice rejected (vendor blocked)."}[decision]
        if decision == "post":
            conn.execute("INSERT INTO ledger (processed_id, amount, posting_ref, created_at) VALUES (?,?,?,?)",
                         (pid, total, f"LG-{pid:05d}", time.time()))
            msg += f" Ledger amount {fmt_inr(total)}."
        return {"ref": ref, "message": msg, "processed_id": pid}

    return await APP.protected_write(request, action="steel.record_invoice", fields=fields, commit=commit)


# ---------------- trusted read-only API (checker / verifier only) ----------------
def rows_json(sql, args=()):
    conn = APP.db()
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return rows


@APP.require_trusted
async def t_snapshot(request: Request):
    return JSONResponse({
        "vendors": rows_json("SELECT * FROM vendors"),
        "purchase_orders": rows_json("SELECT * FROM purchase_orders"),
        "processed_invoices": rows_json("SELECT * FROM processed_invoices"),
        "ledger": rows_json("SELECT * FROM ledger"),
    })


@APP.require_trusted
async def t_idem(request: Request):
    key = request.path_params["key"]
    rows = rows_json("SELECT * FROM idempotency WHERE idem_key=?", (key,))
    return JSONResponse(rows[0] if rows else None)


async def login_get(request):
    return APP.login_page(request)


async def logout(request):
    return APP.logout(request)


async def root_redirect(request):
    return RedirectResponse("/")


app = Starlette(routes=[
    Route("/login", login_get, methods=["GET"]),
    Route("/login", APP.login_submit, methods=["POST"]),
    Route("/logout", logout, methods=["GET"]),
    Route("/", home),
    Route("/vendors", vendors),
    Route("/purchase-orders", purchase_orders),
    Route("/invoices", processed, methods=["GET"]),
    Route("/invoices", invoice_submit, methods=["POST"]),
    Route("/invoices/new", invoice_form),
    Route("/ledger", ledger),
    Route("/api/trusted/snapshot", t_snapshot),
    Route("/api/trusted/idempotency/{key}", t_idem),
    Route("/__faults", APP.faults_endpoint, methods=["GET", "POST"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORTS["steel"], log_level="warning")
