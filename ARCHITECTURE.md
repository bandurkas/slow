# Carry Desk — Architecture

An active, multi-venue funding-carry **organism**: eyes that scan and score every venue, a
brain that decides where capital should be, a metabolism that rotates out of decaying carries
into live ones (only when the move clears its round-trip cost), and an immune system that can
halt any of it. Goal: squeeze the maximum *risk-adjusted* yield out of perp funding across
venues — daily profit, not a single passive position.

> Discipline (unchanged): **measure → paper → testnet/small → scale.** Detection is free and
> safe; execution moves real money; the rotation logic is where money is made *and lost*.

---

## 0. Design principles

1. **Separate the four concerns hard:** DATA (read-only) · DECISION · EXECUTION (risk) ·
   RISK-OVERRIDE. A bug in decision logic must never bypass the risk layer.
2. **Venue = a pluggable adapter**, never special-cased in the core. One interface; HL is the
   first implementation (the `slow` executor), every other venue conforms to it.
3. **Modular monolith first, microservices when the seams actually hurt.** Design clean
   contracts between services now; deploy as one process until scale/ops justify splitting.
   A single-operator system does not need 9 deployed services and k8s on day one — that
   complexity is a cost, not a feature.
4. **Rotation is expensive — default to HOLD.** The validation proved exit is dear; the
   rebalancer must clear a round-trip break-even gate before acting. Churn is the #1 way to
   lose money here.
5. **Custody risk is real and per-venue.** Each venue (esp. CEX/bridged) is a tail risk → hard
   capital caps per venue.

---

## 1. Venue landscape (honest tiering)

Clean delta-neutral carry wants **spot + perp on the SAME venue** (no bridge, no cross-venue
basis). That splits the field:

**Tier A — one-venue hedge (clean, build first):**
- **Hyperliquid** ✅ (perp + spot, hourly funding + floor → persistent PURR). Live now.
- **Bybit** (CEX, perp + spot, 8h funding, tight spreads → good for spike-harvest). Geo: reach
  from VPS3. Custody risk (CEX).
- **Drift** (Solana, perp + spot, funding). Backpack (CEX-ish, perp + spot).

**Tier B — perp-only, need an EXTERNAL spot hedge (higher complexity, sometimes juicier):**
- **dYdX v4**, **Vertex**, **Paradex** (Starknet), **Aevo**, **GMX / Gains** (borrow-fee model,
  not classic funding — different math), **Jupiter Perps**.
- These require a spot leg on another venue (CEX spot or DEX spot/Uniswap) → cross-venue capital
  fragmentation, bridge latency/risk, two-venue execution. Only worth it when funding is *much*
  higher AND persistent. Add as adapters AFTER the Tier-A organism works.

Takeaway: more venues = more opportunity but hedge logistics + custody/bridge risk scale fast.
Expand Tier A fully before reaching into Tier B.

---

## 2. Service decomposition

```
                         ┌─────────────────────────────┐
        venues  ───────▶ │ 1. SCANNER  ("eyes")        │  read-only collectors per venue
   (HL,Bybit,Drift,…)    │  funding+spread+depth, norm │  → normalized OPPORTUNITY BOOK
                         │  persistence + spike scorer │  (radar.py + spike_harvester.py)
                         └──────────────┬──────────────┘
                                        ▼
                         ┌─────────────────────────────┐
                         │ 2. ALLOCATOR ("brain")      │  opportunity book + positions
                         │  target book: which venue/  │  → DESIRED STATE (caps: per-coin
                         │  coin/size, risk-weighted   │   liquidity, per-venue custody, lev)
                         └──────────────┬──────────────┘
                                        ▼
                         ┌─────────────────────────────┐
                         │ 3. REBALANCER ("metabolism")│  diff current vs desired,
                         │  ACT only if Δfunding >      │  emit ENTER/EXIT/RESIZE actions
                         │  round-trip break-even      │  (hysteresis, min-hold, net gate)
                         └──────────────┬──────────────┘
                                        ▼
        ┌───────────────────────────────────────────────────────────┐
        │ 4. EXECUTION ADAPTERS (per venue, common interface)        │  atomic 2-leg entry/close,
        │   HL ✅ | Bybit ⬜ | Drift ⬜ | …                            │  reconcile, leverage, rollback
        └───────────────────────────────────────────────────────────┘
              ▲                         ▲                        ▲
   ┌──────────┴─────────┐   ┌───────────┴──────────┐   ┌─────────┴──────────┐
   │ 5. RISK/GUARDIAN   │   │ 6. ACCOUNTING/LEDGER │   │ 7. TREASURY/ROUTER │
   │ delta-band, margin,│   │ ground-truth funding │   │ USDC across venues,│
   │ exposure caps,     │   │ realized P&L, attrib │   │ bridges, idle-cash │
   │ kill-switch, HALT  │   │ per venue/coin       │   │ minimization       │
   └────────────────────┘   └──────────────────────┘   └────────────────────┘
                                        ▲
                         ┌──────────────┴──────────────┐
                         │ 8. AI CO-PILOT / ORCHESTR.  │  daily summary, "WHY is it spiking"
                         │  decision SUPPORT, human-in- │  (news/exploit/delist/squeeze),
                         │  loop on capital allocation  │  anomaly narration, NL dashboard
                         └──────────────┬──────────────┘
                                        ▼
                              9. NOTIFIER / DASHBOARD (Telegram + web)

cross-cutting: message bus · state store (Postgres + time-series) · secrets manager
               (per-venue keys, NEVER in repo) · observability (logs/metrics/alerts)
```

### Execution adapter interface (every venue conforms)
```
class VenueAdapter:
    def resolve(coin) -> (spot_sym, perp_sym, decimals)
    def funding(coin) -> rate, interval_h          # normalized
    def spread(sym)   -> pct
    def positions()   -> (perp_signed, spot_qty)
    def set_leverage(coin, lev)
    def enter(coin, notional) -> {ok, spot_sz, perp_sz, px...}   # atomic + rollback
    def close(coin, reason)   -> ok                              # reduce-only + sell spot
    def realized_funding(coin, since_ms) -> usd                 # ground truth
```
The `slow` HL `executor.py` already implements this shape — generalize it into the interface,
then Bybit/Drift are just new classes.

---

## 3. The crux: rotation economics (service 3)

This is where the edge is won or lost. Rotating from carry A (funding decaying) into carry B
(funding hot) is only worth it if:

```
expected_extra_funding(B over A, over expected_hold) − round_trip_cost(exit A + enter B) > margin
```

- Round-trip cost = exit-A spreads/fees + enter-B spreads/fees (4 legs). On HL ~0.5%+, on Bybit
  ~0.1–0.5%. Break-even on PURR was ~10 days — **rotation that ignores this churns to death.**
- Gates: minimum hold period, hysteresis band on funding, and a hard net-of-cost check. Default
  HOLD; rotate only on a clear, persistent improvement. Paper-trade this engine before it
  touches real capital.

---

## 4. Honest hard problems (don't hand-wave these)

1. **Rotation churn** — the central algorithm; most validation effort goes here.
2. **Cross-venue capital fragmentation + bridge risk** — USDC moves between venues/chains take
   time and carry bridge/custody risk; idle-cash drag caps nimbleness. Treasury service owns it.
3. **Per-venue custody/counterparty risk** — hard caps; never all capital on one CEX.
4. **Hedge availability (Tier B)** — perp-only venues need an external spot leg = second venue =
   more fragility.
5. **Funding-mechanics normalization** — HL hourly+floor vs Bybit 8h mean-revert vs GMX
   borrow-fees vs dYdX hourly; comparing "carry yield" honestly across these is non-trivial.
6. **Crowded-short reversal** — spike harvests are crowded trades that snap back (funding flips
   negative + price squeeze). Risk service sizes them small and watches for the flip.

---

## 5. Phased roadmap (build order)

- **P0 — Detection** ✅ done: `radar.py` (persistent cores) + `spike_harvester.py` (Bybit spikes).
- **P1 — One live carry** ✅ done: HL PURR live, autonomous hold (`slow`).
- **P2 — Second venue + adapter interface:** generalize executor → `VenueAdapter`; build Bybit
  adapter; prove a *manual* HL↔Bybit harvest end-to-end.
- **P3 — Rebalancer (paper first):** the break-even-gated rotation engine, paper-traded against
  live data until its churn economics are proven.
- **P4 — Risk + Accounting as independent services:** guardian override, P&L attribution.
- **P5 — AI co-pilot + dashboard:** daily Telegram summary, "why" context, NL queries, anomaly
  narration; human-in-loop on allocation.
- **P6 — More venues (Drift, dYdX, …) as adapters**, and split modular monolith → microservices
  *when the seams hurt*, not before.

Each phase is measured before the next risks capital. The organism earns the right to grow.
