#!/usr/bin/env python3
"""
cache-money-audit.py — Scan Claude Code session logs to find cold cache
rebuilds and calculate how much they cost you.

A "cold turn" is one where cache_creation >> cache_read, meaning the
entire context was rebuilt from scratch instead of reading from cache.
This happens when you come back after the TTL expires, resume a session,
or when the prompt prefix changes.

Usage:
    python3 cache-money-audit.py                       # last 7 days (subscription mode)
    python3 cache-money-audit.py --days 30             # last 30 days
    python3 cache-money-audit.py --mode api            # frame waste as dollars
    python3 cache-money-audit.py --infer-ttl           # estimate cache TTL from logs
    python3 cache-money-audit.py --since 2026-03-01    # since specific date
    python3 cache-money-audit.py --json                # machine-readable output
    python3 cache-money-audit.py --verbose             # show every cold turn

Modes:
    subscription  Subscription user (Pro/Max) — frames cold-rebuild waste and
                  per-session costs as a percent of total token spend, since
                  rate-limit headroom is the real constraint. Token spend is
                  assumed proportional to dollar-equivalent API cost. Default.
    api           API user — dollar costs per token type / model.
    --infer-ttl   TTL inference — buckets intra-session turn pairs by elapsed
                  time and looks for the gap at which turns flip from warm
                  (cache hit) to cold (full rebuild). Useful for verifying
                  whether your plan is actually giving you the 5m or 1h TTL.

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

# ─────────────────────── TTL inference ───────────────────────

# Bucket edges (seconds). Fine-grained near 5m, coarser near 1h and beyond.
TTL_BUCKET_EDGES = [
    0, 30, 60, 90, 120, 180, 240, 300, 360, 420, 480, 540, 600,
    900, 1200, 1500, 1800, 2400, 3000, 3600, 4500, 5400, 7200,
    float("inf"),
]

# Known TTL values to match against (seconds, label)
CANONICAL_TTLS = [
    (300,  "5-min TTL (Pro/API default)"),
    (3600, "1-hour TTL (Max plan)"),
]

def build_turn_pairs(all_turns):
    """For each session, yield (gap_seconds, classification) for consecutive turns."""
    sessions = defaultdict(list)
    for t in all_turns:
        sessions[t["session_id"]].append(t)

    pairs = []
    for turns in sessions.values():
        turns_sorted = sorted(turns, key=lambda t: t["timestamp"])
        for i in range(1, len(turns_sorted)):
            gap = (turns_sorted[i]["timestamp"]
                   - turns_sorted[i-1]["timestamp"]).total_seconds()
            if gap <= 0:
                continue
            cls = classify_turn(turns_sorted[i])
            if cls in ("warm", "cold"):
                pairs.append((gap, cls == "cold"))
    return pairs

def bucket_pairs(pairs):
    """Count warm/cold pairs in each TTL_BUCKET_EDGES bucket."""
    buckets = [[0, 0] for _ in range(len(TTL_BUCKET_EDGES) - 1)]  # [warm, cold]
    for gap, is_cold in pairs:
        for i in range(len(TTL_BUCKET_EDGES) - 1):
            if TTL_BUCKET_EDGES[i] <= gap < TTL_BUCKET_EDGES[i+1]:
                buckets[i][1 if is_cold else 0] += 1
                break
    return buckets

def find_transition(buckets, min_samples=3, threshold=0.5):
    """Return index of first bucket where cold rate crosses `threshold`."""
    for i, (w, c) in enumerate(buckets):
        if w + c < min_samples:
            continue
        if c / (w + c) > threshold:
            return i
    return None

def format_gap(s) -> str:
    if s == float("inf"):
        return "∞"
    if s < 120:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s/60)}m"
    hrs = s / 3600
    return f"{int(hrs)}h" if s % 3600 == 0 else f"{hrs:.1f}h"

def best_canonical_match(lo, hi):
    """Return (ttl_seconds, label) for the canonical TTL that falls inside [lo, hi],
    or the closest one if none do."""
    in_range = [(t, lbl) for t, lbl in CANONICAL_TTLS if lo <= t <= hi]
    if in_range:
        return in_range[0]
    midpoint = (lo + hi) / 2 if hi != float("inf") else lo
    return min(CANONICAL_TTLS, key=lambda tl: abs(tl[0] - midpoint))

def run_ttl_inference(all_turns, as_json: bool):
    pairs = build_turn_pairs(all_turns)
    if len(pairs) < 20:
        msg = (f"Not enough intra-session warm/cold pairs ({len(pairs)}) to "
               f"infer TTL. Try increasing --days.")
        if as_json:
            print(json.dumps({"mode": "infer-ttl", "error": msg,
                              "pairs_analyzed": len(pairs)}, indent=2))
        else:
            print(msg, file=sys.stderr)
        sys.exit(1 if not as_json else 0)

    buckets = bucket_pairs(pairs)
    transition_idx = find_transition(buckets)

    warm_gaps = sorted(g for g, cold in pairs if not cold)
    cold_gaps = sorted(g for g, cold in pairs if cold)

    ttl_lo = ttl_hi = None
    canonical_ttl = canonical_label = None
    if transition_idx is not None:
        ttl_lo = TTL_BUCKET_EDGES[transition_idx]
        ttl_hi = TTL_BUCKET_EDGES[transition_idx + 1]
        canonical_ttl, canonical_label = best_canonical_match(ttl_lo, ttl_hi)

    if as_json:
        output = {
            "mode": "infer-ttl",
            "pairs_analyzed": len(pairs),
            "warm_count": len(warm_gaps),
            "cold_count": len(cold_gaps),
            "buckets": [
                {"gap_lo_s": TTL_BUCKET_EDGES[i],
                 "gap_hi_s": (TTL_BUCKET_EDGES[i+1]
                              if TTL_BUCKET_EDGES[i+1] != float("inf") else None),
                 "warm": w, "cold": c,
                 "cold_rate": round(c / (w + c), 4) if (w + c) > 0 else None}
                for i, (w, c) in enumerate(buckets) if (w + c) > 0
            ],
            "inferred_ttl_range_s": [ttl_lo, ttl_hi] if transition_idx is not None else None,
            "best_match_ttl_s": canonical_ttl,
            "best_match_label": canonical_label,
        }
        print(json.dumps(output, indent=2, default=str))
        return

    print()
    print("=" * 65)
    print("  CACHE TTL INFERENCE")
    print(f"  Analyzed {len(pairs):,} intra-session turn pairs "
          f"({len(warm_gaps):,} warm, {len(cold_gaps):,} cold)")
    print("=" * 65)
    print()

    print("  Warm vs cold turns by gap since previous turn in same session:")
    print()
    print(f"    {'Gap range':<14} {'Warm':>7} {'Cold':>7} {'Total':>7} {'Cold rate':>11}")
    print(f"    {'─'*14} {'─'*7} {'─'*7} {'─'*7} {'─'*11}")
    for i, (w, c) in enumerate(buckets):
        if w + c == 0:
            continue
        lo, hi = TTL_BUCKET_EDGES[i], TTL_BUCKET_EDGES[i+1]
        total = w + c
        rate = c / total
        range_str = f"{format_gap(lo)}–{format_gap(hi)}"
        marker = "  ←" if i == transition_idx else ""
        print(f"    {range_str:<14} {w:>7,} {c:>7,} {total:>7,} "
              f"{rate*100:>9.1f}%{marker}")
    print()

    if transition_idx is not None:
        prev_w, prev_c = buckets[transition_idx - 1] if transition_idx > 0 else (0, 0)
        cur_w, cur_c = buckets[transition_idx]
        prev_rate = prev_c / (prev_w + prev_c) if (prev_w + prev_c) else 0
        cur_rate = cur_c / (cur_w + cur_c) if (cur_w + cur_c) else 0

        print(f"  Inferred TTL range:  {format_gap(ttl_lo)} – {format_gap(ttl_hi)} "
              f"({int(ttl_lo)}s – {int(ttl_hi) if ttl_hi != float('inf') else '∞'}s)")
        print(f"  Transition:          cold rate jumps "
              f"{prev_rate*100:.0f}% → {cur_rate*100:.0f}%")
        if canonical_ttl is not None and ttl_lo <= canonical_ttl <= ttl_hi:
            print(f"  Best match:          {canonical_ttl}s — {canonical_label}")
        else:
            print(f"  Closest canonical:   {canonical_ttl}s — {canonical_label} "
                  f"(outside inferred range — investigate)")
    else:
        print("  No clear transition detected. Possible reasons:")
        print("  • Your sessions never idle long enough to hit TTL (good problem).")
        print("  • Not enough data in the analyzed window.")
        print("  • TTL exceeds the largest bucket (2h).")
    print()

    print("=" * 65)
    print("  Notes:")
    print("  • Heuristic — needs enough paired turns straddling the TTL.")
    print("  • First turn of each session is excluded (no predecessor gap).")
    print("  • 'Cold' = cache_creation > cache_read for that turn.")
    print("  • If your inferred TTL is 5m but you expected 1h, check whether")
    print("    telemetry is disabled (see github.com/anthropics/claude-code/issues/45381).")
    print("=" * 65)
    print()


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

def format_pct(frac: float) -> str:
    """Format a fraction (0..1) as a percentage string."""
    pct = frac * 100
    if pct >= 10:
        return f"{pct:.1f}%"
    elif pct >= 1:
        return f"{pct:.2f}%"
    elif pct > 0:
        return f"{pct:.3f}%"
    return "0%"

def format_spend(cost: float, grand_total: float, mode: str) -> str:
    """Dollar amount in api mode; percent of grand_total in subscription mode."""
    if mode == "subscription" and grand_total > 0:
        return format_pct(cost / grand_total)
    return format_cost(cost)

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
    parser.add_argument("--mode", choices=["api", "subscription"], default="subscription",
                        help="subscription: frame waste as percent of total token "
                             "spend (default); api: show dollar costs")
    parser.add_argument("--infer-ttl", action="store_true",
                        help="Infer approximate cache TTL from intra-session "
                             "turn gaps (ignores --mode)")
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

    if args.infer_ttl:
        run_ttl_inference(all_turns, as_json=args.json)
        return

    # ── Classify and analyze ──
    cold_turns = []
    warm_turns = []
    total_input_tokens = 0
    total_cache_write_tokens = 0
    total_cache_read_tokens = 0
    total_output_tokens = 0
    total_wasted = 0.0
    total_actual = 0.0

    # Full spend breakdown per model tier (all turns, not just cold)
    tier_breakdown = defaultdict(lambda: {
        "input_tokens": 0, "cache_write_tokens": 0,
        "cache_read_tokens": 0, "output_tokens": 0,
        "input_cost": 0.0, "cache_write_cost": 0.0,
        "cache_read_cost": 0.0, "output_cost": 0.0,
    })

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
        tier = turn["tier"]

        total_input_tokens += turn["input_tokens"]
        total_cache_write_tokens += turn["cache_creation"]
        total_cache_read_tokens += turn["cache_read"]
        total_output_tokens += turn["output_tokens"]
        total_actual += costs["actual_cost"]

        tb = tier_breakdown[tier]
        tb["input_tokens"] += turn["input_tokens"]
        tb["cache_write_tokens"] += turn["cache_creation"]
        tb["cache_read_tokens"] += turn["cache_read"]
        tb["output_tokens"] += turn["output_tokens"]
        tb["input_cost"] += turn["input_tokens"] * cost_per_token(tier, "input")
        tb["cache_write_cost"] += turn["cache_creation"] * cost_per_token(tier, "cache_write")
        tb["cache_read_cost"] += turn["cache_read"] * cost_per_token(tier, "cache_read")
        tb["output_cost"] += turn["output_tokens"] * cost_per_token(tier, "output")

        if classification == "cold" and turn["cache_creation"] >= args.threshold:
            cold_turns.append({
                **turn,
                **costs,
                "is_first_turn": is_first,
                "classification": classification,
            })
            total_wasted += costs["wasted"]

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

    # Grand totals by token type (across all models) and overall
    total_input_cost = sum(tb["input_cost"] for tb in tier_breakdown.values())
    total_cache_write_cost = sum(tb["cache_write_cost"] for tb in tier_breakdown.values())
    total_cache_read_cost = sum(tb["cache_read_cost"] for tb in tier_breakdown.values())
    total_output_cost = sum(tb["output_cost"] for tb in tier_breakdown.values())
    grand_total_cost = (total_input_cost + total_cache_write_cost
                        + total_cache_read_cost + total_output_cost)
    grand_total_tokens = (total_input_tokens + total_cache_write_tokens
                          + total_cache_read_tokens + total_output_tokens)

    # ── Output ──
    def share(cost: float) -> float:
        return round(cost / grand_total_cost, 6) if grand_total_cost > 0 else 0.0

    if args.json:
        output = {
            "period": {
                "since": since.isoformat(),
                "until": datetime.now(timezone.utc).isoformat(),
            },
            "mode": args.mode,
            "summary": {
                "total_turns": len(all_turns),
                "cold_turns": len(cold_turns),
                "first_turn_cold": len(first_turn_cold),
                "mid_session_cold": len(mid_session_cold),
                "warm_turns": len(warm_turns),
                "total_input_tokens": total_input_tokens,
                "total_cache_write_tokens": total_cache_write_tokens,
                "total_cache_read_tokens": total_cache_read_tokens,
                "total_output_tokens": total_output_tokens,
                "grand_total_tokens": grand_total_tokens,
                "grand_total_cost": round(grand_total_cost, 4),
                "total_cache_cost": round(total_actual, 4),
                "total_wasted": round(total_wasted, 4),
                "total_wasted_share": share(total_wasted),
                "preventable_waste": round(mid_session_wasted, 4),
                "preventable_waste_share": share(mid_session_wasted),
                "unavoidable_first_turn": round(first_turn_wasted, 4),
                "unavoidable_first_turn_share": share(first_turn_wasted),
            },
            "spend_breakdown": {
                "by_token_type": {
                    "input":       {"tokens": total_input_tokens,
                                    "cost": round(total_input_cost, 4),
                                    "share": share(total_input_cost)},
                    "cache_write": {"tokens": total_cache_write_tokens,
                                    "cost": round(total_cache_write_cost, 4),
                                    "share": share(total_cache_write_cost)},
                    "cache_read":  {"tokens": total_cache_read_tokens,
                                    "cost": round(total_cache_read_cost, 4),
                                    "share": share(total_cache_read_cost)},
                    "output":      {"tokens": total_output_tokens,
                                    "cost": round(total_output_cost, 4),
                                    "share": share(total_output_cost)},
                },
                "by_model": {
                    tier: {
                        "input_tokens": tb["input_tokens"],
                        "cache_write_tokens": tb["cache_write_tokens"],
                        "cache_read_tokens": tb["cache_read_tokens"],
                        "output_tokens": tb["output_tokens"],
                        "total_tokens": (tb["input_tokens"] + tb["cache_write_tokens"]
                                          + tb["cache_read_tokens"] + tb["output_tokens"]),
                        "input_cost": round(tb["input_cost"], 4),
                        "cache_write_cost": round(tb["cache_write_cost"], 4),
                        "cache_read_cost": round(tb["cache_read_cost"], 4),
                        "output_cost": round(tb["output_cost"], 4),
                        "total_cost": round(tb["input_cost"] + tb["cache_write_cost"]
                                            + tb["cache_read_cost"] + tb["output_cost"], 4),
                        "share": share(tb["input_cost"] + tb["cache_write_cost"]
                                       + tb["cache_read_cost"] + tb["output_cost"]),
                    }
                    for tier, tb in tier_breakdown.items()
                },
            },
            "by_model": {k: {**v, "wasted": round(v["wasted"], 4),
                             "wasted_share": share(v["wasted"])}
                         for k, v in by_model.items()},
            "by_day": {k: {**v, "wasted": round(v["wasted"], 4),
                           "wasted_share": share(v["wasted"])}
                       for k, v in sorted(by_day.items())},
            "worst_sessions": sorted(
                [{"session": k, **v, "wasted": round(v["wasted"], 4),
                  "wasted_share": share(v["wasted"])}
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
                    "wasted_share": share(t["wasted"]),
                    "is_first_turn": t["is_first_turn"],
                }
                for t in cold_turns
            ]
        print(json.dumps(output, indent=2))
        return

    # ── Human-readable output ──
    is_sub = args.mode == "subscription"
    waste_col = "% spend" if is_sub else "Wasted"

    print()
    print("=" * 65)
    print(f"  CACHE MONEY AUDIT  [{args.mode} mode]")
    print(f"  {since.strftime('%Y-%m-%d')} → {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    print("=" * 65)
    print()

    print(f"  Total turns analyzed:     {len(all_turns):,}")
    print(f"  Warm turns (cache hit):   {len(warm_turns):,}")
    print(f"  Cold turns (cache miss):  {len(cold_turns):,}")
    print(f"    ├─ First turn (unavoidable):  {len(first_turn_cold):,}")
    print(f"    └─ Mid-session (preventable): {len(mid_session_cold):,}")
    print()

    if total_cache_write_tokens + total_cache_read_tokens > 0:
        hit_rate = total_cache_read_tokens / (total_cache_write_tokens + total_cache_read_tokens) * 100
        print(f"  Overall cache hit rate:   {hit_rate:.1f}%")
        print()

    # ── Spend breakdown (both modes) ──
    print("─" * 65)
    print("  SPEND BREAKDOWN")
    print("─" * 65)
    print()
    print(f"  Total spend:              {format_cost(grand_total_cost)}")
    print(f"  Total tokens:             {format_tokens(grand_total_tokens)}")
    print()
    uncached_input_tokens = total_input_tokens + total_cache_write_tokens
    uncached_input_cost = total_input_cost + total_cache_write_cost
    print("  By token type:")
    print(f"    {'':<24} {'Tokens':>12} {'Cost':>12} {'Share':>10}")
    print(f"    {'─'*24} {'─'*12} {'─'*12} {'─'*10}")
    for label, toks, cost in [
        ("Input tokens (uncached)", uncached_input_tokens, uncached_input_cost),
        ("Input tokens (cached)",   total_cache_read_tokens, total_cache_read_cost),
        ("Output tokens",           total_output_tokens, total_output_cost),
    ]:
        pct = format_pct(cost / grand_total_cost) if grand_total_cost > 0 else "0%"
        print(f"    {label:<24} {format_tokens(toks):>12} {format_cost(cost):>12} {pct:>10}")
    print()

    if tier_breakdown:
        print("  By model:")
        print(f"    {'Model':<10} {'Tokens':>14} {'Cost':>12} {'Share':>10}")
        print(f"    {'─'*10} {'─'*14} {'─'*12} {'─'*10}")
        for tier in ["opus", "sonnet", "haiku"]:
            if tier in tier_breakdown:
                tb = tier_breakdown[tier]
                tier_tokens = (tb["input_tokens"] + tb["cache_write_tokens"]
                               + tb["cache_read_tokens"] + tb["output_tokens"])
                tier_cost = (tb["input_cost"] + tb["cache_write_cost"]
                             + tb["cache_read_cost"] + tb["output_cost"])
                pct = format_pct(tier_cost / grand_total_cost) if grand_total_cost > 0 else "0%"
                print(f"    {tier:<10} {format_tokens(tier_tokens):>14} "
                      f"{format_cost(tier_cost):>12} {pct:>10}")
        print()

    # ── Cost impact (framing varies by mode) ──
    print("─" * 65)
    if is_sub:
        print(f"  COLD REBUILD IMPACT (share of {format_cost(grand_total_cost)} total spend)")
    else:
        print("  COST IMPACT")
    print("─" * 65)
    print()
    print(f"  Total cold rebuild:          {format_spend(total_wasted, grand_total_cost, args.mode)}")
    print(f"    ├─ Unavoidable (1st turn):  {format_spend(first_turn_wasted, grand_total_cost, args.mode)}")
    print(f"    └─ Preventable (mid-sess):  {format_spend(mid_session_wasted, grand_total_cost, args.mode)}")
    print()

    if mid_session_wasted > 0:
        if is_sub:
            pct = format_pct(mid_session_wasted / grand_total_cost) if grand_total_cost > 0 else "0%"
            print(f"  >>> {pct} of your token spend went to preventable "
                  f"mid-session cold rebuilds <<<")
        else:
            print(f"  >>> {format_cost(mid_session_wasted)} spent on preventable "
                  f"mid-session cold rebuilds <<<")
        print()

    # By model
    if by_model:
        print("─" * 65)
        print("  BY MODEL (cold turns)")
        print("─" * 65)
        print()
        print(f"  {'Model':<10} {'Cold turns':>12} {'Cold tokens':>14} {waste_col:>12}")
        print(f"  {'─'*10} {'─'*12} {'─'*14} {'─'*12}")
        for tier in ["opus", "sonnet", "haiku"]:
            if tier in by_model:
                m = by_model[tier]
                print(f"  {tier:<10} {m['cold_count']:>12,} "
                      f"{format_tokens(m['cold_tokens']):>14} "
                      f"{format_spend(m['wasted'], grand_total_cost, args.mode):>12}")
        print()

    # By day
    if by_day:
        print("─" * 65)
        print("  BY DAY")
        print("─" * 65)
        print()
        print(f"  {'Date':<12} {'Cold turns':>12} {'Cold tokens':>14} {waste_col:>12}")
        print(f"  {'─'*12} {'─'*12} {'─'*14} {'─'*12}")
        for day in sorted(by_day.keys()):
            d = by_day[day]
            print(f"  {day:<12} {d['cold_count']:>12,} "
                  f"{format_tokens(d['cold_tokens']):>14} "
                  f"{format_spend(d['wasted'], grand_total_cost, args.mode):>12}")
        print()

    # Worst sessions
    worst = sorted(by_session.items(), key=lambda x: x[1]["wasted"], reverse=True)[:5]
    if worst and worst[0][1]["wasted"] > 0:
        print("─" * 65)
        print("  WORST SESSIONS")
        print("─" * 65)
        print()
        print(f"  {'Session':<14} {'Model':<8} {'Cold':>6} {'Tokens':>12} {waste_col:>12}")
        print(f"  {'─'*14} {'─'*8} {'─'*6} {'─'*12} {'─'*12}")
        for sid, s in worst:
            if s["wasted"] > 0:
                print(f"  {sid[:14]:<14} {s['model']:<8} "
                      f"{s['cold_count']:>6} "
                      f"{format_tokens(s['cold_tokens']):>12} "
                      f"{format_spend(s['wasted'], grand_total_cost, args.mode):>12}")
        print()

    # Verbose: every cold turn
    if args.verbose and cold_turns:
        print("─" * 65)
        print("  ALL COLD TURNS")
        print("─" * 65)
        print()
        print(f"  {'Timestamp':<22} {'Session':<14} {'Model':<8} "
              f"{'Write':>10} {'Read':>10} {waste_col:>10} {'Type':<6}")
        print(f"  {'─'*22} {'─'*14} {'─'*8} "
              f"{'─'*10} {'─'*10} {'─'*10} {'─'*6}")
        for t in cold_turns:
            ts = t["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            typ = "1st" if t["is_first_turn"] else "idle"
            print(f"  {ts:<22} {t['session_id'][:14]:<14} {t['tier']:<8} "
                  f"{format_tokens(t['cache_creation']):>10} "
                  f"{format_tokens(t['cache_read']):>10} "
                  f"{format_spend(t['wasted'], grand_total_cost, args.mode):>10} {typ:<6}")
        print()

    print("=" * 65)
    print("  Tips:")
    print("  • First-turn cold starts are unavoidable — every session pays once.")
    print("  • Mid-session cold turns happen when idle gaps exceed the cache TTL.")
    print("  • Consider: /model sonnet → /compact → /model opus after idle gaps.")
    if is_sub:
        print("  • Subscription mode frames costs as % of total spend — your rate")
        print("    limit is finite even if your wallet isn't.")
    print("=" * 65)
    print()


if __name__ == "__main__":
    main()
