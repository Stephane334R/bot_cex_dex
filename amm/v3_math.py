"""
Exact-integer port of the Uniswap v3 core math libraries.

Everything here mirrors the Solidity code in Uniswap/v3-core (FullMath, TickMath,
SqrtPriceMath, SwapMath) operation-for-operation, including the rounding
direction of every division. Python ints are arbitrary precision, so we only
emulate uint256 overflow where it changes the *code path* (see
`next_sqrt_price_from_amount0_rounding_up`). Because the rounding is identical,
quotes match the on-chain QuoterV2 to the wei (see tests/test_quoter_parity.py).

Notation (same as the whitepaper):
    sqrtP  = sqrt(price) * 2**96   (Q64.96), price = token1 / token0 in raw units
    L      = active liquidity
    Within one tick range:
        amount0 = L * (sqrtB - sqrtA) / (sqrtA * sqrtB)   (token0 delta)
        amount1 = L * (sqrtB - sqrtA)                     (token1 delta)
"""

from __future__ import annotations

Q96 = 1 << 96
Q128 = 1 << 128
MAX_UINT160 = (1 << 160) - 1
MAX_UINT256 = (1 << 256) - 1

MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342

FEE_DENOM = 1_000_000  # fees are expressed in pips (hundredths of a bp)


# --------------------------------------------------------------------------- #
# FullMath
# --------------------------------------------------------------------------- #
def mul_div(a: int, b: int, denominator: int) -> int:
    """floor(a * b / denominator)"""
    return (a * b) // denominator


def mul_div_rounding_up(a: int, b: int, denominator: int) -> int:
    """ceil(a * b / denominator)"""
    q, r = divmod(a * b, denominator)
    return q + 1 if r else q


def div_rounding_up(x: int, y: int) -> int:
    q, r = divmod(x, y)
    return q + 1 if r else q


# --------------------------------------------------------------------------- #
# TickMath
# --------------------------------------------------------------------------- #
# Each constant is 2**128 / sqrt(1.0001)**(2**i), used to build sqrt(1.0001)**-|tick|
# bit by bit. Copied from TickMath.sol.
_TICK_MAGIC = (
    (0x2, 0xFFF97272373D413259A46990580E213A),
    (0x4, 0xFFF2E50F5F656932EF12357CF3C7FDCC),
    (0x8, 0xFFE5CACA7E10E4E61C3624EAA0941CD0),
    (0x10, 0xFFCB9843D60F6159C9DB58835C926644),
    (0x20, 0xFF973B41FA98C081472E6896DFB254C0),
    (0x40, 0xFF2EA16466C96A3843EC78B326B52861),
    (0x80, 0xFE5DEE046A99A2A811C461F1969C3053),
    (0x100, 0xFCBE86C7900A88AEDCFFC83B479AA3A4),
    (0x200, 0xF987A7253AC413176F2B074CF7815E54),
    (0x400, 0xF3392B0822B70005940C7A398E4B70F3),
    (0x800, 0xE7159475A2C29B7443B29C7FA6E889D9),
    (0x1000, 0xD097F3BDFD2022B8845AD8F792AA5825),
    (0x2000, 0xA9F746462D870FDF8A65DC1F90E061E5),
    (0x4000, 0x70D869A156D2A1B890BB3DF62BAF32F7),
    (0x8000, 0x31BE135F97D08FD981231505542FCFA6),
    (0x10000, 0x9AA508B5B7A84E1C677DE54F3E99BC9),
    (0x20000, 0x5D6AF8DEDB81196699C329225EE604),
    (0x40000, 0x2216E584F5FA1EA926041BEDFE98),
    (0x80000, 0x48A170391F7DC42444E8FA2),
)


def get_sqrt_ratio_at_tick(tick: int) -> int:
    """sqrt(1.0001**tick) * 2**96, rounded up exactly like TickMath.getSqrtRatioAtTick."""
    abs_tick = -tick if tick < 0 else tick
    if abs_tick > MAX_TICK:
        raise ValueError(f"tick {tick} out of range")
    ratio = 0xFFFCB933BD6FAD37AA2D162D1A594001 if abs_tick & 0x1 else 0x100000000000000000000000000000000
    for bit, magic in _TICK_MAGIC:
        if abs_tick & bit:
            ratio = (ratio * magic) >> 128
    if tick > 0:
        ratio = MAX_UINT256 // ratio
    # Q128.128 -> Q64.96, rounding up
    return (ratio >> 32) + (0 if ratio % (1 << 32) == 0 else 1)


def get_tick_at_sqrt_ratio(sqrt_price_x96: int) -> int:
    """Greatest tick such that get_sqrt_ratio_at_tick(tick) <= sqrt_price_x96.

    Solidity uses a log2 approximation + correction; a binary search over the exact
    forward function gives the identical answer by definition.
    """
    if not (MIN_SQRT_RATIO <= sqrt_price_x96 < MAX_SQRT_RATIO):
        raise ValueError("sqrt price out of range")
    lo, hi = MIN_TICK, MAX_TICK
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if get_sqrt_ratio_at_tick(mid) <= sqrt_price_x96:
            lo = mid
        else:
            hi = mid - 1
    return lo


# --------------------------------------------------------------------------- #
# SqrtPriceMath
# --------------------------------------------------------------------------- #
def next_sqrt_price_from_amount0_rounding_up(sqrt_p: int, liquidity: int, amount: int, add: bool) -> int:
    """New sqrtP after adding/removing `amount` of token0. Rounds up (price moves less)."""
    if amount == 0:
        return sqrt_p
    numerator1 = liquidity << 96
    product = amount * sqrt_p
    if add:
        # Solidity takes the precise path only if amount*sqrtP fits in uint256.
        if product <= MAX_UINT256:
            denominator = numerator1 + product
            if denominator <= MAX_UINT256:
                return mul_div_rounding_up(numerator1, sqrt_p, denominator)
        return div_rounding_up(numerator1, numerator1 // sqrt_p + amount)
    if product > MAX_UINT256 or numerator1 <= product:
        raise ArithmeticError("insufficient liquidity for token0 output")
    return mul_div_rounding_up(numerator1, sqrt_p, numerator1 - product)


def next_sqrt_price_from_amount1_rounding_down(sqrt_p: int, liquidity: int, amount: int, add: bool) -> int:
    """New sqrtP after adding/removing `amount` of token1. Rounds down."""
    if add:
        quotient = (amount << 96) // liquidity
        return sqrt_p + quotient
    quotient = div_rounding_up(amount << 96, liquidity)
    if sqrt_p <= quotient:
        raise ArithmeticError("insufficient liquidity for token1 output")
    return sqrt_p - quotient


def next_sqrt_price_from_input(sqrt_p: int, liquidity: int, amount_in: int, zero_for_one: bool) -> int:
    if zero_for_one:
        return next_sqrt_price_from_amount0_rounding_up(sqrt_p, liquidity, amount_in, True)
    return next_sqrt_price_from_amount1_rounding_down(sqrt_p, liquidity, amount_in, True)


def next_sqrt_price_from_output(sqrt_p: int, liquidity: int, amount_out: int, zero_for_one: bool) -> int:
    if zero_for_one:
        return next_sqrt_price_from_amount1_rounding_down(sqrt_p, liquidity, amount_out, False)
    return next_sqrt_price_from_amount0_rounding_up(sqrt_p, liquidity, amount_out, False)


def get_amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    """|Δtoken0| between two prices: L * (sqrtB - sqrtA) / (sqrtA * sqrtB)."""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    numerator1 = liquidity << 96
    numerator2 = sqrt_b - sqrt_a
    if round_up:
        return div_rounding_up(mul_div_rounding_up(numerator1, numerator2, sqrt_b), sqrt_a)
    return mul_div(numerator1, numerator2, sqrt_b) // sqrt_a


def get_amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    """|Δtoken1| between two prices: L * (sqrtB - sqrtA)."""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if round_up:
        return mul_div_rounding_up(liquidity, sqrt_b - sqrt_a, Q96)
    return mul_div(liquidity, sqrt_b - sqrt_a, Q96)


# --------------------------------------------------------------------------- #
# SwapMath
# --------------------------------------------------------------------------- #
def compute_swap_step(
    sqrt_current: int, sqrt_target: int, liquidity: int, amount_remaining: int, fee_pips: int
) -> tuple[int, int, int, int]:
    """One swap step inside a single tick range.

    amount_remaining > 0 means exact input, < 0 means exact output.
    Returns (sqrt_next, amount_in, amount_out, fee_amount). The fee is charged on
    the input token, on top of amount_in.
    """
    zero_for_one = sqrt_current >= sqrt_target
    exact_in = amount_remaining >= 0

    if exact_in:
        remaining_less_fee = mul_div(amount_remaining, FEE_DENOM - fee_pips, FEE_DENOM)
        amount_in = (
            get_amount0_delta(sqrt_target, sqrt_current, liquidity, True)
            if zero_for_one
            else get_amount1_delta(sqrt_current, sqrt_target, liquidity, True)
        )
        if remaining_less_fee >= amount_in:
            sqrt_next = sqrt_target
        else:
            sqrt_next = next_sqrt_price_from_input(sqrt_current, liquidity, remaining_less_fee, zero_for_one)
    else:
        amount_out = (
            get_amount1_delta(sqrt_target, sqrt_current, liquidity, False)
            if zero_for_one
            else get_amount0_delta(sqrt_current, sqrt_target, liquidity, False)
        )
        if -amount_remaining >= amount_out:
            sqrt_next = sqrt_target
        else:
            sqrt_next = next_sqrt_price_from_output(sqrt_current, liquidity, -amount_remaining, zero_for_one)

    reached_target = sqrt_target == sqrt_next

    if zero_for_one:
        if not (reached_target and exact_in):
            amount_in = get_amount0_delta(sqrt_next, sqrt_current, liquidity, True)
        if not (reached_target and not exact_in):
            amount_out = get_amount1_delta(sqrt_next, sqrt_current, liquidity, False)
    else:
        if not (reached_target and exact_in):
            amount_in = get_amount1_delta(sqrt_current, sqrt_next, liquidity, True)
        if not (reached_target and not exact_in):
            amount_out = get_amount0_delta(sqrt_current, sqrt_next, liquidity, False)

    # Exact output: never hand out more than requested.
    if not exact_in and amount_out > -amount_remaining:
        amount_out = -amount_remaining

    if exact_in and sqrt_next != sqrt_target:
        # Ran out of input inside this range: whatever is left is the fee.
        fee_amount = amount_remaining - amount_in
    else:
        fee_amount = mul_div_rounding_up(amount_in, fee_pips, FEE_DENOM - fee_pips)

    return sqrt_next, amount_in, amount_out, fee_amount
