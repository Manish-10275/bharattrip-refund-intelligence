import os
os.environ.setdefault("TESTING", "True")
import sqlite3
import unittest
import openpyxl
from services.data_service import DataService, normalize_reference_id

WORKBOOK_PATH = "BharatTrip_Refund_Data.xlsx"
TEST_DB_PATH = "test_bharattrip.db"


class TestDataService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        cls.service = DataService(db_path=TEST_DB_PATH)
        cls.service.init_schema()

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except PermissionError:
                pass

    def test_01_workbook_loading(self):
        """Verify workbook exists and can be loaded via openpyxl."""
        self.assertTrue(os.path.exists(WORKBOOK_PATH), f"Workbook missing at {WORKBOOK_PATH}")
        wb = openpyxl.load_workbook(WORKBOOK_PATH, read_only=True)
        self.assertIsNotNone(wb)
        wb.close()

    def test_02_worksheet_availability(self):
        """Verify that all required worksheets are present in the workbook."""
        wb = openpyxl.load_workbook(WORKBOOK_PATH, read_only=True)
        sheet_names = wb.sheetnames
        wb.close()

        expected_sheets = ["Read_Me", "Support_Tracker", "Finance_Tracker", "Escalations"]
        for sheet in expected_sheets:
            self.assertIn(sheet, sheet_names, f"Worksheet {sheet} is missing")

    def test_03_reference_normalization_logic(self):
        """Verify normalize_reference_id handles standard, non-standard, and placeholder values."""
        # Standard
        self.assertEqual(normalize_reference_id("RF-1286"), "RF-1286")
        # Lowercase & spaces & formats
        self.assertEqual(normalize_reference_id("rf-1650"), "RF-1650")
        self.assertEqual(normalize_reference_id("RF 1750"), "RF-1750")
        self.assertEqual(normalize_reference_id("RF1109"), "RF-1109")
        self.assertEqual(normalize_reference_id("RF-1591 "), "RF-1591")
        self.assertEqual(normalize_reference_id("RF-1313 (revised)"), "RF-1313")
        # Placeholders / missing
        self.assertIsNone(normalize_reference_id("no ref"))
        self.assertIsNone(normalize_reference_id("-"))
        self.assertIsNone(normalize_reference_id("not known"))
        self.assertIsNone(normalize_reference_id(None))
        self.assertIsNone(normalize_reference_id(float("nan")))

    def test_04_database_insertion_and_counts(self):
        """Verify ingestion into SQLite and row count consistency with Excel source."""
        result = self.service.import_workbook(WORKBOOK_PATH)

        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["support_rows"], 755)
        self.assertEqual(result["finance_rows"], 689)
        self.assertEqual(result["escalation_rows"], 155)

        # Check database counts directly
        summary = self.service.get_summary()
        self.assertEqual(summary["support_records"], 755)
        self.assertEqual(summary["finance_records"], 689)
        self.assertEqual(summary["escalation_records"], 155)
        self.assertEqual(summary["completed_imports"], 1)

    def test_05_repeated_import_behaviour(self):
        """Verify that attempting to import the same workbook twice skips re-insertion (idempotency)."""
        repeat_result = self.service.import_workbook(WORKBOOK_PATH)
        self.assertEqual(repeat_result["status"], "SKIPPED")
        self.assertIn("already imported", repeat_result["message"])

        # Counts must remain identical (no duplicate rows inserted)
        summary = self.service.get_summary()
        self.assertEqual(summary["support_records"], 755)
        self.assertEqual(summary["finance_records"], 689)
        self.assertEqual(summary["escalation_records"], 155)
        self.assertEqual(summary["completed_imports"], 1)

    def test_06_missing_value_handling(self):
        """Verify that missing values (NaNs) are converted to SQL NULL, not strings 'nan' or 'None'."""
        conn = sqlite3.connect(TEST_DB_PATH)
        cursor = conn.cursor()

        # In support_records, 16 rows have missing refund_amount
        cursor.execute("SELECT COUNT(*) FROM support_records WHERE refund_amount IS NULL")
        null_support_amount = cursor.fetchone()[0]
        self.assertEqual(null_support_amount, 16)

        # In finance_records, 58 rows have missing amount_paid and processed_on
        cursor.execute("SELECT COUNT(*) FROM finance_records WHERE amount_paid IS NULL")
        null_fin_amount = cursor.fetchone()[0]
        self.assertEqual(null_fin_amount, 58)

        cursor.execute("SELECT COUNT(*) FROM finance_records WHERE processed_on IS NULL")
        null_fin_processed = cursor.fetchone()[0]
        self.assertEqual(null_fin_processed, 58)

        # In escalation_records, 6 rows have completely null related_ticket_ref
        cursor.execute("SELECT COUNT(*) FROM escalation_records WHERE related_ticket_ref IS NULL")
        null_esc_ref = cursor.fetchone()[0]
        self.assertEqual(null_esc_ref, 6)

        # In escalation_records, normalized ref is NULL for 19 cases (6 blank + 13 placeholders)
        cursor.execute("SELECT COUNT(*) FROM escalation_records WHERE norm_related_ref IS NULL")
        null_norm_esc_ref = cursor.fetchone()[0]
        self.assertEqual(null_norm_esc_ref, 19)

        # Verify no literal string 'nan' was inserted
        cursor.execute("SELECT COUNT(*) FROM support_records WHERE handled_by = 'nan' OR channel = 'nan'")
        self.assertEqual(cursor.fetchone()[0], 0)

        conn.close()

    def test_07_preservation_of_original_values(self):
        """Verify original reference strings are preserved verbatim while normalized columns are populated."""
        conn = sqlite3.connect(TEST_DB_PATH)
        cursor = conn.cursor()

        # Find a non-standard ticket in support (e.g. 'RF 1750' or 'rf-1650')
        cursor.execute("SELECT ticket_id, norm_ticket_id FROM support_records WHERE ticket_id != norm_ticket_id LIMIT 1")
        row = cursor.fetchone()
        self.assertIsNotNone(row)
        orig_id, norm_id = row[0], row[1]
        self.assertNotEqual(orig_id, norm_id)
        self.assertTrue(norm_id.startswith("RF-"))

        # Find revised finance reference (e.g. 'RF-1313 (revised)')
        cursor.execute("SELECT ref_no, norm_ref_no FROM finance_records WHERE ref_no LIKE '%revised%' LIMIT 1")
        f_row = cursor.fetchone()
        self.assertIsNotNone(f_row)
        self.assertIn("(revised)", f_row[0])
        self.assertTrue(f_row[1].startswith("RF-"))
        self.assertNotIn("(revised)", f_row[1])

        conn.close()


if __name__ == "__main__":
    unittest.main()
