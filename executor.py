"""Phase 1 execution core — forked from ~/Desktop/Fun/live_bot.py (hardened 2026-06-18).

Wraps the hyperliquid SDK Exchange/Info with the battle-tested primitives: metadata
resolution (tick/lot decimals), fill parsing, balance/position reads, atomic two-leg entry
with rollback, and full-position close (reduce-only perp + sell actual spot balance) so a
leg mismatch can never flip us into a directional position.

v1 uses TAKER (market) orders — the path already proven in live_bot. Maker laddering
(config.EXECUTION_MODE == "maker") is a planned refinement before scaling past a few
hundred dollars; not implemented here yet.
"""
import logging

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

import config

logger = logging.getLogger("slow.executor")


def parse_fill(res):
    """(ok, filled_sz, avg_px, detail). ok True only if part actually filled.

    The SDK wraps per-order results in response.data.statuses[]; an outer status=="ok"
    can still hold an inner error or a resting (unfilled) order, so we inspect each.
    """
    try:
        if not isinstance(res, dict) or res.get("status") != "ok":
            return (False, 0.0, 0.0, str(res))
        statuses = res["response"]["data"]["statuses"]
        total_sz = 0.0
        avg_px = 0.0
        errs = []
        for st in statuses:
            if "filled" in st:
                f = st["filled"]
                total_sz += float(f["totalSz"])
                avg_px = float(f["avgPx"])
            elif "error" in st:
                errs.append(st["error"])
            else:
                errs.append(f"unfilled: {st}")
        if total_sz > 0:
            return (True, total_sz, avg_px, errs or None)
        return (False, 0.0, 0.0, errs or "no fill")
    except Exception as e:
        return (False, 0.0, 0.0, f"parse error: {e} / {res}")


class Executor:
    """Holds SDK clients + resolved symbols and exposes enter/close/reconcile."""

    def __init__(self, coin, private_key, account_address, is_testnet):
        self.coin = coin.upper()
        self.account_address = account_address
        base_url = constants.TESTNET_API_URL if is_testnet else constants.MAINNET_API_URL
        self.info = Info(base_url, skip_ws=True)
        wallet = eth_account.Account.from_key(private_key)
        self.exchange = Exchange(wallet, base_url, account_address=account_address)
        self.signer_address = wallet.address
        (self.spot_coin, self.perp_coin,
         self.spot_decimals, self.perp_decimals) = self._resolve_symbols()
        self.common_decimals = min(self.spot_decimals, self.perp_decimals)
        logger.info(f"Executor ready: spot={self.spot_coin} perp={self.perp_coin} "
                    f"common_decimals={self.common_decimals} signer={self.signer_address} "
                    f"account={self.account_address}")

    # ── metadata ────────────────────────────────────────────────────────────
    def _resolve_symbols(self):
        meta = self.info.meta()
        perp_asset = next((u for u in meta.get("universe", [])
                           if u.get("name") == self.coin), None)
        if not perp_asset:
            raise ValueError(f"perp asset '{self.coin}' not in meta")
        perp_decimals = perp_asset["szDecimals"]

        sm = self.info.spot_meta()
        spot_token = next((t for t in sm.get("tokens", [])
                           if t.get("name", "").upper() == self.coin), None)
        if not spot_token:
            raise ValueError(f"token '{self.coin}' not in spotMeta")
        token_idx = spot_token["index"]
        spot_decimals = spot_token["szDecimals"]
        spot_pair = next((p for p in sm.get("universe", [])
                          if p.get("tokens", [None])[0] == token_idx), None)
        if not spot_pair:
            raise ValueError(f"no spot pair for token '{self.coin}'")
        return spot_pair.get("name"), self.coin, spot_decimals, perp_decimals

    # ── reads ─────────────────────────────────────────────────────────────────
    def spot_balance(self):
        try:
            st = self.info.spot_user_state(self.account_address)
            for b in st.get("balances", []):
                if b.get("coin", "").upper() == self.coin:
                    return float(b.get("total", 0.0))
        except Exception as e:
            logger.error(f"spot_balance query failed: {e}")
        return 0.0

    def perp_position(self):
        """Signed perp size (negative = short)."""
        try:
            st = self.info.user_state(self.account_address)
            for ap in st.get("assetPositions", []):
                pos = ap.get("position", {})
                if pos.get("coin", "").upper() == self.coin:
                    return float(pos.get("szi", 0.0))
        except Exception as e:
            logger.error(f"perp_position query failed: {e}")
        return 0.0

    def mid_prices(self):
        """(spot_mid, perp_mid) from L2 top of book; (None,None) on failure."""
        def mid(name):
            try:
                book = self.info.l2_snapshot(name)
                bids, asks = book["levels"][0], book["levels"][1]
                if not bids or not asks:
                    return None
                return (float(bids[0]["px"]) + float(asks[0]["px"])) / 2
            except Exception:
                return None
        return mid(self.spot_coin), mid(self.perp_coin)

    def reconcile(self):
        """(perp_pos, spot_bal, flat) snapshot of real exchange positions."""
        perp = self.perp_position()
        spot = self.spot_balance()
        flat = abs(perp) < 1e-9 and spot < 1e-9
        return perp, spot, flat

    # ── orders ────────────────────────────────────────────────────────────────
    def _coin_size(self, notional_usd, spot_px, perp_px):
        size = (2.0 * notional_usd) / (spot_px + perp_px)
        return round(size, self.common_decimals)

    def set_leverage(self):
        """Force the perp leg to config.LEVERAGE before entry (1x = minimal liq risk)."""
        try:
            res = self.exchange.update_leverage(config.LEVERAGE, self.perp_coin, config.LEVERAGE_IS_CROSS)
            logger.info(f"set leverage {config.LEVERAGE}x cross={config.LEVERAGE_IS_CROSS}: {res}")
            return isinstance(res, dict) and res.get("status") == "ok"
        except Exception as e:
            logger.error(f"set_leverage failed: {e}")
            return False

    def enter(self, notional_usd):
        """Atomic two-leg entry (buy spot, short perp) with rollback on partial fill.

        Returns dict: ok, spot_sz, perp_sz, spot_px, perp_px, detail.
        """
        if not self.set_leverage():
            return {"ok": False, "detail": "could not set leverage; refusing entry"}
        spot_mid, perp_mid = self.mid_prices()
        if not spot_mid or not perp_mid:
            return {"ok": False, "detail": "no book for sizing"}
        size = self._coin_size(notional_usd, spot_mid, perp_mid)
        if size <= 0:
            return {"ok": False, "detail": f"size 0 after rounding ({self.common_decimals}d)"}
        if size * spot_mid < config.MIN_NOTIONAL_USD or size * perp_mid < config.MIN_NOTIONAL_USD:
            return {"ok": False, "detail": f"notional < ${config.MIN_NOTIONAL_USD} min"}

        logger.info(f"ENTER target {size} {self.coin} (~${size*spot_mid:.2f}/leg) buy spot + short perp")
        spot_res = self.exchange.market_open(self.spot_coin, True, size, None, config.SLIPPAGE)
        s_ok, s_sz, s_px, s_detail = parse_fill(spot_res)
        logger.info(f"spot buy: ok={s_ok} sz={s_sz} px={s_px} detail={s_detail}")

        perp_res = self.exchange.market_open(self.perp_coin, False, size, None, config.SLIPPAGE)
        p_ok, p_sz, p_px, p_detail = parse_fill(perp_res)
        logger.info(f"perp short: ok={p_ok} sz={p_sz} px={p_px} detail={p_detail}")

        if s_ok and p_ok:
            if abs(s_sz - p_sz) > (10 ** -self.common_decimals):
                logger.warning(f"leg size mismatch spot {s_sz} vs perp {p_sz}; close flattens each leg")
            return {"ok": True, "spot_sz": s_sz, "perp_sz": p_sz,
                    "spot_px": s_px, "perp_px": p_px, "detail": None}

        logger.error("entry leg failure — rolling back filled leg(s)")
        flat = self.close(reason="entry-rollback", recorded_spot=s_sz if s_ok else 0.0)
        return {"ok": False, "spot_sz": 0.0, "perp_sz": 0.0,
                "rolled_back_flat": flat, "detail": f"spot={s_detail} perp={p_detail}"}

    def close(self, reason, recorded_spot=0.0):
        """Flatten both legs: reduce-only perp close + sell actual spot balance. True if flat."""
        logger.warning(f"CLOSE ({reason})")
        ok = True
        perp_pos = self.perp_position()
        if abs(perp_pos) > 0:
            res = self.exchange.market_close(self.perp_coin, None, None, config.SLIPPAGE)
            p_ok, p_sz, p_px, p_detail = parse_fill(res)
            logger.info(f"perp close: ok={p_ok} sz={p_sz} px={p_px} detail={p_detail}")
            ok = ok and p_ok
        else:
            logger.info("no perp position to close")

        bal = self.spot_balance()
        if bal <= 0 and recorded_spot > 0:
            bal = recorded_spot
        if bal > 0:
            res = self.exchange.market_open(self.spot_coin, False, bal, None, config.SLIPPAGE)
            s_ok, s_sz, s_px, s_detail = parse_fill(res)
            logger.info(f"spot sell: ok={s_ok} sz={s_sz} px={s_px} detail={s_detail}")
            ok = ok and s_ok
        else:
            logger.info("no spot balance to sell")
        return ok
