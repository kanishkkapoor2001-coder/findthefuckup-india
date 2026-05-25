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
RECAPTCHA_SECRET_KEY = os.environ.get('RECAPTCHA_SECRET_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')

# Validate required environment variables
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY environment variable must be set")
if not RECAPTCHA_SECRET_KEY:
    raise ValueError("RECAPTCHA_SECRET_KEY environment variable must be set")


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

        # Analyses metadata table — NO CONTRACT TEXT, only counts + tags
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
                duration_seconds NUMERIC(5,2)
            )
        """)

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


def save_analysis_metadata(email, ip_address, file_size_bytes, paragraph_count,
                            issues, model_used, duration_seconds):
    """
    Save analysis metadata for funnel/marketing intelligence.
    DOES NOT save contract text or issue text — only aggregates and category tags.
    """
    if not DATABASE_URL:
        return

    try:
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

        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO analyses
               (email, domain, ip_address, file_size_bytes, paragraph_count, issue_count,
                severity_high, severity_medium, severity_low, categories, model_used, duration_seconds)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (email, domain, ip_address, file_size_bytes, paragraph_count, len(issues or []),
             counts['high'], counts['medium'], counts['low'],
             ','.join(sorted(categories)) if categories else None,
             model_used, duration_seconds)
        )
        conn.commit()
        cur.close()
        conn.close()
        print(f"Analysis metadata saved (domain={domain}, issues={len(issues or [])}, cats={categories})")
    except Exception as e:
        print(f"Error saving analysis metadata: {str(e)}")


# Initialize database on startup
init_db()


@app.route('/')
def home():
    """Serve the main index.html page"""
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


def validate_email(email):
    """
    Validate email and check if it's corporate (not common free domains)
    Returns: (is_valid: bool, error_message: str|None)
    """
    blocked_domains = [
        'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
        'aol.com', 'icloud.com', 'mail.com', 'protonmail.com',
        'live.com', 'msn.com', 'yandex.com', 'zoho.com'
    ]

    # Basic email format validation
    email_pattern = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
    if not email_pattern.match(email):
        return False, "Invalid email format"

    domain = email.split('@')[1].lower()
    if domain in blocked_domains:
        return False, "Please use your company email address"

    return True, None


def verify_recaptcha(token):
    """Verify reCAPTCHA v3 token with Google"""
    # Local dev bypass — set DISABLE_RECAPTCHA=true to skip verification
    if os.environ.get('DISABLE_RECAPTCHA', '').lower() == 'true':
        print("INFO: reCAPTCHA disabled via DISABLE_RECAPTCHA env var (local dev only)")
        return True
    if not RECAPTCHA_SECRET_KEY:
        print("WARNING: RECAPTCHA_SECRET_KEY not set")
        return True  # Allow through in development if not configured

    try:
        response = requests.post(
            'https://www.google.com/recaptcha/api/siteverify',
            data={
                'secret': RECAPTCHA_SECRET_KEY,
                'response': token
            },
            timeout=5
        )

        result = response.json()

        # For reCAPTCHA v3, check the score (0.0 to 1.0)
        # 0.5 is a reasonable threshold - adjust based on your needs
        if result.get('success') and result.get('score', 0) >= 0.5:
            return True

        print(f"reCAPTCHA verification failed: score={result.get('score', 0)}")
        return False

    except Exception as e:
        print(f"Error verifying reCAPTCHA: {str(e)}")
        return False


@app.route('/api/check-document', methods=['POST'])
@limiter.limit("5 per hour")  # Limit to 5 document checks per hour per IP
def check_document():
    """
    Main endpoint for document analysis
    Expects: multipart/form-data with 'document' file, 'email', and 'recaptcha_token'
    """
    try:
        # STEP 1: Verify reCAPTCHA token
        recaptcha_token = request.form.get('recaptcha_token')
        if not recaptcha_token:
            return jsonify({'error': 'reCAPTCHA verification required'}), 400

        if not verify_recaptcha(recaptcha_token):
            return jsonify({'error': 'reCAPTCHA verification failed. Please try again.'}), 400

        # STEP 2: Validate email
        email = request.form.get('email')
        if not email:
            return jsonify({'error': 'Email is required'}), 400

        is_valid, error_msg = validate_email(email)
        if not is_valid:
            return jsonify({'error': error_msg}), 400

        # Save email to database with IP address
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        if ip_address:
            # Take first IP if there are multiple in X-Forwarded-For
            ip_address = ip_address.split(',')[0].strip()
        save_email(email, ip_address)

        # STEP 3: Get and validate uploaded file
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

        # Combine all paragraphs for analysis
        full_text = '\n\n'.join([p['text'] for p in paragraphs])

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

        # Create detailed prompt for Claude — India-specific
        prompt = f"""You are a meticulous Indian commercial-contracts reviewer with deep familiarity with Indian law and Indian SaaS commercial practice.

Analyse the following contract for potential errors, inconsistencies, and problems — both general drafting issues and Indian-jurisdiction-specific issues.

For each issue you find, provide:
1. The paragraph number where the issue occurs
2. A clear description of the issue (one to two sentences)
3. A suggested fix or improvement (concrete language where possible)
4. Severity level (high, medium, or low)
5. A short category tag from this fixed list (choose the single closest match):
   DPDPA, FEMA, GST, STAMP_DUTY, CONTRACT_ACT, IT_ACT, ARBITRATION_ACT, COMPANIES_ACT,
   LIMITATION_ACT, INDEMNITY, LIABILITY, IP, TERMINATION, GOVERNING_LAW, DEFINITIONS,
   AMBIGUITY, CROSS_BORDER, NON_COMPETE, CONFIDENTIALITY, PAYMENT, OTHER

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
- Stamp duty: contracts that require stamping but don't reference it, executed-in-multiple-states issues, missing e-stamp acknowledgment for high-value contracts
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

Contract text:

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

        response = None
        last_err = None
        for model_name in model_chain:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                        max_output_tokens=8192,
                        temperature=0.2,
                    ),
                )
                print(f"INFO: Gemini call succeeded on model={model_name}")
                model_used = model_name
                break
            except Exception as e:
                last_err = e
                # Retry the chain on availability/quota errors; bail on others
                err_str = str(e)
                if any(code in err_str for code in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED")):
                    print(f"WARN: {model_name} unavailable ({type(e).__name__}); trying next in chain")
                    continue
                # Non-retryable: re-raise
                raise

        if response is None:
            print(f"ERROR: all Gemini models exhausted. Last error: {last_err}")
            return jsonify({'error': 'Gemini is temporarily overloaded across all models. Try again in a few minutes.'}), 503

        # Parse Gemini's response (response_mime_type=application/json returns clean JSON)
        response_text = response.text or ""

        # Belt-and-suspenders: extract JSON if the model added prose
        json_start = response_text.find('{')
        json_end = response_text.rfind('}') + 1
        if json_start != -1 and json_end > json_start:
            response_text = response_text[json_start:json_end]

        analysis = json.loads(response_text)

        # STEP 6: Persist metadata (no contract text, no issue text — counts + tags only)
        duration = round(time.time() - analysis_start, 2)
        save_analysis_metadata(
            email=email,
            ip_address=ip_address,
            file_size_bytes=len(file_content),
            paragraph_count=len(paragraphs),
            issues=analysis.get('issues', []),
            model_used=model_used,
            duration_seconds=duration,
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


@app.route('/issue/<share_id>')
def view_shared_issue(share_id):
    """View a single shared issue"""
    if not DATABASE_URL:
        return "Sharing feature not available", 503

    try:
        conn = psycopg.connect(DATABASE_URL)
        cur = conn.cursor()

        cur.execute(
            """SELECT paragraph_index, issue, suggestion, severity, created_at
               FROM shared_issues
               WHERE share_id = %s""",
            (share_id,)
        )

        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return "Issue not found", 404

        # Simple HTML page to display the issue
        html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta property="og:title" content="Contract Error Found">
    <meta property="og:description" content="{escape(row[1][:150])}...">
    <meta name="twitter:card" content="summary_large_image">
    <title>Contract Error - AI Contract Checker</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: linear-gradient(135deg, #0f1f3d 0%, #1a2f52 100%);
            color: white;
            padding: 40px 20px;
            min-height: 100vh;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
            background: #1a3a5c;
            border-radius: 12px;
            padding: 40px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }}
        h1 {{
            font-size: 32px;
            margin-bottom: 10px;
            color: #93b5d8;
        }}
        .meta {{
            color: #9ca3af;
            font-size: 14px;
            margin-bottom: 30px;
        }}
        .severity {{
            display: inline-block;
            padding: 4px 12px;
            border-radius: 4px;
            font-weight: 600;
            font-size: 12px;
            text-transform: uppercase;
            margin-bottom: 20px;
        }}
        .severity.high {{ background: #dc2626; }}
        .severity.medium {{ background: #f59e0b; }}
        .severity.low {{ background: #10b981; }}
        .section {{
            margin-bottom: 30px;
        }}
        .label {{
            color: #93b5d8;
            font-weight: 600;
            margin-bottom: 8px;
            font-size: 14px;
            text-transform: uppercase;
        }}
        .content {{
            background: rgba(0,0,0,0.2);
            padding: 16px;
            border-radius: 6px;
            line-height: 1.6;
        }}
        .cta {{
            text-align: center;
            margin-top: 40px;
        }}
        .button {{
            display: inline-block;
            background: #93b5d8;
            color: #0f1f3d;
            padding: 14px 32px;
            border-radius: 6px;
            text-decoration: none;
            font-weight: 600;
            transition: all 0.2s;
        }}
        .button:hover {{
            background: #a8c5e0;
            transform: translateY(-2px);
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>Contract Error Found</h1>
        <div class="meta">
            Paragraph {row[0]} • Shared on {row[4].strftime('%B %d, %Y')}
        </div>
        
        <div class="severity {row[3]}">{row[3]} severity</div>
        
        <div class="section">
            <div class="label">Issue</div>
            <div class="content">{escape(row[1])}</div>
        </div>
        
        <div class="section">
            <div class="label">💡 Suggested Fix</div>
            <div class="content">{escape(row[2])}</div>
        </div>
        
        <div class="cta">
            <a href="/" class="button">Check Your Contracts</a>
        </div>
    </div>
</body>
</html>
"""
        return html

    except Exception as e:
        print(f"Error viewing shared issue: {str(e)}")
        return "Error loading issue", 500


@app.route('/gallery')
def gallery():
    """Public gallery of shared issues"""
    html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Gallery - AI Contract Checker</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: linear-gradient(135deg, #0f1f3d 0%, #1a2f52 100%);
            color: white;
            padding: 40px 20px;
            min-height: 100vh;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
        }

        .header {
            text-align: center;
            margin-bottom: 60px;
        }

        .logo {
            font-size: 48px;
            font-weight: 700;
            margin-bottom: 12px;
        }

        .tagline {
            color: #93b5d8;
            font-size: 18px;
            margin-bottom: 20px;
        }

        .cta-link {
            display: inline-block;
            background: #93b5d8;
            color: #0f1f3d;
            padding: 12px 24px;
            border-radius: 6px;
            text-decoration: none;
            font-weight: 600;
            margin-top: 10px;
            transition: all 0.2s;
        }

        .cta-link:hover {
            background: #a8c5e0;
        }

        .gallery-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
            gap: 24px;
            margin-bottom: 40px;
        }

        .issue-card {
            background: #1a3a5c;
            border-radius: 8px;
            padding: 24px;
            cursor: pointer;
            transition: all 0.2s;
            border-left: 4px solid #93b5d8;
        }

        .issue-card:hover {
            transform: translateY(-4px);
            box-shadow: 0 8px 24px rgba(0,0,0,0.3);
        }

        .issue-card.high {
            border-left-color: #dc2626;
        }

        .issue-card.medium {
            border-left-color: #f59e0b;
        }

        .issue-card.low {
            border-left-color: #10b981;
        }

        .issue-location {
            color: #93b5d8;
            font-size: 14px;
            font-weight: 600;
            margin-bottom: 12px;
        }

        .issue-description {
            color: white;
            font-size: 16px;
            margin-bottom: 12px;
            line-height: 1.5;
        }

        .issue-suggestion {
            color: #d1d5db;
            font-size: 14px;
            line-height: 1.5;
            margin-bottom: 12px;
        }

        .issue-date {
            color: #9ca3af;
            font-size: 12px;
        }

        .loading {
            text-align: center;
            padding: 60px 20px;
            font-size: 18px;
            color: #93b5d8;
        }

        .error-message {
            background: #dc2626;
            color: white;
            padding: 16px;
            border-radius: 6px;
            margin-bottom: 20px;
            text-align: center;
        }

        .pagination {
            display: flex;
            justify-content: center;
            gap: 10px;
            margin-top: 40px;
            flex-wrap: wrap;
        }

        .pagination button {
            padding: 10px 16px;
            background: rgba(255, 255, 255, 0.1);
            color: white;
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 6px;
            cursor: pointer;
            font-size: 14px;
            transition: all 0.2s;
        }

        .pagination button:hover:not(:disabled) {
            background: rgba(255, 255, 255, 0.2);
        }

        .pagination button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        .pagination button.active {
            background: #93b5d8;
            color: #0f1f3d;
            border-color: #93b5d8;
        }

        .stats {
            text-align: center;
            margin-bottom: 30px;
            color: #93b5d8;
            font-size: 16px;
        }

        .footer {
            text-align: center;
            margin-top: 60px;
            padding: 20px;
            font-size: 14px;
            color: #9ca3af;
            border-top: 1px solid rgba(255, 255, 255, 0.1);
        }

        @media (max-width: 768px) {
            .gallery-grid {
                grid-template-columns: 1fr;
            }

            .logo {
                font-size: 36px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">Contract Error Gallery</div>
            <div class="tagline">
                Hall of Shame: Real Contract Errors Found
            </div>
            <a href="/" class="cta-link">Check Your Contract</a>
        </div>

        <div id="stats" class="stats"></div>

        <div id="error-container"></div>

        <div id="loading" class="loading">Loading issues...</div>

        <div id="gallery" class="gallery-grid"></div>

        <div id="pagination" class="pagination"></div>

        <div class="footer">
            © 2026 - All Rights Reserved | This tool is not legal advice.
        </div>
    </div>

    <script>
        let currentPage = 1;
        const perPage = 20;

        async function loadGallery(page = 1) {
            try {
                document.getElementById('loading').style.display = 'block';
                document.getElementById('gallery').innerHTML = '';
                document.getElementById('error-container').innerHTML = '';

                const response = await fetch(`/api/gallery?page=${page}&per_page=${perPage}`);
                const data = await response.json();

                document.getElementById('loading').style.display = 'none';

                if (!data.success) {
                    throw new Error(data.error || 'Failed to load gallery');
                }

                if (data.issues.length === 0) {
                    document.getElementById('gallery').innerHTML = '<div class="loading">No issues shared yet. Be the first!</div>';
                    return;
                }

                // Update stats
                document.getElementById('stats').innerHTML = `
                    Showing ${data.issues.length} of ${data.total} shared contract errors
                `;

                // Render issues
                const gallery = document.getElementById('gallery');
                data.issues.forEach(issue => {
                    const card = document.createElement('div');
                    card.className = `issue-card ${issue.severity}`;
                    card.onclick = () => window.open(issue.shareUrl, '_blank');

                    const date = new Date(issue.createdAt);
                    const dateStr = date.toLocaleDateString('en-US', { 
                        month: 'short', 
                        day: 'numeric', 
                        year: 'numeric' 
                    });

                    card.innerHTML = `
                        <div class="issue-location">📍 Paragraph ${issue.paragraphIndex}</div>
                        <div class="issue-description">
                            <strong>Issue:</strong> ${escapeHtml(issue.issue)}
                        </div>
                        <div class="issue-suggestion">
                            💡 ${escapeHtml(issue.suggestion)}
                        </div>
                        <div class="issue-date">Shared on ${dateStr}</div>
                    `;

                    gallery.appendChild(card);
                });

                // Render pagination
                renderPagination(data.page, data.totalPages);

            } catch (error) {
                console.error('Error loading gallery:', error);
                document.getElementById('loading').style.display = 'none';
                document.getElementById('error-container').innerHTML = `
                    <div class="error-message">
                        ⚠️ Failed to load gallery. Please try again later.
                    </div>
                `;
            }
        }

        function renderPagination(currentPage, totalPages) {
            const pagination = document.getElementById('pagination');
            pagination.innerHTML = '';

            if (totalPages <= 1) return;

            // Previous button
            const prevBtn = document.createElement('button');
            prevBtn.textContent = '← Previous';
            prevBtn.disabled = currentPage === 1;
            prevBtn.onclick = () => loadGallery(currentPage - 1);
            pagination.appendChild(prevBtn);

            // Page numbers (show max 5)
            const maxVisible = 5;
            let startPage = Math.max(1, currentPage - Math.floor(maxVisible / 2));
            let endPage = Math.min(totalPages, startPage + maxVisible - 1);

            if (endPage - startPage < maxVisible - 1) {
                startPage = Math.max(1, endPage - maxVisible + 1);
            }

            for (let i = startPage; i <= endPage; i++) {
                const pageBtn = document.createElement('button');
                pageBtn.textContent = i;
                pageBtn.className = i === currentPage ? 'active' : '';
                pageBtn.onclick = () => loadGallery(i);
                pagination.appendChild(pageBtn);
            }

            // Next button
            const nextBtn = document.createElement('button');
            nextBtn.textContent = 'Next →';
            nextBtn.disabled = currentPage === totalPages;
            nextBtn.onclick = () => loadGallery(currentPage + 1);
            pagination.appendChild(nextBtn);
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // Load initial page
        loadGallery(1);
    </script>
</body>
</html>
    """
    return html


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
