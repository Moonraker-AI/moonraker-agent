# Changelog

## v0.6.0 (2026-04-15)

### Added
- Squarespace scout task (`tasks/sq_scout.py`) for SQ site reconnaissance
- Squarespace playbook v1 (`playbooks/squarespace.md`)
- Wix scout task (`tasks/wix_scout.py`) for Wix site reconnaissance
- Wix playbook v1 (`playbooks/wix.md`)
- `GET /tasks/{task_id}/result` endpoint documented in API contract

### Architecture
- All three scouts follow the same pattern: public httpx crawl first (fast, 5-10s), browser fallback if needed
- SQ scout: detects version (7.0/7.1), template family, pages, nav, SEO, connected services, fonts, blog, schema, code injection. Optional admin panel scan with contributor credentials
- Wix scout: detects site ID, Wix Studio/Editor X, pages, nav (with browser fallback for JS-rendered sites), blog, Wix apps, tracking scripts
- Both new scouts use browser_lock (Tier 2) with LIGHT_TASK_COOLDOWN

### Client HQ integration
- Unified `api/trigger-cms-scout.js` dispatches to correct agent endpoint based on `website_platform`
- `api/ingest-cms-scout.js` callback stores reports in `cms_scouts` Supabase table
- Scout button in Content tab now visible for WordPress, Squarespace, and Wix clients
- Auto-loads latest scout result; polls for completion with 5s interval

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
