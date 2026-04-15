#!/bin/bash
# install.sh — copies Cache Money into ~/.claude/
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Create directories
mkdir -p ~/.claude/hooks

# Copy scripts
cp "$SCRIPT_DIR/statusline.sh" ~/.claude/statusline.sh
cp "$SCRIPT_DIR/cache-notify.sh" ~/.claude/hooks/cache-notify.sh
cp "$SCRIPT_DIR/cache-money-audit.py" ~/.claude/cache-money-audit.py
chmod +x ~/.claude/statusline.sh ~/.claude/hooks/cache-notify.sh

echo "Cache Money installed:"
echo "  ~/.claude/statusline.sh          (always-on cache metrics)"
echo "  ~/.claude/hooks/cache-notify.sh  (per-turn desktop notifications)"
echo "  ~/.claude/cache-money-audit.py   (historical cold cache analysis)"
echo ""
echo "Step 1: Merge the following into ~/.claude/settings.json:"
echo ""
cat "$SCRIPT_DIR/settings-snippet.jsonc"
echo ""
echo ""
echo "Step 2: Restart Claude Code"
echo ""
echo "Tip: Set CLAUDE_CACHE_TTL env var (default 300 = 5min)"
echo "  For Max plan with 1h TTL: export CLAUDE_CACHE_TTL=3600"
