"""Event replay must reconstruct historical pool state exactly (offline, synthetic pool)."""

from amm.pool import PoolState, build_bitmap
from amm.v3_math import get_sqrt_ratio_at_tick
from collector.backfill import Replayer


def _pool_from_positions(positions, tick, lo=-3000, hi=3000):
    net, gross = {}, {}
    for tl, tu, L in positions:
        net[tl] = net.get(tl, 0) + L
        net[tu] = net.get(tu, 0) - L
        gross[tl] = gross.get(tl, 0) + L
        gross[tu] = gross.get(tu, 0) + L
    active = sum(L for tl, tu, L in positions if tl <= tick < tu)
    init = [t for t, g in gross.items() if g > 0]
    return PoolState(get_sqrt_ratio_at_tick(tick), tick, active, 500, 10, build_bitmap(init, 10),
                     {t: net[t] for t in init}, lo, hi), gross


def test_replay_undoes_mints_burns_and_tracks_swaps():
    base = [(-2000, 2000, 10**18)]
    # History, oldest first:
    #  block 10: swap moves price to tick 5 (liquidity 1e18)
    #  block 20: mint (-100, 100, 3e18)  -> in range, L = 4e18
    #  block 30: burn (-500, 500, 0)     -> fee poke, no-op
    #  block 40: swap to tick 150 (now outside the minted range, L = 1e18)
    #  block 50: mint (1000, 1500, 2e18) -> out of range
    sp5, sp150 = get_sqrt_ratio_at_tick(5) + 7, get_sqrt_ratio_at_tick(150) + 3
    events = [
        dict(kind="swap", block=10, log_index=0, sqrt_price_x96=sp5, tick=5, liquidity=10**18),
        dict(kind="mint", block=20, log_index=0, tick_lower=-100, tick_upper=100, amount=3 * 10**18),
        dict(kind="burn", block=30, log_index=0, tick_lower=-500, tick_upper=500, amount=0),
        dict(kind="swap", block=40, log_index=0, sqrt_price_x96=sp150, tick=150, liquidity=10**18),
        dict(kind="mint", block=50, log_index=0, tick_lower=1000, tick_upper=1500, amount=2 * 10**18),
    ]
    head, gross = _pool_from_positions(base + [(-100, 100, 3 * 10**18), (1000, 1500, 2 * 10**18)], 150)
    head.sqrt_price_x96 = sp150
    rep = Replayer(head, gross, events)

    s = rep.state_at(60, 3000)
    assert (s.sqrt_price_x96, s.tick, s.liquidity) == (sp150, 150, 10**18)
    assert s.liquidity_net[1000] == 2 * 10**18

    s = rep.state_at(45, 3000)  # before the out-of-range mint
    assert 1000 not in s.liquidity_net and 1500 not in s.liquidity_net
    assert s.liquidity_net[-100] == 3 * 10**18

    s = rep.state_at(25, 3000)  # after the in-range mint, before the second swap
    assert (s.sqrt_price_x96, s.tick, s.liquidity) == (sp5, 5, 4 * 10**18)

    s = rep.state_at(15, 3000)  # before the first mint: only the base position
    assert s.liquidity == 10**18
    assert set(s.liquidity_net) == {-2000, 2000}
    # A swap on the replayed state behaves like a pool with only the base position.
    ref, _ = _pool_from_positions(base, 5)
    ref.sqrt_price_x96 = sp5
    assert s.swap(True, 10**17).amount1 == ref.swap(True, 10**17).amount1

    assert rep.state_at(5, 3000) is None  # no swap known before block 10
