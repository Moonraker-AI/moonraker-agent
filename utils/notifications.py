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

# ── Email template ───────────────────────────────────────────────────────────

def _build_email_html(
    title: str,
    subtitle: str,
    body_html: str,
    cta_url: str = None,
    cta_text: str = None,
) -> str:
    """Build a branded email matching the Client HQ notification template."""

    cta_block = ""
    if cta_url and cta_text:
        cta_block = f"""
        <tr>
          <td style="padding: 24px 32px 8px;">
            <a href="{cta_url}"
               style="display: inline-block; padding: 12px 28px;
                      background: #00D47E; color: #fff; text-decoration: none;
                      border-radius: 6px; font-family: Inter, sans-serif;
                      font-weight: 600; font-size: 14px;">
              {cta_text}
            </a>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0; padding:0; background:#f5f5f7; font-family: Inter, -apple-system, sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f7; padding: 32px 0;">
    <tr>
      <td align="center">
        <table width="600" cellpadding="0" cellspacing="0"
               style="background:#fff; border-radius:12px; overflow:hidden;
                      box-shadow: 0 1px 3px rgba(0,0,0,0.08);">
          <!-- Header -->
          <tr>
            <td style="background: #0a1628; padding: 24px 32px;">
              <img src="https://clients.moonraker.ai/assets/logo.png"
                   alt="Moonraker" height="28"
                   style="display:block;">
            </td>
          </tr>
          <!-- Title -->
          <tr>
            <td style="padding: 32px 32px 8px;">
              <h1 style="margin:0; font-family: Outfit, sans-serif;
                         font-size: 22px; font-weight: 700; color: #0a1628;">
                {title}
              </h1>
              <p style="margin: 6px 0 0; font-size: 14px; color: #666;">
                {subtitle}
              </p>
            </td>
          </tr>
          <!-- Body -->
          <tr>
            <td style="padding: 16px 32px;">
              {body_html}
            </td>
          </tr>
          {cta_block}
          <!-- Footer -->
          <tr>
            <td style="padding: 24px 32px; border-top: 1px solid #eee;">
              <p style="margin:0; font-size: 12px; color: #999;">
                Automated notification from Moonraker Agent Service
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


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
    <p style="margin: 0 0 12px; font-size: 14px; color: #333; line-height: 1.6;">
      The Surge entity audit for <strong>{practice_name}</strong> completed
      successfully and has been submitted for processing.
    </p>
    <table style="width: 100%; font-size: 13px; color: #555; border-collapse: collapse;">
      <tr>
        <td style="padding: 6px 0; border-bottom: 1px solid #f0f0f0;">Surge processing time</td>
        <td style="padding: 6px 0; border-bottom: 1px solid #f0f0f0; text-align: right;
                   font-weight: 600; color: #333;">{duration_minutes} minutes</td>
      </tr>
      <tr>
        <td style="padding: 6px 0; border-bottom: 1px solid #f0f0f0;">Data extracted</td>
        <td style="padding: 6px 0; border-bottom: 1px solid #f0f0f0; text-align: right;
                   font-weight: 600; color: #333;">{data_length:,} characters</td>
      </tr>
      <tr>
        <td style="padding: 6px 0;">Audit ID</td>
        <td style="padding: 6px 0; text-align: right; font-family: monospace;
                   font-size: 12px; color: #666;">{audit_id}</td>
      </tr>
    </table>"""

    html = _build_email_html(
        title=f"Audit Complete: {practice_name}",
        subtitle="Surge data extracted and submitted for processing",
        body_html=body_html,
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
    <p style="margin: 0 0 12px; font-size: 14px; color: #333; line-height: 1.6;">
      The automated Surge audit for <strong>{practice_name}</strong> encountered
      an error and could not complete. The team may need to run this audit manually.
    </p>
    <div style="background: #fef2f2; border: 1px solid #fecaca; border-radius: 8px;
                padding: 12px 16px; margin: 12px 0;">
      <p style="margin: 0; font-size: 13px; color: #991b1b; font-family: monospace;
                word-break: break-word;">
        {display_error}
      </p>
    </div>
    <p style="margin: 12px 0 0; font-size: 12px; color: #999;">
      Task ID: {task_id}
    </p>"""

    html = _build_email_html(
        title=f"Audit Failed: {practice_name}",
        subtitle="Automated Surge audit encountered an error",
        body_html=body_html,
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
    <p style="margin: 0 0 12px; font-size: 14px; color: #333; line-height: 1.6;">
      The Surge audit for <strong>{practice_name}</strong> could not run because
      the account has insufficient credits. Contact the Surge team to add more credits.
    </p>
    <p style="margin: 0; font-size: 14px; color: #333; line-height: 1.6;">
      Once credits are replenished, re-trigger the audit from Client HQ.
    </p>"""

    html = _build_email_html(
        title="Surge Credits Exhausted",
        subtitle="Audits paused until credits are replenished",
        body_html=body_html,
    )

    await _send_email(
        subject=f"Surge Credits Exhausted (attempted: {practice_name})",
        html=html,
    )
