# API Contract

How Client HQ and the agent service communicate. Every request requires `Authorization: Bearer {AGENT_API_KEY}` except where noted.

## Task lifecycle

1. Client HQ `POST`s a task request to the agent
2. Agent returns `202 Accepted` with a `task_id` immediately
3. Agent acquires browser lock and executes the task
4. Client HQ can poll `GET /tasks/{task_id}/status` for progress
5. On completion, agent `POST`s results back to Client HQ via the callback URL

## Endpoints

### Health check

```
GET /health
Authorization: Bearer {AGENT_API_KEY}

Response 200:
{
  "status": "ok",
  "service": "moonraker-agent",
  "version": "0.4.0",
  "active_tasks": 0,
  "total_tasks": 5
}
```

### Task status

```
GET /tasks/{task_id}/status
Authorization: Bearer {AGENT_API_KEY}

Response 200:
{
  "task_id": "scout-abc123",
  "status": "running",            // queued | running | complete | error | credits_exhausted
  "message": "Step 3/7: Scanning plugins...",
  "created_at": "2026-04-15T06:31:35Z",
  "updated_at": "2026-04-15T06:32:10Z",
  "error": null,
  "duration_seconds": 35
}
```

### List tasks

```
GET /tasks?status=complete&limit=10
Authorization: Bearer {AGENT_API_KEY}

Response 200: Array of TaskStatus objects
```

---

## Task types

### Entity audit (Surge)

Runs a full Surge entity audit against a practice's homepage and brand.

```
POST /tasks/surge-audit
Authorization: Bearer {AGENT_API_KEY}
Content-Type: application/json

{
  "audit_id": "uuid",              // entity_audits record ID
  "practice_name": "Sky Therapies",
  "website_url": "https://skytherapies.ca",
  "city": "Toronto",
  "state": "Ontario",
  "geo_target": "Toronto, ON",     // optional
  "gbp_link": "https://...",       // optional
  "client_slug": "anna-skomorovskaia"
}

Response 202:
{ "task_id": "abc-123", "status": "queued" }
```

**Callback:** `POST {CLIENT_HQ_URL}/api/process-entity-audit`
```json
{
  "audit_id": "uuid",
  "client_slug": "anna-skomorovskaia",
  "raw_text": "...160KB of Surge output...",
  "task_id": "abc-123"
}
```

### Content audit (Surge)

Runs a keyword-specific page audit via Surge.

```
POST /tasks/surge-content-audit
{
  "content_page_id": "uuid",
  "website_url": "https://skytherapies.ca",
  "target_keyword": "anxiety therapy toronto",
  "search_query": "anxiety therapy toronto",   // optional, defaults to target_keyword
  "practice_name": "Sky Therapies",            // optional
  "page_type": "service",                      // service | location | bio | faq
  "city": "Toronto",                           // optional
  "state": "Ontario",                          // optional
  "geo_target": "Toronto, ON",                 // optional
  "client_slug": "anna-skomorovskaia",         // optional
  "callback_url": "https://clients.moonraker.ai/api/ingest-surge-content"
}
```

**Callback:** `POST {callback_url}`
```json
{
  "content_page_id": "uuid",
  "raw_data": "...Surge audit text...",
  "task_id": "content-abc-123"
}
```

### Batch audit (Surge)

Runs audits on multiple pages in one Surge session, then extracts a cross-page synthesis.

```
POST /tasks/surge-batch-audit
{
  "batch_id": "uuid",
  "client_slug": "anna-skomorovskaia",
  "brand_name": "Sky Therapies",
  "gbp_url": "https://...",
  "entity_type": "Local Business",
  "geo_target": "Toronto, ON",
  "website_url": "https://skytherapies.ca",
  "pages": [
    { "content_page_id": "uuid1", "keyword": "anxiety therapy", "target_url": "https://..." },
    { "content_page_id": "uuid2", "keyword": "EMDR therapy", "target_url": "https://..." }
  ],
  "callback_url": "https://clients.moonraker.ai/api/ingest-batch-audit"
}
```

**Callback:** `POST {callback_url}`
```json
{
  "batch_id": "uuid",
  "page_results": [
    { "content_page_id": "uuid1", "raw_data": "..." },
    { "content_page_id": "uuid2", "raw_data": "..." }
  ],
  "synthesis": "...cross-page analysis...",
  "task_id": "batch-abc-123"
}
```

### Design asset capture

Takes screenshots and extracts computed CSS from a client's website.

```
POST /tasks/capture-design-assets
{
  "design_spec_id": "uuid",
  "client_slug": "anna-skomorovskaia",
  "website_url": "https://skytherapies.ca",
  "service_page_url": "https://skytherapies.ca/anxiety-therapy",  // optional, auto-discovered
  "about_page_url": "https://skytherapies.ca/about",              // optional, auto-discovered
  "callback_url": "https://clients.moonraker.ai/api/ingest-design-assets"
}
```

**Callback:** `POST {callback_url}`
```json
{
  "design_spec_id": "uuid",
  "screenshots": { "homepage": "base64...", "service": "base64..." },
  "computed_css": { "homepage": { "fonts": [...], "colors": [...] } },
  "crawled_text": { "homepage": "..." }
}
```

### NEO overlay

Composites QR code, logo, and practice info onto a base image. No browser needed.

```
POST /tasks/apply-neo-overlay
{
  "base_image_url": "https://...",
  "client_slug": "anna-skomorovskaia",
  "practice_name": "Sky Therapies",
  "plus_code": "87M2MH62+39",
  "gbp_share_link": "https://...",
  "logo_drive_file_id": "abc123",     // optional
  "logo_url": "https://...",          // optional (alternative to Drive)
  "output_name": "neo-anxiety.webp",  // optional
  "neo_image_id": "uuid",             // optional
  "callback_url": "https://clients.moonraker.ai/api/ingest-neo-overlay"
}
```

### WordPress scout

Reconnaissance of a WordPress admin dashboard. Returns structured report of theme, plugins, pages, menus, SEO setup.

```
POST /tasks/wp-scout
{
  "wp_admin_url": "https://example.com/wp-admin",
  "wp_username": "agent@moonraker.ai",
  "wp_password": "...",
  "client_slug": "client-name",                  // optional
  "callback_url": "https://clients.moonraker.ai/api/ingest-wp-scout"  // optional
}
```

**Result** (stored in task data, also sent to callback if provided):
```json
{
  "scout_version": "1.0",
  "scanned_at": "2026-04-15T06:31:35Z",
  "login": { "success": true, "login_url_used": "...", "notes": "" },
  "wordpress": { "version": "6.7", "multisite": false },
  "theme": { "name": "Flavor", "type": "classic", "version": "2.1" },
  "seo_plugin": { "name": "rankmath", "version": "1.0.230" },
  "page_builder": { "name": "elementor", "version": "3.25" },
  "editor_type": "gutenberg",
  "plugins": [ { "name": "...", "version": "...", "active": true } ],
  "pages": [ { "id": "123", "title": "...", "slug": "...", "status": "Published" } ],
  "menus": { "locations": [...], "items": [...] },
  "permalink_structure": "/%postname%/",
  "media_stats": { "total_items": 234 },
  "screenshots": { "dashboard": "base64...", "plugins": "base64..." },
  "errors": []
}
```

---

## Callback authentication

All callbacks from agent to Client HQ use:

```
Authorization: Bearer {AGENT_API_KEY}
Content-Type: application/json
```

Client HQ validates this via `requireAdminOrInternal(req, res)` in `api/_lib/auth.js`, which accepts the AGENT_API_KEY as an internal caller.

## Error handling

If a task fails, the agent:
1. Sets task status to `error` with an error message
2. Sends an error notification email via Resend (for audit tasks)
3. Does NOT call the success callback

Client HQ's `process-audit-queue.js` cron detects stale `agent_running` tasks (>45 min) and resets them to `queued` for retry.

## Sequential execution

All browser tasks share a single `browser_lock`. Only one browser runs at a time to prevent OOM on the 4GB VPS. Tasks queue in FIFO order. Non-browser tasks (NEO overlay) bypass the lock and run immediately.
