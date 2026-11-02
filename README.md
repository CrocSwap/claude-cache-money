# claude-cache-money

Per-turn visibility into prompt cache hits vs misses in Claude Code.

## What you get

**Statusline** (always visible at bottom of terminal):
```
Opus | ctx:42% | ttl:237s HOT   | cache:96% r:180.2k w:0.5k     ← fresh, every turn cheap
Opus | ctx:42% | ttl:3200s WARM | cache:72% r:140.0k w:40.2k    ← probabilistic zone (~25%/turn invalidation)
Opus | ctx:42% | ttl:COLD       | cache:0% r:0.0k w:180.7k      ← fully expired, full rebuild
Opus | ctx:42% | ttl:48s HOT    | cache:91% r:165.0k w:15.2k    ← about to drop into WARM
```

## How it works

1. **Statusline** runs on every turn and every second (via `refreshInterval: 1`),
   receives `current_usage` with `cache_read_input_tokens` and
   `cache_creation_input_tokens`, and displays them.

2. The TTL counter **ticks down live** while you're idle — the statusline
   re-runs every second but only resets the timestamp when the API usage
   actually changes, so the countdown reflects real elapsed time since
   the last API call.

3. TTL is tracked **per `(session, model)`** — Anthropic's prompt cache is
   keyed on `(model, prefix)`, so switching models invalidates the current
   model's view but leaves the old model's cache alive server-side. Switch
   back within TTL and the statusline correctly shows the old model as
   still warm rather than falsely resetting to 300s. Effort/thinking-level
   changes are not detectable from the statusline input JSON, so those
   appear as generic new turns.

## Decision framework

Three cache states based on elapsed time since the last API call:
- **HOT** — cache definitely warm. Next turn ~10% of base price.
- **WARM** — probabilistic decay; empirically ~25% chance of invalidation per turn on Max plan. Cache may still hit but don't count on it.
- **COLD** — past the hard TTL, full rebuild guaranteed.

| Status | Context useful? | Action |
|--------|-----------------|--------|
| HOT    | No/maybe        | `/clear` — reset while rebuild would still be cheap |
| HOT    | Yes             | Continue — next turn is ~10% of base price |
| WARM   | No/maybe        | `/clear` — reliability degrading anyway |
| WARM   | Yes             | Continue — gamble on the ~75% hit rate |
| COLD   | No/maybe        | `/clear` — you're paying full rebuild regardless |
| COLD   | Yes             | Continue — pay the rebuild, keep context |

## Components

### 1. Statusline (`statusline.sh`)
Always-visible cache metrics at the bottom of your terminal.
- Three-state cache display: **HOT** (green) / **WARM** (yellow) / **COLD** (red),
  with a live countdown to the next transition
- Per-turn cache hit % with read/write token breakdown
- Per-`(session, model)` TTL tracking so model switch-backs revalidate
  instead of falsely resetting

### 2. Audit (`cache-money-audit.py`)
Scans your JSONL session logs and calculates how much you've spent on
cold cache rebuilds — and how much was preventable.

```bash
python3 cache-money-audit.py                       # last 7 days (subscription mode)
python3 cache-money-audit.py --start 18h           # relative lookback
python3 cache-money-audit.py --start 2026-09-01    # ISO date or datetime
python3 cache-money-audit.py --since-reset         # since the last weekly limit reset
python3 cache-money-audit.py --since-5h-reset      # current five-hour limit window
python3 cache-money-audit.py --previous-reset      # the weekly window before that
python3 cache-money-audit.py --previous-5h-reset   # the last finished five-hour window
python3 cache-money-audit.py --days 30             # last 30 days
python3 cache-money-audit.py --mode api            # frame waste as dollars
python3 cache-money-audit.py --infer-ttl           # estimate cache TTL from logs
python3 cache-money-audit.py --no-by-project       # hide repository breakdowns
python3 cache-money-audit.py --verbose             # show every cold turn
python3 cache-money-audit.py --json                # machine-readable
```

`--start` accepts a lookback (`30m`, `18h`, `7d`, or `2w`) or an ISO date or
datetime, interpreted as UTC when no offset is present. `--days N` and
`--since DATE` remain available as shorthands.

`--since-reset` runs the usual report from the last weekly limit reset.
Claude Code only logs a reset time when a limit is hit, so it takes the most
recent weekly limit hit in the logs and rolls that reset forward in whole
weeks. It fails if the logs hold no weekly limit hit, and is wrong if your
weekly reset time has moved since the last one.

`--since-5h-reset` covers the current five-hour window. This is an estimate:
starting from the last logged five-hour limit hit, it replays your activity,
opening each window on the first turn after the previous one ends (floored to
the hour). Usage outside Claude Code on this machine also opens windows and
is not in the logs.

`--previous-reset` reports the weekly window before the current one, e.g.
last week's usage once a new week has started. It runs from one week before
the inferred reset up to that reset, with the same caveats as `--since-reset`.

`--previous-5h-reset` reports the last finished five-hour window: the one
before the current window, or the most recent one if none is open. It uses
the same replay as `--since-5h-reset`. If the next window's start (floored to
the hour) falls before this one's reset, it ends where the next one begins,
so no turn is counted in both. It fails if no earlier window is in the logs.

None of these can be combined with each other or with `--start`, `--days`, or
`--since`. In `--json` output, `period.reset_window` records the inferred
window, including `ended_at` for the previous-window modes.

Spend framing (two modes):
- **`subscription`** (default) — cold-rebuild waste and per-session costs
  framed as percent of total token spend. For Pro/Max users, rate-limit
  headroom is the real constraint.
- **`api`** — same spend breakdown up top, but waste shown in dollars.

The report also breaks all usage down by exact model and project. Model tables
show calls, input, output, cache hit rate, cache writes, and API-equivalent
cost, followed by spend and token splits for uncached input, cache writes,
cache reads, and output.

Project grouping uses the working directory recorded in each Claude session
event and walks up to the nearest Git repository. This keeps subdirectories
and agent worktrees under their parent repository. Each project includes the
same spend and token splits, with indented rows for the exact models used
there. Use `--top-projects N` to change the number shown (default 15), or
`--no-by-project` to omit this section. The same data is available under
`spend_breakdown.by_exact_model` and `by_project` in `--json` output.

TTL inference (`--infer-ttl`):
Buckets intra-session turn pairs by the gap since the previous turn and
computes the cold-rate in each bucket. Finds the **cold threshold** — the
gap at which cold-rate approaches ~1. The probabilistic WARM zone (a
gradual rise in cold-rate starting around the 5-min mark) doesn't produce
a clean cliff, so the inference result is primarily the hard cold point,
not the warm point. Useful for sanity-checking whether your plan is
actually giving you the 5m or 60m cold TTL you expect — e.g. the Max-plan
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

Two thresholds mark the HOT → WARM → COLD transitions. Both are measured
from the same cache-point timestamp (so if warm=300 and cold=3600, you're
WARM for the 55 minutes between the 5-min and 60-min marks).

```bash
# Warm threshold: after this elapsed time, cache becomes probabilistic.
export CLAUDE_CACHE_TTL_WARM=300    # 5 min (default)

# Cold threshold: after this, cache is fully invalidated.
export CLAUDE_CACHE_TTL_COLD=3600   # 60 min (default — matches Max empirical)
```

Defaults match Max-plan empirical observation. Pro/API users on the older
single-stage 5-minute TTL should collapse the WARM zone:

```bash
export CLAUDE_CACHE_TTL_WARM=300
export CLAUDE_CACHE_TTL_COLD=300   # straight HOT → COLD, no WARM
```

Back-compat: the old `CLAUDE_CACHE_TTL` is still honored as a fallback for
`CLAUDE_CACHE_TTL_COLD`, so an existing `CLAUDE_CACHE_TTL=3600` export keeps
working.

## Install

```bash
bash install.sh
```

This copies the scripts into `~/.claude/` **and** patches
`~/.claude/settings.json` for you via `jq`:
- Sets `statusLine` to invoke our script with `refreshInterval: 1` (live TTL).
- Backs up your existing `settings.json` to `.bak` first.
- Scrubs any leftover `cache-notify.sh` Stop hook from older versions of
  this tool (idempotent — safe to re-run).

Restart Claude Code afterwards — `refreshInterval` is not hot-reloaded.

If `jq` isn't installed, the script prints the snippet for manual
merging and exits without touching your settings.

## Dependencies

- `jq` (for JSON parsing)

## Notes

- Cache TTL on Max plans has been unstable — see
  [#46829](https://github.com/anthropics/claude-code/issues/46829).
  Run `python3 cache-money-audit.py --infer-ttl` on your logs to confirm
  what your cold threshold actually is before trusting the defaults.
- If you disable telemetry, you may lose 1h TTL even on Max — see
  [#45381](https://github.com/anthropics/claude-code/issues/45381).
  In that case set `CLAUDE_CACHE_TTL_COLD=300` to collapse the WARM zone.
