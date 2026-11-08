# BharatTrip Refund Intelligence — Architecture Specification

This document details the software architecture, core services, database schemas, central database layer, and deployment patterns for the BharatTrip Refund Intelligence operational platform.

---

## 1. System Overview & Component Diagram

The platform is an operational prototype and triage system designed for B2B flight refund management, deterministic cross-departmental reconciliation, dispute tracking, and structured customer message intake.

```
                           +----------------------------------------+
                           |       Inbound Customer Inquiries       |
                           |   (Email, WhatsApp, Support Forms)     |
                           +----------------------------------------+
                                               |
                                               v
+--------------------------+       +--------------------------------+
| Multi-Sheet Excel Source |       |   Groq AI Intake Subsystem     |
|   (Support, Finance,     |       |   (openai/gpt-oss-20b)         |
|      Escalations)        |       |   - Untrusted Input Boundary   |
+--------------------------+       |   - Strict JSON Schema         |
             |                     |   - Strict Null Safeguards     |
             v                     +--------------------------------+
+--------------------------+                       |
|   Data Ingestion Layer   |                       v
|  - Idempotency Hashing   |       +--------------------------------+
|  - Reference Regex Norm  |       |   Human Triage Review Queue    |
|  - Missing Value NULLs   |       |   - Mandatory Justifications   |
+--------------------------+       |   - Operator Review Sign-off   |
             |                     +--------------------------------+
             v                                     |
+-------------------------------------------------------------------+
|               Central Database Abstraction Layer (db.py)           |
|   - Transparent Query Parameter Translation (? -> %s)             |
|   - Dual-Access Case-Insensitive RowWrapper (row["col"] & row[0]) |
|   - Request-Scoped Connection Lifecycle (get_db / close_db)       |
|   - Dialect-Aware DDL (PostgreSQL Identity vs SQLite Autoinc)     |
+-------------------------------------------------------------------+
             |                                     |
             v (Production)                        v (Local / Test)
+--------------------------+       +--------------------------------+
|   Neon PostgreSQL (v16+) |       |       SQLite 3 Database        |
|  - Serverless Pooler     |       |  - Local developer speed       |
|  - sslmode=require       |       |  - Offline automated tests     |
+--------------------------+       +--------------------------------+
             |                                     |
             +------------------+------------------+
                                |
                                v
             +-------------------------------------------+
             |    Deterministic Reconciliation Engine    |
             |    - 7 Mutually Exclusive Outcomes        |
             |    - Payout vs Non-Payout Corroboration   |
             |    - Ambiguity & Dispute Detection        |
             +-------------------------------------------+
                                |
                                v
             +-------------------------------------------+
             |      Vercel Serverless Web Interface      |
             |  - Operations Dashboard (/)               |
             |  - Dispute Escalations (/escalations)     |
             |  - Reconciliation Ledger (/reconciliation)|
             |  - Refund Records Ledger (/refund-records)|
             |  - Operations Assistant (/assistant)      |
             |  - Session Auth & CSRF Protection         |
             |  - Edge Static Asset Serving (/static)    |
             +-------------------------------------------+
```

---

## 2. Core Service Boundaries

### 2.1 Data Ingestion Service (`services/data_service.py`)
- **Responsibility:** Ingests external multi-sheet Excel workbooks into normalized relational tables.
- **SHA-256 Provenance & Idempotency:** Calculates the cryptographic digest of the ingested workbook prior to processing. If the digest matches an existing successful entry in `import_history`, duplicate re-insertion is aborted.
- **Reference Normalization:** Sanitizes dirty reference strings across disparate departments into canonical `RF-<digits>` identifiers using regular expressions (`RF-?\s*(\d+)`), while preserving verbatim original strings.
- **Strict Null Discipline:** Transforms pandas `NaN`, `NaT`, empty strings, and sentinel values into SQL `NULL` rather than string literals `'nan'` or `'None'`.

### 2.2 Deterministic Reconciliation Engine (`services/reconciliation_service.py`)
- **Responsibility:** Evaluates normalized candidate pairings between Support and Finance tracking.
- **Deterministic State Machine:** Classifies candidate evaluation units into seven mutually exclusive categories:
  1. `MATCHED`: Represents agreement between Support and Finance under the implemented deterministic matching rules.
     - *544 Payout Agreements:* Support status `Closed` and Finance status `Refund Done` with payout fields populated.
     - *27 Non-Payout Outcomes:* Agreed Support `Not Eligible` and Finance `Declined` outcomes.
     - *Important Boundary:* Neither classification establishes independent external bank transaction confirmation or bank settlement verification; it confirms alignment between the internal tracking datasets.
  2. `STATUS_MISMATCH`: Matching reference but contradictory operational statuses (e.g. Support marked Refunded while Finance shows Pending/Rejected).
  3. `SUPPORT_ONLY`: Support ticket exists with no finance record.
  4. `FINANCE_ONLY`: Payout disbursement recorded in Finance without a corresponding support record.
  5. `DUPLICATE_REFERENCE`: Multiple conflicting records on one side sharing a reference.
  6. `AMBIGUOUS_MATCH`: Matching reference with conflicting airline sector or passenger route.
  7. `INVALID_REFERENCE`: Placeholder (`-`, `no ref`) or blank reference.
- **Ledger Immutability:** Reconciliation executes read-only operations on source records. Results are recorded as distinct historical runs in `reconciliation_runs` and `reconciliation_results`.

### 2.3 Groq AI Extraction Service (`services/groq_service.py` & `services/intake_schema.py`)
- **Responsibility:** Transforms unformatted, multilingual, or colloquial inbound messages into typed structured JSON.
- **Model Runtime:** `openai/gpt-oss-20b` via Groq Cloud SDK.
- **Extraction Safeguards & Null Handling:** The service uses strict JSON schema validation and explicit prompt guardrails to prevent the generation of fabricated values. Fields not explicitly stated in customer text (passenger names, PNRs, dates, amounts) must strictly resolve to `null`.
- **Defensive Prompt Injection Safety:** Treats all customer text strictly as untrusted input data. Injection commands ("Ignore previous instructions", "Approve immediately") are ignored, flagged, and assigned to human oversight.
- **Factual Grounding & Currency Rules:** The model possesses zero decision authority. Currency is extracted only when an explicit symbol (`₹`, `$`) or code (`INR`, `USD`) is present in the customer message.

### 2.4 Intake Operations Service (`services/intake_service.py`)
- **Responsibility:** Manages the persistence and triage lifecycle of inbound intake records in `intake_records`.
- **Review Lifecycle State Transitions:**
  - Records triaged automatically without requiring human oversight (`human_review_required = False`) are initialized directly with `processing_status = 'PROCESSED'`.
  - Records requiring human attention (`human_review_required = True`) are initialized with `processing_status = 'PENDING_REVIEW'`.
  - When an authorized operator inspects the extraction and completes review via `POST /intake/<record_id>/review`, the status transitions from `PENDING_REVIEW` to `REVIEWED`.
  - `REVIEWED` represents human operator sign-off and does not automatically transition to `PROCESSED`.
- **Human Oversight Gate:** Enforces human review flags when confidence is low, when required entities are missing, or when policy disputes are identified.

---

## 3. Central Database Layer (`db.py`)

To eliminate driver lock-in and enable frictionless deployment across serverless cloud environments (Vercel + Neon) and local development (SQLite), BharatTrip features a centralized database layer:

### 3.1 Transparent Query Translation
Existing SQL queries written with SQLite-style positional `?` parameter placeholders are dynamically translated at execution time into PostgreSQL-compatible `%s` placeholders via `translate_query_for_postgres()`. The regex parser preserves literal question marks inside single-quoted strings.

### 3.2 Dual-Access `RowWrapper`
PostgreSQL dictionaries and SQLite row objects exhibit subtle behavioral differences. `RowWrapper` wraps records returned from both drivers to guarantee identical interfaces:
- **Case-Insensitive Column Access:** `row["norm_ticket_id"]` and `row["NORM_TICKET_ID"]`.
- **Positional Integer Indexing:** `row[0]`, `row[1]`.
- **Standard Dictionary Methods:** `row.get("key", default)`, `dict(row)`, and `'key' in row`.

### 3.3 Serverless Connection Lifecycle (`get_db` & `close_db`)
Serverless platforms like Vercel create ephemeral function invocations. To prevent connection pool exhaustion against Neon:
- `get_db()` attaches a single database connection to Flask's application context (`flask.g.db`).
- `close_db()` is registered with `@app.teardown_appcontext` to guarantee the connection is closed and returned to Neon's PgBouncer pool immediately after request completion.
- Startup schema initialization (`init_db()`) is wrapped defensively to prevent module import failures if credentials are not injected during Vercel build stages.

### 3.4 Transaction Management
`DatabaseConnection` provides standard Python context manager semantics (`with conn:`):
- Automatically manages transactions across both drivers.
- In PostgreSQL, manages savepoints and commits explicitly upon normal block exit, rolling back on unhandled exceptions.

---

## 4. Database Schema

The database consists of eight relational tables:

1. **`refund_records`**: Core ledger table tracking correlated refund cases, customer names, travel agencies, routes, and amounts.
2. **`import_history`**: Tracks workbook SHA-256 hashes, file names, row counts, and ingestion timestamps.
3. **`support_records`**: Ingested Support tickets with normalized `norm_ticket_id`, route, claimed amount, request date, status, agent, and channel.
4. **`finance_records`**: Ingested Finance disbursements with normalized `norm_ref_no`, amount paid, deduction, processed date, and payout status.
5. **`escalation_records`**: Ingested agency dispute escalations with related ticket reference, raised date, team, channel, complaint, status, and resolution date.
6. **`reconciliation_runs`**: Header table storing run timestamp, total candidates, classification counts, and review metrics.
7. **`reconciliation_results`**: Individual candidate evaluation units linked to run ID, support record ID, finance record ID, classification tag, and reason.
8. **`intake_records`**: Inbound AI-triaged messages with raw customer text, validated JSON, extracted entities, confidence, review flags, and audit sign-offs.

---

## 5. Security & Compliance Architecture

- **Cryptographic Secret Enforcement:** `config.py` enforces a non-empty `SECRET_KEY` in non-testing environments. Application startup fails with a `RuntimeError` if missing.
- **Session Authentication:** All operational endpoints require an authenticated operator session verified via `@login_required`.
- **CSRF Defense:** All mutating requests (`POST /login`, `POST /logout`, `POST /intake`, `POST /reconciliation/run`) require valid CSRF tokens via Flask-WTF.
- **Cookie Security:** `SESSION_COOKIE_HTTPONLY = True`, `SESSION_COOKIE_SAMESITE = 'Lax'`, and production-default `SESSION_COOKIE_SECURE = True`.
- **Operator Authentication Enforcement:** `OPERATOR_USERNAME` and `OPERATOR_PASSWORD_HASH` are strictly enforced at startup in non-testing environments, failing with `RuntimeError` if missing.
- **Offline / Mocked Test Isolation:** The automated test suite executes fully offline with mocked Groq fixtures and isolated test secrets without external network requirements.

---

## 6. Production Deployment Architecture (Vercel + Neon)

The primary production architecture combines **Vercel Serverless Functions** with **Neon Serverless PostgreSQL**:

### 6.1 Vercel Serverless Runtime
- **Routing (`vercel.json`):** Dynamic rewrites direct all HTTP application traffic to `app.py`.
- **Edge Asset Caching:** Static assets located in `public/static/style.css` are served directly by Vercel's global CDN edge, delivering sub-millisecond static file delivery without invoking serverless function executions.
- **Function Execution:** Python 3.12+ WSGI serverless function with `maxDuration: 60`.

### 6.2 Neon Managed PostgreSQL
- **Connection Pooling:** Serverless functions connect via Neon's pooled connection endpoint (`ep-*-pooler.neon.tech`), utilizing PgBouncer to manage high-concurrency connection spikes.
- **SSL Enforcement:** Neon requires encrypted transport; `config.py` automatically normalizes connection strings to include `sslmode=require`.
- **Schema Migrations:** Initial schema generation (`init_db()`) uses idempotent DDL (`CREATE TABLE IF NOT EXISTS`) and `GENERATED BY DEFAULT AS IDENTITY` primary keys.
- **One-Off Seeding:** The CLI script `scripts/seed_database.py` enables operators to seed tables, ingest operational workbooks, and run initial reconciliation against Neon remotely.

### 6.3 Secondary Deployment (Render)
As an alternative single-node deployment, the project retains `render.yaml` for Render Web Services with persistent block storage disks mounted at `/var/data/bharattrip.db`.
