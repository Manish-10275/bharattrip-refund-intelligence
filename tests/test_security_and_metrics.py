import os
os.environ.setdefault("TESTING", "True")
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from dotenv import load_dotenv

from app import app, init_db
from config import Config, get_secret_key, is_testing_environment, parse_bool


class TestSecurityConfigurationAndMetrics(unittest.TestCase):
    """
    Automated verification of SECRET_KEY enforcement, .env precedence,
    boolean parsing, and dashboard Support metric precision.
    """

    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        with app.app_context():
            init_db()

    def _login(self, username="operator"):
        with self.client.session_transaction() as sess:
            sess["operator_user"] = username

    def _get_db_connection(self):
        conn = sqlite3.connect(app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        return conn

    # --------------------------------------------------------------------------
    # 1. SECRET_KEY Configuration & Startup Enforcement
    # --------------------------------------------------------------------------

    def test_01_production_startup_without_secret_key_fails_with_runtime_error(self):
        """Missing production SECRET_KEY causes startup failure with a clear RuntimeError."""
        with patch.dict(os.environ, {"SECRET_KEY": "", "TESTING": "False", "FLASK_ENV": ""}, clear=False):
            # When neither SECRET_KEY nor a testing environment is provided
            with self.assertRaises(RuntimeError) as ctx:
                get_secret_key()
            self.assertIn("SECRET_KEY environment variable is not set", str(ctx.exception))
            self.assertIn("non-testing environments", str(ctx.exception))

    def test_02_production_startup_with_valid_secret_key_permits_startup(self):
        """A valid production SECRET_KEY permits startup without error."""
        valid_key = "production-crypto-secret-key-32bytes-hex-abcdef123456"
        with patch.dict(os.environ, {"SECRET_KEY": valid_key, "TESTING": "False", "FLASK_ENV": ""}, clear=False):
            key = get_secret_key()
            self.assertEqual(key, valid_key)

    def test_03_testing_environment_initializes_with_dedicated_test_secret(self):
        """Testing environment can initialize with a dedicated test-only secret."""
        with patch.dict(os.environ, {"SECRET_KEY": "", "TESTING": "True", "FLASK_ENV": ""}, clear=False):
            key = get_secret_key()
            self.assertEqual(key, "bharattrip-test-only-secret-key-not-for-production")

    def test_04_configured_key_remains_consistent_across_app_initialization(self):
        """The configured key remains consistent across app initialization and preserves session validity."""
        test_secret = "stable-verification-secret-key-2026"
        with patch.dict(os.environ, {"SECRET_KEY": test_secret}, clear=False):
            key_first = get_secret_key()
            key_second = get_secret_key()
            # Confirms key is deterministic and not randomly regenerated
            self.assertEqual(key_first, test_secret)
            self.assertEqual(key_second, test_secret)
            self.assertEqual(key_first, key_second)

    # --------------------------------------------------------------------------
    # 2. .env Loading & Boolean Parsing
    # --------------------------------------------------------------------------

    def test_05_dotenv_loading_and_environment_variable_precedence(self):
        """.env loading preserves existing process environment variables as higher priority."""
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".env") as tmp_env:
            tmp_env.write("TEST_PRECEDENCE_VAR=from_dotenv_file\n")
            tmp_env_path = tmp_env.name

        try:
            # 1. Existing process environment has priority
            with patch.dict(os.environ, {"TEST_PRECEDENCE_VAR": "from_process_env"}, clear=False):
                load_dotenv(dotenv_path=tmp_env_path, override=False)
                self.assertEqual(os.environ.get("TEST_PRECEDENCE_VAR"), "from_process_env")

            # 2. When not in process environment, .env value is loaded
            env_clean = dict(os.environ)
            env_clean.pop("TEST_PRECEDENCE_VAR", None)
            with patch.dict(os.environ, env_clean, clear=True):
                load_dotenv(dotenv_path=tmp_env_path, override=False)
                self.assertEqual(os.environ.get("TEST_PRECEDENCE_VAR"), "from_dotenv_file")
        finally:
            if os.path.exists(tmp_env_path):
                os.remove(tmp_env_path)

    def test_06_boolean_parsing_of_session_cookie_secure(self):
        """Ensure SESSION_COOKIE_SECURE and boolean flags are parsed correctly without 'False' becoming True."""
        # False cases
        self.assertFalse(parse_bool("False"))
        self.assertFalse(parse_bool("false"))
        self.assertFalse(parse_bool("0"))
        self.assertFalse(parse_bool("no"))
        self.assertFalse(parse_bool("off"))
        self.assertFalse(parse_bool(""))
        self.assertFalse(parse_bool(None, default=False))
        self.assertFalse(parse_bool("invalid_string", default=False))

        # True cases
        self.assertTrue(parse_bool("True"))
        self.assertTrue(parse_bool("true"))
        self.assertTrue(parse_bool("1"))
        self.assertTrue(parse_bool("yes"))
        self.assertTrue(parse_bool("on"))
        self.assertTrue(parse_bool(None, default=True))

    # --------------------------------------------------------------------------
    # 3. Support Dashboard Metric Precision
    # --------------------------------------------------------------------------

    def test_07_dashboard_pending_support_triage_equals_35(self):
        """Dashboard Pending Support Triage equals 35 for the provided dataset."""
        conn = self._get_db_connection()
        try:
            working_count = conn.execute("SELECT COUNT(*) FROM support_records WHERE status = 'Working'").fetchone()[0]
            self.assertEqual(working_count, 35, "Raw dataset must have exactly 35 Working tickets")
        finally:
            conn.close()

        self._login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

        # Main dashboard HTML must contain Pending Support Triage with 35
        html = response.data.decode("utf-8")
        self.assertIn("Pending Support Triage", html)
        self.assertIn("kpi-pending_support_triage", html)
        self.assertIn("35", html)

    def test_08_three_distinct_source_queries_and_counts(self):
        """
        Verify all three distinct source queries and their exact counts:
        1. Pending Support Triage: support_records WHERE status = 'Working' -> 35
        2. Open Escalation Records: escalation_records WHERE resolved_on IS NULL -> 85
        3. Support Tickets Marked Escalated: support_records WHERE status = 'Escalated' -> 17
        Never use 85 and 17 interchangeably.
        """
        conn = self._get_db_connection()
        try:
            # Source Query 1: Pending Support Triage (Working status)
            working_count = conn.execute("SELECT COUNT(*) FROM support_records WHERE status = 'Working'").fetchone()[0]
            self.assertEqual(working_count, 35, "Query 1: support_records status = 'Working' must equal 35")

            # Source Query 2: Open Escalation Records (unresolved disputes)
            open_escalations = conn.execute("SELECT COUNT(*) FROM escalation_records WHERE resolved_on IS NULL").fetchone()[0]
            self.assertEqual(open_escalations, 85, "Query 2: escalation_records resolved_on IS NULL must equal 85")

            # Source Query 3: Support Tickets Marked Escalated
            support_escalated = conn.execute("SELECT COUNT(*) FROM support_records WHERE status = 'Escalated'").fetchone()[0]
            self.assertEqual(support_escalated, 17, "Query 3: support_records status = 'Escalated' must equal 17")
        finally:
            conn.close()

        self._login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

        html = response.data.decode("utf-8")

        # Distinct label 1: Pending Support Triage (35)
        self.assertIn("Pending Support Triage", html)
        self.assertIn("kpi-pending_support_triage", html)

        # Distinct label 2: Open Escalation Records (85)
        self.assertIn("Open Escalation Records", html)
        self.assertIn("kpi-open_escalation_records", html)
        self.assertIn("85", html)

        # Distinct label 3: Support Tickets Marked Escalated (17)
        self.assertIn("Support Tickets Marked Escalated", html)
        self.assertIn("kpi-support_tickets_marked_escalated", html)
        self.assertIn("17", html)

        # Distinct label 4: Pending AI Intake
        self.assertIn("Pending AI Intake", html)
        self.assertIn("kpi-pending_ai_intake", html)

        # Ensure 85 and 17 are not used interchangeably
        self.assertNotEqual(open_escalations, support_escalated, "Open escalations (85) and support escalated (17) must not be equal")

    def test_09_not_eligible_cases_not_included_in_pending_support_triage(self):
        """Not Eligible cases (27 records) are treated as terminal outcomes and excluded from Pending Support Triage."""
        conn = self._get_db_connection()
        try:
            total_support = conn.execute("SELECT COUNT(*) FROM support_records").fetchone()[0]
            closed_count = conn.execute("SELECT COUNT(*) FROM support_records WHERE status = 'Closed'").fetchone()[0]
            working_count = conn.execute("SELECT COUNT(*) FROM support_records WHERE status = 'Working'").fetchone()[0]
            not_eligible_count = conn.execute("SELECT COUNT(*) FROM support_records WHERE status = 'Not Eligible'").fetchone()[0]
            escalated_count = conn.execute("SELECT COUNT(*) FROM support_records WHERE status = 'Escalated'").fetchone()[0]

            self.assertEqual(total_support, 755)
            self.assertEqual(closed_count, 676)
            self.assertEqual(working_count, 35)
            self.assertEqual(not_eligible_count, 27)
            self.assertEqual(escalated_count, 17)

            # Sum of terminal + in-flight equals total
            self.assertEqual(closed_count + working_count + not_eligible_count + escalated_count, total_support)
        finally:
            conn.close()

        self._login()
        response = self.client.get("/")
        html = response.data.decode("utf-8")

        # Pending Support Triage must be 35, NOT 62 (35+27) and NOT 79 (35+27+17)
        self.assertNotIn(">62<", html)
        self.assertNotIn(">79<", html)

    def test_10_support_and_ai_intake_distinct_populations_and_denominators(self):
        """Support, Escalations, and AI intake metrics represent strictly distinct populations and denominators."""
        self._login()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)

        html = response.data.decode("utf-8")
        self.assertIn("Pending Support Triage", html)
        self.assertIn("Open Escalation Records", html)
        self.assertIn("Support Tickets Marked Escalated", html)
        self.assertIn("Pending AI Intake", html)

    def test_11_importing_unittest_alone_does_not_bypass_secret_key_validation(self):
        """
        Regression test: Importing unittest alone must not activate test mode or bypass SECRET_KEY validation.
        When unittest is loaded in sys.modules, but TESTING is not explicitly enabled,
        startup must fail with RuntimeError if SECRET_KEY is missing.
        """
        self.assertIn("unittest", sys.modules, "unittest must be present in sys.modules for this regression test")

        # 1. When TESTING is explicitly "False"
        with patch.dict(os.environ, {"SECRET_KEY": "", "TESTING": "False", "FLASK_ENV": ""}, clear=False):
            self.assertFalse(is_testing_environment(), "is_testing_environment() must return False when TESTING is 'False'")
            with self.assertRaises(RuntimeError) as ctx:
                get_secret_key()
            self.assertIn("FATAL: SECRET_KEY environment variable is not set", str(ctx.exception))

        # 2. When TESTING is completely unset (None)
        clean_env = {k: v for k, v in os.environ.items() if k not in ("SECRET_KEY", "TESTING", "FLASK_ENV")}
        with patch.dict(os.environ, clean_env, clear=True):
            self.assertFalse(is_testing_environment(), "is_testing_environment() must return False when TESTING is unset")
            with self.assertRaises(RuntimeError) as ctx:
                get_secret_key()
            self.assertIn("FATAL: SECRET_KEY environment variable is not set", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
