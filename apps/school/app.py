"""Coaching institute — admissions office portal (sandbox, third company).

Applications, batches with seat capacity, existing students, enrolments with fee and
concession, and messages to applicants.  Run: python -m apps.school.app
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
TEMPLATES.env.filters["ist"] = lambda ts: datetime.fromtimestamp(ts, IST).strftime("%d %b %Y, %H:%M") if ts else "—"
APP = SandboxApp("school", app_origin("school"), app_db("school"), "Admissions Office")


def render(request, name, **ctx):
    ctx.setdefault("user", getattr(request.state, "sess", {}).get("user"))
    return TEMPLATES.TemplateResponse(request, name, ctx)


def q(sql, args=()):
    conn = APP.db()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def batches_with_seats():
    return q("""SELECT b.*, (SELECT COUNT(*) FROM enrolments e WHERE e.batch_id=b.id) enrolled
                FROM batches b ORDER BY b.course, b.id""")


@APP.require_login
async def applications(request: Request):
    show = request.query_params.get("show", "pending")
    where = "a.status='pending'" if show == "pending" else "1=1"
    rows = q(f"SELECT a.* FROM applications a WHERE {where} ORDER BY a.submitted_at")
    return render(request, "applications.html", rows=rows, show=show)


@APP.require_login
async def application_detail(request: Request):
    aid = request.path_params["aid"].upper()
    a = q("SELECT * FROM applications WHERE id=?", (aid,))
    if not a:
        return APP.result_page("Not found", "No such application", 404)
    msgs = q("SELECT * FROM messages WHERE application_id=? ORDER BY at", (aid,))
    enr = q("SELECT e.*, b.name batch_name FROM enrolments e JOIN batches b ON b.id=e.batch_id WHERE e.application_id=?",
            (aid,))
    return render(request, "application.html", a=a[0], msgs=msgs, enr=enr, batches=batches_with_seats(),
                  csrf=request.state.sess["csrf"])


@APP.require_login
async def batches(request: Request):
    return render(request, "batches.html", rows=batches_with_seats())


@APP.require_login
async def students(request: Request):
    term = request.query_params.get("q", "").strip()
    rows = q("""SELECT s.*, GROUP_CONCAT(b.course || ' (' || b.id || ')', ', ') courses FROM students s
                LEFT JOIN enrolments e ON e.student_id=s.id LEFT JOIN batches b ON b.id=e.batch_id
                WHERE ?='' OR s.name LIKE ? OR s.phone LIKE ? OR s.email LIKE ? GROUP BY s.id ORDER BY s.id""",
             (term, f"%{term}%", f"%{term}%", f"%{term}%"))
    return render(request, "students.html", rows=rows, q=term)


@APP.require_login
async def fee_schedule(request: Request):
    return render(request, "fees.html", rows=q("SELECT * FROM batches ORDER BY course, id"))


async def _write(request, action, commit):
    form = await request.form()
    try:
        fields = business_fields_from_form(action, form.multi_items())
    except FieldError as e:
        return APP.result_page("Rejected", f"Invalid form: {e}", 422)
    if fields["application_id"] != request.path_params["aid"].upper():
        return APP.result_page("Rejected", "Application mismatch", 422)
    return await APP.protected_write(request, action=action, fields=fields, commit=lambda c: commit(c, fields))


@APP.require_login
async def enrol_submit(request: Request):
    def commit(conn, f):
        a = conn.execute("SELECT * FROM applications WHERE id=?", (f["application_id"],)).fetchone()
        b = conn.execute("SELECT * FROM batches WHERE id=?", (f["batch_id"],)).fetchone()
        if not a or not b:
            raise BusinessReject("Unknown application or batch")
        if b["course"] != a["course"]:
            raise BusinessReject("Batch is for a different course")
        if not (a["doc_marksheet"] and a["doc_id_proof"]):
            raise BusinessReject("Required documents are missing")
        taken = conn.execute("SELECT COUNT(*) FROM enrolments WHERE batch_id=?", (b["id"],)).fetchone()[0]
        if taken >= b["capacity"]:
            raise BusinessReject(f"Batch {b['id']} is full")
        dup = conn.execute("""SELECT 1 FROM enrolments e JOIN students s ON s.id=e.student_id JOIN batches x ON x.id=e.batch_id
                              WHERE (s.phone=? OR s.email=?) AND x.course=?""", (a["phone"], a["email"], a["course"])).fetchone()
        if dup:
            raise BusinessReject("Applicant is already enrolled in this course")
        pct = int(f["concession_pct"])
        if pct < 0 or pct > 50:
            raise BusinessReject("Concession must be between 0% and 50%")
        expected = b["fee"] * (100 - pct) // 100
        if abs(int(f["fee_payable"]) - expected) > 100:
            raise BusinessReject(f"Fee payable must be {fmt_inr(expected)} for {pct}% concession")
        cur = conn.execute("INSERT INTO students (name, phone, email, created_at) VALUES (?,?,?,?)",
                           (a["applicant_name"], a["phone"], a["email"], time.time()))
        sid = cur.lastrowid
        cur = conn.execute("""INSERT INTO enrolments (application_id, student_id, batch_id, concession_pct, fee_payable,
                              created_at) VALUES (?,?,?,?,?,?)""",
                           (a["id"], sid, b["id"], pct, int(f["fee_payable"]), time.time()))
        return {"ref": f"EN-{cur.lastrowid:05d}",
                "message": f"Enrolled in {b['name']} ({b['id']}); fee payable {fmt_inr(int(f['fee_payable']))}."}
    return await _write(request, "school.enrol", commit)


@APP.require_login
async def message_submit(request: Request):
    def commit(conn, f):
        a = conn.execute("SELECT * FROM applications WHERE id=?", (f["application_id"],)).fetchone()
        if not a:
            raise BusinessReject("Unknown application")
        if len(f["body"]) < 10:
            raise BusinessReject("Message is too short")
        if f["new_status"] == "enrolled" and not conn.execute(
                "SELECT 1 FROM enrolments WHERE application_id=?", (a["id"],)).fetchone():
            raise BusinessReject("Cannot mark as enrolled before the enrolment is saved")
        cur = conn.execute("INSERT INTO messages (application_id, body, new_status, at) VALUES (?,?,?,?)",
                           (a["id"], f["body"], f["new_status"], time.time()))
        conn.execute("UPDATE applications SET status=? WHERE id=?", (f["new_status"], a["id"]))
        return {"ref": f"MS-{cur.lastrowid:05d}", "message": f"Message sent; application is now {f['new_status']}."}
    return await _write(request, "school.message", commit)


def rows_json(sql, args=()):
    return [dict(r) for r in q(sql, args)]


@APP.require_trusted
async def t_snapshot(request):
    return JSONResponse({k: rows_json(f"SELECT * FROM {k}") for k in
                         ["applications", "batches", "students", "enrolments", "messages"]})


@APP.require_trusted
async def t_idem(request):
    rows = rows_json("SELECT * FROM idempotency WHERE idem_key=?", (request.path_params["key"],))
    return JSONResponse(rows[0] if rows else None)


async def login_get(request):
    return APP.login_page(request)


async def logout(request):
    return APP.logout(request)


async def root(request):
    return RedirectResponse("/applications")


app = Starlette(routes=[
    Route("/login", login_get, methods=["GET"]),
    Route("/login", APP.login_submit, methods=["POST"]),
    Route("/logout", logout),
    Route("/", root),
    Route("/applications", applications),
    Route("/applications/{aid}", application_detail),
    Route("/applications/{aid}/enrol", enrol_submit, methods=["POST"]),
    Route("/applications/{aid}/message", message_submit, methods=["POST"]),
    Route("/batches", batches),
    Route("/students", students),
    Route("/fees", fee_schedule),
    Route("/api/trusted/snapshot", t_snapshot),
    Route("/api/trusted/idempotency/{key}", t_idem),
    Route("/__faults", APP.faults_endpoint, methods=["GET", "POST"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORTS["school"], log_level="warning")
