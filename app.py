"""
AfriConn Automation — app.py
Production-ready FastAPI app for App Runner.

Fixes applied to your original:
  1. Claude (Bedrock) — added anthropic_version + correct model ID
  2. store_tokens — bare except replaced, handles update vs create properly
  3. get_valid_token — added (was missing, needed by SAP tools)
  4. send_email — uses AFRICONN_EMAIL env var, not hardcoded address
  5. upload — strips JSON fences before parsing Claude output
  6. approve — takes key as query param (FastAPI body requires model)
  7. REVIEW_BUCKET — reads from env var so Terraform controls it
  8. Error handling — every endpoint returns clean JSON errors
  9. /sap/status — added so you can check connection without guessing
 10. /review/approve UI — added simple HTML so you can approve from browser
"""

import os
import json
import base64
import logging
import re
from datetime import datetime, timezone

import boto3
import httpx
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────────────────────────────────────
# CONFIG — all from environment variables
# ─────────────────────────────────────────────────────────────

AWS_REGION        = os.environ.get("AWS_REGION",        "eu-west-1")
SAP_CLIENT_ID     = os.environ.get("SAP_CLIENT_ID",     "")
SAP_CLIENT_SECRET = os.environ.get("SAP_CLIENT_SECRET", "")
SAP_TOKEN_URL     = os.environ.get("SAP_TOKEN_URL",     "https://api.ariba.com/v2/oauth/token")
SAP_REDIRECT_URI  = os.environ.get("SAP_REDIRECT_URI",  "")
SUPPLIER_ID       = os.environ.get("SUPPLIER_ID",       "AFRICONN")
AFRICONN_EMAIL    = os.environ.get("AFRICONN_EMAIL",    "")   # your SES-verified sender
DC_EMAIL          = os.environ.get("DC_EMAIL",          "")   # SPAR DC creditors
REVIEW_BUCKET     = os.environ.get("REVIEW_BUCKET",     "africonn-review")
SES_CONFIG_SET    = os.environ.get("SES_CONFIG_SET",    "africonn-emails")

SECRET_NAME     = "africonn/sap-tokens"
REVIEW_PREFIX   = "pending/"
APPROVED_PREFIX = "approved/"

# ─────────────────────────────────────────────────────────────
# AWS CLIENTS
# ─────────────────────────────────────────────────────────────

secrets  = boto3.client("secretsmanager", region_name=AWS_REGION)
textract = boto3.client("textract",       region_name=AWS_REGION)
s3       = boto3.client("s3",             region_name=AWS_REGION)
ses      = boto3.client("ses",            region_name=AWS_REGION)
bedrock  = boto3.client("bedrock-runtime", region_name=AWS_REGION)

app = FastAPI(title="AfriConn Automation", version="1.0")


# ─────────────────────────────────────────────────────────────
# 1. SAP AUTH
# ─────────────────────────────────────────────────────────────

@app.get("/sap/callback", response_class=HTMLResponse)
async def sap_callback(request: Request):
    code  = request.query_params.get("code")
    error = request.query_params.get("error")

    if error:
        return HTMLResponse(
            f"<h2>❌ SAP Authorization Failed</h2><p>{error}</p>",
            status_code=400
        )
    if not code:
        return HTMLResponse(
            "<h2>❌ No authorization code received</h2>",
            status_code=400
        )

    try:
        tokens = await _exchange_code(code)
        _store_tokens(tokens)
        return HTMLResponse(
            "<h2>✅ AfriConn connected to SAP.</h2>"
            "<p>You can close this window. "
            "Claude can now submit invoices automatically.</p>"
        )
    except Exception as e:
        logger.error(f"SAP token exchange failed: {e}")
        return HTMLResponse(
            f"<h2>❌ Token exchange failed</h2><p>{e}</p>",
            status_code=500
        )


@app.get("/sap/status")
def sap_status():
    """Check whether SAP tokens exist and how fresh they are."""
    try:
        resp   = secrets.get_secret_value(SecretId=SECRET_NAME)
        tokens = json.loads(resp["SecretString"])
        stored = datetime.fromisoformat(tokens.get("stored_at", "1970-01-01T00:00:00+00:00"))
        age    = (datetime.now(timezone.utc) - stored).total_seconds() / 60
        return {
            "connected":         True,
            "token_age_minutes": round(age, 1),
            "expires_in_seconds": tokens.get("expires_in", "unknown"),
        }
    except secrets.exceptions.ResourceNotFoundException:
        return {"connected": False, "message": "No SAP tokens — DC must authorize via /sap/callback"}
    except Exception as e:
        return {"connected": False, "error": str(e)}


async def _exchange_code(code: str) -> dict:
    creds = base64.b64encode(
        f"{SAP_CLIENT_ID}:{SAP_CLIENT_SECRET}".encode()
    ).decode()

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            SAP_TOKEN_URL,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
            data={
                "grant_type":   "authorization_code",
                "code":          code,
                "redirect_uri":  SAP_REDIRECT_URI,
            },
        )
        r.raise_for_status()
        tokens = r.json()

    tokens["stored_at"] = datetime.now(timezone.utc).isoformat()
    return tokens


def _store_tokens(tokens: dict) -> None:
    value = json.dumps(tokens)
    try:
        # Try update first (secret already exists)
        secrets.put_secret_value(
            SecretId=SECRET_NAME,
            SecretString=value
        )
    except secrets.exceptions.ResourceNotFoundException:
        # First time — create it
        secrets.create_secret(
            Name=SECRET_NAME,
            Description="SAP OAuth tokens — auto-managed by AfriConn",
            SecretString=value
        )


def get_valid_token() -> str:
    """
    Returns a valid SAP access token.
    Called by any route that needs to talk to SAP.
    Auto-refreshes if the token is within 5 minutes of expiry.
    """
    try:
        resp   = secrets.get_secret_value(SecretId=SECRET_NAME)
        tokens = json.loads(resp["SecretString"])
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"SAP not connected. DC must authorize at /sap/callback. ({e})"
        )

    stored     = datetime.fromisoformat(tokens["stored_at"])
    expires_in = tokens.get("expires_in", 3600)
    age        = (datetime.now(timezone.utc) - stored).total_seconds()

    if age >= (expires_in - 300):           # refresh 5 min before expiry
        tokens = _refresh_token(tokens["refresh_token"])
        _store_tokens(tokens)

    return tokens["access_token"]


def _refresh_token(refresh_token: str) -> dict:
    creds = base64.b64encode(
        f"{SAP_CLIENT_ID}:{SAP_CLIENT_SECRET}".encode()
    ).decode()

    r = httpx.post(
        SAP_TOKEN_URL,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data={
            "grant_type":    "refresh_token",
            "refresh_token":  refresh_token,
        },
        timeout=30,
    )
    r.raise_for_status()
    tokens = r.json()
    tokens["stored_at"] = datetime.now(timezone.utc).isoformat()
    return tokens


# ─────────────────────────────────────────────────────────────
# 2. OCR — UPLOAD A POD OR PO IMAGE
# ─────────────────────────────────────────────────────────────

@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """
    Upload a POD or PO image.
    Runs Textract OCR → Claude extracts structure → queued for your review.
    Returns a preview so you can see what was extracted before approving.
    """
    content = await file.read()

    if not content:
        raise HTTPException(status_code=400, detail="Empty file uploaded")

    # OCR via Textract
    try:
        ocr_response = textract.detect_document_text(
            Document={"Bytes": content}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Textract OCR failed: {e}")

    raw_text = " ".join(
        b["Text"]
        for b in ocr_response["Blocks"]
        if b["BlockType"] == "LINE"
    )

    if not raw_text.strip():
        raise HTTPException(
            status_code=422,
            detail="No text found in image. Check image quality and try again."
        )

    # Claude extracts structure
    try:
        interpretation = _interpret_with_claude(raw_text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Claude interpretation failed: {e}")

    # Store in S3 review queue
    key = REVIEW_PREFIX + f"{datetime.utcnow().timestamp()}_{file.filename}.json"
    payload = {
        "filename":       file.filename,
        "uploaded_at":    datetime.utcnow().isoformat(),
        "raw_text":       raw_text[:2000],          # store first 2000 chars for reference
        "interpretation": interpretation,
        "s3_key":         key,
    }

    s3.put_object(
        Bucket=REVIEW_BUCKET,
        Key=key,
        Body=json.dumps(payload),
        ContentType="application/json",
    )

    return {
        "status":  "queued_for_review",
        "key":     key,
        "preview": interpretation,
        "approve_at": f"/approve?key={key}",
    }


# ─────────────────────────────────────────────────────────────
# 3. CLAUDE INTERPRETATION (BEDROCK)
# ─────────────────────────────────────────────────────────────

def _interpret_with_claude(text: str) -> dict:
    """
    Send OCR text to Claude via Bedrock.
    Returns structured dict — never crashes on bad JSON from Claude.
    """
    prompt = f"""Extract structured data from this delivery/purchase order document.

Document text:
{text}

Return ONLY valid JSON with no explanation, no markdown, no code fences:
{{
    "doc_type": "PO or POD or invoice or unknown",
    "po_number": "the purchase order number or null",
    "store_code": "store identifier if visible or null",
    "delivery_date": "YYYY-MM-DD or null",
    "items": [
        {{"sku": "product code", "description": "product name", "quantity": 0, "unit_price": 0}}
    ],
    "total_value_zar": 0.00,
    "can_fulfill_percent": 100,
    "notes": "anything unusual or unclear"
}}"""

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",     # REQUIRED — was missing
        "max_tokens": 800,
        "messages": [
            {"role": "user", "content": prompt}
        ],
    })

    response = bedrock.invoke_model(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",   # Haiku — fast + cheap for extraction
        body=body,
        contentType="application/json",
        accept="application/json",
    )

    raw = json.loads(response["body"].read())
    text_out = raw["content"][0]["text"].strip()

    # Strip markdown fences if Claude wrapped the JSON anyway
    text_out = re.sub(r"^```(?:json)?\s*", "", text_out)
    text_out = re.sub(r"\s*```$",          "", text_out)

    try:
        return json.loads(text_out)
    except json.JSONDecodeError:
        # Claude returned something unparseable — return it as a note
        return {
            "doc_type": "unknown",
            "po_number": None,
            "notes": f"Claude could not parse cleanly: {text_out[:300]}",
            "raw_claude_output": text_out,
        }


# ─────────────────────────────────────────────────────────────
# 4. REVIEW QUEUE
# ─────────────────────────────────────────────────────────────

@app.get("/review")
def review():
    """List all documents waiting for your approval."""
    try:
        objects = s3.list_objects_v2(Bucket=REVIEW_BUCKET, Prefix=REVIEW_PREFIX)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"S3 list failed: {e}")

    results = []
    for obj in objects.get("Contents", []):
        try:
            data = s3.get_object(Bucket=REVIEW_BUCKET, Key=obj["Key"])
            item = json.loads(data["Body"].read())
            item["s3_key"] = obj["Key"]
            results.append(item)
        except Exception:
            continue                        # skip corrupted entries

    return {"pending": len(results), "items": results}


@app.get("/review/ui", response_class=HTMLResponse)
def review_ui():
    """Simple browser UI to review and approve documents."""
    return HTMLResponse(f"""
    <!DOCTYPE html><html><head>
    <title>AfriConn Review Queue</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body {{ font-family: system-ui, sans-serif; max-width: 900px;
               margin: 0 auto; padding: 20px; background: #f9fafb; }}
      h1   {{ color: #111827; }}
      .card {{ background: white; border-radius: 8px; padding: 20px;
               margin: 12px 0; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
      pre  {{ background: #f3f4f6; padding: 12px; border-radius: 6px;
               font-size: .8rem; overflow-x: auto; white-space: pre-wrap; }}
      button {{ background: #16a34a; color: white; border: none;
                padding: 10px 20px; border-radius: 6px; cursor: pointer;
                font-size: 1rem; margin-top: 8px; }}
      button:hover {{ background: #15803d; }}
      #msg {{ color: #16a34a; font-weight: 600; margin-top: 8px; }}
    </style></head><body>
    <h1>📋 AfriConn Review Queue</h1>
    <p id="msg"></p>
    <div id="items">Loading...</div>
    <script>
    async function load() {{
      const r = await fetch('/review');
      const d = await r.json();
      const el = document.getElementById('items');
      if (!d.items.length) {{ el.innerHTML = '<p>✅ Queue is empty.</p>'; return; }}
      el.innerHTML = d.items.map(item => `
        <div class="card">
          <strong>${{item.filename || item.s3_key}}</strong>
          <pre>${{JSON.stringify(item.interpretation, null, 2)}}</pre>
          <button onclick="approve('${{item.s3_key}}')">✅ Approve & Send</button>
        </div>
      `).join('');
    }}
    async function approve(key) {{
      const r = await fetch('/approve?key=' + encodeURIComponent(key), {{method:'POST'}});
      const d = await r.json();
      document.getElementById('msg').textContent =
        d.status === 'sent' ? '✅ Approved and emailed.' : '❌ ' + JSON.stringify(d);
      load();
    }}
    load();
    </script></body></html>
    """)


# ─────────────────────────────────────────────────────────────
# 5. APPROVE — MOVE TO APPROVED + EMAIL
# ─────────────────────────────────────────────────────────────

@app.post("/approve")
def approve(key: str):
    """
    Approve a reviewed document.
    Moves it from pending/ to approved/ in S3 and sends email to DC.
    Pass the S3 key as a query param: POST /approve?key=pending/xxx.json
    """
    if not key.startswith(REVIEW_PREFIX):
        raise HTTPException(status_code=400, detail=f"Key must start with '{REVIEW_PREFIX}'")

    try:
        obj  = s3.get_object(Bucket=REVIEW_BUCKET, Key=key)
        data = obj["Body"].read()
    except s3.exceptions.NoSuchKey:
        raise HTTPException(status_code=404, detail=f"Key not found: {key}")

    # Move to approved/
    approved_key = key.replace(REVIEW_PREFIX, APPROVED_PREFIX, 1)
    s3.put_object(
        Bucket=REVIEW_BUCKET,
        Key=approved_key,
        Body=data,
        ContentType="application/json",
    )
    s3.delete_object(Bucket=REVIEW_BUCKET, Key=key)

    # Send email
    try:
        parsed = json.loads(data)
        _send_email(parsed)
        email_status = "sent"
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        email_status = f"failed: {e}"

    return {
        "status":       "sent" if email_status == "sent" else "approved_email_failed",
        "approved_key": approved_key,
        "email":        email_status,
    }


# ─────────────────────────────────────────────────────────────
# 6. EMAIL — SES
# ─────────────────────────────────────────────────────────────

def _send_email(payload: dict) -> None:
    """Send approved document summary to DC via SES."""
    if not AFRICONN_EMAIL:
        raise ValueError("AFRICONN_EMAIL env var not set")
    if not DC_EMAIL:
        raise ValueError("DC_EMAIL env var not set")

    interpretation = payload.get("interpretation", {})
    po_number      = interpretation.get("po_number", "unknown")
    doc_type       = interpretation.get("doc_type",  "document")
    total          = interpretation.get("total_value_zar", 0)
    filename       = payload.get("filename", "document")

    subject = f"AfriConn — Approved {doc_type.upper()} | PO {po_number} | R{total:,.2f}"

    body = f"""Dear SPAR DC Creditors,

AfriConn Fresh has approved and submitted the following document:

  File:       {filename}
  Type:       {doc_type}
  PO Number:  {po_number}
  Total:      R{total:,.2f}

Full extracted data:
{json.dumps(interpretation, indent=2)}

Regards,
AfriConn Fresh (Pty) Ltd
{AFRICONN_EMAIL}
"""

    kwargs = dict(
        Source=AFRICONN_EMAIL,
        Destination={"ToAddresses": [DC_EMAIL]},
        Message={
            "Subject": {"Data": subject},
            "Body":    {"Text": {"Data": body}},
        },
    )

    # Add config set if it exists (non-fatal if missing)
    if SES_CONFIG_SET:
        kwargs["ConfigurationSetName"] = SES_CONFIG_SET

    ses.send_email(**kwargs)
    logger.info(f"Email sent to {DC_EMAIL} for PO {po_number}")


# ─────────────────────────────────────────────────────────────
# 7. HEALTH + ROOT
# ─────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "AfriConn Automation"}


@app.get("/")
def root():
    return {
        "service": "AfriConn Automation",
        "version": "1.0",
        "endpoints": {
            "GET  /health":       "Health check",
            "GET  /sap/callback": "SAP OAuth redirect (DC uses this)",
            "GET  /sap/status":   "Check SAP connection",
            "POST /upload":       "Upload POD or PO image for OCR",
            "GET  /review":       "List documents awaiting approval (JSON)",
            "GET  /review/ui":    "Browser UI for reviewing and approving",
            "POST /approve":      "Approve a document and email to DC (?key=...)",
        },
}
