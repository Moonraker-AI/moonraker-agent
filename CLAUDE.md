# Moonraker Agent Service

## What This Is

A self-hosted Python/FastAPI service that runs autonomous browser automation tasks for Moonraker AI's Client HQ platform. Currently handles Surge entity audit automation.

**Production URL:** `https://agent.moonraker.ai`
**VPS:** Hetzner CPX31 at `87.99.133.69` (Ashburn, Ubuntu 24.04, 2 vCPU / 4GB RAM)
**Stack:** Python 3.12, FastAPI, Browser Use 0.12.x, Docker, Caddy reverse proxy

## Architecture

```
Client HQ (Vercel)                    Agent Service (Hetzner VPS)
┌─────────────────────┐               ┌──────────────────────────┐
│ Admin clicks         │  POST /api/   │                          │
│ "Run Automated Audit"│──trigger──────│ FastAPI server           │
│                      │  agent.js     │   ├─ server.py           │
│ Poll for status      │               │   ├─ tasks/              │
│ every 30s via        │  GET /api/    │   │   └─ surge_audit.py  │
│ /api/poll-agent      │──poll─────────│   └─ utils/              │
│                      │  agent.js     │       └─ notifications.py│
│                      │               │                          │
│ /api/process-entity- │  POST results │ Browser Use + Chromium   │
│ audit.js receives    │◄──callback────│ (headless browser)       │
│ Surge data           │               │                          │
└─────────────────────┘               └──────────────────────────┘
```

## Repo Structure

```
moonraker-agent/
  server.py              # FastAPI app: task queue, auth, status polling
  tasks/
    __init__.py
    surge_audit.py       # Surge audit workflow (Browser Use + Playwright)
  utils/
    __init__.py
    notifications.py     # Resend email notifications
  Dockerfile             # Python 3.12 + Chromium + Browser Use
  docker-compose.yml     # Container config with 2GB shm
  requirements.txt       # browser-use>=0.12.0, fastapi, uvicorn, httpx, etc.
  .env                   # Credentials (NOT in git, lives on VPS only)
  CLAUDE.md              # This file
```

## Deployment

All code lives at `/opt/moonraker-agent/` on the VPS. There is no CI/CD pipeline yet — deployment is manual via SSH.

### To deploy changes:

```bash
ssh root@87.99.133.69
cd /opt/moonraker-agent

# Edit files directly, or SCP them up
# Then rebuild:
docker compose build --no-cache
docker compose up -d

# Verify:
sleep 5 && curl -s http://localhost:8000/health | python3 -m json.tool

# Check logs:
docker compose logs --tail=50
docker compose logs -f  # follow live
```

### Health check:
```bash
curl -s https://agent.moonraker.ai/health
```

## Authentication

Agent ↔ Client HQ use a shared Bearer token stored as:
- `AGENT_API_KEY` in the VPS `.env` file
- `AGENT_API_KEY` in Vercel env vars for Client HQ

All API endpoints require `Authorization: Bearer <key>`.

## Environment Variables (.env on VPS)

```
ANTHROPIC_API_KEY=         # For Browser Use LLM (Claude Sonnet)
SURGE_URL=https://www.surgeaiprotocol.com
SURGE_EMAIL=support@moonraker.ai
SURGE_PASSWORD=            # Surge platform credentials
CLIENT_HQ_URL=https://clients.moonraker.ai
AGENT_API_KEY=             # Shared secret for auth
SUPABASE_URL=https://ofmmwcjhdrhvxxkhcuww.supabase.co
SUPABASE_SERVICE_ROLE_KEY= # For direct Supabase access
RESEND_API_KEY=            # For email notifications
```

## Surge Audit Workflow

The audit runs in 4 phases:

1. **Login + Form Fill** (~2 min, uses Browser Use + Claude Sonnet)
   - Navigate to Surge, log in with email/password
   - Fill the New Analysis form with client data
   - Submit and wait for analysis to start

2. **Wait for Completion** (~20-35 min, raw Playwright polling, $0 API cost)
   - Poll page content every 30 seconds
   - Look for "Run completed in" text as completion signal
   - Report progress to Client HQ via task status updates

3. **Extract Results** (raw Playwright, $0 API cost)
   - Click the "Copy raw text" button on the results page
   - Intercept clipboard data via injected JS
   - Multiple fallback strategies if primary extraction fails

4. **Post Results** (HTTP callback)
   - POST surge data to Client HQ's `/api/process-entity-audit`
   - Client HQ processes with Claude Opus, extracts scores, deploys pages
   - Send email notifications to team

## Content Audit Workflow

Same 4-phase pattern as entity audits, but for keyword-specific page builds:

1. **Login + Form Fill** (~2 min, Browser Use + Claude Opus 4.6)
   - Navigate to Surge, log in
   - Fill form with: website URL, search query (keyword + location), practice name
   - Submit and wait for analysis to start

2. **Wait for Completion** (~20-35 min, raw Playwright polling, $0 API cost)
   - Same detection strategies as entity audit (URL redirect, content indicators)

3. **Extract Results** (raw Playwright, $0 API cost)
   - Same extraction strategies (clipboard intercept, page text, full HTML)

4. **Post Results** (HTTP callback)
   - POST to Client HQ's `/api/ingest-surge-content` (NOT `/api/process-entity-audit`)
   - Client HQ extracts RTPBA + schema recommendations, updates `content_pages` table
   - Send email notification to team

Key differences from entity audits:
- Triggered from Client HQ Content tab (not Audit tab)
- Input: target_keyword + search_query + website_url (not brand_query)
- Updates: `content_pages` table (not `entity_audits`)
- Task ID prefix: `content-` (not `surge-`)
- Purpose: Extract Ready-to-Publish Best Answer for Pagemaster page building

## Browser Use 0.12.x API Notes

The library restructured significantly in 0.12.x:

- **No more BrowserConfig** — pass kwargs directly to `Browser()`
- **No more BrowserContextConfig** — merged into Browser constructor
- **LLM:** Use `from browser_use.llm import ChatAnthropic` (NOT langchain)
- **Page access:** `page = await browser.get_current_page()` (method on Browser directly)
- **Keep browser open after agent.run():** `Browser(keep_alive=True)`
- **Chromium args:** Parameter is `args=` not `extra_chromium_args=`
- **Agent constructor:** `Agent(task=..., llm=..., browser=browser)`

## Client HQ Integration Points

These files in the `Moonraker-AI/client-hq` repo talk to this agent:

- `api/trigger-agent.js` — Triggers audit, returns task_id
- `api/poll-agent.js` — Proxies status polling to agent
- `admin/clients/index.html` — UI with "Run Automated Audit" button, status bar, polling logic

## Key API Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | No | Health check + version |
| GET | `/tasks` | Bearer | List all tasks |
| POST | `/tasks/surge-audit` | Bearer | Start a new audit |
| POST | `/tasks/surge-content-audit` | Bearer | Start a keyword-specific content audit |
| GET | `/tasks/{id}/status` | Bearer | Poll task status |

## Common Issues

- **Import errors after browser-use update:** Check `browser_use.__init__` exports; API changes between versions
- **Browser closes after agent.run():** Need `keep_alive=True` on Browser
- **Clipboard extraction fails:** Surge's Copy button uses `document.execCommand('copy')` not clipboard API; need JS interceptor
- **Docker build slow:** Use `--no-cache` flag when changing requirements.txt
- **VPS memory:** 4GB total, Chromium needs 2GB shm — only run one audit at a time (enforced by asyncio lock)

## Future Workflows (planned)

- CMS page deployment via browser automation — deploying approved Pagemaster HTML to WordPress/Squarespace/Wix
- LocalFalcon campaign setup
- GBP place_id lookups
