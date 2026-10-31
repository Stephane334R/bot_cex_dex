"""
Snapshot the live pool + QuoterV2 answers at one block into tests/fixtures/,
so the parity test runs offline and deterministically.

    python -m scripts.make_quoter_fixture
"""

from __future__ import annotations

import asyncio
import json

from collector.chain import Chain
from config import ROOT, load_config, setup_logging

# (direction, mode, human amount). "b2q" = base->quote (WETH->USDC).
CASES = [
    ("b2q", "in", 0.01), ("b2q", "in", 1), ("b2q", "in", 25), ("b2q", "in", 150), ("b2q", "in", 600),
    ("q2b", "in", 50), ("q2b", "in", 20_000), ("q2b", "in", 400_000), ("q2b", "in", 1_500_000),
    ("q2b", "out", 0.5), ("q2b", "out", 40), ("q2b", "out", 400),
    ("b2q", "out", 1_000), ("b2q", "out", 250_000), ("b2q", "out", 1_200_000),
]


async def main() -> None:
    cfg = load_config()
    setup_logging("INFO")
    chain = Chain(cfg.rpc_url, cfg)
    meta = await chain.resolve_pool()
    block = await chain.block_number() - 2
    state = await chain.load_pool_state(block, tick_window=1500)
    base, quote = cfg.dex.base_token, cfg.dex.quote_token
    dec = {base.lower(): 18, quote.lower(): 6}
    cases = []
    for direction, mode, amt in CASES:
        t_in, t_out = (base, quote) if direction == "b2q" else (quote, base)
        raw = int(amt * 10 ** dec[(t_in if mode == "in" else t_out).lower()])
        if mode == "in":
            q, sp = await chain.quoter_exact_input(t_in, t_out, raw, block)
        else:
            q, sp = await chain.quoter_exact_output(t_in, t_out, raw, block)
        cases.append(dict(direction=direction, mode=mode, amount=str(raw), quoter=str(q), sqrt_after=str(sp)))
        print(direction, mode, amt, "->", q)
    fixture = dict(
        block=block, pool=meta.address, base_is_token0=meta.base_is_token0,
        sqrt_price_x96=str(state.sqrt_price_x96), tick=state.tick, liquidity=str(state.liquidity),
        fee=state.fee, tick_spacing=state.tick_spacing, tick_lo=state.tick_lo, tick_hi=state.tick_hi,
        bitmap={str(k): hex(v) for k, v in state.bitmap.items()},
        liquidity_net={str(k): str(v) for k, v in state.liquidity_net.items()},
        cases=cases,
    )
    out = ROOT / "tests" / "fixtures" / "quoter_parity.json"
    out.write_text(json.dumps(fixture, indent=1))
    print("wrote", out, "ticks", len(state.liquidity_net))


if __name__ == "__main__":
    asyncio.run(main())
