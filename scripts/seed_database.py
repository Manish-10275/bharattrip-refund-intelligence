#!/usr/bin/env python3
"""
Database Seeding and Schema Initialization Utility for BharatTrip Refund Intelligence.
Supports both PostgreSQL (Neon Serverless) and local SQLite environments.

Usage:
    # Seed using environment DATABASE_URL or .env:
    python scripts/seed_database.py

    # Seed explicit Neon PostgreSQL database:
    python scripts/seed_database.py --database-url "postgresql://user:pass@ep-xyz.us-east-2.aws.neon.tech/neondb?sslmode=require"

    # Seed explicit custom workbook file:
    python scripts/seed_database.py --file path/to/BharatTrip_Refund_Data.xlsx
"""

import argparse
import os
import re
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config, normalize_database_url
from db import get_connection, init_all_schemas
from services.data_service import DataService
from services.reconciliation_service import ReconciliationService


def sanitize_log(message: str) -> str:
    """Mask any passwords embedded in connection strings or exception messages."""
    if not message:
        return ""
    return re.sub(r":([^@/]+)@", r":***@", str(message))


def seed_database(database_url: str = None, excel_path: str = None, run_rec: bool = True) -> bool:
    target_url = normalize_database_url(database_url or Config.load_database_url())
    excel_file = excel_path or os.path.join(os.path.dirname(__file__), "..", "BharatTrip_Refund_Data.xlsx")
    excel_file = os.path.abspath(excel_file)

    backend = "PostgreSQL (Neon)" if (target_url and "postgres" in target_url) else "SQLite"
    print("==================================================")
    print("BharatTrip Refund Intelligence - Database Seeding")
    print(f"Target Backend : {backend}")
    print(f"Workbook File  : {excel_file}")
    print("==================================================")

    # 1. Connect & Initialize Schemas
    print("\n[1/3] Initializing database schemas...")
    conn = get_connection(database_url=target_url) if target_url else get_connection()
    try:
        init_all_schemas(conn)
        print("  [OK] All 8 tables and indexes verified successfully.")
    finally:
        conn.close()

    # 2. Ingest Excel Workbook
    data_service = DataService(database_url=target_url)
    if not os.path.exists(excel_file):
        print(f"\n[2/3] [WARN] Excel file '{excel_file}' not found. Skipping workbook ingestion.")
    else:
        print("\n[2/3] Ingesting operational dataset from Excel...")
        import_result = data_service.import_workbook(excel_file, force=True)
        print(f"  Import Status : {import_result.get('status')}")
        print(f"  Support Rows  : {import_result.get('support_rows', 0)}")
        print(f"  Finance Rows  : {import_result.get('finance_rows', 0)}")
        print(f"  Escalations   : {import_result.get('escalation_rows', 0)}")

    # 3. Execute Deterministic Reconciliation
    if run_rec:
        print("\n[3/3] Running automated 3-way reconciliation...")
        rec_service = ReconciliationService(database_url=target_url)
        rec_result = rec_service.run_reconciliation("Initial Automated Seeding Run")
        print(f"  Reconciliation Status  : {rec_result.get('status')}")
        print(f"  Total Evaluation Units : {rec_result.get('total_candidates', 0)}")
        print(f"  Deterministic Matched  : {rec_result.get('matched_count', 0)}")
        print(f"  Status Mismatches      : {rec_result.get('status_mismatch_count', 0)}")
        print(f"  Review Required        : {rec_result.get('review_required_count', 0)}")

    print("\n==================================================")
    print("Database seeding completed successfully!")
    print("==================================================")
    return True


def main():
    parser = argparse.ArgumentParser(description="Seed BharatTrip database for PostgreSQL or SQLite")
    parser.add_argument("--database-url", type=str, default=None, help="PostgreSQL connection string (Neon)")
    parser.add_argument("--file", type=str, default=None, help="Path to BharatTrip_Refund_Data.xlsx")
    parser.add_argument("--no-reconciliation", action="store_true", help="Skip initial reconciliation run")

    args = parser.parse_args()
    try:
        success = seed_database(
            database_url=args.database_url,
            excel_path=args.file,
            run_rec=not args.no_reconciliation
        )
        sys.exit(0 if success else 1)
    except Exception as e:
        clean_err = sanitize_log(str(e))
        print(f"\n[ERROR] Database seeding failed: {clean_err}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
