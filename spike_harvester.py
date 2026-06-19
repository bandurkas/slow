#!/usr/bin/env python3
"""Bybit funding-SPIKE harvester scanner (read-only) — the active-management lever.

The persistence radar finds buy-and-hold cores (PURR). This finds the opposite: short-lived
funding SPIKES worth a tactical 1-3 day harvest (short perp + long spot, collect funding,
exit before reversion). Viable on Bybit because spreads are tight, so a short hold clears the
round-trip cost — e.g. +100% APR funding over 3d ≈ +0.82% vs ~0.3% round-trip = net positive.

For each hedgeable Bybit coin with current funding above a threshold it reports: current APR,
recent-prints mean APR + positivity (is the spike SUSTAINED over the last K prints or a single
blip?), round-trip cost from live spreads, break-even days, and net edge over the harvest
window. Flags HARVEST / WATCH / RISKY (recent negative prints = reversal/squeeze risk).

⚠️ Run from a venue where Bybit is reachable (VPS3). Read-only, no keys.
"""
import argparse
import statistics as st

# reuse the hardened Bybit read helpers from the radar (no duplication)
from radar import bybit_hedgeable_and_funding, bybit_funding_hist, bybit_spread


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-apr", type=float, default=25.0, help="current funding APR%% floor to consider")
    ap.add_argument("--harvest-days", type=float, default=3.0, help="planned tactical hold length")
    ap.add_argument("--max-rt", type=float, default=1.0, help="max round-trip cost%% (spot+perp spread) to allow")
    ap.add_argument("--recent-k", type=int, default=6, help="recent funding prints to judge sustain")
    args = ap.parse_args()

    by = bybit_hedgeable_and_funding()
    if not by:
        print("[harvester] Bybit returned no data (run from VPS3 — geo).")
        return

    # prefilter on current funding APR (bulk, cheap)
    cand = []
    for coin, meta in by.items():
        cur_apr = meta["funding_i"] * (8760.0 / meta["interval_h"]) * 100.0
        if cur_apr >= args.min_apr:
            cand.append((coin, meta, cur_apr))
    cand.sort(key=lambda x: -x[2])
    print(f"[harvester] {len(cand)} Bybit coins with current funding >= {args.min_apr}% APR "
          f"| harvest window {args.harvest_days:.0f}d\n")
    if not cand:
        print("No live spikes above threshold right now. (Quiet funding regime — check back.)")
        return

    rows = []
    for coin, meta, cur_apr in cand:
        recent = bybit_funding_hist(meta["symbol"], days=max(4, int(args.recent_k * meta["interval_h"] / 24) + 2))
        recent = recent[-args.recent_k:] if recent else []
        if not recent:
            continue
        per_year = 8760.0 / meta["interval_h"]
        recent_apr = st.fmean(recent) * per_year * 100
        pos = 100.0 * sum(1 for r in recent if r > 0) / len(recent)
        had_neg = any(r < 0 for r in recent)
        perp_sp = bybit_spread("linear", meta["symbol"])
        spot_sp = bybit_spread("spot", coin + "USDT")
        if perp_sp is None or spot_sp is None:
            continue
        rt = perp_sp + spot_sp
        daily = cur_apr / 365.0
        breakeven_d = rt / daily if daily > 0 else 999
        # net over the harvest window using the CONSERVATIVE recent mean (not the peak print)
        net_window = recent_apr / 365.0 * args.harvest_days - rt
        rows.append(dict(coin=coin, cur_apr=cur_apr, recent_apr=recent_apr, pos=pos,
                         had_neg=had_neg, rt=rt, be=breakeven_d, net=net_window))

    rows.sort(key=lambda r: -r["net"])
    print(f"{'#':>2} {'COIN':9} {'curAPR':>8} {'recentAPR':>9} {'pos':>5} {'rt%':>5} "
          f"{'be_d':>5} {'net'+str(int(args.harvest_days))+'d%':>7} {'flag'}")
    for i, r in enumerate(rows, 1):
        harvest = (r["cur_apr"] >= args.min_apr and r["pos"] >= 80 and not r["had_neg"]
                   and r["be"] <= args.harvest_days and r["rt"] <= args.max_rt and r["net"] > 0)
        flag = "HARVEST" if harvest else ("RISKY(neg)" if r["had_neg"] else "WATCH")
        print(f"{i:>2} {r['coin']:9} {r['cur_apr']:+8.0f} {r['recent_apr']:+9.0f} {r['pos']:4.0f}% "
              f"{r['rt']:5.2f} {r['be']:5.1f} {r['net']:+7.2f} {flag}")
    print(f"\nnet{int(args.harvest_days)}d%% uses the CONSERVATIVE recent-mean funding (not the peak print).")
    print("HARVEST = spike sustained (pos>=80%, no recent negatives), break-even < hold, tight spread, net>0.")
    print("RISKY(neg) = recent negative print => reversal/squeeze risk, the crowded-short trap. Skip or size tiny.")


if __name__ == "__main__":
    main()
