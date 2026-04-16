# Architecture

## System overview

The Moonraker Agent Service is a browser automation worker that executes tasks triggered by Client HQ. It runs on a dedicated VPS, isolated from the Vercel-hosted web applications.

### Components

**Client HQ** (clients.moonraker.ai, Vercel)
The admin platform where Moonraker's team manages clients. Contains 17 API routes that interact with the agent: trigger endpoints that dispatch tasks, callback endpoints that receive results, and polling endpoints that check progress.

**Agent Service** (agent.moonraker.ai, Hetzner VPS)
A Docker container running FastAPI + Playwright + Browser Use. Receives task requests via HTTP, manages a sequential queue (one browser at a time), and executes browser automation scripts. Posts results back to Client HQ or writes directly to Supabase.

**Supabase** (database)
Stores all persistent data: audit results, content pages, batch status, design specs, contact records.

### Communication flow

```
1. Admin clicks "Run Audit" in Client HQ
2. Client HQ POST /api/trigger-agent → Agent POST /tasks/surge-audit
3. Agent returns 202 (queued) immediately
4. Agent acquires browser_lock, launches headless Chrome
5. Browser Use + Claude navigate the Surge platform
6. Agent extracts results (~160KB of audit data)
7. Agent POST results to Client HQ /api/process-entity-audit
8. Client HQ processes data with Claude, stores in Supabase
9. Admin sees results in the client deep-dive
```

### Task execution model

All browser tasks share a single `asyncio.Lock` called `browser_lock`. This ensures only one headless Chrome instance runs at a time, preventing OOM on the 4GB VPS.

Tasks are categorized into tiers:
- **Heavy (browser_lock + 10s cooldown):** Browser Use + LLM tasks that consume significant memory and API credits
- **Light (browser_lock + 2s cooldown):** Playwright-only tasks that use the browser but no LLM
- **Tier 1 (no lock):** CPU-only tasks like image compositing that don't need a browser

### Hybrid engine approach

The agent uses two browser engines depending on the task:

**Browser Use + Claude** for tasks requiring judgment:
- Navigating unfamiliar UIs (Surge platform)
- Filling forms with dynamic options
- Handling unexpected popups or errors
- Cost: ~$0.05-0.15 per run (Anthropic API)

**Raw Playwright** for tasks following a deterministic script:
- WordPress admin navigation (known selectors)
- Screenshot capture
- CSS extraction
- Cost: $0 (no API calls)

This hybrid approach keeps costs low while handling both predictable and unpredictable browser interactions.

## Infrastructure

### VPS (Hetzner Cloud)

| Property | Value |
|---|---|
| IP | 87.99.133.69 |
| Plan | CPX31 (2 vCPU, 4GB RAM) |
| OS | Ubuntu 24.04 |
| Location | Ashburn |
| Cost | ~$5.59/mo |

### Docker container

| Property | Value |
|---|---|
| Base image | python:3.12-slim |
| Browser | Chromium (system package) + Playwright browsers |
| Shared memory | 2GB (`shm_size`) |
| Memory limit | 4GB (with 5GB swap) |
| Port | 127.0.0.1:8000 (proxied by Caddy) |
| Restart policy | unless-stopped |

### Security

- SSH: key-only authentication, passwords disabled
- UFW firewall: deny all, allow 22/80/443
- Docker ports bound to localhost only (never 0.0.0.0)
- FastAPI docs/OpenAPI disabled in production
- Bearer token auth with timing-safe comparison
- Fail-closed: missing AGENT_API_KEY env var = 500 error

## Playbooks

Playbooks are platform-specific reference documents for browser automation tasks. They live in `playbooks/` and document:

- Login flows and DOM selectors
- Navigation patterns
- CRUD operations (create page, upload media, set SEO)
- Error recovery patterns
- WAF/security challenge handling
- REST API alternatives

Current playbooks: WordPress v1. Planned: Squarespace, Wix.

Playbooks are consumed by both:
1. **Claude during development** (read as context when writing task files)
2. **Browser Use agents at runtime** (could be injected as system context for LLM-driven tasks)

## Key dependencies

| Package | Purpose | Version |
|---|---|---|
| browser-use | LLM-to-browser bridge | >=0.12.0 |
| langchain-anthropic | Claude integration for Browser Use | >=0.3.0 |
| playwright | Browser automation | >=1.49.0 |
| fastapi | HTTP server | >=0.115.0 |
| httpx | Async HTTP client for callbacks | >=0.27.0 |
| Pillow (PIL) | Image processing for NEO overlays | (via qrcode[pil]) |
