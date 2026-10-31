"""Turn an evaluation into storage rows. Shared by the live collector and the backfill."""

from __future__ import annotations

import json

from strategy.costs import DEX_BUY, CostParams, pnl
from strategy.engine import DirectionEval, Market


def delayed_net(ev: DirectionEval, bid_d: float, ask_d: float, gas_usd: float, cp: CostParams) -> float | None:
    """Same size and same DEX fill, CEX leg re-priced at the quote seen after the latency."""
    if ev.best is None:
        return None
    b = ev.best
    return pnl(b.direction, b.size, b.dex_quote_usd, b.dex_fee_usd, bid_d, ask_d, gas_usd, cp).net_usd


def build_rows(run_id: str, ts: float, block: int, block_ts: int, cex_ts: float, mkt: Market,
               dex_mid: float, pool, base_fee: int, l1_price: int, gas_usd: float,
               evals: dict[str, DirectionEval], cp: CostParams, delayed: tuple[float, float] | None,
               inventory_snapshot: dict | None) -> tuple[dict, list[dict], list[dict]]:
    row = dict(
        run_id=run_id, ts=ts, block=block, block_ts=block_ts, cex_ts=cex_ts,
        bid=mkt.bid, ask=mkt.ask, bid_qty=mkt.bid_qty, ask_qty=mkt.ask_qty,
        dex_mid=dex_mid, sqrt_price_x96=str(pool.sqrt_price_x96), tick=pool.tick, liquidity=str(pool.liquidity),
        base_fee_wei=base_fee, l1_price_wei=l1_price, gas_usd=gas_usd,
        spread_buy_bps=(mkt.bid - dex_mid) / dex_mid * 1e4,
        spread_sell_bps=(dex_mid - mkt.ask) / mkt.ask * 1e4,
    )
    dirs, opps = [], []
    bid_d, ask_d = delayed if delayed else (None, None)
    for d, ev in evals.items():
        b = ev.best
        nd = delayed_net(ev, bid_d, ask_d, gas_usd, cp) if delayed else None
        dirs.append(dict(
            run_id=run_id, direction=d,
            size=b.size if b else None, notional=b.notional_usd if b else None,
            dex_quote_usd=b.dex_quote_usd if b else None, gross=b.gross_usd if b else None,
            dex_fee=b.dex_fee_usd if b else None, cex_fee=b.cex_fee_usd if b else None,
            gas=b.gas_usd if b else None, haircut=b.haircut_usd if b else None,
            net=b.net_usd if b else None, net_bps=b.net_bps if b else None,
            cap=ev.cap, cap_reason=ev.cap_reason, taken=int(ev.taken),
            bid_delayed=bid_d, ask_delayed=ask_d, net_delayed=nd,
            sqrt_after=str(ev.sqrt_price_after) if ev.sqrt_price_after else None,
            curve=json.dumps([[round(x, 8) for x in p] for p in ev.curve]),
        ))
        if ev.taken:
            opps.append(dict(
                run_id=run_id, ts=ts, block=block, direction=d, size=b.size, notional=b.notional_usd,
                gross=b.gross_usd, dex_fee=b.dex_fee_usd, cex_fee=b.cex_fee_usd, gas=b.gas_usd,
                haircut=b.haircut_usd, net=b.net_usd, net_bps=b.net_bps, net_delayed=nd,
                sqrt_before=str(pool.sqrt_price_x96), sqrt_after=str(ev.sqrt_price_after),
                inventory=json.dumps(inventory_snapshot) if inventory_snapshot else None,
            ))
    return row, dirs, opps


def inventory_flows(ev: DirectionEval) -> tuple[float, float]:
    """(USDC moved on the DEX, USDC moved on the CEX) for an executed opportunity."""
    b = ev.best
    if b.direction == DEX_BUY:
        return b.dex_quote_usd, b.notional_usd - b.cex_fee_usd
    return b.dex_quote_usd, b.notional_usd + b.cex_fee_usd
