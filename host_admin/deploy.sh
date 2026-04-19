#!/bin/bash
# ============================================================
# Moonraker Host Admin Service — Deployment Script
# ============================================================
# Run this on the VPS as root:
#   curl -sL <url> | bash
# Or paste the contents directly.
#
# What it does:
# 1. Installs Python dependencies (fastapi, uvicorn)
# 2. Creates /opt/moonraker-admin/ with the service code
# 3. Creates a .env file (reads AGENT_API_KEY from the agent container)
# 4. Sets up a systemd service
# 5. Detects the reverse proxy (Caddy/nginx) and adds routing
# 6. Starts the service
# ============================================================

set -e

echo "═══════════════════════════════════════════════════"
echo "  Moonraker Host Admin Service — Setup"
echo "═══════════════════════════════════════════════════"

# ── Step 1: Python deps ──────────────────────────────────────

echo ""
echo "[1/6] Installing Python dependencies..."

if ! command -v pip3 &>/dev/null; then
    apt-get update -qq && apt-get install -y -qq python3-pip
fi

pip3 install fastapi uvicorn python-dotenv --break-system-packages -q 2>/dev/null || \
pip3 install fastapi uvicorn python-dotenv -q

echo "  ✓ Dependencies installed"

# ── Step 2: Create service directory ─────────────────────────

echo ""
echo "[2/6] Creating /opt/moonraker-admin/..."

mkdir -p /opt/moonraker-admin

# Copy the service file (assumes admin_service.py is in current directory
# or we write it inline)
if [ -f "./admin_service.py" ]; then
    cp ./admin_service.py /opt/moonraker-admin/admin_service.py
    echo "  ✓ Copied admin_service.py"
else
    echo "  ✗ admin_service.py not found in current directory."
    echo "    Place it here and re-run, or copy it to /opt/moonraker-admin/ manually."
    exit 1
fi

# ── Step 3: Environment file ────────────────────────────────

echo ""
echo "[3/6] Setting up environment..."

# Try to read the AGENT_API_KEY from the running agent container
AGENT_KEY=""
if docker inspect moonraker-agent &>/dev/null; then
    AGENT_KEY=$(docker exec moonraker-agent printenv AGENT_API_KEY 2>/dev/null || echo "")
fi

if [ -z "$AGENT_KEY" ]; then
    echo "  Could not read AGENT_API_KEY from agent container."
    read -p "  Enter AGENT_API_KEY manually: " AGENT_KEY
fi

cat > /opt/moonraker-admin/.env << ENVEOF
AGENT_API_KEY=${AGENT_KEY}
ENVEOF

chmod 600 /opt/moonraker-admin/.env
echo "  ✓ Environment file created"

# ── Step 4: Systemd service ─────────────────────────────────

echo ""
echo "[4/6] Creating systemd service..."

cat > /etc/systemd/system/moonraker-admin.service << 'SVCEOF'
[Unit]
Description=Moonraker Host Admin Service
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
WorkingDirectory=/opt/moonraker-admin
EnvironmentFile=/opt/moonraker-admin/.env
ExecStart=/usr/bin/python3 -m uvicorn admin_service:app --host 127.0.0.1 --port 8001
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable moonraker-admin
echo "  ✓ Systemd service created and enabled"

# ── Step 5: Reverse proxy configuration ─────────────────────

echo ""
echo "[5/6] Configuring reverse proxy..."

# Detect which reverse proxy is running
if command -v caddy &>/dev/null && systemctl is-active --quiet caddy; then
    echo "  Detected: Caddy"
    
    CADDYFILE=$(caddy environ 2>/dev/null | grep CADDYFILE | cut -d= -f2 || echo "/etc/caddy/Caddyfile")
    [ -z "$CADDYFILE" ] && CADDYFILE="/etc/caddy/Caddyfile"
    
    # Check if admin routes already configured
    if grep -q "admin" "$CADDYFILE" 2>/dev/null; then
        echo "  Admin routes may already exist in Caddyfile. Please verify:"
        echo "  $CADDYFILE"
    else
        echo ""
        echo "  Add this inside the agent.moonraker.ai block in $CADDYFILE:"
        echo ""
        echo '  handle /admin/* {'
        echo '      reverse_proxy 127.0.0.1:8001'
        echo '  }'
        echo ""
        echo "  Then run: systemctl reload caddy"
    fi

elif command -v nginx &>/dev/null && systemctl is-active --quiet nginx; then
    echo "  Detected: nginx"
    
    # Find the agent config
    NGINX_CONF=$(grep -rl "agent.moonraker.ai" /etc/nginx/ 2>/dev/null | head -1)
    
    if [ -n "$NGINX_CONF" ]; then
        if grep -q "admin" "$NGINX_CONF" 2>/dev/null; then
            echo "  Admin routes may already exist. Please verify: $NGINX_CONF"
        else
            echo ""
            echo "  Add this inside the server block in $NGINX_CONF:"
            echo ""
            echo '  location /admin/ {'
            echo '      proxy_pass http://127.0.0.1:8001;'
            echo '      proxy_set_header Host $host;'
            echo '      proxy_set_header X-Real-IP $remote_addr;'
            echo '      proxy_read_timeout 300s;'
            echo '  }'
            echo ""
            echo "  Then run: nginx -t && systemctl reload nginx"
        fi
    else
        echo "  Could not find nginx config for agent.moonraker.ai"
        echo "  Manually add a location /admin/ block proxying to 127.0.0.1:8001"
    fi

else
    echo "  No known reverse proxy detected (checked Caddy, nginx)."
    echo "  The admin service will listen on 127.0.0.1:8001."
    echo "  You'll need to configure your proxy to forward /admin/* to port 8001."
fi

# ── Step 6: Start ───────────────────────────────────────────

echo ""
echo "[6/6] Starting service..."

systemctl start moonraker-admin

sleep 2

if systemctl is-active --quiet moonraker-admin; then
    echo "  ✓ moonraker-admin is running"
    
    # Quick health check
    HEALTH=$(curl -s http://127.0.0.1:8001/admin/health 2>/dev/null)
    if echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print('  ✓ Health check passed:', d.get('status'))" 2>/dev/null; then
        true
    else
        echo "  ⚠ Service is running but health check failed. Check: journalctl -u moonraker-admin -n 20"
    fi
else
    echo "  ✗ Service failed to start. Check: journalctl -u moonraker-admin -n 20"
    exit 1
fi

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Setup complete!"
echo ""
echo "  Service:  moonraker-admin (systemd)"
echo "  Port:     127.0.0.1:8001"
echo "  Logs:     journalctl -u moonraker-admin -f"
echo "            /var/log/moonraker-admin.log"
echo ""
echo "  Test locally:"
echo "    curl -s http://127.0.0.1:8001/admin/health"
echo ""
echo "  Test via proxy (after proxy config):"
echo "    curl -s -H 'Authorization: Bearer <token>' \\"
echo "      https://agent.moonraker.ai/admin/system"
echo "═══════════════════════════════════════════════════"
