"""
QuoterV2 parity: the local integer simulator must reproduce Uniswap's on-chain
QuoterV2 to the wei.

* test_parity_fixture: offline, against a recorded snapshot (scripts/make_quoter_fixture.py).
* test_parity_live:    hits the RPC at a fresh block. Run with `pytest -m network`.
"""

import asyncio
import json
from pathlib import Path

import pytest

from amm.pool import PoolState

FIXTURE = Path(__file__).parent / "fixtures" / "quoter_parity.json"


def _state_from_fixture(fx) -> PoolState:
    return PoolState(
        sqrt_price_x96=int(fx["sqrt_price_x96"]), tick=fx["tick"], liquidity=int(fx["liquidity"]),
        fee=fx["fee"], tick_spacing=fx["tick_spacing"],
        bitmap={int(k): int(v, 16) for k, v in fx["bitmap"].items()},
        liquidity_net={int(k): int(v) for k, v in fx["liquidity_net"].items()},
        tick_lo=fx["tick_lo"], tick_hi=fx["tick_hi"], block=fx["block"],
    )


def local_quote(state: PoolState, base_is_token0: bool, direction: str, mode: str, amount: int) -> tuple[int, int]:
    """Return (quoted amount, sqrtPriceAfter) in the same shape QuoterV2 returns."""
    # b2q: base (token0 when base_is_token0) -> quote
    zero_for_one = (direction == "b2q") == base_is_token0
    r = state.swap(zero_for_one, amount if mode == "in" else -amount)
    return (r.amount_out() if mode == "in" else r.amount_in()), r.sqrt_price_after


@pytest.fixture(scope="module")
def fx():
    if not FIXTURE.exists():
        pytest.skip("run `python -m scripts.make_quoter_fixture` first")
    return json.loads(FIXTURE.read_text())


def test_parity_fixture(fx):
    state = _state_from_fixture(fx)
    for c in fx["cases"]:
        q, sp = local_quote(state, fx["base_is_token0"], c["direction"], c["mode"], int(c["amount"]))
        assert q == int(c["quoter"]), c
        assert sp == int(c["sqrt_after"]), c


def test_fixture_contains_tick_crossing_cases(fx):
    state = _state_from_fixture(fx)
    crossed = [state.swap(True, int(c["amount"])).ticks_crossed
               for c in fx["cases"] if c["direction"] == "b2q" and c["mode"] == "in"]
    assert max(crossed) >= 10  # the big sizes really exercise tick crossing


@pytest.mark.network
def test_parity_live():
    from collector.chain import Chain
    from config import load_config

    async def run():
        cfg = load_config()
        chain = Chain(cfg.rpc_url, cfg)
        meta = await chain.resolve_pool()
        block = await chain.block_number() - 2
        state = await chain.load_pool_state(block, 1500)
        b, q = cfg.dex.base_token, cfg.dex.quote_token
        for amt in (10**16, 10**18, 50 * 10**18, 300 * 10**18):
            on, sp_on = await chain.quoter_exact_input(b, q, amt, block)
            loc, sp_loc = local_quote(state, meta.base_is_token0, "b2q", "in", amt)
            assert (on, sp_on) == (loc, sp_loc)
            on, _ = await chain.quoter_exact_output(q, b, amt, block)
            loc, _ = local_quote(state, meta.base_is_token0, "q2b", "out", amt)
            assert on == loc

    asyncio.run(run())
