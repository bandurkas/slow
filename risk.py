"""Safety checks: STOP-file kill switch and delta-band (hedge integrity).

Kept tiny and pure so run.py can call it each loop. Margin-buffer / API-throttle live in
run.py where the SDK clients are.
"""
import os

import config


def stop_requested():
    """True if a STOP file exists in the working dir — graceful flatten + halt."""
    return os.path.exists(config.STOP_FILE)


def delta_broken(spot_qty, perp_qty):
    """True if the two legs have drifted apart beyond DELTA_BAND_TOKENS_PCT.

    perp_qty is signed (short = negative); we compare magnitudes. A real delta-neutral
    carry holds |spot| == |perp|; a large gap means a leg failed to fill/close => HALT.
    """
    sp = abs(spot_qty)
    pp = abs(perp_qty)
    ref = max(sp, pp)
    if ref < 1e-9:
        return False  # both flat
    gap_pct = abs(sp - pp) / ref * 100.0
    return gap_pct > config.DELTA_BAND_TOKENS_PCT
