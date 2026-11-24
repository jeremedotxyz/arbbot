#!/usr/bin/env python3
"""
Paper triangular-arbitrage scanner — Kraken, no keys, no real money.

What it does
------------
Polls Kraken's public Ticker, builds every 3-asset triangle it can trade
(e.g. USD -> BTC -> ETH -> USD), and flags the ones that would still profit
AFTER Kraken's 0.26% taker fee on each of the 3 legs (0.78% total drag).

Paper mode: a virtual balance (default $1,000). When a triangle clears the
profit threshold, the scanner "executes" a paper trade of fixed notional
(default $100) and logs it. Nothing is ever sent to Kraken — this only reads
public prices.

Honest limitations (read before believing the numbers):
- Top-of-book prices only. A real fill eats into the book, so slippage on a
  $100 notional is small but on a $10k notional it is not.
- It assumes all 3 legs fill instantly at the quoted prices. In reality the
  gap can close between leg 1 and leg 3, and you would be left holding the
  middle asset. That is the main risk of triangular arb.
- Kraken's fee used here is the default taker rate (0.26%). Volume discounts
  would lower it; stablecoin pairs have their own fee quirks.
- If it prints nothing for minutes, that is normal: on a liquid exchange,
  after-fee triangular gaps are rare and tiny. Zeroes are the scanner
  working, not broken.

Usage
-----
  python3 triangular_arb_scanner.py --duration 300 --interval 3
  python3 triangular_arb_scanner.py --once            # single snapshot
  python3 triangular_arb_scanner.py --threshold 0.02  # flag anything over 0.02% net

Logs paper trades to paper_trades.jsonl next to this script.
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

KRAKEN = "https://api.kraken.com/0/public"
TAKER_FEE = 0.0026          # Kraken default taker fee, per leg
FEE_FACTOR = 1.0 - TAKER_FEE

# Liquid assets to build triangles from. USD is the home base.
WATCH_ASSETS = {
    "XXBT", "XETH", "SOL", "USDT", "ZUSD", "XRP", "ADA", "DOT",
    "LTC", "DOGE", "LINK", "AVAX", "ATOM", "UNI", "XLM", "USDC",
}
# Human-readable names for the log lines.
DISPLAY = {
    "XXBT": "BTC", "XETH": "ETH", "ZUSD": "USD", "USDT": "USDT",
    "USDC": "USDC", "SOL": "SOL", "XRP": "XRP", "ADA": "ADA",
    "DOT": "DOT", "LTC": "LTC", "DOGE": "DOGE", "LINK": "LINK",
    "AVAX": "AVAX", "ATOM": "ATOM", "UNI": "UNI", "XLM": "XLM",
}


def disp(asset: str) -> str:
    return DISPLAY.get(asset, asset)


def fetch_pairs(session: requests.Session) -> dict:
    """All online Kraken pairs, restricted to watchlist assets."""
    r = session.get(f"{KRAKEN}/AssetPairs", timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken AssetPairs error: {data['error']}")
    pairs = {}
    for key, p in data["result"].items():
        base, quote = p["base"], p["quote"]
        if base in WATCH_ASSETS and quote in WATCH_ASSETS and base != quote:
            pairs[key] = (base, quote)
    return pairs


def build_triangles(pairs: dict) -> list:
    """Find every 3-cycle A -> B -> C -> A tradable with the given pairs.

    For a pair (base, quote): base -> quote sells at the bid,
    quote -> base buys at the ask. An edge exists in both directions.
    """
    edges = defaultdict(set)  # asset -> set of assets reachable in one leg
    for base, quote in pairs.values():
        edges[base].add(quote)
        edges[quote].add(base)
    triangles = []
    for a in edges:
        for b in edges[a]:
            if b == a:
                continue
            for c in edges[b]:
                if c in (a, b):
                    continue
                if a in edges[c]:
                    triangles.append((a, b, c))
    # Each cycle appears 3x (one per starting asset); keep one rotation.
    seen, unique = set(), []
    for tri in triangles:
        rotations = {tuple(tri[i:] + tri[:i]) for i in range(3)}
        key = min(rotations)
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def leg_rate(base: str, quote: str, from_asset: str, tick: dict) -> float:
    """Rate to convert 1 unit of from_asset into the other asset of the pair."""
    ask = float(tick["a"][0])
    bid = float(tick["b"][0])
    if from_asset == base:
        return bid          # sell base for quote
    return 1.0 / ask        # buy base with quote


def scan_once(session, pairs, triangles, pair_of_edge):
    """One poll: fetch all tickers, price every triangle, return results."""
    ticks = {}
    pair_keys = list(pairs)
    # Kraken's Ticker takes many pairs at once, but the egress proxy chokes
    # on very long URLs — fetch in small batches and merge.
    for i in range(0, len(pair_keys), 10):
        batch = pair_keys[i:i + 10]
        r = session.get(f"{KRAKEN}/Ticker", params={"pair": ",".join(batch)},
                        timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken Ticker error: {data['error']}")
        ticks.update(data["result"])
    results = []
    for tri in triangles:
        amount = 1.0
        legs = []
        ok = True
        for i in range(3):
            src, dst = tri[i], tri[(i + 1) % 3]
            pair_key = pair_of_edge[(src, dst)]
            tick = ticks.get(pair_key)
            if not tick:
                ok = False
                break
            base, quote = pairs[pair_key]
            rate = leg_rate(base, quote, src, tick) * FEE_FACTOR
            legs.append(f"{disp(src)}->{disp(dst)} @{rate:.6g}")
            amount *= rate
        if not ok:
            continue
        profit_pct = (amount - 1.0) * 100
        results.append({"triangle": tri, "profit_pct": profit_pct, "legs": legs})
    results.sort(key=lambda x: x["profit_pct"], reverse=True)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="Paper triangular-arb scanner (Kraken)")
    ap.add_argument("--duration", type=int, default=300, help="seconds to run (default 300)")
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between polls (default 3)")
    ap.add_argument("--threshold", type=float, default=0.05, help="flag net profit above this %% (default 0.05)")
    ap.add_argument("--notional", type=float, default=100.0, help="paper trade size in USD (default 100)")
    ap.add_argument("--balance", type=float, default=1000.0, help="starting virtual balance in USD (default 1000)")
    ap.add_argument("--once", action="store_true", help="single snapshot, then exit")
    ap.add_argument("--log", default="paper_trades.jsonl", help="paper trade log file")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": "paper-tri-arb-scanner/1.0"})

    print("Fetching Kraken pairs…", flush=True)
    pairs = fetch_pairs(session)
    pair_of_edge = {}
    for key, (base, quote) in pairs.items():
        pair_of_edge[(base, quote)] = key
        pair_of_edge[(quote, base)] = key
    triangles = build_triangles(pairs)
    print(f"{len(pairs)} pairs, {len(triangles)} triangles, "
          f"fee {TAKER_FEE*100:.2f}%/leg, threshold {args.threshold:.3f}% net",
          flush=True)

    balance = args.balance
    trades = 0
    cooldown = defaultdict(float)   # triangle -> next allowed paper-trade time
    best_seen = None
    start = time.time()
    poll = 0

    log_path = args.log
    if "/" not in log_path:
        import os
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), log_path)

    while True:
        poll += 1
        now = time.time()
        try:
            results = scan_once(session, pairs, triangles, pair_of_edge)
        except Exception as e:  # noqa: BLE001 - keep scanning through API hiccups
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] poll failed: {e}",
                  flush=True)
            if args.once:
                return 1
            time.sleep(args.interval)
            continue

        if results:
            top = results[0]
            if best_seen is None or top["profit_pct"] > best_seen["profit_pct"]:
                best_seen = top
            for res in results:
                if res["profit_pct"] <= args.threshold:
                    break
                tri = res["triangle"]
                if now < cooldown[tri]:
                    continue
                cooldown[tri] = now + 60  # one paper trade per triangle per minute
                profit_usd = args.notional * res["profit_pct"] / 100.0
                balance += profit_usd
                trades += 1
                tri_str = " -> ".join(disp(a) for a in tri) + f" -> {disp(tri[0])}"
                line = (f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] PAPER TRADE "
                        f"{tri_str}  net +{res['profit_pct']:.3f}%  "
                        f"+${profit_usd:.2f} on ${args.notional:.0f}  "
                        f"balance ${balance:.2f}")
                print(line, flush=True)
                with open(log_path, "a") as f:
                    f.write(json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "triangle": [disp(a) for a in tri],
                        "profit_pct": round(res["profit_pct"], 4),
                        "profit_usd": round(profit_usd, 4),
                        "notional_usd": args.notional,
                        "balance_usd": round(balance, 2),
                        "legs": res["legs"],
                    }) + "\n")
        if args.once:
            break
        if poll % 20 == 0 and best_seen is not None:
            tri_str = " -> ".join(disp(a) for a in best_seen["triangle"])
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] heartbeat: "
                  f"{poll} polls, best triangle so far {tri_str} "
                  f"at {best_seen['profit_pct']:+.3f}% net, "
                  f"{trades} paper trades, balance ${balance:.2f}", flush=True)
        if time.time() - start >= args.duration:
            break
        time.sleep(args.interval)

    print(f"\nDone: {poll} polls in {time.time() - start:.0f}s, "
          f"{trades} paper trades, virtual balance ${balance:.2f} "
          f"(started ${args.balance:.2f}, P&L ${balance - args.balance:+.2f})", flush=True)
    if best_seen is not None:
        tri_str = " -> ".join(disp(a) for a in best_seen["triangle"])
        print(f"Best triangle seen: {tri_str} at {best_seen['profit_pct']:+.3f}% net "
              f"(after fees)", flush=True)
    else:
        print("No triangle priced this run.", flush=True)
    print(f"Paper trades logged to {log_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
