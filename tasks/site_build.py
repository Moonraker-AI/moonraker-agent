"""
tasks/site_build.py
===================
Site-migration job 3 (v4 add): operator-triggered build + deploy without a
per-page rewrite.

Spec: docs/site-migration-agent-job-spec.md §3.

Used after operator hand-edits to .astro files in the per-migration working
tree, or after token override changes that didn't go through site-rewrite.

Pipeline (mirrors §2.6 build + §2.7 deploy, with target prefix selectable):
  1. Verify the working tree at /tmp/build/<migration_id>/ exists. Refuse
     if absent — there's nothing to build.
  2. Run npm install (cached) + astro build.
  3. Walk dist/ and push every file to R2:
       deploy_to=staging    -> migration/<id>/staging/
       deploy_to=production -> migration/<id>/dist/
     With Content-Type and Cache-Control by extension (spec §2.7).
  4. Best-effort: re-run a per-section pixel diff for every captured page
     in the migration. (TODO: implement when staging origin screenshots
     are still on R2; v4 punts this to a future commit.)
  5. Patch site_migrations.last_built_at + last_deployed_at.

Reuses helpers from tasks/site_rewrite.py to keep the build/deploy code in
exactly one place.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from utils import r2_client
from utils.site_migration_db import log_error

from tasks.site_rewrite import (
    WORK_TREE_BASE,
    _deploy_dist,
    _patch_migration,
    _run_build,
)

logger = logging.getLogger("agent.site_build")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Public entrypoint ────────────────────────────────────────────────────────

async def run_site_build(task_id, params, status_callback, env=None):
    migration_id = params.get("migration_id")
    deploy_to = (params.get("deploy_to") or "staging").lower().strip()
    if deploy_to not in ("staging", "production"):
        msg = f"deploy_to must be 'staging' or 'production' (got {deploy_to!r})"
        await log_error(kind="site-build", migration_id=migration_id, page_id=None, error=msg)
        await status_callback(task_id, "error", msg)
        return

    await status_callback(
        task_id,
        "running",
        f"site-build starting (deploy_to={deploy_to})",
    )

    if not r2_client.is_configured():
        msg = "R2 not configured (R2_INGEST_URL / R2_INGEST_SECRET / R2_MIGRATION_BUCKET)"
        await log_error(kind="site-build", migration_id=migration_id, page_id=None, error=msg)
        await status_callback(task_id, "error", msg)
        return

    if not migration_id:
        msg = "migration_id required"
        await status_callback(task_id, "error", msg)
        return

    work_tree = WORK_TREE_BASE / str(migration_id)
    if not (work_tree / ".cloned").exists():
        msg = (
            f"working tree missing at {work_tree} — run /tasks/site-rewrite "
            "for at least one page before /tasks/site-build"
        )
        await log_error(kind="site-build", migration_id=migration_id, page_id=None, error=msg)
        await status_callback(task_id, "error", msg)
        return

    # 1. Build
    build_ok = await _run_build(work_tree, migration_id, status_callback, task_id)
    if not build_ok:
        return  # _run_build already logged + set status

    # 2. Deploy
    if deploy_to == "staging":
        target_prefix = f"migration/{migration_id}/staging/"
    else:
        target_prefix = f"migration/{migration_id}/dist/"

    deployed = await _deploy_dist(
        work_tree=work_tree,
        migration_id=migration_id,
        target_prefix=target_prefix,
        status_callback=status_callback,
        task_id=task_id,
    )
    if not deployed:
        await log_error(
            kind="site-build",
            migration_id=migration_id,
            page_id=None,
            error=f"deploy to {deploy_to} produced no uploads",
        )
        await status_callback(task_id, "error", "deploy step uploaded zero files")
        await _patch_migration(migration_id, {"last_built_at": _now()})
        return

    # 3. Patch migration row
    await _patch_migration(migration_id, {
        "last_built_at": _now(),
        "last_deployed_at": _now(),
    })

    # 4. Per-section visual diff is best-effort + deferred to a future
    # commit. The capture-time origin section screenshots live on R2 at
    # migration/<id>/raw/<sha>/sections/*.png; matching them to the
    # built page output is non-trivial and we punt for v4.
    await status_callback(
        task_id,
        "complete",
        f"site-build deployed to {deploy_to} ({target_prefix})",
    )
