# Adding a new task type

This guide walks through creating a new browser automation task for the agent service. The pattern is the same whether the task uses Browser Use (LLM-driven) or raw Playwright (scripted).

## Step 1: Create the task file

Create `tasks/your_task.py` with this skeleton:

```python
"""
your_task.py

Brief description of what this task does.
Engine: Browser Use + Claude (or Playwright only)
Duration: estimated time
Cost: estimated API cost per run
"""

import asyncio
import logging

import httpx
from playwright.async_api import async_playwright

logger = logging.getLogger("moonraker.your_task")


async def run_your_task(task_id, params, status_callback, env):
    """
    Main entry point. Called by server.py.

    params: dict from the request payload
    status_callback: async function(task_id, status, message)
    env: dict of environment variables
    """
    # Extract params
    some_param = params.get("some_param", "")
    callback_url = params.get("callback_url", "")
    agent_api_key = env.get("AGENT_API_KEY", "")

    if not some_param:
        await status_callback(task_id, "failed", "some_param required")
        return

    await status_callback(task_id, "running", "Starting your task...")

    browser = None
    playwright_instance = None

    try:
        playwright_instance = await async_playwright().start()
        browser = await playwright_instance.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()

        # Your automation logic here
        await status_callback(task_id, "running", "Step 1/3: Doing something...")
        # ...

        await status_callback(task_id, "complete", "Task finished successfully")

        # Send results back to Client HQ
        if callback_url:
            async with httpx.AsyncClient(timeout=30) as client:
                await client.post(
                    callback_url,
                    json={"task_id": task_id, "results": {}},
                    headers={
                        "Authorization": f"Bearer {agent_api_key}",
                        "Content-Type": "application/json",
                    },
                )

    except Exception as e:
        logger.exception(f"Task failed: {e}")
        await status_callback(task_id, "error", f"Failed: {str(e)[:200]}")

    finally:
        if browser:
            await browser.close()
        if playwright_instance:
            await playwright_instance.stop()
```

## Step 2: Add the import and model to server.py

At the top of `server.py`, add the import:

```python
from tasks.your_task import run_your_task
```

Add a Pydantic request model:

```python
class YourTaskRequest(BaseModel):
    some_param: str
    client_slug: Optional[str] = None
    callback_url: Optional[str] = None
```

## Step 3: Add the endpoint

Add the POST endpoint (before the GET `/tasks/{task_id}/status` route):

```python
@app.post("/tasks/your-task", dependencies=[Depends(verify_api_key)])
async def create_your_task(request: YourTaskRequest):
    task_id = f"yourtask-{uuid.uuid4()}"
    now = datetime.now(timezone.utc).isoformat()

    tasks[task_id] = {
        "task_id": task_id,
        "type": "your-task",
        "status": "queued",
        "message": "Task queued, waiting for browser",
        "created_at": now,
        "updated_at": now,
        "error": None,
        "duration_seconds": None,
        "request": request.model_dump(),
    }

    asyncio.create_task(_run_your_task_with_lock(task_id))
    logger.info(f"Queued your-task {task_id[:12]}")

    return {"task_id": task_id, "status": "queued"}
```

## Step 4: Add the lock wrapper

At the bottom of `server.py`, add the execution wrapper:

```python
async def _run_your_task_with_lock(task_id: str):
    """Acquire the browser lock, then run the task."""
    async with browser_lock:
        try:
            from utils.cleanup import preflight_cleanup
            await asyncio.to_thread(preflight_cleanup)
        except Exception as cleanup_err:
            logger.warning(f"Pre-flight cleanup failed: {cleanup_err}")

        update_task(task_id, "running", "Starting task")
        try:
            env = {
                "AGENT_API_KEY": os.getenv("AGENT_API_KEY", ""),
                # Add other env vars your task needs
            }
            await run_your_task(
                task_id=task_id,
                params=tasks[task_id]["request"],
                status_callback=_async_update_task,
                env=env,
            )
        except Exception as e:
            logger.exception(f"Task {task_id[:12]} failed")
            update_task(task_id, "error", f"Failed: {str(e)[:200]}", error=str(e))

        await asyncio.sleep(LIGHT_TASK_COOLDOWN)  # or HEAVY_TASK_COOLDOWN for LLM tasks
```

**Choosing the right lock tier:**
- `browser_lock` for any task that opens a browser (Playwright or Browser Use)
- No lock for CPU-only tasks (like NEO overlay) that don't use a browser

**Choosing the right cooldown:**
- `HEAVY_TASK_COOLDOWN` (10s) for Browser Use + LLM tasks (memory-intensive)
- `LIGHT_TASK_COOLDOWN` (2s) for Playwright-only tasks

## Step 5: Rebuild and deploy

```bash
cd /opt/moonraker-agent
git pull
docker compose down
docker compose build
docker compose up -d
```

Or use `deploy.sh` which does this automatically.

## Step 6: Add the trigger in Client HQ

Create `api/trigger-your-task.js` in the client-hq repo:

```javascript
var sb = require('./_lib/supabase');
var { requireAdmin } = require('./_lib/auth');

module.exports = async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).end();
  await requireAdmin(req, res);

  var { client_slug } = req.body;

  // Call the agent
  var agentResp = await fetch(process.env.AGENT_URL + '/tasks/your-task', {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer ' + process.env.AGENT_API_KEY,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      some_param: 'value',
      client_slug: client_slug,
      callback_url: process.env.CLIENT_HQ_URL + '/api/ingest-your-task',
    }),
  });

  var data = await agentResp.json();
  return res.json({ ok: true, task_id: data.task_id });
};
```

## Checklist

- [ ] Task file created in `tasks/`
- [ ] Import added to `server.py`
- [ ] Pydantic model added
- [ ] POST endpoint added
- [ ] Lock wrapper function added
- [ ] Docker image rebuilt
- [ ] Trigger API route in Client HQ
- [ ] Callback/ingest API route in Client HQ (if needed)
- [ ] API contract documented in `docs/api-contract.md`
- [ ] Playbook added to `playbooks/` (if platform-specific)
