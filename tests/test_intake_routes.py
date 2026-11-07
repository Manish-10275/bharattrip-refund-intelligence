import os
os.environ.setdefault("TESTING", "True")
import json
import re
import sqlite3
import unittest
from unittest.mock import patch, MagicMock
from werkzeug.security import generate_password_hash

from app import app, init_db, get_db
from services.intake_schema import RefundIntakeExtraction, ComplaintCategory
from services.groq_service import (
    MissingAPIKeyError,
    GroqAPIError,
    InvalidResponseError,
    ExtractionValidationError,
)


class TestIntakeRoutes(unittest.TestCase):
    """
    Offline tests for AI Refund Intake Flask routes, Authentication, CSRF, and SQLite persistence.
    All Groq LLM interactions are strictly mocked with zero network calls.
    """

    def setUp(self):
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret-key-audit-12345"
        app.config["OPERATOR_USERNAME"] = "operator"
        app.config["OPERATOR_PASSWORD_HASH"] = generate_password_hash("TestPassword123!")
        app.config["WTF_CSRF_ENABLED"] = False  # Disabled by default; enabled explicitly in CSRF tests
        self.client = app.test_client()
        with app.app_context():
            init_db()

    def _get_db_connection(self):
        """Helper to open a direct SQLite connection to the database."""
        conn = sqlite3.connect(app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        return conn

    def _login(self, username="operator"):
        """Establish an authenticated operator session."""
        with self.client.session_transaction() as sess:
            sess["operator_user"] = username

    # --------------------------------------------------------------------------
    # 1-5: Unauthenticated Route Protection
    # --------------------------------------------------------------------------

    def test_01_unauthenticated_dashboard_redirects_to_login(self):
        """1. Unauthenticated GET / redirects to /login."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_02_unauthenticated_reconciliation_redirects_to_login(self):
        """2. Unauthenticated GET /reconciliation redirects to /login."""
        response = self.client.get("/reconciliation")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_03_unauthenticated_intake_redirects_to_login(self):
        """3. Unauthenticated GET /intake redirects to /login."""
        response = self.client.get("/intake")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_04_unauthenticated_intake_review_redirects_to_login(self):
        """4. Unauthenticated GET /intake/review redirects to /login."""
        response = self.client.get("/intake/review")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_05_unauthenticated_intake_detail_redirects_to_login(self):
        """5. Unauthenticated GET /intake/<id> redirects to /login."""
        response = self.client.get("/intake/1")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    # --------------------------------------------------------------------------
    # 6-7: Public Routes
    # --------------------------------------------------------------------------

    def test_06_public_health_returns_200_without_login(self):
        """6. Public GET /health returns 200 without login and minimal sanitized payload."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data.get("status"), "healthy")
        self.assertEqual(data.get("database"), "connected")
        # Ensure no customer or sensitive operational counts are leaked
        self.assertNotIn("records_summary", data)
        self.assertNotIn("reconciliation_summary", data)
        self.assertNotIn("intake_summary", data)

    def test_07_public_login_returns_200(self):
        """7. Public GET /login returns 200 with operator sign-in interface."""
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Operator Sign In", response.data)
        self.assertIn(b"username", response.data)
        self.assertIn(b"password", response.data)

    # --------------------------------------------------------------------------
    # 8-10: Authentication & Session Management
    # --------------------------------------------------------------------------

    def test_08_valid_login_establishes_session_and_redirects(self):
        """8. Valid login establishes session and redirects to intended next page."""
        response = self.client.post(
            "/login?next=/intake/review",
            data={"username": "operator", "password": "TestPassword123!"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/intake/review", response.location)

        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("operator_user"), "operator")

    def test_09_invalid_login_shows_error_and_does_not_establish_session(self):
        """9. Invalid login shows error message and does not establish session."""
        response = self.client.post(
            "/login",
            data={"username": "operator", "password": "WrongPassword!"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid operator username or password", response.data)

        with self.client.session_transaction() as sess:
            self.assertNotIn("operator_user", sess)

    def test_10_logout_terminates_session_and_redirects(self):
        """10. POST /logout terminates session and redirects to /login."""
        self._login("operator")
        response = self.client.post("/logout")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

        with self.client.session_transaction() as sess:
            self.assertNotIn("operator_user", sess)

    # --------------------------------------------------------------------------
    # 11-14: CSRF Protection
    # --------------------------------------------------------------------------

    def test_11_csrf_rejection_intake(self):
        """11. CSRF rejection: POST /intake without CSRF token returns 400 when CSRF enabled."""
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            self._login()
            response = self.client.post("/intake", data={
                "customer_message": "Customer refund message without token",
                "source_channel": "Email"
            })
            self.assertEqual(response.status_code, 400)
            self.assertIn(b"CSRF", response.data)
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_12_csrf_rejection_intake_review(self):
        """12. CSRF rejection: POST /intake/<id>/review without CSRF token returns 400."""
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            self._login()
            response = self.client.post("/intake/1/review")
            self.assertEqual(response.status_code, 400)
            self.assertIn(b"CSRF", response.data)
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_13_csrf_rejection_reconciliation_run(self):
        """13. CSRF rejection: POST /reconciliation/run without CSRF token returns 400."""
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            self._login()
            response = self.client.post("/reconciliation/run")
            self.assertEqual(response.status_code, 400)
            self.assertIn(b"CSRF", response.data)
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    @patch("app.analyze_refund_message")
    def test_14_valid_csrf_token_accepted(self, mock_analyze):
        """14. Valid CSRF token allows successful POST /intake."""
        mock_analyze.return_value = RefundIntakeExtraction(
            customer_name="Aarav Gupta",
            agency_name="MakeMyTrip",
            booking_reference="MMT110",
            refund_reference="RF-1100",
            route="DEL-BOM",
            refund_amount=5500.0,
            currency="INR",
            request_date="2026-06-15",
            complaint_category=ComplaintCategory.REFUND_STATUS,
            missing_information=[],
            summary="Valid inquiry.",
            confidence=0.95,
            human_review_required=False,
            human_review_reason=None
        )

        app.config["WTF_CSRF_ENABLED"] = True
        try:
            self._login()
            get_res = self.client.get("/intake")
            self.assertEqual(get_res.status_code, 200)

            # Extract CSRF token from rendered form
            match = re.search(r'name="csrf_token"\s+value="([^"]+)"', get_res.data.decode("utf-8"))
            self.assertIsNotNone(match, "CSRF token must be present in /intake HTML")
            csrf_token = match.group(1)

            post_res = self.client.post("/intake", data={
                "csrf_token": csrf_token,
                "customer_message": "Aarav Gupta, MMT110, RF-1100. Status please?",
                "source_channel": "Email"
            })
            self.assertEqual(post_res.status_code, 302)
            self.assertTrue(post_res.location.startswith("/intake/"))
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    # --------------------------------------------------------------------------
    # 15-17: Review Queue Lifecycle & Metrics
    # --------------------------------------------------------------------------

    def test_15_default_intake_review_displays_only_pending_review(self):
        """15. Default /intake/review displays only records with processing_status = 'PENDING_REVIEW'."""
        conn = self._get_db_connection()
        try:
            conn.execute("DELETE FROM intake_records")
            # 1. Pending review record
            conn.execute("""
                INSERT INTO intake_records (
                    original_message, source_channel, customer_name, booking_reference,
                    complaint_category, missing_information, summary, confidence, human_review_required, human_review_reason,
                    processing_status, model_name, prompt_version, extracted_json
                ) VALUES ('Pending Msg', 'Email', 'Pending Customer', 'PNR-001', 'refund_status', '[]', 'Pending summary', 0.8, 1, 'Needs check', 'PENDING_REVIEW', 'model', 'v1', '{}')
            """)
            # 2. Auto-processed record
            conn.execute("""
                INSERT INTO intake_records (
                    original_message, source_channel, customer_name, booking_reference,
                    complaint_category, missing_information, summary, confidence, human_review_required, human_review_reason,
                    processing_status, model_name, prompt_version, extracted_json
                ) VALUES ('Auto Msg', 'WhatsApp', 'Auto Customer', 'PNR-002', 'refund_status', '[]', 'Auto summary', 0.95, 0, NULL, 'PROCESSED', 'model', 'v1', '{}')
            """)
            # 3. Already reviewed record
            conn.execute("""
                INSERT INTO intake_records (
                    original_message, source_channel, customer_name, booking_reference,
                    complaint_category, missing_information, summary, confidence, human_review_required, human_review_reason,
                    processing_status, model_name, prompt_version, extracted_json
                ) VALUES ('Reviewed Msg', 'Manual', 'Reviewed Customer', 'PNR-003', 'amount_dispute', '[]', 'Reviewed summary', 0.7, 1, 'Dispute resolved', 'REVIEWED', 'model', 'v1', '{}')
            """)
            conn.commit()
        finally:
            conn.close()

        self._login()
        response = self.client.get("/intake/review")
        self.assertEqual(response.status_code, 200)

        # Pending customer should be visible in table
        self.assertIn(b"Pending Customer", response.data)
        # Processed and Reviewed customers should NOT be visible in default table
        self.assertNotIn(b"Auto Customer", response.data)
        self.assertNotIn(b"Reviewed Customer", response.data)

        # Count in queue must match pending KPI (1)
        self.assertIn(b"Showing 1 of 1 matching records", response.data)
        self.assertIn(b"Total in Queue: 1", response.data)

    def test_16_reviewed_record_transitions_and_leaves_default_queue(self):
        """16. Reviewed record transitions to REVIEWED and is removed from default queue."""
        conn = self._get_db_connection()
        try:
            conn.execute("DELETE FROM intake_records")
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO intake_records (
                    original_message, source_channel, customer_name, booking_reference,
                    complaint_category, missing_information, summary, confidence, human_review_required, human_review_reason,
                    processing_status, model_name, prompt_version, extracted_json
                ) VALUES ('Pending Msg 2', 'Email', 'Pooja Verma', 'PNR-PV1', 'no_update', '[]', 'Pooja summary', 0.6, 1, 'No status update', 'PENDING_REVIEW', 'model', 'v1', '{}')
            """)
            record_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        self._login("ops_specialist")

        # Confirm visible before review
        res_before = self.client.get("/intake/review")
        self.assertIn(b"Pooja Verma", res_before.data)

        # Perform review action
        review_post = self.client.post(f"/intake/{record_id}/review")
        self.assertEqual(review_post.status_code, 302)

        # Confirm removed from default pending queue
        res_after = self.client.get("/intake/review")
        self.assertNotIn(b"Pooja Verma", res_after.data)

        # Confirm visible under explicit ?status=REVIEWED filter
        res_reviewed = self.client.get("/intake/review?status=REVIEWED")
        self.assertEqual(res_reviewed.status_code, 200)
        self.assertIn(b"Pooja Verma", res_reviewed.data)

        # Confirm database state
        conn = self._get_db_connection()
        try:
            row = conn.execute("SELECT processing_status, reviewed_by, reviewed_at, human_review_required FROM intake_records WHERE id = ?", (record_id,)).fetchone()
            self.assertEqual(row["processing_status"], "REVIEWED")
            self.assertEqual(row["reviewed_by"], "ops_specialist")
            self.assertIsNotNone(row["reviewed_at"])
            # Historical recommendation must remain unchanged
            self.assertEqual(row["human_review_required"], 1)
        finally:
            conn.close()

    def test_17_attempt_to_review_already_reviewed_record_safely_rejected(self):
        """17. Attempt to review already-reviewed or processed record is safely rejected without error."""
        conn = self._get_db_connection()
        try:
            conn.execute("DELETE FROM intake_records")
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO intake_records (
                    original_message, source_channel, customer_name,
                    complaint_category, missing_information, summary, confidence, human_review_required,
                    processing_status, reviewed_by, reviewed_at, model_name, prompt_version, extracted_json
                ) VALUES ('Already reviewed', 'Email', 'Karan Johar', 'refund_status', '[]', 'Karan summary', 0.9, 1, 'REVIEWED', 'initial_reviewer', '2026-06-01 10:00:00', 'model', 'v1', '{}')
            """)
            record_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()

        self._login("second_reviewer")

        # Second review attempt
        response = self.client.post(f"/intake/{record_id}/review")
        # Should gracefully redirect without crash or mutation
        self.assertEqual(response.status_code, 302)

        conn = self._get_db_connection()
        try:
            row = conn.execute("SELECT processing_status, reviewed_by, reviewed_at FROM intake_records WHERE id = ?", (record_id,)).fetchone()
            self.assertEqual(row["processing_status"], "REVIEWED")
            self.assertEqual(row["reviewed_by"], "initial_reviewer")
            self.assertEqual(row["reviewed_at"], "2026-06-01 10:00:00")
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # 18: Verbatim Customer Input Preservation
    # --------------------------------------------------------------------------

    @patch("app.analyze_refund_message")
    def test_18_exact_original_message_with_whitespace_preserved_verbatim(self, mock_analyze):
        """18. Exact original customer message with whitespace, tabs, and newlines is preserved verbatim in SQLite."""
        mock_extraction = RefundIntakeExtraction(
            customer_name="Neeraj Chopra",
            agency_name=None,
            booking_reference="OLY-2024",
            refund_reference="RF-9999",
            route="DEL-PAR",
            refund_amount=15000.0,
            currency="INR",
            request_date="2026-06-18",
            complaint_category=ComplaintCategory.REFUND_STATUS,
            missing_information=[],
            summary="Athlete refund request.",
            confidence=0.98,
            human_review_required=False,
            human_review_reason=None
        )
        mock_analyze.return_value = mock_extraction

        raw_verbatim = "  \n\n  Dear BharatTrip Team,\n\tI need a prompt refund for PNR OLY-2024.\n   Amount: INR 15000.\n  Thanks!  \t\n"

        self._login()
        post_res = self.client.post("/intake", data={
            "customer_message": raw_verbatim,
            "source_channel": "Email"
        })
        self.assertEqual(post_res.status_code, 302)
        record_id = int(post_res.location.split("/")[-1])

        # Verify analyze_refund_message received the verbatim string
        mock_analyze.assert_called_once_with(raw_verbatim)

        # Verify SQLite stored exact string without stripping
        conn = self._get_db_connection()
        try:
            row = conn.execute("SELECT original_message FROM intake_records WHERE id = ?", (record_id,)).fetchone()
            self.assertEqual(row["original_message"], raw_verbatim)
            self.assertTrue(row["original_message"].startswith("  \n\n  Dear"))
            self.assertTrue(row["original_message"].endswith("  \t\n"))
        finally:
            conn.close()

    # --------------------------------------------------------------------------
    # Additional Robustness & Financial Safety Tests
    # --------------------------------------------------------------------------

    def test_19_empty_and_whitespace_input_rejected(self):
        """Verify empty and whitespace inputs are rejected with 400."""
        self._login()
        res_empty = self.client.post("/intake", data={
            "customer_message": "",
            "source_channel": "Email"
        })
        self.assertEqual(res_empty.status_code, 400)
        self.assertIn(b"empty or whitespace", res_empty.data)

        res_ws = self.client.post("/intake", data={
            "customer_message": "   \n\t  ",
            "source_channel": "WhatsApp"
        })
        self.assertEqual(res_ws.status_code, 400)
        self.assertIn(b"empty or whitespace", res_ws.data)

    @patch("app.analyze_refund_message")
    def test_20_marking_reviewed_does_not_change_refund_or_finance_status(self, mock_analyze):
        """Verify marking an intake record reviewed never alters Support or Finance tables."""
        conn = self._get_db_connection()
        try:
            support_count_before = conn.execute("SELECT COUNT(*) FROM support_records").fetchone()[0]
            finance_count_before = conn.execute("SELECT COUNT(*) FROM finance_records").fetchone()[0]
            finance_done_before = conn.execute("SELECT COUNT(*) FROM finance_records WHERE payout_status = 'Refund Done'").fetchone()[0]
        finally:
            conn.close()

        mock_extraction = RefundIntakeExtraction(
            customer_name="Financial Test Passenger",
            agency_name=None,
            booking_reference=None,
            refund_reference=None,
            route=None,
            refund_amount=None,
            currency=None,
            request_date=None,
            complaint_category=ComplaintCategory.OTHER,
            missing_information=[],
            summary="Financial safety test.",
            confidence=0.5,
            human_review_required=True,
            human_review_reason="Test reason."
        )
        mock_analyze.return_value = mock_extraction

        self._login()
        post_res = self.client.post("/intake", data={
            "customer_message": "General feedback message for financial safety test.",
            "source_channel": "Manual"
        })
        record_id = int(post_res.location.split("/")[-1])

        # Mark reviewed
        self.client.post(f"/intake/{record_id}/review")

        # Verify operational financial/support tables are completely unchanged
        conn = self._get_db_connection()
        try:
            support_count_after = conn.execute("SELECT COUNT(*) FROM support_records").fetchone()[0]
            finance_count_after = conn.execute("SELECT COUNT(*) FROM finance_records").fetchone()[0]
            finance_done_after = conn.execute("SELECT COUNT(*) FROM finance_records WHERE payout_status = 'Refund Done'").fetchone()[0]

            self.assertEqual(support_count_before, support_count_after)
            self.assertEqual(finance_count_before, finance_count_after)
            self.assertEqual(finance_done_before, finance_done_after)
        finally:
            conn.close()

    @patch("app.analyze_refund_message")
    def test_21_api_errors_handled_without_exposing_secrets(self, mock_analyze):
        """Verify API errors present safe error messages without exposing API keys or tracebacks."""
        secret_key = "gsk_sensitive_secret_token_xyz999"
        self._login()

        # Case A: Missing API Key
        mock_analyze.side_effect = MissingAPIKeyError("GROQ_API_KEY is not configured.")
        res_missing = self.client.post("/intake", data={"customer_message": "Check my refund."})
        self.assertEqual(res_missing.status_code, 503)
        self.assertIn(b"Groq API Key is not configured", res_missing.data)

        # Case B: Groq API Error with secret redaction
        mock_analyze.side_effect = GroqAPIError("Unauthorized connection using [REDACTED]")
        res_api_err = self.client.post("/intake", data={"customer_message": "Check my refund."})
        self.assertEqual(res_api_err.status_code, 502)
        self.assertIn(b"Communication with Groq AI service failed", res_api_err.data)
        self.assertNotIn(secret_key.encode("utf-8"), res_api_err.data)

        # Case C: Invalid Model Response
        mock_analyze.side_effect = InvalidResponseError("Model returned non-JSON")
        res_inv = self.client.post("/intake", data={"customer_message": "Check my refund."})
        self.assertEqual(res_inv.status_code, 502)
        self.assertIn(b"invalid response format", res_inv.data)

    def test_22_existing_workbook_data_remains_unchanged(self):
        """Verify core ingested tables retain exact row counts after intake operations."""
        conn = self._get_db_connection()
        try:
            support_rows = conn.execute("SELECT COUNT(*) FROM support_records").fetchone()[0]
            finance_rows = conn.execute("SELECT COUNT(*) FROM finance_records").fetchone()[0]
            escalation_rows = conn.execute("SELECT COUNT(*) FROM escalation_records").fetchone()[0]

            self.assertEqual(support_rows, 755, "Support records must remain 755")
            self.assertEqual(finance_rows, 689, "Finance records must remain 689")
            self.assertEqual(escalation_rows, 155, "Escalation records must remain 155")
        finally:
            conn.close()

    def test_23_dashboard_kpis_pending_support_and_pending_ai_intake(self):
        """Verify main dashboard displays both Pending Support Triage and Pending AI Intake KPIs."""
        self._login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Pending Support Triage", response.data)
        self.assertIn(b"Pending AI Intake", response.data)


if __name__ == "__main__":
    unittest.main()
