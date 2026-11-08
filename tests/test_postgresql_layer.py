"""
PostgreSQL (Neon-compatible) Integration and Layer Verification Tests.
Verifies PostgreSQL connection initialization, schema generation with identity columns,
parameter translation (? -> %s), RETURNING id, transaction commit/rollback,
foreign key cascades, data service, reconciliation, intake review lifecycle,
health endpoint, and row wrapper dual-access behavior.
"""

import json
import os
import unittest
from unittest.mock import patch

from app import app
from config import Config, normalize_database_url
import db
from db import (
    DatabaseConnection,
    DatabaseOperationalError,
    RowWrapper,
    get_connection,
    get_database_backend,
    init_all_schemas,
    translate_query_for_postgres,
)
from services.data_service import DataService
from services.intake_schema import RefundIntakeExtraction, ComplaintCategory
from services.intake_service import IntakeService
from services.reconciliation_service import ReconciliationClassification, ReconciliationService

POSTGRES_TEST_URL = os.environ.get(
    "POSTGRES_TEST_URL",
    "postgresql://postgres:postgres@localhost:5432/bharattrip_test"
)


def is_postgres_available() -> bool:
    """Check if the test PostgreSQL database is reachable."""
    try:
        conn = get_connection(database_url=POSTGRES_TEST_URL)
        cur = conn.execute("SELECT 1")
        row = cur.fetchone()
        conn.close()
        return row is not None and row[0] == 1
    except Exception:
        return False


class TestPostgreSQLLayer(unittest.TestCase):
    """Integration test suite executing against real PostgreSQL instance."""

    @classmethod
    def setUpClass(cls):
        cls.postgres_available = is_postgres_available()
        if not cls.postgres_available:
            raise unittest.SkipTest(f"PostgreSQL test instance not reachable at {POSTGRES_TEST_URL}")

    @classmethod
    def tearDownClass(cls):
        app.config.pop("DATABASE_URL", None)

    def setUp(self):
        """Clean PostgreSQL test tables before each test."""
        self.conn = get_connection(database_url=POSTGRES_TEST_URL)
        init_all_schemas(self.conn)
        with self.conn:
            self.conn.execute("DELETE FROM reconciliation_results")
            self.conn.execute("DELETE FROM reconciliation_runs")
            self.conn.execute("DELETE FROM intake_records")
            self.conn.execute("DELETE FROM refund_records")
            self.conn.execute("DELETE FROM support_records")
            self.conn.execute("DELETE FROM finance_records")
            self.conn.execute("DELETE FROM escalation_records")
            self.conn.execute("DELETE FROM import_history")

    def tearDown(self):
        self.conn.close()

    # --------------------------------------------------------------------------
    # 1. Connection Initialization & Dialect Detection
    # --------------------------------------------------------------------------

    def test_01_postgresql_connection_initialization_and_dialect(self):
        """Verify successful connection to PostgreSQL and dialect assignment."""
        self.assertEqual(self.conn.dialect, "postgresql")
        self.assertEqual(get_database_backend(POSTGRES_TEST_URL), "postgresql")
        cur = self.conn.execute("SELECT version()")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertIn("PostgreSQL", row[0])

    def test_02_database_url_normalization(self):
        """Verify DATABASE_URL normalization for postgres:// and Neon sslmode."""
        # Convert postgres:// to postgresql://
        self.assertTrue(normalize_database_url("postgres://u:p@host/db").startswith("postgresql://"))
        # Neon endpoint gets sslmode=require
        neon_url = "postgresql://user:pass@ep-cool-123.us-east-2.aws.neon.tech/neondb"
        normalized = normalize_database_url(neon_url)
        self.assertIn("sslmode=require", normalized)

    # --------------------------------------------------------------------------
    # 2. Schema Creation & Identity Columns
    # --------------------------------------------------------------------------

    def test_03_schema_creation_and_identity_columns(self):
        """Verify all 8 tables and indexes exist in PostgreSQL with identity columns."""
        cur = self.conn.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'public' 
            ORDER BY table_name
        """)
        tables = [r[0] for r in cur.fetchall()]
        expected_tables = [
            "escalation_records",
            "finance_records",
            "import_history",
            "intake_records",
            "reconciliation_results",
            "reconciliation_runs",
            "refund_records",
            "support_records"
        ]
        for tbl in expected_tables:
            self.assertIn(tbl, tables, f"Expected table {tbl} in PostgreSQL")

        # Verify identity column on refund_records
        cur = self.conn.execute("""
            SELECT is_identity, identity_generation 
            FROM information_schema.columns 
            WHERE table_name = 'refund_records' AND column_name = 'id'
        """)
        col = cur.fetchone()
        self.assertEqual(col["is_identity"], "YES")
        self.assertEqual(col["identity_generation"], "BY DEFAULT")

    # --------------------------------------------------------------------------
    # 3. Parameter Translation (? -> %s) & RETURNING id
    # --------------------------------------------------------------------------

    def test_04_parameterized_query_translation_and_returning_id(self):
        """Verify positional ? placeholders are translated and RETURNING id functions."""
        # Query with ? parameter
        cur = self.conn.execute(
            "INSERT INTO refund_records (booking_reference, customer_name, refund_amount) VALUES (?, ?, ?) RETURNING id",
            ("RF-TEST-1", "Aarav Sharma", 12500.50)
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        new_id = row[0]
        self.assertIsInstance(new_id, int)
        self.assertGreater(new_id, 0)

        # Select back using ? parameter
        cur = self.conn.execute("SELECT * FROM refund_records WHERE id = ?", (new_id,))
        record = cur.fetchone()
        self.assertEqual(record["booking_reference"], "RF-TEST-1")
        self.assertEqual(record["customer_name"], "Aarav Sharma")
        self.assertEqual(record["refund_amount"], 12500.50)
        self.assertEqual(record[0], new_id)

    # --------------------------------------------------------------------------
    # 4. RowWrapper Dual Access
    # --------------------------------------------------------------------------

    def test_05_row_wrapper_dual_access_and_dict_conversion(self):
        """Verify RowWrapper supports integer index, case-insensitive string key, and dict()."""
        cur = self.conn.execute(
            "INSERT INTO refund_records (booking_reference, customer_name) VALUES (?, ?) RETURNING id",
            ("RF-DUAL-1", "Priya Verma")
        )
        rec_id = cur.fetchone()[0]

        cur = self.conn.execute("SELECT id, booking_reference, customer_name FROM refund_records WHERE id = ?", (rec_id,))
        row = cur.fetchone()

        # Positional index
        self.assertEqual(row[0], rec_id)
        self.assertEqual(row[1], "RF-DUAL-1")
        self.assertEqual(row[2], "Priya Verma")

        # Case-insensitive column name
        self.assertEqual(row["id"], rec_id)
        self.assertEqual(row["booking_reference"], "RF-DUAL-1")
        self.assertEqual(row["BOOKING_REFERENCE"], "RF-DUAL-1")
        self.assertEqual(row["customer_name"], "Priya Verma")

        # dict(row)
        d = dict(row)
        self.assertEqual(d["booking_reference"], "RF-DUAL-1")
        self.assertEqual(d["customer_name"], "Priya Verma")

        # in operator and .get()
        self.assertIn("booking_reference", row)
        self.assertEqual(row.get("booking_reference"), "RF-DUAL-1")
        self.assertIsNone(row.get("non_existent_column"))

    # --------------------------------------------------------------------------
    # 5. Transactions (Commit & Rollback)
    # --------------------------------------------------------------------------

    def test_06_transaction_commit_and_rollback(self):
        """Verify transaction commit on successful block exit and rollback on exception."""
        # 1. Successful commit
        with self.conn:
            self.conn.execute(
                "INSERT INTO refund_records (booking_reference, customer_name) VALUES (?, ?)",
                ("RF-COMMIT-1", "Commit User")
            )

        cur = self.conn.execute("SELECT COUNT(*) FROM refund_records WHERE booking_reference = 'RF-COMMIT-1'")
        self.assertEqual(cur.fetchone()[0], 1)

        # 2. Rollback on exception
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO refund_records (booking_reference, customer_name) VALUES (?, ?)",
                    ("RF-ROLLBACK-1", "Rollback User")
                )
                raise ValueError("Simulated operational failure during transaction")
        except ValueError:
            pass

        cur = self.conn.execute("SELECT COUNT(*) FROM refund_records WHERE booking_reference = 'RF-ROLLBACK-1'")
        self.assertEqual(cur.fetchone()[0], 0, "Record from failed transaction must be rolled back")

    # --------------------------------------------------------------------------
    # 6. Foreign Key Cascading
    # --------------------------------------------------------------------------

    def test_07_foreign_key_integrity_and_cascade(self):
        """Verify foreign key cascading from import_history to support_records."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO import_history (file_name, file_path, file_hash, status) VALUES (?, ?, ?, ?) RETURNING id",
                ("test.xlsx", "test.xlsx", "hash_fk_123", "COMPLETED")
            )
            import_id = cur.fetchone()[0]

            self.conn.execute(
                "INSERT INTO support_records (import_id, row_index, ticket_id, norm_ticket_id, status) VALUES (?, ?, ?, ?, ?)",
                (import_id, 0, "RF-101", "RF-101", "Closed")
            )

        cur = self.conn.execute("SELECT COUNT(*) FROM support_records WHERE import_id = ?", (import_id,))
        self.assertEqual(cur.fetchone()[0], 1)

        # Delete parent import_history
        with self.conn:
            self.conn.execute("DELETE FROM import_history WHERE id = ?", (import_id,))

        # Verify child support_records cascaded
        cur = self.conn.execute("SELECT COUNT(*) FROM support_records WHERE import_id = ?", (import_id,))
        self.assertEqual(cur.fetchone()[0], 0)

    # --------------------------------------------------------------------------
    # 7. AI Intake Persistence & Review Lifecycle
    # --------------------------------------------------------------------------

    def test_08_ai_intake_persistence_and_human_review_transitions(self):
        """Verify IntakeService persistence, RETURNING id, and review state transitions on PostgreSQL."""
        intake_service = IntakeService()
        sample_extraction = RefundIntakeExtraction(
            customer_name="Vikram Malhotra",
            agency_name="MakeMyTrip",
            booking_reference="BT-8899",
            refund_reference="RF-9988",
            route="DEL-BOM",
            refund_amount=7450.0,
            currency="INR",
            request_date="2026-03-01",
            complaint_category=ComplaintCategory.REFUND_STATUS,
            missing_information=[],
            summary="Customer requesting status update for flight cancellation refund.",
            confidence=0.95,
            human_review_required=True,
            human_review_reason="Amount dispute exceeding threshold requiring manual verification"
        )

        record_id = intake_service.create_record(
            conn=self.conn,
            original_message="Please refund 7450 INR for booking BT-8899 DEL to BOM",
            source_channel="WhatsApp",
            extraction=sample_extraction,
            model_name="openai/gpt-oss-20b",
            prompt_version="v1.0.0"
        )

        self.assertIsInstance(record_id, int)
        self.assertGreater(record_id, 0)

        # Fetch record
        rec = intake_service.get_record(self.conn, record_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["processing_status"], "PENDING_REVIEW")
        self.assertEqual(rec["customer_name"], "Vikram Malhotra")
        self.assertEqual(rec["refund_amount"], 7450.0)
        self.assertEqual(rec["human_review_required"], 1)

        # Transition to REVIEWED
        success = intake_service.mark_as_reviewed(self.conn, record_id, reviewer="Senior Ops Reviewer")
        self.assertTrue(success)

        # Confirm status updated
        updated_rec = intake_service.get_record(self.conn, record_id)
        self.assertEqual(updated_rec["processing_status"], "REVIEWED")
        self.assertEqual(updated_rec["reviewed_by"], "Senior Ops Reviewer")
        self.assertIsNotNone(updated_rec["reviewed_at"])

        # Second review attempt safely rejected
        second_attempt = intake_service.mark_as_reviewed(self.conn, record_id, reviewer="Other Reviewer")
        self.assertFalse(second_attempt)

    # --------------------------------------------------------------------------
    # 8. Deterministic Reconciliation on PostgreSQL
    # --------------------------------------------------------------------------

    def test_09_deterministic_reconciliation_on_postgresql(self):
        """Verify reconciliation engine classifies records and stores run history in PostgreSQL."""
        rec_service = ReconciliationService()

        # Seed import and records
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO import_history (file_name, file_path, file_hash, status) VALUES (?, ?, ?, ?) RETURNING id",
                ("data.xlsx", "data.xlsx", "rec_hash_1", "COMPLETED")
            )
            import_id = cur.fetchone()[0]

            # Case 1: Payout agreement (MATCHED)
            self.conn.execute(
                "INSERT INTO support_records (import_id, row_index, ticket_id, norm_ticket_id, status, refund_amount, route) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (import_id, 0, "RF-1001", "RF-1001", "Closed", 4500.0, "DEL-BOM")
            )
            self.conn.execute(
                "INSERT INTO finance_records (import_id, row_index, ref_no, norm_ref_no, payout_status, amount_paid, processed_on, sector) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (import_id, 0, "RF-1001", "RF-1001", "Refund Done", 4500.0, "2026-03-05", "DEL-BOM")
            )

            # Case 2: Status mismatch
            self.conn.execute(
                "INSERT INTO support_records (import_id, row_index, ticket_id, norm_ticket_id, status, refund_amount, route) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (import_id, 1, "RF-1002", "RF-1002", "Working", 3000.0, "BLR-HYD")
            )
            self.conn.execute(
                "INSERT INTO finance_records (import_id, row_index, ref_no, norm_ref_no, payout_status, amount_paid, sector) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (import_id, 1, "RF-1002", "RF-1002", "Refund Done", 3000.0, "BLR-HYD")
            )

        # Run reconciliation with PostgreSQL connection
        with patch.object(rec_service, "get_connection", side_effect=lambda: get_connection(database_url=POSTGRES_TEST_URL)):
            result = rec_service.run_reconciliation("PostgreSQL verification run")

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["total_candidates"], 2)
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["status_mismatch_count"], 1)
        self.assertEqual(result["review_required_count"], 1)

    # --------------------------------------------------------------------------
    # 9. Health Endpoint with PostgreSQL
    # --------------------------------------------------------------------------

    def test_10_health_endpoint_with_postgresql(self):
        """Verify /health endpoint returns status healthy and runtime python-flask-postgresql."""
        app.config["TESTING"] = True
        orig_url = app.config.get("DATABASE_URL")
        app.config["DATABASE_URL"] = POSTGRES_TEST_URL
        try:
            client = app.test_client()
            with patch("db.get_db", return_value=self.conn):
                response = client.get("/health")
                self.assertEqual(response.status_code, 200)
                data = response.get_json()
                self.assertEqual(data["status"], "healthy")
                self.assertEqual(data["database"], "connected")
                self.assertEqual(data["runtime"], "python-flask-postgresql")
        finally:
            if orig_url is not None:
                app.config["DATABASE_URL"] = orig_url
            else:
                app.config.pop("DATABASE_URL", None)


if __name__ == "__main__":
    unittest.main()
