import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from db import DatabaseConnection, DatabaseOperationalError, get_connection as db_get_connection, init_all_schemas
from services.intake_schema import RefundIntakeExtraction

logger = logging.getLogger(__name__)

VALID_CHANNELS = ["Email", "WhatsApp", "Manual"]


class IntakeService:
    """
    Persistence and query service for AI customer refund intake records.
    Maintains complete audit trails while strictly avoiding modification
    of Finance or Support operational tables.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path

    def get_connection(self) -> DatabaseConnection:
        """Create a connection to the database (PostgreSQL if configured, else SQLite)."""
        return db_get_connection(db_path=self.db_path)

    def init_schema(self, conn: Optional[DatabaseConnection] = None) -> None:
        """Idempotently initialize the intake_records table and indexes."""
        close_needed = False
        if conn is None:
            conn = self.get_connection()
            close_needed = True
        try:
            init_all_schemas(conn)
        finally:
            if close_needed:
                conn.close()

    def create_record(
        self,
        conn: DatabaseConnection,
        original_message: str,
        source_channel: str,
        extraction: RefundIntakeExtraction,
        model_name: str,
        prompt_version: str
    ) -> int:
        """
        Persist an AI extraction result alongside the verbatim original message.
        
        Args:
            conn: Active SQLite connection.
            original_message: Customer message exactly as received.
            source_channel: Source channel ('Email', 'WhatsApp', or 'Manual').
            extraction: Validated RefundIntakeExtraction instance.
            model_name: Identifier of the model used (e.g. 'openai/gpt-oss-20b').
            prompt_version: System prompt version string (e.g. 'v1.0.0').
            
        Returns:
            int: The primary key ID of the newly inserted record.
        """
        clean_channel = source_channel.strip() if source_channel else "Manual"
        if clean_channel not in VALID_CHANNELS:
            clean_channel = "Manual"

        # Determine processing status: claims requiring human attention are flagged PENDING_REVIEW
        processing_status = "PENDING_REVIEW" if extraction.human_review_required else "PROCESSED"

        extracted_dict = extraction.model_dump()
        extracted_json_str = json.dumps(extracted_dict, ensure_ascii=False)
        missing_info_json = json.dumps(extraction.missing_information, ensure_ascii=False)

        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO intake_records (
                original_message,
                source_channel,
                customer_name,
                agency_name,
                booking_reference,
                refund_reference,
                route,
                refund_amount,
                currency,
                request_date,
                complaint_category,
                missing_information,
                summary,
                confidence,
                human_review_required,
                human_review_reason,
                processing_status,
                model_name,
                prompt_version,
                extracted_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
        """, (
            original_message,
            clean_channel,
            extraction.customer_name,
            extraction.agency_name,
            extraction.booking_reference,
            extraction.refund_reference,
            extraction.route,
            extraction.refund_amount,
            extraction.currency,
            extraction.request_date,
            str(extraction.complaint_category),
            missing_info_json,
            extraction.summary,
            extraction.confidence,
            1 if extraction.human_review_required else 0,
            extraction.human_review_reason,
            processing_status,
            model_name,
            prompt_version,
            extracted_json_str
        ))
        row = cursor.fetchone()
        conn.commit()
        return row[0] if row else cursor.lastrowid

    def get_record(self, conn: DatabaseConnection, record_id: int) -> Optional[Dict[str, Any]]:
        """Fetch a single intake record by ID, deserializing nested JSON fields."""
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM intake_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if not row:
            return None

        record = dict(row)
        try:
            record["missing_information"] = json.loads(record.get("missing_information") or "[]")
        except Exception:
            record["missing_information"] = []

        try:
            record["extracted_data"] = json.loads(record.get("extracted_json") or "{}")
        except Exception:
            record["extracted_data"] = {}

        return record

    def get_review_queue(
        self,
        conn: DatabaseConnection,
        category: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        List records requiring human operator review with optional filtering.
        
        Args:
            conn: Active SQLite connection.
            category: Filter by complaint category or None for ALL.
            status: Filter by processing_status ('PENDING_REVIEW', 'REVIEWED', 'PROCESSED') or None.
            limit: Pagination limit.
            offset: Pagination offset.
            
        Returns:
            Tuple of (list of records, total count).
        """
        cursor = conn.cursor()

        where_clauses = []
        params: List[Any] = []

        # Strict Status Filtering Rules:
        # 1. Default queue view: only records awaiting human review (processing_status = 'PENDING_REVIEW')
        # 2. Explicit PENDING_REVIEW: only pending records
        # 3. Explicit REVIEWED: only reviewed records
        # 4. Explicit PROCESSED: only auto-processed records
        # 5. Explicit ALL: all records (no filter on processing_status)
        if status == "ALL":
            pass
        elif status in ("PENDING_REVIEW", "REVIEWED", "PROCESSED"):
            where_clauses.append("processing_status = ?")
            params.append(status)
        else:
            # Default review queue view
            where_clauses.append("processing_status = 'PENDING_REVIEW'")

        if category and category != "ALL":
            where_clauses.append("complaint_category = ?")
            params.append(category)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        # Count total matching
        cursor.execute(f"SELECT COUNT(*) FROM intake_records {where_sql}", params)
        total_count = cursor.fetchone()[0]

        # Fetch page
        query = f"""
            SELECT * FROM intake_records 
            {where_sql}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """
        cursor.execute(query, params + [limit, offset])
        rows = cursor.fetchall()

        results = []
        for r in rows:
            rec = dict(r)
            try:
                rec["missing_information"] = json.loads(rec.get("missing_information") or "[]")
            except Exception:
                rec["missing_information"] = []
            results.append(rec)

        return results, total_count

    def mark_as_reviewed(
        self,
        conn: DatabaseConnection,
        record_id: int,
        reviewer: str = "Support Operator"
    ) -> bool:
        """
        Mark an intake record as reviewed by human operator.
        
        STRICT OPERATIONAL SAFETY:
        - Only allows review of records with processing_status = 'PENDING_REVIEW'.
        - Safely rejects attempts to review records that are already 'REVIEWED' or 'PROCESSED'.
        - Preserves human_review_required unchanged (historical AI recommendation).
        - NEVER modifies Support records, Finance records, payout statuses,
          or refund eligibility decisions.
        """
        cursor = conn.cursor()
        cursor.execute("SELECT id, processing_status FROM intake_records WHERE id = ?", (record_id,))
        row = cursor.fetchone()
        if not row:
            return False

        # Reject attempts to review records that are not in PENDING_REVIEW status
        if row["processing_status"] != "PENDING_REVIEW":
            logger.warning(
                f"Attempted to review record #{record_id} with non-pending status '{row['processing_status']}'"
            )
            return False

        cursor.execute("""
            UPDATE intake_records 
            SET processing_status = 'REVIEWED',
                reviewed_at = CURRENT_TIMESTAMP,
                reviewed_by = ?
            WHERE id = ? AND processing_status = 'PENDING_REVIEW'
        """, (reviewer, record_id))
        conn.commit()
        return cursor.rowcount > 0

    def get_stats(self, conn: DatabaseConnection) -> Dict[str, int]:
        """Aggregate summary counts of intake records."""
        cursor = conn.cursor()
        stats = {
            "total_intakes": 0,
            "pending_review": 0,
            "reviewed": 0,
            "auto_processed": 0,
        }
        try:
            cursor.execute("SELECT COUNT(*) FROM intake_records")
            stats["total_intakes"] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM intake_records WHERE processing_status = 'PENDING_REVIEW'")
            stats["pending_review"] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM intake_records WHERE processing_status = 'REVIEWED'")
            stats["reviewed"] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM intake_records WHERE processing_status = 'PROCESSED'")
            stats["auto_processed"] = cursor.fetchone()[0]
        except DatabaseOperationalError:
            pass

        return stats
