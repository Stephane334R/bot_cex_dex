"""
Simulated two-venue inventory.

CEX–DEX arbitrage is not capital-free: direction A needs USDC on the DEX and ETH on
the CEX, direction B needs USDC on the CEX and ETH on the DEX. Trading drains
one side of each venue, so without rebalancing (slow and costly: withdrawals,
bridging) a direction eventually stops. We track that and the peak capital
actually used.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .costs import DEX_BUY


@dataclass
class Inventory:
    cex_base: float
    cex_quote: float
    dex_base: float
    dex_quote: float
    start: dict = field(default_factory=dict)
    low: dict = field(default_factory=dict)  # lowest balance seen per bucket

    def __post_init__(self):
        self.start = self.balances()
        self.low = dict(self.start)

    @classmethod
    def from_cfg(cls, cfg) -> "Inventory":
        i = cfg.inventory
        return cls(i["cex"]["base"], i["cex"]["quote"], i["dex"]["base"], i["dex"]["quote"])

    def balances(self) -> dict:
        return dict(cex_base=self.cex_base, cex_quote=self.cex_quote,
                    dex_base=self.dex_base, dex_quote=self.dex_quote)

    def limits(self, direction: str) -> tuple[float, float]:
        """(max base the base-selling venue holds, max USDC the buying venue holds)."""
        if direction == DEX_BUY:
            return self.cex_base, self.dex_quote
        return self.dex_base, self.cex_quote

    def can_afford(self, direction: str, size: float, quote_needed: float) -> bool:
        base_avail, quote_avail = self.limits(direction)
        return size <= base_avail + 1e-12 and quote_needed <= quote_avail + 1e-9

    def apply(self, direction: str, size: float, dex_quote: float, cex_quote: float) -> None:
        """dex_quote / cex_quote: USDC amounts that move on each venue (fees included)."""
        if direction == DEX_BUY:
            self.dex_quote -= dex_quote
            self.dex_base += size
            self.cex_base -= size
            self.cex_quote += cex_quote
        else:
            self.cex_quote -= cex_quote
            self.cex_base += size
            self.dex_base -= size
            self.dex_quote += dex_quote
        for k, v in self.balances().items():
            self.low[k] = min(self.low[k], v)

    def capital_used_usd(self, eth_usd: float) -> float:
        """Peak drawdown of every bucket, valued in USD: the capital the run really needed."""
        used = {k: self.start[k] - self.low[k] for k in self.start}
        return (used["cex_base"] + used["dex_base"]) * eth_usd + used["cex_quote"] + used["dex_quote"]
