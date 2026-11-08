# BharatTrip Refund Intelligence

An operational prototype and triage system designed for B2B flight refund management, deterministic cross-departmental reconciliation, dispute tracking, and structured customer message intake.

---

## 1. Project Overview

BharatTrip Refund Intelligence is an operations prototype built to identify and triage cross-departmental discrepancies in flight booking refund workflows. It bridges the gap between customer-facing support tickets, finance payout disbursements, and partner agency dispute escalations by providing:
- Deterministic, rule-based cross-departmental reconciliation.
- Automated extraction of unstructured customer communications using Groq AI with strict JSON schema validation.
- Human-in-the-loop review workflows with financial guardrails.
- Read-only operational ledgers with search, filtering, and pagination.

---

## 2. Business Problem

Online travel agencies (OTAs) and flight booking consolidators face operational frictions during refund processing:
- **Disparate Record Systems:** Support teams log customer cancellation claims with loose ticket references (`RF-1234`, `RF 1234`, `rf-1234`), while Finance tracks payouts against bank batch references, and external agencies log dispute escalations with missing or placeholder references (`-`, `no ref`).
- **Status Divergence:** Support may mark a ticket as "Refunded" based on airline communication, while Finance still shows the payout as "Pending" or "Failed" in the banking rail, leaving customers without funds.
- **Unstructured Inbound Channels:** Customers send unstructured refund inquiries across email, SMS, and WhatsApp in colloquial English or Romanized Hindi ("paisa wapas kab aayega", "flight cancelled refund not received"). Agents spend substantial time manually parsing PNRs, dates, and amounts.
- **Financial Leakage & Over-Disbursement:** Without automated reconciliation, double-payments, duplicate claims, and ambiguous sector disputes go unnoticed, resulting in unrecovered operational losses.

---

## 3. Proposed Operational Solution

BharatTrip Refund Intelligence provides an integrated three-layer operational system:

1. **Ingestion & Data Sanitization Layer:**
   - Cryptographic SHA-256 ingestion hashing to enforce idempotency.
   - Regex-based canonicalization of reference numbers (`RF-<digits>`) while preserving original raw strings verbatim.
   - Strict `NULL` discipline for missing values, eliminating SQL aggregation errors caused by sentinel strings (`'nan'`, `'None'`).

2. **Deterministic Reconciliation Engine:**
   - Evaluates normalized candidate pairings between Support and Finance into seven mutually exclusive, deterministic classifications.
   - Separates Support `Closed` / Finance `Refund Done` payout agreements (544 cases) from agreed non-payouts (27 `Not Eligible` / `Declined` cases).
   - Surfaces status mismatches, duplicate claims, and ambiguous sector/route conflicts directly into a human review queue.

3. **Defensive AI Intake & Human Review Subsystem:**
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
|               SQLite Core Database (refund_intelligence.db)       |
|  - import_history       - reconciliation_runs   - intake_records  |
|  - support_records      - reconciliation_results                  |
|  - finance_records      - escalation_records                      |
+-------------------------------------------------------------------+
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
             |         Web Operations Interface          |
             |  - Operations Dashboard (/)               |
             |  - Dispute Escalations (/escalations)     |
             |  - Reconciliation Ledger (/reconciliation)|
             |  - Correlated Refund Records (/refund-records)
             |  - Operations Assistant (/assistant)      |
             |  - Session Auth & CSRF Protection         |
             +-------------------------------------------+
```

---

## 5. Technology Stack

- **Runtime:** Python 3.14 (tested runtime; compatible with Python 3.10–3.14 supported by package dependencies)
- **Web Framework:** Flask 3.1 (WSGI application)
- **WSGI Production Server:** Gunicorn 21+ (multi-worker UNIX HTTP server)
- **Session Security & Forms:** Flask-WTF 1.2+ (CSRF protection, secure cookie handling)
- **Relational Storage:** SQLite 3 (Python native `sqlite3` driver with configurable persistent disk path)
- **Data Ingestion & Cleansing:** Pandas 2.0+ & OpenPyXL 3.1+
- **Data Validation & Typing:** Pydantic 2.0+
- **AI Model & Inference:** Groq Python SDK (`openai/gpt-oss-20b`)
- **Testing Framework:** Python standard library `unittest` (89 automated tests)

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
├── README.md                      # Platform documentation & operational guide
├── render.yaml                    # Render Blueprint deployment specification
├── Procfile                       # Gunicorn web process definition
├── .gitignore                     # Git exclusion rules
├── .env.example                   # Environment variable template
├── requirements.txt               # Python package dependencies
├── app.py                         # Flask application & route controllers
├── config.py                      # Application settings & security configuration
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
├── tests/                         # Automated unit & integration tests (89 tests)
│   ├── __init__.py
│   ├── test_data_service.py       # Ingestion & normalization tests (7 tests)
│   ├── test_groq_service.py       # Groq extraction & safety tests (15 tests)
│   ├── test_intake_routes.py      # Route authentication & CSRF tests (23 tests)
│   ├── test_navigation_and_pages.py # Navigation & read-only tests (19 tests)
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

## 10. Installation & Quick Start

### Step 1: Clone the Repository & Set Up Virtual Environment

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

### Step 3: Configure Environment Variables (`.env`)

Copy `.env.example` to create your local `.env` configuration file:

```bash
# Windows:
copy .env.example .env
# Linux / macOS:
cp .env.example .env
```

Generate a secure cryptographic `SECRET_KEY` and an operator password hash:

```bash
# 1. Generate SECRET_KEY (paste output into .env)
python -c "import secrets; print(secrets.token_hex(32))"

# 2. Generate OPERATOR_PASSWORD_HASH (paste output into .env)
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('YourSecurePasswordHere'))"
```

Configure your `.env` file:
```dotenv
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

> [!NOTE]
> `GROQ_API_KEY` is optional for running automated tests. If omitted, the test suite executes using offline mock fixtures. An active Groq API key is only required for live intake extraction.

---

## 11. Database Initialisation & Dataset Import

### Step 4: Initialize the SQLite Schema

```bash
python -c "import app; app.app.app_context().push(); app.init_db(); print('Database initialized successfully.')"
```

### Step 5: Ingest Operational Workbook

> [!IMPORTANT]
> **Dataset Availability Notice:**  
> The original operational dataset (`BharatTrip_Refund_Data.xlsx`) is an internal business document containing operational flight booking records and is **not included in this public repository** to protect operational privacy.  
> Users wishing to run the full historical analysis must provide an authorized copy of the Excel workbook placed in the project root directory.

Once the authorized workbook (`BharatTrip_Refund_Data.xlsx`) is placed in the project root:

```bash
python -c "from services.data_service import DataService; result = DataService().import_workbook('BharatTrip_Refund_Data.xlsx'); print(result)"
```
*Expected Ingestion Outcome:* 755 Support records, 689 Finance records, and 155 Escalation records imported under `import_id = 1`.

### Step 6: Execute Deterministic Reconciliation

```bash
python -c "from services.reconciliation_service import ReconciliationService; result = ReconciliationService().run_reconciliation(); print(result)"
```
*Expected Reconciliation Outcome:* Exactly 789 evaluation units processed (571 MATCHED, 65 STATUS_MISMATCH, 66 SUPPORT_ONLY, 16 FINANCE_ONLY, 44 DUPLICATE_REFERENCE, 27 AMBIGUOUS_MATCH).

---

## 12. Application Startup

Start the local Flask development server:

```bash
python app.py
```

Open your browser and navigate to:
```
http://127.0.0.1:5000/login
```
Sign in with the `OPERATOR_USERNAME` and the plaintext password configured during Step 3.

### Documented Flask Routes:
- `/` — Main operations dashboard, pending triage KPIs, and records ledger overview.
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
- `/health` — Public health check endpoint returning JSON runtime status.

---

## 13. Running Automated Tests

The automated test suite contains 89 tests across 6 modules, requiring zero external network connectivity:

```bash
python -m unittest discover tests -v
```

### Test Suite Structure:
- `tests/test_data_service.py` (7 tests) — Ingestion provenance, reference regex normalization, idempotency hashing.
- `tests/test_groq_service.py` (15 tests) — Strict schema constraints, prompt injection neutralization, error sanitization, currency discipline.
- `tests/test_intake_routes.py` (23 tests) — Session authentication, login/logout redirects, CSRF enforcement, review queue transitions.
- `tests/test_navigation_and_pages.py` (19 tests) — Navigation routing, filtering, search, pagination, read-only immutability.
- `tests/test_reconciliation_service.py` (10 tests) — Deterministic classification, status mismatch detection, duplicate reference handling.
- `tests/test_security_and_metrics.py` (15 tests) — Production `SECRET_KEY`, `OPERATOR_USERNAME`, and `OPERATOR_PASSWORD_HASH` enforcement, `SESSION_COOKIE_SECURE` production defaults, persistent disk parent directory creation, and metric independence.

---

## 14. Production Deployment on Render

The repository includes a ready-to-use Render Blueprint (`render.yaml`) and Gunicorn WSGI configuration (`Procfile`).

### 14.1 Render Blueprint Deployment (Recommended)

1. Connect your GitHub repository to [Render](https://render.com/).
2. Select **Blueprints** -> **New Blueprint Instance**.
3. Select `bharattrip-refund-intelligence`.
4. Render automatically parses `render.yaml` and configures:
   - **Service Name:** `bharattrip-refund-intelligence`
   - **Environment:** `python`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn --bind 0.0.0.0:$PORT app:app`
   - **Health Check Path:** `/health`
   - **Persistent Disk:** Mounted at `/var/data` (1 GB)
5. Configure the required secret environment variables in the Render Dashboard:
   - `OPERATOR_USERNAME`: Your designated operator login username (e.g. `admin_ops`).
   - `OPERATOR_PASSWORD_HASH`: Werkzeug scrypt hash generated with:
     ```bash
     python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('YourSecurePasswordHere'))"
     ```
   - `GROQ_API_KEY`: (Optional) Your Groq API key for live AI customer inquiry intake.
6. Deploy the service. Render will automatically provision a random `SECRET_KEY`, mount the persistent disk at `/var/data`, and initialize the SQLite database at `/var/data/bharattrip.db`.

### 14.2 Manual Render Web Service Setup

If configuring manually without Blueprints:
| Configuration Field | Recommended Production Value |
| :--- | :--- |
| **Runtime** | `Python` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn --bind 0.0.0.0:$PORT app:app` |
| **Health Check Path** | `/health` |
| **Disk Name** | `bharattrip-data` |
| **Mount Path** | `/var/data` |
| **Disk Size** | `1 GB` |

#### Required Environment Variables:
```dotenv
DATABASE=/var/data/bharattrip.db
SECRET_KEY=<generate-via-python-secrets>
OPERATOR_USERNAME=<your-operator-username>
OPERATOR_PASSWORD_HASH=<generate-via-werkzeug-hash>
SESSION_COOKIE_SECURE=true
WTF_CSRF_ENABLED=true
GROQ_API_KEY=<your-groq-api-key>
GROQ_MODEL=openai/gpt-oss-20b
GROQ_REQUEST_TIMEOUT=30.0
```

### 14.3 Persistent Disk & Initial Data Loading

Because SQLite data is stored on the persistent disk mounted at `/var/data`, tables are initialized automatically upon first application start. To import operational historical data on Render:
1. Open the Render Web Shell for the deployed service.
2. If the operational workbook is uploaded or transferred:
   ```bash
   python -c "from services.data_service import DataService; print(DataService().import_workbook('BharatTrip_Refund_Data.xlsx'))"
   python -c "from services.reconciliation_service import ReconciliationService; print(ReconciliationService().run_reconciliation())"
   ```
3. All ingested records and reconciliation runs remain persisted on `/var/data/bharattrip.db` across future code updates, container deploys, and restarts.

---

## 15. Known Prototype Limitations

1. **Local SQLite Architecture:** This prototype uses a local SQLite database suitable for single-node evaluation and testing. Multi-node horizontal scaling requires migrating to a hosted relational database (e.g. PostgreSQL) with connection pooling.
2. **Synchronous LLM Inference:** Inbound AI intake runs synchronously within the HTTP request cycle with a 30-second timeout. High-throughput environments require an asynchronous background task worker (e.g. Celery / Redis Queue).
3. **Absence of Banking Rail Integration:** The platform reconciles recorded ledger entries; it does not directly dispatch automated payment orders to banking APIs (e.g. UPI, NEFT, IMPS rails).

---

## 16. Security & Privacy Notes

- **Zero Hardcoded Credentials:** The repository contains no production API keys, secrets, or database passwords. All configuration is loaded from environment variables via `config.py`.
- **Enforced Production Security:** Non-testing environments strictly mandate `SECRET_KEY`, `OPERATOR_USERNAME`, and `OPERATOR_PASSWORD_HASH` at application startup, failing immediately if any are omitted.
- **Untrusted Input Security:** Inbound customer messages are treated strictly as untrusted data, preventing prompt injection attacks from overriding system instructions.
- **CSRF & Session Security:** Mutating HTTP requests require valid CSRF tokens. Sessions are authenticated via HTTP-only cookies with `SESSION_COOKIE_SECURE=True` enforced in production.
- **Data Protection:** The original operational workbook, `.env` secrets, and database files are strictly excluded from version control via `.gitignore`.
