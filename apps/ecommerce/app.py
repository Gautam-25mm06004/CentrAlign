"""E-commerce store — seller support admin panel (sandbox).

Tickets, customers, orders, inventory, refunds, replacements and customer replies.
Run: python -m apps.ecommerce.app
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

from apps.common import BusinessReject, SandboxApp
from shared.actions import FieldError, business_fields_from_form, fmt_inr
from shared.config import PORTS, app_db, app_origin

IST = timezone(timedelta(hours=5, minutes=30))
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["inr"] = fmt_inr
TEMPLATES.env.filters["ist"] = lambda ts: datetime.fromtimestamp(ts, IST).strftime("%d %b %Y, %H:%M IST") if ts else "—"
APP = SandboxApp("ecommerce", app_origin("ecommerce"), app_db("ecommerce"), "Store Support Desk")


def render(request: Request, name: str, **ctx):
    ctx.setdefault("user", getattr(request.state, "sess", {}).get("user"))
    ctx["alt"] = bool(APP.faults.rename_labels)
    return TEMPLATES.TemplateResponse(request, name, ctx)


def q(sql, args=()):
    conn = APP.db()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


@APP.require_login
async def tickets(request: Request):
    status = request.query_params.get("status", "open")
    where = "t.status IN ('open','waiting_on_customer')" if status == "open" else "1=1"
    rows = q(f"""SELECT t.*, c.name customer_name FROM tickets t JOIN customers c ON c.id=t.customer_id
                 WHERE {where} ORDER BY t.created_at""")
    return render(request, "tickets.html", rows=rows, status=status)


def lines_for_customer(cid):
    return q("""SELECT l.*, o.delivered_at, o.id order_id, p.name product_name, p.category, p.stock, p.id product_id,
                COALESCE((SELECT SUM(amount) FROM refunds r WHERE r.order_line_id=l.id),0) refunded
                FROM order_lines l JOIN orders o ON o.id=l.order_id JOIN products p ON p.id=l.product_id
                WHERE o.customer_id=? ORDER BY o.delivered_at""", (cid,))


@APP.require_login
async def ticket_detail(request: Request):
    tid = request.path_params["tid"].upper()
    t = q("SELECT t.*, c.name customer_name, c.email FROM tickets t JOIN customers c ON c.id=t.customer_id WHERE t.id=?", (tid,))
    if not t:
        return APP.result_page("Not found", "No such ticket", 404)
    t = t[0]
    msgs = q("SELECT * FROM messages WHERE ticket_id=? ORDER BY at", (tid,))
    replies = q("SELECT * FROM replies WHERE ticket_id=? ORDER BY at", (tid,))
    refunds = q("SELECT * FROM refunds WHERE ticket_id=?", (tid,))
    lines = lines_for_customer(t["customer_id"])
    return render(request, "ticket.html", t=t, msgs=msgs, replies=replies, refunds=refunds, lines=lines,
                  csrf=request.state.sess["csrf"])


@APP.require_login
async def customer(request: Request):
    cid = request.path_params["cid"].upper()
    c = q("SELECT * FROM customers WHERE id=?", (cid,))
    if not c:
        return APP.result_page("Not found", "No such customer", 404)
    return render(request, "customer.html", c=c[0], lines=lines_for_customer(cid))


@APP.require_login
async def order(request: Request):
    oid = request.path_params["oid"].upper()
    o = q("SELECT o.*, c.name customer_name FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.id=?", (oid,))
    if not o:
        return APP.result_page("Not found", "No such order", 404)
    lines = q("""SELECT l.*, p.name product_name, p.category, p.stock,
                 COALESCE((SELECT SUM(amount) FROM refunds r WHERE r.order_line_id=l.id),0) refunded
                 FROM order_lines l JOIN products p ON p.id=l.product_id WHERE l.order_id=?""", (oid,))
    return render(request, "order.html", o=o[0], lines=lines)


@APP.require_login
async def products(request: Request):
    return render(request, "products.html", rows=q("SELECT * FROM products ORDER BY id"))


async def _write(request: Request, action: str, commit):
    form = await request.form()
    try:
        fields = business_fields_from_form(action, form.multi_items())
    except FieldError as e:
        return APP.result_page("Rejected", f"Invalid form: {e}", 422)
    if fields["ticket_id"] != request.path_params["tid"].upper():
        return APP.result_page("Rejected", "Ticket mismatch", 422)
    return await APP.protected_write(request, action=action, fields=fields, commit=lambda c: commit(c, fields))


def _line_for_ticket(conn, fields):
    t = conn.execute("SELECT * FROM tickets WHERE id=?", (fields["ticket_id"],)).fetchone()
    if not t:
        raise BusinessReject("Unknown ticket")
    line = conn.execute("""SELECT l.*, o.customer_id FROM order_lines l JOIN orders o ON o.id=l.order_id
                           WHERE l.id=?""", (fields["order_line_id"],)).fetchone()
    if not line or line["customer_id"] != t["customer_id"]:
        raise BusinessReject("Order line does not belong to this ticket's customer")
    return t, line


@APP.require_login
async def refund_submit(request: Request):
    def commit(conn, f):
        t, line = _line_for_ticket(conn, f)
        refunded = conn.execute("SELECT COALESCE(SUM(amount),0) FROM refunds WHERE order_line_id=?",
                                (line["id"],)).fetchone()[0]
        amount = int(f["amount"])
        if amount <= 0 or amount > line["paid_amount"] - refunded:
            raise BusinessReject(f"Amount exceeds remaining refundable balance {fmt_inr(line['paid_amount'] - refunded)}")
        cur = conn.execute("INSERT INTO refunds (ticket_id, order_line_id, amount, created_at) VALUES (?,?,?,?)",
                           (t["id"], line["id"], amount, time.time()))
        return {"ref": f"RF-{cur.lastrowid:05d}", "message": f"Refund of {fmt_inr(amount)} issued."}
    return await _write(request, "ecom.refund", commit)


@APP.require_login
async def replace_submit(request: Request):
    def commit(conn, f):
        t, line = _line_for_ticket(conn, f)
        qty = int(f["quantity"])
        if qty <= 0 or qty > line["qty"]:
            raise BusinessReject("Invalid replacement quantity")
        p = conn.execute("SELECT * FROM products WHERE id=?", (line["product_id"],)).fetchone()
        if p["stock"] < qty:
            raise BusinessReject(f"Insufficient stock for {p['name']} (available {p['stock']})")
        conn.execute("UPDATE products SET stock=stock-?, version=version+1 WHERE id=?", (qty, p["id"]))
        cur = conn.execute("""INSERT INTO replacements (ticket_id, order_line_id, product_id, quantity, created_at)
                              VALUES (?,?,?,?,?)""", (t["id"], line["id"], p["id"], qty, time.time()))
        return {"ref": f"RP-{cur.lastrowid:05d}", "message": f"Replacement of {qty} × {p['name']} dispatched."}
    return await _write(request, "ecom.replace", commit)


@APP.require_login
async def reply_submit(request: Request):
    def commit(conn, f):
        t = conn.execute("SELECT * FROM tickets WHERE id=?", (f["ticket_id"],)).fetchone()
        if not t:
            raise BusinessReject("Unknown ticket")
        if len(f["body"]) < 10:
            raise BusinessReject("Reply is too short")
        cur = conn.execute("INSERT INTO replies (ticket_id, body, new_status, at) VALUES (?,?,?,?)",
                           (t["id"], f["body"], f["new_status"], time.time()))
        conn.execute("UPDATE tickets SET status=? WHERE id=?", (f["new_status"], t["id"]))
        return {"ref": f"RY-{cur.lastrowid:05d}", "message": f"Reply sent; ticket status is now {f['new_status']}."}
    return await _write(request, "ecom.reply", commit)


def rows_json(sql, args=()):
    return [dict(r) for r in q(sql, args)]


@APP.require_trusted
async def t_snapshot(request: Request):
    return JSONResponse({k: rows_json(f"SELECT * FROM {k}") for k in
                         ["customers", "products", "orders", "order_lines", "tickets", "messages",
                          "refunds", "replacements", "replies"]})


@APP.require_trusted
async def t_idem(request: Request):
    rows = rows_json("SELECT * FROM idempotency WHERE idem_key=?", (request.path_params["key"],))
    return JSONResponse(rows[0] if rows else None)


async def login_get(request):
    return APP.login_page(request)


async def logout(request):
    return APP.logout(request)


async def root(request):
    return RedirectResponse("/tickets")


app = Starlette(routes=[
    Route("/login", login_get, methods=["GET"]),
    Route("/login", APP.login_submit, methods=["POST"]),
    Route("/logout", logout),
    Route("/", root),
    Route("/tickets", tickets),
    Route("/tickets/{tid}", ticket_detail),
    Route("/tickets/{tid}/refund", refund_submit, methods=["POST"]),
    Route("/tickets/{tid}/replacement", replace_submit, methods=["POST"]),
    Route("/tickets/{tid}/reply", reply_submit, methods=["POST"]),
    Route("/customers/{cid}", customer),
    Route("/orders/{oid}", order),
    Route("/products", products),
    Route("/api/trusted/snapshot", t_snapshot),
    Route("/api/trusted/idempotency/{key}", t_idem),
    Route("/__faults", APP.faults_endpoint, methods=["GET", "POST"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORTS["ecommerce"], log_level="warning")
