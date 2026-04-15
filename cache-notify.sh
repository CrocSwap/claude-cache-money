#!/bin/bash
# ~/.claude/hooks/cache-notify.sh
# Fires after each Claude turn with a desktop notification showing cache hit/miss.
# Reads stats written by the statusline script.

CACHE_FILE="$HOME/.claude/.last_cache_stats"

if [ ! -f "$CACHE_FILE" ]; then
    exit 0
fi

STATS=$(cat "$CACHE_FILE")
CR=$(echo "$STATS" | jq '.cache_read // 0')
CC=$(echo "$STATS" | jq '.cache_creation // 0')
INPUT=$(echo "$STATS" | jq '.input_tokens // 0')
OUTPUT=$(echo "$STATS" | jq '.output_tokens // 0')
HIT_PCT=$(echo "$STATS" | jq '.hit_pct // 0')
CTX_PCT=$(echo "$STATS" | jq '.ctx_pct // 0')

# Format as K
CR_K=$(awk "BEGIN {printf \"%.1fk\", $CR/1000}")
CC_K=$(awk "BEGIN {printf \"%.1fk\", $CC/1000}")
INPUT_K=$(awk "BEGIN {printf \"%.1fk\", $INPUT/1000}")
OUT_K=$(awk "BEGIN {printf \"%.1fk\", $OUTPUT/1000}")
TOTAL_IN=$((INPUT + CR + CC))
TOTAL_K=$(awk "BEGIN {printf \"%.1fk\", $TOTAL_IN/1000}")

# Determine if this was a cache hit or miss turn
if [ "$CC" -gt "$CR" ] && [ "$CC" -gt 1000 ]; then
    STATUS="CACHE MISS"
    SUBTITLE="Cache rebuilt — ${CC_K} tokens written"
else
    STATUS="CACHE HIT ${HIT_PCT}%"
    SUBTITLE="Read: ${CR_K} | Write: ${CC_K} | New: ${INPUT_K}"
fi

BODY="Context: ${CTX_PCT}% | Total in: ${TOTAL_K} | Out: ${OUT_K}"

# --- Platform notification ---
# macOS
if command -v osascript &>/dev/null; then
    osascript -e "display notification \"${BODY}\" with title \"${STATUS}\" subtitle \"${SUBTITLE}\""
# Linux
elif command -v notify-send &>/dev/null; then
    notify-send "${STATUS}" "${SUBTITLE}\n${BODY}" --urgency=low
fi

exit 0
