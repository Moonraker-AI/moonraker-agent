# Moonraker Agent Service

Browser automation agent for [Moonraker AI](https://moonraker.ai), a digital marketing agency serving therapy practices. Runs headless browser tasks triggered from Client HQ, including website audits, design asset capture, content analysis, and CMS page deployment.

## Architecture

```
Client HQ (Vercel)          VPS (Hetzner)              Supabase
┌──────────────────┐   POST  ┌──────────────────┐       ┌──────────┐
│ Admin clicks      │───────>│ FastAPI server    │       │ Database │
│ "Run Audit"       │        │                   │       │          │
│                   │<───────│ Browser Use +     │──────>│ Results  │
│ Shows results     │  POST  │ Playwright        │ write │          │
└──────────────────┘ callback└──────────────────┘       └──────────┘
```

**Trigger:** Client HQ POSTs a task request with parameters (client info, URLs, credentials).
**Execute:** The agent queues the task, acquires a browser lock (one at a time), and runs the automation.
**Report:** On completion, the agent POSTs results back to Client HQ via a callback URL and optionally writes directly to Supabase.

## Task types

| Task | Endpoint | Engine | Cost/run | Duration |
|------|----------|--------|----------|----------|
| Entity audit | `POST /tasks/surge-audit` | Browser Use + Claude | ~$0.10 | 20-35 min |
| Content audit | `POST /tasks/surge-content-audit` | Browser Use + Claude | ~$0.08 | 15-25 min |
| Batch audit | `POST /tasks/surge-batch-audit` | Browser Use + Claude | ~$0.15 | 30-60 min |
| Design capture | `POST /tasks/capture-design-assets` | Playwright only | $0 | 30-60 sec |
| NEO overlay | `POST /tasks/apply-neo-overlay` | PIL (no browser) | $0 | 5-10 sec |
| WP scout | `POST /tasks/wp-scout` | Playwright only | $0 | 30-60 sec |

**Browser Use tasks** use Claude (Opus 4.6) to navigate complex UIs with LLM decision-making.
**Playwright tasks** follow deterministic scripts with no LLM, making them faster and free.
**PIL tasks** do image processing with no browser at all.

## Setup

### Prerequisites

- A Linux server (tested on Ubuntu 24.04, Hetzner CX23)
- Docker and Docker Compose
- A domain pointed at the server (for HTTPS via Caddy)
- API keys: Anthropic, Resend, Supabase

### Deploy

```bash
# Clone
git clone https://github.com/Moonraker-AI/moonraker-agent.git
cd moonraker-agent

# Configure
cp .env.example .env
# Edit .env with your API keys

# Build and start
docker compose up -d --build

# Verify
curl -H "Authorization: Bearer YOUR_AGENT_API_KEY" http://localhost:8000/health
```

### Caddy reverse proxy (HTTPS)

```
agent.yourdomain.com {
    reverse_proxy 127.0.0.1:8000
}
```

### Security hardening

- SSH: key-only, `PasswordAuthentication no`
- UFW: deny default, allow 22/80/443 only
- Docker: bind to `127.0.0.1:8000:8000` (never expose directly)
- FastAPI: `docs_url=None, redoc_url=None, openapi_url=None`
- Auth: `secrets.compare_digest` for timing-safe token comparison

## Authentication

All endpoints (except unauthenticated health check) require:

```
Authorization: Bearer {AGENT_API_KEY}
```

## Monitoring

```bash
# Health check
curl -H "Authorization: Bearer $TOKEN" https://agent.yourdomain.com/health

# List recent tasks
curl -H "Authorization: Bearer $TOKEN" https://agent.yourdomain.com/tasks?limit=10

# Task status
curl -H "Authorization: Bearer $TOKEN" https://agent.yourdomain.com/tasks/{task_id}/status
```

## Adding a new task type

See [docs/adding-a-task.md](docs/adding-a-task.md) for the step-by-step guide.

## Playbooks

Platform-specific guides for browser automation tasks live in `playbooks/`:

- [WordPress](playbooks/wordpress.md) - Login, page creation, SEO, menus, REST API
- Squarespace (planned)
- Wix (planned)

## Project structure

```
moonraker-agent/
  server.py              # FastAPI app, routing, auth, task queue
  Dockerfile             # Chrome + Playwright + Python
  docker-compose.yml     # Container config (2GB shm, 4GB mem limit)
  requirements.txt       # Python dependencies
  .env.example           # Required environment variables
  deploy.sh              # Pull + rebuild + restart
  CLAUDE.md              # Development context for Claude sessions
  tasks/
    surge_audit.py       # Entity/homepage audits via Surge platform
    surge_content_audit.py  # Keyword-specific page audits
    surge_batch_audit.py    # Multi-page batch audits with synthesis
    capture_design_assets.py  # Screenshot + CSS extraction
    apply_neo_overlay.py     # Image compositing (QR codes, logos)
    wp_scout.py              # WordPress admin reconnaissance
  utils/
    cleanup.py           # Pre-flight process/memory cleanup
    notifications.py     # Resend email notifications
  playbooks/
    wordpress.md         # WordPress automation reference
  docs/
    api-contract.md      # Task request/response schemas
    adding-a-task.md     # How to create new task types
    architecture.md      # System architecture overview
```

## License

Private. Copyright Moonraker AI.
