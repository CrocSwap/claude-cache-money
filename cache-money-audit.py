#!/usr/bin/env python3
"""
cache-money-audit.py — Scan Claude Code session logs to find cold cache
rebuilds and calculate how much they cost you.

A "cold turn" is one where cache_creation >> cache_read, meaning the
entire context was rebuilt from scratch instead of reading from cache.
This happens when you come back after the TTL expires, resume a session,
or when the prompt prefix changes.

Usage:
    python3 cache-money-audit.py                    # last 7 days
    python3 cache-money-audit.py --days 30          # last 30 days
    python3 cache-money-audit.py --since 2026-03-01 # since specific date
    python3 cache-money-audit.py --json             # machine-readable output
    python3 cache-money-audit.py --verbose           # show every cold turn

Reads JSONL files from ~/.claude/projects/
"""

import json
import os
import sys
import glob
import argparse
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ─────────────────────── Pricing (per MTok, as of April 2026) ───────────────────────

PRICING = {
    "opus": {
        "input":         15.00,
        "cache_write":    6.25,   # 5m TTL (was different at 1h)
        "cache_read":     0.50,
        "output":        75.00,
    },
    "sonnet": {
        "input":          3.00,
        "cache_write":    1.50,
        "cache_read":     0.15,
        "output":        15.00,
    },
    "haiku": {
        "input":          0.80,
        "cache_write":    0.40,
        "cache_read":     0.04,
        "output":         4.00,
    },
}

def get_model_tier(model_name: str) -> str:
    model_lower = model_name.lower()
    if "opus" in model_lower:
        return "opus"
    elif "haiku" in model_lower:
        return "haiku"
    else:
        return "sonnet"

def cost_per_token(tier: str, token_type: str) -> float:
    return PRICING.get(tier, PRICING["sonnet"]).get(token_type, 0) / 1_000_000

# ─────────────────────── JSONL Parsing ───────────────────────

def parse_session_file(filepath: str, since: datetime):
    """Parse a single JSONL session file, yielding assistant turns with usage data."""
    seen_request_ids = set()

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Only assistant messages have usage data
                msg = record.get("message", {})
                if msg.get("role") != "assistant":
                    continue

                usage = msg.get("usage", {})
                if not usage:
                    continue

                # Deduplicate by message ID (streaming creates multiple entries)
                msg_id = msg.get("id", "")
                if msg_id:
                    if msg_id in seen_request_ids:
                        continue
                    seen_request_ids.add(msg_id)

                # Parse timestamp
                ts_str = record.get("timestamp", "")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue

                if ts < since:
                    continue

                cache_read = usage.get("cache_read_input_tokens", 0) or 0
                cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
                output_tokens = usage.get("output_tokens", 0) or 0
                input_tokens = usage.get("input_tokens", 0) or 0

                model = msg.get("model", "unknown")
                session_id = record.get("sessionId", os.path.basename(filepath).replace(".jsonl", ""))

                yield {
                    "timestamp": ts,
                    "session_id": session_id,
                    "model": model,
                    "tier": get_model_tier(model),
                    "cache_read": cache_read,
                    "cache_creation": cache_creation,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "file": filepath,
                }
    except (IOError, OSError) as e:
        print(f"Warning: Could not read {filepath}: {e}", file=sys.stderr)

def find_session_files():
    """Find all JSONL session files."""
    base = os.path.expanduser("~/.claude/projects")
    if not os.path.exists(base):
        print(f"No session directory found at {base}", file=sys.stderr)
        sys.exit(1)

    patterns = [
        os.path.join(base, "**", "*.jsonl"),
    ]

    files = set()
    for pattern in patterns:
        files.update(glob.glob(pattern, recursive=True))

    return sorted(files)

# ─────────────────────── Analysis ───────────────────────

def classify_turn(turn: dict) -> str:
    """Classify a turn as cold, warm, or first."""
    cr = turn["cache_read"]
    cc = turn["cache_creation"]
    total_cache = cr + cc

    if total_cache == 0:
        return "nocache"

    hit_ratio = cr / total_cache if total_cache > 0 else 0

    # First turn of a session always has cc > 0 and cr == 0
    # Cold turns also have cc >> cr, but after the first turn
    if cr == 0 and cc > 0:
        return "cold"
    elif hit_ratio < 0.5:
        return "cold"
    else:
        return "warm"

def calculate_cold_cost(turn: dict) -> dict:
    """Calculate actual cost and what it would have cost if cache was warm."""
    tier = turn["tier"]

    actual_write_cost = turn["cache_creation"] * cost_per_token(tier, "cache_write")
    actual_read_cost = turn["cache_read"] * cost_per_token(tier, "cache_read")

    # If cache had been warm, cache_creation tokens would have been cache_read instead
    hypothetical_read_cost = (turn["cache_creation"] + turn["cache_read"]) * cost_per_token(tier, "cache_read")

    actual_total = actual_write_cost + actual_read_cost
    hypothetical_total = hypothetical_read_cost

    wasted = actual_total - hypothetical_total

    return {
        "actual_cost": actual_total,
        "warm_cost": hypothetical_total,
        "wasted": max(0, wasted),
        "cache_write_cost": actual_write_cost,
        "cache_read_cost": actual_read_cost,
    }

def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)

def format_cost(c: float) -> str:
    if c >= 1.0:
        return f"${c:.2f}"
    elif c >= 0.01:
        return f"${c:.3f}"
    else:
        return f"${c:.4f}"

# ─────────────────────── Main ───────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Audit Claude Code session logs for cold cache rebuilds."
    )
    parser.add_argument("--days", type=int, default=7,
                        help="Analyze last N days (default: 7)")
    parser.add_argument("--since", type=str,
                        help="Analyze since date (YYYY-MM-DD), overrides --days")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show every cold turn")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--threshold", type=int, default=10000,
                        help="Min cache_creation tokens to count as significant cold turn (default: 10000)")
    args = parser.parse_args()

    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

    files = find_session_files()
    if not files:
        print("No session files found.", file=sys.stderr)
        sys.exit(1)

    # ── Collect all turns ──
    all_turns = []
    for f in files:
        for turn in parse_session_file(f, since):
            all_turns.append(turn)

    all_turns.sort(key=lambda t: t["timestamp"])

    if not all_turns:
        print(f"No turns found since {since.strftime('%Y-%m-%d')}.", file=sys.stderr)
        sys.exit(0)

    # ── Classify and analyze ──
    cold_turns = []
    warm_turns = []
    total_cache_write_tokens = 0
    total_cache_read_tokens = 0
    total_wasted = 0.0
    total_actual = 0.0

    by_model = defaultdict(lambda: {"cold_count": 0, "warm_count": 0,
                                     "wasted": 0.0, "cold_tokens": 0})
    by_session = defaultdict(lambda: {"cold_count": 0, "wasted": 0.0,
                                       "cold_tokens": 0, "model": ""})
    by_day = defaultdict(lambda: {"cold_count": 0, "wasted": 0.0,
                                   "cold_tokens": 0})

    # Track per-session turn order to identify first turns
    session_first_turn = {}

    for turn in all_turns:
        sid = turn["session_id"]
        is_first = sid not in session_first_turn
        if is_first:
            session_first_turn[sid] = turn["timestamp"]

        classification = classify_turn(turn)
        costs = calculate_cold_cost(turn)

        total_cache_write_tokens += turn["cache_creation"]
        total_cache_read_tokens += turn["cache_read"]
        total_actual += costs["actual_cost"]

        if classification == "cold" and turn["cache_creation"] >= args.threshold:
            cold_turns.append({
                **turn,
                **costs,
                "is_first_turn": is_first,
                "classification": classification,
            })
            total_wasted += costs["wasted"]

            tier = turn["tier"]
            day = turn["timestamp"].strftime("%Y-%m-%d")

            by_model[tier]["cold_count"] += 1
            by_model[tier]["wasted"] += costs["wasted"]
            by_model[tier]["cold_tokens"] += turn["cache_creation"]

            by_session[sid]["cold_count"] += 1
            by_session[sid]["wasted"] += costs["wasted"]
            by_session[sid]["cold_tokens"] += turn["cache_creation"]
            by_session[sid]["model"] = tier

            by_day[day]["cold_count"] += 1
            by_day[day]["wasted"] += costs["wasted"]
            by_day[day]["cold_tokens"] += turn["cache_creation"]
        else:
            warm_turns.append(turn)

    # Separate first-turn cold (unavoidable) from mid-session cold (preventable)
    first_turn_cold = [t for t in cold_turns if t["is_first_turn"]]
    mid_session_cold = [t for t in cold_turns if not t["is_first_turn"]]

    first_turn_wasted = sum(t["wasted"] for t in first_turn_cold)
    mid_session_wasted = sum(t["wasted"] for t in mid_session_cold)

    # ── Output ──
    if args.json:
        output = {
            "period": {
                "since": since.isoformat(),
                "until": datetime.now(timezone.utc).isoformat(),
            },
            "summary": {
                "total_turns": len(all_turns),
                "cold_turns": len(cold_turns),
                "first_turn_cold": len(first_turn_cold),
                "mid_session_cold": len(mid_session_cold),
                "warm_turns": len(warm_turns),
                "total_cache_write_tokens": total_cache_write_tokens,
                "total_cache_read_tokens": total_cache_read_tokens,
                "total_cache_cost": round(total_actual, 4),
                "total_wasted": round(total_wasted, 4),
                "preventable_waste": round(mid_session_wasted, 4),
                "unavoidable_first_turn": round(first_turn_wasted, 4),
            },
            "by_model": {k: {**v, "wasted": round(v["wasted"], 4)}
                         for k, v in by_model.items()},
            "by_day": {k: {**v, "wasted": round(v["wasted"], 4)}
                       for k, v in sorted(by_day.items())},
            "worst_sessions": sorted(
                [{"session": k, **v, "wasted": round(v["wasted"], 4)}
                 for k, v in by_session.items()],
                key=lambda x: x["wasted"], reverse=True
            )[:10],
        }
        if args.verbose:
            output["cold_turns"] = [
                {
                    "timestamp": t["timestamp"].isoformat(),
                    "session": t["session_id"][:12],
                    "model": t["tier"],
                    "cache_creation": t["cache_creation"],
                    "cache_read": t["cache_read"],
                    "wasted": round(t["wasted"], 4),
                    "is_first_turn": t["is_first_turn"],
                }
                for t in cold_turns
            ]
        print(json.dumps(output, indent=2))
        return

    # ── Human-readable output ──
    print()
    print("=" * 65)
    print("  CACHE MONEY AUDIT")
    print(f"  {since.strftime('%Y-%m-%d')} → {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print("=" * 65)
    print()

    print(f"  Total turns analyzed:     {len(all_turns):,}")
    print(f"  Warm turns (cache hit):   {len(warm_turns):,}")
    print(f"  Cold turns (cache miss):  {len(cold_turns):,}")
    print(f"    ├─ First turn (unavoidable):  {len(first_turn_cold):,}")
    print(f"    └─ Mid-session (preventable): {len(mid_session_cold):,}")
    print()

    print(f"  Total cache write tokens: {format_tokens(total_cache_write_tokens)}")
    print(f"  Total cache read tokens:  {format_tokens(total_cache_read_tokens)}")
    if total_cache_write_tokens + total_cache_read_tokens > 0:
        hit_rate = total_cache_read_tokens / (total_cache_write_tokens + total_cache_read_tokens) * 100
        print(f"  Overall cache hit rate:   {hit_rate:.1f}%")
    print()

    print("─" * 65)
    print("  COST IMPACT")
    print("─" * 65)
    print()
    print(f"  Total cold rebuild cost:     {format_cost(total_wasted)}")
    print(f"    ├─ Unavoidable (1st turn):  {format_cost(first_turn_wasted)}")
    print(f"    └─ Preventable (mid-sess):  {format_cost(mid_session_wasted)}")
    print()

    if mid_session_wasted > 0:
        print(f"  >>> {format_cost(mid_session_wasted)} spent on preventable "
              f"mid-session cold rebuilds <<<")
        print()

    # By model
    if by_model:
        print("─" * 65)
        print("  BY MODEL")
        print("─" * 65)
        print()
        print(f"  {'Model':<10} {'Cold turns':>12} {'Cold tokens':>14} {'Wasted':>12}")
        print(f"  {'─'*10} {'─'*12} {'─'*14} {'─'*12}")
        for tier in ["opus", "sonnet", "haiku"]:
            if tier in by_model:
                m = by_model[tier]
                print(f"  {tier:<10} {m['cold_count']:>12,} "
                      f"{format_tokens(m['cold_tokens']):>14} "
                      f"{format_cost(m['wasted']):>12}")
        print()

    # By day
    if by_day:
        print("─" * 65)
        print("  BY DAY")
        print("─" * 65)
        print()
        print(f"  {'Date':<12} {'Cold turns':>12} {'Cold tokens':>14} {'Wasted':>12}")
        print(f"  {'─'*12} {'─'*12} {'─'*14} {'─'*12}")
        for day in sorted(by_day.keys()):
            d = by_day[day]
            print(f"  {day:<12} {d['cold_count']:>12,} "
                  f"{format_tokens(d['cold_tokens']):>14} "
                  f"{format_cost(d['wasted']):>12}")
        print()

    # Worst sessions
    worst = sorted(by_session.items(), key=lambda x: x[1]["wasted"], reverse=True)[:5]
    if worst and worst[0][1]["wasted"] > 0:
        print("─" * 65)
        print("  WORST SESSIONS")
        print("─" * 65)
        print()
        print(f"  {'Session':<14} {'Model':<8} {'Cold':>6} {'Tokens':>12} {'Wasted':>12}")
        print(f"  {'─'*14} {'─'*8} {'─'*6} {'─'*12} {'─'*12}")
        for sid, s in worst:
            if s["wasted"] > 0:
                print(f"  {sid[:14]:<14} {s['model']:<8} "
                      f"{s['cold_count']:>6} "
                      f"{format_tokens(s['cold_tokens']):>12} "
                      f"{format_cost(s['wasted']):>12}")
        print()

    # Verbose: every cold turn
    if args.verbose and cold_turns:
        print("─" * 65)
        print("  ALL COLD TURNS")
        print("─" * 65)
        print()
        print(f"  {'Timestamp':<22} {'Session':<14} {'Model':<8} "
              f"{'Write':>10} {'Read':>10} {'Wasted':>10} {'Type':<6}")
        print(f"  {'─'*22} {'─'*14} {'─'*8} "
              f"{'─'*10} {'─'*10} {'─'*10} {'─'*6}")
        for t in cold_turns:
            ts = t["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            typ = "1st" if t["is_first_turn"] else "idle"
            print(f"  {ts:<22} {t['session_id'][:14]:<14} {t['tier']:<8} "
                  f"{format_tokens(t['cache_creation']):>10} "
                  f"{format_tokens(t['cache_read']):>10} "
                  f"{format_cost(t['wasted']):>10} {typ:<6}")
        print()

    print("=" * 65)
    print("  Tips:")
    print("  • First-turn cold starts are unavoidable — every session pays once.")
    print("  • Mid-session cold turns happen when idle gaps exceed the cache TTL.")
    print("  • Consider: /model sonnet → /compact → /model opus after idle gaps.")
    print("=" * 65)
    print()


if __name__ == "__main__":
    main()
