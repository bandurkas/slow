#!/usr/bin/env python3
"""Phase 0 paper simulator — full FSM on LIVE PURR funding+spreads, no keys, no orders.

Unlike carry_logger (which just records data), this runs the real decision FSM:
  FLAT -> (should_enter) -> CARRYING -> (should_exit) -> FLAT
accruing REAL funding (rate * notional * dt) while CARRYING, charging one maker round-trip
cost per position, and logging net P&L to CSV. Goal: confirm the strategy LOGIC (entry/
exit/hysteresis) and the ~+24.5% APR model survive live spreads before we write the
key-holding executor (Phase 1).

Public read-only API only. Restart-safe via state.json. Run:
  python3 paper_sim.py                # loop forever (deploy in screen/systemd)
  python3 paper_sim.py --once         # single tick (smoke test)
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import config
from market import current_funding, book_mid_spread, trailing_funding_apr, spot_pair_name
from strategy import should_enter, should_exit

STATE_FILE = "state.json"
LOG_FILE = "paper_log.csv"

FRESH_STATE = {
    "coin": config.COIN,
    "fsm": "FLAT",
    "entry_ts": None,
    "accrued_funding_usd": 0.0,    # within current position
    "rt_cost_booked_usd": 0.0,     # round-trip cost charged this position
    "realized_pnl_usd": 0.0,       # cumulative net over all closed positions
    "hours_below_exit": 0.0,       # hysteresis: consecutive hrs trailing APR < EXIT_APR
    "entry_funding_apr": None,
    "last_tick_ts": None,
}


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                s = dict(FRESH_STATE)
                s.update(json.load(f))
                return s
        except (json.JSONDecodeError, OSError):
            pass
    return dict(FRESH_STATE)


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, STATE_FILE)


def ensure_log_header():
    if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0:
        with open(LOG_FILE, "a", newline="") as f:
            csv.writer(f).writerow([
                "ts_iso", "fsm", "funding_apr", "trailing_apr", "spread_pct",
                "accrued_usd", "rt_cost_usd", "realized_pnl_usd",
                "days_in_pos", "net_apr_so_far", "note",
            ])


def widest_spread(coin, spot_name):
    """Max of perp & spot top-of-book spread % (both legs are paid)."""
    _, perp_sp = book_mid_spread(coin)
    spot_sp = None
    if spot_name:
        _, spot_sp = book_mid_spread(spot_name)
    vals = [s for s in (perp_sp, spot_sp) if s is not None]
    return max(vals) if vals else None


def tick(state, spot_name):
    now = time.time()
    iso = datetime.now(timezone.utc).isoformat()
    fr = current_funding(config.COIN)                 # hourly rate, instantaneous
    trailing_apr = trailing_funding_apr(config.COIN, config.FUNDING_LOOKBACK_H)
    spread = widest_spread(config.COIN, spot_name)
    fr_apr = fr * 8760 * 100 if fr is not None else None

    last = state["last_tick_ts"]
    dt_h = (now - last) / 3600.0 if last else 0.0
    in_pos = state["fsm"] == "CARRYING"
    note = ""

    # ── accrual while carrying ──────────────────────────────────────────────
    if in_pos and fr is not None and dt_h > 0:
        state["accrued_funding_usd"] += fr * config.NOTIONAL_PER_SIDE * dt_h

    # ── hysteresis timer for exit ───────────────────────────────────────────
    if in_pos and trailing_apr is not None and dt_h > 0:
        if trailing_apr < config.EXIT_APR:
            state["hours_below_exit"] += dt_h
        else:
            state["hours_below_exit"] = 0.0

    # ── transitions ─────────────────────────────────────────────────────────
    if not in_pos and should_enter(trailing_apr, spread, in_pos):
        state["fsm"] = "CARRYING"
        state["entry_ts"] = now
        state["accrued_funding_usd"] = 0.0
        state["rt_cost_booked_usd"] = config.MAKER_RT_COST_PCT / 100.0 * config.NOTIONAL_PER_SIDE
        state["hours_below_exit"] = 0.0
        state["entry_funding_apr"] = trailing_apr
        note = f"ENTER trailing_apr={trailing_apr:.1f}% spread={spread:.3f}%"
    elif in_pos and should_exit(trailing_apr, state["hours_below_exit"], in_pos):
        net = state["accrued_funding_usd"] - state["rt_cost_booked_usd"]
        state["realized_pnl_usd"] += net
        days = (now - state["entry_ts"]) / 86400.0 if state["entry_ts"] else 0.0
        note = (f"EXIT net=${net:+.4f} held={days:.1f}d "
                f"trailing_apr={trailing_apr:.1f}% (below {config.EXIT_APR}% for "
                f"{state['hours_below_exit']:.1f}h)")
        state.update(fsm="FLAT", entry_ts=None, accrued_funding_usd=0.0,
                     rt_cost_booked_usd=0.0, hours_below_exit=0.0, entry_funding_apr=None)

    # ── derived reporting ───────────────────────────────────────────────────
    if state["fsm"] == "CARRYING" and state["entry_ts"]:
        days_in_pos = (now - state["entry_ts"]) / 86400.0
        net_now = state["accrued_funding_usd"] - state["rt_cost_booked_usd"]
        net_apr = (net_now / config.NOTIONAL_PER_SIDE) / max(days_in_pos / 365.0, 1e-9) * 100
    else:
        days_in_pos = 0.0
        net_apr = 0.0

    state["last_tick_ts"] = now

    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            iso, state["fsm"],
            f"{fr_apr:.2f}" if fr_apr is not None else "",
            f"{trailing_apr:.2f}" if trailing_apr is not None else "",
            f"{spread:.4f}" if spread is not None else "",
            f"{state['accrued_funding_usd']:.4f}",
            f"{state['rt_cost_booked_usd']:.4f}",
            f"{state['realized_pnl_usd']:.4f}",
            f"{days_in_pos:.2f}", f"{net_apr:.2f}", note,
        ])
    save_state(state)
    f_s = f"{fr_apr:.1f}%" if fr_apr is not None else "NA"
    t_s = f"{trailing_apr:.1f}%" if trailing_apr is not None else "NA"
    s_s = f"{spread:.3f}%" if spread is not None else "NA"
    tail = f" | {note}" if note else ""
    print(f"[paper] {iso} fsm={state['fsm']} fundingAPR={f_s} trailingAPR={t_s} "
          f"spread={s_s} accrued=${state['accrued_funding_usd']:.4f} "
          f"realized=${state['realized_pnl_usd']:.4f}{tail}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single tick then exit (smoke test)")
    args = ap.parse_args()

    ensure_log_header()
    state = load_state()
    spot_name = spot_pair_name(config.COIN)
    print(f"[paper] start coin={config.COIN} notional/side=${config.NOTIONAL_PER_SIDE} "
          f"spot_pair={spot_name} entry>{config.ENTRY_APR}%APR exit<{config.EXIT_APR}%APR "
          f"sustain={config.EXIT_SUSTAIN_H}h interval={config.POLL_INTERVAL_SEC}s "
          f"testnet={config.TESTNET}", flush=True)

    if args.once:
        tick(state, spot_name)
        return

    while True:
        try:
            tick(state, spot_name)
        except Exception as e:
            print(f"[paper] tick error: {e}", file=sys.stderr, flush=True)
        time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
