# slow — PURR delta-neutral funding-carry bot

Long PURR **spot** + short PURR **perp** (equal notional) on Hyperliquid. The two legs are
the *same token*, so delta is **structurally zero forever** — no rebalancing. While funding
is positive the short leg receives it hourly; the spot leg neutralizes price. Validated on a
full year of real HL funding: **NET +24.5% APR** after a realistic round-trip cost, positive
funding 98% of hours, persistence holds in both train and holdout halves.

> Built measure-before-risk: paper → testnet → mainnet $400 → scale. We are at **Phase 0**.

## Phase 0 — paper (current)
Read-only public API, **no keys, no orders**. `paper_sim.py` runs the real decision FSM on
live funding + spreads to confirm the logic and the +24.5% model survive live conditions.

```
config.py     # COIN, notional, entry/exit APR thresholds, lookback, guards, costs
market.py     # read-only HL /info: current_funding, funding_history, trailing APR, book spread
strategy.py   # pure should_enter() / should_exit() (funding + spread, hysteresis)
validate.py   # historical backtest (real fundingHistory, paginated, train/holdout, $/day)
paper_sim.py  # Phase 0 FSM on live data -> paper_log.csv (restart-safe via state.json)
```

State machine: `FLAT → CARRYING → FLAT` (executor/risk/accounting/notify modules land in
Phase 1+). Entry is rare; exit rarer — a round-trip is ~3 days of carry, so we HOLD.

### Run
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

python3 validate.py                 # expect PURR ~ NET +24.5% APR
python3 paper_sim.py --once         # single smoke-test tick
python3 paper_sim.py                # loop (deploy in screen/systemd)
```

### Deploy (VPS3, screen)
```bash
ssh root@187.127.114.34
git clone git@github.com:bandurkas/slow.git /root/slow && cd /root/slow
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
screen -S slow -dm python3 paper_sim.py
```

## Roadmap
- **Phase 0** paper on VPS3, ~1–2 weeks — confirm net on live spreads *(now)*
- **Phase 1** testnet: fork `live_bot.py` → `executor.py`, atomic maker two-leg entry/exit
- **Phase 2** mainnet $400, ~2 weeks, reconcile net carry vs model
- **Phase 3** scale to ~$5k (~$2.5/day) if it tracks. Bonus: HL Season 2 airdrop volume.
