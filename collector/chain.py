"""
Read-only Arbitrum access: pool resolution, block-pinned pool snapshots via
Multicall3, QuoterV2 quotes, gas prices and event logs.

There is no signing code anywhere in this module (or this project). All calls are
eth_call / eth_getLogs / eth_getBlockByNumber.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from eth_abi import decode, encode
from eth_utils import keccak, to_checksum_address
from web3 import AsyncHTTPProvider, AsyncWeb3

from amm.pool import PoolState
from config import log

logger = logging.getLogger("chain")

ARB_GAS_INFO = "0x000000000000000000000000000000000000006C"  # Arbitrum precompile

SWAP_TOPIC = "0x" + keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
MINT_TOPIC = "0x" + keccak(text="Mint(address,address,int24,int24,uint128,uint256,uint256)").hex()
BURN_TOPIC = "0x" + keccak(text="Burn(address,int24,int24,uint128,uint256,uint256)").hex()


def _sel(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def calldata(sig: str, types: list[str] | None = None, args: list | None = None) -> bytes:
    """ABI-encode a call from its canonical signature, e.g. calldata('ticks(int24)', ['int24'], [-100])."""
    return _sel(sig) + (encode(types, args) if types else b"")


TICK_OUT = ["uint128", "int128", "uint256", "uint256", "int56", "uint160", "uint32", "bool"]
SLOT0_OUT = ["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"]


@dataclass
class TokenInfo:
    address: str
    symbol: str
    decimals: int


@dataclass
class PoolMeta:
    address: str
    token0: TokenInfo
    token1: TokenInfo
    fee: int
    tick_spacing: int
    base_is_token0: bool  # True when WETH is token0 (the case for WETH/USDC on Arbitrum)


class Chain:
    def __init__(self, rpc_url: str, cfg):
        self.cfg = cfg
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
        self.multicall_addr = to_checksum_address(cfg.dex.multicall3)
        self.meta: PoolMeta | None = None
        self._last_tick: int | None = None

    # ------------------------------------------------------------------ #
    # Low-level helpers
    # ------------------------------------------------------------------ #
    async def call(self, to: str, data: bytes, block="latest") -> bytes:
        return bytes(await self.w3.eth.call({"to": to, "data": "0x" + data.hex()}, block_identifier=block))

    async def multicall(self, calls: list[tuple[str, bytes]], block="latest", chunk: int = 400) -> list[bytes | None]:
        """Multicall3.aggregate3 with allowFailure=true. All sub-calls see the same block."""
        out: list[bytes | None] = []
        for i in range(0, len(calls), chunk):
            part = calls[i : i + chunk]
            data = calldata(
                "aggregate3((address,bool,bytes)[])",
                ["(address,bool,bytes)[]"],
                [[(to_checksum_address(t), True, d) for t, d in part]],
            )
            raw = await self.call(self.multicall_addr, data, block)
            (results,) = decode(["(bool,bytes)[]"], raw)
            out.extend(r if ok else None for ok, r in results)
        return out

    async def block_number(self) -> int:
        return await self.w3.eth.block_number

    async def get_block(self, block="latest"):
        return await self.w3.eth.get_block(block)

    # ------------------------------------------------------------------ #
    # Startup: resolve and describe the pool
    # ------------------------------------------------------------------ #
    async def token_info(self, addr: str) -> TokenInfo:
        sym, dec = await self.multicall(
            [(addr, calldata("symbol()")), (addr, calldata("decimals()"))]
        )
        return TokenInfo(to_checksum_address(addr), decode(["string"], sym)[0], decode(["uint8"], dec)[0])

    async def resolve_pool(self) -> PoolMeta:
        d = self.cfg.dex
        chain_id = await self.w3.eth.chain_id
        if chain_id != d.chain_id:
            raise RuntimeError(f"RPC is on chain {chain_id}, config expects {d.chain_id}")
        raw = await self.call(
            to_checksum_address(d.factory),
            calldata("getPool(address,address,uint24)", ["address", "address", "uint24"],
                     [to_checksum_address(d.base_token), to_checksum_address(d.quote_token), d.fee_tier]),
        )
        pool = to_checksum_address(decode(["address"], raw)[0])
        if int(pool, 16) == 0:
            raise RuntimeError("factory.getPool returned the zero address: no such pool")
        t0, t1, fee, ts = await self.multicall(
            [(pool, calldata("token0()")), (pool, calldata("token1()")),
             (pool, calldata("fee()")), (pool, calldata("tickSpacing()"))]
        )
        t0 = decode(["address"], t0)[0]
        t1 = decode(["address"], t1)[0]
        tok0, tok1 = await self.token_info(t0), await self.token_info(t1)
        self.meta = PoolMeta(
            address=pool, token0=tok0, token1=tok1,
            fee=decode(["uint24"], fee)[0], tick_spacing=decode(["int24"], ts)[0],
            base_is_token0=tok0.address.lower() == d.base_token.lower(),
        )
        log(logger, "pool resolved via factory.getPool", pool=pool, token0=tok0.symbol, token1=tok1.symbol,
            fee=self.meta.fee, tick_spacing=self.meta.tick_spacing, chain_id=chain_id)
        return self.meta

    # ------------------------------------------------------------------ #
    # Pool snapshot
    # ------------------------------------------------------------------ #
    def _words_for(self, tick_lo: int, tick_hi: int) -> range:
        ts = self.meta.tick_spacing
        return range((tick_lo // ts) >> 8, ((tick_hi // ts) >> 8) + 1)

    async def load_pool_state(self, block: int, tick_window: int, with_gross: bool = False):
        """Snapshot slot0, liquidity, bitmap and initialized ticks within ±tick_window at `block`.

        Two round-trips in the common case: (slot0 + liquidity + bitmap words guessed from
        the previous tick), then (ticks). Returns PoolState, plus {tick: liquidityGross}
        when with_gross=True (the backfill needs it to replay Mint/Burn).
        """
        m = self.meta
        pool, ts = m.address, m.tick_spacing
        guess = self._last_tick
        words: range = range(0)
        if guess is not None:
            words = self._words_for(guess - tick_window - 2 * ts * 256, guess + tick_window + 2 * ts * 256)
        calls = [(pool, calldata("slot0()")), (pool, calldata("liquidity()"))]
        calls += [(pool, calldata("tickBitmap(int16)", ["int16"], [w])) for w in words]
        res = await self.multicall(calls, block)
        slot0 = decode(SLOT0_OUT, res[0])
        liquidity = decode(["uint128"], res[1])[0]
        sqrt_p, tick = slot0[0], slot0[1]
        self._last_tick = tick

        tick_lo = ((tick - tick_window) // ts) * ts
        tick_hi = -((-(tick + tick_window)) // ts) * ts
        need = self._words_for(tick_lo, tick_hi)
        bitmap = {w: decode(["uint256"], r)[0] for w, r in zip(words, res[2:]) if r is not None}
        missing = [w for w in need if w not in bitmap]
        if missing:
            r2 = await self.multicall([(pool, calldata("tickBitmap(int16)", ["int16"], [w])) for w in missing], block)
            bitmap.update({w: decode(["uint256"], r)[0] for w, r in zip(missing, r2)})
        bitmap = {w: bitmap[w] for w in need}

        init_ticks = []
        for w, word in bitmap.items():
            while word:
                lsb = (word & -word).bit_length() - 1
                t = ((w << 8) + lsb) * ts
                if tick_lo <= t <= tick_hi:
                    init_ticks.append(t)
                word &= word - 1
        tick_res = await self.multicall(
            [(pool, calldata("ticks(int24)", ["int24"], [t])) for t in init_ticks], block
        )
        liq_net, liq_gross = {}, {}
        for t, r in zip(init_ticks, tick_res):
            vals = decode(TICK_OUT, r)
            liq_gross[t], liq_net[t] = vals[0], vals[1]

        state = PoolState(sqrt_p, tick, liquidity, m.fee, ts, bitmap, liq_net, tick_lo, tick_hi, block)
        return (state, liq_gross) if with_gross else state

    # ------------------------------------------------------------------ #
    # QuoterV2 (on-chain reference quotes, used for parity checks)
    # ------------------------------------------------------------------ #
    async def quoter_exact_input(self, token_in: str, token_out: str, amount_in: int, block="latest") -> tuple[int, int]:
        data = calldata(
            "quoteExactInputSingle((address,address,uint256,uint24,uint160))",
            ["(address,address,uint256,uint24,uint160)"],
            [(to_checksum_address(token_in), to_checksum_address(token_out), amount_in, self.meta.fee, 0)],
        )
        out = decode(["uint256", "uint160", "uint32", "uint256"], await self.call(self.cfg.dex.quoter_v2, data, block))
        return out[0], out[1]  # amountOut, sqrtPriceX96After

    async def quoter_exact_output(self, token_in: str, token_out: str, amount_out: int, block="latest") -> tuple[int, int]:
        data = calldata(
            "quoteExactOutputSingle((address,address,uint256,uint24,uint160))",
            ["(address,address,uint256,uint24,uint160)"],
            [(to_checksum_address(token_in), to_checksum_address(token_out), amount_out, self.meta.fee, 0)],
        )
        out = decode(["uint256", "uint160", "uint32", "uint256"], await self.call(self.cfg.dex.quoter_v2, data, block))
        return out[0], out[1]  # amountIn, sqrtPriceX96After

    # ------------------------------------------------------------------ #
    # Gas
    # ------------------------------------------------------------------ #
    async def l1_price_per_byte_wei(self, block="latest") -> int:
        """ArbGasInfo.getPricesInWei()[1]: wei per byte of L1 calldata for the next batch."""
        raw = await self.call(ARB_GAS_INFO, calldata("getPricesInWei()"), block)
        return decode(["uint256"] * 6, raw)[1]

    # ------------------------------------------------------------------ #
    # Logs
    # ------------------------------------------------------------------ #
    async def get_logs(self, from_block: int, to_block: int, topics0: list[str]):
        return await self.w3.eth.get_logs(
            {"address": self.meta.address, "fromBlock": from_block, "toBlock": to_block, "topics": [topics0]}
        )


def decode_swap_log(lg) -> dict:
    amount0, amount1, sqrt_p, liq, tick = decode(["int256", "int256", "uint160", "uint128", "int24"], bytes(lg["data"]))
    return dict(
        block=lg["blockNumber"], log_index=lg["logIndex"], tx_hash="0x" + bytes(lg["transactionHash"]).hex(),
        amount0=amount0, amount1=amount1, sqrt_price_x96=sqrt_p, liquidity=liq, tick=tick,
    )


def decode_mint_burn_log(lg) -> dict:
    topic0 = "0x" + bytes(lg["topics"][0]).hex()
    tick_lower = decode(["int24"], bytes(lg["topics"][2]))[0]
    tick_upper = decode(["int24"], bytes(lg["topics"][3]))[0]
    if topic0 == MINT_TOPIC:
        _sender, amount, _a0, _a1 = decode(["address", "uint128", "uint256", "uint256"], bytes(lg["data"]))
        kind = "mint"
    else:
        amount, _a0, _a1 = decode(["uint128", "uint256", "uint256"], bytes(lg["data"]))
        kind = "burn"
    return dict(kind=kind, block=lg["blockNumber"], log_index=lg["logIndex"],
                tick_lower=tick_lower, tick_upper=tick_upper, amount=amount)
