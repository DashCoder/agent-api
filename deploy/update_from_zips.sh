#!/bin/bash
# Update agent-api and agent-ui on the Mac mini from new zips, keeping settings, data and avatar clips.
# Run as aiagents after copying agent-api.zip and agent-ui.zip to ~/apps:
#   cd ~/apps && rm -rf _new && mkdir _new && unzip -q agent-api.zip -d _new && unzip -q agent-ui.zip -d _new \
#     && bash _new/agent-api/deploy/update_from_zips.sh
set -euo pipefail
export PATH=/opt/homebrew/bin:$PATH
cd ~/apps
[ -d _new/agent-api ] && [ -d _new/agent-ui ] || { echo "Unzip both zips into ~/apps/_new first"; exit 1; }

echo "→ agent-api: code (keeping .env, .venv, data)"
rsync -a --delete --exclude .env --exclude .venv --exclude data _new/agent-api/ agent-api/
agent-api/.venv/bin/pip install -q -e agent-api

echo "→ agent-ui: code (keeping avatar clips)"
rsync -a --delete --exclude node_modules --exclude dist --exclude public/avatar _new/agent-ui/ agent-ui/
cd agent-ui && npm ci --silent && npm run build --silent && mkdir -p dist/avatar && cp -R public/avatar/. dist/avatar/
cd ~/apps && rm -rf _new agent-api.zip agent-ui.zip

echo "✓ Updated. Now restart agent-api as admin:"
echo "   sudo launchctl kickstart -k system/ai.agents.agent-api"
