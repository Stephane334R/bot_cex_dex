"""
Local Uniswap v3 pool simulator: the `swap()` loop from UniswapV3Pool.sol,
including tick crossing, run against a snapshot of pool state.

The snapshot only holds the tick data in a *loaded window* around the current
price (fetching every tick on a dense pool every block would be wasteful). If a
swap would need tick data outside that window, the simulator raises
`OutOfLoadedRange` instead of returning a silently wrong quote.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .v3_math import (
    MAX_SQRT_RATIO,
    MAX_TICK,
    MIN_SQRT_RATIO,
    MIN_TICK,
    compute_swap_step,
    get_sqrt_ratio_at_tick,
    get_tick_at_sqrt_ratio,
)


class OutOfLoadedRange(Exception):
    """The swap walked past the tick window we have data for."""


@dataclass
class SwapResult:
    amount0: int  # pool's token0 delta: >0 means the pool received token0
    amount1: int  # pool's token1 delta
    fee_paid: int  # in the input token, raw units (LP + protocol fee)
    sqrt_price_after: int
    tick_after: int
    liquidity_after: int
    ticks_crossed: int  # initialized ticks crossed (QuoterV2 reports the same notion)

    def amount_in(self) -> int:
        return self.amount0 if self.amount0 > 0 else self.amount1

    def amount_out(self) -> int:
        return -self.amount1 if self.amount0 > 0 else -self.amount0


@dataclass
class PoolState:
    """Snapshot of a v3 pool at one block.

    `bitmap` maps word position -> 256-bit word, exactly like the on-chain
    `tickBitmap`. `liquidity_net` maps initialized tick -> liquidityNet.
    Tick data is trusted only for ticks in [tick_lo, tick_hi].
    """

    sqrt_price_x96: int
    tick: int
    liquidity: int
    fee: int  # pips, e.g. 500 = 0.05%
    tick_spacing: int
    bitmap: dict[int, int] = field(default_factory=dict)
    liquidity_net: dict[int, int] = field(default_factory=dict)
    tick_lo: int = MIN_TICK
    tick_hi: int = MAX_TICK
    block: int | None = None

    # ------------------------------------------------------------------ #
    # TickBitmap.nextInitializedTickWithinOneWord, with a range guard
    # ------------------------------------------------------------------ #
    def next_initialized_tick_within_one_word(self, tick: int, lte: bool) -> tuple[int, bool, bool]:
        """Returns (next_tick, initialized, clamped_to_window).

        If the true answer lies outside the loaded window we return the window edge
        instead (clamped=True). That is exact as long as the swap stops before the
        edge; the swap loop raises if it would have to cross it.
        """
        ts = self.tick_spacing
        compressed = tick // ts  # Python floors toward -inf, matching the Solidity adjustment
        if lte:
            word_pos, bit_pos = compressed >> 8, compressed & 0xFF
            mask = (1 << bit_pos) - 1 + (1 << bit_pos)
            masked = self.bitmap.get(word_pos, 0) & mask
            initialized = masked != 0
            if initialized:
                nxt = (compressed - (bit_pos - (masked.bit_length() - 1))) * ts
            else:
                nxt = (compressed - bit_pos) * ts
            # Every tick in [nxt, tick] must be inside the window for the answer to be trustworthy.
            if nxt < self.tick_lo:
                return self.tick_lo, False, True
        else:
            compressed += 1
            word_pos, bit_pos = compressed >> 8, compressed & 0xFF
            mask = ~((1 << bit_pos) - 1) & ((1 << 256) - 1)
            masked = self.bitmap.get(word_pos, 0) & mask
            initialized = masked != 0
            if initialized:
                lsb = (masked & -masked).bit_length() - 1
                nxt = (compressed + (lsb - bit_pos)) * ts
            else:
                nxt = (compressed + (255 - bit_pos)) * ts
            if nxt > self.tick_hi:
                return self.tick_hi, False, True
        return nxt, initialized, False

    # ------------------------------------------------------------------ #
    # UniswapV3Pool.swap (state is not mutated: this is a quote)
    # ------------------------------------------------------------------ #
    def swap(self, zero_for_one: bool, amount_specified: int, sqrt_price_limit_x96: int | None = None) -> SwapResult:
        """Simulate a swap. amount_specified > 0: exact input; < 0: exact output."""
        if amount_specified == 0:
            raise ValueError("amount_specified must be non-zero")
        if sqrt_price_limit_x96 is None:
            sqrt_price_limit_x96 = MIN_SQRT_RATIO + 1 if zero_for_one else MAX_SQRT_RATIO - 1
        if zero_for_one:
            assert MIN_SQRT_RATIO < sqrt_price_limit_x96 < self.sqrt_price_x96, "bad price limit"
        else:
            assert self.sqrt_price_x96 < sqrt_price_limit_x96 < MAX_SQRT_RATIO, "bad price limit"

        exact_input = amount_specified > 0
        remaining = amount_specified
        calculated = 0
        sqrt_price = self.sqrt_price_x96
        tick = self.tick
        liquidity = self.liquidity
        fee_paid = 0
        crossed = 0

        while remaining != 0 and sqrt_price != sqrt_price_limit_x96:
            sqrt_start = sqrt_price
            tick_next, initialized, clamped = self.next_initialized_tick_within_one_word(tick, zero_for_one)
            tick_next = max(MIN_TICK, min(MAX_TICK, tick_next))
            sqrt_next = get_sqrt_ratio_at_tick(tick_next)

            if zero_for_one:
                target = sqrt_price_limit_x96 if sqrt_next < sqrt_price_limit_x96 else sqrt_next
            else:
                target = sqrt_price_limit_x96 if sqrt_next > sqrt_price_limit_x96 else sqrt_next

            sqrt_price, amt_in, amt_out, fee_amt = compute_swap_step(
                sqrt_price, target, liquidity, remaining, self.fee
            )
            fee_paid += fee_amt
            if exact_input:
                remaining -= amt_in + fee_amt
                calculated -= amt_out
            else:
                remaining += amt_out
                calculated += amt_in + fee_amt

            if clamped and sqrt_price == sqrt_next and remaining != 0 and sqrt_price != sqrt_price_limit_x96:
                raise OutOfLoadedRange(f"swap needs tick data beyond [{self.tick_lo}, {self.tick_hi}]")

            if sqrt_price == sqrt_next:
                # Crossed into the next tick range: update active liquidity.
                if initialized:
                    net = self.liquidity_net.get(tick_next, 0)
                    if zero_for_one:
                        net = -net
                    liquidity += net
                    if liquidity < 0:
                        raise ArithmeticError("negative liquidity: inconsistent tick data")
                    crossed += 1
                tick = tick_next - 1 if zero_for_one else tick_next
            elif sqrt_price != sqrt_start:
                tick = get_tick_at_sqrt_ratio(sqrt_price)

        if zero_for_one == exact_input:
            amount0, amount1 = amount_specified - remaining, calculated
        else:
            amount0, amount1 = calculated, amount_specified - remaining

        return SwapResult(amount0, amount1, fee_paid, sqrt_price, tick, liquidity, crossed)

    # ------------------------------------------------------------------ #
    # Convenience helpers
    # ------------------------------------------------------------------ #
    def window_limit(self, zero_for_one: bool) -> int:
        """sqrt price at the edge of the loaded window in the swap direction."""
        edge = self.tick_lo if zero_for_one else self.tick_hi
        edge = max(MIN_TICK + 1, min(MAX_TICK - 1, edge))
        return get_sqrt_ratio_at_tick(edge)

    def max_swap_within_window(self, zero_for_one: bool) -> SwapResult:
        """Largest swap the loaded window can price: sweep to the window edge."""
        limit = self.window_limit(zero_for_one)
        huge = 1 << 200
        if (zero_for_one and limit >= self.sqrt_price_x96) or (not zero_for_one and limit <= self.sqrt_price_x96):
            return SwapResult(0, 0, 0, self.sqrt_price_x96, self.tick, self.liquidity, 0)
        return self.swap(zero_for_one, huge, limit)

    def price(self, decimals0: int, decimals1: int) -> float:
        """Human price of token0 in token1 (e.g. USDC per WETH)."""
        p = (self.sqrt_price_x96 / (1 << 96)) ** 2
        return p * 10 ** (decimals0 - decimals1)


def build_bitmap(initialized_ticks, tick_spacing: int) -> dict[int, int]:
    """Rebuild tickBitmap words from a set of initialized ticks."""
    bitmap: dict[int, int] = {}
    for t in initialized_ticks:
        if t % tick_spacing:
            raise ValueError(f"tick {t} not aligned to spacing {tick_spacing}")
        c = t // tick_spacing
        bitmap[c >> 8] = bitmap.get(c >> 8, 0) | (1 << (c & 0xFF))
    return bitmap
