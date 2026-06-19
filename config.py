"""Central config for the `slow` PURR funding-carry bot (Phase 0 — paper).

Strategy (validated 2026-06-19 on a full year of real HL funding): long PURR spot +
short PURR perp = structurally zero delta forever; the short leg earns funding hourly
while funding is positive. NET +24.5% APR after a realistic round-trip cost.

Phase 0 uses ONLY public read-only data (no keys). All execution/risk params below are
declared now so later phases inherit one source of truth.
"""

# ── Instrument ──────────────────────────────────────────────────────────────
COIN = "PURR"                 # lead carry coin (HYPE is a weaker diversifier, not lead)
NOTIONAL_PER_SIDE = 300.0     # $ per leg; ~$400 deposit equivalent at 1x

# ── Entry / exit policy (funding-driven; entry rare, exit rarer) ─────────────
# Decision uses the trailing-average funding APR over FUNDING_LOOKBACK_H hours.
FUNDING_LOOKBACK_H = 60       # 48–72h smoothing window for the funding signal
ENTRY_APR = 15.0              # enter when trailing avg funding APR > +15% (above floor)
EXIT_APR = 0.0               # exit when trailing avg funding APR < ~0% (sustained)
EXIT_SUSTAIN_H = 12          # exit only after EXIT_APR breached for this many hours
FUNDING_FLOOR_APR = 10.9     # HL floor (premium=0): floor hours alone must NOT trigger entry

# ── Execution sizing (maker ladder — Phase 1+) ──────────────────────────────
CLIP_USD = 50.0              # per-clip notional; small => one-leg risk is pennies
MAX_UNHEDGED_USD = 25.0      # max |delta| tolerated mid-entry before chasing lagging leg
MAX_UNHEDGED_SECONDS = 60    # max seconds one leg may sit unhedged before taker catch-up

# ── Cost / guards ───────────────────────────────────────────────────────────
SPREAD_GUARD_PCT = 0.60      # skip entry if either leg's top-of-book spread % exceeds this
MAKER_RT_COST_PCT = 0.10     # round-trip cost, fees-only maker (taker-est was 0.70%)

# ── Runtime ─────────────────────────────────────────────────────────────────
TESTNET = False              # Phase 0 reads MAINNET public data; live keys come in Phase 1
POLL_INTERVAL_SEC = 600.0    # 10 min between samples (funding acts hourly)

# HL public info endpoint (no key, no SDK needed for Phase 0)
MAINNET_INFO_URL = "https://api.hyperliquid.xyz/info"
TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"


def info_url() -> str:
    return TESTNET_INFO_URL if TESTNET else MAINNET_INFO_URL
