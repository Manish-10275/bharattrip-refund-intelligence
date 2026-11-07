# BharatTrip Refund Intelligence — Data Quality & Cleansing Methodology

This document outlines the data normalization principles, missing value handling, duplicate handling, and validation rules used by the BharatTrip Refund Intelligence platform.

---

## 1. Overview & Data Quality Challenges

Operational data in travel refund processing typically originates from disparate departments with divergent logging conventions:
- **Support Tracker:** Customer-facing tickets, informal notes, loose reference strings, and status revisions.
- **Finance Tracker:** ERP/payout ledgers, batch bank processing timestamps, revised transaction tags.
- **Escalation Logs:** Dispute records submitted by external partner agencies with missing or placeholder references (`-`, `no ref`, `blank`).

The platform establishes an automated, deterministic cleansing layer that guarantees ledger integrity without destroying or mutating raw operational inputs.

---

## 2. Ingestion Provenance & SHA-256 Idempotency

To prevent accidental double-counting and ensure reproducible historical tracking:
1. Every input workbook is hashed using SHA-256 prior to opening (`hashlib.sha256(file_bytes).hexdigest()`).
2. The hash is checked against `import_history`.
3. If an import with the same hash succeeded previously, the ingestion engine safely skips execution and returns the existing `import_id`.
4. Ingested records record the `import_id` and original spreadsheet row index (`row_index`) to preserve row-level provenance back to the source file.

---

## 3. Reference ID Normalization Rules

Cross-departmental reconciliation fails if references have inconsistent formatting or syntax errors. The `DataService.normalize_reference_id()` engine applies canonicalization rules:

### Normalization Logic:
```python
import re

def normalize_reference_id(raw_val: str | None) -> str | None:
    if raw_val is None:
        return None
    val = str(raw_val).strip()
    if not val or val.lower() in {'nan', 'none', 'null', '-', 'no ref', 'na', 'n/a'}:
        return None
    
    # Match standard or dirty RF patterns (e.g. 'rf-1234', 'RF 1234', 'RF1234', 'RF-1234 (revised)')
    match = re.search(r'rf[ -]?(\d+)', val, re.IGNORECASE)
    if match:
        return f"RF-{match.group(1)}"
    
    return val
```

### Observed Edge Cases Handled:
| Source Department | Raw Ingested String | Normalized Canonical ID | Original String Preserved |
| :--- | :--- | :--- | :---: |
| **Support** | `RF 1750` | `RF-1750` | Yes |
| **Support** | `rf-1650` | `RF-1650` | Yes |
| **Finance** | `RF1109` | `RF-1109` | Yes |
| **Finance** | `RF-1045 (revised)` | `RF-1045` | Yes |
| **Escalations** | `no ref` | `NULL` | Yes |
| **Escalations** | `-` | `NULL` | Yes |
| **Escalations** | `[Blank / NaN]` | `NULL` | Yes |

*Crucial Rule:* The canonical normalized reference (`norm_ticket_id`, `norm_ref_no`, `norm_related_ref`) is stored in a separate column alongside the verbatim original text, ensuring complete auditability.

---

## 4. Multi-Format Date & Timestamp Parsing

Operational workbooks contain mixed date representations:
- Native openpyxl/Excel datetime objects (`2026-03-15 10:30:00`)
- ISO strings (`2026-04-12`)
- European/Indian formatted strings (`09-06-2026`, `30/05/26`)

### Cleansing Strategy:
- Dates are normalized to standard ISO 8601 strings (`YYYY-MM-DD HH:MM:SS` or `YYYY-MM-DD`) where parseable.
- Ambiguous dates are parsed with deterministic day-first conventions appropriate for Indian travel operations (`dayfirst=True`).
- Unparseable strings retain their textual representations in notes, avoiding data loss.

---

## 5. Strict Missing Value (NULL) Discipline

In relational databases and statistical operations, string literals like `'nan'`, `'None'`, or `'N/A'` cause severe bugs in SQL aggregation (e.g. `COUNT(column)` counting strings as valid values).

### Transformation Rules:
1. All pandas `NaN`, `NaT`, empty strings `""`, and whitespace-only strings are explicitly cast to SQLite `NULL` (`None` in Python).
2. Numerical amounts: Missing refund amounts are stored as `NULL`, never defaulted to `0.00` (which would falsely imply a zero-dollar claim).
3. Agents / Channels: Missing assignees are recorded as `NULL`, surfaced in the UI as `"Unassigned"` or `"Unspecified"` with appropriate visual badges.

---

## 6. Duplicate Resolution & Ledger Immutability

When multiple rows share the same normalized reference ID within the same ledger:
- In `support_records`, duplicate pairs (e.g. repeated ticket submissions) are preserved verbatim with distinct primary keys and row indices.
- The reconciliation engine flags these records under `DUPLICATE_REFERENCE`, preventing erroneous 1-to-many matches against single Finance disbursements.
- Reconciliation is strictly **read-only**: it never updates, deletes, or mutates ingested source rows. All reconciliation conclusions are stored in dedicated run ledger tables (`reconciliation_runs`, `reconciliation_results`).
