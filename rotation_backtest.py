#!/usr/bin/env python3
"""Backtest the ACTIVE multi-venue carry-rotation strategy BEFORE building the desk.

Honest question: if we actively rotated capital into the best-funding hedgeable coin across
HL + Bybit, paying a real round-trip cost on every switch, what NET APR would we have earned?
Is there even 30%+ there, or does churn + reversion eat it?

Method (no fooling ourselves):
  - Universe chosen by LIQUIDITY (Bybit 24h turnover) + all HL hedgeable — NOT by funding
    outcome (avoids selecting on the answer). Survivorship caveat: delisted coins excluded.
  - Align everything to a common 8h funding grid (HL hourly summed into 8h buckets; Bybit is
    already 8h). Payoff in bucket t = the coin's realized funding that bucket (short earns
    positive, PAYS negative — negatives included, no cherry-picking).
  - Signal = trailing mean funding up to t-1 (NO lookahead). Hold the best; rotate only if the
    best alternative beats the held coin's trailing signal by > rotation_gate, and pay the
    per-venue round-trip cost on the switch.
  - Compare: baseline (hold PURR) vs ACTIVE rotation vs PERFECT-FORESIGHT ceiling (rotate into
    the actual best NEXT bucket, still paying rt) — the upper bound on capturable edge.
  - train/holdout split + gate/window sweep so the rule isn't curve-fit.

Run from VPS3 (Bybit geo). Read-only.
"""
import argparse
import statistics as st
import time

from radar import hl_hedgeable_and_funding, _by

BUCKET_MS = 8 * 3600 * 1000
RT = {"HL": 0.50, "Bybit": 0.30}   # round-trip cost % of notional per venue (spread+fees)


def bybit_universe_by_liquidity(top):
    """Top-N Bybit linear perps by 24h turnover that ALSO have a USDT spot (hedgeable)."""
    spot = set()
    res = _by("/v5/market/instruments-info", {"category": "spot"})
    for it in (res or {}).get("list", []):
        if it.get("quoteCoin") == "USDT":
            spot.add(it["baseCoin"].upper())
    res = _by("/v5/market/tickers", {"category": "linear"})
    rows = []
    for t in (res or {}).get("list", []):
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4].upper()
        if base in spot and t.get("turnover24h"):
            rows.append((base, sym, float(t["turnover24h"])))
    rows.sort(key=lambda r: -r[2])
    return [(b, s) for b, s, _ in rows[:top]]


def to_grid(series, is_hourly):
    """series: list[(ts_ms, rate)] -> dict bucket_ts -> rate_per_8h."""
    g = {}
    for ts, r in series:
        b = (ts // BUCKET_MS) * BUCKET_MS
        if is_hourly:
            g[b] = g.get(b, 0.0) + r        # sum hourly into the 8h bucket
        else:
            g[b] = r
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--bybit-top", type=int, default=35)
    ap.add_argument("--window", type=int, default=9, help="trailing buckets for signal (9=72h)")
    ap.add_argument("--gate-apr", type=float, default=40.0,
                    help="extra trailing APR the best alt must beat held by to rotate")
    args = ap.parse_args()

    # We need (ts,rate). radar's *_funding_hist return rates only; re-pull with timestamps here.
    import requests
    HL_INFO = "https://api.hyperliquid.xyz/info"

    def hl_hist_ts(coin, days):
        start = int(time.time() * 1000) - days * 86400000
        out, seen = [], set()
        while True:
            ch = requests.post(HL_INFO, json={"type": "fundingHistory", "coin": coin, "startTime": start}, timeout=20).json()
            if not ch:
                break
            new = [h for h in ch if h["time"] not in seen]
            if not new:
                break
            for h in new:
                seen.add(h["time"])
            out += [(h["time"], float(h["fundingRate"])) for h in new]
            last = max(h["time"] for h in ch)
            if len(ch) < 500 or last <= start:
                break
            start = last + 1
            time.sleep(0.03)
        return out

    def by_hist_ts(sym, days):
        end = int(time.time() * 1000); start = end - days * 86400000; cur = end
        out, seen = [], set()
        while True:
            res = _by("/v5/market/funding/history", {"category": "linear", "symbol": sym, "limit": 200, "startTime": start, "endTime": cur})
            lst = (res or {}).get("list", [])
            if not lst:
                break
            for x in lst:
                ts = int(x["fundingRateTimestamp"])
                if ts not in seen:
                    seen.add(ts); out.append((ts, float(x["fundingRate"])))
            oldest = min(int(x["fundingRateTimestamp"]) for x in lst)
            if len(lst) < 200 or oldest <= start:
                break
            cur = oldest - 1; time.sleep(0.03)
        return out

    print(f"[bt] building universe | {args.days}d | Bybit top {args.bybit_top} by turnover", flush=True)
    cols = {}
    hl = hl_hedgeable_and_funding()
    for coin in hl:
        g = to_grid(hl_hist_ts(coin, args.days), is_hourly=True)
        if len(g) > args.days * 3 * 0.5:
            cols[("HL", coin)] = g
    print(f"[bt] HL coins kept: {len(cols)}", flush=True)
    for base, sym in bybit_universe_by_liquidity(args.bybit_top):
        g = to_grid(by_hist_ts(sym, args.days), is_hourly=False)
        if len(g) > args.days * 3 * 0.5:
            cols[("Bybit", base)] = g
    print(f"[bt] total coins in universe: {len(cols)}", flush=True)

    # common 8h grid
    end = (int(time.time() * 1000) // BUCKET_MS) * BUCKET_MS
    start = end - args.days * 86400000
    grid = list(range(start, end, BUCKET_MS))
    keys = list(cols.keys())
    per_year = 3 * 365 * 100  # 8h buckets -> APR%

    def trailing(ci, t_idx):
        vals = [cols[keys[ci]].get(grid[j]) for j in range(max(0, t_idx - args.window), t_idx)]
        vals = [v for v in vals if v is not None]
        return st.fmean(vals) if vals else None

    def simulate(gate_per_bucket, perfect=False, lo=0, hi=None):
        hi = hi or len(grid)
        held = None; pnl = 0.0; switches = 0
        for t in range(max(args.window, lo), hi):
            avail = [ci for ci in range(len(keys)) if cols[keys[ci]].get(grid[t]) is not None]
            if not avail:
                continue
            if perfect:
                best = max(avail, key=lambda ci: cols[keys[ci]].get(grid[t]))
            else:
                scored = [(ci, trailing(ci, t)) for ci in avail]
                scored = [(ci, s) for ci, s in scored if s is not None]
                if not scored:
                    continue
                best = max(scored, key=lambda x: x[1])[0]
            if held is None:
                held = best; pnl -= RT[keys[best][0]] / 100; switches += 1
            elif best != held:
                if perfect:
                    do = True
                else:
                    sb = dict(scored)
                    do = (sb.get(best, -9) - sb.get(held, -9)) > gate_per_bucket
                if do:
                    pnl -= (RT[keys[held][0]] + RT[keys[best][0]]) / 2 / 100
                    held = best; switches += 1
            r = cols[keys[held]].get(grid[t])
            if r is not None:
                pnl += r
        years = (hi - max(args.window, lo)) / (3 * 365)
        return pnl / years * 100 if years > 0 else 0.0, switches

    gate = args.gate_apr / per_year   # APR gate -> per-bucket rate
    purr_key = next((i for i, k in enumerate(keys) if k == ("HL", "PURR")), None)

    def baseline(lo, hi):
        if purr_key is None:
            return None
        pnl = -RT["HL"] / 100
        for t in range(lo, hi):
            r = cols[keys[purr_key]].get(grid[t])
            if r is not None:
                pnl += r
        years = (hi - lo) / (3 * 365)
        return pnl / years * 100

    n = len(grid); sp = int(n * 0.65)
    print(f"\n=== ROTATION BACKTEST | {len(keys)} coins | {n} 8h-buckets (~{n/3:.0f}d) | gate {args.gate_apr:.0f}% APR, window {args.window*8}h ===")
    print(f"{'metric':28} {'FULL':>10} {'train':>10} {'holdout':>10}")
    for name, fn in [
        ("baseline hold-PURR APR%", lambda lo, hi: (baseline(lo, hi), 0)),
        ("ACTIVE rotation APR%", lambda lo, hi: simulate(gate, False, lo, hi)),
        ("perfect-foresight APR%", lambda lo, hi: simulate(0, True, lo, hi)),
    ]:
        full = fn(0, n); tr = fn(0, sp); ho = fn(sp, n)
        def fmt(x):
            v, sw = x if isinstance(x, tuple) else (x, None)
            return f"{v:+7.1f}" + (f"({sw})" if sw else "      ")
        print(f"{name:28} {fmt(full):>10} {fmt(tr):>10} {fmt(ho):>10}")
    print("\n(switch counts in parens) | gate sweep: re-run with --gate-apr 20/30/60; window with --window 3/9/21")
    print("If ACTIVE holdout APR ≈ baseline or churns below it → rotation has no edge worth building.")


if __name__ == "__main__":
    main()
