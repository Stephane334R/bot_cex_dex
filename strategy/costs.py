"""
Cost model and per-size PnL for both arbitrage directions.

Sizes are always expressed in the BASE asset (ETH), so both legs trade the same
quantity and the position is flat in ETH after the round trip; PnL is in USD(C).

    A  "dex_buy":  buy ETH on the DEX (exact-output, pay USDC), sell the same ETH on the CEX at the bid
    B  "dex_sell": buy ETH on the CEX at the ask, sell it on the DEX (exact-input, receive USDC)

net = gross edge - DEX fee - CEX taker fee - gas - slippage/latency haircut

The DEX fee is already inside the swap math (it reduces the amount that moves the
price). We report it separately only for attribution: gross := net + all costs.
"""

from __future__ import annotations

from dataclasses import dataclass

from amm.pool import PoolState

DEX_BUY = "dex_buy"  # direction A
DEX_SELL = "dex_sell"  # direction B
DIRECTIONS = (DEX_BUY, DEX_SELL)


@dataclass(frozen=True)
class CostParams:
    cex_fee_bps: float = 10.0
    haircut_bps: float = 1.0
    swap_gas_units: int = 130_000
    l1_calldata_bytes: int = 250
    gas_price_multiplier: float = 1.0

    @classmethod
    def from_cfg(cls, cfg) -> "CostParams":
        return cls(cfg.cex.taker_fee_bps, cfg.strategy.slippage_haircut_bps, cfg.gas.swap_gas_units,
                   cfg.gas.l1_calldata_bytes, cfg.gas.gas_price_multiplier)


def gas_cost_usd(p: CostParams, l2_base_fee_wei: int, l1_price_per_byte_wei: int, eth_usd: float) -> float:
    """Arbitrum fee = L2 execution (gas units * base fee) + L1 data posting (bytes * L1 price/byte)."""
    l2 = p.swap_gas_units * l2_base_fee_wei * p.gas_price_multiplier
    l1 = p.l1_calldata_bytes * l1_price_per_byte_wei
    return (l2 + l1) / 1e18 * eth_usd


@dataclass
class DexLeg:
    """Result of the DEX leg for a given base size."""

    size: float  # base units
    quote_amount: float  # USDC paid (dex_buy) or received (dex_sell)
    fee_usd: float  # DEX LP fee, in USD
    sqrt_price_after: int


class DexQuoter:
    """Wraps a PoolState with base/quote semantics and token decimals."""

    def __init__(self, pool: PoolState, base_is_token0: bool, base_dec: int = 18, quote_dec: int = 6):
        self.pool = pool
        self.base_is_token0 = base_is_token0
        self.bs, self.qs = 10**base_dec, 10**quote_dec
        p = pool.price(base_dec, quote_dec) if base_is_token0 else 1 / pool.price(quote_dec, base_dec)
        self.mid = p  # USDC per ETH at the current pool price (before fees)

    def leg(self, direction: str, size: float) -> DexLeg:
        raw = int(size * self.bs)
        if direction == DEX_BUY:  # quote in, exact base out
            zfo = not self.base_is_token0
            r = self.pool.swap(zfo, -raw)
            return DexLeg(size, r.amount_in() / self.qs, r.fee_paid / self.qs, r.sqrt_price_after)
        zfo = self.base_is_token0  # base in, exact
        r = self.pool.swap(zfo, raw)
        return DexLeg(size, r.amount_out() / self.qs, r.fee_paid / self.bs * self.mid, r.sqrt_price_after)

    def max_size(self, direction: str) -> float:
        """Largest base size the loaded tick window can price in this direction."""
        if direction == DEX_BUY:
            r = self.pool.max_swap_within_window(not self.base_is_token0)
            return r.amount_out() / self.bs
        r = self.pool.max_swap_within_window(self.base_is_token0)
        return r.amount_in() / self.bs


@dataclass
class Pnl:
    direction: str
    size: float
    notional_usd: float
    dex_quote_usd: float
    cex_price: float
    gross_usd: float
    dex_fee_usd: float
    cex_fee_usd: float
    gas_usd: float
    haircut_usd: float
    net_usd: float
    net_bps: float


def pnl(direction: str, size: float, dex_quote_usd: float, dex_fee_usd: float,
        bid: float, ask: float, gas_usd: float, p: CostParams) -> Pnl:
    """Net PnL of one round trip given the DEX leg result and CEX top of book.

    Pure arithmetic, so the dashboard reuses it to recompute PnL under other assumptions.
    """
    if direction == DEX_BUY:
        cex_px = bid
        notional = size * bid
        cex_fee = notional * p.cex_fee_bps / 1e4
        haircut = notional * p.haircut_bps / 1e4
        net = notional - cex_fee - dex_quote_usd - gas_usd - haircut
    else:
        cex_px = ask
        notional = size * ask
        cex_fee = notional * p.cex_fee_bps / 1e4
        haircut = notional * p.haircut_bps / 1e4
        net = dex_quote_usd - notional - cex_fee - gas_usd - haircut
    gross = net + dex_fee_usd + cex_fee + gas_usd + haircut
    bps = net / notional * 1e4 if notional > 0 else 0.0
    return Pnl(direction, size, notional, dex_quote_usd, cex_px, gross, dex_fee_usd, cex_fee, gas_usd, haircut, net, bps)
