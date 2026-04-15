"""
Notifications
=============
Sends email notifications via Resend when audits complete, fail, or need attention.
"""

import logging
import os

import httpx

logger = logging.getLogger("agent.notifications")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
CLIENT_HQ_URL = os.getenv("CLIENT_HQ_URL", "https://clients.moonraker.ai")

FROM_EMAIL = "audits@clients.moonraker.ai"
TEAM_RECIPIENTS = [
    "chris@moonraker.ai",
    "scott@moonraker.ai",
    "support@moonraker.ai",
]

LOGO_URL = "https://clients.moonraker.ai/assets/logo.png"

# ── Email template ───────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    if not s:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _build_email_html(
    title: str,
    subtitle: str,
    body_html: str,
    header_label: str = "Audit Notification",
    cta_url: str = None,
    cta_text: str = None,
) -> str:
    """Build a branded email matching the Client HQ shared email template.

    Dark navy (#141C3A) header and footer, white body, mint background.
    """

    cta_block = ""
    if cta_url and cta_text:
        cta_block = f"""<table cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:24px 0;"><tr><td align="center">
            <a href="{_esc(cta_url)}" style="display:inline-block;background:#00D47E;color:#FFFFFF;font-family:Inter,sans-serif;font-weight:600;font-size:15px;text-decoration:none;padding:14px 32px;border-radius:8px;">{_esc(cta_text)}</a>
            </td></tr></table>"""

    year = __import__("datetime").datetime.now().year

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>@import url("https://fonts.googleapis.com/css2?family=Outfit:wght@700&family=Inter:wght@400;500;600&display=swap");</style>
</head>
<body style="margin:0;padding:0;background:#F7FDFB;font-family:Inter,-apple-system,BlinkMacSystemFont,sans-serif;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#F7FDFB;">
<tr><td align="center" style="padding:24px 16px;">
<table cellpadding="0" cellspacing="0" border="0" width="600" style="max-width:600px;width:100%;">

<!-- Header: dark navy bar -->
<tr><td style="background:#141C3A;padding:24px 32px;border-radius:14px 14px 0 0;">
  <table cellpadding="0" cellspacing="0" border="0" width="100%"><tr>
    <td style="vertical-align:middle;"><img src="{LOGO_URL}" alt="Moonraker" height="28" style="display:block;"></td>
    <td style="text-align:right;vertical-align:middle;"><span style="color:#FFFFFF;font-family:Inter,sans-serif;font-size:12px;letter-spacing:0.03em;">{_esc(header_label)}</span></td>
  </tr></table>
</td></tr>

<!-- Body: white content area -->
<tr><td style="background:#FFFFFF;padding:32px;border-left:1px solid #E2E8F0;border-right:1px solid #E2E8F0;">
  <h1 style="font-family:Outfit,sans-serif;font-size:22px;font-weight:700;color:#1E2A5E;margin:0 0 6px;">{_esc(title)}</h1>
  <p style="font-family:Inter,sans-serif;font-size:14px;color:#6B7599;margin:0 0 20px;">{_esc(subtitle)}</p>
  {body_html}
  {cta_block}
</td></tr>

<!-- Footer: dark navy bar -->
<tr><td style="background:#141C3A;padding:24px 32px;border-radius:0 0 14px 14px;text-align:center;">
  <p style="font-size:12px;color:rgba(232,245,239,.55);margin:0 0 4px;font-family:Inter,sans-serif;">Automated notification from Moonraker Agent Service</p>
  <p style="font-size:12px;color:rgba(232,245,239,.35);margin:0;font-family:Inter,sans-serif;">&copy; {year} Moonraker AI</p>
</td></tr>

</table></td></tr></table></body></html>"""


# ── Send helpers ─────────────────────────────────────────────────────────────

async def _send_email(subject: str, html: str):
    """Send email via Resend API."""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set, skipping email notification")
        return

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": f"Moonraker Audits <{FROM_EMAIL}>",
                "to": TEAM_RECIPIENTS,
                "subject": subject,
                "html": html,
            },
        )

        if response.status_code not in (200, 201):
            logger.error(f"Resend API error {response.status_code}: {response.text[:300]}")
        else:
            logger.info(f"Notification sent: {subject}")


# ── Public notification functions ────────────────────────────────────────────

async def send_success_notification(
    practice_name: str,
    client_slug: str,
    audit_id: str,
    duration_minutes: int,
    data_length: int,
):
    """Notify team that an audit completed successfully."""
    deep_dive_url = f"{CLIENT_HQ_URL}/admin/clients#deep-dive={client_slug}&tab=audit"

    body_html = f"""
    <p style="font-family:Inter,sans-serif;font-size:15px;color:#333F70;line-height:1.7;margin:0 0 16px;">
      The Surge entity audit for <strong>{_esc(practice_name)}</strong> completed
      successfully and has been submitted for processing.
    </p>
    <table style="width:100%;font-size:13px;color:#555;border-collapse:collapse;">
      <tr>
        <td style="padding:6px 0;border-bottom:1px solid #f0f0f0;">Surge processing time</td>
        <td style="padding:6px 0;border-bottom:1px solid #f0f0f0;text-align:right;font-weight:600;color:#1E2A5E;">{duration_minutes} minutes</td>
      </tr>
      <tr>
        <td style="padding:6px 0;border-bottom:1px solid #f0f0f0;">Data extracted</td>
        <td style="padding:6px 0;border-bottom:1px solid #f0f0f0;text-align:right;font-weight:600;color:#1E2A5E;">{data_length:,} characters</td>
      </tr>
      <tr>
        <td style="padding:6px 0;">Audit ID</td>
        <td style="padding:6px 0;text-align:right;font-family:monospace;font-size:12px;color:#6B7599;">{audit_id}</td>
      </tr>
    </table>"""

    html = _build_email_html(
        title=f"Audit Complete: {practice_name}",
        subtitle="Surge data extracted and submitted for processing",
        body_html=body_html,
        header_label="Audit Complete",
        cta_url=deep_dive_url,
        cta_text="View in Client HQ",
    )

    await _send_email(
        subject=f"Surge Audit Complete: {practice_name}",
        html=html,
    )


async def send_error_notification(
    practice_name: str,
    client_slug: str,
    error_message: str,
    task_id: str,
):
    """Notify team that an audit failed."""
    deep_dive_url = f"{CLIENT_HQ_URL}/admin/clients#deep-dive={client_slug}&tab=audit"

    # Truncate error for display
    display_error = error_message[:300]
    if len(error_message) > 300:
        display_error += "..."

    body_html = f"""
    <p style="font-family:Inter,sans-serif;font-size:15px;color:#333F70;line-height:1.7;margin:0 0 16px;">
      The automated Surge audit for <strong>{_esc(practice_name)}</strong> encountered
      an error and could not complete. The team may need to run this audit manually.
    </p>
    <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:12px 16px;margin:0 0 16px;">
      <p style="margin:0;font-size:13px;color:#991b1b;font-family:monospace;word-break:break-word;">
        {_esc(display_error)}
      </p>
    </div>
    <p style="margin:0;font-size:12px;color:#6B7599;">
      Task ID: {task_id}
    </p>"""

    html = _build_email_html(
        title=f"Audit Failed: {practice_name}",
        subtitle="Automated Surge audit encountered an error",
        body_html=body_html,
        header_label="Audit Error",
        cta_url=deep_dive_url,
        cta_text="View Client",
    )

    await _send_email(
        subject=f"Surge Audit Failed: {practice_name}",
        html=html,
    )


async def send_credits_notification(practice_name: str, client_slug: str):
    """Notify team that Surge credits are exhausted."""
    body_html = f"""
    <p style="font-family:Inter,sans-serif;font-size:15px;color:#333F70;line-height:1.7;margin:0 0 16px;">
      The Surge audit for <strong>{_esc(practice_name)}</strong> could not run because
      the account has insufficient credits. Contact the Surge team to add more credits.
    </p>
    <p style="font-family:Inter,sans-serif;font-size:15px;color:#333F70;line-height:1.7;margin:0;">
      Once credits are replenished, re-trigger the audit from Client HQ.
    </p>"""

    html = _build_email_html(
        title="Surge Credits Exhausted",
        subtitle="Audits paused until credits are replenished",
        body_html=body_html,
        header_label="Credits Alert",
    )

    await _send_email(
        subject=f"Surge Credits Exhausted (attempted: {practice_name})",
        html=html,
    )


async def send_batch_notification(
    brand_name: str,
    client_slug: str,
    pages_extracted: int,
    pages_total: int,
    has_synthesis: bool,
    task_id: str,
    env: dict,
):
    """Notify team that a batch audit has completed."""
    synth_text = "Yes" if has_synthesis else "Not generated"
    body_html = f"""
    <p style="font-family:Inter,sans-serif;font-size:15px;color:#333F70;line-height:1.7;margin:0 0 16px;">
      Batch audit for <strong>{_esc(brand_name)}</strong> has completed.
    </p>
    <table cellpadding="0" cellspacing="0" border="0" style="margin:0 0 16px;font-size:14px;">
      <tr><td style="padding:4px 16px 4px 0;color:#6B7599;">Pages Extracted</td><td style="font-weight:600;color:#1E2A5E;">{pages_extracted} / {pages_total}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;color:#6B7599;">Synthesis</td><td style="font-weight:600;color:#1E2A5E;">{synth_text}</td></tr>
      <tr><td style="padding:4px 16px 4px 0;color:#6B7599;">Task ID</td><td style="font-weight:600;color:#1E2A5E;">{task_id[:12]}</td></tr>
    </table>
    <p style="font-family:Inter,sans-serif;font-size:15px;color:#333F70;line-height:1.7;margin:0;">
      Data has been sent to Client HQ for processing. Check the Content tab for this client.
    </p>"""

    client_url = f"https://clients.moonraker.ai/admin/clients?slug={client_slug}&tab=content"

    html = _build_email_html(
        title="Batch Audit Complete",
        subtitle=f"{_esc(brand_name)} \u2014 {pages_extracted}/{pages_total} pages",
        body_html=body_html,
        header_label="Content Audit",
        cta_url=client_url,
        cta_text="View in Content Tab",
    )

    await _send_email(
        subject=f"Batch Audit Complete: {brand_name} ({pages_extracted}/{pages_total} pages)",
        html=html,
    )
