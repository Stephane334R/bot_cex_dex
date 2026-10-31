"""
Optimal sizing and the trade/no-trade decision.

For a fixed market snapshot, net PnL as a function of size q is concave:
the CEX leg is linear in q (top of book), the DEX leg cost is convex (price
impact grows with q), and gas is a constant. So the optimum is found reliably by:

  1. a geometric scan over [min_size, cap] (this scan is also stored as the
     "size curve" for the dashboard), then
  2. golden-section refinement between the scan points around the best one.

The cap is the tightest of: max notional, CEX top-of-book size (unless
`assume_deep_liquidity`), the loaded tick window, and available inventory.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from amm.pool import OutOfLoadedRange

from .costs import DEX_BUY, DIRECTIONS, CostParams, DexQuoter, Pnl, pnl
from .inventory import Inventory

GOLDEN = (math.sqrt(5) - 1) / 2


@dataclass
class Market:
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float


@dataclass
class StrategyParams:
    max_notional_usd: float = 250_000
    min_size: float = 0.01
    threshold_usd: float = 1.0
    threshold_bps: float = 0.0
    grid_points: int = 32
    assume_deep_liquidity: bool = False

    @classmethod
    def from_cfg(cls, cfg) -> "StrategyParams":
        s = cfg.strategy
        return cls(s.max_notional_usd, s.min_size, s.threshold_usd, s.threshold_bps, s.grid_points,
                   cfg.cex.assume_deep_liquidity)


@dataclass
class DirectionEval:
    direction: str
    best: Pnl | None
    cap: float
    cap_reason: str
    curve: list[list[float]] = field(default_factory=list)  # [size, dex_quote_usd, dex_fee_usd]
    sqrt_price_after: int | None = None
    taken: bool = False


def geometric_grid(lo: float, hi: float, n: int) -> list[float]:
    if hi <= lo:
        return [lo]
    r = (hi / lo) ** (1 / (n - 1))
    return [lo * r**i for i in range(n)]


def evaluate_direction(direction: str, dq: DexQuoter, mkt: Market, gas_usd: float,
                       cp: CostParams, sp: StrategyParams, inv: Inventory | None) -> DirectionEval:
    ref_px = mkt.bid if direction == DEX_BUY else mkt.ask

    # --- caps that don't depend on the PnL curve --------------------------------
    caps = {"max_notional": sp.max_notional_usd / ref_px}
    try:
        # 0.999: the window max is exact, and exact-output rounding can need one more wei of range.
        caps["tick_window"] = dq.max_size(direction) * 0.999
    except OutOfLoadedRange:
        caps["tick_window"] = 0.0
    curve_max = min(caps.values())  # the stored curve ignores book/inventory caps on purpose
    if not sp.assume_deep_liquidity:
        caps["cex_top_of_book"] = mkt.bid_qty if direction == DEX_BUY else mkt.ask_qty
    if inv is not None:
        caps["inventory_base"] = inv.limits(direction)[0]
    cap_reason = min(caps, key=caps.get)
    cap = caps[cap_reason]

    # --- size curve ---------------------------------------------------------------
    legs = {}

    def leg(q: float):
        if q not in legs:
            legs[q] = dq.leg(direction, q)
        return legs[q]

    def objective(q: float) -> Pnl:
        lg = leg(q)
        return pnl(direction, q, lg.quote_amount, lg.fee_usd, mkt.bid, mkt.ask, gas_usd, cp)

    curve = []
    if curve_max >= sp.min_size:
        for q in geometric_grid(sp.min_size, curve_max, sp.grid_points):
            lg = leg(q)
            curve.append([q, lg.quote_amount, lg.fee_usd])

    # --- quote-currency inventory cap (needs the DEX leg, so done after) ---------
    if inv is not None and cap >= sp.min_size:
        def affordable(q: float) -> bool:
            r = objective(q)
            quote_needed = r.dex_quote_usd if direction == DEX_BUY else r.notional_usd + r.cex_fee_usd
            return inv.can_afford(direction, q, quote_needed)

        if not affordable(cap):
            lo, hi = 0.0, cap
            for _ in range(40):
                mid = 0.5 * (lo + hi)
                lo, hi = (mid, hi) if affordable(mid) else (lo, mid)
            cap, cap_reason = lo, "inventory_quote"

    ev = DirectionEval(direction, None, cap, cap_reason, curve)
    if cap < sp.min_size:
        return ev

    # --- scan + golden-section refinement ------------------------------------------
    pts = [q for q, *_ in curve if q <= cap] + [cap]
    vals = [objective(q).net_usd for q in pts]
    i = max(range(len(pts)), key=vals.__getitem__)
    a = pts[i - 1] if i > 0 else sp.min_size
    b = pts[i + 1] if i + 1 < len(pts) else cap
    c, d = b - GOLDEN * (b - a), a + GOLDEN * (b - a)
    fc, fd = objective(c).net_usd, objective(d).net_usd
    for _ in range(40):
        if b - a < 1e-6 * max(b, 1e-9):
            break
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - GOLDEN * (b - a)
            fc = objective(c).net_usd
        else:
            a, c, fc = c, d, fd
            d = a + GOLDEN * (b - a)
            fd = objective(d).net_usd
    candidates = [pts[i], c, d]
    q_best = max(candidates, key=lambda q: objective(q).net_usd)

    ev.best = objective(q_best)
    ev.sqrt_price_after = leg(q_best).sqrt_price_after
    ev.taken = ev.best.net_usd > sp.threshold_usd and ev.best.net_bps > sp.threshold_bps
    return ev


def evaluate(dq: DexQuoter, mkt: Market, gas_usd: float, cp: CostParams, sp: StrategyParams,
             inv: Inventory | None = None) -> dict[str, DirectionEval]:
    return {d: evaluate_direction(d, dq, mkt, gas_usd, cp, sp, inv) for d in DIRECTIONS}
