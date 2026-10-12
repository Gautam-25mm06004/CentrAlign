"""School integration adapter: admission facts from trusted records and outcome verification.
Used only by the checker/verifier, never by the agent."""
from __future__ import annotations

from operator_core import trusted
from shared.actions import fmt_inr

COMPANY = "school"


def scope() -> list[str]:
    snap = trusted.snapshot(COMPANY)
    return sorted(a["id"] for a in snap["applications"] if a["status"] == "pending")


def _seats(snap):
    taken = {}
    for e in snap["enrolments"]:
        taken[e["batch_id"]] = taken.get(e["batch_id"], 0) + 1
    return {b["id"]: b["capacity"] - taken.get(b["id"], 0) for b in snap["batches"]}


def facts(case_id: str, snap: dict | None = None) -> tuple[dict, dict]:
    snap = snap or trusted.snapshot(COMPANY)
    a = next((x for x in snap["applications"] if x["id"] == case_id), None)
    if not a:
        return {}, {}
    students = {s["id"]: s for s in snap["students"]}
    batches = {b["id"]: b for b in snap["batches"]}
    seats = _seats(snap)
    course_batches = [b for b in snap["batches"] if b["course"] == a["course"]]
    f = {
        "already_enrolled_in_course": any(
            batches[e["batch_id"]]["course"] == a["course"] and e["application_id"] != case_id
            and (students[e["student_id"]]["phone"] == a["phone"] or students[e["student_id"]]["email"] == a["email"])
            for e in snap["enrolments"]),
        "documents_complete": bool(a["doc_marksheet"] and a["doc_id_proof"]),
        "seats_left_in_course": sum(max(seats[b["id"]], 0) for b in course_batches),
        "concession_pct": int(a["concession_requested_pct"] or 0),
    }
    exp: dict = {}
    pref = batches.get(a["preferred_batch"])
    choice = pref if pref and pref["course"] == a["course"] and seats[pref["id"]] > 0 else \
        next((b for b in course_batches if seats[b["id"]] > 0), None)
    if choice:
        pct = min(f["concession_pct"], 50)
        exp = {"batch_id": choice["id"], "concession_pct": str(pct),
               "fee_payable": f"{choice['fee'] * (100 - pct) // 100 / 100:.2f}"}
    return f, exp


def verify(case_id: str, writes: list[dict], before: dict, after: dict) -> dict:
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    committed = [w for w in writes if w["status"] == "committed"]
    app = next(x for x in after["applications"] if x["id"] == case_id)
    new = lambda t: [r for r in after[t] if r.get("application_id") == case_id and  # noqa: E731
                     r["id"] not in {b["id"] for b in before[t]}]
    enrol, msgs = new("enrolments"), new("messages")
    if not committed:
        check("application was handled", False, "no authorized write committed for this application")
        return {"status": "not_handled", "checks": checks, "disposition": None}
    auth_en = [w["fields"] for w in committed if w["action_type"] == "school.enrol"]
    auth_ms = [w["fields"] for w in committed if w["action_type"] == "school.message"]
    check("enrolment count matches authorization", len(enrol) == len(auth_en), f"{len(enrol)} saved, {len(auth_en)} authorized")
    for a in auth_en:
        e = next((x for x in enrol if x["batch_id"] == a["batch_id"]), None)
        check(f"enrolled in {a['batch_id']}", e is not None)
        if e:
            # identity link: the enrolment must belong to a NEW student record for this applicant
            stu = next((x for x in after["students"] if x["id"] == e["student_id"]), None)
            new_student = stu is not None and stu["id"] not in {x["id"] for x in before["students"]}
            same_person = stu is not None and (stu["name"], stu["phone"], stu["email"]) == \
                (app["applicant_name"], app["phone"], app["email"])
            check("enrolment linked to a new student record for this applicant", new_student and same_person,
                  f"student {e['student_id']}: {stu and (stu['name'], stu['phone'], stu['email'])}")
            batch = next((b for b in after["batches"] if b["id"] == e["batch_id"]), None)
            check("batch belongs to the applicant's course", batch is not None and batch["course"] == app["course"],
                  f"batch course {batch and batch['course']!r}, applied for {app['course']!r}")
            if batch is not None:
                taken = sum(1 for x in after["enrolments"] if x["batch_id"] == batch["id"])
                check("batch is not over capacity", taken <= batch["capacity"], f"{taken} of {batch['capacity']}")
            check("concession matches authorization", int(e["concession_pct"]) == int(a["concession_pct"]),
                  f"saved {e['concession_pct']}%, authorized {a['concession_pct']}%")
            check("fee payable matches authorization", int(e["fee_payable"]) == int(a["fee_payable"]),
                  f"saved {fmt_inr(int(e['fee_payable']))}, authorized {fmt_inr(int(a['fee_payable']))}")
    check("applicant was messaged", len(msgs) >= 1 and len(auth_ms) >= 1, f"{len(msgs)} message(s)")
    for a in auth_ms:
        check("message text and status match authorization",
              any(" ".join(m["body"].split()) == a["body"] and m["new_status"] == a["new_status"] for m in msgs))
    if auth_ms:
        check("application status updated", app["status"] == auth_ms[-1]["new_status"], f"status {app['status']}")
    if auth_en and not auth_ms:
        check("confirmation message sent", False, "enrolled but applicant not informed")
    ok = all(c["ok"] for c in checks)
    return {"status": "verified" if ok else "failed", "checks": checks, "disposition": app["status"]}


def unexpected_effects(before: dict, after: dict, cases_with_writes: set[str]) -> list[str]:
    out = []
    linked = {e["student_id"] for e in after["enrolments"]}
    old = {x["id"] for x in before["students"]}
    out += [f"student record {x['id']} created without an enrolment" for x in after["students"]
            if x["id"] not in old and x["id"] not in linked]
    changed = [b["id"] for b in after["batches"] if b not in before["batches"]]
    if changed:
        out.append(f"batch records changed: {changed}")
    for t in ("enrolments", "messages"):
        ids = {b["id"] for b in before[t]}
        for r in after[t]:
            if r["id"] not in ids and r.get("application_id") not in cases_with_writes:
                out.append(f"unexpected {t[:-1]} for {r.get('application_id')}")
    return out
