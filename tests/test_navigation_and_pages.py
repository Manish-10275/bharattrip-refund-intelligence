import os
os.environ.setdefault("TESTING", "True")
import sqlite3
import unittest
from werkzeug.security import generate_password_hash

from app import app, init_db


class TestNavigationAndPages(unittest.TestCase):
    """
    Automated regression tests for Task 5A:
    - Sidebar navigation destinations (/escalations, /refund-records, /assistant)
    - Authentication protection and HTTP response codes
    - Active navigation state in templates
    - Escalation counts, filtering, search, and pagination
    - Support and Finance status separation on Refund Records
    - Operations Assistant truthfulness without fabricated tools
    - Immutability of core database records across navigation
    """

    def setUp(self):
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret-key-audit-12345"
        app.config["OPERATOR_USERNAME"] = "operator"
        app.config["OPERATOR_PASSWORD_HASH"] = generate_password_hash("TestPassword123!")
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        with app.app_context():
            init_db()

    def _login(self, username="operator"):
        """Establish an authenticated operator session."""
        with self.client.session_transaction() as sess:
            sess["operator_user"] = username

    def _get_db_connection(self):
        """Helper to open a direct SQLite connection to the database."""
        conn = sqlite3.connect(app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        return conn

    # --------------------------------------------------------------------------
    # 1. Authentication Protection for Sidebar Destinations
    # --------------------------------------------------------------------------

    def test_01_unauthenticated_escalations_redirects_to_login(self):
        """Unauthenticated GET /escalations redirects to /login."""
        response = self.client.get("/escalations")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_02_unauthenticated_refund_records_redirects_to_login(self):
        """Unauthenticated GET /refund-records redirects to /login."""
        response = self.client.get("/refund-records")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_03_unauthenticated_operations_assistant_redirects_to_login(self):
        """Unauthenticated GET /assistant redirects to /login."""
        response = self.client.get("/assistant")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    # --------------------------------------------------------------------------
    # 2. Authenticated Status Codes & Active Navigation State
    # --------------------------------------------------------------------------

    def test_04_authenticated_escalations_status_200_and_active_nav(self):
        """Authenticated GET /escalations returns 200 and activates 'escalations' nav link."""
        self._login()
        response = self.client.get("/escalations")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertIn("Dispute Escalations Ledger", html)
        # Check active navigation class on escalations link
        self.assertIn('href="/escalations" class="nav-item active"', html)

    def test_05_authenticated_refund_records_status_200_and_active_nav(self):
        """Authenticated GET /refund-records returns 200 and activates 'records' nav link."""
        self._login()
        response = self.client.get("/refund-records")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertIn("Unified Refund Records Ledger", html)
        # Check active navigation class on refund-records link
        self.assertIn('href="/refund-records" class="nav-item active"', html)

    def test_06_authenticated_operations_assistant_status_200_and_active_nav(self):
        """Authenticated GET /assistant returns 200 and activates 'assistant' nav link."""
        self._login()
        response = self.client.get("/assistant")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")
        self.assertIn("Refund Operations Assistant", html)
        # Check active navigation class on assistant link
        self.assertIn('href="/assistant" class="nav-item active"', html)

    def test_07_sidebar_has_no_inert_hash_anchors(self):
        """Verify sidebar navigation contains no '#escalations', '#records', or '#assistant' placeholder anchors."""
        self._login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")

        # Verify placeholders are replaced with actual routes
        self.assertNotIn('href="#escalations"', html)
        self.assertNotIn('href="#records"', html)
        self.assertNotIn('href="#assistant"', html)

        self.assertIn('href="/escalations"', html)
        self.assertIn('href="/refund-records"', html)
        self.assertIn('href="/assistant"', html)

    # --------------------------------------------------------------------------
    # 3. Escalations Ledger: Counts, Filtering, Search & Pagination
    # --------------------------------------------------------------------------

    def test_08_escalations_counts_match_sqlite_database(self):
        """Escalation counts in HTML match exact records in SQLite database."""
        conn = self._get_db_connection()
        try:
            total_db = conn.execute("SELECT COUNT(*) FROM escalation_records").fetchone()[0]
            open_db = conn.execute("SELECT COUNT(*) FROM escalation_records WHERE resolved_on IS NULL").fetchone()[0]
            resolved_db = conn.execute("SELECT COUNT(*) FROM escalation_records WHERE resolved_on IS NOT NULL").fetchone()[0]

            self.assertEqual(total_db, 155)
            self.assertEqual(open_db, 85)
            self.assertEqual(resolved_db, 70)
        finally:
            conn.close()

        self._login()
        response = self.client.get("/escalations")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")

        self.assertIn(f"{total_db} Records", html)
        self.assertIn(f"{open_db} Open", html)
        self.assertIn(f"{resolved_db} Resolved", html)

    def test_09_escalations_status_filters(self):
        """Filter escalations by resolution status (OPEN vs RESOLVED)."""
        self._login()

        # Open only: 85 records
        resp_open = self.client.get("/escalations?status=OPEN")
        self.assertEqual(resp_open.status_code, 200)
        html_open = resp_open.data.decode("utf-8")
        self.assertIn("85 matching escalations", html_open)
        self.assertIn("status-open", html_open)

        # Resolved only: 70 records
        resp_res = self.client.get("/escalations?status=RESOLVED")
        self.assertEqual(resp_res.status_code, 200)
        html_res = resp_res.data.decode("utf-8")
        self.assertIn("70 matching escalations", html_res)
        self.assertIn("status-resolved", html_res)

    def test_10_escalations_channel_filters(self):
        """Filter escalations by channel (Email, Phone, WhatsApp)."""
        self._login()

        # WhatsApp: 36 records
        resp_wa = self.client.get("/escalations?channel=WhatsApp")
        self.assertEqual(resp_wa.status_code, 200)
        html_wa = resp_wa.data.decode("utf-8")
        self.assertIn("36 matching escalations", html_wa)
        self.assertIn("WhatsApp", html_wa)

        # Email: 77 records
        resp_email = self.client.get("/escalations?channel=Email")
        self.assertEqual(resp_email.status_code, 200)
        html_email = resp_email.data.decode("utf-8")
        self.assertIn("77 matching escalations", html_email)

    def test_11_escalations_search_by_reference_and_agency(self):
        """Search escalations by escalation ID, related ticket, or agency name."""
        self._login()

        # Search by escalation ID
        resp_id = self.client.get("/escalations?search=ESC-501")
        self.assertEqual(resp_id.status_code, 200)
        html_id = resp_id.data.decode("utf-8")
        self.assertIn("ESC-501", html_id)
        self.assertIn("RF-1325", html_id)

        # Search by related ticket ref
        resp_ref = self.client.get("/escalations?search=RF-1096")
        self.assertEqual(resp_ref.status_code, 200)
        html_ref = resp_ref.data.decode("utf-8")
        self.assertIn("ESC-502", html_ref)

        # Search by agency name
        resp_agency = self.client.get("/escalations?search=TripHub")
        self.assertEqual(resp_agency.status_code, 200)
        html_agency = resp_agency.data.decode("utf-8")
        self.assertIn("TripHub", html_agency)

    def test_12_escalations_pagination(self):
        """Escalations pagination traverses pages correctly."""
        self._login()

        # Page 1 (25 per page out of 155 = 7 total pages)
        resp_p1 = self.client.get("/escalations?page=1")
        self.assertEqual(resp_p1.status_code, 200)
        html_p1 = resp_p1.data.decode("utf-8")
        self.assertIn("Page 1 of 7", html_p1)
        self.assertIn("Next →", html_p1)

        # Page 2
        resp_p2 = self.client.get("/escalations?page=2")
        self.assertEqual(resp_p2.status_code, 200)
        html_p2 = resp_p2.data.decode("utf-8")
        self.assertIn("Page 2 of 7", html_p2)
        self.assertIn("← Previous", html_p2)

    # --------------------------------------------------------------------------
    # 4. Refund Records: Support vs Finance Separation & Disbursement Evidence
    # --------------------------------------------------------------------------

    def test_13_refund_records_counts_and_classification_filters(self):
        """Refund records counts match latest reconciliation run and filter by classification."""
        conn = self._get_db_connection()
        try:
            run_id = conn.execute("SELECT MAX(id) FROM reconciliation_runs").fetchone()[0]
            total_cand = conn.execute("SELECT COUNT(*) FROM reconciliation_results WHERE run_id = ?", (run_id,)).fetchone()[0]
            matched_cand = conn.execute("SELECT COUNT(*) FROM reconciliation_results WHERE run_id = ? AND classification = 'MATCHED'", (run_id,)).fetchone()[0]
            review_cand = conn.execute("SELECT COUNT(*) FROM reconciliation_results WHERE run_id = ? AND review_required = 1", (run_id,)).fetchone()[0]

            self.assertEqual(total_cand, 789)
            self.assertEqual(matched_cand, 571)
            self.assertEqual(review_cand, 218)
        finally:
            conn.close()

        self._login()
        response = self.client.get("/refund-records")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")

        self.assertIn(f"{total_cand} Records", html)
        self.assertIn(f"{matched_cand} Matched", html)
        self.assertIn(f"{review_cand} Discrepancies", html)

        # Filter by MATCHED
        resp_matched = self.client.get("/refund-records?classification=MATCHED")
        self.assertEqual(resp_matched.status_code, 200)
        html_matched = resp_matched.data.decode("utf-8")
        self.assertIn("571 matching records", html_matched)

        # Filter by UNRESOLVED
        resp_unres = self.client.get("/refund-records?classification=UNRESOLVED")
        self.assertEqual(resp_unres.status_code, 200)
        html_unres = resp_unres.data.decode("utf-8")
        self.assertIn("218 matching records", html_unres)

    def test_14_refund_records_clearly_distinguishes_support_and_finance_statuses(self):
        """Refund Records table contains separate Support Status and Finance Status columns and data."""
        self._login()
        response = self.client.get("/refund-records")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")

        # Distinct column headers
        self.assertIn("Support Status", html)
        self.assertIn("Finance Status", html)
        self.assertIn("Payout Evidence", html)

        # Inspect status mismatch case (RF-1001: Support Escalated, Finance Pending Payout)
        resp_mismatch = self.client.get("/refund-records?search=RF-1001")
        self.assertEqual(resp_mismatch.status_code, 200)
        html_mismatch = resp_mismatch.data.decode("utf-8")
        self.assertIn("status-escalated", html_mismatch)
        self.assertIn("status-pending payout", html_mismatch)
        self.assertIn("MISMATCH", html_mismatch)

    def test_15_refund_records_disbursement_verification_and_guardrail_callouts(self):
        """Refund Records page displays operational notice that Support closure does not prove payout."""
        self._login()
        response = self.client.get("/refund-records")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")

        # Policy callout verified
        self.assertIn("Operational Policy & Audit Safeguard", html)
        self.assertIn("Support ticket closure (<code>Closed</code>) merely documents that customer service resolved communication", html)
        self.assertIn("does <strong>not</strong> prove bank disbursement", html)

        # Verified Payout badge exists for corroborated claims
        self.assertIn("Verified Payout", html)

    def test_16_refund_records_search_by_refund_and_booking_reference(self):
        """Search refund records by refund reference and booking reference."""
        self._login()

        # Search by reference RF-1003
        resp_ref = self.client.get("/refund-records?search=RF-1003")
        self.assertEqual(resp_ref.status_code, 200)
        html_ref = resp_ref.data.decode("utf-8")
        self.assertIn("RF-1003", html_ref)
        self.assertIn("17800.00", html_ref)

        # Search by agency
        resp_agency = self.client.get("/refund-records?search=GoFly")
        self.assertEqual(resp_agency.status_code, 200)
        html_agency = resp_agency.data.decode("utf-8")
        self.assertIn("GoFly Holidays", html_agency)

    def test_17_refund_records_pagination(self):
        """Refund records pagination traverses candidates correctly."""
        self._login()

        # Page 1 (25 per page out of 789 = 32 total pages)
        resp_p1 = self.client.get("/refund-records?page=1")
        self.assertEqual(resp_p1.status_code, 200)
        html_p1 = resp_p1.data.decode("utf-8")
        self.assertIn("Page 1 of 32", html_p1)
        self.assertIn("Next →", html_p1)

        # Page 2
        resp_p2 = self.client.get("/refund-records?page=2")
        self.assertEqual(resp_p2.status_code, 200)
        html_p2 = resp_p2.data.decode("utf-8")
        self.assertIn("Page 2 of 32", html_p2)
        self.assertIn("← Previous", html_p2)

    # --------------------------------------------------------------------------
    # 5. Operations Assistant Truthfulness & No Fabricated Tools
    # --------------------------------------------------------------------------

    def test_18_operations_assistant_does_not_show_fabricated_results_or_fake_chat(self):
        """Operations Assistant outlines real available tools and clearly flags roadmap features."""
        self._login()
        response = self.client.get("/assistant")
        self.assertEqual(response.status_code, 200)
        html = response.data.decode("utf-8")

        # Honest overview
        self.assertIn("Refund Operations Assistant", html)
        self.assertIn("Currently Available Operational Workflows", html)
        self.assertIn("Planned Future Capabilities", html)

        # Working links to live production workflows
        self.assertIn('href="/intake"', html)
        self.assertIn('href="/intake/review"', html)
        self.assertIn('href="/reconciliation"', html)
        self.assertIn('href="/escalations"', html)
        self.assertIn('href="/refund-records"', html)

        # Explicitly marked roadmap items
        self.assertIn("Carrier Dispute Auto-Filing", html)
        self.assertIn("Airline Policy & Fare Rule Retrieval", html)
        self.assertIn("Planned", html)

        # Ensure no fake chatbot input box exists
        self.assertNotIn('<input type="text" placeholder="Ask assistant"', html)
        self.assertNotIn('<textarea placeholder="Ask assistant"', html)

    # --------------------------------------------------------------------------
    # 6. Database Immutability Across All Operations
    # --------------------------------------------------------------------------

    def test_19_sidebar_and_page_visits_do_not_mutate_source_records(self):
        """Visiting sidebar pages performs read-only operations and strictly preserves database counts."""
        conn = self._get_db_connection()
        try:
            supp_before = conn.execute("SELECT COUNT(*) FROM support_records").fetchone()[0]
            fin_before = conn.execute("SELECT COUNT(*) FROM finance_records").fetchone()[0]
            esc_before = conn.execute("SELECT COUNT(*) FROM escalation_records").fetchone()[0]
            rec_before = conn.execute("SELECT COUNT(*) FROM reconciliation_results").fetchone()[0]
        finally:
            conn.close()

        self._login()
        # Visit all pages
        self.client.get("/")
        self.client.get("/escalations")
        self.client.get("/escalations?status=OPEN&channel=Email")
        self.client.get("/refund-records")
        self.client.get("/refund-records?classification=STATUS_MISMATCH")
        self.client.get("/assistant")

        conn = self._get_db_connection()
        try:
            supp_after = conn.execute("SELECT COUNT(*) FROM support_records").fetchone()[0]
            fin_after = conn.execute("SELECT COUNT(*) FROM finance_records").fetchone()[0]
            esc_after = conn.execute("SELECT COUNT(*) FROM escalation_records").fetchone()[0]
            rec_after = conn.execute("SELECT COUNT(*) FROM reconciliation_results").fetchone()[0]
            latest_run_results = conn.execute("SELECT COUNT(*) FROM reconciliation_results WHERE run_id = (SELECT MAX(id) FROM reconciliation_runs)").fetchone()[0]

            self.assertEqual(supp_before, supp_after, "Support records must remain unchanged")
            self.assertEqual(fin_before, fin_after, "Finance records must remain unchanged")
            self.assertEqual(esc_before, esc_after, "Escalation records must remain unchanged")
            self.assertEqual(rec_before, rec_after, "Reconciliation records must remain unchanged across page visits")

            self.assertEqual(supp_after, 755)
            self.assertEqual(fin_after, 689)
            self.assertEqual(esc_after, 155)
            self.assertEqual(latest_run_results, 789)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
