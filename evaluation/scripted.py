"""Scripted model client for OFFLINE tests (no Gemini calls).

It drives the real runtime — real browser, real sandbox apps, real gate, checker and verifier —
by reading the same observations and tool results a model would receive. It exists to test the
mechanisms when Gemini is unavailable. Runs using it are labelled 'scripted' everywhere and are
never reported as live autonomy. `mistakes` lets tests inject a model error (e.g. treating a 5%
quantity deviation as acceptable) to exercise the disagreement path.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from operator_core import store
from operator_core.llm import Budget, ModelTurn

IST = timezone(timedelta(hours=5, minutes=30))


def C(_tool, **args):
    return {"name": _tool, "args": args}


class ScriptedModel:
    kind = "scripted"

    def __init__(self, run_id: str, mistakes: set[str] | None = None):
        self.run_id = run_id
        self.mistakes = mistakes or set()
        self.budget = Budget(run_id)
        self.r = None
        self.gen = None

    def attach(self, runner):
        self.r = runner
        company = runner.company["id"]
        self.gen = {"steel": self.steel, "ecommerce": self.ecommerce, "school": self.school}[company]()

    # ---- model interface
    def generate(self, system, user, tools, kind="step"):
        self.budget.admit()
        cid = store.record_call(self.run_id, "dispatched", "scripted")
        try:
            calls = next(self.gen)
        except StopIteration:
            calls = [C("request_completion", summary="Scripted agent finished.")]
        store.update_call(cid, status="ok", input_tokens=len(user) // 4, output_tokens=40)
        return ModelTurn(calls=calls if isinstance(calls, list) else [calls])

    # ---- helpers reading what a model would see
    @property
    def last(self) -> str:
        return self.r.steps[-1]["result"] if self.r.steps else ""

    def els(self):
        return self.r.browser.last_obs["elements"]

    def ref(self, name=None, kind=None, label=None, form=None):
        for e in self.els():
            if name and e.get("name") != name:
                continue
            if kind and e["kind"] != kind:
                continue
            if label and label.lower() not in e["label"].lower():
                continue
            if form and form.lower() not in (e.get("form") or "").lower():
                continue
            return e["ref"]
        raise RuntimeError(f"scripted: element not found name={name} kind={kind} label={label} form={form}")

    def rows(self, table=0):
        t = self.r.browser.last_obs["tables"]
        return [r.split(" | ") for r in t[table][1:]] if len(t) > table else []

    def signed_in_guard(self):
        """If the last result shows the session expired, sign in again (adaptation)."""
        for _ in range(3):
            if "not signed in" in self.last or "Session expired" in self.last:
                yield C("sign_in")

    def perform(self, authorized_text: str, forms: dict, form_page):
        """Perform every authorized write through the normal forms; adapt to session expiry/uncertainty."""
        writes = re.findall(r"\d+\. POST (\S+) \[(\S+)\] with (.*?)  \(authorization", authorized_text)
        for route, action, fieldtxt in writes:
            fields = {k: json.loads(v) for k, v in re.findall(r'(\w+)=("(?:[^"\\]|\\.)*")', fieldtxt)}
            for attempt in range(3):
                yield C("open_page", path=form_page(route))
                yield from self.signed_in_guard()
                fill = []
                for k, v in fields.items():
                    if k in ("ticket_id", "application_id"):
                        continue
                    fill.append({"ref": self.ref(name=k, form=forms.get(action)), "value": v})
                if action == "steel.record_invoice":
                    fill.append({"ref": self.ref(name="reason"), "value": fields.get("decision") + " per policy check"})
                btn = self.ref(kind="button", form=forms.get(action)) if forms.get(action) else \
                    self.ref(kind="button", label="Save")
                yield [C("fill", fields=fill), C("click", ref=btn)]
                if "not signed in" in self.last or "Session expired" in self.last or "HTTP 401" in self.last:
                    yield C("sign_in")
                    continue
                if "not_saved_after_reconciliation" in self.last:
                    continue
                break

    # ------------------------------------------------------------ steel
    def steel(self):
        yield [C("set_plan", steps=["List inbox invoices", "For each: read PDF, check vendor (by GSTIN), duplicates, PO",
                                    "Propose post/hold/reject", "Record the authorized decision", "Request verification"],
                 success_criteria=["Every inbox invoice has exactly one recorded decision",
                                   "Posted invoices have one ledger entry with the invoice total"]),
               C("list_files")]
        files = re.findall(r"inv_\d+\.pdf", self.last)
        for f in files:
            yield C("read_document", name=f)
            doc = self.last
            g = lambda pat: (re.search(pat, doc) or [None, None])[1]  # noqa: E731
            gstin, inv, po = g(r"GSTIN: (\S+)"), g(r"Invoice No: (\S+)"), g(r"PO Reference: (\S+)")
            m = re.search(r"Amount \(INR\)\n(.+?)\s+(\d+\.\d{3})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})", doc)
            grade, qty, rate = m.group(1), m.group(2), m.group(3).replace(",", "")
            sub = g(r"Taxable Value: ([\d,.]+)").replace(",", "")
            tax = g(r"GST @ 18%: ([\d,.]+)").replace(",", "")
            total = g(r"Invoice Total: ([\d,.]+)").replace(",", "")
            yield C("open_page", path=f"/vendors?q={gstin}")
            yield from self.signed_in_guard()
            vrow = self.rows()[0]
            vendor_id, vstatus = vrow[0], vrow[3]
            yield C("open_page", path=f"/invoices?q={inv}")
            dup = any(r[1] == inv and r[2].endswith(f"({vendor_id})") and r[5] == "post" for r in self.rows()
                      if len(r) > 5)
            yield C("open_page", path=f"/purchase-orders?q={po}")
            prow = next((r for r in self.rows() if r[0] == po), None)
            yield C("note", case_id=f, key="vendor", value=f"{vendor_id} ({vstatus})", source=f"Vendors page, GSTIN {gstin}")
            action, why = "post", "Matches PO, vendor active, not a duplicate."
            if vstatus == "blocked":
                action, why = "reject_blocked", "Vendor is blocked."
            elif dup:
                action, why = "reject_duplicate", "Same invoice already posted."
            elif not prow or prow[2] != vendor_id:
                action, why = "hold", "PO missing or belongs to another vendor."
            else:
                po_qty = float(prow[4])
                dev = abs(float(qty) - po_qty) / po_qty * 100
                po_rate = prow[5].replace("₹", "").replace(",", "")
                if dev > 2 and "accept_quantity_deviation" not in self.mistakes:
                    action, why = "hold", f"Quantity deviates {dev:.1f}% from PO."
                elif float(po_rate) != float(rate):
                    action, why = "hold", "Rate differs from PO."
            vals = dict(vendor_id=vendor_id, invoice_no=inv, po_no=po, grade=grade, quantity_t=qty, rate=rate,
                        subtotal=sub, tax=tax, total=total, reason=why)
            yield C("propose_decision", case_id=f, action=action, rationale=why,
                    values=[{"name": k, "value": v} for k, v in vals.items()], evidence=[f, f"/vendors?q={gstin}"])
            res = self.last
            if res.startswith("HUMAN DECISION") and "propose" in res:
                rules_action = re.search(r"outcome '(\w+)'", res).group(1)
                yield C("propose_decision", case_id=f, action=rules_action, rationale="Following rules outcome",
                        values=[{"name": k, "value": v} for k, v in vals.items()], evidence=[f])
                res = self.last
            if res.startswith("AUTHORIZED"):
                yield from self.perform(res, {"steel.record_invoice": None}, lambda route: "/invoices/new")
        yield C("request_completion", summary="All inbox invoices processed.")
        yield C("request_completion", summary="All inbox invoices processed.")

    # ------------------------------------------------------------ e-commerce
    def ecommerce(self):
        yield [C("set_plan", steps=["Open the ticket list", "For each ticket: identify order line, apply policy",
                                    "Propose action with reply", "Perform authorized writes", "Request verification"],
                 success_criteria=["Each open ticket has a policy-compliant outcome and a reply"]),
               C("open_page", path="/tickets")]
        yield from self.signed_in_guard()
        tickets = [r[0] for r in self.rows()]
        forms = {"ecom.refund": "Issue refund", "ecom.replace": "Send replacement", "ecom.reply": "Reply to customer"}
        for t in tickets:
            yield C("open_page", path=f"/tickets/{t}")
            yield from self.signed_in_guard()
            text = self.r.browser.last_obs["text"]
            rtype = re.search(r"Customer request: (\w+)|Requested remedy: (\w+)", text)
            rtype = (rtype.group(1) or rtype.group(2)) if rtype else "return_refund"
            received = re.search(r"Received: (\d\d \w{3} \d{4}, \d\d:\d\d) IST", text).group(1)
            linked = re.search(r"Order referenced: (O-\d+)", text)
            lines = [r for r in self.rows(0)]
            msg = text.lower()
            if linked:
                cand = [r for r in lines if r[1] == linked.group(1)]
            else:
                words = [w for w in re.findall(r"[a-z]+", msg) if len(w) > 3]
                cand = [r for r in lines if any(w in r[2].lower() for w in words)]
            if len(cand) != 1:
                vals = {"reply_text": "Thanks for reaching out. You have more than one matching order — could you "
                                      "tell us the order number so we can help?"}
                action, why = "clarify", f"{len(cand)} possible order lines; cannot identify the order."
            else:
                line = cand[0]
                lid, category = line[0], line[3]
                delivered = datetime.strptime(line[4].replace(" IST", ""), "%d %b %Y, %H:%M")
                req = datetime.strptime(received, "%d %b %Y, %H:%M")
                days = (req - delivered).total_seconds() / 86400
                paid = float(line[5].replace("₹", "").replace(",", ""))
                refunded = float(line[6].replace("₹", "").replace(",", ""))
                stock = int(line[7])
                amount = f"{paid - refunded:.2f}"
                if category == "innerwear":
                    action, why = "deny", "Innerwear is non-returnable."
                    vals = {"order_line_id": lid, "reply_text": "We're sorry, innerwear items cannot be returned "
                                                                "under our store policy."}
                elif days > 7:
                    action, why = "deny", f"Requested {days:.1f} days after delivery (limit 7)."
                    vals = {"order_line_id": lid, "reply_text": "We're sorry, this request falls outside our 7-day "
                                                                "return window, so we can't accept the return."}
                elif rtype == "replacement" and stock > 0:
                    action, why = "replace", "Eligible replacement, stock available."
                    vals = {"order_line_id": lid, "quantity": "1", "reply_text": "A replacement is on its way."}
                else:
                    action = "refund"
                    why = "Eligible return." if rtype != "replacement" else "Replacement out of stock; refunding."
                    vals = {"order_line_id": lid, "amount": amount,
                            "reply_text": f"We've issued a refund of ₹{amount} for your order. Sorry for the trouble."}
            yield C("propose_decision", case_id=t, action=action, rationale=why,
                    values=[{"name": k, "value": v} for k, v in vals.items()], evidence=[f"/tickets/{t}"])
            res = self.last
            if res.startswith("AUTHORIZED"):
                yield from self.perform(res, forms, lambda route: "/" + "/".join(route.strip("/").split("/")[:2]))
        yield C("request_completion", summary="All open tickets handled.")
        yield C("request_completion", summary="All open tickets handled.")

    # ------------------------------------------------------------ school (third company; test client only)
    def school(self):
        yield [C("set_plan", steps=["Open pending applications", "For each: duplicates, documents, seats, concession",
                                    "Propose and perform the decision", "Request verification"],
                 success_criteria=["Every pending application is enrolled, waitlisted, rejected or awaiting documents"]),
               C("open_page", path="/applications")]
        yield from self.signed_in_guard()
        apps = [r[0] for r in self.rows()]
        forms = {"school.enrol": "Enrol in a batch", "school.message": "Message the applicant"}
        for aid in apps:
            yield C("open_page", path=f"/applications/{aid}")
            yield from self.signed_in_guard()
            t = self.r.browser.last_obs["text"]
            course = re.search(r"Course: (.+?)\s+Preferred batch:", t).group(1).strip()
            pref = re.search(r"Preferred batch: (B-\d+)", t).group(1)
            phone = re.search(r"Phone: (\+[\d ]+?)\s+Email", t).group(1).strip()
            email = re.search(r"Email: (\S+)", t).group(1)
            docs_ok = "missing" not in t.split("Documents received:")[1].split("Fee concession")[0]
            pct = int(re.search(r"Fee concession requested: (\d+)%", t).group(1))
            seats = [(r[0], int(r[3].split(" of ")[0]), float(r[4].replace("₹", "").replace(",", ""))) for r in self.rows(0)]
            yield C("open_page", path=f"/students?q={phone.replace(' ', '%20').replace('+', '%2B')}")
            dup = any(len(r) > 4 and course in r[4] for r in self.rows())
            yield C("open_page", path=f"/students?q={email}")
            dup = dup or any(len(r) > 4 and course in r[4] for r in self.rows())
            name = "Applicant"
            if dup:
                action, vals = "reject_duplicate", {"message": f"Dear {name}, our records show you are already enrolled in {course}."}
            elif not docs_ok:
                action, vals = "request_documents", {"message": "Please submit your Class X marksheet and photo ID proof to complete admission."}
            else:
                free = [s for s in seats if s[1] > 0]
                pick = next((s for s in free if s[0] == pref), free[0] if free else None)
                if not pick:
                    action, vals = "waitlist", {"message": f"All {course} batches are full; you are on the waitlist."}
                else:
                    fee = pick[2] * (100 - pct) / 100
                    action, vals = "enrol", {"batch_id": pick[0], "concession_pct": str(pct), "fee_payable": f"{fee:.2f}",
                                             "message": f"Welcome! You are enrolled in batch {pick[0]}. Fee payable: Rs {fee:.2f}."}
            yield C("propose_decision", case_id=aid, action=action, rationale=f"{action} per admissions policy",
                    values=[{"name": k, "value": v} for k, v in vals.items()], evidence=[f"/applications/{aid}"])
            res = self.last
            if res.startswith("AUTHORIZED"):
                yield from self.perform(res, forms, lambda route: "/" + "/".join(route.strip("/").split("/")[:2]))
        yield C("request_completion", summary="All pending applications processed.")
        yield C("request_completion", summary="All pending applications processed.")
