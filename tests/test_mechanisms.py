"""Mechanism tests (no model calls). Requires the sandbox apps to be running.

  python -m unittest tests.test_mechanisms -v
"""
from __future__ import annotations

import re
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

import yaml

from operator_core import checker, store, trusted
from operator_core.authz import Gate, Human
from operator_core.browser import Browser
from operator_core.company import load as load_company
from scripts.seed import seed_ecom, seed_steel
from shared import capability
from shared.actions import normalize
from shared.config import RUNS, load_secrets

STEEL = "http://127.0.0.1:8101"
FIELDS = dict(source_doc="inv_1042.pdf", vendor_id="V-101", invoice_no="ERM/24-25/0912", po_no="PO-4501",
              grade="IS 2062 E250BR plates", quantity_t="6.000", rate="52000", subtotal="312000", tax="56160",
              total="368160", decision="post")


def session():
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()), urllib.request.ProxyHandler({}))
    c = load_secrets()["credentials"]["steel"]
    html = op.open(f"{STEEL}/login", urllib.parse.urlencode(
        {"username": c["username"], "password": c["password"], "next": "/invoices/new"}).encode()).read().decode()
    return op, re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def post(op, csrf, fields, token=None):
    data = urllib.parse.urlencode({**fields, "csrf_token": csrf, "reason": "test"}).encode()
    req = urllib.request.Request(f"{STEEL}/invoices", data=data,
                                 headers={capability.HEADER: token} if token else {})
    try:
        r = op.open(req)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def token(fields, **over):
    kw = dict(audience=STEEL, method="POST", route="/invoices", action="steel.record_invoice", case_id="inv_1042.pdf",
              digest=capability.payload_digest(normalize("steel.record_invoice", fields)), run_id="test",
              idempotency_key="idem-" + str(time.time()))
    kw.update(over)
    key = kw.pop("key", load_secrets()["capability_key"])
    return capability.issue(key, **kw)


class AppEnforcement(unittest.TestCase):
    def setUp(self):
        seed_steel(["inv_1042.pdf"])
        trusted.set_faults("steel", {"reset": True})

    def ledger(self):
        return len(trusted.snapshot("steel")["ledger"])

    def test_missing_forged_expired_altered_wrong_audience(self):
        op, csrf = session()
        n0 = self.ledger()
        self.assertEqual(post(op, csrf, FIELDS)[0], 403)                                    # missing
        self.assertEqual(post(op, csrf, FIELDS, token(FIELDS, key="f" * 64))[0], 403)       # forged
        self.assertEqual(post(op, csrf, FIELDS, token(FIELDS, ttl_s=-1))[0], 403)           # expired
        self.assertEqual(post(op, csrf, FIELDS, token(FIELDS, audience="http://x"))[0], 403)  # other destination
        altered = dict(FIELDS, total="368170")
        self.assertEqual(post(op, csrf, altered, token(FIELDS))[0], 403)                    # changed amount
        self.assertEqual(self.ledger(), n0)

    def test_idempotent_replay_and_key_reuse(self):
        op, csrf = session()
        n0 = self.ledger()
        t = token(FIELDS, idempotency_key="same-key")
        self.assertEqual(post(op, csrf, FIELDS, t)[0], 200)
        status, body = post(op, csrf, FIELDS, t)
        self.assertEqual(status, 200)
        self.assertIn("Already recorded", body)
        self.assertEqual(self.ledger(), n0 + 1)                                              # one side effect
        other = dict(FIELDS, decision="hold")
        self.assertEqual(post(op, csrf, other, token(other, idempotency_key="same-key"))[0], 409)

    def test_live_state_rechecked_on_save(self):
        op, csrf = session()
        blocked = dict(FIELDS, vendor_id="V-103", po_no="PO-4504")                         # blocked vendor
        self.assertEqual(post(op, csrf, blocked, token(blocked))[0], 422)


class GateInBrowser(unittest.TestCase):
    """The agent's browser cannot save anything without a matching authorization."""

    def setUp(self):
        seed_steel(["inv_1042.pdf"])
        trusted.set_faults("steel", {"reset": True})
        store.init()
        self.rid = store.create_run("steel", "test", "none", "scripted", "auto", label="mechanism-test")
        store.update_run(self.rid, scope={"cases": ["inv_1042.pdf"], "before": trusted.snapshot("steel")})
        self.company = load_company("steel")
        self.gate = Gate(self.rid, self.company, Human(self.rid, "auto"), agent_source="scripted")
        d = RUNS / self.rid
        d.mkdir(parents=True, exist_ok=True)
        self.b = Browser(self.company["profile"], d, self.gate)
        self.b.start()
        self.b.sign_in()

    def tearDown(self):
        self.b.close()
        store.delete_run(self.rid)
        import shutil
        shutil.rmtree(RUNS / self.rid, ignore_errors=True)

    def submit(self, fields):
        self.b.open("/invoices/new")
        els = {e["name"]: e["ref"] for e in self.b.last_obs["elements"] if e.get("name")}
        self.b.fill([{"ref": els[k], "value": v} for k, v in fields.items()] + [{"ref": els["reason"], "value": "x"}])
        btn = next(e["ref"] for e in self.b.last_obs["elements"] if e["kind"] == "button")
        self.b.click(btn)
        return self.gate.last_write

    def test_unauthorized_submit_blocked(self):
        w = self.submit(FIELDS)
        self.assertEqual(w["status"], "blocked")
        self.assertEqual(len(trusted.snapshot("steel")["ledger"]), 1)

    def test_authorized_then_altered_value_blocked_then_exact_value_commits(self):
        msg = self.gate.propose("inv_1042.pdf", "post", {k: v for k, v in FIELDS.items() if k not in
                                                        ("source_doc", "decision")}, [], "test", ["inv_1042.pdf"])
        self.assertTrue(msg.startswith("AUTHORIZED"), msg)
        w = self.submit(dict(FIELDS, total="368170"))
        self.assertEqual(w["status"], "blocked")
        self.assertIn("total", w["message"])
        w = self.submit(dict(FIELDS, rate="52,000.00", total="3,68,160.00"))  # same values, different formatting
        self.assertEqual(w["status"], "committed")

    def test_reauthorizing_a_saved_write_is_marked_done(self):
        vals = {k: v for k, v in FIELDS.items() if k not in ("source_doc", "decision")}
        self.gate.propose("inv_1042.pdf", "post", vals, [], "test", ["inv_1042.pdf"])
        self.assertEqual(self.submit(FIELDS)["status"], "committed")
        again = self.gate.propose("inv_1042.pdf", "post", vals, [], "again", ["inv_1042.pdf"])
        self.assertIn("ALREADY SAVED", again)
        self.assertFalse(store.authorized(self.rid, statuses=("authorized",)))

    def test_restricted_paths_and_destinations(self):
        with self.assertRaises(Exception):
            self.b.open("/api/trusted/snapshot")
        self.b.page.evaluate("fetch('https://example.com').catch(()=>0)")
        time.sleep(0.5)
        self.assertTrue(any("non-allowed destination" in n for n in self.b.pop_notices()))

    def test_stale_ref_rejected(self):
        self.b.open("/vendors")
        old = self.b.last_obs["elements"][0]["ref"]
        self.b.observe()
        from operator_core.browser import ToolError
        with self.assertRaises(ToolError):
            self.b.click(old)


class CheckerBoundaries(unittest.TestCase):
    steel = yaml.safe_load(open("companies/steel/policies.yaml", encoding="utf-8"))
    ecom = yaml.safe_load(open("companies/ecommerce/policies.yaml", encoding="utf-8"))
    base = dict(vendor_status="active", already_posted=False, po_found=True, po_belongs_to_vendor=True,
                grade_matches=True, quantity_deviation_pct=0.0, rate_matches=True, arithmetic_ok=True,
                total_paise=100000)

    def test_quantity_tolerance_boundary(self):          # S9
        self.assertEqual(checker.evaluate(self.steel, dict(self.base, quantity_deviation_pct=2.0))["outcome"], "post")
        self.assertEqual(checker.evaluate(self.steel, dict(self.base, quantity_deviation_pct=2.0001))["outcome"], "hold")

    def test_approval_threshold_boundary(self):          # S10
        f = dict(self.base)
        self.assertEqual(checker.approval_needed(self.steel, f, "post", {"total": "500000.00"}, {"total": "total_paise"}), [])
        self.assertTrue(checker.approval_needed(self.steel, f, "post", {"total": "500000.01"}, {"total": "total_paise"}))

    def test_hard_denial_survives_and_blocks_post(self):  # S8
        r = checker.evaluate(self.steel, dict(self.base, vendor_status="blocked", total_paise=90000000))
        self.assertEqual(r["allowed_actions"], ["reject_blocked"])
        self.assertIn("post", r["hard_denied_actions"])

    def test_missing_fact_is_not_covered(self):           # S11
        f = dict(self.base)
        del f["rate_matches"]
        self.assertEqual(checker.evaluate(self.steel, f)["status"], "not_covered")

    def test_return_window_boundary(self):                 # E8
        f = dict(requested_remedy="refund", order_identified=True, category_returnable=True,
                 refundable_remaining_paise=1000, refund_amount_paise=1000, stock_available=1)
        self.assertEqual(checker.evaluate(self.ecom, dict(f, days_since_delivery=7.0))["outcome"], "refund")
        self.assertEqual(checker.evaluate(self.ecom, dict(f, days_since_delivery=7.01))["outcome"], "deny")


if __name__ == "__main__":
    unittest.main()
