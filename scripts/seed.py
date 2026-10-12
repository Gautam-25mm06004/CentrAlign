"""Create fresh sandbox databases and the steel invoice inbox.

Usage:  python -m scripts.seed            # all fixtures
        python -m scripts.seed --steel inv_1042.pdf --ecom T-301   # subsets (used by the evaluator)
"""
from __future__ import annotations

import sys as _sys

for _s in (_sys.stdout, _sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

import argparse
import shutil
import time

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from apps.common import IDEMPOTENCY_DDL, connect
from evaluation import fixtures as fx
from shared.actions import fmt_inr
from shared.config import COMPANIES, app_db, load_secrets

STEEL_DDL = """
CREATE TABLE vendors (id TEXT PRIMARY KEY, name TEXT, gstin TEXT UNIQUE, status TEXT);
CREATE TABLE purchase_orders (id TEXT PRIMARY KEY, vendor_id TEXT REFERENCES vendors(id), grade TEXT,
    tonnes TEXT, rate INTEGER, status TEXT DEFAULT 'open', version INTEGER DEFAULT 1);
CREATE TABLE processed_invoices (id INTEGER PRIMARY KEY AUTOINCREMENT, source_doc TEXT, vendor_id TEXT,
    invoice_no TEXT, invoice_no_norm TEXT, po_no TEXT, grade TEXT, quantity_t TEXT, rate INTEGER,
    subtotal INTEGER, tax INTEGER, total INTEGER, decision TEXT, reason TEXT, created_at REAL);
CREATE UNIQUE INDEX one_posting_per_invoice ON processed_invoices(vendor_id, invoice_no_norm) WHERE decision='post';
CREATE TABLE ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, processed_id INTEGER UNIQUE REFERENCES processed_invoices(id),
    amount INTEGER, posting_ref TEXT UNIQUE, created_at REAL);
""" + IDEMPOTENCY_DDL

ECOM_DDL = """
CREATE TABLE customers (id TEXT PRIMARY KEY, name TEXT, email TEXT);
CREATE TABLE products (id TEXT PRIMARY KEY, name TEXT, category TEXT, price INTEGER, stock INTEGER, version INTEGER DEFAULT 1);
CREATE TABLE orders (id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers(id), delivered_at REAL, status TEXT DEFAULT 'delivered');
CREATE TABLE order_lines (id TEXT PRIMARY KEY, order_id TEXT REFERENCES orders(id), product_id TEXT REFERENCES products(id),
    qty INTEGER, paid_amount INTEGER);
CREATE TABLE tickets (id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers(id), subject TEXT, request_type TEXT,
    created_at REAL, linked_order_id TEXT, status TEXT DEFAULT 'open');
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, sender TEXT, body TEXT, at REAL);
CREATE TABLE refunds (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, order_line_id TEXT REFERENCES order_lines(id),
    amount INTEGER, created_at REAL);
CREATE TABLE replacements (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, order_line_id TEXT, product_id TEXT,
    quantity INTEGER, created_at REAL);
CREATE TABLE replies (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, body TEXT, new_status TEXT, at REAL);
""" + IDEMPOTENCY_DDL


def fresh_db(company: str, ddl: str):
    path = app_db(company)
    for suffix in ("", "-wal", "-shm"):
        p = path.with_name(path.name + suffix)
        if p.exists():
            p.unlink()
    conn = connect(path)
    conn.executescript(ddl)
    return conn


def make_invoice_pdf(path, d: dict):
    sub, tax, total = fx.invoice_amounts(d["qty"], d["rate"])
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    y = h - 60
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, y, d["printed_vendor"])
    c.setFont("Helvetica", 10)
    y -= 16
    c.drawString(50, y, f"GSTIN: {d['gstin']}")
    y -= 30
    c.setFont("Helvetica-Bold", 13)
    c.drawString(50, y, "TAX INVOICE")
    c.setFont("Helvetica", 10)
    rows = [
        ("Invoice No", d["invoice_no"]),
        ("Invoice Date", d["date"]),
        ("Bill To", "Steel Trading Company - Accounts Payable"),
        ("PO Reference", d["po"]),
    ]
    for k, v in rows:
        y -= 16
        c.drawString(50, y, f"{k}: {v}")
    y -= 30
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Description")
    c.drawString(260, y, "Quantity (MT)")
    c.drawString(360, y, "Rate (INR/MT)")
    c.drawString(470, y, "Amount (INR)")
    c.setFont("Helvetica", 10)
    y -= 16
    c.drawString(50, y, d["grade"])
    c.drawString(260, y, d["qty"])
    c.drawString(360, y, f"{d['rate'] / 100:,.2f}")
    c.drawString(470, y, f"{sub / 100:,.2f}")
    y -= 30
    for k, v in (("Taxable Value", sub), ("GST @ 18%", tax), ("Invoice Total", total)):
        c.drawString(330, y, f"{k}:")
        c.drawRightString(545, y, f"{v / 100:,.2f}")
        y -= 16
    y -= 10
    c.drawString(50, y, f"Amount in figures: INR {fmt_inr(total)[1:]}")
    if d.get("note"):
        y -= 30
        c.setFont("Helvetica-Oblique", 9)
        import textwrap
        for i, part in enumerate(textwrap.wrap(d["note"], 105)):
            c.drawString(50, y - 12 * i, part)
    c.setFont("Helvetica", 8)
    c.drawString(50, 50, "Payment terms: 30 days. Bank: Synthetic Bank Ltd. This is a synthetic sandbox document.")
    c.save()


def seed_steel(invoices: list[str] | None = None):
    conn = fresh_db("steel", STEEL_DDL)
    conn.executemany("INSERT INTO vendors VALUES (?,?,?,?)", fx.STEEL_VENDORS)
    conn.executemany("INSERT INTO purchase_orders (id, vendor_id, grade, tonnes, rate) VALUES (?,?,?,?,?)",
                     fx.STEEL_POS)
    for p in fx.STEEL_PREPOSTED:
        norm = "".join(ch for ch in p["invoice_no"].upper() if ch.isalnum())
        cur = conn.execute(
            """INSERT INTO processed_invoices (source_doc, vendor_id, invoice_no, invoice_no_norm, po_no, grade,
               quantity_t, rate, subtotal, tax, total, decision, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p["source_doc"], p["vendor_id"], p["invoice_no"], norm, p["po_no"], p["grade"], p["quantity_t"],
             p["rate"], p["subtotal"], p["tax"], p["total"], p["decision"], p["reason"], time.time() - 86400 * 10))
        conn.execute("INSERT INTO ledger (processed_id, amount, posting_ref, created_at) VALUES (?,?,?,?)",
                     (cur.lastrowid, p["total"], f"LG-{cur.lastrowid:05d}", time.time() - 86400 * 10))
    conn.close()
    inbox = COMPANIES / "steel" / "inbox"
    if inbox.exists():
        shutil.rmtree(inbox)
    inbox.mkdir(parents=True)
    for name, d in fx.STEEL_INVOICES.items():
        if invoices is None or name in invoices:
            make_invoice_pdf(inbox / name, d)


def seed_ecom(tickets: list[str] | None = None):
    conn = fresh_db("ecommerce", ECOM_DDL)
    conn.executemany("INSERT INTO customers VALUES (?,?,?)", fx.ECOM_CUSTOMERS)
    conn.executemany("INSERT INTO products (id, name, category, price, stock) VALUES (?,?,?,?,?)", fx.ECOM_PRODUCTS)
    for oid, cid, delivered, lines in fx.ECOM_ORDERS:
        conn.execute("INSERT INTO orders (id, customer_id, delivered_at) VALUES (?,?,?)", (oid, cid, fx.ts(delivered)))
        for lid, pid, qty, paid in lines:
            conn.execute("INSERT INTO order_lines VALUES (?,?,?,?,?)", (lid, oid, pid, qty, paid))
    for tid, cid, subj, rtype, created, linked, msg in fx.ECOM_TICKETS:
        status = "open" if tickets is None or tid in tickets else "resolved"
        conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?)", (tid, cid, subj, rtype, fx.ts(created), linked, status))
        conn.execute("INSERT INTO messages (ticket_id, sender, body, at) VALUES (?,?,?,?)",
                     (tid, "customer", msg, fx.ts(created)))
    conn.close()


SCHOOL_DDL = """
CREATE TABLE batches (id TEXT PRIMARY KEY, course TEXT, name TEXT, timing TEXT, capacity INTEGER, fee INTEGER);
CREATE TABLE students (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, phone TEXT, email TEXT, created_at REAL);
CREATE TABLE applications (id TEXT PRIMARY KEY, applicant_name TEXT, phone TEXT, email TEXT, course TEXT,
    preferred_batch TEXT, doc_marksheet INTEGER, doc_id_proof INTEGER, concession_requested_pct INTEGER,
    concession_reason TEXT, note TEXT, submitted_at REAL, status TEXT DEFAULT 'pending');
CREATE TABLE enrolments (id INTEGER PRIMARY KEY AUTOINCREMENT, application_id TEXT, student_id INTEGER REFERENCES students(id),
    batch_id TEXT REFERENCES batches(id), concession_pct INTEGER, fee_payable INTEGER, created_at REAL);
CREATE UNIQUE INDEX one_enrolment_per_application ON enrolments(application_id) WHERE application_id IS NOT NULL;
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, application_id TEXT, body TEXT, new_status TEXT, at REAL);
""" + IDEMPOTENCY_DDL


def seed_school(applications: list[str] | None = None):
    conn = fresh_db("school", SCHOOL_DDL)
    n = 0
    for bid, course, name, timing, cap, fee, already in fx.SCHOOL_BATCHES:
        conn.execute("INSERT INTO batches VALUES (?,?,?,?,?,?)", (bid, course, name, timing, cap, fee))
        for _ in range(already - sum(1 for e in fx.SCHOOL_EXISTING if e[3] == bid)):
            n += 1
            cur = conn.execute("INSERT INTO students (name, phone, email, created_at) VALUES (?,?,?,?)",
                               (f"Enrolled student {n:03d}", f"+91 90000 {n:05d}", f"student{n:03d}@example.com", 0))
            conn.execute("INSERT INTO enrolments (application_id, student_id, batch_id, concession_pct, fee_payable,"
                         " created_at) VALUES (NULL,?,?,0,?,0)", (cur.lastrowid, bid, fee))
    for name, phone, email, bid in fx.SCHOOL_EXISTING:
        fee = next(b[5] for b in fx.SCHOOL_BATCHES if b[0] == bid)
        cur = conn.execute("INSERT INTO students (name, phone, email, created_at) VALUES (?,?,?,?)", (name, phone, email, 0))
        conn.execute("INSERT INTO enrolments (application_id, student_id, batch_id, concession_pct, fee_payable, created_at)"
                     " VALUES (NULL,?,?,0,?,0)", (cur.lastrowid, bid, fee))
    for a in fx.SCHOOL_APPLICATIONS:
        status = "pending" if applications is None or a[0] in applications else "closed"
        conn.execute("INSERT INTO applications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (*a[:11], fx.ts(a[11]), status))
    conn.close()


SEEDERS = {"steel": seed_steel, "ecommerce": seed_ecom, "school": seed_school}  # sandbox construction, per app


def seed(company: str, cases: list[str] | None = None):
    SEEDERS[company](cases)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--steel", nargs="*")
    ap.add_argument("--ecom", nargs="*")
    ap.add_argument("--school", nargs="*")
    a = ap.parse_args()
    load_secrets()
    seed_steel(a.steel)
    seed_ecom(a.ecom)
    seed_school(a.school)
    print("Seeded steel, e-commerce and school sandboxes.")


if __name__ == "__main__":
    main()
