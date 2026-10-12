"""Regression tests for the second review: request scope grammar, unenforceable requests, empty or unknown scope,
shared stock in replacement verification and full record comparison for read-only runs. No model calls.
Requires the sandbox apps to be running.

  python -m unittest tests.test_second_review -v
"""
from __future__ import annotations

import copy
import unittest

from companies.ecommerce import adapter as ecom
from operator_core import store, trusted, verifier
from operator_core.taskscope import interpret, looks_read_only, request_mode
from scripts.seed import seed_ecom
from tests.test_review_findings import REFUND, Harness, RunnerHarness, db_exec

OPEN = ["T-301", "T-302", "T-303"]


class ScopeGrammar(unittest.TestCase):
    def test_named_all_except_and_excluding(self):
        self.assertEqual(interpret("Resolve ticket T-301 only.", OPEN)["cases"], ["T-301"])
        self.assertEqual(interpret("Resolve all tickets except T-301", OPEN)["cases"], ["T-302", "T-303"])
        self.assertEqual(interpret("Resolve all open tickets excluding T-301 and T-302", OPEN)["cases"], ["T-303"])
        self.assertEqual(interpret("Resolve the open tickets", OPEN)["cases"], OPEN)

    def test_unknown_requested_case_is_unresolved_and_never_widens(self):
        s = interpret("Resolve T-999", OPEN)
        self.assertEqual(s["cases"], [])
        self.assertEqual(s["unresolved"], ["T-999"])
        s = interpret("Resolve T-301 and T-999", OPEN)
        self.assertEqual((s["cases"], s["unresolved"]), (["T-301"], ["T-999"]))

    def test_requested_and_excluded_is_ambiguous(self):
        self.assertTrue(interpret("Resolve T-301 and T-302 but not T-302", OPEN)["issues"])

    def test_information_only_wording(self):
        for t in ("Inspect the tickets without making edits", "Do not issue refunds; just prepare recommendations.",
                  "Review T-301 and T-303 and tell me what should happen to each. Do not change anything.",
                  "Dry run: process the inbox"):
            with self.subTest(t=t):
                self.assertTrue(looks_read_only(t))

    def test_partial_restrictions_and_filters_are_unclear_never_silently_writable(self):
        for t in ("Resolve all tickets; do not change customer contact details", "Refund all tickets over 5000",
                  "Process the invoices received before Monday"):
            with self.subTest(t=t):
                self.assertEqual(request_mode(t)[0], "unclear")
        for t in ("Resolve the open return and replacement tickets using our store policy.",
                  "Process the supplier invoices in the inbox and tell me what was posted, held, rejected or needs approval.",
                  "Process the pending admission applications according to our admissions policy."):
            with self.subTest(t=t):
                self.assertEqual(request_mode(t)[0], "write")


class ScopeAtRunStart(RunnerHarness):
    def run_setup(self, task, open_tickets, human="auto", read_only=None):
        rn = self.make("ecommerce", task, [], lambda: seed_ecom(open_tickets), human=human, read_only=read_only)
        rn._setup()
        rn.browser.close()
        return rn

    def test_missing_case_finishes_without_changes_or_model_calls(self):
        rn = self.run_setup("Resolve ticket T-399", ["T-301", "T-303"])
        run = store.get_run(self.rid)
        self.assertEqual(run["status"], "needs_clarification")
        self.assertFalse(store.calls(self.rid))
        v = [e for e in store.events(self.rid, types=("verification",))][-1]["payload"]
        self.assertEqual(v["overall"], "needs_clarification")
        self.assertIn("T-399", v["note"])
        self.assertTrue(rn.finished)

    def test_valid_empty_queue_is_complete_with_explanation(self):
        self.run_setup("Resolve the open tickets", [])
        self.assertEqual(store.get_run(self.rid)["status"], "completed")
        v = [e for e in store.events(self.rid, types=("verification",))][-1]["payload"]
        self.assertEqual((v["overall"], v["note"]), ("completed", "No pending work was found."))

    def test_unenforceable_restriction_asks_first_default_is_recommendations_only(self):
        rn = self.run_setup("Resolve all tickets; do not change customer contact details", ["T-301"])
        decs = store.decisions(self.rid)
        self.assertEqual([d["kind"] for d in decs], ["scope"])
        self.assertTrue(store.get_run(self.rid)["read_only"])
        res = rn.gate.propose("T-301", "refund", REFUND, [], "t", rn.scope())
        self.assertTrue(res.startswith("READ-ONLY TASK"), res)

    def test_unenforceable_restriction_stop_means_no_run(self):
        self.run_setup("Refund all tickets over 5000", ["T-301"], human="auto:scope=stop")
        self.assertEqual(store.get_run(self.rid)["status"], "needs_clarification")

    def test_unenforceable_restriction_proceed_is_a_recorded_human_choice(self):
        rn = self.run_setup("Refund all tickets over 5000", ["T-301"], human="auto:scope=proceed")
        self.assertFalse(store.get_run(self.rid)["read_only"])
        self.assertEqual(store.unj(store.get_run(self.rid)["scope_json"])["clarified"]["choice"], "proceed")
        self.assertTrue(rn.gate.propose("T-301", "refund", REFUND, [], "t", rn.scope()).startswith("AUTHORIZED"))

    def test_explicit_read_only_flag_blocks_authorization(self):
        rn = self.run_setup("Resolve the open tickets", ["T-301"], read_only=True)
        self.assertTrue(rn.gate.propose("T-301", "refund", REFUND, [], "t", rn.scope()).startswith("READ-ONLY TASK"))
        self.assertFalse(store.decisions(self.rid))


class SharedStock(unittest.TestCase):
    """Two tickets each replace one unit of the same product: stock 5 -> 3 is correct for both."""

    def snapshots(self, stock_after):
        before = {"tickets": [{"id": "T-1", "status": "open"}, {"id": "T-2", "status": "open"}],
                  "order_lines": [{"id": "L-1", "product_id": "P-1"}, {"id": "L-2", "product_id": "P-1"}],
                  "products": [{"id": "P-1", "stock": 5}], "refunds": [], "replacements": [], "replies": []}
        after = copy.deepcopy(before)
        after["products"][0]["stock"] = stock_after
        for t in after["tickets"]:
            t["status"] = "resolved"
        after["replacements"] = [{"id": 1, "ticket_id": "T-1", "order_line_id": "L-1", "product_id": "P-1", "quantity": 1},
                                 {"id": 2, "ticket_id": "T-2", "order_line_id": "L-2", "product_id": "P-1", "quantity": 1}]
        after["replies"] = [{"id": i, "ticket_id": t, "body": "ok", "new_status": "resolved"}
                            for i, t in ((1, "T-1"), (2, "T-2"))]
        return before, after

    @staticmethod
    def writes(ticket, line):
        return [{"status": "committed", "action_type": "ecom.replace", "fields": {"order_line_id": line, "quantity": "1"}},
                {"status": "committed", "action_type": "ecom.reply", "fields": {"body": "ok", "new_status": "resolved"}}]

    def test_each_ticket_verifies_and_aggregate_matches(self):
        before, after = self.snapshots(3)
        for t, ln in (("T-1", "L-1"), ("T-2", "L-2")):
            with self.subTest(t=t):
                self.assertEqual(ecom.verify(t, self.writes(t, ln), before, after)["status"], "verified")
        self.assertEqual(ecom.unexpected_effects(before, after, {"T-1", "T-2"}), [])

    def test_incorrect_aggregate_stock_change_fails(self):
        before, after = self.snapshots(4)
        self.assertEqual(ecom.verify("T-1", self.writes("T-1", "L-1"), before, after)["status"], "failed")
        self.assertTrue(ecom.unexpected_effects(before, after, {"T-1", "T-2"}))


class ReadOnlyRecordsUnchanged(Harness):
    company, cases = "ecommerce", ["T-301"]
    task = "Just tell me what should happen with T-301 - don't change anything"
    read_only = None

    def seed(self):
        seed_ecom(["T-301", "T-303"])

    def recommend(self):
        self.assertTrue(self.propose("T-301", "refund", REFUND).startswith("READ-ONLY TASK"))

    def test_unchanged_records_complete(self):
        self.recommend()
        self.assertEqual(verifier.verify_run(self.rid, self.co)["overall"], "completed")

    def test_changed_ticket_status_fails(self):
        self.recommend()
        db_exec("ecommerce", "UPDATE tickets SET status='resolved' WHERE id='T-303'")
        v = verifier.verify_run(self.rid, self.co)
        self.assertEqual(v["overall"], "failed")
        self.assertTrue(any("T-303 changed (status)" in x for x in v["unexpected_effects"]), v["unexpected_effects"])

    def test_deleted_record_fails(self):
        self.recommend()
        db_exec("ecommerce", "DELETE FROM messages WHERE ticket_id='T-303'")
        self.assertEqual(verifier.verify_run(self.rid, self.co)["overall"], "failed")


if __name__ == "__main__":
    unittest.main()
