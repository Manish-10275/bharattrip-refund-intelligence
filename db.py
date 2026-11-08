"""
Central Database Layer for BharatTrip Refund Intelligence.
Supports Neon / PostgreSQL (via psycopg 3) as production database,
and SQLite as local development and test database backend.

Provides:
- Transparent query parameter placeholder translation (? -> %s for PostgreSQL).
- Dual-access RowWrapper supporting row["col"] (case-insensitive) and row[0].
- Dialect-aware schema generation (PostgreSQL identity columns vs SQLite autoincrement).
- Flask request lifecycle connection management (get_db, close_db).
- Transaction context management with automatic commit and rollback.
"""

import logging
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import psycopg
    from psycopg import errors as pg_errors
    from psycopg.rows import RowFactory
    PSYCOPG_AVAILABLE = True
except ImportError:
    PSYCOPG_AVAILABLE = False
    pg_errors = None

from flask import g, has_app_context, current_app
from config import Config, is_testing_environment, normalize_database_url

logger = logging.getLogger(__name__)

# Unified OperationalError tuple for safe exception handling across drivers
if PSYCOPG_AVAILABLE:
    DatabaseOperationalError = (sqlite3.OperationalError, psycopg.OperationalError, psycopg.Error)
else:
    DatabaseOperationalError = (sqlite3.OperationalError,)


def translate_query_for_postgres(sql_query: str) -> str:
    """
    Translate an SQLite-style query with '?' parameter placeholders
    into a PostgreSQL-compatible query with '%s' placeholders.
    Preserves literal question marks inside single-quoted string literals.
    """
    pattern = re.compile(r"('(?:''|[^'])*')|(\?)")

    def replacer(match):
        if match.group(1):
            return match.group(1)
        return "%s"

    return pattern.sub(replacer, sql_query)


class RowWrapper:
    """
    Dual-access row wrapper compatible with both sqlite3.Row and dict_row.
    Supports:
      - row['column_name'] (case-insensitive key lookup)
      - row[0] (positional integer index access)
      - dict(row) (full dictionary serialization)
      - row.get('column_name', default)
      - 'column_name' in row
    """
    __slots__ = ("_values", "_mapping", "_keys")

    def __init__(self, values: Tuple[Any, ...], mapping: Dict[str, int], keys: List[str]):
        self._values = values
        self._mapping = mapping
        self._keys = keys

    def __getitem__(self, item: Union[int, str, slice]) -> Any:
        if isinstance(item, (int, slice)):
            return self._values[item]
        if isinstance(item, str):
            idx = self._mapping.get(item.lower())
            if idx is not None:
                return self._values[idx]
            raise KeyError(item)
        raise TypeError(f"Row indices must be integers, slices, or strings, not {type(item).__name__}")

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(key, str):
            idx = self._mapping.get(key.lower())
            if idx is not None:
                return self._values[idx]
        return default

    def keys(self) -> List[str]:
        return list(self._keys)

    def values(self) -> List[Any]:
        return list(self._values)

    def items(self) -> List[Tuple[str, Any]]:
        return [(self._keys[idx], self._values[idx]) for idx in range(len(self._keys))]

    def __iter__(self):
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, key: Any) -> bool:
        return isinstance(key, str) and key.lower() in self._mapping

    def __repr__(self) -> str:
        return f"<Row {dict(self.items())}>"


def psycopg_row_wrapper_factory(cursor) -> Any:
    """Row factory for psycopg 3 returning RowWrapper instances."""
    desc = cursor.description
    if not desc:
        return lambda values: values
    keys = [col.name for col in desc]
    mapping = {col.name.lower(): idx for idx, col in enumerate(desc)}
    return lambda values: RowWrapper(tuple(values), mapping, keys)


class DatabaseCursor:
    """Cursor wrapper providing unified parameter handling and fetch operations."""

    def __init__(self, raw_cursor: Any, dialect: str):
        self._cur = raw_cursor
        self.dialect = dialect

    def execute(self, query: str, params: Any = None) -> "DatabaseCursor":
        if self.dialect == "postgresql":
            translated_query = translate_query_for_postgres(query)
            if params is not None:
                self._cur.execute(translated_query, params)
            else:
                self._cur.execute(translated_query)
        else:
            if params is not None:
                self._cur.execute(query, params)
            else:
                self._cur.execute(query)
        return self

    def executemany(self, query: str, params_seq: Any) -> "DatabaseCursor":
        if self.dialect == "postgresql":
            translated_query = translate_query_for_postgres(query)
            self._cur.executemany(translated_query, params_seq)
        else:
            self._cur.executemany(query, params_seq)
        return self

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> List[Any]:
        return self._cur.fetchall()

    def fetchmany(self, size: int = 1) -> List[Any]:
        return self._cur.fetchmany(size)

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self) -> Optional[int]:
        return getattr(self._cur, "lastrowid", None)

    @property
    def description(self) -> Any:
        return self._cur.description

    def close(self) -> None:
        self._cur.close()

    def __iter__(self):
        return iter(self._cur)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class DatabaseConnection:
    """Connection wrapper ensuring transaction safety and dialect abstraction."""

    def __init__(self, raw_conn: Any, dialect: str):
        self._conn = raw_conn
        self.dialect = dialect
        self._tx = None

    def cursor(self) -> DatabaseCursor:
        if self.dialect == "postgresql":
            cur = self._conn.cursor(row_factory=psycopg_row_wrapper_factory)
        else:
            cur = self._conn.cursor()
        return DatabaseCursor(cur, self.dialect)

    def execute(self, query: str, params: Any = None) -> DatabaseCursor:
        cur = self.cursor()
        cur.execute(query, params)
        return cur

    def executemany(self, query: str, params_seq: Any) -> DatabaseCursor:
        cur = self.cursor()
        cur.executemany(query, params_seq)
        return cur

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    @property
    def raw_connection(self) -> Any:
        return self._conn

    @property
    def row_factory(self) -> Any:
        return getattr(self._conn, "row_factory", None)

    @row_factory.setter
    def row_factory(self, val: Any) -> None:
        if hasattr(self._conn, "row_factory"):
            self._conn.row_factory = val

    def __enter__(self) -> "DatabaseConnection":
        if self.dialect == "postgresql":
            self._tx = self._conn.transaction()
            self._tx.__enter__()
        else:
            self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.dialect == "postgresql":
            res = self._tx.__exit__(exc_type, exc_val, exc_tb)
            self._tx = None
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
            return res
        else:
            return self._conn.__exit__(exc_type, exc_val, exc_tb)


def get_database_backend(custom_url: Optional[str] = None) -> str:
    """
    Detect whether PostgreSQL or SQLite is the active backend.
    Returns 'postgresql' if a PostgreSQL DATABASE_URL is configured, else 'sqlite'.
    """
    url = custom_url
    if not url and has_app_context():
        url = current_app.config.get("DATABASE_URL")
    if not url:
        url = getattr(Config, "DATABASE_URL", None) or os.environ.get("DATABASE_URL", "")
    url = (url or "").strip().lower()
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        return "postgresql"
    return "sqlite"


def get_connection(
    db_path: Optional[str] = None,
    database_url: Optional[str] = None
) -> DatabaseConnection:
    """
    Create a standalone DatabaseConnection instance.
    - If database_url (or active app config DATABASE_URL) is PostgreSQL, connects using psycopg 3.
    - Otherwise connects to SQLite at db_path (or Config.DATABASE).
    """
    if db_path and (str(db_path).startswith("postgresql://") or str(db_path).startswith("postgres://")):
        if database_url is None:
            database_url = db_path
        db_path = None

    if has_app_context():
        if database_url is None:
            database_url = current_app.config.get("DATABASE_URL")
        if db_path is None:
            db_path = current_app.config.get("DATABASE")

    raw_url = database_url
    if raw_url is None and not has_app_context():
        # Outside app context, only fall back to environment/Config DATABASE_URL
        # if the caller did not specify an explicit custom SQLite path
        if db_path is None:
            raw_url = Config.load_database_url() if hasattr(Config, "load_database_url") else os.environ.get("DATABASE_URL", "")

    url = normalize_database_url(raw_url)

    if url and (url.startswith("postgresql://") or url.startswith("postgres://")):
        if not PSYCOPG_AVAILABLE:
            raise RuntimeError(
                "psycopg is not installed. Run 'pip install psycopg[binary]' to connect to PostgreSQL."
            )
        try:
            raw_conn = psycopg.connect(url, connect_timeout=15, autocommit=False)
            return DatabaseConnection(raw_conn, "postgresql")
        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL database: {e}")
            raise

    # Fallback to SQLite (local development and unit testing)
    target_path = db_path or (Config.DATABASE if hasattr(Config, "DATABASE") else os.environ.get("DATABASE", "bharattrip.db"))
    dir_name = os.path.dirname(os.path.abspath(target_path))
    if dir_name and not os.path.exists(dir_name):
        os.makedirs(dir_name, exist_ok=True)

    raw_conn = sqlite3.connect(target_path)
    raw_conn.row_factory = sqlite3.Row
    return DatabaseConnection(raw_conn, "sqlite")


def get_db() -> DatabaseConnection:
    """
    Retrieve or create the request-scoped DatabaseConnection.
    When inside Flask application context, reuses g.db and guarantees closure on teardown.
    When outside application context, returns a dedicated connection.
    """
    if has_app_context():
        if "db" not in g:
            g.db = get_connection()
        return g.db
    return get_connection()


def close_db(error: Optional[BaseException] = None) -> None:
    """Flask teardown handler to close the request-scoped database connection."""
    if has_app_context():
        db = g.pop("db", None)
        if db is not None:
            db.close()


def init_all_schemas(
    conn: Optional[DatabaseConnection] = None,
    db_path: Optional[str] = None
) -> None:
    """
    Initialize all database tables and indexes for the active dialect
    (PostgreSQL identity columns vs SQLite autoincrement).
    """
    close_needed = False
    if conn is None:
        if db_path is not None:
            conn = get_connection(db_path=db_path)
            close_needed = True
        else:
            conn = get_db() if has_app_context() else get_connection()
            close_needed = not has_app_context()

    try:
        with conn:
            if conn.dialect == "postgresql":
                _init_postgresql_schemas(conn)
            else:
                _init_sqlite_schemas(conn)
    finally:
        if close_needed:
            conn.close()


def _init_postgresql_schemas(conn: DatabaseConnection) -> None:
    """Create PostgreSQL tables and indexes using PostgreSQL identity columns."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS refund_records (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            booking_reference TEXT NOT NULL,
            customer_name TEXT NOT NULL,
            travel_agency TEXT,
            route TEXT,
            refund_amount DOUBLE PRECISION,
            request_date TEXT,
            status TEXT DEFAULT 'PENDING',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS import_history (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_hash TEXT NOT NULL UNIQUE,
            support_rows INTEGER DEFAULT 0,
            finance_rows INTEGER DEFAULT 0,
            escalation_rows INTEGER DEFAULT 0,
            status TEXT DEFAULT 'COMPLETED',
            imported_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS support_records (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            import_id INTEGER NOT NULL REFERENCES import_history(id) ON DELETE CASCADE,
            row_index INTEGER NOT NULL,
            ticket_id TEXT,
            norm_ticket_id TEXT,
            agent TEXT,
            route TEXT,
            refund_amount DOUBLE PRECISION,
            request_date TEXT,
            last_updated TEXT,
            status TEXT,
            handled_by TEXT,
            channel TEXT,
            notes TEXT,
            source_sheet TEXT DEFAULT 'Support_Tracker',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (import_id, row_index)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS finance_records (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            import_id INTEGER NOT NULL REFERENCES import_history(id) ON DELETE CASCADE,
            row_index INTEGER NOT NULL,
            ref_no TEXT,
            norm_ref_no TEXT,
            agent_name TEXT,
            sector TEXT,
            amount_paid DOUBLE PRECISION,
            deduction DOUBLE PRECISION,
            received_on TEXT,
            processed_on TEXT,
            payout_status TEXT,
            approved_by TEXT,
            remarks TEXT,
            source_sheet TEXT DEFAULT 'Finance_Tracker',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (import_id, row_index)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS escalation_records (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            import_id INTEGER NOT NULL REFERENCES import_history(id) ON DELETE CASCADE,
            row_index INTEGER NOT NULL,
            escalation_id TEXT,
            norm_escalation_id TEXT,
            raised_on TEXT,
            related_ticket_ref TEXT,
            norm_related_ref TEXT,
            raised_by TEXT,
            agent_team TEXT,
            channel TEXT,
            complaint TEXT,
            status TEXT,
            resolved_on TEXT,
            days_open INTEGER,
            source_sheet TEXT DEFAULT 'Escalations',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (import_id, row_index)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reconciliation_runs (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            run_timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            total_candidates INTEGER DEFAULT 0,
            matched_count INTEGER DEFAULT 0,
            status_mismatch_count INTEGER DEFAULT 0,
            support_only_count INTEGER DEFAULT 0,
            finance_only_count INTEGER DEFAULT 0,
            duplicate_reference_count INTEGER DEFAULT 0,
            ambiguous_match_count INTEGER DEFAULT 0,
            invalid_reference_count INTEGER DEFAULT 0,
            review_required_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'COMPLETED',
            notes TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reconciliation_results (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            run_id INTEGER NOT NULL REFERENCES reconciliation_runs(id) ON DELETE CASCADE,
            support_record_id INTEGER REFERENCES support_records(id) ON DELETE SET NULL,
            finance_record_id INTEGER REFERENCES finance_records(id) ON DELETE SET NULL,
            original_support_ref TEXT,
            original_finance_ref TEXT,
            norm_reference TEXT,
            classification TEXT NOT NULL,
            reason TEXT NOT NULL,
            review_required BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS intake_records (
            id INTEGER PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY,
            original_message TEXT NOT NULL,
            source_channel TEXT NOT NULL,
            customer_name TEXT,
            agency_name TEXT,
            booking_reference TEXT,
            refund_reference TEXT,
            route TEXT,
            refund_amount DOUBLE PRECISION,
            currency TEXT,
            request_date TEXT,
            complaint_category TEXT NOT NULL,
            missing_information TEXT NOT NULL,
            summary TEXT NOT NULL,
            confidence DOUBLE PRECISION NOT NULL,
            human_review_required INTEGER NOT NULL DEFAULT 0,
            human_review_reason TEXT,
            processing_status TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
            model_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            extracted_json TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP WITH TIME ZONE,
            reviewed_by TEXT
        )
    """)

    # Indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_support_norm_ticket ON support_records(norm_ticket_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_finance_norm_ref ON finance_records(norm_ref_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_escalation_norm_ref ON escalation_records(norm_related_ref)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_run_id ON reconciliation_results(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_classification ON reconciliation_results(classification)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_review_required ON reconciliation_results(review_required)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_norm_ref ON reconciliation_results(norm_reference)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intake_created_at ON intake_records(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intake_review ON intake_records(human_review_required, processing_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intake_complaint ON intake_records(complaint_category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intake_refund_ref ON intake_records(refund_reference)")


def _init_sqlite_schemas(conn: DatabaseConnection) -> None:
    """Create SQLite tables and indexes using SQLite AUTOINCREMENT."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS refund_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_reference TEXT NOT NULL,
            customer_name TEXT NOT NULL,
            travel_agency TEXT,
            route TEXT,
            refund_amount REAL,
            request_date TEXT,
            status TEXT DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS import_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_hash TEXT NOT NULL UNIQUE,
            support_rows INTEGER DEFAULT 0,
            finance_rows INTEGER DEFAULT 0,
            escalation_rows INTEGER DEFAULT 0,
            status TEXT DEFAULT 'COMPLETED',
            imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS support_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            ticket_id TEXT,
            norm_ticket_id TEXT,
            agent TEXT,
            route TEXT,
            refund_amount REAL,
            request_date TEXT,
            last_updated TEXT,
            status TEXT,
            handled_by TEXT,
            channel TEXT,
            notes TEXT,
            source_sheet TEXT DEFAULT 'Support_Tracker',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (import_id) REFERENCES import_history(id) ON DELETE CASCADE,
            UNIQUE (import_id, row_index)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS finance_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            ref_no TEXT,
            norm_ref_no TEXT,
            agent_name TEXT,
            sector TEXT,
            amount_paid REAL,
            deduction REAL,
            received_on TEXT,
            processed_on TEXT,
            payout_status TEXT,
            approved_by TEXT,
            remarks TEXT,
            source_sheet TEXT DEFAULT 'Finance_Tracker',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (import_id) REFERENCES import_history(id) ON DELETE CASCADE,
            UNIQUE (import_id, row_index)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS escalation_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            escalation_id TEXT,
            norm_escalation_id TEXT,
            raised_on TEXT,
            related_ticket_ref TEXT,
            norm_related_ref TEXT,
            raised_by TEXT,
            agent_team TEXT,
            channel TEXT,
            complaint TEXT,
            status TEXT,
            resolved_on TEXT,
            days_open INTEGER,
            source_sheet TEXT DEFAULT 'Escalations',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (import_id) REFERENCES import_history(id) ON DELETE CASCADE,
            UNIQUE (import_id, row_index)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reconciliation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_candidates INTEGER DEFAULT 0,
            matched_count INTEGER DEFAULT 0,
            status_mismatch_count INTEGER DEFAULT 0,
            support_only_count INTEGER DEFAULT 0,
            finance_only_count INTEGER DEFAULT 0,
            duplicate_reference_count INTEGER DEFAULT 0,
            ambiguous_match_count INTEGER DEFAULT 0,
            invalid_reference_count INTEGER DEFAULT 0,
            review_required_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'COMPLETED',
            notes TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reconciliation_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            support_record_id INTEGER,
            finance_record_id INTEGER,
            original_support_ref TEXT,
            original_finance_ref TEXT,
            norm_reference TEXT,
            classification TEXT NOT NULL,
            reason TEXT NOT NULL,
            review_required BOOLEAN NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES reconciliation_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (support_record_id) REFERENCES support_records(id) ON DELETE SET NULL,
            FOREIGN KEY (finance_record_id) REFERENCES finance_records(id) ON DELETE SET NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS intake_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_message TEXT NOT NULL,
            source_channel TEXT NOT NULL,
            customer_name TEXT,
            agency_name TEXT,
            booking_reference TEXT,
            refund_reference TEXT,
            route TEXT,
            refund_amount REAL,
            currency TEXT,
            request_date TEXT,
            complaint_category TEXT NOT NULL,
            missing_information TEXT NOT NULL,
            summary TEXT NOT NULL,
            confidence REAL NOT NULL,
            human_review_required INTEGER NOT NULL DEFAULT 0,
            human_review_reason TEXT,
            processing_status TEXT NOT NULL DEFAULT 'PENDING_REVIEW',
            model_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            extracted_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP,
            reviewed_by TEXT
        )
    """)

    # Indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_support_norm_ticket ON support_records(norm_ticket_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_finance_norm_ref ON finance_records(norm_ref_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_escalation_norm_ref ON escalation_records(norm_related_ref)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_run_id ON reconciliation_results(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_classification ON reconciliation_results(classification)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_review_required ON reconciliation_results(review_required)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_norm_ref ON reconciliation_results(norm_reference)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intake_created_at ON intake_records(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intake_review ON intake_records(human_review_required, processing_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intake_complaint ON intake_records(complaint_category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_intake_refund_ref ON intake_records(refund_reference)")
