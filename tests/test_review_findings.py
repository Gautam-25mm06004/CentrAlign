"""Regression tests for the external review (findings D1-D10 and R1-R6). No model calls.
Each test is the review's acceptance test, run against the real sandbox apps, gate, browser and verifier.
Requires the sandbox apps to be running.

  python -m unittest tests.test_review_findings -v
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

from operator_core import checker, runner as runner_mod, store, trusted, verifier
from operator_core.authz import Gate, Human
from operator_core.browser import Browser
from operator_core.company import load as load_company
from operator_core.context import PromptTooLarge, build_user_prompt
from operator_core.llm import Budget, ModelTurn, QuotaExhausted
from scripts.seed import seed_ecom, seed_school, seed_steel
from shared import capability
from shared.actions import FieldError, normalize, normalize_field
from shared.config import DATA, ROOT, RUNS, app_db, load_secrets

STEEL = "http://127.0.0.1:8101"
INV = dict(vendor_id="V-101", invoice_no="ERM/24-25/0912", po_no="PO-4501", grade="IS 2062 E250BR plates",
           quantity_t="6.000", rate="52000", subtotal="312000", tax="56160", total="368160", reason="test")
REFUND = dict(order_line_id="L-1", amount="1499.00", reply_text="We have refunded your kurta. Sorry for the trouble.")


def new_run(company, cases, task="review test", human="auto", read_only=False):
    store.init()
    rid = store.create_run(company, task, "none", "scripted", human, label="review-test", read_only=read_only)
    app = load_company(company)["profile"]["app"]
    store.update_run(rid, scope={"cases": cases, "before": trusted.snapshot(app)})
    return rid


def db_exec(company, sql, args=()):
    c = sqlite3.connect(app_db(company))
    c.execute(sql, args)
    c.commit()
    c.close()


class Harness(unittest.TestCase):
    """A run, its gate and a signed-in controlled browser for one company."""
    company = "steel"
    cases = ["inv_1042.pdf"]
    human = "auto"
    task = "review test"
    read_only = False

    def seed(self):
        seed_steel(["inv_1042.pdf"])

    def setUp(self):
        self.seed()
        trusted.set_faults(load_company(self.company)["profile"]["app"], {"reset": True})
        self.rid = new_run(self.company, self.cases, self.task, self.human, self.read_only)
        self.co = load_company(self.company)
        self.gate = Gate(self.rid, self.co, Human(self.rid, self.human), agent_source="scripted")
        (RUNS / self.rid).mkdir(parents=True, exist_ok=True)
        self.b = Browser(self.co["profile"], RUNS / self.rid, self.gate)
        self.b.start()
        self.b.sign_in()

    def tearDown(self):
        self.b.close()
        store.delete_run(self.rid)
        shutil.rmtree(RUNS / self.rid, ignore_errors=True)

    def propose(self, case, action, values):
        return self.gate.propose(case, action, values, [], "test", self.cases)

    def submit(self, page, route, fields):
        """Fill the application's own form and click its submit button (goes through the request guard)."""
        p = self.b.page
        p.goto(self.b.origin + page)
        form = p.locator(f'form[action="{route}"]')
        for k, v in fields.items():
            el = form.locator(f'[name="{k}"]')
            if not el.count():
                continue
            kind = el.first.evaluate("e => e.tagName + ':' + (e.type || '')")
            if kind.endswith(":hidden"):
                continue
            el.first.select_option(str(v)) if kind.startswith("SELECT") else el.first.fill(str(v))
        self.gate.last_write = None
        form.locator('button[type="submit"]').first.click()
        p.wait_for_load_state("load")
        return self.gate.last_write or {}

    def steel_submit(self, vals=None, decision="post"):
        v = dict(INV, **(vals or {}))
        return self.submit("/invoices/new", "/invoices", {**v, "source_doc": "inv_1042.pdf", "decision": decision})

    def ledger(self):
        return len(trusted.snapshot("steel")["ledger"])


def direct_post(fields, token, app="steel", route="/invoices", form_page="/invoices/new"):
    """Bypass the browser guard entirely (as if it were compromised) to test the app's own enforcement."""
    import re
    from http.cookiejar import CookieJar
    from shared.config import app_origin
    origin = app_origin(app)
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()), urllib.request.ProxyHandler({}))
    c = load_secrets()["credentials"][app]
    html = op.open(f"{origin}/login", urllib.parse.urlencode(
        {"username": c["username"], "password": c["password"], "next": form_page}).encode()).read().decode()
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    data = urllib.parse.urlencode({**fields, "csrf_token": csrf, "reason": "x"}).encode()
    try:
        r = op.open(urllib.request.Request(f"{origin}{route}", data=data, headers={capability.HEADER: token}))
        return r.status
    except urllib.error.HTTPError as e:
        return e.code


def steel_token(fields, idem="idem-" + str(time.time())):
    norm = normalize("steel.record_invoice", fields)
    return capability.issue(load_secrets()["capability_key"], audience=STEEL, method="POST", route="/invoices",
                            action="steel.record_invoice", case_id="inv_1042.pdf",
                            digest=capability.payload_digest(norm), run_id="t", idempotency_key=idem)


import urllib.parse  # noqa: E402


# ---------------------------------------------------------------- D1
class D1MaterialInvoiceFields(Harness):
    human = "auto:disagreement=stop"

    def test_each_material_field_change_goes_to_a_human(self):
        changes = {"grade": "UNRELATED PRODUCT", "subtotal": "1", "tax": "2", "total": "368170", "rate": "52100",
                   "quantity_t": "6.050", "invoice_no": "ERM/24-25/0999", "po_no": "PO-4505", "vendor_id": "V-102"}
        for field, bad in changes.items():
            with self.subTest(field=field):
                rid = new_run("steel", self.cases, human="auto:disagreement=stop")
                g = Gate(rid, self.co, Human(rid, "auto:disagreement=stop"), agent_source="scripted")
                res = g.propose("inv_1042.pdf", "post", dict(INV, **{field: bad}), [], "t", self.cases)
                self.assertTrue(res.startswith("HUMAN DECISION: stop"), f"{field}: {res[:120]}")
                self.assertFalse(store.authorized(rid, statuses=("authorized",)))
                store.delete_run(rid)

    def test_inconsistent_values_never_authorized_even_if_human_follows_agent(self):
        rid = new_run("steel", self.cases, human="auto:disagreement=follow_gemini")
        g = Gate(rid, self.co, Human(rid, "auto:disagreement=follow_gemini"), agent_source="scripted")
        res = g.propose("inv_1042.pdf", "post", dict(INV, grade="UNRELATED PRODUCT", subtotal="1", tax="2"), [],
                        "t", self.cases)
        self.assertIn("not internally consistent", res)
        self.assertFalse(store.authorized(rid))
        store.delete_run(rid)

    def test_app_refuses_inconsistent_or_mismatched_posting(self):
        for bad in ({"grade": "UNRELATED PRODUCT"}, {"subtotal": "1", "tax": "2"}, {"rate": "53000"}):
            f = dict(INV, **bad, source_doc="inv_1042.pdf", decision="post")
            f.pop("reason")
            self.assertEqual(direct_post(f, steel_token(f)), 422, bad)
        self.assertEqual(self.ledger(), 1)  # only the archived posting


# ---------------------------------------------------------------- D2
class D2StopRevokes(Harness):
    human = "auto:disagreement=stop"

    def test_stop_revokes_earlier_authorization_and_survives_restart(self):
        self.assertTrue(self.propose("inv_1042.pdf", "post", INV).startswith("AUTHORIZED"))
        res = self.propose("inv_1042.pdf", "hold", INV)          # disagrees -> test human stops the case
        self.assertTrue(res.startswith("HUMAN DECISION: stop"))
        w = self.steel_submit()
        self.assertEqual(w["status"], "blocked")
        self.assertEqual(self.ledger(), 1)
        restarted = Gate(self.rid, self.co, Human(self.rid, "auto"), agent_source="scripted")  # new worker
        self.assertIn("inv_1042.pdf", restarted.stopped_cases)
        self.assertTrue(restarted.propose("inv_1042.pdf", "post", INV, [], "t", self.cases).startswith("CASE STOPPED"))

    def test_guard_checks_stop_state_at_the_write_boundary(self):
        # even if a permission were still outstanding (e.g. stop recorded by another process), the write is blocked
        self.assertTrue(self.propose("inv_1042.pdf", "post", INV).startswith("AUTHORIZED"))
        store.set_case_control(self.rid, "inv_1042.pdf", "stopped", "operator", "test")
        self.assertEqual(self.steel_submit()["status"], "blocked")
        self.assertEqual(self.ledger(), 1)

    def test_rejected_approval_blocks_outstanding_writes(self):
        seed_steel(["inv_1045.pdf"])
        rid = new_run("steel", ["inv_1045.pdf"], human="auto:approval=reject")
        g = Gate(rid, self.co, Human(rid, "auto:approval=reject"), agent_source="scripted")
        v = dict(INV, vendor_id="V-104", invoice_no="NPT/7740", po_no="PO-4503", grade="ERW pipe 48.3x3.2",
                 quantity_t="12.000", rate="54000", subtotal="648000", tax="116640", total="764640")
        self.assertIn("REJECTED", g.propose("inv_1045.pdf", "post", v, [], "t", ["inv_1045.pdf"]))
        self.assertIn("inv_1045.pdf", store.stopped_cases(rid))
        self.assertFalse(store.authorized(rid, statuses=("authorized",)))
        store.delete_run(rid)


# ---------------------------------------------------------------- D3
class D3Cancellation(Harness):
    def test_guard_blocks_after_cancellation(self):
        self.assertTrue(self.propose("inv_1042.pdf", "post", INV).startswith("AUTHORIZED"))
        store.update_run(self.rid, status="cancel_requested")
        self.assertEqual(self.steel_submit()["status"], "blocked")
        self.assertEqual(self.ledger(), 1)
        self.assertTrue(all(a["status"] == "revoked" for a in store.authorized(self.rid)))


class Stub:
    """Test model: each script step receives the runner and returns tool calls (or an exception to raise)."""
    kind = "scripted"

    def __init__(self, run_id, script):
        self.run_id, self.script, self.i = run_id, script, 0
        self.budget = Budget(run_id)

    def attach(self, r):
        self.r = r

    def generate(self, system, user, tools, kind="step"):
        store.record_call(self.run_id, "ok", "scripted")
        if self.i >= len(self.script):
            return ModelTurn(calls=[{"name": "request_completion", "args": {"summary": "done"}}])
        out = self.script[self.i](self.r)
        self.i += 1
        if isinstance(out, Exception):
            raise out
        return ModelTurn(calls=out)


def C(name, **args):
    return {"name": name, "args": args}


def refs(r, form=None):
    return {e["name"]: e["ref"] for e in r.browser.last_obs["elements"] if e.get("name")
            and (form is None or form.lower() in (e.get("form") or "").lower())}


def button(r, form=None):
    return next(e["ref"] for e in r.browser.last_obs["elements"] if e["kind"] == "button"
                and (form is None or form.lower() in (e.get("form") or "").lower()))


def vals(d):
    return [{"name": k, "value": v} for k, v in d.items()]


class RunnerHarness(unittest.TestCase):
    def make(self, company, task, script, seeder, human="auto", read_only=None):
        seeder()
        trusted.set_faults(load_company(company)["profile"]["app"], {"reset": True})
        store.init()
        self.rid = store.create_run(company, task, "none", "scripted", human, label="review-test", read_only=read_only)
        self.model = Stub(self.rid, script)
        return runner_mod.Runner(self.rid, model=self.model)

    def tearDown(self):
        store.delete_run(self.rid)
        shutil.rmtree(RUNS / self.rid, ignore_errors=True)


class D3CancelDuringModelCall(RunnerHarness):
    def test_cancel_while_model_thinks_discards_returned_click(self):
        rid_box = {}

        def cancel_then_click(r):
            store.update_run(r.run_id, status="cancel_requested")
            return [C("click", ref=button(r))]
        script = [lambda r: [C("sign_in")],
                  lambda r: [C("propose_decision", case_id="inv_1042.pdf", action="post", values=vals(INV), rationale="t")],
                  lambda r: [C("open_page", path="/invoices/new")],
                  lambda r: [C("fill", fields=[{"ref": ref, "value": v} for k, ref in refs(r).items()
                                               for v in [dict(INV, source_doc="inv_1042.pdf", decision="post").get(k)]
                                               if v is not None])],
                  cancel_then_click]
        rn = self.make("steel", "Process inv_1042", script, lambda: seed_steel(["inv_1042.pdf"]))
        rid_box["r"] = rn
        rn.execute()
        self.assertEqual(store.get_run(self.rid)["status"], "cancelled")
        self.assertFalse([p for p in trusted.snapshot("steel")["processed_invoices"] if p["source_doc"] == "inv_1042.pdf"])
        self.assertTrue(all(a["status"] == "revoked" for a in store.authorized(self.rid)))
        # the actions returned by the in-flight model call were discarded before any was executed
        self.assertTrue(store.events(self.rid, types=("calls_discarded",)))
        self.assertFalse([e for e in store.events(self.rid, types=("step",)) if e["payload"]["tool"] == "click"])
        self.assertFalse(store.events(self.rid, types=("write_submitted",)))

# ---------------------------------------------------------------- D4
class D4StaleAuthorization(Harness):
    def test_changed_po_after_authorization_needs_new_decision(self):
        self.assertTrue(self.propose("inv_1042.pdf", "post", INV).startswith("AUTHORIZED"))
        db_exec("steel", "UPDATE purchase_orders SET rate=5300000, tonnes='5.000' WHERE id='PO-4501'")
        w = self.steel_submit()
        self.assertEqual(w["status"], "blocked")
        self.assertIn("changed", w["message"])
        self.assertTrue(all(a["status"] == "stale" for a in store.authorized(self.rid)))
        self.assertEqual(self.ledger(), 1)
        f = dict(INV, source_doc="inv_1042.pdf", decision="post")
        f.pop("reason")
        self.assertEqual(direct_post(f, steel_token(f)), 422)    # the app's own transaction check, too


# ---------------------------------------------------------------- D5
class D5ArtifactBoundary(unittest.TestCase):
    def test_traversal_and_unknown_runs_rejected_real_artifacts_served(self):
        store.init()
        (DATA / "review-canary.txt").write_text("canary", encoding="utf-8")
        rid = store.create_run("steel", "t", "none", "scripted", "auto", label="review-test")
        (RUNS / rid).mkdir(parents=True, exist_ok=True)
        (RUNS / rid / "report.md").write_text("# report", encoding="utf-8")
        env = dict(os.environ, CONSOLE_PORT="8190")
        srv = subprocess.Popen([sys.executable, "-m", "console.server"], cwd=str(ROOT), env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def get(path):
            try:
                return op.open("http://127.0.0.1:8190" + path, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        try:
            for _ in range(40):
                try:
                    op.open("http://127.0.0.1:8190/", timeout=1)
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.25)
            self.assertEqual(get(f"/runs/{rid}/report.md"), 200)
            for bad in ("/runs/%2e%2e/data/review-canary.txt", "/runs/..%2fdata/review-canary.txt",
                        f"/runs/{rid}/..%2f..%2fdata/review-canary.txt", "/runs/run-00000000/report.md",
                        "/runs/%2e%2e/data/secrets.json"):
                self.assertEqual(get(bad), 404, bad)
        finally:
            srv.terminate()
            srv.wait(5)
            (DATA / "review-canary.txt").unlink(missing_ok=True)
            store.delete_run(rid)
            shutil.rmtree(RUNS / rid, ignore_errors=True)


# ---------------------------------------------------------------- D6
class D6RestartRecovery(RunnerHarness):
    def test_restart_reconciles_dispatched_write_and_closes_interrupted_decisions(self):
        rn = self.make("steel", "Process inv_1042", [], lambda: seed_steel(["inv_1042.pdf"]))
        store.update_run(self.rid, scope={"cases": ["inv_1042.pdf"], "before": trusted.snapshot("steel")})
        g = Gate(self.rid, rn.company, Human(self.rid, "auto"), agent_source="scripted")
        self.assertTrue(g.propose("inv_1042.pdf", "post", INV, [], "t", ["inv_1042.pdf"]).startswith("AUTHORIZED"))
        a = store.authorized(self.rid)[0]
        token = capability.issue(load_secrets()["capability_key"], audience=STEEL, method="POST", route="/invoices",
                                 action=a["action_type"], case_id=a["case_id"], digest=a["digest"], run_id=self.rid,
                                 idempotency_key=a["idem_key"])
        f = dict(INV, source_doc="inv_1042.pdf", decision="post")
        f.pop("reason")
        self.assertEqual(direct_post(f, token), 200)          # the commit happened ...
        store.update_authorized(a["id"], status="submitted")  # ... but the worker died before recording it
        did = store.add_decision(self.rid, "inv_1042.pdf", "approval", {"proposal_id": a["proposal_id"]})
        store.emit(self.rid, "step", {"n": 1, "tool": "sign_in", "args": {}, "result": "ok"})
        calls = []
        orig = trusted.idempotency
        trusted.idempotency = lambda app, key: (calls.append(key), orig(app, key))[1]
        try:
            rn._setup()
        finally:
            trusted.idempotency = orig
            rn.browser.close()
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.authorized(self.rid)[0]["status"], "committed")
        self.assertEqual(store.get_decision(did)["response"]["choice"], "interrupted")
        self.assertIn("RECOVERY", rn.steps[-1]["result"])
        self.assertEqual(len([p for p in trusted.snapshot("steel")["processed_invoices"]
                              if p["source_doc"] == "inv_1042.pdf"]), 1)


# ---------------------------------------------------------------- D7
class D7VerificationMutations(Harness):
    def _posted(self):
        self.assertTrue(self.propose("inv_1042.pdf", "post", INV).startswith("AUTHORIZED"))
        self.assertEqual(self.steel_submit()["status"], "committed")
        self.assertEqual(verifier.verify_run(self.rid, self.co)["overall"], "completed")

    def test_saved_quantity_or_grade_mutation_fails(self):
        self._posted()
        for sql in ("UPDATE processed_invoices SET quantity_t='999.000' WHERE source_doc='inv_1042.pdf'",
                    "UPDATE processed_invoices SET quantity_t='6.000', grade='UNRELATED' WHERE source_doc='inv_1042.pdf'"):
            with self.subTest(sql=sql):
                db_exec("steel", sql)
                self.assertEqual(verifier.verify_run(self.rid, self.co)["overall"], "failed")


class D7SchoolLink(Harness):
    company, cases = "school", ["A-501"]

    def seed(self):
        seed_school(["A-501"])

    def test_enrolment_linked_to_wrong_student_fails(self):
        v = {"batch_id": "B-11", "concession_pct": "0", "fee_payable": "48000.00",
             "message": "Welcome Riya, you are enrolled in batch B-11. Fee payable Rs 48000."}
        res = self.propose("A-501", "enrol", v)
        self.assertTrue(res.startswith("AUTHORIZED"), res)
        self.assertEqual(self.submit("/applications/A-501", "/applications/A-501/enrol",
                                     {"batch_id": "B-11", "concession_pct": "0", "fee_payable": "48000.00"})["status"], "committed")
        self.assertEqual(self.submit("/applications/A-501", "/applications/A-501/message",
                                     {"body": v["message"], "new_status": "enrolled"})["status"], "committed")
        self.assertEqual(verifier.verify_run(self.rid, self.co)["overall"], "completed")
        db_exec("school", "UPDATE enrolments SET student_id=1 WHERE application_id='A-501'")
        r = verifier.verify_run(self.rid, self.co)
        self.assertNotEqual(r["cases"]["A-501"]["status"], "verified")


class D7Replacement(Harness):
    company, cases = "ecommerce", ["T-305"]

    def seed(self):
        seed_ecom(["T-305"])
        db_exec("ecommerce", "UPDATE products SET stock=5 WHERE id='P-11'")

    def test_replacement_line_product_and_quantity_verified(self):
        v = {"order_line_id": "L-7", "quantity": "1", "reply_text": "A replacement pair is on its way to you."}
        self.assertTrue(self.propose("T-305", "replace", v).startswith("AUTHORIZED"))
        self.assertEqual(self.submit("/tickets/T-305", "/tickets/T-305/replacement",
                                     {"order_line_id": "L-7", "quantity": "1"})["status"], "committed")
        self.assertEqual(self.submit("/tickets/T-305", "/tickets/T-305/reply",
                                     {"body": v["reply_text"], "new_status": "resolved"})["status"], "committed")
        self.assertEqual(verifier.verify_run(self.rid, self.co)["overall"], "completed")
        for sql in ("UPDATE replacements SET quantity=99", "UPDATE replacements SET quantity=1, product_id='P-12'"):
            with self.subTest(sql=sql):
                db_exec("ecommerce", sql)
                self.assertEqual(verifier.verify_run(self.rid, self.co)["overall"], "failed")


# ---------------------------------------------------------------- D8, D9, D10, R6 (unit level)
class Units(unittest.TestCase):
    def test_d8_return_window_exact_seconds(self):
        ad = load_company("ecommerce")["adapter"]
        pol = load_company("ecommerce")["policy"]
        base = {"customers": [], "products": [{"id": "P", "category": "apparel", "stock": 3}],
                "orders": [{"id": "O", "customer_id": "C", "delivered_at": 1_000_000.0}],
                "order_lines": [{"id": "L", "order_id": "O", "product_id": "P", "qty": 1, "paid_amount": 1000}],
                "messages": [], "refunds": []}
        for delta, outcome in ((7 * 86400, "refund"), (7 * 86400 + 1, "deny"), (-60, "clarify")):
            snap = dict(base, tickets=[{"id": "T", "customer_id": "C", "request_type": "return_refund",
                                        "created_at": 1_000_000.0 + delta, "linked_order_id": "O"}])
            f, _ = ad.facts("T", snap)
            self.assertEqual(checker.evaluate(pol, f)["outcome"], outcome, delta)

    def test_d9_parsing_rejects_reinterpretation(self):
        for t, v in (("qty3", "-6.000"), ("int", "1.5"), ("money", "1e3"), ("money", "-5"), ("money", "12abc"),
                     ("qty3", "6.0005"), ("money", "1.234")):
            with self.subTest(v=v), self.assertRaises(FieldError):
                normalize_field(t, v)
        self.assertEqual(normalize_field("money", "₹3,68,160.00"), "36816000")
        self.assertEqual(normalize_field("money", "368160"), "36816000")
        self.assertEqual(normalize_field("int", "25%"), "25")

    def test_d10_limits_read_at_run_time_and_full_request_ceiling(self):
        old = {k: os.environ.get(k) for k in ("MAX_TOOL_STEPS", "MAX_RUN_SECONDS", "CONTEXT_TOKEN_CEILING")}
        try:
            os.environ.update(MAX_TOOL_STEPS="7", MAX_RUN_SECONDS="12")
            self.assertEqual(runner_mod.limits(), (7, 12))
            store.init()
            rid = store.create_run("steel", "t", "none", "scripted", "auto", label="review-test")
            store.add_fact(rid, None, "huge", "x" * 100_000, "test")
            os.environ["CONTEXT_TOKEN_CEILING"] = "10000"
            p = build_user_prompt(store.get_run(rid), [], "obs " * 20000, {}, overhead_tokens=3000)
            self.assertLessEqual(len(p) // 4 + 1 + 3000, 10000)
            os.environ["CONTEXT_TOKEN_CEILING"] = "100"
            with self.assertRaises(PromptTooLarge):
                build_user_prompt(store.get_run(rid), [], "obs", {}, overhead_tokens=3000)
            store.delete_run(rid)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_r6_per_run_cap_and_scripted_calls_not_counted_as_quota(self):
        store.init()
        rid = store.create_run("steel", "t", "none", "gemini", "auto", label="review-test", call_cap=2)
        other = store.create_run("steel", "t", "none", "scripted", "auto", label="review-test")
        old = os.environ.get("GEMINI_RPD")
        try:
            self.assertEqual(Budget(rid).per_run, 2)
            os.environ["GEMINI_RPD"] = "1000000"
            for _ in range(30):
                store.record_call(other, "ok", "scripted")
            before = Budget(rid).status()["today_calls"]
            for _ in range(3):
                store.record_call(other, "ok", "scripted")
            self.assertEqual(Budget(rid).status()["today_calls"], before)   # scripted calls are not provider usage
            store.record_call(rid, "ok", "step")
            store.record_call(rid, "ok", "step")
            with self.assertRaises(QuotaExhausted):
                Budget(rid).admit()                                          # per-run cap of 2 enforced
        finally:
            os.environ.pop("GEMINI_RPD", None) if old is None else os.environ.__setitem__("GEMINI_RPD", old)
            store.delete_run(rid)
            store.delete_run(other)


# ---------------------------------------------------------------- R1, R2 (quota -> rules executor)
class R1RulesExecutorAfterQuota(RunnerHarness):
    def test_rules_execute_covered_cases_after_quota_ends(self):
        rn = self.make("steel", "Process the supplier invoices in the inbox",
                       [lambda r: QuotaExhausted("provider_daily_quota", "test")], lambda: seed_steel(None),
                       human="auto:approval=approve")
        rn.execute()
        run = store.get_run(self.rid)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["mode"], "rules_fallback")
        snap = trusted.snapshot("steel")
        got = {p["source_doc"]: p["decision"] for p in snap["processed_invoices"] if p["source_doc"].startswith("inv_")}
        self.assertEqual(got, {"inv_1042.pdf": "post", "inv_1043.pdf": "reject_duplicate", "inv_1044.pdf": "hold",
                               "inv_1045.pdf": "post", "inv_1046.pdf": "post", "inv_1047.pdf": "reject_blocked"})
        self.assertTrue(any(d["kind"] == "approval" and d["case_id"] == "inv_1045.pdf" for d in store.decisions(self.rid)))
        self.assertTrue(all(p["source"] == "rules" for p in store.proposals(self.rid)))
        self.assertEqual(len([c for c in store.calls(self.rid)]), 1)       # no model call after capacity ended


class R1UncoveredCasesGoToHuman(RunnerHarness):
    def test_case_rules_cannot_decide_is_left_for_a_human_not_executed(self):
        def seed():
            seed_steel(["inv_1042.pdf"])
            from reportlab.pdfgen import canvas
            c = canvas.Canvas(str(ROOT / "companies" / "steel" / "inbox" / "inv_9999.pdf"))
            c.drawString(50, 800, "Unreadable scanned invoice")
            c.save()
        rn = self.make("steel", "Process the supplier invoices in the inbox",
                       [lambda r: QuotaExhausted("provider_daily_quota", "test")], seed)
        rn.execute()
        self.assertEqual(store.get_run(self.rid)["status"], "waiting_for_quota")
        handoff = next(d for d in store.decisions(self.rid) if d["kind"] == "quota_handoff")
        self.assertEqual(handoff["request"]["executed_by_rules"], ["inv_1042.pdf"])
        self.assertEqual([x["case"] for x in handoff["request"]["recommendations"]], ["inv_9999.pdf"])
        self.assertFalse([p for p in trusted.snapshot("steel")["processed_invoices"] if p["source_doc"] == "inv_9999.pdf"])


class R2PartialCaseFinishedAfterQuota(RunnerHarness):
    def test_committed_refund_with_pending_reply_is_completed_not_dropped(self):
        script = [lambda r: [C("sign_in")],
                  lambda r: [C("propose_decision", case_id="T-301", action="refund", values=vals(REFUND), rationale="t")],
                  lambda r: [C("open_page", path="/tickets/T-301")],
                  lambda r: [C("fill", fields=[{"ref": refs(r, "Issue refund")["order_line_id"], "value": "L-1"},
                                               {"ref": refs(r, "Issue refund")["amount"], "value": "1499.00"}]),
                             C("click", ref=button(r, "Issue refund"))],
                  lambda r: QuotaExhausted("provider_daily_quota", "test")]
        rn = self.make("ecommerce", "Resolve the open return tickets", script, lambda: seed_ecom(["T-301"]))
        rn.execute()
        self.assertEqual(store.get_run(self.rid)["status"], "completed")
        snap = trusted.snapshot("ecommerce")
        self.assertEqual([r["amount"] for r in snap["refunds"] if r["ticket_id"] == "T-301"], [149900])
        self.assertEqual([r["body"] for r in snap["replies"] if r["ticket_id"] == "T-301"], [REFUND["reply_text"]])


# ---------------------------------------------------------------- R3
class R3Sequencing(Harness):
    company, cases = "ecommerce", ["T-301"]

    def seed(self):
        seed_ecom(["T-301"])

    def test_reply_cannot_precede_refund_and_expired_refund_is_not_completed(self):
        self.assertTrue(self.propose("T-301", "refund", REFUND).startswith("AUTHORIZED"))
        w = self.submit("/tickets/T-301", "/tickets/T-301/reply", {"body": REFUND["reply_text"], "new_status": "resolved"})
        self.assertEqual(w["status"], "blocked")
        self.assertIn("in order", w["message"])
        refund = next(a for a in store.authorized(self.rid) if a["action_type"] == "ecom.refund")
        with store.db() as c:
            c.execute("UPDATE authorized_actions SET expires_at=? WHERE id=?", (time.time() - 1, refund["id"]))
        self.assertEqual(self.submit("/tickets/T-301", "/tickets/T-301/refund",
                                     {"order_line_id": "L-1", "amount": "1499.00"})["status"], "blocked")
        # even if the reply were saved by other means, the expired refund obligation keeps the case incomplete
        reply = next(a for a in store.authorized(self.rid) if a["action_type"] == "ecom.reply")
        tok = capability.issue(load_secrets()["capability_key"], audience="http://127.0.0.1:8102", method="POST",
                               route=reply["route"], action="ecom.reply", case_id="T-301", digest=reply["digest"],
                               run_id=self.rid, idempotency_key=reply["idem_key"])
        self.assertEqual(direct_post({"ticket_id": "T-301", "body": REFUND["reply_text"], "new_status": "resolved"},
                                     tok, "ecommerce", reply["route"], "/tickets/T-301"), 200)
        store.update_authorized(reply["id"], status="committed")
        r = verifier.verify_run(self.rid, self.co)
        self.assertNotEqual(r["cases"]["T-301"]["status"], "verified")
        self.assertNotEqual(r["overall"], "completed")


# ---------------------------------------------------------------- R4
class R4NarrowScope(RunnerHarness):
    def test_named_case_limits_scope(self):
        rn = self.make("ecommerce", "Please resolve ticket T-301 only", [], lambda: seed_ecom(None))
        rn._setup()
        rn.browser.close()
        self.assertEqual(rn.scope(), ["T-301"])
        res = rn.gate.propose("T-303", "refund", dict(REFUND, order_line_id="L-3", amount="8999.00"), [], "t", rn.scope())
        self.assertIn("not in this task's scope", res)


class R4UnknownCaseNeverWidensScope(unittest.TestCase):
    def test_named_case_that_is_not_open_gives_empty_scope(self):
        from operator_core.taskscope import narrow_scope
        scope, note = narrow_scope("Resolve ticket T-399", ["T-301", "T-303"])
        self.assertEqual(scope, [])
        self.assertIn("T-399", note)
        self.assertEqual(narrow_scope("Process inv_1042", ["inv_1042.pdf", "inv_1043.pdf"])[0], ["inv_1042.pdf"])


class R4ReadOnly(Harness):
    company, cases = "ecommerce", ["T-301"]
    task = "Just tell me what should happen with T-301 - don't change anything"
    read_only = None

    def seed(self):
        seed_ecom(["T-301"])

    def setUp(self):
        super().setUp()
        self.assertTrue(store.get_run(self.rid)["read_only"])  # detected from the wording

    def test_read_only_task_cannot_authorize_or_write(self):
        res = self.propose("T-301", "refund", REFUND)
        self.assertTrue(res.startswith("READ-ONLY TASK"), res)
        self.assertFalse(store.authorized(self.rid))
        self.assertEqual(self.submit("/tickets/T-301", "/tickets/T-301/refund",
                                     {"order_line_id": "L-1", "amount": "1499.00"})["status"], "blocked")
        self.assertEqual(verifier.verify_run(self.rid, self.co)["overall"], "completed")
        self.assertFalse(trusted.snapshot("ecommerce")["refunds"])


# ---------------------------------------------------------------- R5
class R5UnknownOutcome(Harness):
    def test_failed_reconciliation_is_not_absence_and_blocks_new_intent(self):
        self.assertTrue(self.propose("inv_1042.pdf", "post", INV).startswith("AUTHORIZED"))
        trusted.set_faults("steel", {"drop_after_commit": 1})
        import operator_core.authz as authz_mod
        orig = authz_mod.trusted.idempotency

        def broken(app, key):
            raise ConnectionError("trusted API unreachable")
        authz_mod.trusted.idempotency = broken
        try:
            w = self.steel_submit()
            self.assertEqual(w["status"], "unknown_after_reconciliation")
            self.assertEqual(store.authorized(self.rid)[0]["status"], "uncertain")
            self.assertTrue(self.propose("inv_1042.pdf", "hold", INV).startswith("BLOCKED"))
        finally:
            authz_mod.trusted.idempotency = orig
        # once reconciliation works again the real outcome is recorded (it was saved exactly once)
        self.gate.reconcile_case("inv_1042.pdf")
        self.assertEqual(store.authorized(self.rid)[0]["status"], "committed")
        self.assertEqual(self.ledger(), 2)


if __name__ == "__main__":
    unittest.main()
