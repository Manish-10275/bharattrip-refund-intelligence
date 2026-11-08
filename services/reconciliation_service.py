import logging
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ReconciliationClassification:
    """Deterministic classification outcomes for refund reconciliation."""
    MATCHED = "MATCHED"
    STATUS_MISMATCH = "STATUS_MISMATCH"
    SUPPORT_ONLY = "SUPPORT_ONLY"
    FINANCE_ONLY = "FINANCE_ONLY"
    DUPLICATE_REFERENCE = "DUPLICATE_REFERENCE"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    INVALID_REFERENCE = "INVALID_REFERENCE"

    ALL_CLASSIFICATIONS = [
        MATCHED,
        STATUS_MISMATCH,
        SUPPORT_ONLY,
        FINANCE_ONLY,
        DUPLICATE_REFERENCE,
        AMBIGUOUS_MATCH,
        INVALID_REFERENCE,
    ]


class ReconciliationService:
    """
    Deterministic Financial Reconciliation Engine for BharatTrip Refund Intelligence.
    Compares Support Tracker and Finance Tracker records to identify matches,
    status mismatches, duplicates, orphaned records, and cases requiring human review.
    """

    def __init__(self, db_path: str = "bharattrip.db"):
        self.db_path = db_path

    def get_connection(self) -> sqlite3.Connection:
        """Create a connection to SQLite with row_factory enabled."""
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self, conn: Optional[sqlite3.Connection] = None) -> None:
        """Initialize reconciliation_runs and reconciliation_results tables."""
        close_needed = False
        if conn is None:
            conn = self.get_connection()
            close_needed = True

        try:
            with conn:
                # 1. Reconciliation runs ledger
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

                # 2. Detailed candidate results table
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
                        FOREIGN KEY (support_record_id) REFERENCES support_records(id),
                        FOREIGN KEY (finance_record_id) REFERENCES finance_records(id)
                    )
                """)

                # Indexes for filtering and fast queries
                conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_run_id ON reconciliation_results(run_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_classification ON reconciliation_results(classification)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_review_required ON reconciliation_results(review_required)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_rec_norm_ref ON reconciliation_results(norm_reference)")
        finally:
            if close_needed:
                conn.close()

    def run_reconciliation(self, notes: Optional[str] = None) -> Dict[str, Any]:
        """
        Execute deterministic reconciliation comparing Support and Finance records.
        Preserves original records, flags ambiguities, and writes results to reconciliation_results.
        """
        self.init_schema()
        conn = self.get_connection()

        try:
            cursor = conn.cursor()

            # Start a new reconciliation run entry
            with conn:
                cursor.execute(
                    "INSERT INTO reconciliation_runs (status, notes) VALUES ('RUNNING', ?)",
                    (notes or "Full deterministic reconciliation run",)
                )
                run_id = cursor.lastrowid

            # 1. Fetch all support records
            cursor.execute("""
                SELECT id, ticket_id, norm_ticket_id, status, agent, route, refund_amount, request_date
                FROM support_records
            """)
            support_rows = cursor.fetchall()

            # 2. Fetch all finance records
            cursor.execute("""
                SELECT id, ref_no, norm_ref_no, payout_status, agent_name, sector, amount_paid, deduction, processed_on, received_on
                FROM finance_records
            """)
            finance_rows = cursor.fetchall()

            results: List[Tuple[Any, ...]] = []

            # Group records by normalized reference
            support_by_ref = defaultdict(list)
            for r in support_rows:
                support_by_ref[r["norm_ticket_id"]].append(r)

            finance_by_ref = defaultdict(list)
            for r in finance_rows:
                finance_by_ref[r["norm_ref_no"]].append(r)

            # 3. Handle invalid, missing, or blank references first
            if None in support_by_ref:
                for s in support_by_ref[None]:
                    results.append((
                        run_id,
                        s["id"],
                        None,
                        s["ticket_id"],
                        None,
                        None,
                        ReconciliationClassification.INVALID_REFERENCE,
                        "Support record has invalid, missing, or unparseable reference ID.",
                        1  # Review required
                    ))

            if None in finance_by_ref:
                for f in finance_by_ref[None]:
                    results.append((
                        run_id,
                        None,
                        f["id"],
                        None,
                        f["ref_no"],
                        None,
                        ReconciliationClassification.INVALID_REFERENCE,
                        "Finance record has invalid, missing, or unparseable reference ID.",
                        1  # Review required
                    ))

            # Set of valid normalized references
            valid_refs = set(ref for ref in (set(support_by_ref.keys()) | set(finance_by_ref.keys())) if ref is not None)

            # 4. Process each normalized reference group
            for ref in sorted(valid_refs):
                s_list = support_by_ref.get(ref, [])
                f_list = finance_by_ref.get(ref, [])

                # --- Scenario A: Support Only ---
                if len(s_list) > 0 and len(f_list) == 0:
                    if len(s_list) == 1:
                        s = s_list[0]
                        results.append((
                            run_id,
                            s["id"],
                            None,
                            s["ticket_id"],
                            None,
                            ref,
                            ReconciliationClassification.SUPPORT_ONLY,
                            "Refund request logged in Support but absent from Finance Tracker.",
                            1
                        ))
                    else:
                        for s in s_list:
                            results.append((
                                run_id,
                                s["id"],
                                None,
                                s["ticket_id"],
                                None,
                                ref,
                                ReconciliationClassification.DUPLICATE_REFERENCE,
                                f"Duplicate reference in Support Tracker ({len(s_list)} occurrences) with no Finance record.",
                                1
                            ))

                # --- Scenario B: Finance Only ---
                elif len(s_list) == 0 and len(f_list) > 0:
                    if len(f_list) == 1:
                        f = f_list[0]
                        results.append((
                            run_id,
                            None,
                            f["id"],
                            None,
                            f["ref_no"],
                            ref,
                            ReconciliationClassification.FINANCE_ONLY,
                            "Disbursement logged in Finance but absent from Support Tracker.",
                            1
                        ))
                    else:
                        for f in f_list:
                            results.append((
                                run_id,
                                None,
                                f["id"],
                                None,
                                f["ref_no"],
                                ref,
                                ReconciliationClassification.DUPLICATE_REFERENCE,
                                f"Duplicate reference in Finance Tracker ({len(f_list)} occurrences) with no Support record.",
                                1
                            ))

                # --- Scenario C: Multiple candidates on one or both sides ---
                elif len(s_list) > 1 or len(f_list) > 1:
                    has_s_status_conflict = len(s_list) > 1 and len(set(s["status"] for s in s_list)) > 1
                    has_s_amt_conflict = len(s_list) > 1 and len(set(s["refund_amount"] for s in s_list if s["refund_amount"] is not None)) > 1
                    has_f_status_conflict = len(f_list) > 1 and len(set(f["payout_status"] for f in f_list)) > 1
                    has_f_amt_conflict = len(f_list) > 1 and len(set(f["amount_paid"] for f in f_list if f["amount_paid"] is not None)) > 1

                    is_ambiguous = has_s_status_conflict or has_s_amt_conflict or has_f_status_conflict or has_f_amt_conflict

                    if is_ambiguous:
                        # Ambiguous match: conflicting details across duplicate candidates
                        for s in s_list:
                            results.append((
                                run_id,
                                s["id"],
                                None,
                                s["ticket_id"],
                                None,
                                ref,
                                ReconciliationClassification.AMBIGUOUS_MATCH,
                                f"Ambiguous candidate for {ref}: multiple records exist with conflicting status or amount.",
                                1
                            ))
                        for f in f_list:
                            results.append((
                                run_id,
                                None,
                                f["id"],
                                None,
                                f["ref_no"],
                                ref,
                                ReconciliationClassification.AMBIGUOUS_MATCH,
                                f"Ambiguous candidate for {ref}: multiple records exist with conflicting status or amount.",
                                1
                            ))
                    else:
                        # Exact duplicate copies on one or both sides (e.g. repeated rows)
                        if len(s_list) > 1:
                            for s in s_list:
                                results.append((
                                    run_id,
                                    s["id"],
                                    f_list[0]["id"] if len(f_list) == 1 else None,
                                    s["ticket_id"],
                                    f_list[0]["ref_no"] if len(f_list) == 1 else None,
                                    ref,
                                    ReconciliationClassification.DUPLICATE_REFERENCE,
                                    f"Duplicate reference in Support Tracker ({len(s_list)} occurrences).",
                                    1
                                ))
                        if len(f_list) > 1:
                            for f in f_list:
                                results.append((
                                    run_id,
                                    s_list[0]["id"] if len(s_list) == 1 else None,
                                    f["id"],
                                    s_list[0]["ticket_id"] if len(s_list) == 1 else None,
                                    f["ref_no"],
                                    ref,
                                    ReconciliationClassification.DUPLICATE_REFERENCE,
                                    f"Duplicate reference in Finance Tracker ({len(f_list)} occurrences).",
                                    1
                                ))

                # --- Scenario D: Exactly 1 Support and 1 Finance record ---
                elif len(s_list) == 1 and len(f_list) == 1:
                    s = s_list[0]
                    f = f_list[0]
                    s_status = s["status"]
                    f_status = f["payout_status"]

                    s_route = (s["route"] or "").strip().upper()
                    f_sector = (f["sector"] or "").strip().upper()

                    # Identity check: verify route does not contradict
                    if s_route and f_sector and s_route != f_sector:
                        results.append((
                            run_id,
                            s["id"],
                            f["id"],
                            s["ticket_id"],
                            f["ref_no"],
                            ref,
                            ReconciliationClassification.AMBIGUOUS_MATCH,
                            f"Normalized reference matches ({ref}) but flight route contradicts: Support '{s['route']}' vs Finance '{f['sector']}'.",
                            1
                        ))
                    else:
                        # Status comparison using source values
                        if s_status == "Closed" and f_status == "Refund Done":
                            # Payout evidence check: amount_paid and processed_on must exist
                            has_payout_evidence = (f["amount_paid"] is not None) and (f["processed_on"] is not None)
                            if not has_payout_evidence:
                                results.append((
                                    run_id,
                                    s["id"],
                                    f["id"],
                                    s["ticket_id"],
                                    f["ref_no"],
                                    ref,
                                    ReconciliationClassification.STATUS_MISMATCH,
                                    "Support marked Closed and Finance marked Refund Done, but payout evidence is missing (amount paid or processed date missing).",
                                    1
                                ))
                            else:
                                results.append((
                                    run_id,
                                    s["id"],
                                    f["id"],
                                    s["ticket_id"],
                                    f["ref_no"],
                                    ref,
                                    ReconciliationClassification.MATCHED,
                                    "Support Closed and Finance Refund Done with verified payout evidence.",
                                    0  # No review required
                                ))

                        elif s_status == "Not Eligible" and f_status == "Declined":
                            # Both teams agree no refund is due
                            results.append((
                                run_id,
                                s["id"],
                                f["id"],
                                s["ticket_id"],
                                f["ref_no"],
                                ref,
                                ReconciliationClassification.MATCHED,
                                "Consistent non-payout: Support Not Eligible and Finance Declined.",
                                0  # No review required
                            ))

                        elif s_status == "Closed" and f_status == "Pending Payout":
                            results.append((
                                run_id,
                                s["id"],
                                f["id"],
                                s["ticket_id"],
                                f["ref_no"],
                                ref,
                                ReconciliationClassification.STATUS_MISMATCH,
                                "Status mismatch: Support Closed ticket but Finance has Pending Payout (unpaid refund).",
                                1
                            ))

                        elif s_status == "Closed" and f_status == "Declined":
                            results.append((
                                run_id,
                                s["id"],
                                f["id"],
                                s["ticket_id"],
                                f["ref_no"],
                                ref,
                                ReconciliationClassification.STATUS_MISMATCH,
                                "Status mismatch: Support marked Closed but Finance Declined refund.",
                                1
                            ))

                        elif s_status == "Working" and f_status == "Pending Payout":
                            results.append((
                                run_id,
                                s["id"],
                                f["id"],
                                s["ticket_id"],
                                f["ref_no"],
                                ref,
                                ReconciliationClassification.STATUS_MISMATCH,
                                "Status mismatch: Support still Working while Finance is Pending Payout.",
                                1
                            ))

                        elif s_status == "Escalated" and f_status == "Pending Payout":
                            results.append((
                                run_id,
                                s["id"],
                                f["id"],
                                s["ticket_id"],
                                f["ref_no"],
                                ref,
                                ReconciliationClassification.STATUS_MISMATCH,
                                "Status mismatch: Support ticket Escalated while Finance is Pending Payout.",
                                1
                            ))

                        elif s_status == "Working" and f_status == "Declined":
                            results.append((
                                run_id,
                                s["id"],
                                f["id"],
                                s["ticket_id"],
                                f["ref_no"],
                                ref,
                                ReconciliationClassification.STATUS_MISMATCH,
                                "Status mismatch: Support Working while Finance Declined refund.",
                                1
                            ))

                        elif s_status == "Working" and f_status == "Refund Done":
                            results.append((
                                run_id,
                                s["id"],
                                f["id"],
                                s["ticket_id"],
                                f["ref_no"],
                                ref,
                                ReconciliationClassification.STATUS_MISMATCH,
                                "Status mismatch: Support Working but Finance already completed payout.",
                                1
                            ))

                        elif s_status == "Escalated" and f_status == "Refund Done":
                            results.append((
                                run_id,
                                s["id"],
                                f["id"],
                                s["ticket_id"],
                                f["ref_no"],
                                ref,
                                ReconciliationClassification.STATUS_MISMATCH,
                                "Status mismatch: Support Escalated ticket while Finance marked Refund Done.",
                                1
                            ))

                        else:
                            results.append((
                                run_id,
                                s["id"],
                                f["id"],
                                s["ticket_id"],
                                f["ref_no"],
                                ref,
                                ReconciliationClassification.STATUS_MISMATCH,
                                f"Status mismatch: Support '{s_status}' and Finance '{f_status}'.",
                                1
                            ))

            # 5. Persist all reconciliation results in batch
            with conn:
                conn.executemany("""
                    INSERT INTO reconciliation_results (
                        run_id, support_record_id, finance_record_id,
                        original_support_ref, original_finance_ref, norm_reference,
                        classification, reason, review_required
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, results)

                # Compute count metrics
                counts = defaultdict(int)
                review_count = 0
                for r in results:
                    counts[r[6]] += 1
                    if r[8] == 1:
                        review_count += 1

                # Update reconciliation_runs ledger
                conn.execute("""
                    UPDATE reconciliation_runs
                    SET total_candidates = ?,
                        matched_count = ?,
                        status_mismatch_count = ?,
                        support_only_count = ?,
                        finance_only_count = ?,
                        duplicate_reference_count = ?,
                        ambiguous_match_count = ?,
                        invalid_reference_count = ?,
                        review_required_count = ?,
                        status = 'COMPLETED'
                    WHERE id = ?
                """, (
                    len(results),
                    counts[ReconciliationClassification.MATCHED],
                    counts[ReconciliationClassification.STATUS_MISMATCH],
                    counts[ReconciliationClassification.SUPPORT_ONLY],
                    counts[ReconciliationClassification.FINANCE_ONLY],
                    counts[ReconciliationClassification.DUPLICATE_REFERENCE],
                    counts[ReconciliationClassification.AMBIGUOUS_MATCH],
                    counts[ReconciliationClassification.INVALID_REFERENCE],
                    review_count,
                    run_id
                ))

            logger.info(f"Reconciliation run {run_id} completed: {len(results)} total candidates, {review_count} require review.")

            return {
                "status": "SUCCESS",
                "run_id": run_id,
                "total_candidates": len(results),
                "matched_count": counts[ReconciliationClassification.MATCHED],
                "status_mismatch_count": counts[ReconciliationClassification.STATUS_MISMATCH],
                "support_only_count": counts[ReconciliationClassification.SUPPORT_ONLY],
                "finance_only_count": counts[ReconciliationClassification.FINANCE_ONLY],
                "duplicate_reference_count": counts[ReconciliationClassification.DUPLICATE_REFERENCE],
                "ambiguous_match_count": counts[ReconciliationClassification.AMBIGUOUS_MATCH],
                "invalid_reference_count": counts[ReconciliationClassification.INVALID_REFERENCE],
                "review_required_count": review_count,
            }

        finally:
            conn.close()

    def get_latest_run(self) -> Optional[Dict[str, Any]]:
        """Fetch the most recent reconciliation run metadata."""
        conn = self.get_connection()
        try:
            self.init_schema(conn)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM reconciliation_runs 
                WHERE status = 'COMPLETED' 
                ORDER BY id DESC LIMIT 1
            """)
            row = cursor.fetchone()
            if not row:
                return None
            return dict(row)
        finally:
            conn.close()

    def get_run_summary(self, run_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Fetch summary metrics for a given run ID (or latest completed run)."""
        conn = self.get_connection()
        try:
            self.init_schema(conn)
            cursor = conn.cursor()
            if run_id:
                cursor.execute("SELECT * FROM reconciliation_runs WHERE id = ?", (run_id,))
            else:
                cursor.execute("SELECT * FROM reconciliation_runs WHERE status = 'COMPLETED' ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            if not row:
                return None
            return dict(row)
        finally:
            conn.close()

    def get_results(
        self,
        run_id: Optional[int] = None,
        classification: Optional[str] = None,
        review_required: Optional[bool] = None,
        search_query: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Query paginated reconciliation results with optional classification and review status filters.
        Returns (list_of_results, total_count).
        """
        conn = self.get_connection()
        try:
            self.init_schema(conn)
            cursor = conn.cursor()

            if not run_id:
                cursor.execute("SELECT id FROM reconciliation_runs WHERE status = 'COMPLETED' ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                if not row:
                    return [], 0
                run_id = row[0]

            where_clauses = ["r.run_id = ?"]
            params: List[Any] = [run_id]

            if classification and classification != "ALL":
                where_clauses.append("r.classification = ?")
                params.append(classification)

            if review_required is not None:
                where_clauses.append("r.review_required = ?")
                params.append(1 if review_required else 0)

            if search_query and search_query.strip():
                q = f"%{search_query.strip()}%"
                where_clauses.append(
                    "(r.norm_reference LIKE ? OR r.original_support_ref LIKE ? OR r.original_finance_ref LIKE ? OR r.reason LIKE ?)"
                )
                params.extend([q, q, q, q])

            where_sql = " AND ".join(where_clauses)

            # Count total
            count_query = f"SELECT COUNT(*) FROM reconciliation_results r WHERE {where_sql}"
            cursor.execute(count_query, params)
            total_count = cursor.fetchone()[0]

            # Fetch paginated rows with joined details
            data_query = f"""
                SELECT 
                    r.id,
                    r.run_id,
                    r.classification,
                    r.norm_reference,
                    r.original_support_ref,
                    r.original_finance_ref,
                    r.reason,
                    r.review_required,
                    r.created_at,
                    s.status AS support_status,
                    s.refund_amount AS support_amount,
                    s.agent AS support_agent,
                    s.route AS support_route,
                    f.payout_status AS finance_status,
                    f.amount_paid AS finance_paid,
                    f.deduction AS finance_deduction,
                    f.agent_name AS finance_agent,
                    f.sector AS finance_sector,
                    f.processed_on AS finance_processed_on
                FROM reconciliation_results r
                LEFT JOIN support_records s ON r.support_record_id = s.id
                LEFT JOIN finance_records f ON r.finance_record_id = f.id
                WHERE {where_sql}
                ORDER BY r.review_required DESC, r.id ASC
                LIMIT ? OFFSET ?
            """
            cursor.execute(data_query, params + [limit, offset])
            rows = [dict(row) for row in cursor.fetchall()]

            return rows, total_count
        finally:
            conn.close()
