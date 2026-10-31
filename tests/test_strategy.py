"""Cost model, optimal sizing, inventory and competition-check tests (offline)."""

import math

import pytest

from amm.pool import PoolState, build_bitmap
from amm.v3_math import get_sqrt_ratio_at_tick
from strategy import competition
from strategy.costs import DEX_BUY, DEX_SELL, CostParams, DexQuoter, gas_cost_usd, pnl
from strategy.engine import Market, StrategyParams, evaluate_direction
from strategy.inventory import Inventory

# A WETH(token0)/USDC(token1) pool around $2000: tick ≈ log_1.0001(2000e-12)
TICK_2000 = round(math.log(2000e-12) / math.log(1.0001) / 10) * 10


def make_pool(liq=4 * 10**17, width=3000):
    lo, hi = TICK_2000 - width, TICK_2000 + width
    net = {lo: liq, hi: -liq}
    return PoolState(get_sqrt_ratio_at_tick(TICK_2000), TICK_2000, liq, 500, 10,
                     build_bitmap(net, 10), net, lo, hi)


# ----------------------------------------------------------------------------- cost model
def test_pnl_breakdown_is_additive():
    cp = CostParams(cex_fee_bps=10, haircut_bps=2)
    r = pnl(DEX_BUY, 1.0, dex_quote_usd=1990.0, dex_fee_usd=1.0, bid=2000.0, ask=2000.1, gas_usd=0.05, p=cp)
    assert r.cex_fee_usd == pytest.approx(2.0)
    assert r.haircut_usd == pytest.approx(0.4)
    assert r.net_usd == pytest.approx(2000 - 2.0 - 1990 - 0.05 - 0.4)
    assert r.gross_usd == pytest.approx(r.net_usd + 1.0 + 2.0 + 0.05 + 0.4)
    assert r.net_bps == pytest.approx(r.net_usd / 2000 * 1e4)


def test_dex_sell_uses_ask():
    r = pnl(DEX_SELL, 2.0, 4010.0, 2.0, bid=1999.9, ask=2000.0, gas_usd=0.0, p=CostParams(0, 0))
    assert r.notional_usd == 4000.0 and r.net_usd == pytest.approx(10.0)


def test_gas_cost_includes_l1_component():
    cp = CostParams(swap_gas_units=100_000, l1_calldata_bytes=200, gas_price_multiplier=2.0)
    usd = gas_cost_usd(cp, l2_base_fee_wei=10**7, l1_price_per_byte_wei=10**9, eth_usd=2000)
    assert usd == pytest.approx((100_000 * 10**7 * 2 + 200 * 10**9) / 1e18 * 2000)


# ----------------------------------------------------------------------------- sizing
def _brute_force_best(direction, dq, mkt, gas, cp, cap):
    best = -1e18
    for i in range(1, 4001):
        q = cap * i / 4000
        lg = dq.leg(direction, q)
        best = max(best, pnl(direction, q, lg.quote_amount, lg.fee_usd, mkt.bid, mkt.ask, gas, cp).net_usd)
    return best


@pytest.mark.parametrize("direction,bid,ask", [(DEX_BUY, 2010.0, 2010.2), (DEX_SELL, 1989.8, 1990.0)])
def test_optimal_size_matches_brute_force(direction, bid, ask):
    dq = DexQuoter(make_pool(), base_is_token0=True)
    assert dq.mid == pytest.approx(2000, rel=0.01)
    mkt = Market(bid, ask, 1e9, 1e9)
    cp = CostParams(cex_fee_bps=1, haircut_bps=0.5)
    sp = StrategyParams(max_notional_usd=5e6, threshold_usd=1, assume_deep_liquidity=True)
    ev = evaluate_direction(direction, dq, mkt, 0.5, cp, sp, None)
    assert ev.taken and ev.best.size > sp.min_size
    assert ev.best.size < ev.cap  # interior optimum: price impact eventually eats the edge
    brute = _brute_force_best(direction, dq, mkt, 0.5, cp, ev.cap)
    assert ev.best.net_usd >= brute - 1e-6 * abs(brute)
    assert len(ev.curve) == sp.grid_points


def test_no_edge_no_trade():
    dq = DexQuoter(make_pool(), True)
    mid = dq.mid
    mkt = Market(mid * 0.9999, mid * 1.0001, 100, 100)
    ev = evaluate_direction(DEX_BUY, dq, mkt, 0.05, CostParams(), StrategyParams(), None)
    assert not ev.taken and ev.best.net_usd < 0


def test_size_capped_by_top_of_book_unless_deep():
    dq = DexQuoter(make_pool(), True)
    mkt = Market(2050.0, 2050.1, 0.7, 0.7)
    sp = StrategyParams(max_notional_usd=1e7)
    ev = evaluate_direction(DEX_BUY, dq, mkt, 0.0, CostParams(1, 0), sp, None)
    assert ev.cap_reason == "cex_top_of_book" and ev.best.size <= 0.7 + 1e-9
    sp_deep = StrategyParams(max_notional_usd=1e7, assume_deep_liquidity=True)
    ev2 = evaluate_direction(DEX_BUY, dq, mkt, 0.0, CostParams(1, 0), sp_deep, None)
    assert ev2.best.size > 0.7


def test_max_notional_cap():
    dq = DexQuoter(make_pool(liq=10**20), True)  # very deep pool: edge never dies within the cap
    mkt = Market(2050.0, 2050.1, 1e9, 1e9)
    sp = StrategyParams(max_notional_usd=20_000, assume_deep_liquidity=True)
    ev = evaluate_direction(DEX_BUY, dq, mkt, 0.0, CostParams(1, 0), sp, None)
    assert ev.cap_reason == "max_notional"
    assert ev.best.size == pytest.approx(20_000 / 2050.0, rel=1e-4)


# ----------------------------------------------------------------------------- inventory
def test_inventory_blocks_direction_when_drained():
    inv = Inventory(cex_base=1.0, cex_quote=1e6, dex_base=10.0, dex_quote=1e6)
    dq = DexQuoter(make_pool(), True)
    mkt = Market(2050.0, 2050.1, 1e9, 1e9)
    sp = StrategyParams(max_notional_usd=1e7)
    ev = evaluate_direction(DEX_BUY, dq, mkt, 0.0, CostParams(1, 0), sp, inv)
    assert ev.cap_reason == "inventory_base" and ev.best.size <= 1.0
    inv.apply(DEX_BUY, 1.0, 2000.0, 2049.0)
    assert inv.cex_base == 0.0 and inv.dex_base == 11.0
    ev2 = evaluate_direction(DEX_BUY, dq, mkt, 0.0, CostParams(1, 0), sp, inv)
    assert ev2.best is None and not ev2.taken
    assert inv.capital_used_usd(2000) == pytest.approx(1.0 * 2000 + 2000.0)


def test_inventory_quote_cap():
    inv = Inventory(cex_base=100, cex_quote=1e6, dex_base=100, dex_quote=500.0)
    dq = DexQuoter(make_pool(), True)
    ev = evaluate_direction(DEX_BUY, dq, Market(2050, 2050.1, 1e9, 1e9), 0.0, CostParams(1, 0),
                            StrategyParams(max_notional_usd=1e7), inv)
    assert ev.cap_reason == "inventory_quote"
    assert ev.best.dex_quote_usd <= 500.0 + 1e-6


# ----------------------------------------------------------------------------- competition
def test_competition_detects_same_direction_close():
    before = get_sqrt_ratio_at_tick(TICK_2000)
    after = get_sqrt_ratio_at_tick(TICK_2000 + 20)  # our trade would push price up 20 ticks
    swaps = [
        dict(block=101, amount0=10**18, amount1=-2000 * 10**6, sqrt_price_x96=before - 10**20),  # opposite dir
        dict(block=102, amount0=-(10**18), amount1=2000 * 10**6, sqrt_price_x96=get_sqrt_ratio_at_tick(TICK_2000 + 10)),
        dict(block=104, amount0=-(3 * 10**18), amount1=6000 * 10**6, sqrt_price_x96=get_sqrt_ratio_at_tick(TICK_2000 + 25)),
    ]
    r = competition.check(DEX_BUY, before, after, swaps, True, 100, {104: 1001.0}, 1000.0)
    assert r["closed"] == 1 and r["after_blocks"] == 4 and r["after_s"] == 1.0
    assert r["swaps"] == 2 and r["volume_base"] == pytest.approx(4.0)


def test_competition_partial():
    before = get_sqrt_ratio_at_tick(TICK_2000)
    after = get_sqrt_ratio_at_tick(TICK_2000 - 20)  # dex_sell pushes price down
    swaps = [dict(block=101, amount0=10**18, amount1=-1, sqrt_price_x96=get_sqrt_ratio_at_tick(TICK_2000 - 10))]
    r = competition.check(DEX_SELL, before, after, swaps, True, 100)
    assert r["closed"] == 0 and r["frac"] == pytest.approx(0.5, abs=0.01)
