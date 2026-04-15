# Changelog

## v0.5.0 (2026-04-15)

### Added
- WordPress scout task (`tasks/wp_scout.py`) for CMS reconnaissance
- WordPress playbook v1 (`playbooks/wordpress.md`)
- Full documentation: README, architecture, API contract, task creation guide
- WAF/security challenge handling (SiteGround, Cloudflare, Sucuri, Wordfence)
- Stealth anti-detection for headless Chrome (navigator.webdriver override)
- All VPS code synced to GitHub (previously untracked)

### Fixed
- Missing `qrcode` module that crashed container on startup
- Removed `.env.backup` from repo (contained credentials)

## v0.4.0 (2026-04-14)

### Added
- NEO overlay task (`tasks/apply_neo_overlay.py`) for image compositing
- Design asset capture task (`tasks/capture_design_assets.py`)
- Batch audit task (`tasks/surge_batch_audit.py`) for multi-page Surge audits
- Content audit task (`tasks/surge_content_audit.py`) for keyword-specific audits
- Pre-flight cleanup utility (`utils/cleanup.py`)
- Tiered task execution: heavy (browser + LLM), light (browser only), tier 1 (no browser)

### Changed
- Sequential browser lock prevents OOM on 4GB VPS
- Cooldown between tasks: 10s heavy, 2s light

## v0.2.1 (2026-04-08)

### Added
- Initial agent service with FastAPI + Browser Use
- Surge entity audit task (`tasks/surge_audit.py`)
- Email notifications via Resend (`utils/notifications.py`)
- Docker container with Chromium + Playwright
- Bearer token authentication
- Health check and task status endpoints
