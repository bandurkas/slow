"""Pure entry/exit decision logic — funding + spread only, no I/O.

Entry is rare; exit is rarer (a round-trip is ~3 days of carry, so we HOLD). Floor hours
(funding at the HL premium=0 floor, ~+10.9% APR) must NOT, on their own, trigger entry —
the edge is hours with premium ABOVE the floor. Exit uses a dead-band + sustain timer so
a brief dip below 0% APR does not knock us out of a position that's still net-positive.
"""
import config


def should_enter(trailing_apr, spread_pct, in_position):
    """Enter when funding is convincingly above the floor and spreads are sane."""
    if in_position or trailing_apr is None or spread_pct is None:
        return False
    if trailing_apr <= config.ENTRY_APR:
        return False
    if trailing_apr <= config.FUNDING_FLOOR_APR:   # floor hours alone are not edge
        return False
    if spread_pct > config.SPREAD_GUARD_PCT:
        return False
    return True


def should_exit(trailing_apr, hours_below_exit, in_position):
    """Exit when trailing funding APR has stayed below EXIT_APR for EXIT_SUSTAIN_H hours.

    `hours_below_exit` is the caller-tracked running count of consecutive hours the
    trailing APR has been under EXIT_APR (the dead-band / hysteresis state).
    """
    if not in_position or trailing_apr is None:
        return False
    if trailing_apr >= config.EXIT_APR:
        return False
    return hours_below_exit >= config.EXIT_SUSTAIN_H
