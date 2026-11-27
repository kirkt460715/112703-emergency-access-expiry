"""核验临时身份授权与提前收回处置。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import urgent_access
from records_view import load_sources


class UrgentAccessTests(unittest.TestCase):
    def setUp(self):
        self.sources = load_sources(Path(__file__).with_name("records"))

    def assess(self, ticket, at, **request):
        return urgent_access.assess(self.sources, ticket, at, **request)

    # --- 身份核验与审批 -----------------------------------------------

    def test_requester_identity_alone_is_not_high_assurance_authorization(self):
        result = self.assess("ticket-b", 44)
        self.assertFalse(result["confirmation"]["authorized"])
        self.assertEqual(result["ticket"]["status"], "unapproved")
        self.assertIn("identity_record_is_not_authorization", result["confirmation"]["refusal_reasons"])
        self.assertEqual(result["confirmation"]["identity_record_used_as"], "identity_only")

        result = self.assess("ticket-b", 44, actor="x", case="W3", region="west",
                             operation="contact_view")
        self.assertFalse(result["confirmation"]["authorized"])

    def test_approval_matches_requester_case_and_limits(self):
        result = self.assess("ticket-a", 48, actor="x", case="E1", region="east",
                             operation="contact_view", point="east-desk")
        self.assertTrue(result["confirmation"]["authorized"])
        bounds = result["confirmation"]["bound_to"]
        self.assertEqual(bounds["actor"], "x")
        self.assertEqual(bounds["case"], "E1")
        self.assertEqual(bounds["operations"], ["contact_view"])
        self.assertTrue(result["confirmation"]["not_inheritable"])

    def test_approval_does_not_follow_another_case_or_person(self):
        result = self.assess("ticket-a", 48, actor="x", case="W3", region="east",
                             operation="contact_view")
        self.assertFalse(result["confirmation"]["authorized"])
        self.assertIn("case_mismatch", result["confirmation"]["refusal_reasons"])

        result = self.assess("ticket-a", 48, actor="z", case="E1", region="east",
                             operation="contact_view")
        self.assertFalse(result["confirmation"]["authorized"])
        self.assertIn("not_ticket_holder", result["confirmation"]["refusal_reasons"])

        result = self.assess("ticket-a", 48, actor="x", case="E1", region="east",
                             operation="case_edit")
        self.assertFalse(result["confirmation"]["authorized"])
        self.assertIn("operation_outside_scope", result["confirmation"]["refusal_reasons"])

    def test_self_approval_and_unavailable_approver_are_refused(self):
        altered = copy.deepcopy(self.sources)
        ticket = altered["requests"][0]
        ticket["approved_by"] = "x"
        self.assertIn("self_approval", self.assess_altered(altered, 48))

        altered = copy.deepcopy(self.sources)
        altered["identity"][1]["until"] = 40
        self.assertIn("approver_not_available_at_approval", self.assess_altered(altered, 48))

    def assess_altered(self, sources, at):
        return urgent_access.assess(sources, "ticket-a", at)["ticket"]["approval"]["reasons"]

    def test_approver_availability_is_reported(self):
        result = self.assess("ticket-a", 48)
        self.assertTrue(result["approval"]["available_at"])
        self.assertEqual(result["approval"]["until"], 55)
        result = self.assess("ticket-a", 56)
        self.assertEqual(result["confirmation"]["approvers_available_now"], [])

    def test_verification_after_approval_does_not_backfill(self):
        altered = copy.deepcopy(self.sources)
        altered["requests"][0]["approved_at"] = 30
        reasons = self.assess_altered(altered, 48)
        self.assertIn("requester_not_verified_at_approval", reasons)

    def test_scope_ends_at_scheduled_end_without_early_end(self):
        altered = copy.deepcopy(self.sources)
        altered["requests"] = [row for row in altered["requests"] if row["id"] != "end-a"]
        result = urgent_access.assess(altered, "ticket-a", 63, actor="x", case="E1",
                                      region="east", operation="contact_view")
        self.assertEqual(result["ticket"]["effective_end"], 62)
        self.assertEqual(result["ticket"]["status"], "expired")
        self.assertFalse(result["confirmation"]["authorized"])

        result = urgent_access.assess(altered, "ticket-a", 61, actor="x", case="E1",
                                      region="east", operation="contact_view")
        self.assertTrue(result["confirmation"]["authorized"])

    def test_shift_end_caps_temporary_scope(self):
        altered = copy.deepcopy(self.sources)
        altered["identity"][0]["on_shift_until"] = 45
        altered["requests"] = [row for row in altered["requests"] if row["id"] != "end-a"]
        result = urgent_access.assess(altered, "ticket-a", 47, actor="x", case="E1",
                                      region="east", operation="contact_view")
        self.assertEqual(result["ticket"]["effective_end"], 45)
        self.assertEqual(result["ticket"]["status"], "expired")
        self.assertFalse(result["confirmation"]["authorized"])

    # --- 多机构 -------------------------------------------------------

    def test_region_listing_is_scoped(self):
        east = urgent_access.list_region(self.sources, "east")
        self.assertEqual([row["id"] for row in east["tickets"]], ["ticket-a"])
        west = urgent_access.list_region(self.sources, "west")
        self.assertEqual([row["id"] for row in west["tickets"]], ["ticket-b"])
        for row in east["tickets"] + west["tickets"]:
            self.assertEqual(row["region"], east["region"] if row in east["tickets"] else west["region"])

    def test_emergency_access_does_not_extend_to_other_region(self):
        result = self.assess("ticket-a", 48, actor="x", case="E1", region="west",
                             operation="contact_view")
        self.assertFalse(result["confirmation"]["authorized"])
        self.assertIn("region_outside_authorization", result["confirmation"]["refusal_reasons"])
        self.assertEqual(result["current_access"]["disclosure_scope"], "none")
        self.assertEqual(result["current_access"]["operations"], [])

    # --- 提前结束与收回 -----------------------------------------------

    def test_early_end_shortens_scope_and_reports_pending_points(self):
        result = urgent_access.handle(self.sources, "ticket-a", 51, at=52)
        self.assertEqual(result["registration"]["effective_end"], 51)
        self.assertFalse(result["new_access"]["authorized"])
        self.assertEqual(sorted(result["pending_for_follow_up"]), ["east-desk", "joint-desk"])
        self.assertFalse(result["registration"]["persisted"])

    def test_receipts_recorded_by_operator(self):
        result = urgent_access.handle(self.sources, "ticket-a", 51, at=58,
                                      receipts=[{"point": "joint-desk", "applied_at": 57}])
        self.assertEqual(sorted(result["acknowledged"]), ["east-desk", "joint-desk"])
        self.assertEqual(result["pending_for_follow_up"], [])
        self.assertEqual(result["registration"]["receipt_records"][0]["point"], "joint-desk")

    def test_receipt_before_end_does_not_count(self):
        result = urgent_access.handle(self.sources, "ticket-a", 51, at=58,
                                      receipts=[{"point": "joint-desk", "applied_at": 50}])
        claims = {claim["id"]: claim for entry in result["receipts"] for claim in entry["claims"]}
        self.assertEqual(claims["receipt-ticket-a-joint-desk-50"]["verdict"], "applied_before_end")
        self.assertEqual(claims["ack-joint"]["verdict"], "pending")
        self.assertIn("joint-desk", result["pending_for_follow_up"])

    def test_receipt_after_query_is_not_yet_observed(self):
        result = urgent_access.handle(self.sources, "ticket-a", 51, at=55,
                                      receipts=[{"point": "east-desk", "applied_at": 53}])
        self.assertIn("east-desk", result["acknowledged"])
        self.assertIn("joint-desk", result["pending_for_follow_up"])

    def test_expired_scope_not_reused_while_receipt_pending(self):
        for at in (51, 56, 70):
            result = self.assess("ticket-a", at, actor="x", case="E1", region="east",
                                 operation="contact_view", point="joint-desk")
            self.assertFalse(result["confirmation"]["authorized"], at)
            self.assertEqual(result["ticket"]["status"], "expired", at)
            self.assertIn("expired", result["confirmation"]["refusal_reasons"])
            self.assertIn("joint-desk", result["revocation"]["pending_for_follow_up"])

    def test_handle_rejects_end_before_approval(self):
        with self.assertRaises(urgent_access.VerificationError):
            urgent_access.handle(self.sources, "ticket-a", 39)
        with self.assertRaises(urgent_access.VerificationError):
            urgent_access.handle(self.sources, "ticket-b", 50)

    # --- 操作报告 -----------------------------------------------------

    def test_operations_keep_reports_and_state_basis_separately(self):
        result = self.assess("ticket-a", 60)
        by_id = {event["id"]: event for event in result["events"]}
        self.assertEqual(by_id["use-1"]["report"], {
            "id": "use-1", "ticket": "ticket-a", "case": "E1",
            "point": "east-desk", "operation": "contact_view", "at": 48,
        })
        self.assertTrue(by_id["use-1"]["had_basis_at_operation"])
        self.assertTrue(by_id["use-1"]["occurred_before_revocation"])

        self.assertTrue(by_id["use-2"]["occurred_after_revocation"])
        self.assertTrue(by_id["use-2"]["reported_after_revocation"])
        self.assertFalse(by_id["use-2"]["had_basis_at_operation"])
        self.assertIn("after_effective_end", by_id["use-2"]["reasons"])
        self.assertEqual(by_id["use-2"]["report"]["reported_at"], 56)

    def test_apply_plan_preserves_original_sources(self):
        before = {name: copy.deepcopy(rows) for name, rows in self.sources.items()}
        plan = urgent_access.handle(self.sources, "ticket-a", 45, at=58,
                                    receipts=[{"point": "joint-desk", "applied_at": 47}])
        with tempfile.TemporaryDirectory() as directory:
            urgent_access.apply_plan(self.sources, plan, directory)
            written = load_sources(Path(directory))
        self.assertEqual(self.sources, before)
        written_ids = {row["id"] for row in written["requests"]}
        self.assertIn("end-ticket-a-45", written_ids)
        self.assertEqual(written["checks"][-1]["revocation_applied_at"], 47)
        self.assertIn("end-a", written_ids)


if __name__ == "__main__":
    unittest.main()
