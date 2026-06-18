from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from markupsafe import escape
import os
from google import genai
from google.genai import types as genai_types
from docx import Document
import io
import requests
import psycopg
from datetime import datetime
import uuid
import re
import json

app = Flask(__name__)

# CORS Configuration - Update with your actual domain(s)
CORS(app, origins=[
    "https://findthefuckup.in",
    "https://www.findthefuckup.in",
    "https://sigil91.com",
    "http://localhost:3000",  # For local development
    "http://localhost:5000"   # For local development
])

# Rate limiting configuration
# For production with multiple instances, use Redis: "redis://localhost:6379"
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# File upload limits
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB max file size

# Environment variables - MUST be set before running
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')

# Validate required environment variables
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable must be set")


def init_db():
    """Initialize database and create tables if they don't exist"""
    if not DATABASE_URL:
        print("WARNING: DATABASE_URL not set, database features disabled")
        return

    try:
        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()

        # Create emails table for tracking users
        cur.execute("""
            CREATE TABLE IF NOT EXISTS emails (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL,
                domain VARCHAR(255) NOT NULL,
                timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
                ip_address VARCHAR(45)
            )
        """)

        # Create shared_issues table for public gallery
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shared_issues (
                id SERIAL PRIMARY KEY,
                share_id VARCHAR(36) UNIQUE NOT NULL,
                paragraph_index INTEGER NOT NULL,
                issue TEXT NOT NULL,
                suggestion TEXT NOT NULL,
                severity VARCHAR(10) NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)

        # Analyses table — stores contract text, paragraphs, analysis JSON, plus metadata
        cur.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id SERIAL PRIMARY KEY,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                email VARCHAR(255),
                domain VARCHAR(255),
                ip_address VARCHAR(45),
                file_size_bytes INTEGER,
                paragraph_count INTEGER,
                issue_count INTEGER,
                severity_high INTEGER DEFAULT 0,
                severity_medium INTEGER DEFAULT 0,
                severity_low INTEGER DEFAULT 0,
                categories TEXT,
                model_used VARCHAR(50),
                duration_seconds NUMERIC(5,2),
                contract_text TEXT,
                analysis_summary TEXT,
                analysis_issues_json TEXT
            )
        """)

        # Best-effort migration for existing tables that pre-date the text columns
        for column_def in (
            "contract_text TEXT",
            "analysis_summary TEXT",
            "analysis_issues_json TEXT",
        ):
            col_name = column_def.split()[0]
            try:
                cur.execute(f"ALTER TABLE analyses ADD COLUMN IF NOT EXISTS {column_def}")
            except Exception as mig_err:
                print(f"(migration skipped for {col_name}: {mig_err})")

        # Create index for faster lookups
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_share_id ON shared_issues(share_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_analyses_domain ON analyses(domain)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_analyses_created_at ON analyses(created_at)
        """)

        conn.commit()
        cur.close()
        conn.close()
        print("Database initialized successfully")
    except Exception as e:
        print(f"Error initializing database: {str(e)}")


def save_email(email, ip_address=None):
    """Save email to database for analytics/tracking"""
    if not DATABASE_URL:
        return

    try:
        domain = email.split('@')[1].lower()

        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute(
            "INSERT INTO emails (email, domain, timestamp, ip_address) VALUES (%s, %s, %s, %s)",
            (email, domain, datetime.utcnow(), ip_address)
        )

        conn.commit()
        cur.close()
        conn.close()
        print(f"Email saved: {email}")
    except Exception as e:
        print(f"Error saving email: {str(e)}")


def save_analysis(email, ip_address, file_size_bytes, paragraph_count,
                   issues, model_used, duration_seconds,
                   contract_text=None, analysis_summary=None):
    """
    Save the analysis: full contract text + paragraphs + AI analysis + metadata.
    """
    if not DATABASE_URL:
        return

    try:
        import json as _json
        domain = email.split('@')[1].lower() if email and '@' in email else None
        counts = {'high': 0, 'medium': 0, 'low': 0}
        categories = set()
        for iss in issues or []:
            sev = (iss.get('severity') or '').lower()
            if sev in counts:
                counts[sev] += 1
            cat = (iss.get('category') or '').upper().strip()
            if cat:
                categories.add(cat)

        issues_json = _json.dumps(issues or [], ensure_ascii=False)

        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO analyses
               (email, domain, ip_address, file_size_bytes, paragraph_count, issue_count,
                severity_high, severity_medium, severity_low, categories, model_used, duration_seconds,
                contract_text, analysis_summary, analysis_issues_json)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (email, domain, ip_address, file_size_bytes, paragraph_count, len(issues or []),
             counts['high'], counts['medium'], counts['low'],
             ','.join(sorted(categories)) if categories else None,
             model_used, duration_seconds,
             contract_text, analysis_summary, issues_json)
        )
        conn.commit()
        cur.close()
        conn.close()
        print(f"Analysis saved (domain={domain}, paragraphs={paragraph_count}, issues={len(issues or [])}, cats={categories})")
    except Exception as e:
        print(f"Error saving analysis: {str(e)}")


# Initialize database on startup
init_db()


@app.route('/')
def home():
    """Serve index.html"""
    try:
        return app.send_static_file('index.html')
    except Exception as e:
        print(f"Error serving index.html: {str(e)}")
        return "Error loading page", 500


def extract_text_from_docx(file_content):
    """Extract text from docx file with paragraph tracking"""
    try:
        doc = Document(io.BytesIO(file_content))
        paragraphs = []
        for i, para in enumerate(doc.paragraphs):
            if para.text.strip():
                paragraphs.append({
                    'index': i,
                    'text': para.text
                })
        return paragraphs
    except Exception as e:
        raise ValueError(f"Failed to parse document: {str(e)}")


@app.route('/api/check-document', methods=['POST'])
@limiter.limit("10 per hour")  # Limit to 10 document checks per hour per IP
def check_document():
    """
    Main endpoint for document analysis.
    Email + reCAPTCHA gating removed for the open public preview.
    Expects: multipart/form-data with 'document' file.
    """
    try:
        # Optional email — kept as a write-through so we can still build the funnel
        # without gating access on it. May be absent.
        email = request.form.get('email') or None
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        if ip_address:
            ip_address = ip_address.split(',')[0].strip()
        if email:
            save_email(email, ip_address)

        # Get and validate uploaded file
        if 'document' not in request.files:
            return jsonify({'error': 'No document uploaded'}), 400

        file = request.files['document']
        if not file or not file.filename:
            return jsonify({'error': 'No document uploaded'}), 400

        if not file.filename.endswith('.docx'):
            return jsonify({'error': 'Only .docx files are supported'}), 400

        # Read and validate file size
        file_content = file.read()
        if len(file_content) > 10 * 1024 * 1024:  # 10MB
            return jsonify({'error': 'File too large (max 10MB)'}), 400

        # STEP 4: Extract text from document
        paragraphs = extract_text_from_docx(file_content)

        if not paragraphs or len(paragraphs) == 0:
            return jsonify({'error': 'Document appears to be empty'}), 400

        # Combine all paragraphs for analysis. Tag each with its real paragraph number
        # ([¶N], the same index the UI renders) so the model references paragraphs by the
        # exact index used downstream instead of inventing its own 0,1,2… counting.
        full_text = '\n\n'.join([f"[¶{p['index']}] {p['text']}" for p in paragraphs])

        # STEP 5: Call Gemini API for analysis
        import time
        analysis_start = time.time()
        model_used = None

        client = genai.Client(api_key=GEMINI_API_KEY)
        # Resilient model fallback chain (primary -> lite -> 2.0-flash on quota/availability errors)
        model_chain = [GEMINI_MODEL, "gemini-2.5-flash-lite", "gemini-2.0-flash"]
        # De-dup while preserving order
        seen = set()
        model_chain = [m for m in model_chain if not (m in seen or seen.add(m))]

        # Create detailed prompt for Gemini — India-specific
        prompt = f"""You are a meticulous Indian commercial-contracts reviewer with deep familiarity with Indian law and Indian SaaS commercial practice.

Analyse the following contract for potential errors, inconsistencies, and problems — both general drafting issues and Indian-jurisdiction-specific issues.

For each issue you find, provide:
1. The paragraph number where the issue occurs. Every paragraph in the contract below is
   prefixed with its number in the form [¶N]. Use that exact integer N as paragraphIndex —
   do NOT invent your own numbering and do NOT use clause labels like "GCC 5.7.7" or
   "General". If an issue is document-wide, use the [¶N] of the single most relevant paragraph.
2. A clear description of the issue (one to two sentences)
3. A suggested fix or improvement (concrete language where possible)
4. Severity level (high, medium, or low) — calibration rules:
   - HIGH: direct logical contradictions inside a clause or across clauses (e.g. "30 days notice" + "immediate effect" in the same clause); clauses void or unenforceable under Indian statute (Section 27 restraint of trade, Section 28 shortened limitation, Section 74 penalty); uncapped indemnity; DPDPA non-compliance that would attract a penalty; FEMA non-compliance that would attract RBI scrutiny; missing stamp duty on a high-value contract.
   - MEDIUM: ambiguity or drafting error that creates real risk but is fixable; weak DPDPA/GST/FEMA hygiene; non-fatal stamp duty silence; misaligned caps/exclusions; missing standard protections.
   - LOW: stylistic, definitional, or housekeeping issues that don't change enforceability.
5. A short category tag from this fixed list (choose the single closest match):
   DPDPA, FEMA, GST, STAMP_DUTY, CONTRACT_ACT, IT_ACT, ARBITRATION_ACT, COMPANIES_ACT,
   LIMITATION_ACT, INDEMNITY, LIABILITY, IP, TERMINATION, GOVERNING_LAW, DEFINITIONS,
   AMBIGUITY, CROSS_BORDER, NON_COMPETE, CONFIDENTIALITY, PAYMENT, OTHER

   Category routing rules (apply BEFORE picking a tag):
   - Any "penalty vs liquidated damages" / Section 74 issue → CONTRACT_ACT (NOT PAYMENT).
   - Any restraint-of-trade / Section 27 issue → NON_COMPETE.
   - Any contractually shortened limitation period / Section 28 issue → LIMITATION_ACT.
   - PAYMENT is for invoicing mechanics, currency, set-off, late fees ONLY when the statute isn't the point.
   - When in doubt between a statute tag and a generic tag, ALWAYS pick the statute tag.

GENERAL drafting issues to flag:
- Contradictory clauses
- Ambiguous language
- Missing or undefined defined terms
- Broken or incorrect cross-references
- Unusual or asymmetric risk terms (e.g. uncapped indemnities, perpetual licences, IP transfer hidden in payment clauses)
- Liability caps that don't align with indemnity exposure
- Termination triggers vs notice periods that conflict
- Governing law vs jurisdiction conflicts
- Grammatical errors that change meaning
- Inconsistent terminology (different names for the same concept)

INDIAN-LAW-SPECIFIC issues to flag (these are the highest-value finds for Indian SaaS):
- DPDPA 2023 compliance gaps: missing consent provisions, no breach notification mechanism (72-hour requirement), no data principal rights, no purpose limitation, no retention schedule, cross-border transfer terms that don't address the DPDPA blacklist mechanism
- Indian Contract Act 1872 issues: clauses that fail for want of consideration, unenforceable restraint-of-trade beyond Section 27, penalty clauses (vs liquidated damages) that won't survive Section 74
- FEMA implications: USD payment terms without addressing FEMA compliance, foreign-entity invoicing structure, intercompany agreements that look like undisclosed external commercial borrowing (ECB)
- GST treatment: missing or wrong place-of-supply language, no GST registration warranties, no clarity on whether prices are inclusive or exclusive of GST, no obligation to issue tax invoice
- Stamp duty (DEFAULT CHECK — always flag if the contract is silent on stamping): commercial contracts intended for execution in India must address stamp duty. If the document does not mention stamping, the applicable Stamp Act/state schedule, an e-stamp acknowledgement, or how duty is allocated between parties, FLAG IT — silence on stamp duty is itself the issue. Also flag multi-state execution without addressing the higher-duty-state rule, missing e-stamp/SHCIL reference for high-value contracts, and unstamped instruments that would be inadmissible under Section 35 of the Indian Stamp Act.
- IT Act 2000: electronic signature clauses that don't reference Section 5 / Section 3A, electronic record clauses that don't account for Indian evidentiary rules
- Arbitration & Conciliation Act 1996: seat vs venue confusion (BALCO line), unclear "Indian" governing law for foreign parties, missing emergency arbitrator provisions
- Companies Act 2013: related-party transaction clauses missing Section 188 compliance, director indemnity beyond what Section 197 permits
- Limitation Act 1963: contractually shortened limitation periods that won't survive (Section 28), claim-extinction clauses
- Bar Council of India advertising restrictions where one party is a law firm or legal services provider
- Indian payroll / employment compliance gaps in services agreements involving on-site personnel (PF, ESI, professional tax, Shops & Establishments registration)

ALSO LOOK FOR:
- Foreign-law MSA being signed by an Indian entity without considering Indian-side enforceability (e.g. Delaware governing law with no carve-out for Indian mandatory law)
- Cross-border data flows without addressing both GDPR and DPDPA
- Currency-conversion clauses that ignore RBI reference rates
- Indemnity payable in foreign currency by an Indian entity (FEMA implications)

Contract text (each paragraph is prefixed with its number as [¶N] — reference these exact numbers):

{full_text}

Respond in JSON format with this structure:
{{
    "summary": "Brief overall assessment (1-2 sentences) — be direct, mention if the contract is Indian-law or foreign-law and flag the single most serious issue first",
    "issues": [
        {{
            "paragraphIndex": 0,
            "issue": "Description of the issue",
            "suggestion": "How to fix it",
            "severity": "high|medium|low",
            "category": "DPDPA|FEMA|GST|STAMP_DUTY|CONTRACT_ACT|IT_ACT|ARBITRATION_ACT|COMPANIES_ACT|LIMITATION_ACT|INDEMNITY|LIABILITY|IP|TERMINATION|GOVERNING_LAW|DEFINITIONS|AMBIGUITY|CROSS_BORDER|NON_COMPETE|CONFIDENTIALITY|PAYMENT|OTHER"
        }}
    ]
}}"""

        # Walk the model chain. On EACH model we make up to 2 attempts: the second only fires if
        # the response came back but failed to parse (truncation / prose leakage). Availability
        # errors (503/429) skip immediately to the next model.
        analysis = None
        model_used = None
        last_err = None
        json_parse_fail_detail = None
        for model_name in model_chain:
            for attempt in (1, 2):
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=genai_types.GenerateContentConfig(
                            response_mime_type="application/json",
                            max_output_tokens=32768,  # bumped from 8192 — long contracts produce >8k of JSON
                            temperature=0.2,
                        ),
                    )
                    response_text = (response.text or "").strip()
                    # Belt-and-suspenders: extract JSON if the model added prose
                    js = response_text.find('{')
                    je = response_text.rfind('}') + 1
                    if js != -1 and je > js:
                        response_text = response_text[js:je]
                    analysis = json.loads(response_text)
                    print(f"INFO: Gemini call succeeded on model={model_name} attempt={attempt}")
                    model_used = model_name
                    break  # parsed cleanly, exit attempt loop
                except json.JSONDecodeError as je_err:
                    # Output came back but was truncated / malformed. Retry once on the SAME model.
                    json_parse_fail_detail = f"{model_name} attempt {attempt}: {je_err}"
                    print(f"WARN: JSON parse failed on {model_name} attempt {attempt}: {je_err}; tail={response_text[-200:]!r}")
                    if attempt == 1:
                        continue  # retry same model
                    # Second attempt also failed — try the next model
                    break
                except Exception as e:
                    last_err = e
                    err_str = str(e)
                    if any(code in err_str for code in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED")):
                        print(f"WARN: {model_name} unavailable ({type(e).__name__}); trying next in chain")
                        break  # exit attempt loop, go to next model
                    raise
            if analysis is not None:
                break

        if analysis is None:
            # All models failed. Give the user a concrete, actionable message.
            if json_parse_fail_detail:
                print(f"ERROR: every model returned unparseable JSON. Last: {json_parse_fail_detail}")
                return jsonify({'error': 'The AI returned a malformed response (often a truncation issue on very long contracts). Please try again, or split the contract into smaller sections.'}), 502
            print(f"ERROR: all Gemini models exhausted. Last error: {last_err}")
            return jsonify({'error': 'The AI is temporarily overloaded. Please try again in 1-2 minutes.'}), 503

        # Normalise paragraphIndex before it goes any further. The model is asked for an
        # integer matching the [¶N] tags, but on some documents it returns a clause label
        # ("GCC 5.7.7", "General") or a stray number. A non-int would (a) hide the jump link
        # in the UI and (b) blow up the INTEGER paragraph_index column when shared. Coerce to
        # a valid known index, else None — the UI falls back gracefully and sharing sends 0.
        valid_indices = {p['index'] for p in paragraphs}
        for iss in analysis.get('issues', []) or []:
            raw = iss.get('paragraphIndex')
            norm = None
            if isinstance(raw, bool):
                norm = None  # bool is a subclass of int — guard against True/False
            elif isinstance(raw, int):
                norm = raw
            elif isinstance(raw, float):
                norm = int(raw)
            elif isinstance(raw, str) and raw.strip().isdigit():
                norm = int(raw.strip())
            iss['paragraphIndex'] = norm if norm in valid_indices else None

        # STEP 6: Persist the analysis (contract text + paragraphs + AI output + metadata)
        duration = round(time.time() - analysis_start, 2)
        save_analysis(
            email=email,
            ip_address=ip_address,
            file_size_bytes=len(file_content),
            paragraph_count=len(paragraphs),
            issues=analysis.get('issues', []),
            model_used=model_used,
            duration_seconds=duration,
            contract_text=full_text,
            analysis_summary=analysis.get('summary'),
        )

        # STEP 7: Return results with original paragraphs
        return jsonify({
            'success': True,
            'summary': analysis.get('summary', 'Analysis complete'),
            'issues': analysis.get('issues', []),
            'paragraphs': paragraphs
        })

    except json.JSONDecodeError as e:
        print(f"JSON parsing error: {str(e)}")
        return jsonify({'error': 'Error processing analysis results'}), 500
    except Exception as e:
        print(f"Unexpected error in check_document: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'An unexpected error occurred'}), 500


@app.route('/api/share-issue', methods=['POST'])
def share_issue():
    """
    Create a shareable link for an issue
    Expects: JSON with 'paragraphIndex', 'issue', 'suggestion', 'severity'
    """
    if not DATABASE_URL:
        return jsonify({'error': 'Sharing feature not available'}), 503

    try:
        data = request.get_json()

        # Validate required fields
        required_fields = ['paragraphIndex', 'issue', 'suggestion', 'severity']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Missing required field: {field}'}), 400

        # Generate unique share ID
        share_id = str(uuid.uuid4())

        # Save to database
        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute(
            """INSERT INTO shared_issues 
               (share_id, paragraph_index, issue, suggestion, severity, created_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                share_id,
                data['paragraphIndex'],
                data['issue'],
                data['suggestion'],
                data['severity'],
                datetime.utcnow()
            )
        )

        conn.commit()
        cur.close()
        conn.close()

        # Return shareable URL
        share_url = f"{request.host_url}issue/{share_id}"

        return jsonify({
            'success': True,
            'shareUrl': share_url,
            'shareId': share_id
        })

    except Exception as e:
        print(f"Error sharing issue: {str(e)}")
        return jsonify({'error': 'Failed to create share link'}), 500


@app.route('/api/gallery', methods=['GET'])
def get_gallery():
    """
    Get paginated list of shared issues for public gallery
    Query params: page (default 1), per_page (default 20)
    """
    if not DATABASE_URL:
        return jsonify({'error': 'Gallery not available'}), 503

    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 20))
        per_page = min(per_page, 100)  # Max 100 items per page

        offset = (page - 1) * per_page

        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()

        # Get total count
        cur.execute("SELECT COUNT(*) FROM shared_issues")
        total = cur.fetchone()[0]

        # Get paginated issues
        cur.execute(
            """SELECT share_id, paragraph_index, issue, suggestion, severity, created_at
               FROM shared_issues
               ORDER BY created_at DESC
               LIMIT %s OFFSET %s""",
            (per_page, offset)
        )

        issues = []
        for row in cur.fetchall():
            issues.append({
                'shareId': row[0],
                'paragraphIndex': row[1],
                'issue': row[2],
                'suggestion': row[3],
                'severity': row[4],
                'createdAt': row[5].isoformat(),
                'shareUrl': f"{request.host_url}issue/{row[0]}"
            })

        cur.close()
        conn.close()

        total_pages = (total + per_page - 1) // per_page

        return jsonify({
            'success': True,
            'issues': issues,
            'page': page,
            'perPage': per_page,
            'total': total,
            'totalPages': total_pages
        })

    except Exception as e:
        print(f"Error fetching gallery: {str(e)}")
        return jsonify({'error': 'Failed to load gallery'}), 500


# ---------------------------------------------------------------------------
# Shared chrome for /gallery and /issue/<id>. Mirrors the design tokens in
# static/index.html so the Hall of Shame matches the rest of the tool.
# ---------------------------------------------------------------------------
HOS_HEAD_CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<style>
:root {
    --canvas: #0A0A0A; --surface: #111111; --elevated: #161616;
    --ink: #FAFAF7;
    --ink-85: rgba(250,250,247,0.85); --ink-70: rgba(250,250,247,0.70);
    --ink-55: rgba(250,250,247,0.55); --ink-40: rgba(250,250,247,0.40);
    --ink-25: rgba(250,250,247,0.25); --ink-12: rgba(250,250,247,0.12);
    --ink-08: rgba(250,250,247,0.08);
    --high: #f87171; --medium: #fbbf24; --low: #86efac;
}
*, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
html, body {
    background: var(--canvas); color: var(--ink);
    font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
    min-height: 100vh;
}
a { color: inherit; }
.italic, .serif { font-family: "Instrument Serif", Georgia, serif; font-style: italic; }
.page { max-width: 960px; margin: 0 auto; padding: 28px 32px 80px; }
.nav { display: flex; align-items: center; justify-content: space-between; padding-bottom: 40px; }
.brand { display: inline-flex; align-items: center; gap: 12px; text-decoration: none; color: var(--ink); }
.brand-mark { color: var(--ink-70); }
.brand-name { font-family: "Instrument Serif", Georgia, serif; font-size: 19px; letter-spacing: -0.022em; }
.nav-meta { font-family: "Instrument Serif", Georgia, serif; font-style: italic; font-size: 13px; color: var(--ink-55); display: inline-flex; align-items: center; gap: 8px; }
.nav-link { color: var(--ink-70); text-decoration: none; border-bottom: 1px solid var(--ink-25); padding-bottom: 1px; transition: 0.15s; }
.nav-link:hover { color: var(--ink); border-bottom-color: var(--ink); }
.nav-sep { color: var(--ink-25); }
.eyebrow { font-family: "Instrument Serif", Georgia, serif; font-style: italic; font-size: 13px; color: var(--ink-55); margin-bottom: 14px; }
h1.page-title { font-family: "Instrument Serif", Georgia, serif; font-size: 48px; letter-spacing: -0.025em; line-height: 1.05; margin-bottom: 18px; font-weight: 400; }
h1.page-title em { font-style: italic; color: var(--ink-70); }
.lede { font-size: 17px; color: var(--ink-70); line-height: 1.55; max-width: 560px; margin-bottom: 36px; }
.cta-row { display: flex; gap: 18px; align-items: center; margin-bottom: 48px; flex-wrap: wrap; }
.cta { display: inline-flex; align-items: center; gap: 8px; background: var(--ink); color: var(--canvas); padding: 12px 22px; text-decoration: none; font-size: 14px; font-weight: 500; border-radius: 3px; transition: 0.15s; }
.cta:hover { background: var(--ink-85); }
.cta-secondary { display: inline-flex; align-items: center; gap: 6px; color: var(--ink-70); font-family: "Instrument Serif", Georgia, serif; font-style: italic; font-size: 14px; text-decoration: none; border-bottom: 1px solid var(--ink-25); padding-bottom: 1px; transition: 0.15s; }
.cta-secondary:hover { color: var(--ink); border-bottom-color: var(--ink); }
.stats { color: var(--ink-55); font-family: "Instrument Serif", Georgia, serif; font-style: italic; font-size: 14px; margin-bottom: 28px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 18px; }
.card { background: var(--surface); border: 1px solid var(--ink-08); border-radius: 4px; padding: 22px 22px 18px; cursor: pointer; transition: 0.15s; display: flex; flex-direction: column; gap: 12px; }
.card:hover { border-color: var(--ink-25); transform: translateY(-1px); }
.card-meta { display: flex; align-items: center; gap: 10px; font-size: 12px; }
.sev { display: inline-flex; align-items: center; gap: 6px; color: var(--ink-70); text-transform: lowercase; font-family: "Instrument Serif", Georgia, serif; font-style: italic; font-size: 13px; }
.dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }
.dot.high { background: var(--high); } .dot.medium { background: var(--medium); } .dot.low { background: var(--low); }
.where { color: var(--ink-40); font-family: "Instrument Serif", Georgia, serif; font-style: italic; font-size: 13px; }
.card-text { color: var(--ink-85); font-size: 15px; line-height: 1.55; }
.card-fix { color: var(--ink-55); font-size: 13.5px; line-height: 1.55; padding-top: 4px; border-top: 1px solid var(--ink-08); }
.card-fix-label { color: var(--ink-40); font-family: "Instrument Serif", Georgia, serif; font-style: italic; margin-right: 6px; }
.card-date { color: var(--ink-40); font-size: 12px; margin-top: auto; }
.loading, .empty { padding: 80px 20px; text-align: center; color: var(--ink-55); font-family: "Instrument Serif", Georgia, serif; font-style: italic; font-size: 16px; }
.pagination { display: flex; justify-content: center; gap: 6px; margin-top: 56px; flex-wrap: wrap; }
.pagination button { padding: 8px 14px; background: transparent; color: var(--ink-70); border: 1px solid var(--ink-12); border-radius: 3px; cursor: pointer; font-size: 13px; transition: 0.15s; font-family: inherit; }
.pagination button:hover:not(:disabled) { border-color: var(--ink-40); color: var(--ink); }
.pagination button:disabled { opacity: 0.4; cursor: not-allowed; }
.pagination button.active { background: var(--ink); color: var(--canvas); border-color: var(--ink); }
.detail-panel { background: var(--surface); border: 1px solid var(--ink-08); border-radius: 4px; padding: 40px 44px; }
.detail-meta { color: var(--ink-55); font-family: "Instrument Serif", Georgia, serif; font-style: italic; font-size: 14px; margin-bottom: 26px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.detail-section { margin-bottom: 28px; }
.detail-label { color: var(--ink-40); font-family: "Instrument Serif", Georgia, serif; font-style: italic; font-size: 13px; margin-bottom: 10px; }
.detail-body { color: var(--ink); font-size: 17px; line-height: 1.6; }
.detail-body.fix { color: var(--ink-85); font-size: 15.5px; }
footer { margin-top: 60px; padding: 22px 0 0; border-top: 1px solid var(--ink-08); color: var(--ink-40); font-family: "Instrument Serif", Georgia, serif; font-style: italic; font-size: 13px; display: flex; justify-content: space-between; }
footer a { color: var(--ink-70); text-decoration: none; border-bottom: 1px solid var(--ink-25); padding-bottom: 1px; }
footer a:hover { color: var(--ink); border-bottom-color: var(--ink); }
@media (max-width: 600px) {
    .page { padding: 20px 18px 60px; }
    h1.page-title { font-size: 36px; }
    .detail-panel { padding: 28px 24px; }
    .nav-meta { font-size: 12px; }
}
</style>
"""

HOS_NAV = """
<header class="nav">
    <a class="brand" href="/">
        <svg class="brand-mark" width="32" height="12" viewBox="0 0 36 14" fill="none" aria-hidden>
            <path d="M 2 11 C 7 1, 13 12, 18 7 C 23 2, 29 5, 34 9" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" fill="none"/>
        </svg>
        <span class="brand-name">Find the Flaw · India</span>
    </a>
    <span class="nav-meta">
        <a href="/gallery" class="nav-link">Hall of Shame</a>
        <span class="nav-sep">·</span>
        By <a href="https://sigil91.com" target="_blank" rel="noopener noreferrer" class="nav-link">Sigil</a>
    </span>
</header>
"""

HOS_FOOTER = """
<footer>
    <span>© 2026 Sigil · Not legal advice.</span>
    <a href="https://sigil91.com" target="_blank" rel="noopener noreferrer">sigil91.com</a>
</footer>
"""


@app.route('/issue/<share_id>')
def view_shared_issue(share_id):
    """View a single shared issue — dark editorial palette matching the main app."""
    if not DATABASE_URL:
        return "Sharing feature not available", 503

    try:
        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            """SELECT paragraph_index, issue, suggestion, severity, created_at
               FROM shared_issues WHERE share_id = %s""",
            (share_id,)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return "Issue not found", 404

        issue_text = escape(row[1])
        suggestion_text = escape(row[2] or "")
        severity = row[3] or 'medium'
        para_idx = row[0]
        shared_on = row[4].strftime('%-d %B %Y')
        og_desc = escape(row[1][:200])

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta property="og:title" content="A flaw, found · Find the Flaw">
    <meta property="og:description" content="{og_desc}">
    <meta name="twitter:card" content="summary_large_image">
    <title>A flaw, found · Find the Flaw · India</title>
    {HOS_HEAD_CSS}
</head>
<body>
    <div class="page">
        {HOS_NAV}
        <div class="eyebrow">Hall of Shame</div>
        <h1 class="page-title">A flaw, <em>found</em>.</h1>

        <div class="detail-panel">
            <div class="detail-meta">
                <span class="sev"><span class="dot {severity}"></span>{severity}</span>
                <span class="nav-sep">·</span>
                <span>paragraph {para_idx}</span>
                <span class="nav-sep">·</span>
                <span>shared on {shared_on}</span>
            </div>
            <div class="detail-section">
                <div class="detail-label">The flaw</div>
                <div class="detail-body">{issue_text}</div>
            </div>
            <div class="detail-section">
                <div class="detail-label">Suggested fix</div>
                <div class="detail-body fix">{suggestion_text}</div>
            </div>
        </div>

        <div class="cta-row" style="margin-top: 36px;">
            <a class="cta" href="/">Check your contract →</a>
            <a class="cta-secondary" href="/gallery">See the full Hall of Shame</a>
        </div>

        {HOS_FOOTER}
    </div>
</body>
</html>"""

    except Exception as e:
        print(f"Error viewing shared issue: {{str(e)}}")
        return "Error loading issue", 500


@app.route('/gallery')
def gallery():
    """Public gallery of shared issues — dark editorial palette matching the main app."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hall of Shame · Find the Flaw · India</title>
    {HOS_HEAD_CSS}
</head>
<body>
    <div class="page">
        {HOS_NAV}
        <div class="eyebrow">A public ledger</div>
        <h1 class="page-title">Hall of <em>Shame</em>.</h1>
        <p class="lede">
            Real flaws, found in real contracts. Every entry was sent here by someone
            who ran a contract through the tool and thought the world should see it.
        </p>

        <div class="cta-row">
            <a class="cta" href="/">Check your contract →</a>
            <a class="cta-secondary" href="https://sigil91.com" target="_blank" rel="noopener noreferrer">Want an advocate to sign off?</a>
        </div>

        <div id="stats" class="stats"></div>
        <div id="loading" class="loading">Loading the wall…</div>
        <div id="error-container"></div>
        <div id="gallery" class="grid"></div>
        <div id="pagination" class="pagination"></div>

        {HOS_FOOTER}
    </div>

    <script>
        const perPage = 20;

        async function loadGallery(page = 1) {{
            try {{
                document.getElementById('loading').style.display = 'block';
                document.getElementById('gallery').innerHTML = '';
                document.getElementById('error-container').innerHTML = '';

                const response = await fetch('/api/gallery?page=' + page + '&per_page=' + perPage);
                const data = await response.json();
                document.getElementById('loading').style.display = 'none';

                if (!data.success) throw new Error(data.error || 'Failed to load gallery');

                if (data.issues.length === 0) {{
                    document.getElementById('gallery').innerHTML =
                        '<div class="empty">Nothing here yet — be the first to send something to the wall.</div>';
                    return;
                }}

                document.getElementById('stats').innerHTML =
                    data.total + ' flaw' + (data.total === 1 ? '' : 's') + ' on the wall · showing ' + data.issues.length;

                const gallery = document.getElementById('gallery');
                data.issues.forEach(issue => {{
                    const card = document.createElement('div');
                    card.className = 'card';
                    card.onclick = () => window.open(issue.shareUrl, '_blank');

                    const date = new Date(issue.createdAt);
                    const dateStr = date.toLocaleDateString('en-IN', {{
                        month: 'short', day: 'numeric', year: 'numeric'
                    }});

                    card.innerHTML =
                        '<div class="card-meta">' +
                            '<span class="sev"><span class="dot ' + issue.severity + '"></span>' + issue.severity + '</span>' +
                            '<span class="nav-sep">·</span>' +
                            '<span class="where">paragraph ' + issue.paragraphIndex + '</span>' +
                        '</div>' +
                        '<div class="card-text">' + escapeHtml(issue.issue) + '</div>' +
                        (issue.suggestion ? '<div class="card-fix"><span class="card-fix-label">Fix —</span>' + escapeHtml(issue.suggestion) + '</div>' : '') +
                        '<div class="card-date">shared ' + dateStr + '</div>';
                    gallery.appendChild(card);
                }});

                renderPagination(data.page, data.totalPages);
            }} catch (error) {{
                console.error('Error loading gallery:', error);
                document.getElementById('loading').style.display = 'none';
                document.getElementById('error-container').innerHTML =
                    '<div class="empty">Could not load the wall — try again in a minute.</div>';
            }}
        }}

        function renderPagination(currentPage, totalPages) {{
            const pagination = document.getElementById('pagination');
            pagination.innerHTML = '';
            if (totalPages <= 1) return;

            const prevBtn = document.createElement('button');
            prevBtn.textContent = '← Previous';
            prevBtn.disabled = currentPage === 1;
            prevBtn.onclick = () => loadGallery(currentPage - 1);
            pagination.appendChild(prevBtn);

            const maxVisible = 5;
            let startPage = Math.max(1, currentPage - Math.floor(maxVisible / 2));
            let endPage = Math.min(totalPages, startPage + maxVisible - 1);
            if (endPage - startPage < maxVisible - 1) {{
                startPage = Math.max(1, endPage - maxVisible + 1);
            }}

            for (let i = startPage; i <= endPage; i++) {{
                const pageBtn = document.createElement('button');
                pageBtn.textContent = i;
                pageBtn.className = i === currentPage ? 'active' : '';
                pageBtn.onclick = () => loadGallery(i);
                pagination.appendChild(pageBtn);
            }}

            const nextBtn = document.createElement('button');
            nextBtn.textContent = 'Next →';
            nextBtn.disabled = currentPage === totalPages;
            nextBtn.onclick = () => loadGallery(currentPage + 1);
            pagination.appendChild(nextBtn);
        }}

        function escapeHtml(text) {{
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }}

        loadGallery(1);
    </script>
</body>
</html>"""


@app.route('/api/stats', methods=['GET'])
def stats():
    """
    Aggregate funnel intelligence. No contract content — only counts + category tags.
    Protect this in prod (admin auth) before exposing publicly.
    """
    if not DATABASE_URL:
        return jsonify({'error': 'Stats unavailable (no DATABASE_URL)'}), 503

    try:
        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute("""
            SELECT
                COUNT(*) AS total_analyses,
                COUNT(DISTINCT domain) AS distinct_domains,
                COALESCE(SUM(issue_count), 0) AS total_issues,
                COALESCE(SUM(severity_high), 0) AS total_high,
                COALESCE(SUM(severity_medium), 0) AS total_medium,
                COALESCE(SUM(severity_low), 0) AS total_low,
                COALESCE(AVG(issue_count), 0) AS avg_issues_per_doc,
                COALESCE(AVG(duration_seconds), 0) AS avg_duration_s
            FROM analyses
        """)
        row = cur.fetchone()
        overall = {
            'total_analyses': row[0],
            'distinct_domains': row[1],
            'total_issues': row[2],
            'total_high': row[3],
            'total_medium': row[4],
            'total_low': row[5],
            'avg_issues_per_doc': round(float(row[6]), 1),
            'avg_duration_s': round(float(row[7]), 1),
        }

        # Top issue categories across the funnel
        cur.execute("""
            SELECT trim(unnest) AS category, COUNT(*) AS times_seen
            FROM analyses, unnest(string_to_array(categories, ','))
            WHERE categories IS NOT NULL AND trim(unnest) <> ''
            GROUP BY 1
            ORDER BY 2 DESC
            LIMIT 25
        """)
        category_counts = [{'category': r[0], 'analyses_with_it': r[1]} for r in cur.fetchall()]

        # Top domains uploading (good for sales lead identification)
        cur.execute("""
            SELECT domain, COUNT(*) AS uploads
            FROM analyses
            WHERE domain IS NOT NULL
            GROUP BY 1
            ORDER BY 2 DESC
            LIMIT 25
        """)
        top_domains = [{'domain': r[0], 'uploads': r[1]} for r in cur.fetchall()]

        cur.close()
        conn.close()
        return jsonify({
            'overall': overall,
            'categories': category_counts,
            'top_domains': top_domains,
        })
    except Exception as e:
        print(f"Error in /api/stats: {str(e)}")
        return jsonify({'error': 'Failed to fetch stats'}), 500


@app.errorhandler(413)
def file_too_large(e):
    return jsonify({'error': 'File too large (max 10MB)'}), 413


@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({'error': 'Too many requests. Please try again later.'}), 429


@app.errorhandler(Exception)
def handle_error(e):
    # Log the full error internally
    print(f"Unhandled error: {str(e)}")
    import traceback
    traceback.print_exc()

    # Return generic message to user
    return jsonify({'error': 'An error occurred processing your request'}), 500


if __name__ == '__main__':
    # This should only be used for local development
    # In production, use gunicorn (see Procfile)
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5001)))
