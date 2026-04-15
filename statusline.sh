#!/bin/bash
# ~/.claude/statusline.sh
# Shows model, context %, cache TTL countdown, and per-turn cache hit/miss breakdown.

input=$(cat)

MODEL=$(echo "$input" | jq -r '.model.display_name // "?"')
CTX_PCT=$(echo "$input" | jq -r '.context_window.used_percentage // 0' | awk '{printf "%d", $1}')
CTX_SIZE=$(echo "$input" | jq -r '.context_window.context_window_size // 200000')
USAGE=$(echo "$input" | jq '.context_window.current_usage')
SESSION_ID=$(echo "$input" | jq -r '.session_id // "default"' | tr -dc 'a-zA-Z0-9_-')
[ -z "$SESSION_ID" ] && SESSION_ID="default"

# The prompt cache is keyed on (model, prefix). Switching models invalidates
# the current-model cache but the old-model cache keeps living server-side —
# so switching back within TTL should see it as still warm. Partition the
# TTL state per (session, model) so switch-back auto-revalidates.
# Claude Code's statusline JSON does not expose effort/thinking level, so
# we can't partition on that — effort switches appear as generic new turns.
MODEL_ID=$(echo "$input" | jq -r '.model.id // .model.display_name // "unknown"' | tr -dc 'a-zA-Z0-9_.-')
[ -z "$MODEL_ID" ] && MODEL_ID="unknown"

# --- TTL config (two-stage) ---
# Empirically the cache has two decay points: a "warm" threshold where
# invalidations start happening probabilistically (~25%/turn observed on Max),
# and a "cold" threshold at which the cache is fully gone. Both thresholds
# are measured from the same cache timestamp (the last API-call touch),
# so WARM spans (TTL_WARM, TTL_COLD) of elapsed time.
# Defaults match Max-plan empirical observation. Pro/API users should set
# TTL_COLD=300 to collapse WARM away.
# Back-compat: the old single-var CLAUDE_CACHE_TTL maps to TTL_COLD so
# existing Max-plan setups (CLAUDE_CACHE_TTL=3600) keep working.
TTL_WARM=${CLAUDE_CACHE_TTL_WARM:-300}
TTL_COLD=${CLAUDE_CACHE_TTL_COLD:-${CLAUDE_CACHE_TTL:-3600}}

# Per-session state so concurrent Claude Code sessions don't race on
# shared files. Without this partition, two sessions overwrite each
# other's fingerprints and the TTL pins at the full value.
# TIMESTAMP_FILE is further per-model so that model switches show the
# correct TTL for the active model's cache (and revalidate on switch-back).
# USAGE_FILE is kept per-session (global fp): it's the "last turn we saw"
# marker used to detect new turns — a model switch without a turn must
# not count as a new turn, so the fp must be shared across models.
STATE_DIR="$HOME/.claude/cache-money"
mkdir -p "$STATE_DIR"
TIMESTAMP_FILE="$STATE_DIR/api-call-$SESSION_ID-$MODEL_ID"
USAGE_FILE="$STATE_DIR/usage-$SESSION_ID"

if [ -n "$CACHE_MONEY_DEBUG" ]; then
    {
        echo "━━━ invocation $(date '+%H:%M:%S') ━━━"
        echo "  session_id_raw=$(echo "$input" | jq -r '.session_id // "(absent)"')"
        echo "  SESSION_ID=$SESSION_ID"
        echo "  MODEL_ID=$MODEL_ID"
        echo "  usage_present=$([ "$USAGE" != "null" ] && [ "$USAGE" != "" ] && echo yes || echo NO)"
    } >> /tmp/cache-money-debug.log
fi

# --- Parse usage fields (needed for both change-detection and display) ---
if [ "$USAGE" != "null" ] && [ "$USAGE" != "" ]; then
    INPUT=$(echo "$USAGE" | jq '.input_tokens // 0')
    CR=$(echo "$USAGE" | jq '.cache_read_input_tokens // 0')
    CC=$(echo "$USAGE" | jq '.cache_creation_input_tokens // 0')
    OUTPUT=$(echo "$USAGE" | jq '.output_tokens // 0')
    CACHED_TOTAL=$((CR + CC))
    if [ $CACHED_TOTAL -gt 0 ]; then
        HIT_PCT=$((CR * 100 / CACHED_TOTAL))
    else
        HIT_PCT=0
    fi

    # Fingerprint just the token counts — these are what change on a real
    # new API call. We intentionally avoid comparing the whole usage JSON
    # because Claude Code may include auxiliary fields that drift between
    # refresh ticks, which would falsely look like a new turn and reset
    # the TTL countdown.
    USAGE_FP="$INPUT:$CR:$CC:$OUTPUT"
    LAST_FP=""
    [ -f "$USAGE_FILE" ] && LAST_FP=$(cat "$USAGE_FILE")

    if [ -n "$CACHE_MONEY_DEBUG" ]; then
        MATCH=$([ "$USAGE_FP" = "$LAST_FP" ] && echo same || echo NEW)
        if [ -f "$TIMESTAMP_FILE" ]; then
            ELAPSED_DBG="$(( $(date +%s) - $(cat "$TIMESTAMP_FILE") ))s"
        else
            ELAPSED_DBG="(no ts yet)"
        fi
        {
            echo "─── $(date '+%H:%M:%S') ───"
            echo "  session=$SESSION_ID  model=$MODEL_ID"
            echo "  ts_file=$TIMESTAMP_FILE  (exists=$([ -f "$TIMESTAMP_FILE" ] && echo yes || echo NO))"
            echo "  fp=$USAGE_FP  prev=$LAST_FP  -> $MATCH"
            echo "  elapsed_since_last_ts=$ELAPSED_DBG"
            echo "  raw_usage=$(echo "$USAGE" | jq -c .)"
        } >> /tmp/cache-money-debug.log
    fi

    if [ "$USAGE_FP" != "$LAST_FP" ]; then
        date +%s > "$TIMESTAMP_FILE"
        printf '%s' "$USAGE_FP" > "$USAGE_FILE"
    fi
fi

# --- Cache TTL countdown (read after potential timestamp update above) ---
# Three-state: HOT until TTL_WARM, WARM until TTL_COLD, COLD after.
# Countdown shown is "time until the next state transition".
if [ -f "$TIMESTAMP_FILE" ]; then
    LAST_TS=$(cat "$TIMESTAMP_FILE")
    NOW=$(date +%s)
    ELAPSED=$((NOW - LAST_TS))

    if [ $ELAPSED -ge $TTL_COLD ]; then
        TTL_DISPLAY="\033[91mCOLD\033[0m"
    elif [ $ELAPSED -ge $TTL_WARM ]; then
        REMAINING=$((TTL_COLD - ELAPSED))
        TTL_DISPLAY="\033[93m${REMAINING}s WARM\033[0m"
    else
        REMAINING=$((TTL_WARM - ELAPSED))
        TTL_DISPLAY="\033[92m${REMAINING}s HOT\033[0m"
    fi
else
    TTL_DISPLAY="\033[90m--\033[0m"
fi

# --- Per-turn cache breakdown display ---
if [ "$USAGE" != "null" ] && [ "$USAGE" != "" ]; then
    CR_K=$(awk "BEGIN {printf \"%.1f\", $CR/1000}")
    CC_K=$(awk "BEGIN {printf \"%.1f\", $CC/1000}")
    INPUT_K=$(awk "BEGIN {printf \"%.1f\", $INPUT/1000}")

    if [ $CACHED_TOTAL -gt 0 ]; then
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
else
    CACHE_DISPLAY="\033[90m--\033[0m"
fi

printf "%s | ctx:%d%% | ttl:%b | cache:%b" "$MODEL" "$CTX_PCT" "$TTL_DISPLAY" "$CACHE_DISPLAY"
