"""
Competition check: after we flag an opportunity at block b, did someone else
close it in blocks (b, b+N]?

We know the pool price before our hypothetical trade (sqrt_before) and the price
our optimal trade would have pushed it to (sqrt_after, roughly where the arb
stops being profitable). We follow the post-swap price of every Swap event in the
window. The gap counts as closed by others when a swap in the same direction
moves the pool price to sqrt_after or beyond.
"""

from __future__ import annotations

import math

from .costs import DEX_BUY


def check(direction: str, sqrt_before: int, sqrt_after: int, swaps: list[dict], base_is_token0: bool,
          opp_block: int, block_ts: dict[int, float] | None = None, opp_ts: float | None = None,
          base_decimals: int = 18) -> dict:
    """swaps: Swap events in (opp_block, opp_block+N], sorted. Returns the competition stats."""
    # Progress is measured in log-price; the sign of `span` encodes the direction of travel
    # (buying base pushes sqrtP up when base is token0, down otherwise), so frac is sign-agnostic.
    span = math.log(sqrt_after) - math.log(sqrt_before)
    res = dict(closed=0, after_blocks=None, after_s=None, frac=0.0, swaps=0, volume_base=0.0)
    if span == 0:
        return res
    for s in swaps:
        a_base = int(s["amount0"] if base_is_token0 else s["amount1"])  # pool's base delta
        same_dir = a_base < 0 if direction == DEX_BUY else a_base > 0  # pool paid out base = someone bought
        if not same_dir:
            continue
        res["swaps"] += 1
        res["volume_base"] += abs(a_base) / 10**base_decimals
        frac = (math.log(int(s["sqrt_price_x96"])) - math.log(sqrt_before)) / span
        res["frac"] = max(res["frac"], frac)
        if not res["closed"] and frac >= 1.0:
            res["closed"] = 1
            res["after_blocks"] = s["block"] - opp_block
            if block_ts and opp_ts is not None and s["block"] in block_ts:
                res["after_s"] = max(0.0, block_ts[s["block"]] - opp_ts)
    res["frac"] = min(res["frac"], 1.0)
    return res
