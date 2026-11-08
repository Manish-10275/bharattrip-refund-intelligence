# BharatTrip Refund Intelligence

An enterprise operational prototype and triage platform designed for B2B flight refund management, deterministic cross-departmental reconciliation, dispute tracking, and structured customer message intake.

[![Python Tests](https://img.shields.io/badge/tests-99%20passed-brightgreen.svg)](tests/)
[![Database](https://img.shields.io/badge/database-Neon%20PostgreSQL%20%7C%20SQLite-blue.svg)](db.py)
[![Deployment](https://img.shields.io/badge/deployment-Vercel%20Serverless-black.svg)](vercel.json)

---

## 1. Project Overview

BharatTrip Refund Intelligence is an operations platform built to identify and triage cross-departmental discrepancies in flight booking refund workflows. It bridges the gap between customer-facing support tickets, finance payout disbursements, and partner agency dispute escalations by providing:
- **Centralized Database Layer:** Native support for **Neon Serverless PostgreSQL** in production and **SQLite 3** for local development and offline unit testing.
- **Serverless Production Architecture:** Optimized for zero-configuration deployment on **Vercel** with CDN edge static delivery and pooled database connections.
- **Deterministic Reconciliation Engine:** Rule-based cross-departmental reconciliation evaluating 789 operational units across 7 mutually exclusive classifications.
- **Defensive Groq AI Intake:** Automated structured extraction of unstructured customer communications using Groq AI (`openai/gpt-oss-20b`) with strict JSON schema validation.
- **Human-in-the-Loop Review:** Operational oversight workflows with financial guardrails preventing automated leakage.
- **Read-Only Operational Ledgers:** High-contrast, light-theme enterprise interface featuring search, filtering, and pagination.

---

## 2. Business Problem

Online travel agencies (OTAs) and flight booking consolidators face operational frictions during refund processing:
- **Disparate Record Systems:** Support teams log customer cancellation claims with loose ticket references (`RF-1234`, `RF 1234`, `rf-1234`), while Finance tracks payouts against bank batch references, and external agencies log dispute escalations with missing or placeholder references (`-`, `no ref`).
- **Status Divergence:** Support may mark a ticket as "Refunded" based on airline communication, while Finance still shows the payout as "Pending" or "Failed" in the banking rail, leaving customers without funds.
- **Unstructured Inbound Channels:** Customers send unstructured refund inquiries across email, SMS, and WhatsApp in colloquial English or Romanized Hindi ("paisa wapas kab aayega", "flight cancelled refund not received"). Agents spend substantial time manually parsing PNRs, dates, and amounts.
- **Financial Leakage & Over-Disbursement:** Without automated reconciliation, double-payments, duplicate claims, and ambiguous sector disputes go unnoticed, resulting in unrecovered operational losses.

---

## 3. Proposed Operational Solution

BharatTrip Refund Intelligence provides an integrated four-layer operational system:

1. **Central Database Layer (`db.py`):**
   - Universal database adapter supporting **Neon PostgreSQL** via `psycopg 3` and **SQLite 3** via Python stdlib.
   - Transparent SQL parameter translation (`?` to `%s`) preserving single-quoted literals.
   - Dual-access case-insensitive `RowWrapper` (`row["col"]` and `row[0]`).
   - Request-scoped connection lifecycle management (`get_db()` / `close_db()`) preventing serverless pool exhaustion.

2. **Ingestion & Data Sanitization Layer:**
   - Cryptographic SHA-256 ingestion hashing to enforce idempotency.
   - Regex-based canonicalization of reference numbers (`RF-<digits>`) while preserving original raw strings verbatim.
   - Strict `NULL` discipline for missing values, eliminating SQL aggregation errors caused by sentinel strings (`'nan'`, `'None'`).

3. **Deterministic Reconciliation Engine:**
   - Evaluates normalized candidate pairings between Support and Finance into seven mutually exclusive, deterministic classifications.
   - Separates Support `Closed` / Finance `Refund Done` payout agreements (544 cases) from agreed non-payouts (27 `Not Eligible` / `Declined` cases).
   - Surfaces status mismatches, duplicate claims, and ambiguous sector/route conflicts directly into a human review queue.

4. **Defensive AI Intake & Human Review Subsystem:**
   - Employs Groq AI (`openai/gpt-oss-20b`) with strict JSON schema validation to convert unstructured inquiries into typed records.
   - Treats customer text strictly as untrusted input, neutralizing prompt injection attempts.
   - Enforces strict null handling (prohibiting fabricated extraction of unmentioned PNRs, dates, or amounts) and explicit currency discipline (never defaulting to INR without explicit symbols/codes).
   - Flags records for mandatory operator review whenever confidence is low or required data is absent.

---

## 4. System Architecture

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
             |  - Correlated Refund Records (/refund-records)
             |  - Operations Assistant (/assistant)      |
             |  - Session Auth & CSRF Protection         |
             |  - Edge Static Asset Serving (/static)    |
             +-------------------------------------------+
```

---

## 5. Technology Stack

- **Runtime:** Python 3.12+ (compatible with Python 3.10–3.14)
- **Web Framework:** Flask 3.1 (WSGI serverless and standard web application)
- **Primary Production Database:** **Neon PostgreSQL** via `psycopg 3` (`psycopg[binary]>=3.1.0`) with PgBouncer connection pooling and SSL enforcement (`sslmode=require`)
- **Local / Test Database:** SQLite 3 (standard library `sqlite3` driver)
- **Serverless Hosting:** **Vercel** (`vercel.json` native Python WSGI runtime with Edge static routing)
- **Fallback WSGI Server:** Gunicorn 21+ (`gunicorn --bind 0.0.0.0:$PORT app:app` via `Procfile` / `render.yaml`)
- **Session Security & Forms:** Flask-WTF 1.2+ (CSRF protection, secure HTTP-only cookies)
- **Data Ingestion & Cleansing:** Pandas 2.0+ & OpenPyXL 3.1+
- **Data Validation & Typing:** Pydantic 2.0+
- **AI Model & Inference:** Groq Python SDK (`openai/gpt-oss-20b`)
- **Testing Framework:** Python standard library `unittest` (99 automated tests, including live PostgreSQL integration verification)

---

## 6. Deterministic Reconciliation Methodology

The reconciliation engine operates as a deterministic finite-state machine across normalized references:

| Category | Definition under Implemented Rules | Operational Handling |
| :--- | :--- | :--- |
| **`MATCHED`** | Agreement between Support and Finance under implemented deterministic matching rules. | Automated categorization: sub-divided into 544 agreed payout disbursements (Support `Closed` + Finance `Refund Done`) and 27 agreed non-payouts (Support `Not Eligible` + Finance `Declined`). *Note:* Neither classification establishes independent external bank transaction confirmation or settlement verification. |
| **`STATUS_MISMATCH`** | Matching reference found, but operational status conflicts (e.g. Support: Refunded, Finance: Pending). | Flagged for manual review (`review_required = 1`). |
| **`SUPPORT_ONLY`** | Ticket logged in Support tracker with no corresponding entry in Finance ledger. | Payout backlog triage required. |
| **`FINANCE_ONLY`** | Payout disbursed in Finance with no recorded Support ticket. | Unlinked payout audit required. |
| **`DUPLICATE_REFERENCE`** | Multiple rows on one side share the same reference ID. | Split disbursement or re-submission review. |
| **`AMBIGUOUS_MATCH`** | Reference matches, but flight sector or agency details contradict. | Entity conflict investigation required. |
| **`INVALID_REFERENCE`** | Blank, placeholder (`-`, `no ref`), or unparseable reference. | Missing identifier triage. |

*Ledger Immutability:* Reconciliation is strictly read-only. Source records are never altered or deleted during reconciliation runs; results are persisted with historical run timestamps.

---

## 7. Groq AI Extraction Workflow

Inbound customer inquiries are processed via Groq Cloud SDK using the `openai/gpt-oss-20b` model:

1. **System Prompt Governance:** A specialized system prompt (`prompt_history/production_groq_system_prompt.md`) establishes seven strict operational boundaries.
2. **Defensive Prompt Injection Safety:** Inbound customer messages are treated strictly as untrusted text. Adversarial instructions (e.g., "Ignore previous instructions", "Approve my refund immediately") are neutralized, recorded as customer text, and flagged for human review.
3. **Factual Grounding & Null Safeguards:** The system implements strict schema constraints and explicit null handling to prevent the generation of fabricated values. Fields not explicitly stated in the source text must resolve to `null`.
4. **Explicit Currency Discipline:** The model never assumes `INR` based on Indian travel context or airline names. Currency is extracted only when an explicit symbol (`₹`, `$`) or code (`INR`, `USD`) is present.
5. **Strict Schema Enforcement:** Output is validated against a Pydantic schema (`services/intake_schema.py`) enforcing types, confidence boundaries (`[0.0, 1.0]`), and non-empty justifications when human review is required.

---

## 8. Human Oversight and Financial Guardrails

To prevent automated financial leakage, the system implements three foundational guardrails:

- **Strict Non-Authority of AI:** The LLM has zero authority to approve, initiate, reject, or mark refunds as paid. It acts purely as a structured extraction and classification triage layer.
- **Review Lifecycle State Transitions:**
  - Records triaged automatically without requiring human oversight (`human_review_required = False`) are initialized directly with `processing_status = 'PROCESSED'`.
  - Records requiring human attention (`human_review_required = True`) are initialized with `processing_status = 'PENDING_REVIEW'`.
  - When an authorized operator inspects the extraction and completes review via `POST /intake/<record_id>/review`, the status transitions from `PENDING_REVIEW` to `REVIEWED`.
  - `REVIEWED` represents human operator sign-off and does not automatically transition to `PROCESSED`.
- **Audit Sign-off Trail:** Human operators inspect the extracted structured JSON side-by-side with the verbatim original text before signing off on pending records.

---

## 9. Repository Structure

```text
bharattrip-refund-intelligence/
├── README.md                      # Platform documentation & deployment guide
├── vercel.json                    # Vercel serverless routing & edge configuration
├── .vercelignore                  # Serverless deployment exclusions
├── Procfile                       # Gunicorn fallback web process definition
├── render.yaml                    # Alternative Render Blueprint specification
├── .gitignore                     # Git exclusion rules
├── .env.example                   # Environment variable template (PostgreSQL & SQLite)
├── requirements.txt               # Python package dependencies (including psycopg[binary])
├── app.py                         # Flask application & route controllers
├── config.py                      # Application settings & security configuration
├── db.py                          # Central database abstraction layer
│
├── public/                        # Edge-cached static assets for Vercel
│   └── static/
│       └── style.css              # Direct CDN stylesheet delivery
│
├── scripts/                       # Operational management scripts
│   └── seed_database.py           # CLI database seeding & reconciliation runner
│
├── services/                      # Core business logic services
│   ├── __init__.py
│   ├── data_service.py            # Workbook ingestion & reference normalization
│   ├── groq_service.py            # Groq AI extraction & prompt injection defense
│   ├── intake_schema.py           # Pydantic extraction schema & JSON constraints
│   ├── intake_service.py          # Intake database persistence & review queues
│   └── reconciliation_service.py  # Deterministic 7-state reconciliation engine
│
├── templates/                     # Jinja2 presentation templates
│   ├── base.html                  # Shell layout & authenticated navigation
│   ├── dashboard.html             # Operations dashboard & KPI counters
│   ├── escalations.html           # Dispute escalation records ledger
│   ├── intake_detail.html         # AI extraction inspector & review interface
│   ├── intake_form.html           # Inbound message submission form
│   ├── intake_review.html         # Human triage review queue
│   ├── login.html                 # Operator sign-in view
│   ├── operations_assistant.html  # Operations quick navigation assistant
│   ├── reconciliation.html        # Reconciliation ledger & run trigger
│   └── refund_records.html        # Correlated refund candidates ledger
│
├── static/
│   └── style.css                  # Light-theme enterprise CSS
│
├── tests/                         # Automated test suite (99 tests)
│   ├── __init__.py
│   ├── test_data_service.py       # Ingestion & normalization tests (7 tests)
│   ├── test_groq_service.py       # Groq extraction & safety tests (15 tests)
│   ├── test_intake_routes.py      # Route authentication & CSRF tests (23 tests)
│   ├── test_navigation_and_pages.py # Navigation & read-only tests (19 tests)
│   ├── test_postgresql_layer.py   # PostgreSQL live integration tests (10 tests)
│   ├── test_reconciliation_service.py # Reconciliation state tests (10 tests)
│   └── test_security_and_metrics.py # Security & metric isolation tests (15 tests)
│
├── docs/                          # Architecture & methodology documentation
│   ├── architecture.md            # System architecture specification
│   ├── ai_prompt_log.md           # Production prompt versioning & boundaries
│   └── data_quality_methodology.md # Cleansing & normalization methodology
│
└── prompt_history/                # Verified development prompt records
    ├── antigravity_development_prompt_history.md # Genuine task directives
    └── production_groq_system_prompt.md         # Verbatim Groq system prompt
```

---

## 10. Local Installation & Quick Start

### Step 1: Clone Repository & Create Virtual Environment

```bash
git clone https://github.com/Manish-10275/bharattrip-refund-intelligence.git
cd bharattrip-refund-intelligence

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows PowerShell:
.\venv\Scripts\Activate.ps1
# Linux / macOS:
source venv/bin/activate
```

### Step 2: Install Dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Step 3: Configure Local Environment (`.env`)

Copy the template to create `.env`:

```bash
# Windows:
copy .env.example .env
# Linux / macOS:
cp .env.example .env
```

Generate a secure cryptographic `SECRET_KEY` and an operator password hash:

```bash
# 1. Generate SECRET_KEY
python -c "import secrets; print(secrets.token_hex(32))"

# 2. Generate OPERATOR_PASSWORD_HASH
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('YourSecurePasswordHere'))"
```

Configure your `.env` file:
```dotenv
# To use local SQLite (default for development):
DATABASE=bharattrip.db
DATABASE_URL=

# To use local or remote PostgreSQL:
# DATABASE_URL=postgresql://postgres:postgres@localhost:5432/bharattrip_dev

SECRET_KEY=your_generated_64_character_hex_secret_here
OPERATOR_USERNAME=operator
OPERATOR_PASSWORD_HASH=your_generated_werkzeug_scrypt_password_hash_here
TESTING=False
SESSION_COOKIE_SECURE=False
WTF_CSRF_ENABLED=True
GROQ_API_KEY=your_groq_api_key_here
GROQ_MODEL=openai/gpt-oss-20b
GROQ_REQUEST_TIMEOUT=30.0
```

---

## 11. Database Seeding & Reconciliation Runner

BharatTrip includes a unified seeding utility (`scripts/seed_database.py`) that initializes schemas, ingests the operational workbook, and executes deterministic reconciliation in a single command across either database engine:

```bash
# Seed local SQLite database (uses DATABASE or .env):
python scripts/seed_database.py

# Seed explicit PostgreSQL database (e.g. Neon):
python scripts/seed_database.py --database-url "postgresql://user:pass@ep-xyz-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"
```

### Expected Seeding Output:
```text
==================================================
BharatTrip Refund Intelligence - Database Seeding
Target Backend : PostgreSQL (Neon)
Workbook File  : BharatTrip_Refund_Data.xlsx
==================================================

[1/3] Initializing database schemas...
  [OK] All 8 tables and indexes verified successfully.

[2/3] Ingesting operational dataset from Excel...
  Import Status : SUCCESS
  Support Rows  : 755
  Finance Rows  : 689
  Escalations   : 155

[3/3] Running automated 3-way reconciliation...
  Reconciliation Status  : SUCCESS
  Total Evaluation Units : 789
  Deterministic Matched  : 571
  Status Mismatches      : 65
  Review Required        : 218

==================================================
Database seeding completed successfully!
==================================================
```

---

## 12. Local Application Startup

Start the local Flask development server:

```bash
python app.py
```

Open your browser and navigate to:
```
http://127.0.0.1:5000/login
```
Sign in using `OPERATOR_USERNAME` and the plaintext password configured in `.env`.

### Core Application Routes:
- `/` — Operations dashboard, pending triage KPIs, and records ledger overview.
- `/reconciliation` — Deterministic reconciliation ledger and candidate review queue.
- `/reconciliation/run` — On-demand deterministic reconciliation trigger (`POST`, CSRF-protected).
- `/escalations` — Partner agency dispute escalations with resolution filters, search, and pagination.
- `/refund-records` — Correlated refund candidates ledger correlating Support and Finance statuses.
- `/assistant` — Operations quick navigation assistant with direct workflow shortcuts.
- `/intake` — Inbound customer message intake form (`GET`) and analysis submission (`POST`).
- `/intake/<int:record_id>` — Structured extraction inspector displaying extracted fields and confidence.
- `/intake/<int:record_id>/review` — Operator review sign-off action (`POST`, CSRF-protected).
- `/intake/review` — Human oversight queue for triaging and processing pending AI intake records.
- `/login` — Operator authentication interface (`GET`/`POST`).
- `/logout` — Secure operator session termination (`POST`, CSRF-protected).
- `/health` — Public health check endpoint returning runtime backend status (`python-flask-postgresql` or `python-flask-sqlite`).

---

## 13. Running Automated Tests

The test suite contains **99 automated tests** across 7 test modules:

```bash
python -m unittest discover tests -v
```

### Test Suite Structure:
- `tests/test_postgresql_layer.py` (10 tests) — Live PostgreSQL verification: identity columns, query translation (`?` -> `%s`), `INSERT ... RETURNING id`, dual-access `RowWrapper`, transaction rollback/commit, foreign key cascades, data service, and reconciliation.
- `tests/test_data_service.py` (7 tests) — Ingestion provenance, reference regex normalization, idempotency hashing.
- `tests/test_groq_service.py` (15 tests) — Strict schema constraints, prompt injection neutralization, error sanitization, currency discipline.
- `tests/test_intake_routes.py` (23 tests) — Session authentication, login/logout redirects, CSRF enforcement, review queue transitions.
- `tests/test_navigation_and_pages.py` (19 tests) — Navigation routing, filtering, search, pagination, read-only immutability.
- `tests/test_reconciliation_service.py` (10 tests) — Deterministic classification, status mismatch detection, duplicate reference handling.
- `tests/test_security_and_metrics.py` (15 tests) — Production `SECRET_KEY`, `OPERATOR_USERNAME`, and `OPERATOR_PASSWORD_HASH` enforcement, `SESSION_COOKIE_SECURE` production defaults, persistent disk parent directory creation, and metric independence.

---

## 14. Primary Production Deployment: Vercel + Neon PostgreSQL

The recommended production deployment utilizes **Vercel** for serverless application hosting and **Neon** for managed serverless PostgreSQL.

### Step 1: Provision Neon PostgreSQL Database

1. Sign up or log in to [Neon Console](https://console.neon.tech/).
2. Create a new project (e.g. `bharattrip-production`).
3. Under **Dashboard**, locate your connection details:
   - Select **Connection pooling** (PgBouncer). *Connection pooling is critical for serverless environments to prevent connection starvation during sudden traffic spikes.*
   - Copy the pooled connection string. It will look like:
     ```
     postgresql://user:password@ep-cool-xyz-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require
     ```
   - Ensure `sslmode=require` is present (the database layer enforces SSL automatically for Neon endpoints).

### Step 2: Seed the Neon Database

Run the seeding script from your development terminal or CI pipeline to initialize schemas, ingest historical data, and run baseline reconciliation:

```bash
python scripts/seed_database.py --database-url "postgresql://user:password@ep-cool-xyz-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"
```

### Step 3: Deploy to Vercel

1. Push your repository to GitHub.
2. In the [Vercel Dashboard](https://vercel.com/dashboard), click **Add New Project** and select `bharattrip-refund-intelligence`.
3. Vercel automatically detects the Python project and uses `vercel.json`.
4. Under **Settings -> Environment Variables**, configure the production environment variables:

| Variable Name | Value / Description | Required |
| :--- | :--- | :--- |
| `DATABASE_URL` | Neon pooled connection string (`sslmode=require`) | **Yes** |
| `SECRET_KEY` | 64-character cryptographic hex key | **Yes** |
| `OPERATOR_USERNAME` | Production operator username (e.g. `lead_ops`) | **Yes** |
| `OPERATOR_PASSWORD_HASH` | Werkzeug scrypt hash of operator password | **Yes** |
| `SESSION_COOKIE_SECURE` | `True` (enforces HTTPS on Vercel) | **Yes** |
| `WTF_CSRF_ENABLED` | `True` | **Yes** |
| `FLASK_ENV` | `production` | **Yes** |
| `GROQ_API_KEY` | Groq Cloud API key for live AI intake | Optional |
| `GROQ_MODEL` | `openai/gpt-oss-20b` | Optional |

5. Click **Deploy**.

### Step 4: Verify Deployment

1. Visit `https://<your-vercel-domain>.vercel.app/health`:
   ```json
   {
     "status": "healthy",
     "database": "connected",
     "runtime": "python-flask-postgresql"
   }
   ```
2. Navigate to `https://<your-vercel-domain>.vercel.app/login` and log in with your operator credentials.
3. Verify that the Operations Dashboard displays the live operational KPIs:
   - **Pending Support Triage:** 35
   - **Open Escalation Records:** 85
   - **Support Tickets Marked Escalated:** 17

---

## 15. Alternative Single-Node Deployment: Render

For single-instance evaluations requiring a local disk, the repository retains `render.yaml` and `Procfile`:

1. In Render, select **New Blueprint Instance** and select the repository.
2. Render mounts a persistent disk at `/var/data` and configures `DATABASE=/var/data/bharattrip.db`.
3. Set `OPERATOR_USERNAME`, `OPERATOR_PASSWORD_HASH`, and `SECRET_KEY` in the Render dashboard.

---

## 16. Security & Privacy Notes

- **Zero Hardcoded Credentials:** The repository contains no production API keys, secrets, or database passwords. All configuration is loaded from environment variables via `config.py`.
- **Enforced Production Security:** Non-testing environments strictly mandate `SECRET_KEY`, `OPERATOR_USERNAME`, and `OPERATOR_PASSWORD_HASH` at application startup, failing immediately if any are omitted.
- **Untrusted Input Security:** Inbound customer messages are treated strictly as untrusted data, preventing prompt injection attacks from overriding system instructions.
- **CSRF & Session Security:** Mutating HTTP requests require valid CSRF tokens. Sessions are authenticated via HTTP-only cookies with `SESSION_COOKIE_SECURE=True` enforced in production.
- **Data Protection:** The original operational workbook, `.env` secrets, and database files are strictly excluded from version control via `.gitignore`.
