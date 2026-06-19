#!/usr/bin/env python3
"""Cross-venue funding-carry radar (read-only, no keys) — foundation of the rotation desk.

Scans Hyperliquid + Bybit for SPOT-HEDGEABLE coins (a coin with BOTH a perp and a USDC/USDT
spot market on that venue, so long-spot + short-perp is delta-neutral on one venue), pulls
real funding history, and scores PERSISTENCE — because the year-long validation proved the
edge is *persistent* funding, not peak magnitude. A +150% APR alt that flips negative
tomorrow is a trap, not a carry.

Per (venue, coin) it reports: net APR (after a spread-based round-trip proxy), % intervals
positive, train/holdout halves (persistence), longest negative-funding streak, and a robust
score = min(train, holdout) APR (rewards funding that holds up out-of-sample). Ranked, with
PURR shown as the benchmark.

Public endpoints only:
  HL    POST /info  (metaAndAssetCtxs, spotMeta, fundingHistory, l2Book)
  Bybit GET  /v5/market/{instruments-info,tickers,funding/history,orderbook}
"""
import argparse
import statistics as st
import time

import requests

HL_INFO = "https://api.hyperliquid.xyz/info"
BYBIT = "https://api.bybit.com"


# ───────────────────────── Hyperliquid ─────────────────────────
def _hl(payload, retries=4):
    for i in range(retries):
        try:
            r = requests.post(HL_INFO, json=payload, timeout=20)
            if r.status_code == 200:
                return r.json()
            time.sleep(1.0 * (2 ** i) if r.status_code == 429 else 0.8)
        except Exception:
            time.sleep(0.8)
    return None


def hl_hedgeable_and_funding():
    """Return {COIN: {'spot': pair_name, 'funding_h': hourly_rate}} for hedgeable HL coins."""
    sm = _hl({"type": "spotMeta"})
    data = _hl({"type": "metaAndAssetCtxs"})
    if not sm or not data:
        return {}
    tok = {t["index"]: t["name"].upper() for t in sm["tokens"]}
    spot = {}
    for p in sm["universe"]:
        a, b = p["tokens"]
        names = {tok.get(a), tok.get(b)}
        if "USDC" in names:
            base = (names - {"USDC"}).pop()
            if base:
                spot[base] = p["name"]
    meta, ctxs = data
    out = {}
    for u, c in zip(meta["universe"], ctxs):
        nm = u["name"].upper()
        if nm in spot and c.get("funding") is not None:
            out[nm] = {"spot": spot[nm], "funding_h": float(c["funding"])}
    return out


def hl_funding_hist(coin, days):
    start = int(time.time() * 1000) - days * 86_400_000
    out, seen = [], set()
    while True:
        chunk = _hl({"type": "fundingHistory", "coin": coin, "startTime": start})
        if not chunk:
            break
        new = [h for h in chunk if h["time"] not in seen]
        if not new:
            break
        for h in new:
            seen.add(h["time"])
        out.extend(new)
        last = max(h["time"] for h in chunk)
        if len(chunk) < 500 or last <= start:
            break
        start = last + 1
        time.sleep(0.04)
    out.sort(key=lambda h: h["time"])
    return [float(h["fundingRate"]) for h in out]


def hl_spread(name):
    bk = _hl({"type": "l2Book", "coin": name})
    try:
        b, a = bk["levels"][0], bk["levels"][1]
        bid, ask = float(b[0]["px"]), float(a[0]["px"])
        return (ask - bid) / ((ask + bid) / 2) * 100
    except Exception:
        return None


# ───────────────────────── Bybit ─────────────────────────
def _by(path, params, retries=4):
    for i in range(retries):
        try:
            r = requests.get(BYBIT + path, params=params, timeout=20)
            if r.status_code == 200:
                j = r.json()
                if j.get("retCode") == 0:
                    return j["result"]
            time.sleep(0.8 * (2 ** i))
        except Exception:
            time.sleep(0.8)
    return None


def bybit_hedgeable_and_funding():
    """Return {COIN: {'symbol','interval_h','funding_i'}} for coins with linear perp AND spot (USDT)."""
    # spot bases
    spot_bases = set()
    res = _by("/v5/market/instruments-info", {"category": "spot"})
    if res:
        for it in res.get("list", []):
            if it.get("quoteCoin") == "USDT":
                spot_bases.add(it.get("baseCoin", "").upper())
    # linear perps + funding interval
    perps = {}
    res = _by("/v5/market/instruments-info", {"category": "linear"})
    if res:
        for it in res.get("list", []):
            if it.get("quoteCoin") == "USDT" and it.get("contractType") == "LinearPerpetual":
                base = it.get("baseCoin", "").upper()
                fi = it.get("fundingInterval")  # minutes
                perps[base] = {"symbol": it["symbol"], "interval_h": (fi / 60.0) if fi else 8.0}
    # current funding from tickers
    out = {}
    res = _by("/v5/market/tickers", {"category": "linear"})
    if res:
        cur = {t["symbol"]: t.get("fundingRate") for t in res.get("list", [])}
        for base, info in perps.items():
            if base in spot_bases and cur.get(info["symbol"]) not in (None, ""):
                info = dict(info)
                info["funding_i"] = float(cur[info["symbol"]])
                out[base] = info
    return out


def bybit_funding_hist(symbol, days):
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    out, seen = [], set()
    cursor = end
    while True:
        res = _by("/v5/market/funding/history",
                  {"category": "linear", "symbol": symbol, "limit": 200,
                   "startTime": start, "endTime": cursor})
        if not res:
            break
        lst = res.get("list", [])
        if not lst:
            break
        for x in lst:
            ts = int(x["fundingRateTimestamp"])
            if ts not in seen:
                seen.add(ts)
                out.append((ts, float(x["fundingRate"])))
        oldest = min(int(x["fundingRateTimestamp"]) for x in lst)
        if len(lst) < 200 or oldest <= start:
            break
        cursor = oldest - 1
        time.sleep(0.04)
    out.sort()
    return [r for _, r in out]


def bybit_spread(category, symbol):
    res = _by("/v5/market/orderbook", {"category": category, "symbol": symbol, "limit": 1})
    try:
        bid = float(res["b"][0][0]); ask = float(res["a"][0][0])
        return (ask - bid) / ((ask + bid) / 2) * 100
    except Exception:
        return None


# ───────────────────────── metrics ─────────────────────────
def metrics(rates, interval_h, rt_cost_pct):
    n = len(rates)
    if n < 40:
        return None
    per_year = 8760.0 / interval_h
    mean = st.fmean(rates)
    pos = 100.0 * sum(1 for r in rates if r > 0) / n
    sp = int(n * 0.65)
    tr = st.fmean(rates[:sp]) * per_year * 100
    ho = st.fmean(rates[sp:]) * per_year * 100
    # net over full hold after one round-trip
    rt = rt_cost_pct / 100.0
    cum = -rt
    streak = cur = 0
    for r in rates:
        cum += r
        if r < 0:
            cur += 1; streak = max(streak, cur)
        else:
            cur = 0
    span_years = n / per_year
    net_apr = cum / span_years * 100 if span_years > 0 else 0.0
    return {
        "n": n, "interval_h": interval_h, "gross_apr": mean * per_year * 100,
        "net_apr": net_apr, "pos": pos, "train_apr": tr, "holdout_apr": ho,
        "robust_apr": min(tr, ho), "neg_streak_h": streak * interval_h,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=150)
    ap.add_argument("--top", type=int, default=12, help="coins per venue to deep-scan (by current funding)")
    args = ap.parse_args()

    rows = []

    print(f"[radar] scanning HL + Bybit | history {args.days}d | deep-scan top {args.top}/venue by current funding\n")

    # HL
    hl = hl_hedgeable_and_funding()
    hl_rank = sorted(hl.items(), key=lambda kv: -kv[1]["funding_h"])
    hl_pick = dict(hl_rank[:args.top])
    hl_pick["PURR"] = hl.get("PURR", hl_pick.get("PURR"))  # always include benchmark
    for coin, meta in hl_pick.items():
        if not meta:
            continue
        rates = hl_funding_hist(coin, args.days)
        perp_sp = hl_spread(coin) or 0.3
        spot_sp = hl_spread(meta["spot"]) or 0.3
        m = metrics(rates, 1.0, perp_sp + spot_sp)
        if m:
            m.update(venue="HL", coin=coin, spread=perp_sp + spot_sp)
            rows.append(m)

    # Bybit
    by = bybit_hedgeable_and_funding()
    if by:
        by_rank = sorted(by.items(), key=lambda kv: -kv[1]["funding_i"])
        for coin, meta in dict(by_rank[:args.top]).items():
            rates = bybit_funding_hist(meta["symbol"], args.days)
            perp_sp = bybit_spread("linear", meta["symbol"]) or 0.1
            spot_sp = bybit_spread("spot", coin + "USDT") or 0.1
            m = metrics(rates, meta["interval_h"], perp_sp + spot_sp)
            if m:
                m.update(venue="Bybit", coin=coin, spread=perp_sp + spot_sp)
                rows.append(m)
    else:
        print("[radar] WARNING: Bybit returned no data (region block / API down)\n")

    # rank by robust (persistence-weighted) net edge
    rows.sort(key=lambda r: -r["robust_apr"])
    print(f"{'#':>2} {'VENUE':5} {'COIN':8} {'net%APR':>8} {'train':>7} {'holdout':>8} "
          f"{'robust':>7} {'pos%':>5} {'negStrk':>8} {'rt%':>5} {'flag'}")
    for i, r in enumerate(rows, 1):
        persistent = r["pos"] >= 90 and r["train_apr"] > 0 and r["holdout_apr"] > 0
        fading = r["holdout_apr"] < 0.5 * r["train_apr"]
        flag = "PERSIST" if persistent and not fading else ("fading" if fading else "weak")
        mark = " <-- PURR" if r["coin"] == "PURR" else ""
        print(f"{i:>2} {r['venue']:5} {r['coin']:8} {r['net_apr']:+8.1f} {r['train_apr']:+7.1f} "
              f"{r['holdout_apr']:+8.1f} {r['robust_apr']:+7.1f} {r['pos']:5.0f} "
              f"{r['neg_streak_h']:7.0f}h {r['spread']:5.2f} {flag}{mark}")
    print("\nrobust = min(train,holdout) APR — forward-honest. PERSIST = pos>=90% & both halves positive & not fading.")
    print("Carry candidate = high robust + PERSIST + tight rt spread. High net but 'fading'/'weak' = likely a trap.")


if __name__ == "__main__":
    main()
