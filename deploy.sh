#!/bin/bash
# Deploy Moonraker Agent Service to VPS
# Run from: /home/claude/moonraker-agent/
# Usage: bash deploy.sh

set -e

VPS="root@204.168.251.129"
REMOTE_DIR="/opt/moonraker-agent"

echo "=== Deploying Moonraker Agent Service ==="

# 1. Create tar of all files (exclude deploy script itself)
echo "Packaging files..."
tar czf /tmp/agent-deploy.tar.gz \
  --exclude='deploy.sh' \
  --exclude='.env.example' \
  -C /home/claude/moonraker-agent .

# 2. Upload to VPS
echo "Uploading to VPS..."
scp /tmp/agent-deploy.tar.gz $VPS:/tmp/

# 3. Extract and rebuild on VPS
echo "Extracting and rebuilding..."
ssh $VPS << 'REMOTE'
  set -e
  cd /opt/moonraker-agent

  # Back up current .env if it exists and has been customized
  if [ -f .env ]; then
    cp .env .env.backup
  fi

  # Extract new files
  tar xzf /tmp/agent-deploy.tar.gz

  # Restore backed-up .env if it had more content than the new one
  if [ -f .env.backup ]; then
    # Only restore if backup has filled-in keys (not just template)
    if grep -q "ANTHROPIC_API_KEY=sk-" .env.backup 2>/dev/null; then
      echo "Restoring customized .env from backup"
      cp .env.backup .env
    fi
  fi

  # Rebuild and restart
  echo "Rebuilding Docker image..."
  docker compose build --no-cache

  echo "Restarting service..."
  docker compose down
  docker compose up -d

  # Wait and check health
  sleep 5
  echo "Checking health..."
  curl -s http://localhost:8000/health | python3 -m json.tool

  echo "=== Deploy complete ==="
REMOTE

rm /tmp/agent-deploy.tar.gz
echo "Done!"
