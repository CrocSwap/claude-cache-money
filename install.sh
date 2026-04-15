#!/bin/bash
# install.sh — copies Cache Money into ~/.claude/ and patches settings.json
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"

cp "$SCRIPT_DIR/statusline.sh"         "$HOME/.claude/statusline.sh"
cp "$SCRIPT_DIR/cache-money-audit.py"  "$HOME/.claude/cache-money-audit.py"
chmod +x "$HOME/.claude/statusline.sh"

# Remove any leftover from the older version of this tool that shipped a
# Stop-hook notifier. A dangling hook pointing at a missing file would spam
# exec errors every turn.
if [ -f "$HOME/.claude/hooks/cache-notify.sh" ]; then
    rm "$HOME/.claude/hooks/cache-notify.sh"
    echo "Removed stale ~/.claude/hooks/cache-notify.sh (from older install)."
fi

echo "Files installed:"
echo "  ~/.claude/statusline.sh          (always-on cache metrics)"
echo "  ~/.claude/cache-money-audit.py   (historical cold cache analysis)"
echo ""

if ! command -v jq >/dev/null 2>&1; then
    echo "⚠  jq is not installed — cannot auto-patch settings.json."
    echo "   Install jq (brew install jq / apt install jq), rerun, or paste"
    echo "   this snippet into $SETTINGS manually:"
    echo ""
    cat "$SCRIPT_DIR/settings-snippet.jsonc"
    exit 0
fi

# Start settings.json if missing
if [ ! -f "$SETTINGS" ]; then
    echo "{}" > "$SETTINGS"
fi

# Back up before modifying
BACKUP="$SETTINGS.bak"
cp "$SETTINGS" "$BACKUP"

# Warn if an existing statusLine points elsewhere — we overwrite, but the
# backup file is right there for restoration.
EXISTING_CMD=$(jq -r '.statusLine.command // empty' "$SETTINGS" 2>/dev/null || true)
if [ -n "$EXISTING_CMD" ] && [ "$EXISTING_CMD" != "bash ~/.claude/statusline.sh" ]; then
    echo "⚠  Replacing existing statusLine command:"
    echo "     old: $EXISTING_CMD"
    echo "     new: bash ~/.claude/statusline.sh"
    echo ""
fi

# Merge: overwrite statusLine (this is our tool's config) and scrub any
# Stop-hook entry left over from the older version that shipped cache-notify.
TMP=$(mktemp)
jq '
  .statusLine = {
    "type": "command",
    "command": "bash ~/.claude/statusline.sh",
    "refreshInterval": 1
  }
  | if .hooks.Stop then
      .hooks.Stop |= map(
        .hooks |= map(select(.command != "bash ~/.claude/hooks/cache-notify.sh"))
      )
      | .hooks.Stop |= map(select((.hooks // []) | length > 0))
      | if (.hooks.Stop | length) == 0 then del(.hooks.Stop) else . end
    else . end
  | if (.hooks // {} | length) == 0 then del(.hooks) else . end
' "$SETTINGS" > "$TMP"

if [ ! -s "$TMP" ]; then
    echo "✗ jq failed to produce output — settings.json unchanged."
    rm -f "$TMP"
    exit 1
fi

mv "$TMP" "$SETTINGS"

echo "Patched $SETTINGS:"
echo "  statusLine.command         = bash ~/.claude/statusline.sh"
echo "  statusLine.refreshInterval = 1           (live TTL countdown)"
echo ""
echo "Backup saved to $BACKUP."
echo "To undo this install, run:"
echo "  mv $BACKUP $SETTINGS"
echo ""
echo "Restart Claude Code for changes to take effect."
echo ""
echo "Tip: set CLAUDE_CACHE_TTL in your shell profile (default 300 = 5min)."
echo "     For Max plan with 1h TTL: export CLAUDE_CACHE_TTL=3600"
