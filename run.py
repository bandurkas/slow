#!/usr/bin/env python3
"""Phase 1 LIVE state machine for the PURR carry bot.

FSM: FLAT -> ENTERING -> CARRYING -> EXITING -> FLAT (+ HALTED). With TAKER execution
ENTERING/EXITING resolve synchronously (market fills), but the states are persisted so a
crash mid-action is recoverable. Decision is funding-driven (strategy.py) over the trailing
funding APR; exit uses hysteresis. Reconcile at startup (refuse to start on a state/exchange
mismatch = anti-double-open) and every RECONCILE_INTERVAL (delta-band -> HALT).

Usage:
  python3 run.py status     # print exchange snapshot + funding (read-only, safe)
  python3 run.py enter      # ONE forced atomic entry (smoke test)
  python3 run.py exit       # ONE forced flatten (smoke test)
  python3 run.py            # autonomous loop (real funding decisions)
Kill switch: create a file named STOP in the working dir -> graceful flatten + halt.
"""
import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

import config
import risk
from accounting import realized_funding_since
from executor import Executor
from market import current_funding, book_mid_spread, trailing_funding_apr, spot_pair_name
from notify import notify
from strategy import should_enter, should_exit

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("slow.run")

STATE_PATH = Path(__file__).resolve().parent / "state_live.json"

FRESH = {
    "coin": config.COIN, "fsm": "FLAT",
    "spot_size": 0.0, "perp_size": 0.0,
    "entry_ts": None, "entry_ts_ms": None, "entry_funding_apr": None,
    "hours_below_exit": 0.0, "last_tick_ts": None,
}


def load_state():
    if STATE_PATH.exists():
        try:
            s = dict(FRESH)
            s.update(json.loads(STATE_PATH.read_text()))
            return s
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"state load failed ({e}); starting fresh")
    return dict(FRESH)


def save_state(s):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    os.replace(tmp, STATE_PATH)


def build_executor():
    load_dotenv()
    pk = os.getenv(config.ENV_PRIVATE_KEY)
    addr = os.getenv(config.ENV_ACCOUNT_ADDRESS)
    is_testnet = os.getenv(config.ENV_IS_TESTNET, "True").lower() == "true"
    # Keep the read layer (market.py funding/spread) on the SAME network we trade,
    # so the spread guard and funding decision see the book the orders will hit.
    config.TESTNET = is_testnet
    if not pk or not addr:
        logger.error(f"{config.ENV_PRIVATE_KEY} and {config.ENV_ACCOUNT_ADDRESS} must be set in .env")
        sys.exit(1)
    if not addr.startswith("0x") or len(addr) != 42:
        logger.error("HL_ACCOUNT_ADDRESS looks invalid (need 42-char 0x master address)")
        sys.exit(1)
    logger.info(f"Mode: {'TESTNET' if is_testnet else 'MAINNET'} | coin={config.COIN} "
                f"| notional/side=${config.LIVE_NOTIONAL_PER_SIDE} | exec={config.EXECUTION_MODE}")
    if config.EXECUTION_MODE != "taker":
        logger.warning(f"EXECUTION_MODE={config.EXECUTION_MODE} not implemented; v1 is taker-only")
    return Executor(config.COIN, pk, addr, is_testnet)


def startup_reconcile(ex, state):
    """Refuse to start on a dangerous state/exchange mismatch (anti-double-open)."""
    perp, spot, flat = ex.reconcile()
    logger.info(f"startup snapshot: perp={perp} spot={spot} flat={flat} | state.fsm={state['fsm']}")
    in_pos = state["fsm"] in ("CARRYING", "ENTERING", "EXITING")
    if in_pos and flat:
        logger.warning("state says IN POSITION but exchange is flat -> reset to FLAT")
        state.update(dict(FRESH))
        save_state(state)
    elif not in_pos and not flat:
        logger.error("exchange shows an OPEN position but state is FLAT. Refusing to start "
                     "(avoid double-open). Flatten manually or fix state_live.json.")
        notify("HALT at startup: exchange open but state flat — manual check needed")
        sys.exit(1)


def print_status(ex, state):
    perp, spot, flat = ex.reconcile()
    fr = current_funding(config.COIN)
    tr = trailing_funding_apr(config.COIN, config.FUNDING_LOOKBACK_H)
    _, perp_sp = book_mid_spread(config.COIN)
    fr_apr = fr * 8760 * 100 if fr is not None else None
    print(f"fsm={state['fsm']} flat={flat} perp_pos={perp} spot_bal={spot}")
    print(f"funding now: {fr_apr:.2f}% APR" if fr_apr is not None else "funding now: NA")
    print(f"trailing {config.FUNDING_LOOKBACK_H}h: {tr:.2f}% APR" if tr is not None else "trailing: NA")
    print(f"perp spread: {perp_sp:.4f}%" if perp_sp is not None else "perp spread: NA")
    if state.get("entry_ts_ms"):
        usd, n = realized_funding_since(ex.account_address, config.COIN, state["entry_ts_ms"])
        print(f"realized funding since entry: ${usd:+.4f} ({n} events)")


def do_enter(ex, state):
    state["fsm"] = "ENTERING"
    save_state(state)
    res = ex.enter(config.LIVE_NOTIONAL_PER_SIDE)
    if res.get("ok"):
        now = time.time()
        state.update(fsm="CARRYING", spot_size=res["spot_sz"], perp_size=res["perp_sz"],
                     entry_ts=now, entry_ts_ms=int(now * 1000),
                     entry_funding_apr=trailing_funding_apr(config.COIN, config.FUNDING_LOOKBACK_H),
                     hours_below_exit=0.0)
        save_state(state)
        logger.info(f"ENTERED spot={res['spot_sz']} perp={res['perp_sz']} "
                    f"@ spot {res['spot_px']} / perp {res['perp_px']}")
        notify(f"ENTER {config.COIN} spot={res['spot_sz']} perp={res['perp_sz']}")
        return True
    state.update(dict(FRESH))
    save_state(state)
    logger.error(f"ENTER failed: {res.get('detail')} rolled_back_flat={res.get('rolled_back_flat')}")
    if res.get("rolled_back_flat") is False:
        notify("HALT: entry rollback FAILED — possible naked exposure")
        state["fsm"] = "HALTED"
        save_state(state)
    return False


def do_exit(ex, state, reason):
    state["fsm"] = "EXITING"
    save_state(state)
    ok = ex.close(reason=reason, recorded_spot=state.get("spot_size", 0.0))
    if ok:
        usd = n = None
        if state.get("entry_ts_ms"):
            usd, n = realized_funding_since(ex.account_address, config.COIN, state["entry_ts_ms"])
        state.update(dict(FRESH))
        save_state(state)
        logger.info(f"EXITED ({reason}); flat." + (f" realized funding ${usd:+.4f}" if usd is not None else ""))
        notify(f"EXIT {config.COIN} ({reason})" + (f" funding ${usd:+.4f}" if usd is not None else ""))
        return True
    logger.error(f"EXIT ({reason}) did not fully flatten; will retry")
    state["fsm"] = "CARRYING"  # stay in pos and retry next loop
    save_state(state)
    return False


def tick(ex, state):
    """One autonomous decision cycle."""
    now = time.time()
    perp, spot, flat = ex.reconcile()

    # delta-band integrity (only meaningful when we believe we're in a position)
    if state["fsm"] == "CARRYING" and risk.delta_broken(spot, perp):
        logger.error(f"DELTA BROKEN spot={spot} perp={perp} -> HALT")
        notify(f"HALT: delta broken spot={spot} perp={perp}")
        state["fsm"] = "HALTED"
        save_state(state)
        return

    fr = trailing_funding_apr(config.COIN, config.FUNDING_LOOKBACK_H)
    _, spread = book_mid_spread(config.COIN)
    in_pos = state["fsm"] == "CARRYING"

    # hysteresis timer
    last = state["last_tick_ts"]
    dt_h = (now - last) / 3600.0 if last else 0.0
    if in_pos and fr is not None and dt_h > 0:
        if fr < config.EXIT_APR:
            state["hours_below_exit"] += dt_h
        else:
            state["hours_below_exit"] = 0.0
    state["last_tick_ts"] = now

    fr_s = f"{fr:.1f}%" if fr is not None else "NA"
    sp_s = f"{spread:.3f}%" if spread is not None else "NA"
    logger.info(f"tick fsm={state['fsm']} trailingAPR={fr_s} spread={sp_s} "
                f"below_exit={state['hours_below_exit']:.1f}h perp={perp} spot={spot}")

    if not in_pos and state["fsm"] == "FLAT" and should_enter(fr, spread, in_pos):
        logger.info(f"ENTRY SIGNAL trailingAPR={fr_s} spread={sp_s}")
        do_enter(ex, state)
    elif in_pos and should_exit(fr, state["hours_below_exit"], in_pos):
        logger.info(f"EXIT SIGNAL trailingAPR={fr_s} below_exit={state['hours_below_exit']:.1f}h")
        do_exit(ex, state, "funding-decay")
    else:
        save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", nargs="?", default="loop", choices=["loop", "status", "enter", "exit"])
    args = ap.parse_args()

    ex = build_executor()
    state = load_state()
    startup_reconcile(ex, state)

    if args.action == "status":
        print_status(ex, state)
        return
    if args.action == "enter":
        do_enter(ex, state)
        return
    if args.action == "exit":
        do_exit(ex, state, "manual")
        return

    last_reconcile = 0.0
    logger.info("autonomous loop start")
    while True:
        try:
            if risk.stop_requested():
                logger.warning("STOP file present -> flatten + halt")
                if state["fsm"] == "CARRYING":
                    do_exit(ex, state, "STOP-file")
                state["fsm"] = "HALTED"
                save_state(state)
                notify("HALTED by STOP file")
                break
            if state["fsm"] == "HALTED":
                logger.error("FSM HALTED — manual intervention required. Exiting loop.")
                break
            tick(ex, state)
        except Exception as e:
            logger.error(f"tick error: {e}", exc_info=True)
        time.sleep(config.POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
