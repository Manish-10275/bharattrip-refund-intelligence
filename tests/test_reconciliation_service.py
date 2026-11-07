import os
os.environ.setdefault("TESTING", "True")
import sqlite3
import unittest
from services.data_service import DataService
from services.reconciliation_service import ReconciliationService, ReconciliationClassification

TEST_DB = "test_reconciliation.db"


class TestReconciliationService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        cls.data_service = DataService(db_path=TEST_DB)
        cls.data_service.init_schema()
        cls.service = ReconciliationService(db_path=TEST_DB)
        cls.service.init_schema()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB):
            try:
                os.remove(TEST_DB)
            except PermissionError:
                pass

    def setUp(self):
        # Clear tables before each test
        conn = sqlite3.connect(TEST_DB)
        with conn:
            conn.execute("DELETE FROM reconciliation_results")
            conn.execute("DELETE FROM reconciliation_runs")
            conn.execute("DELETE FROM support_records")
            conn.execute("DELETE FROM finance_records")
            conn.execute("DELETE FROM import_history")
            conn.execute("INSERT INTO import_history (id, file_name, file_path, file_hash, status) VALUES (1, 'mock.xlsx', 'mock.xlsx', 'hash123', 'COMPLETED')")
        conn.close()

    def test_01_exact_reference_matches(self):
        """Verify exact reference match with consistent status and payout evidence yields MATCHED."""
        conn = sqlite3.connect(TEST_DB)
        with conn:
            conn.execute("""
                INSERT INTO support_records (id, import_id, row_index, ticket_id, norm_ticket_id, agent, route, refund_amount, status)
                VALUES (1, 1, 0, 'RF-1001', 'RF-1001', 'Test Agent', 'DEL-BOM', 5000.0, 'Closed')
            """)
            conn.execute("""
                INSERT INTO finance_records (id, import_id, row_index, ref_no, norm_ref_no, agent_name, sector, amount_paid, payout_status, processed_on)
                VALUES (1, 1, 0, 'RF-1001', 'RF-1001', 'Test Agent', 'DEL-BOM', 5000.0, 'Refund Done', '2026-03-01 00:00:00')
            """)
        conn.close()

        summary = self.service.run_reconciliation()
        self.assertEqual(summary["matched_count"], 1)
        self.assertEqual(summary["review_required_count"], 0)

        results, count = self.service.get_results(summary["run_id"])
        self.assertEqual(count, 1)
        self.assertEqual(results[0]["classification"], ReconciliationClassification.MATCHED)
        self.assertEqual(results[0]["norm_reference"], "RF-1001")
        self.assertFalse(results[0]["review_required"])

    def test_02_reference_normalization_variations(self):
        """Verify matching candidates with formatting variations (lowercase, spaces, prefixes)."""
        conn = sqlite3.connect(TEST_DB)
        with conn:
            conn.execute("""
                INSERT INTO support_records (id, import_id, row_index, ticket_id, norm_ticket_id, agent, route, refund_amount, status)
                VALUES (2, 1, 1, 'rf 2002', 'RF-2002', 'Skyline', 'BLR-GOI', 7500.0, 'Closed')
            """)
            conn.execute("""
                INSERT INTO finance_records (id, import_id, row_index, ref_no, norm_ref_no, agent_name, sector, amount_paid, payout_status, processed_on)
                VALUES (2, 1, 1, 'RF-2002 (revised)', 'RF-2002', 'Skyline', 'BLR-GOI', 7500.0, 'Refund Done', '2026-03-05 00:00:00')
            """)
        conn.close()

        summary = self.service.run_reconciliation()
        self.assertEqual(summary["matched_count"], 1)
        results, _ = self.service.get_results(summary["run_id"])
        self.assertEqual(results[0]["classification"], ReconciliationClassification.MATCHED)
        self.assertEqual(results[0]["original_support_ref"], "rf 2002")
        self.assertEqual(results[0]["original_finance_ref"], "RF-2002 (revised)")
        self.assertEqual(results[0]["norm_reference"], "RF-2002")

    def test_03_support_only_records(self):
        """Verify Support records with no Finance counterpart are classified as SUPPORT_ONLY."""
        conn = sqlite3.connect(TEST_DB)
        with conn:
            conn.execute("""
                INSERT INTO support_records (id, import_id, row_index, ticket_id, norm_ticket_id, agent, route, refund_amount, status)
                VALUES (3, 1, 2, 'RF-3003', 'RF-3003', 'Solo Agent', 'CCU-DEL', 4200.0, 'Closed')
            """)
        conn.close()

        summary = self.service.run_reconciliation()
        self.assertEqual(summary["support_only_count"], 1)
        self.assertEqual(summary["review_required_count"], 1)

        results, _ = self.service.get_results(summary["run_id"])
        self.assertEqual(results[0]["classification"], ReconciliationClassification.SUPPORT_ONLY)
        self.assertIsNone(results[0]["finance_paid"])
        self.assertTrue(results[0]["review_required"])

    def test_04_finance_only_records(self):
        """Verify Finance records with no Support counterpart are classified as FINANCE_ONLY."""
        conn = sqlite3.connect(TEST_DB)
        with conn:
            conn.execute("""
                INSERT INTO finance_records (id, import_id, row_index, ref_no, norm_ref_no, agent_name, sector, amount_paid, payout_status, processed_on)
                VALUES (4, 1, 3, 'RF-4004', 'RF-4004', 'Finance Dept', 'BOM-HYD', 6300.0, 'Refund Done', '2026-04-10 00:00:00')
            """)
        conn.close()

        summary = self.service.run_reconciliation()
        self.assertEqual(summary["finance_only_count"], 1)
        self.assertEqual(summary["review_required_count"], 1)

        results, _ = self.service.get_results(summary["run_id"])
        self.assertEqual(results[0]["classification"], ReconciliationClassification.FINANCE_ONLY)
        self.assertIsNone(results[0]["support_status"])
        self.assertTrue(results[0]["review_required"])

    def test_05_duplicate_references(self):
        """Verify multiple identical records on one side are flagged as DUPLICATE_REFERENCE."""
        conn = sqlite3.connect(TEST_DB)
        with conn:
            # Two identical duplicate rows in Support
            conn.execute("""
                INSERT INTO support_records (id, import_id, row_index, ticket_id, norm_ticket_id, agent, route, refund_amount, status)
                VALUES (5, 1, 4, 'RF-5005', 'RF-5005', 'Dup Agent', 'DEL-BOM', 3000.0, 'Closed')
            """)
            conn.execute("""
                INSERT INTO support_records (id, import_id, row_index, ticket_id, norm_ticket_id, agent, route, refund_amount, status)
                VALUES (6, 1, 5, 'RF-5005', 'RF-5005', 'Dup Agent', 'DEL-BOM', 3000.0, 'Closed')
            """)
        conn.close()

        summary = self.service.run_reconciliation()
        self.assertEqual(summary["duplicate_reference_count"], 2)
        self.assertEqual(summary["review_required_count"], 2)

        results, _ = self.service.get_results(summary["run_id"])
        for r in results:
            self.assertEqual(r["classification"], ReconciliationClassification.DUPLICATE_REFERENCE)

    def test_06_ambiguous_matches(self):
        """Verify contradictory flight routes or multiple conflicting candidates yield AMBIGUOUS_MATCH."""
        conn = sqlite3.connect(TEST_DB)
        with conn:
            # Case 1: Route contradiction (DEL-BOM vs BLR-DXB)
            conn.execute("""
                INSERT INTO support_records (id, import_id, row_index, ticket_id, norm_ticket_id, agent, route, refund_amount, status)
                VALUES (7, 1, 6, 'RF-6001', 'RF-6001', 'Agent A', 'DEL-BOM', 5000.0, 'Closed')
            """)
            conn.execute("""
                INSERT INTO finance_records (id, import_id, row_index, ref_no, norm_ref_no, agent_name, sector, amount_paid, payout_status, processed_on)
                VALUES (7, 1, 6, 'RF-6001', 'RF-6001', 'Agent A', 'BLR-DXB', 5000.0, 'Refund Done', '2026-03-01 00:00:00')
            """)

            # Case 2: Support has conflicting duplicate tickets (Working vs Closed)
            conn.execute("""
                INSERT INTO support_records (id, import_id, row_index, ticket_id, norm_ticket_id, agent, route, refund_amount, status)
                VALUES (8, 1, 7, 'RF-6002', 'RF-6002', 'Agent B', 'GOI-BOM', 8800.0, 'Working')
            """)
            conn.execute("""
                INSERT INTO support_records (id, import_id, row_index, ticket_id, norm_ticket_id, agent, route, refund_amount, status)
                VALUES (9, 1, 8, 'RF-6002', 'RF-6002', 'Agent B', 'GOI-BOM', 8300.0, 'Closed')
            """)
            conn.execute("""
                INSERT INTO finance_records (id, import_id, row_index, ref_no, norm_ref_no, agent_name, sector, amount_paid, payout_status, processed_on)
                VALUES (8, 1, 7, 'RF-6002', 'RF-6002', 'Agent B', 'GOI-BOM', 8800.0, 'Refund Done', '2026-03-02 00:00:00')
            """)
        conn.close()

        summary = self.service.run_reconciliation()
        self.assertGreaterEqual(summary["ambiguous_match_count"], 3)

        results, _ = self.service.get_results(summary["run_id"], classification=ReconciliationClassification.AMBIGUOUS_MATCH)
        self.assertGreaterEqual(len(results), 3)
        for r in results:
            self.assertTrue(r["review_required"])

    def test_07_status_mismatches(self):
        """Verify explicitly required status mismatches are identified and flagged for review."""
        test_cases = [
            # Support status, Finance payout_status, processed_on, amount_paid, expected_reason_contains
            ("Closed", "Pending Payout", None, None, "Pending Payout"),
            ("Closed", "Declined", "2026-03-01 00:00:00", 0.0, "Declined"),
            ("Working", "Pending Payout", None, None, "Working"),
            ("Escalated", "Pending Payout", None, None, "Escalated"),
            # Closed + Refund Done but missing payout evidence
            ("Closed", "Refund Done", None, None, "payout evidence is missing"),
        ]

        conn = sqlite3.connect(TEST_DB)
        with conn:
            for idx, (s_st, f_st, proc_on, amt, _) in enumerate(test_cases, start=100):
                ref = f"RF-{idx}"
                conn.execute("""
                    INSERT INTO support_records (id, import_id, row_index, ticket_id, norm_ticket_id, agent, route, refund_amount, status)
                    VALUES (?, 1, ?, ?, ?, 'Agent X', 'DEL-BOM', 10000.0, ?)
                """, (idx, idx, ref, ref, s_st))
                conn.execute("""
                    INSERT INTO finance_records (id, import_id, row_index, ref_no, norm_ref_no, agent_name, sector, amount_paid, payout_status, processed_on)
                    VALUES (?, 1, ?, ?, ?, 'Agent X', 'DEL-BOM', ?, ?, ?)
                """, (idx, idx, ref, ref, amt, f_st, proc_on))
        conn.close()

        summary = self.service.run_reconciliation()
        self.assertEqual(summary["status_mismatch_count"], len(test_cases))

        results, _ = self.service.get_results(summary["run_id"], classification=ReconciliationClassification.STATUS_MISMATCH)
        self.assertEqual(len(results), len(test_cases))
        for r in results:
            self.assertTrue(r["review_required"])

    def test_08_blank_and_placeholder_references(self):
        """Verify blank, null, or placeholder references ('no ref', '-') are classified as INVALID_REFERENCE."""
        conn = sqlite3.connect(TEST_DB)
        with conn:
            conn.execute("""
                INSERT INTO support_records (id, import_id, row_index, ticket_id, norm_ticket_id, agent, route, refund_amount, status)
                VALUES (201, 1, 201, 'no ref', NULL, 'Unknown', 'DEL-BOM', 2000.0, 'Closed')
            """)
            conn.execute("""
                INSERT INTO finance_records (id, import_id, row_index, ref_no, norm_ref_no, agent_name, sector, amount_paid, payout_status, processed_on)
                VALUES (202, 1, 202, '-', NULL, 'Unknown', 'DEL-BOM', 2000.0, 'Pending Payout', NULL)
            """)
        conn.close()

        summary = self.service.run_reconciliation()
        self.assertEqual(summary["invalid_reference_count"], 2)
        results, _ = self.service.get_results(summary["run_id"], classification=ReconciliationClassification.INVALID_REFERENCE)
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertTrue(r["review_required"])

    def test_09_repeated_reconciliation_runs(self):
        """Verify repeated runs create distinct historical runs in reconciliation_runs without collisions."""
        conn = sqlite3.connect(TEST_DB)
        with conn:
            conn.execute("""
                INSERT INTO support_records (id, import_id, row_index, ticket_id, norm_ticket_id, agent, route, refund_amount, status)
                VALUES (301, 1, 301, 'RF-3001', 'RF-3001', 'Agent Y', 'DEL-BOM', 5000.0, 'Closed')
            """)
            conn.execute("""
                INSERT INTO finance_records (id, import_id, row_index, ref_no, norm_ref_no, agent_name, sector, amount_paid, payout_status, processed_on)
                VALUES (301, 1, 301, 'RF-3001', 'RF-3001', 'Agent Y', 'DEL-BOM', 5000.0, 'Refund Done', '2026-03-01 00:00:00')
            """)
        conn.close()

        run1 = self.service.run_reconciliation(notes="First run")
        run2 = self.service.run_reconciliation(notes="Second run")

        self.assertNotEqual(run1["run_id"], run2["run_id"])
        self.assertEqual(run1["matched_count"], 1)
        self.assertEqual(run2["matched_count"], 1)

        # Check latest run method
        latest = self.service.get_latest_run()
        self.assertEqual(latest["id"], run2["run_id"])

    def test_10_preservation_of_original_source_data(self):
        """Verify reconciliation engine strictly preserves source records and never alters them."""
        conn = sqlite3.connect(TEST_DB)
        with conn:
            conn.execute("""
                INSERT INTO support_records (id, import_id, row_index, ticket_id, norm_ticket_id, agent, route, refund_amount, status)
                VALUES (401, 1, 401, 'RF 4001 ', 'RF-4001', 'Pristine Agent', 'DEL-BOM', 9999.0, 'Working')
            """)
            conn.execute("""
                INSERT INTO finance_records (id, import_id, row_index, ref_no, norm_ref_no, agent_name, sector, amount_paid, payout_status, processed_on)
                VALUES (401, 1, 401, 'RF-4001 (revised)', 'RF-4001', 'Pristine Agent', 'DEL-BOM', 9999.0, 'Pending Payout', NULL)
            """)
        conn.close()

        self.service.run_reconciliation()

        conn = sqlite3.connect(TEST_DB)
        c = conn.cursor()
        c.execute("SELECT ticket_id, agent, refund_amount, status FROM support_records WHERE id = 401")
        s = c.fetchone()
        self.assertEqual(s[0], "RF 4001 ")
        self.assertEqual(s[1], "Pristine Agent")
        self.assertEqual(s[2], 9999.0)
        self.assertEqual(s[3], "Working")

        c.execute("SELECT ref_no, agent_name, amount_paid, payout_status FROM finance_records WHERE id = 401")
        f = c.fetchone()
        self.assertEqual(f[0], "RF-4001 (revised)")
        self.assertEqual(f[1], "Pristine Agent")
        self.assertEqual(f[2], 9999.0)
        self.assertEqual(f[3], "Pending Payout")
        conn.close()


if __name__ == "__main__":
    unittest.main()
