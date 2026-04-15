#!/bin/bash
# deploy.sh — Pull latest code from GitHub and rebuild the agent container
# Usage: ./deploy.sh [--no-cache]
#
# Run from /opt/moonraker-agent on the VPS, or trigger via admin/exec:
#   curl -X POST -H "Authorization: Bearer $TOKEN" \
#     https://agent.moonraker.ai/admin/exec \
#     -d '{"command": "cd /opt/moonraker-agent && bash deploy.sh", "timeout": 300}'

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Moonraker Agent Deploy ==="
echo "Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "Directory: $(pwd)"

# Step 1: Pull latest from GitHub
echo ""
echo "--- Step 1: Git pull ---"
git fetch origin main
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "Already up to date at $(git log --oneline -1)"
    # Still rebuild if --force flag is passed
    if [ "$1" != "--force" ] && [ "$1" != "--no-cache" ]; then
        echo "Use --force to rebuild anyway, or --no-cache for clean rebuild"
        echo "Checking if container is running..."
        if docker compose ps --format json | python3 -c "import sys,json; data=[json.loads(l) for l in sys.stdin if l.strip()]; print('running' if any(d.get('State')=='running' for d in data) else 'stopped')" 2>/dev/null | grep -q "running"; then
            echo "Container is running. No action needed."
            exit 0
        else
            echo "Container not running. Will restart."
        fi
    fi
else
    echo "Updating: $(git log --oneline -1) -> $(git log --oneline origin/main -1)"
    git reset --hard origin/main
    echo "Now at: $(git log --oneline -1)"
fi

# Step 2: Rebuild and restart container
echo ""
echo "--- Step 2: Docker rebuild ---"

BUILD_FLAG=""
if [ "$1" = "--no-cache" ]; then
    BUILD_FLAG="--no-cache"
    echo "Building with --no-cache (clean rebuild)"
fi

docker compose down
docker compose build $BUILD_FLAG
docker compose up -d

# Step 3: Wait for health check
echo ""
echo "--- Step 3: Health check ---"
sleep 5

AGENT_API_KEY=$(grep AGENT_API_KEY .env | head -1 | cut -d'=' -f2)
HEALTH=$(curl -s --max-time 10 -H "Authorization: Bearer $AGENT_API_KEY" http://127.0.0.1:8000/health 2>/dev/null || echo '{"status":"error"}')
STATUS=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null || echo "error")

if [ "$STATUS" = "ok" ]; then
    echo "Health check: OK"
    echo "$HEALTH" | python3 -m json.tool 2>/dev/null || echo "$HEALTH"
else
    echo "Health check: FAILED"
    echo "Response: $HEALTH"
    echo ""
    echo "Container logs (last 20 lines):"
    docker compose logs --tail 20
    exit 1
fi

echo ""
echo "=== Deploy complete ==="
echo "Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
