# BharatTrip Refund Intelligence — Architecture Specification

This document details the software architecture, core services, database schemas, and integration patterns for the BharatTrip Refund Intelligence operational prototype.

---

## 1. System Overview & Component Diagram

The platform is designed as an operational prototype and triage system built on Flask 3.1, SQLite 3, Pydantic 2, and Groq SDK.

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

## 3. Database Schema

The SQLite schema consists of seven relational tables:

1. **`import_history`**: Tracks workbook SHA-256 hashes, file names, row counts, and ingestion timestamps.
2. **`support_records`**: Ingested Support tickets with normalized `norm_ticket_id`, route, claimed amount, request date, status, agent, and channel.
3. **`finance_records`**: Ingested Finance disbursements with normalized `norm_ref_no`, amount paid, deduction, processed date, and payout status.
4. **`escalation_records`**: Ingested agency dispute escalations with related ticket reference, raised date, team, channel, complaint, status, and resolution date.
5. **`reconciliation_runs`**: Header table storing run timestamp, total candidates, classification counts, and review metrics.
6. **`reconciliation_results`**: Individual candidate evaluation units linked to run ID, support record ID, finance record ID, classification tag, and reason.
7. **`intake_records`**: Inbound AI-triaged messages with raw customer text, validated JSON, extracted entities, confidence, review flags, and audit sign-offs.

---

## 4. Security & Compliance Architecture

- **Cryptographic Secret Enforcement:** `config.py` enforces a non-empty `SECRET_KEY` in non-testing environments. Application startup fails with a `RuntimeError` if missing.
- **Session Authentication:** All operational endpoints require an authenticated operator session verified via `@login_required`.
- **CSRF Defense:** All mutating requests (`POST /login`, `POST /logout`, `POST /intake`, `POST /reconciliation/run`) require valid CSRF tokens via Flask-WTF.
- **Cookie Security:** `SESSION_COOKIE_HTTPONLY = True`, `SESSION_COOKIE_SAMESITE = 'Lax'`, and production-default `SESSION_COOKIE_SECURE = True`.
- **Operator Authentication Enforcement:** `OPERATOR_USERNAME` and `OPERATOR_PASSWORD_HASH` are strictly enforced at startup in non-testing environments, failing with `RuntimeError` if missing.
- **Offline / Mocked Test Isolation:** The automated test suite executes fully offline with mocked Groq fixtures and isolated test secrets without external network requirements.

---

## 5. Production Deployment Architecture (Render)

The application is structured for cloud deployment on platforms like Render using Gunicorn and persistent volume storage:

- **WSGI Application Server:** Gunicorn 21+ (`gunicorn --bind 0.0.0.0:$PORT app:app`) with dynamic `$PORT` binding.
- **Persistent Storage (Render Disks):** SQLite is mounted on a persistent block storage disk at `/var/data/bharattrip.db` (`DATABASE=/var/data/bharattrip.db`), ensuring imported records, reconciliation history, and AI intake records persist across deploys and container recycles.
- **Automatic Directory Provisioning:** Application and service connection handlers automatically ensure parent directories (`os.makedirs`) exist before opening database connections.
- **Automatic Startup Migration:** `app.py` automatically initializes database tables on startup within the persistent disk location.
- **Declarative Blueprint (`render.yaml`):** Infrastructure-as-code configuration specifying Python runtime, build command, Gunicorn start command, health check route (`/health`), persistent disk mounts, and environment variables.
