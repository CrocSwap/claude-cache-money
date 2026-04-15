# claude-cache-money

Per-turn visibility into prompt cache hits vs misses in Claude Code.

## What you get

**Statusline** (always visible at bottom of terminal):
```
Opus | ctx:42% | ttl:237s | cache:96% r:180.2k w:0.5k    ← warm, almost all cached
Opus | ctx:42% | ttl:COLD | cache:0% r:0.0k w:180.7k     ← cold, full rebuild
Opus | ctx:42% | ttl:48s  | cache:91% r:165.0k w:15.2k   ← warm but expiring soon
```

**Desktop notification** (after each turn):
```
┌──────────────────────────────────┐
│ CACHE HIT 96%                    │
│ Read: 180.2k | Write: 0.5k      │
│ Context: 42% | Total in: 181k   │
└──────────────────────────────────┘
```

## How it works

1. **Statusline** runs after each API call, receives `current_usage` with 
   `cache_read_input_tokens` and `cache_creation_input_tokens`. Displays 
   them and writes stats to `~/.claude/.last_cache_stats`.

2. **Stop hook** fires when Claude finishes a turn, reads the stats file, 
   and sends a desktop notification with the breakdown.

3. The statusline also tracks time since last API call to predict whether 
   the *next* prompt will hit cache or pay cold-start.

## Decision framework

| Statusline shows | Context useful? | Action |
|------------------|-----------------|--------|
| `ttl:COLD`       | No/maybe        | `/clear` — you're paying full price anyway |
| `ttl:COLD`       | Yes             | Continue — pay the rebuild, keep context |
| `ttl:237s`       | No/maybe        | `/clear` — cache is warm but context isn't helping |
| `ttl:237s`       | Yes             | Continue — next turn is cheap (10% of base) |

## Components

### 1. Statusline (`statusline.sh`)
Always-visible cache metrics at the bottom of your terminal.
- TTL countdown with color coding (green → yellow → red → COLD)
- Per-turn cache hit % with read/write token breakdown
- Writes stats to disk for the other components to read

### 2. Stop Hook (`cache-notify.sh`)
Desktop notification after each turn showing cache hit/miss breakdown.
Runs async so it never blocks Claude's response.

### 3. Audit (`cache-money-audit.py`)
Scans your JSONL session logs and calculates how much you've spent on
cold cache rebuilds — and how much was preventable.

```bash
python3 cache-money-audit.py                       # last 7 days (subscription mode)
python3 cache-money-audit.py --days 30             # last 30 days
python3 cache-money-audit.py --mode api            # frame waste as dollars
python3 cache-money-audit.py --infer-ttl           # estimate cache TTL from logs
python3 cache-money-audit.py --verbose             # show every cold turn
python3 cache-money-audit.py --json                # machine-readable
```

Spend framing (two modes):
- **`subscription`** (default) — cold-rebuild waste and per-session costs
  framed as percent of total token spend. For Pro/Max users, rate-limit
  headroom is the real constraint.
- **`api`** — same spend breakdown up top, but waste shown in dollars.

TTL inference (`--infer-ttl`):
Buckets intra-session turn pairs by the gap since the previous turn and
computes the cold-rate in each bucket. TTL is the gap at which cold-rate
jumps from ~0 to ~1. Useful for sanity-checking whether your plan is
actually giving you the 5m or 1h TTL you expected — e.g. the Max-plan
1h TTL has been flaky ([#46829](https://github.com/anthropics/claude-code/issues/46829)).

Example output:
```
=================================================================
  CACHE MONEY AUDIT
  2026-03-15 → 2026-04-14
=================================================================

  Total turns analyzed:     1,847
  Warm turns (cache hit):   1,612
  Cold turns (cache miss):  235
    ├─ First turn (unavoidable):  89
    └─ Mid-session (preventable): 146

  Total cache write tokens: 48.2M
  Total cache read tokens:  892.1M
  Overall cache hit rate:   94.9%

─────────────────────────────────────────────────────────────────
  COST IMPACT
─────────────────────────────────────────────────────────────────

  Total cold rebuild cost:     $287.42
    ├─ Unavoidable (1st turn):  $53.18
    └─ Preventable (mid-sess):  $234.24

  >>> $234.24 spent on preventable mid-session cold rebuilds <<<
```

Key distinction: first-turn cold starts are unavoidable (every new
session builds the cache once). Mid-session cold turns happen when you
come back after TTL expiry — these are the ones you can actually
reduce, by `/clear`ing earlier or not letting idle gaps exceed the
TTL.

## Config

Set your TTL via environment variable before launching Claude Code:

```bash
# Pro/API plan (5 minute TTL) — this is the default
export CLAUDE_CACHE_TTL=300

# Max plan (1 hour TTL) — verify this is actually active for you
export CLAUDE_CACHE_TTL=3600
```

## Install

```bash
bash install.sh
```

Then merge `settings-snippet.jsonc` into your `~/.claude/settings.json`
and restart Claude Code.

## Dependencies

- `jq` (for JSON parsing)
- `osascript` (macOS) or `notify-send` (Linux) for desktop notifications

## Notes

- The notification runs as `async: true` so it doesn't block Claude's response cycle.
- Cache TTL on Max plans has been unstable — see
  [#46829](https://github.com/anthropics/claude-code/issues/46829).
  Start with TTL=300 and verify your actual hit rates.
- If you disable telemetry, you may lose 1h TTL even on Max — see
  [#45381](https://github.com/anthropics/claude-code/issues/45381).
