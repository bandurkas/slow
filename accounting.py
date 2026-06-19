"""Ground-truth funding accounting from the HL userFunding ledger.

The paper sim ESTIMATES accrual (rate*notional*dt); live P&L must come from what HL
actually paid. userFunding returns the real per-hour funding settlements for the account;
we sum them since entry to get realized funding $ (the whole point of the $90 test — see
the real $0.0x show up).
"""
import logging
import time

import requests

from config import info_url

logger = logging.getLogger("slow.accounting")


def realized_funding_since(account_address, coin, since_ms):
    """Sum actual funding payments (USD) for `coin` since `since_ms` from userFunding.

    HL convention: a short that RECEIVES funding shows a positive delta to the account;
    the ledger 'usdc' field carries the signed amount. Returns (total_usd, n_events).
    """
    payload = {"type": "userFunding", "user": account_address, "startTime": int(since_ms)}
    try:
        r = requests.post(info_url(), json=payload, timeout=20)
        if r.status_code != 200:
            logger.error(f"userFunding HTTP {r.status_code}")
            return 0.0, 0
        events = r.json() or []
    except Exception as e:
        logger.error(f"userFunding query failed: {e}")
        return 0.0, 0

    total = 0.0
    n = 0
    for ev in events:
        d = ev.get("delta", {})
        if d.get("coin", "").upper() != coin.upper():
            continue
        try:
            total += float(d.get("usdc", 0.0))
            n += 1
        except (TypeError, ValueError):
            continue
    return total, n


if __name__ == "__main__":
    import sys
    addr = sys.argv[1]
    coin = sys.argv[2] if len(sys.argv) > 2 else "PURR"
    since = int(time.time() * 1000) - 7 * 86_400_000
    usd, n = realized_funding_since(addr, coin, since)
    print(f"{coin} realized funding last 7d: ${usd:+.4f} over {n} events")
