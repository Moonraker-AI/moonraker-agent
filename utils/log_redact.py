"""
Log Secret Redaction
====================
Scrubs environment-provided secrets from log records before they reach any
handler. Protects docker json-file logs (what `docker logs moonraker-agent`
returns) from leaking SURGE_PASSWORD, API keys, and bearer tokens on every
audit run.

Residual risk not covered by this filter:
- Secrets sent to external APIs (e.g. Anthropic) as part of task prompts are
  still transmitted in those conversations. Fixing that requires restructuring
  the audit flow to pre-login via raw Playwright before handing control to
  Browser Use. Tracked as a follow-up.

Usage: call `install()` once at app startup, after `load_dotenv()` so the
env vars are populated.
"""

import logging
import os


# Minimum length to be considered a secret worth redacting. Prevents an empty
# env var from turning every log line into "***REDACTED***".
_MIN_SECRET_LEN = 6

_SECRET_ENV_VARS = (
    "SURGE_PASSWORD",
    "SURGE_EMAIL",
    "ANTHROPIC_API_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "AGENT_API_KEY",
    "RESEND_API_KEY",
    "SQ_PASSWORD",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
)

_REPLACEMENT = "***REDACTED***"


def _collect_secrets() -> list[str]:
    values = []
    for name in _SECRET_ENV_VARS:
        v = os.getenv(name, "")
        if v and len(v) >= _MIN_SECRET_LEN:
            values.append(v)
    # Deduplicate + sort by length descending so longer secrets are replaced
    # first (matters when one secret is a prefix of another, e.g. a URL that
    # contains a key).
    return sorted(set(values), key=len, reverse=True)


class _RedactFilter(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            msg = record.getMessage()
            clean = msg
            for s in self._secrets:
                if s and s in clean:
                    clean = clean.replace(s, _REPLACEMENT)
            if clean != msg:
                record.msg = clean
                record.args = ()
        except Exception:
            # Never let redaction error drop a log record.
            pass
        return True


def install() -> None:
    """Attach the redaction filter to the root logger and known child loggers."""
    secrets = _collect_secrets()
    if not secrets:
        return
    f = _RedactFilter(secrets)

    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(f)

    # Common library loggers that emit request/response bodies.
    for name in (
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "agent",
        "agent.surge",
        "agent.surge_status",
        "agent.debug_capture",
        "agent.notifications",
        "agent.supabase_patch",
        "agent.cleanup",
        "browser_use",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(name).addFilter(f)
