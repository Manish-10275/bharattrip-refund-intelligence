import hashlib
import logging
import os
import re
import sqlite3
import warnings
from typing import Any, Dict, List, Optional
import openpyxl
import pandas as pd

logger = logging.getLogger(__name__)

REQUIRED_WORKSHEETS = ["Read_Me", "Support_Tracker", "Finance_Tracker", "Escalations"]


def compute_file_hash(file_path: str) -> str:
    """Compute SHA-256 hash of a file to detect duplicates and track provenance."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            sha256.update(chunk)
    return sha256.hexdigest()


def normalize_reference_id(raw_val: Any) -> Optional[str]:
    """
    Normalise reference ID for reliable cross-sheet matching without altering original value.
    Extracts canonical 'RF-<digits>' format.
    Safely handles empty cells, text variations, whitespace, and non-reference values
    such as 'no ref', '-', 'not known', etc.
    """
    if raw_val is None or pd.isna(raw_val):
        return None
    val_str = str(raw_val).strip()
    if val_str.lower() in {"no ref", "-", "not known", "none", "n/a", "null", "", "nan"}:
        return None
    match = re.search(r"RF\s*-?\s*(\d+)", val_str, re.IGNORECASE)
    if match:
        return f"RF-{match.group(1)}"
    return None


def safe_str(val: Any) -> Optional[str]:
    """Convert cell value to clean string safely or return None if empty/NA."""
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    return s if s else None


def safe_float(val: Any) -> Optional[float]:
    """Convert cell value to float safely or return None if empty/NA."""
    if val is None or pd.isna(val):
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def safe_int(val: Any) -> Optional[int]:
    """Convert cell value to int safely or return None if empty/NA."""
    if val is None or pd.isna(val):
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def safe_date_str(val: Any) -> Optional[str]:
    """Format date / timestamp or preserve string date safely without crashing."""
    if val is None or pd.isna(val):
        return None
    if isinstance(val, (pd.Timestamp, pd.DatetimeIndex)):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    s = str(val).strip()
    return s if s else None


class DataService:
    """
    Enterprise Data Ingestion and Validation Service for BharatTrip Refund Intelligence.
    Optimized for memory efficiency, data integrity, and reproducible imports.
    """

    def __init__(self, db_path: str = "bharattrip.db"):
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        """Create a connection to the SQLite database."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Create required tables for data ingestion if they do not exist."""
        close_needed = False
        if conn is None:
            conn = self.get_connection()
            close_needed = True

        try:
            with conn:
                # 1. Import history tracking table
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

                # 2. Support tracker table
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

                # 3. Finance tracker table
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

                # 4. Escalations table
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

                # Performance indexes for normalized references
                conn.execute("CREATE INDEX IF NOT EXISTS idx_support_norm_ticket ON support_records(norm_ticket_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_finance_norm_ref ON finance_records(norm_ref_no)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_escalation_norm_ref ON escalation_records(norm_related_ref)")
        finally:
            if close_needed:
                conn.close()

    def inspect_workbook(self, file_path: str) -> Dict[str, Any]:
        """
        Inspect workbook structure, verifying required worksheets, shapes, column names,
        missing values, and duplicate records.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Workbook not found at: {file_path}")

        wb = openpyxl.load_workbook(file_path, read_only=True)
        sheet_names = wb.sheetnames
        wb.close()

        missing_sheets = [s for s in REQUIRED_WORKSHEETS if s not in sheet_names]
        if missing_sheets:
            raise ValueError(f"Workbook is missing required worksheet(s): {missing_sheets}")

        inspection: Dict[str, Any] = {
            "file_name": os.path.basename(file_path),
            "file_path": file_path,
            "file_hash": compute_file_hash(file_path),
            "sheets": {},
        }

        for sheet in REQUIRED_WORKSHEETS:
            df = pd.read_excel(file_path, sheet_name=sheet, engine="openpyxl")
            row_count, col_count = df.shape

            missing_per_col = df.isnull().sum().to_dict()
            duplicate_rows_full = int(df.duplicated().sum())

            sheet_info: Dict[str, Any] = {
                "row_count": row_count,
                "column_count": col_count,
                "columns": list(df.columns),
                "missing_values": missing_per_col,
                "duplicate_full_rows": duplicate_rows_full,
            }

            if sheet == "Support_Tracker" and "Ticket ID" in df.columns:
                sheet_info["unique_ticket_ids"] = int(df["Ticket ID"].nunique())
                sheet_info["duplicate_ticket_ids"] = int(df.duplicated(subset=["Ticket ID"]).sum())
                sheet_info["missing_ticket_ids"] = int(df["Ticket ID"].isnull().sum())
            elif sheet == "Finance_Tracker" and "Ref No" in df.columns:
                sheet_info["unique_ref_nos"] = int(df["Ref No"].nunique())
                sheet_info["duplicate_ref_nos"] = int(df.duplicated(subset=["Ref No"]).sum())
                sheet_info["missing_ref_nos"] = int(df["Ref No"].isnull().sum())
            elif sheet == "Escalations" and "Escalation ID" in df.columns:
                sheet_info["unique_escalation_ids"] = int(df["Escalation ID"].nunique())
                sheet_info["missing_related_refs"] = int(df["Related Ticket / Ref"].isnull().sum())

            inspection["sheets"][sheet] = sheet_info

        return inspection

    def import_workbook(self, file_path: str, force: bool = False) -> Dict[str, Any]:
        """
        Ingest worksheets into SQLite.
        - Idempotent: checks file hash to avoid re-inserting duplicates.
        - Preserves original values while calculating normalized references.
        - Maintains import metadata.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Workbook file does not exist: {file_path}")

        self.init_schema()
        file_hash = compute_file_hash(file_path)
        file_name = os.path.basename(file_path)

        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, imported_at, support_rows, finance_rows, escalation_rows, status FROM import_history WHERE file_hash = ?",
                (file_hash,),
            )
            existing = cursor.fetchone()

            if existing and not force:
                logger.info(f"Workbook {file_name} was already imported on {existing['imported_at']}. Skipping re-insertion.")
                return {
                    "status": "SKIPPED",
                    "message": f"Workbook already imported on {existing['imported_at']}. Re-insertion skipped to prevent duplicates.",
                    "import_id": existing["id"],
                    "file_hash": file_hash,
                    "support_rows": existing["support_rows"],
                    "finance_rows": existing["finance_rows"],
                    "escalation_rows": existing["escalation_rows"],
                }

            if existing and force:
                old_id = existing["id"]
                with conn:
                    conn.execute("DELETE FROM support_records WHERE import_id = ?", (old_id,))
                    conn.execute("DELETE FROM finance_records WHERE import_id = ?", (old_id,))
                    conn.execute("DELETE FROM escalation_records WHERE import_id = ?", (old_id,))
                    conn.execute("DELETE FROM import_history WHERE id = ?", (old_id,))

            with conn:
                cursor.execute(
                    "INSERT INTO import_history (file_name, file_path, file_hash, status) VALUES (?, ?, ?, 'PROCESSING')",
                    (file_name, file_path, file_hash),
                )
                import_id = cursor.lastrowid

                # 1. Ingest Support_Tracker
                df_support = pd.read_excel(file_path, sheet_name="Support_Tracker", engine="openpyxl")
                support_rows = []
                for idx, row in df_support.iterrows():
                    raw_ticket = safe_str(row.get("Ticket ID"))
                    support_rows.append((
                        import_id,
                        int(idx),
                        raw_ticket,
                        normalize_reference_id(raw_ticket),
                        safe_str(row.get("Agent")),
                        safe_str(row.get("Route")),
                        safe_float(row.get("Refund Amount (INR)")),
                        safe_date_str(row.get("Request Date")),
                        safe_date_str(row.get("Last Updated")),
                        safe_str(row.get("Status")),
                        safe_str(row.get("Handled By")),
                        safe_str(row.get("Channel")),
                        safe_str(row.get("Notes")),
                        "Support_Tracker"
                    ))

                conn.executemany("""
                    INSERT INTO support_records (
                        import_id, row_index, ticket_id, norm_ticket_id, agent, route,
                        refund_amount, request_date, last_updated, status, handled_by, channel, notes, source_sheet
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, support_rows)
                del df_support

                # 2. Ingest Finance_Tracker
                df_finance = pd.read_excel(file_path, sheet_name="Finance_Tracker", engine="openpyxl")
                finance_rows = []
                for idx, row in df_finance.iterrows():
                    raw_ref = safe_str(row.get("Ref No"))
                    finance_rows.append((
                        import_id,
                        int(idx),
                        raw_ref,
                        normalize_reference_id(raw_ref),
                        safe_str(row.get("Agent Name")),
                        safe_str(row.get("Sector")),
                        safe_float(row.get("Amount Paid (INR)")),
                        safe_float(row.get("Deduction (INR)")),
                        safe_date_str(row.get("Received On")),
                        safe_date_str(row.get("Processed On")),
                        safe_str(row.get("Payout Status")),
                        safe_str(row.get("Approved By")),
                        safe_str(row.get("Remarks")),
                        "Finance_Tracker"
                    ))

                conn.executemany("""
                    INSERT INTO finance_records (
                        import_id, row_index, ref_no, norm_ref_no, agent_name, sector,
                        amount_paid, deduction, received_on, processed_on, payout_status, approved_by, remarks, source_sheet
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, finance_rows)
                del df_finance

                # 3. Ingest Escalations
                df_escalations = pd.read_excel(file_path, sheet_name="Escalations", engine="openpyxl")
                escalation_rows = []
                for idx, row in df_escalations.iterrows():
                    raw_esc_id = safe_str(row.get("Escalation ID"))
                    raw_rel_ref = safe_str(row.get("Related Ticket / Ref"))
                    escalation_rows.append((
                        import_id,
                        int(idx),
                        raw_esc_id,
                        safe_str(raw_esc_id).upper() if raw_esc_id else None,
                        safe_date_str(row.get("Raised On")),
                        raw_rel_ref,
                        normalize_reference_id(raw_rel_ref),
                        safe_str(row.get("Raised By")),
                        safe_str(row.get("Agent / Team")),
                        safe_str(row.get("Channel")),
                        safe_str(row.get("Complaint")),
                        safe_str(row.get("Status")),
                        safe_date_str(row.get("Resolved On")),
                        safe_int(row.get("Days Open")),
                        "Escalations"
                    ))

                conn.executemany("""
                    INSERT INTO escalation_records (
                        import_id, row_index, escalation_id, norm_escalation_id, raised_on,
                        related_ticket_ref, norm_related_ref, raised_by, agent_team, channel, complaint,
                        status, resolved_on, days_open, source_sheet
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, escalation_rows)
                del df_escalations

                # Update import_history with completed counts
                conn.execute("""
                    UPDATE import_history
                    SET support_rows = ?,
                        finance_rows = ?,
                        escalation_rows = ?,
                        status = 'COMPLETED'
                    WHERE id = ?
                """, (len(support_rows), len(finance_rows), len(escalation_rows), import_id))

            return {
                "status": "SUCCESS",
                "import_id": import_id,
                "file_name": file_name,
                "file_hash": file_hash,
                "support_rows": len(support_rows),
                "finance_rows": len(finance_rows),
                "escalation_rows": len(escalation_rows),
                "message": f"Successfully ingested {len(support_rows)} support, {len(finance_rows)} finance, and {len(escalation_rows)} escalation records."
            }
        finally:
            conn.close()

    def get_summary(self) -> Dict[str, Any]:
        """Fetch counts of records in the database."""
        conn = self.get_connection()
        try:
            cursor = conn.cursor()
            self.init_schema(conn)

            cursor.execute("SELECT COUNT(*) FROM import_history WHERE status = 'COMPLETED'")
            completed_imports = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM support_records")
            support_count = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM finance_records")
            finance_count = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM escalation_records")
            escalation_count = cursor.fetchone()[0]

            return {
                "completed_imports": completed_imports,
                "support_records": support_count,
                "finance_records": finance_count,
                "escalation_records": escalation_count,
            }
        finally:
            conn.close()
