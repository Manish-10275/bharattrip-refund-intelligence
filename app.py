import math
import os
from functools import wraps
from urllib.parse import urlparse
from flask import Flask, render_template, request, redirect, url_for, jsonify, g, session
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import check_password_hash
from config import Config
from db import (
    get_db as db_get_db,
    close_db as db_close_db,
    init_all_schemas,
    DatabaseOperationalError,
    get_database_backend,
)
from services.data_service import DataService
from services.reconciliation_service import ReconciliationService, ReconciliationClassification
from services.intake_service import IntakeService
from services.groq_service import (
    groq_intake_service,
    analyze_refund_message,
    PROMPT_VERSION,
    MissingAPIKeyError,
    GroqAPIError,
    InvalidResponseError,
    ExtractionValidationError,
)

app = Flask(__name__)
app.config.from_object(Config)
app.config["SECRET_KEY"] = Config.load_secret_key()

# Enable CSRF protection for all state-changing requests
csrf = CSRFProtect(app)

# Initialize data, reconciliation, and intake services
data_service = DataService(db_path=app.config["DATABASE"])
reconciliation_service = ReconciliationService(db_path=app.config["DATABASE"])
intake_service = IntakeService(db_path=app.config["DATABASE"])


# ==============================================================================
# Security & Authentication Utilities
# ==============================================================================

def is_safe_url(target: str) -> bool:
    """Validate that redirection target is relative and safe from open redirect vulnerabilities."""
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(target)
    return (test_url.scheme in ("http", "https") and ref_url.netloc == test_url.netloc) or (not test_url.netloc and target.startswith("/"))


def login_required(f):
    """Decorator to require authenticated operator session for sensitive operational routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "operator_user" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated_function


# ==============================================================================
# Database Layer (PostgreSQL / Neon in production, SQLite in local dev/testing)
# ==============================================================================

def get_db():
    """Get or create an application-context DatabaseConnection."""
    # Sync service database paths/configs with active app configuration if changed
    data_service.db_path = app.config.get("DATABASE", "bharattrip.db")
    reconciliation_service.db_path = app.config.get("DATABASE", "bharattrip.db")
    intake_service.db_path = app.config.get("DATABASE", "bharattrip.db")
    return db_get_db()


@app.teardown_appcontext
def close_db(error=None):
    """Close the database connection at the end of the request."""
    db_close_db(error)


def init_db():
    """Initialize database tables for application and data ingestion."""
    init_all_schemas(get_db())


# Safely attempt database initialization at startup without failing if DB is not yet available
try:
    with app.app_context():
        init_db()
except Exception as _e:
    app.logger.warning(f"Startup database schema initialization deferred: {_e}")


# ==============================================================================
# Authentication Routes
# ==============================================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    """Authenticate operator session using Config credentials."""
    if "operator_user" in session:
        return redirect(url_for("dashboard"))

    error_message = None
    next_url = request.args.get("next") or request.form.get("next") or url_for("dashboard")

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        configured_user = app.config.get("OPERATOR_USERNAME", "operator")
        configured_hash = app.config.get("OPERATOR_PASSWORD_HASH", "")

        if username == configured_user and configured_hash and check_password_hash(configured_hash, password):
            session.clear()
            session["operator_user"] = username
            if not is_safe_url(next_url):
                next_url = url_for("dashboard")
            return redirect(next_url)
        else:
            error_message = "Invalid operator username or password."

    return render_template(
        "login.html",
        title="Operator Sign In",
        error_message=error_message,
        next_url=next_url
    )

app.add_url_rule("/login", endpoint="login_action", view_func=login, methods=["POST"])


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    """Terminate operator session and redirect to login."""
    session.clear()
    return redirect(url_for("login"))

app.add_url_rule("/logout", endpoint="logout_action", view_func=logout, methods=["POST"])


# ==============================================================================
# Application Routes
# ==============================================================================

@app.route("/")
@login_required
def dashboard():
    """Dashboard view displaying platform overview, KPIs, and recent records."""
    init_db()
    db = get_db()
    cursor = db.cursor()

    intake_stats = intake_service.get_stats(db)

    has_support = False
    support_total = 0
    try:
        cursor.execute("SELECT COUNT(*) FROM support_records")
        support_total = cursor.fetchone()[0]
        has_support = support_total > 0
    except DatabaseOperationalError:
        has_support = False

    if has_support:
        cursor.execute("""
            SELECT 
                ticket_id AS booking_reference,
                COALESCE(handled_by, 'Unassigned') AS customer_name,
                COALESCE(agent, 'Direct') AS travel_agency,
                route,
                refund_amount,
                status,
                request_date AS created_at
            FROM support_records
            ORDER BY id DESC
            LIMIT 10
        """)
        records = cursor.fetchall()

        # Pending Support Triage strictly counts active 'Working' status tickets
        # (Excludes terminal 'Not Eligible' rejections and separately counted 'Escalated' cases)
        cursor.execute("SELECT COUNT(*) FROM support_records WHERE status = 'Working'")
        pending_support_triage = cursor.fetchone()[0]

        # Use reconciliation summary if available
        rec_summary = reconciliation_service.get_latest_run()
        if rec_summary:
            reconciled_display = f"{rec_summary['matched_count']:,}"
            reconciled_hint = f"Verified matches ({rec_summary['status_mismatch_count']} mismatches)"
        else:
            cursor.execute("SELECT COUNT(*) FROM finance_records WHERE payout_status = 'Refund Done'")
            reconciled_display = f"{cursor.fetchone()[0]:,}"
            reconciled_hint = "Completed payout records"

        # Open Escalation Records metric (from unresolved escalation records, count = 85)
        cursor.execute("SELECT COUNT(*) FROM escalation_records WHERE resolved_on IS NULL")
        open_escalation_records = cursor.fetchone()[0]

        # Support Tickets Marked Escalated metric (support tickets with Escalated status, count = 17)
        cursor.execute("SELECT COUNT(*) FROM support_records WHERE status = 'Escalated'")
        support_tickets_marked_escalated = cursor.fetchone()[0]

        cursor.execute("SELECT SUM(refund_amount) FROM support_records WHERE refund_amount IS NOT NULL")
        total_vol = cursor.fetchone()[0] or 0.0

        kpis = [
            {
                "id": "pending_support_triage",
                "title": "Pending Support Triage",
                "value": f"{pending_support_triage:,}",
                "hint": "Support tickets in working status",
                "icon": "clock"
            },
            {
                "id": "open_escalation_records",
                "title": "Open Escalation Records",
                "value": f"{open_escalation_records:,}",
                "hint": "Escalations tracker (resolved_on is null)",
                "icon": "alert-triangle"
            },
            {
                "id": "support_tickets_marked_escalated",
                "title": "Support Tickets Marked Escalated",
                "value": f"{support_tickets_marked_escalated:,}",
                "hint": "Support tickets with Escalated status",
                "icon": "alert-circle"
            },
            {
                "id": "pending_ai_intake",
                "title": "Pending AI Intake",
                "value": f"{intake_stats['pending_review']:,}",
                "hint": "Customer messages awaiting review",
                "icon": "inbox"
            },
            {
                "id": "auto_reconciled",
                "title": "Auto-Reconciled",
                "value": reconciled_display,
                "hint": reconciled_hint,
                "icon": "check-circle"
            },
            {
                "id": "total_claims",
                "title": "Total Intake Volume",
                "value": f"₹{total_vol:,.2f}",
                "hint": f"Across {support_total} total claims",
                "icon": "file-text"
            }
        ]
        total_records_display = f"{support_total:,} Total Records"
    else:
        cursor.execute("SELECT * FROM refund_records ORDER BY created_at DESC LIMIT 10")
        records = cursor.fetchall()
        kpis = [
            {"id": "pending_support_triage", "title": "Pending Support Triage", "value": "0", "hint": "Support tickets in working status", "icon": "clock"},
            {"id": "open_escalation_records", "title": "Open Escalation Records", "value": "0", "hint": "Escalations tracker (resolved_on is null)", "icon": "alert-triangle"},
            {"id": "support_tickets_marked_escalated", "title": "Support Tickets Marked Escalated", "value": "0", "hint": "Support tickets with Escalated status", "icon": "alert-circle"},
            {"id": "pending_ai_intake", "title": "Pending AI Intake", "value": f"{intake_stats['pending_review']:,}", "hint": "Customer messages awaiting review", "icon": "inbox"},
            {"id": "auto_reconciled", "title": "Auto-Reconciled", "value": "0", "hint": "Resolved without manual intervention", "icon": "check-circle"},
            {"id": "total_claims", "title": "Total Intake Volume", "value": "₹0.00", "hint": "Processed this period", "icon": "file-text"}
        ]
        total_records_display = "0 Total Records"

    return render_template(
        "dashboard.html",
        title="Operations Dashboard",
        active_page="dashboard",
        kpis=kpis,
        records=records,
        total_records_display=total_records_display
    )


@app.route("/reconciliation")
@login_required
def reconciliation_view():
    """Refund Reconciliation view displaying reconciliation metrics, queue, and ledger."""
    init_db()

    # Ensure at least one reconciliation run exists
    summary = reconciliation_service.get_latest_run()
    if not summary:
        summary = reconciliation_service.run_reconciliation("Auto initial reconciliation run")

    # Read filter query parameters
    classification = request.args.get("classification", "ALL")
    review_param = request.args.get("review", "ALL")
    search_query = request.args.get("search", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    limit = 50
    offset = (page - 1) * limit

    review_filter = None
    if review_param == "REQUIRED":
        review_filter = True
    elif review_param == "NOT_REQUIRED":
        review_filter = False

    results, total_count = reconciliation_service.get_results(
        run_id=summary["id"],
        classification=classification,
        review_required=review_filter,
        search_query=search_query,
        limit=limit,
        offset=offset
    )

    total_pages = max(1, math.ceil(total_count / limit)) if total_count > 0 else 1

    return render_template(
        "reconciliation.html",
        title="Refund Reconciliation",
        active_page="reconciliation",
        summary=summary,
        results=results,
        total_count=total_count,
        current_page=page,
        total_pages=total_pages,
        current_classification=classification,
        current_review=review_param,
        search_query=search_query
    )


@app.route("/reconciliation/run", methods=["POST"])
@login_required
def run_reconciliation_action():
    """Trigger an on-demand reconciliation run and redirect back to ledger."""
    init_db()
    reconciliation_service.run_reconciliation("Manual trigger from dashboard")
    return redirect(url_for("reconciliation_view"))


# ==============================================================================
# AI Customer Refund Intake Routes
# ==============================================================================

@app.route("/intake", methods=["GET"])
@login_required
def intake_form_view():
    """Display customer message intake form with example prompt."""
    init_db()
    db = get_db()
    stats = intake_service.get_stats(db)

    return render_template(
        "intake_form.html",
        title="AI Customer Refund Intake",
        active_page="intake",
        model_name=groq_intake_service.model,
        prompt_version=PROMPT_VERSION,
        groq_configured=groq_intake_service.is_configured,
        stats=stats,
        selected_channel="Email",
        entered_message=""
    )


@app.route("/intake", methods=["POST"])
@login_required
def intake_submit_action():
    """
    Validate customer message, run Groq structured extraction,
    and persist intake record in SQLite.
    Preserves exact unstripped customer message verbatim for audit.
    """
    init_db()
    db = get_db()
    stats = intake_service.get_stats(db)

    raw_message = request.form.get("customer_message", "")
    source_channel = request.form.get("source_channel", "Manual").strip()

    # 1. Validate empty/whitespace input (strip used ONLY for validation check)
    if not raw_message.strip():
        return render_template(
            "intake_form.html",
            title="AI Customer Refund Intake",
            active_page="intake",
            model_name=groq_intake_service.model,
            prompt_version=PROMPT_VERSION,
            groq_configured=groq_intake_service.is_configured,
            stats=stats,
            error_message="Customer message cannot be empty or whitespace.",
            selected_channel=source_channel,
            entered_message=raw_message
        ), 400

    # 2. Call Groq AI service with robust error handling (no leaked secrets)
    try:
        extraction = analyze_refund_message(raw_message)
    except MissingAPIKeyError:
        return render_template(
            "intake_form.html",
            title="AI Customer Refund Intake",
            active_page="intake",
            model_name=groq_intake_service.model,
            prompt_version=PROMPT_VERSION,
            groq_configured=False,
            stats=stats,
            error_message="Groq API Key is not configured. Please set GROQ_API_KEY in the server environment to enable AI intake processing.",
            selected_channel=source_channel,
            entered_message=raw_message
        ), 503
    except InvalidResponseError:
        return render_template(
            "intake_form.html",
            title="AI Customer Refund Intake",
            active_page="intake",
            model_name=groq_intake_service.model,
            prompt_version=PROMPT_VERSION,
            groq_configured=groq_intake_service.is_configured,
            stats=stats,
            error_message="The AI service returned an invalid response format. Please retry or enter details manually.",
            selected_channel=source_channel,
            entered_message=raw_message
        ), 502
    except ExtractionValidationError:
        return render_template(
            "intake_form.html",
            title="AI Customer Refund Intake",
            active_page="intake",
            model_name=groq_intake_service.model,
            prompt_version=PROMPT_VERSION,
            groq_configured=groq_intake_service.is_configured,
            stats=stats,
            error_message="The extracted information failed schema validation rules. Please review input message or triage manually.",
            selected_channel=source_channel,
            entered_message=raw_message
        ), 422
    except GroqAPIError:
        return render_template(
            "intake_form.html",
            title="AI Customer Refund Intake",
            active_page="intake",
            model_name=groq_intake_service.model,
            prompt_version=PROMPT_VERSION,
            groq_configured=groq_intake_service.is_configured,
            stats=stats,
            error_message="Communication with Groq AI service failed. Please verify network connectivity and API status.",
            selected_channel=source_channel,
            entered_message=raw_message
        ), 502
    except ValueError as ve:
        return render_template(
            "intake_form.html",
            title="AI Customer Refund Intake",
            active_page="intake",
            model_name=groq_intake_service.model,
            prompt_version=PROMPT_VERSION,
            groq_configured=groq_intake_service.is_configured,
            stats=stats,
            error_message=str(ve),
            selected_channel=source_channel,
            entered_message=raw_message
        ), 400
    except Exception:
        return render_template(
            "intake_form.html",
            title="AI Customer Refund Intake",
            active_page="intake",
            model_name=groq_intake_service.model,
            prompt_version=PROMPT_VERSION,
            groq_configured=groq_intake_service.is_configured,
            stats=stats,
            error_message="An unexpected error occurred while processing the customer message. Please try again.",
            selected_channel=source_channel,
            entered_message=raw_message
        ), 500

    # 3. Persist extraction and verbatim message to SQLite
    try:
        record_id = intake_service.create_record(
            conn=db,
            original_message=raw_message,
            source_channel=source_channel,
            extraction=extraction,
            model_name=groq_intake_service.model,
            prompt_version=PROMPT_VERSION
        )
    except Exception:
        return render_template(
            "intake_form.html",
            title="AI Customer Refund Intake",
            active_page="intake",
            model_name=groq_intake_service.model,
            prompt_version=PROMPT_VERSION,
            groq_configured=groq_intake_service.is_configured,
            stats=stats,
            error_message="A database error occurred while saving the intake record. Please contact system administrator.",
            selected_channel=source_channel,
            entered_message=raw_message
        ), 500

    # 4. Redirect to intake result page
    return redirect(url_for("intake_detail_view", record_id=record_id))


@app.route("/intake/<int:record_id>", methods=["GET"])
@login_required
def intake_detail_view(record_id: int):
    """Display original customer message and extracted structured fields."""
    init_db()
    db = get_db()
    record = intake_service.get_record(db, record_id)
    if not record:
        return render_template(
            "dashboard.html",
            title="Record Not Found",
            error_message=f"Intake record #{record_id} does not exist.",
            kpis=[],
            records=[]
        ), 404

    review_success = request.args.get("reviewed") == "1"
    return render_template(
        "intake_detail.html",
        title=f"Intake Record #{record_id}",
        active_page="intake",
        record=record,
        review_success=review_success
    )


@app.route("/intake/review", methods=["GET"])
@login_required
def intake_review_queue():
    """Review queue listing records requiring human attention or verification."""
    init_db()
    db = get_db()

    category = request.args.get("category", "ALL")
    status = request.args.get("status", "PENDING_REVIEW")
    page = max(1, int(request.args.get("page", 1)))
    limit = 50
    offset = (page - 1) * limit

    records, total_count = intake_service.get_review_queue(
        conn=db,
        category=category,
        status=status,
        limit=limit,
        offset=offset
    )
    stats = intake_service.get_stats(db)

    return render_template(
        "intake_review.html",
        title="Human Review Queue",
        active_page="review",
        records=records,
        total_count=total_count,
        stats=stats,
        current_category=category,
        current_status=status,
        current_page=page
    )


@app.route("/intake/<int:record_id>/review", methods=["POST"])
@login_required
def intake_mark_reviewed_action(record_id: int):
    """
    Mark an intake record as reviewed by human operator.
    STRICT OPERATIONAL SAFETY:
    This action only changes processing_status and reviewed_at.
    It NEVER approves/rejects refunds, modifies Finance records, or alters payout amounts.
    """
    init_db()
    db = get_db()

    record = intake_service.get_record(db, record_id)
    if not record:
        return render_template(
            "dashboard.html",
            title="Record Not Found",
            error_message=f"Intake record #{record_id} does not exist.",
            kpis=[],
            records=[]
        ), 404

    reviewer_name = session.get("operator_user", "Support Operator")
    intake_service.mark_as_reviewed(db, record_id, reviewer=reviewer_name)

    # If referred from the review queue, redirect back to review queue
    next_url = request.args.get("next") or request.referrer
    if next_url and "review" in next_url:
        return redirect(url_for("intake_review_queue"))

    return redirect(url_for("intake_detail_view", record_id=record_id, reviewed="1"))


# ==============================================================================
# Escalations & Partner Disputes
# ==============================================================================

@app.route("/escalations")
@login_required
def escalations_view():
    """
    Escalations Ledger view displaying B2B partner & customer disputes.
    Provides filtering by status (Open/Resolved), channel, and search by reference/agency.
    """
    init_db()
    db = get_db()
    cursor = db.cursor()

    # Total counts for KPI badges
    cursor.execute("SELECT COUNT(*) FROM escalation_records")
    total_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM escalation_records WHERE resolved_on IS NULL")
    open_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM escalation_records WHERE resolved_on IS NOT NULL")
    resolved_count = cursor.fetchone()[0]

    # Query parameters
    status_filter = request.args.get("status", "ALL").strip().upper()
    channel_filter = request.args.get("channel", "ALL").strip()
    search_query = request.args.get("search", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = 25
    offset = (page - 1) * per_page

    # Base query building
    conditions = []
    params = []

    if status_filter == "OPEN":
        conditions.append("resolved_on IS NULL")
    elif status_filter == "RESOLVED":
        conditions.append("resolved_on IS NOT NULL")

    if channel_filter and channel_filter != "ALL":
        conditions.append("channel = ?")
        params.append(channel_filter)

    if search_query:
        search_pattern = f"%{search_query}%"
        conditions.append("""(
            escalation_id LIKE ? OR 
            norm_escalation_id LIKE ? OR 
            related_ticket_ref LIKE ? OR 
            norm_related_ref LIKE ? OR 
            agent_team LIKE ? OR 
            complaint LIKE ?
        )""")
        params.extend([search_pattern] * 6)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    # Filtered count
    cursor.execute(f"SELECT COUNT(*) FROM escalation_records {where_clause}", params)
    filtered_count = cursor.fetchone()[0]

    total_pages = max(1, math.ceil(filtered_count / per_page)) if filtered_count > 0 else 1
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * per_page

    # Retrieve records
    query = f"""
        SELECT 
            id,
            escalation_id,
            norm_escalation_id,
            raised_on,
            related_ticket_ref,
            norm_related_ref,
            raised_by,
            agent_team,
            channel,
            complaint,
            status,
            resolved_on,
            days_open
        FROM escalation_records
        {where_clause}
        ORDER BY id ASC
        LIMIT ? OFFSET ?
    """
    cursor.execute(query, params + [per_page, offset])
    records = cursor.fetchall()

    # Distinct channels for filter dropdown
    cursor.execute("SELECT DISTINCT channel FROM escalation_records WHERE channel IS NOT NULL ORDER BY channel")
    channels = [row[0] for row in cursor.fetchall()]

    return render_template(
        "escalations.html",
        title="Dispute Escalations",
        active_page="escalations",
        records=records,
        total_count=total_count,
        open_count=open_count,
        resolved_count=resolved_count,
        filtered_count=filtered_count,
        status_filter=status_filter,
        channel_filter=channel_filter,
        channels=channels,
        search_query=search_query,
        page=page,
        total_pages=total_pages,
        per_page=per_page
    )


# ==============================================================================
# Refund Records Ledger
# ==============================================================================

@app.route("/refund-records")
@login_required
def refund_records_view():
    """
    Unified Refund Records Ledger displaying Support, Finance, and Reconciliation data.
    Clearly distinguishes Support status from Finance status, displays payout evidence,
    and highlights status mismatches and unresolved cases without conflating Support closure with disbursement.
    """
    init_db()
    db = get_db()
    cursor = db.cursor()

    # Ensure reconciliation results are available
    summary = reconciliation_service.get_latest_run()
    if not summary:
        summary = reconciliation_service.run_reconciliation("Auto initial reconciliation run")
    run_id = summary["id"]

    # Overall totals for the latest run
    cursor.execute("SELECT COUNT(*) FROM reconciliation_results WHERE run_id = ?", (run_id,))
    total_candidates = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM reconciliation_results WHERE run_id = ? AND classification = 'MATCHED'", (run_id,))
    matched_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM reconciliation_results WHERE run_id = ? AND review_required IS TRUE", (run_id,))
    review_required_count = cursor.fetchone()[0]

    # Filters
    classification_filter = request.args.get("classification", "ALL").strip().upper()
    support_status_filter = request.args.get("support_status", "ALL").strip()
    finance_status_filter = request.args.get("finance_status", "ALL").strip()
    search_query = request.args.get("search", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = 25
    offset = (page - 1) * per_page

    conditions = ["r.run_id = ?"]
    params = [run_id]

    if classification_filter == "MATCHED":
        conditions.append("r.classification = 'MATCHED'")
    elif classification_filter == "UNRESOLVED":
        conditions.append("r.classification != 'MATCHED'")
    elif classification_filter in ("STATUS_MISMATCH", "SUPPORT_ONLY", "FINANCE_ONLY", "DUPLICATE_REFERENCE", "AMBIGUOUS_MATCH", "INVALID_REFERENCE"):
        conditions.append("r.classification = ?")
        params.append(classification_filter)

    if support_status_filter and support_status_filter != "ALL":
        if support_status_filter == "NONE":
            conditions.append("s.status IS NULL")
        else:
            conditions.append("s.status = ?")
            params.append(support_status_filter)

    if finance_status_filter and finance_status_filter != "ALL":
        if finance_status_filter == "NONE":
            conditions.append("f.payout_status IS NULL")
        else:
            conditions.append("f.payout_status = ?")
            params.append(finance_status_filter)

    if search_query:
        search_pattern = f"%{search_query}%"
        conditions.append("""(
            r.norm_reference LIKE ? OR 
            s.ticket_id LIKE ? OR 
            f.ref_no LIKE ? OR 
            s.agent LIKE ? OR 
            f.agent_name LIKE ? OR 
            s.route LIKE ? OR 
            f.sector LIKE ?
        )""")
        params.extend([search_pattern] * 7)

    where_clause = "WHERE " + " AND ".join(conditions)

    count_query = f"""
        SELECT COUNT(*)
        FROM reconciliation_results r
        LEFT JOIN support_records s ON r.support_record_id = s.id
        LEFT JOIN finance_records f ON r.finance_record_id = f.id
        {where_clause}
    """
    cursor.execute(count_query, params)
    filtered_count = cursor.fetchone()[0]

    total_pages = max(1, math.ceil(filtered_count / per_page)) if filtered_count > 0 else 1
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * per_page

    data_query = f"""
        SELECT 
            r.id,
            r.norm_reference,
            s.ticket_id AS support_ref,
            f.ref_no AS finance_ref,
            COALESCE(s.agent, f.agent_name, 'Unknown Agency') AS agency,
            COALESCE(s.route, f.sector, 'N/A') AS route,
            s.refund_amount AS support_amount,
            f.amount_paid AS finance_amount,
            s.status AS support_status,
            f.payout_status AS finance_status,
            f.processed_on AS finance_processed_on,
            r.classification,
            r.review_required,
            r.reason
        FROM reconciliation_results r
        LEFT JOIN support_records s ON r.support_record_id = s.id
        LEFT JOIN finance_records f ON r.finance_record_id = f.id
        {where_clause}
        ORDER BY r.id ASC
        LIMIT ? OFFSET ?
    """
    cursor.execute(data_query, params + [per_page, offset])
    records = cursor.fetchall()

    return render_template(
        "refund_records.html",
        title="Unified Refund Records",
        active_page="records",
        records=records,
        total_candidates=total_candidates,
        matched_count=matched_count,
        review_required_count=review_required_count,
        filtered_count=filtered_count,
        classification_filter=classification_filter,
        support_status_filter=support_status_filter,
        finance_status_filter=finance_status_filter,
        search_query=search_query,
        page=page,
        total_pages=total_pages,
        per_page=per_page
    )


# ==============================================================================
# Operations Assistant
# ==============================================================================

@app.route("/assistant")
@login_required
def operations_assistant_view():
    """
    Operations Assistant informational overview.
    Clearly outlines active AI intake & reconciliation capabilities versus planned roadmap features.
    Provides direct working links to production workflows.
    """
    return render_template(
        "operations_assistant.html",
        title="Operations Assistant",
        active_page="assistant"
    )


@app.route("/health")
def health():
    """Health check route verifying application and database connectivity without exposing operational counts."""
    db_status = "disconnected"
    try:
        db = get_db()
        db.execute("SELECT 1")
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    return jsonify({
        "status": "healthy" if db_status == "connected" else "degraded",
        "service": app.config.get("APP_NAME", "BharatTrip Refund Intelligence"),
        "database": db_status,
        "runtime": f"python-flask-{get_database_backend(app.config.get('DATABASE_URL'))}"
    }), (200 if db_status == "connected" else 503)


# ==============================================================================
# Error Handlers
# ==============================================================================

@app.errorhandler(404)
def not_found_error(error):
    return render_template(
        "dashboard.html",
        title="Page Not Found",
        error_message="The requested page could not be located.",
        kpis=[],
        records=[]
    ), 404


@app.errorhandler(500)
def internal_error(error):
    db = g.pop("db", None)
    if db is not None:
        db.close()
    return jsonify({
        "error": "Internal Server Error",
        "message": "An unexpected error occurred. Please contact system administrator."
    }), 500


if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(
        host=app.config["HOST"],
        port=app.config["PORT"],
        debug=app.config["DEBUG"]
    )
