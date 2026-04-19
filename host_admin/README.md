# Moonraker Host Admin Service

Tiny FastAPI service that runs on the VPS **host** (not in Docker). Provides authenticated remote command execution so Claude (and humans) can operate the host via HTTPS. Reached at `https://agent.moonraker.ai/admin/*` because Caddy routes `/admin/*` to `127.0.0.1:8001` while the agent container owns everything else on `:8000`.

## Endpoints

- `GET  /admin/health` — auth probe, returns service metadata
- `POST /admin/exec`   — run a shell command. Body: `{"command": "...", "timeout": 60, "working_dir": "/tmp"}`. `timeout` clamps to 300s. stdout/stderr capped at 1MB.

Both require `Authorization: Bearer $AGENT_API_KEY` (timing-safe comparison).

## Security posture (as of 2026-04-19)

- Runs as unprivileged user `mradmin` (uid 996). In the `docker` group so `docker compose` works; cannot `sudo`, cannot write outside its own files.
- `AGENT_API_KEY` value in `/opt/moonraker-admin/.env` is **distinct** from the agent container's `AGENT_API_KEY`. The admin token is NOT in Vercel env and is not shared with CHQ.
- `.env` is `600 mradmin:mradmin`.
- Audit log at `/var/log/moonraker-admin/app.log` (10MB × 3 rotation, owned by mradmin).
- Every exec is logged with command text, timeout, cwd, exit code, duration.
- Bind is `127.0.0.1:8001`. Only Caddy reaches it, and Caddy terminates TLS.

## Install / update

```bash
# From this folder on the VPS
sudo bash deploy.sh

# Or manual:
sudo mkdir -p /opt/moonraker-admin
sudo cp admin_service.py /opt/moonraker-admin/
sudo cp moonraker-admin.service /etc/systemd/system/
sudo mkdir -p /etc/systemd/system/moonraker-admin.service.d
sudo cp override.conf /etc/systemd/system/moonraker-admin.service.d/
# .env must contain: AGENT_API_KEY=<admin-token-not-in-vercel>
sudo chmod 600 /opt/moonraker-admin/.env
sudo chown -R mradmin:mradmin /opt/moonraker-admin /var/log/moonraker-admin
sudo systemctl daemon-reload
sudo systemctl restart moonraker-admin.service
```

## Rotate admin token

```bash
# as root
NEW=$(openssl rand -hex 32)
printf 'AGENT_API_KEY=%s\n' "$NEW" > /opt/moonraker-admin/.env
chmod 600 /opt/moonraker-admin/.env
chown mradmin:mradmin /opt/moonraker-admin/.env
systemctl restart moonraker-admin.service
echo "New admin token: $NEW"  # save to password manager; do NOT put in Vercel
```

## Unit override gotcha

`moonraker-admin.service.d/override.conf` pins `WorkingDirectory=/opt/moonraker-admin`. Do not change — uvicorn loads `admin_service:app` from CWD, and setting it to `/home/mradmin` breaks startup with "Could not import module 'admin_service'".
