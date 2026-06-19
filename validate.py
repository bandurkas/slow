#!/usr/bin/env python3
"""Historical PURR funding-carry validation — REAL funding, long window, net of fees.

Ported from ~/Desktop/Fun/carry_validate.py so the `slow` repo is self-contained for
both backtest and forward paper. Pulls REAL hourly fundingHistory (paginated, 500/call)
over many months and answers honestly:
  - does positive funding PERSIST (train vs holdout halves, % hours positive)?
  - cumulative NET carry after a realistic delta-neutral round-trip cost, and its drawdown
    (stretches where funding goes negative => the short PAYS)?
  - net $/day at $400/$1000/$5000-equivalent notional, and break-even holding days.

Delta-neutral carry is a HOLD (long spot + short perp, HL unified margin). Realized funding
per hour = rate * notional (positive => short receives, negative => short pays); we use the
FULL mean incl. negative hours (no cherry-picking). Expected: PURR ~ NET +24.5% APR.
"""
import argparse
import datetime as dt
import statistics as st

from market import funding_history

HL_SPOT_HEDGEABLE = {"HYPE", "PURR", "AZTEC", "STABLE"}


def analyze(coin, hist, notional_set, rt_cost_pct):
    rates = [float(h["fundingRate"]) for h in hist]   # hourly
    times = [h["time"] for h in hist]
    n = len(rates)
    if n < 100:
        print(f"\n{coin}: only {n} hrs — skip")
        return
    span_d = (times[-1] - times[0]) / 86_400_000
    mean_hr = st.fmean(rates)
    pos = 100 * sum(1 for r in rates if r > 0) / n
    sd = st.pstdev(rates)
    apr_gross = mean_hr * 8760 * 100
    d0 = dt.datetime.utcfromtimestamp(times[0] / 1000).date()
    d1 = dt.datetime.utcfromtimestamp(times[-1] / 1000).date()

    # cumulative NET carry per $1 notional: accrue hourly rate, pay rt_cost once at entry
    rt = rt_cost_pct / 100.0
    cum = -rt
    peak = cum
    maxdd = 0.0
    neg_streak = cur = 0
    for r in rates:
        cum += r
        peak = max(peak, cum)
        maxdd = min(maxdd, cum - peak)
        if r < 0:
            cur += 1
            neg_streak = max(neg_streak, cur)
        else:
            cur = 0
    curve_end = cum  # net fraction of notional over the whole hold (after one round trip)

    # train/holdout persistence
    sp = int(n * 0.65)
    tr_mean = st.fmean(rates[:sp])
    ho_mean = st.fmean(rates[sp:])
    tr_pos = 100 * sum(1 for r in rates[:sp] if r > 0) / sp
    ho_pos = 100 * sum(1 for r in rates[sp:] if r > 0) / (n - sp)

    hedge = "HL-spot" if coin in HL_SPOT_HEDGEABLE else "cross-venue"
    print(f"\n===== {coin}  ({hedge} hedge) =====")
    print(f"  window {d0}..{d1} = {span_d:.0f}d / {n} hrs | funding {pos:.0f}% hrs positive | σ/hr {sd*100:.4f}%")
    print(f"  GROSS funding APR {apr_gross:+.1f}%  (mean {mean_hr*100:+.5f}%/hr)")
    print(f"  PERSISTENCE  train mean {tr_mean*8760*100:+.1f}% APR ({tr_pos:.0f}% pos) | "
          f"holdout {ho_mean*8760*100:+.1f}% APR ({ho_pos:.0f}% pos)")
    print(f"  NET over full {span_d:.0f}d hold (after {rt_cost_pct:.2f}% round-trip): "
          f"{curve_end*100:+.2f}% of notional => {curve_end/span_d*36500:+.1f}% NET APR")
    print(f"  cumulative-carry maxDD {maxdd*100:.2f}% of notional | longest negative-funding streak {neg_streak} hrs")
    be_days = (rt / mean_hr / 24) if mean_hr > 0 else float("inf")
    print(f"  break-even hold (cover round-trip): {be_days:.1f} days")
    print(f"  NET $/day after fees at notional/side:")
    for dep, notion in notional_set:
        gross_day = mean_hr * 24 * notion
        net_day = (curve_end / span_d) * notion   # amortized net incl the one round-trip
        print(f"     ${dep:>5} dep (~${notion:>5.0f}/side): gross ${gross_day:+.3f}/day | net ${net_day:+.3f}/day")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="PURR,HYPE,HEMI")
    ap.add_argument("--days", type=int, default=360)
    ap.add_argument("--rt-cost-pct", type=float, default=0.70,
                    help="round-trip cost %% of notional (PURR books ~0.6-0.76 taker)")
    args = ap.parse_args()
    # (deposit, notional/side): spot 1:1 hedge, modest leverage on perp via unified margin
    notional_set = [(400, 300.0), (1000, 750.0), (5000, 3750.0)]
    print(f"Real HL funding carry validation | rt-cost {args.rt_cost_pct}% | "
          f"notional/side = $300/$750/$3750 (~$400/$1000/$5000 dep)")
    for coin in [c.strip().upper() for c in args.coins.split(",")]:
        hist = funding_history(coin, args.days)
        if not hist:
            print(f"\n{coin}: no funding history (coin may not exist)")
            continue
        analyze(coin, hist, notional_set, args.rt_cost_pct)
    print("\nVERDICT test: NET APR > 0 with persistence holding in BOTH train & holdout, "
          "and cumulative maxDD tolerable. Otherwise carry is too thin/unstable net of fees.")


if __name__ == "__main__":
    main()
