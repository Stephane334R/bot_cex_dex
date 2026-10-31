"""Unit tests for the Uniswap v3 integer math and the swap loop (offline)."""

import math

import pytest

from amm.pool import OutOfLoadedRange, PoolState, build_bitmap
from amm.v3_math import (
    MAX_SQRT_RATIO,
    MAX_TICK,
    MIN_SQRT_RATIO,
    MIN_TICK,
    Q96,
    compute_swap_step,
    get_amount0_delta,
    get_amount1_delta,
    get_sqrt_ratio_at_tick,
    get_tick_at_sqrt_ratio,
)


# ----------------------------------------------------------------------------- TickMath
def test_tick_math_bounds_match_solidity_constants():
    assert get_sqrt_ratio_at_tick(MIN_TICK) == MIN_SQRT_RATIO
    assert get_sqrt_ratio_at_tick(MAX_TICK) == MAX_SQRT_RATIO
    assert get_sqrt_ratio_at_tick(0) == Q96


@pytest.mark.parametrize("tick", [1, -1, 10, -10, 887, 50_000, -50_000, -199_170, 199_170, 400_000, -800_000])
def test_tick_math_matches_float(tick):
    # Exercises every magic constant across these ticks; float is accurate to ~1e-12 relative.
    exact = get_sqrt_ratio_at_tick(tick) / Q96
    assert exact == pytest.approx(1.0001 ** (tick / 2), rel=1e-11)


def test_tick_math_known_values_from_v3_core_tests():
    # Values from Uniswap/v3-core test/__snapshots__/TickMath.spec.ts.snap
    assert get_sqrt_ratio_at_tick(50) == 79426470787362580746886972461
    assert get_sqrt_ratio_at_tick(-50) == 79030349367926598376800521322
    assert get_sqrt_ratio_at_tick(MIN_TICK + 1) == 4295343490


@pytest.mark.parametrize("tick", [MIN_TICK, -199_169, -1, 0, 1, 12345, MAX_TICK - 1])
def test_tick_at_sqrt_ratio_inverse(tick):
    sp = get_sqrt_ratio_at_tick(tick)
    assert get_tick_at_sqrt_ratio(sp) == tick
    assert get_tick_at_sqrt_ratio(sp + 1) == tick
    if tick > MIN_TICK:
        assert get_tick_at_sqrt_ratio(sp - 1) == tick - 1


# ----------------------------------------------------------------------------- deltas
def test_amount_deltas_rounding_direction():
    a, b, L = get_sqrt_ratio_at_tick(-100), get_sqrt_ratio_at_tick(100), 10**18
    assert get_amount0_delta(a, b, L, True) - get_amount0_delta(a, b, L, False) in (0, 1)
    assert get_amount1_delta(a, b, L, True) - get_amount1_delta(a, b, L, False) in (0, 1)
    # Closed form: amount1 = L*(sqrtB - sqrtA)
    assert get_amount1_delta(a, b, L, False) == L * (b - a) // Q96


# ----------------------------------------------------------------------------- swap step
def test_swap_step_exact_in_capped_by_target_charges_fee_on_top():
    price = Q96
    target = get_sqrt_ratio_at_tick(10)
    L = 2 * 10**18
    sp_next, amt_in, amt_out, fee = compute_swap_step(price, target, L, 10**30, 500)
    assert sp_next == target
    assert amt_in == get_amount1_delta(price, target, L, True)
    # fee = ceil(amountIn * fee / (1e6 - fee))
    assert fee == -(-amt_in * 500 // (1_000_000 - 500))


def test_swap_step_exact_out_does_not_exceed_request():
    _, _, amt_out, _ = compute_swap_step(Q96, get_sqrt_ratio_at_tick(-1000), 10**18, -(10**15), 3000)
    assert amt_out == 10**15


# ----------------------------------------------------------------------------- swap loop
def _pool(positions, tick=0, fee=500, spacing=10, lo=-5000, hi=5000):
    """Build a pool from LP positions [(tickLower, tickUpper, L)]."""
    net: dict[int, int] = {}
    for tl, tu, L in positions:
        net[tl] = net.get(tl, 0) + L
        net[tu] = net.get(tu, 0) - L
    active = sum(L for tl, tu, L in positions if tl <= tick < tu)
    return PoolState(get_sqrt_ratio_at_tick(tick), tick, active, fee, spacing, build_bitmap(net, spacing), net, lo, hi)


def test_single_range_matches_closed_form():
    L = 10**20
    pool = _pool([(-1000, 1000, L)])
    amt_in = 10**18  # token1 in (price goes up)
    r = pool.swap(False, amt_in)
    in_less_fee = amt_in * (1_000_000 - 500) // 1_000_000
    sqrt_new = Q96 + (in_less_fee << 96) // L
    expected_out = get_amount0_delta(Q96, sqrt_new, L, False)
    assert r.sqrt_price_after == sqrt_new
    assert -r.amount0 == expected_out
    assert r.amount1 == amt_in
    assert r.ticks_crossed == 0


def test_tick_crossing_updates_liquidity_and_equals_piecewise_sum():
    L1, L2 = 10**18, 5 * 10**18
    # Wide position plus a second one that only becomes active above tick 100.
    pool = _pool([(-2000, 2000, L1), (100, 2000, L2)])
    r = pool.swap(False, 10**17)  # big enough to cross tick 100 going up
    assert r.tick_after >= 100 and r.ticks_crossed == 1
    assert r.liquidity_after == L1 + L2

    # Piecewise check: amount to reach tick 100 at L1, the rest at L1+L2.
    s100 = get_sqrt_ratio_at_tick(100)
    in_to_100 = get_amount1_delta(Q96, s100, L1, True)
    fee_to_100 = -(-in_to_100 * 500 // (1_000_000 - 500))
    remaining = 10**17 - in_to_100 - fee_to_100
    rem_less_fee = remaining * (1_000_000 - 500) // 1_000_000
    s_end = s100 + (rem_less_fee << 96) // (L1 + L2)
    out = get_amount0_delta(Q96, s100, L1, False) + get_amount0_delta(s100, s_end, L1 + L2, False)
    assert r.sqrt_price_after == s_end
    assert -r.amount0 == out


def test_crossing_down_subtracts_liquidity_net():
    pool = _pool([(-2000, 2000, 10**18), (-2000, -100, 4 * 10**18)])
    assert pool.liquidity == 10**18
    r = pool.swap(True, 10**17)  # token0 in, price down
    assert r.tick_after < -100
    assert r.liquidity_after == 5 * 10**18


def test_exact_in_and_exact_out_are_consistent():
    pool = _pool([(-3000, 3000, 10**19), (-500, 700, 3 * 10**19)])
    for zfo in (True, False):
        r_in = pool.swap(zfo, 10**17)
        r_out = pool.swap(zfo, -r_in.amount_out())
        # Asking for exactly that output must cost at most the same input (rounding <= a few wei).
        assert 0 <= r_in.amount_in() - r_out.amount_in() <= 2
        assert r_out.amount_out() == r_in.amount_out()


def test_out_of_loaded_range_raises():
    pool = _pool([(-200, 200, 10**18)], lo=-200, hi=200)
    with pytest.raises(OutOfLoadedRange):
        pool.swap(False, 10**30)
    # Sweeping exactly to the window edge is allowed.
    r = pool.max_swap_within_window(False)
    assert r.sqrt_price_after == get_sqrt_ratio_at_tick(200)


def test_fee_is_charged_once_per_unit_of_input():
    pool = _pool([(-5000, 5000, 10**22)])
    r = pool.swap(True, 10**18)
    assert r.fee_paid == pytest.approx(10**18 * 0.0005, rel=1e-6)


def test_price_helper():
    pool = _pool([(-5000, 5000, 10**18)], tick=-199_170, lo=-205_000, hi=-195_000)
    assert math.isclose(pool.price(18, 6), 1.0001 ** -199_170 * 1e12, rel_tol=1e-9)
