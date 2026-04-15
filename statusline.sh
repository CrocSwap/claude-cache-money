#!/bin/bash
# ~/.claude/statusline.sh
# Shows model, context %, cache TTL countdown, and per-turn cache hit/miss breakdown.
# Also writes last-turn cache stats to a file for the Stop hook to pick up.

input=$(cat)

MODEL=$(echo "$input" | jq -r '.model.display_name // "?"')
CTX_PCT=$(echo "$input" | jq -r '.context_window.used_percentage // 0' | awk '{printf "%d", $1}')
CTX_SIZE=$(echo "$input" | jq -r '.context_window.context_window_size // 200000')
USAGE=$(echo "$input" | jq '.context_window.current_usage')

# --- TTL config ---
# Set to 300 for Pro/API (5m), 3600 for Max (1h)
# Check your actual TTL - Max may have regressed to 5m, see github.com/anthropics/claude-code/issues/46829
TTL=${CLAUDE_CACHE_TTL:-300}

CACHE_FILE="$HOME/.claude/.last_cache_stats"
TIMESTAMP_FILE="$HOME/.claude/.last_api_call"

# --- Cache TTL countdown ---
if [ -f "$TIMESTAMP_FILE" ]; then
    LAST_TS=$(cat "$TIMESTAMP_FILE")
    NOW=$(date +%s)
    ELAPSED=$((NOW - LAST_TS))
    REMAINING=$((TTL - ELAPSED))

    if [ $ELAPSED -ge $TTL ]; then
        TTL_DISPLAY="\033[91mCOLD\033[0m"
    elif [ $REMAINING -le 60 ]; then
        TTL_DISPLAY="\033[93m${REMAINING}s\033[0m"
    else
        TTL_DISPLAY="\033[92m${REMAINING}s\033[0m"
    fi
else
    TTL_DISPLAY="\033[90m--\033[0m"
fi

# --- Per-turn cache breakdown ---
if [ "$USAGE" != "null" ] && [ "$USAGE" != "" ]; then
    INPUT=$(echo "$USAGE" | jq '.input_tokens // 0')
    CR=$(echo "$USAGE" | jq '.cache_read_input_tokens // 0')
    CC=$(echo "$USAGE" | jq '.cache_creation_input_tokens // 0')
    OUTPUT=$(echo "$USAGE" | jq '.output_tokens // 0')

    CACHED_TOTAL=$((CR + CC))

    # Format token counts as K for readability
    CR_K=$(awk "BEGIN {printf \"%.1f\", $CR/1000}")
    CC_K=$(awk "BEGIN {printf \"%.1f\", $CC/1000}")
    INPUT_K=$(awk "BEGIN {printf \"%.1f\", $INPUT/1000}")

    # Hit percentage (of cacheable tokens)
    if [ $CACHED_TOTAL -gt 0 ]; then
        HIT_PCT=$((CR * 100 / CACHED_TOTAL))

        if [ $HIT_PCT -ge 80 ]; then
            HIT_COLOR="\033[92m"  # green
        elif [ $HIT_PCT -ge 40 ]; then
            HIT_COLOR="\033[93m"  # yellow
        else
            HIT_COLOR="\033[91m"  # red
        fi

        CACHE_DISPLAY="${HIT_COLOR}${HIT_PCT}%\033[0m r:${CR_K}k w:${CC_K}k"
    else
        CACHE_DISPLAY="\033[90mnew:${INPUT_K}k\033[0m"
    fi

    # Update timestamp (API call just happened)
    date +%s > "$TIMESTAMP_FILE"

    # Write stats for Stop hook notification
    cat > "$CACHE_FILE" <<EOF
{
  "input_tokens": $INPUT,
  "cache_read": $CR,
  "cache_creation": $CC,
  "output_tokens": $OUTPUT,
  "hit_pct": ${HIT_PCT:-0},
  "ctx_pct": $CTX_PCT
}
EOF
else
    CACHE_DISPLAY="\033[90m--\033[0m"
fi

printf "%s | ctx:%d%% | ttl:%b | cache:%b" "$MODEL" "$CTX_PCT" "$TTL_DISPLAY" "$CACHE_DISPLAY"
