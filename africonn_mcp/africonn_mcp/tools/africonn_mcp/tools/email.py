"""
tools/email.py  (SES version — replaces Outlook/Exchange)
==========================================================
Drop-in replacement for the win32com/exchangelib version.

NOTHING CHANGES for Claude or the MCP server:
  - Same tool name:       bundle_and_email
  - Same input model:     BundleEmailInput
  - Same output JSON:     {sent, method, to, subject, attachments ...}
  - Same subject format:  "AfriConn Fresh | Dropshipment Remittance | ..."

What changes:
  - Sends via AWS SES instead of Outlook
  - Attachments read from local disk (same as before)
  - No EXCHANGE_SERVER / EXCHANGE_PASSWORD needed
  - Works anywhere — App Runner, laptop, Batch job

Prerequisites (you said steps 1 & 2 are done):
  ✓ Domain verified in SES console
  ✓ DNS records (DKIM CNAMEs + SPF TXT) added
  □ Production access approved (Step 3 — submit the request if not done)
      Until approved: only sends to verified addresses (fine for testing)

Environment variables needed in App Runner:
  AFRICONN_EMAIL   — your verified sender, e.g. invoices@africonn.co.za
  DC_EMAIL         — SPAR DC email, e.g. dropshipments@spar.co.za
  AFRICONN_CC      — your CC address (optional)
  AWS_REGION       — af-south-1
  SES_CONFIG_SET   — africonn-emails  (created by Terraform)
"""

import json
import os
import boto3
import logging
from pathlib import Path
from typing import Optional
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field, ConfigDict, field_validator

from utils.ledger import SessionLedger

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────
DC_EMAIL_DEFAULT = os.getenv("DC_EMAIL",       "dropshipments@spar.co.za")
CC_EMAIL_DEFAULT = os.getenv("AFRICONN_CC",    "")
FROM_EMAIL       = os.getenv("AFRICONN_EMAIL", ""hm@afri-conn.com)
AWS_REGION       = os.getenv("AWS_REGION",     "eu-west-1")
CONFIG_SET       = os.getenv("SES_CONFIG_SET", "africonn-emails")

# SES client — boto3 picks up IAM role automatically on App Runner
ses = boto3.client("ses", region_name=AWS_REGION)


# ── Input model — identical to the old version ────────────────
class BundleEmailInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    excel_path: str = Field(
        ...,
        description="Path to the Excel remittance file produced by build_remittance. "
                    "Example: '/tmp/africonn_remittance/AfriConn_Remittance_2026-W14.xlsx'",
    )
    week_ref: str = Field(
        ...,
        description="ISO week reference, e.g. '2026-W14'.",
        pattern=r"^\d{4}-W\d{2}$",
    )
    total_value_zar: float = Field(
        ...,
        description="Total invoice value in ZAR — used in the subject line.",
        gt=0,
    )
    dc_email: Optional[str] = Field(
        default=None,
        description=f"DC creditors email. Defaults to DC_EMAIL env var ({DC_EMAIL_DEFAULT}).",
    )
    cc_email: Optional[str] = Field(
        default=None,
        description="CC email address. Defaults to AFRICONN_CC env var.",
    )
    extra_attachments: Optional[list[str]] = Field(
        default_factory=list,
        description="Additional file paths to attach (PDF invoices, POD scans).",
    )
    dry_run: bool = Field(
        default=False,
        description="If true, compose the email but do NOT send — returns a preview.",
    )

    @field_validator("total_value_zar")
    @classmethod
    def round_to_cents(cls, v: float) -> float:
        return round(v, 2)


# ── Email body — identical to old version ────────────────────
def _build_body(week_ref: str, total: float, matched_count: int) -> str:
    return f"""Dear {DC_EMAIL_DEFAULT.split('@')[0].replace('.', ' ').title()},

Please find attached the AfriConn Fresh weekly dropshipment remittance schedule \
for {week_ref}.

Summary:
  • Documents submitted:    {matched_count}
  • Total value (excl VAT): R {total:,.2f}
  • Week reference:         {week_ref}

All PODs included in this bundle carry the required store stamp and signature.
Please refer to the cover sheet for the document cut-off date and any notes.

Kindly acknowledge receipt and advise of any discrepancies at your earliest \
convenience so that any issues can be resolved before the payment run.

Documents received after the stated cut-off will be processed in the next \
payment month as per the agreed dropshipment procedure.

Regards,
AfriConn Fresh (Pty) Ltd
Email: {FROM_EMAIL or '[insert]'}

---
This email was generated automatically by the AfriConn MCP document workflow.
"""


# ── SES sender ────────────────────────────────────────────────
def _send_via_ses(
    to: str,
    cc: str,
    subject: str,
    body: str,
    attachments: list[str],
    week_ref: str,
) -> tuple[bool, str]:
    """
    Send via AWS SES using raw email (required for attachments).
    Returns (success: bool, message_id_or_error: str).
    """
    if not FROM_EMAIL:
        return False, (
            "AFRICONN_EMAIL environment variable not set. "
            "Add it to App Runner environment variables."
        )

    # Build MIME message
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = f"AfriConn Fresh <{FROM_EMAIL}>"
    msg["To"]      = to
    if cc:
        msg["CC"] = cc

    # Plain text body
    msg.attach(MIMEText(body, "plain"))

    # Attach files
    missing = []
    for path in attachments:
        p = Path(path)
        if not p.exists():
            missing.append(path)
            continue

        # Detect MIME type by extension
        suffix = p.suffix.lower()
        if suffix == ".xlsx":
            mime_type = (
                "application",
                "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        elif suffix == ".pdf":
            mime_type = ("application", "pdf")
        else:
            mime_type = ("application", "octet-stream")

        part = MIMEBase(*mime_type)
        part.set_payload(p.read_bytes())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f'attachment; filename="{p.name}"',
        )
        msg.attach(part)

    # Build destinations list
    destinations = [to]
    if cc:
        destinations.append(cc)

    # Send
    try:
        response = ses.send_raw_email(
            Source=FROM_EMAIL,
            Destinations=destinations,
            RawMessage={"Data": msg.as_bytes()},
            ConfigurationSetName=CONFIG_SET,
            Tags=[
                {"Name": "EmailType", "Value": "remittance"},
                {"Name": "WeekRef",   "Value": week_ref},
            ],
        )
        msg_id = response["MessageId"]
        if missing:
            logger.warning(f"Sent but {len(missing)} attachment(s) not found: {missing}")
        return True, msg_id

    except ses.exceptions.MessageRejected as e:
        return False, f"SES rejected message: {e}"
    except ses.exceptions.MailFromDomainNotVerifiedException:
        return False, (
            f"Sender domain not verified in SES. "
            f"Verify {FROM_EMAIL.split('@')[1]} in the SES console first."
        )
    except ses.exceptions.ConfigurationSetDoesNotExistException:
        # Non-fatal — retry without config set
        logger.warning(f"Config set '{CONFIG_SET}' not found — sending without it")
        try:
            response = ses.send_raw_email(
                Source=FROM_EMAIL,
                Destinations=destinations,
                RawMessage={"Data": msg.as_bytes()},
            )
            return True, response["MessageId"]
        except Exception as e2:
            return False, str(e2)
    except Exception as e:
        return False, str(e)


# ── Tool function — same signature as before ──────────────────
async def bundle_and_email(params: BundleEmailInput, ctx: Context) -> str:
    """
    Attach the Excel remittance schedule and any additional PDFs and send
    to the SPAR DC Eastern Cape creditors inbox via AWS SES.

    Identical interface to the Outlook version — Claude calls it the same way.
    """
    to_email = params.dc_email or DC_EMAIL_DEFAULT
    cc_email = params.cc_email or CC_EMAIL_DEFAULT

    subject = (
        f"AfriConn Fresh | Dropshipment Remittance | "
        f"{params.week_ref} | R {params.total_value_zar:,.2f}"
    )

    # Get matched doc count from ledger for email body
    try:
        ledger: SessionLedger = ctx.request_context.lifespan_state["ledger"]
        from utils.ledger import DocStatus
        matched_count = len(ledger.by_status(DocStatus.BUNDLED))
    except Exception:
        matched_count = 0

    body = _build_body(params.week_ref, params.total_value_zar, matched_count)

    # Collect attachments
    attachments: list[str] = [params.excel_path]
    for path in params.extra_attachments or []:
        if Path(path).exists():
            attachments.append(path)
        else:
            await ctx.log_info(f"Attachment not found, skipping: {path}")

    attachment_names = [Path(a).name for a in attachments]

    # Dry run — preview only
    if params.dry_run:
        return json.dumps({
            "sent":         False,
            "dry_run":      True,
            "to":           to_email,
            "cc":           cc_email or None,
            "subject":      subject,
            "body_preview": body[:300] + "...",
            "attachments":  attachment_names,
            "method":       "AWS SES (would send)",
            "next_step":    "Set dry_run=false to send for real.",
        }, indent=2)

    await ctx.report_progress(0.3, f"Sending via SES to {to_email}…")

    sent, result = _send_via_ses(
        to=to_email,
        cc=cc_email,
        subject=subject,
        body=body,
        attachments=attachments,
        week_ref=params.week_ref,
    )

    if sent:
        await ctx.report_progress(1.0, "Email sent successfully via SES")
        return json.dumps({
            "sent":       True,
            "method":     "AWS SES",
            "message_id": result,
            "to":         to_email,
            "cc":         cc_email or None,
            "subject":    subject,
            "attachments": attachment_names,
            "dry_run":    False,
            "next_step":  (
                "Monitor inbox for DC acknowledgement. "
                "If no response within 2 business days, follow up with "
                f"{to_email}."
            ),
        }, indent=2)
    else:
        return json.dumps({
            "sent":  False,
            "error": result,
            "help":  _ses_help(result),
            "subject":     subject,
            "attachments": attachment_names,
            "manual_fallback": (
                f"If urgent: email {to_email} manually with the Excel attached."
            ),
        }, indent=2)


def _ses_help(error: str) -> str:
    """Return a plain-English hint based on the error message."""
    if "not verified" in error.lower():
        return (
            "Your sender domain is not verified in SES. "
            "Go to AWS Console → SES → Verified identities → check your domain shows 'Verified'."
        )
    if "sandbox" in error.lower() or "not authorized" in error.lower():
        return (
            "SES is still in sandbox mode — you can only send to verified addresses. "
            "Submit a production access request: AWS Console → SES → Account dashboard → "
            "Request production access."
        )
    if "configuration set" in error.lower():
        return (
            "The SES configuration set 'africonn-emails' was not found. "
            "Run terraform apply to create it, or remove SES_CONFIG_SET env var."
        )
    if "AFRICONN_EMAIL" in error:
        return (
            "Set AFRICONN_EMAIL in your App Runner environment variables. "
            "It must be your SES-verified sender address."
        )
    return (
        "SES send failed. Check: "
        "(1) AFRICONN_EMAIL env var is set to your verified address. "
        "(2) App Runner IAM role has ses:SendRawEmail permission (already in compute.tf). "
        "(3) SES domain is verified (AWS Console → SES → Verified identities)."
)
