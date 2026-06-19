"""Read-only HL market access for Phase 0 (raw POST /info, requests-only, no keys).

Kept SDK-free on purpose: Phase 0 deploy needs nothing but `pip install requests`.
Phase 1 execution will use the hyperliquid SDK's Exchange (keys); this module stays the
public read layer.

Provides:
  current_funding(coin)        -> hourly funding rate (float) from metaAndAssetCtxs
  funding_history(coin, days)  -> paginated [{time, fundingRate, premium}, ...]
  trailing_funding_apr(coin,h) -> mean hourly rate over the last h hours, annualized %
  book_mid_spread(name)        -> (mid, spread_pct) from top of book
  spot_pair_name(coin)         -> "PURR/USDC"-style spot pair for the spot leg's book
"""
import math
import time

import requests

from config import info_url


def _post(payload, retries=5, backoff=1.2):
    url = info_url()
    for i in range(retries):
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 200:
                return r.json()
            time.sleep(backoff * (2 ** i) if r.status_code == 429 else backoff)
        except Exception:
            time.sleep(backoff)
    return None


def current_funding(coin):
    """Current hourly funding rate for `coin` perp, or None on failure."""
    data = _post({"type": "metaAndAssetCtxs"})
    if not data or len(data) != 2:
        return None
    meta, ctxs = data
    for u, c in zip(meta["universe"], ctxs):
        if u["name"].upper() == coin.upper():
            try:
                return float(c.get("funding"))
            except (TypeError, ValueError):
                return None
    return None


def funding_history(coin, days):
    """Paginate fundingHistory forward from `days` ago to now (HL caps 500/call)."""
    start = int(time.time() * 1000) - days * 86_400_000
    out, seen = [], set()
    while True:
        chunk = _post({"type": "fundingHistory", "coin": coin, "startTime": start})
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
        time.sleep(0.05)
    out.sort(key=lambda h: h["time"])
    return out


def trailing_funding_apr(coin, hours):
    """Mean hourly funding rate over the last `hours`, annualized to %.

    Returns None if insufficient history. Uses ceil(hours/24)+1 days to be safe.
    """
    days = math.ceil(hours / 24) + 1
    hist = funding_history(coin, days)
    if not hist:
        return None
    cutoff = int(time.time() * 1000) - int(hours * 3_600_000)
    rates = [float(h["fundingRate"]) for h in hist if h["time"] >= cutoff]
    if len(rates) < max(2, hours // 4):  # need a meaningful sample
        return None
    return sum(rates) / len(rates) * 8760 * 100


def book_mid_spread(name):
    """(mid, spread_pct) from top of book for a perp coin or spot pair name."""
    book = _post({"type": "l2Book", "coin": name})
    if not book:
        return None, None
    try:
        bids, asks = book["levels"][0], book["levels"][1]
        if not bids or not asks:
            return None, None
        bid, ask = float(bids[0]["px"]), float(asks[0]["px"])
        mid = (bid + ask) / 2
        return mid, (ask - bid) / mid * 100
    except (KeyError, IndexError, ValueError, ZeroDivisionError):
        return None, None


def spot_pair_name(coin):
    """Map a perp coin -> its USDC spot pair name (e.g. PURR -> 'PURR/USDC')."""
    sm = _post({"type": "spotMeta"})
    if not sm:
        return None
    tok = {t["index"]: t["name"].upper() for t in sm["tokens"]}
    for p in sm["universe"]:
        a, b = p["tokens"]
        names = {tok.get(a), tok.get(b)}
        if "USDC" in names and coin.upper() in names:
            return p["name"]
    return None
