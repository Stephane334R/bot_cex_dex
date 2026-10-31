"""
Live shadow collector.

    python -m collector.main          (or: make collect)

One asyncio process with these tasks:
  * CEX feed       websocket bookTicker -> in-memory latest quote + history
  * quote saver    downsampled quotes -> cex_quotes (for the latency slider)
  * head tracker   newHeads WS (or polling) -> latest block
  * evaluator      on each new block (throttled): snapshot pool at that block, size both
                   directions, decide, update inventory, then after `latency_ms` re-price
                   the CEX leg and write everything to SQLite
  * gas refresher  ArbGasInfo L1 price every `l1_refresh_s`
  * competition    for flagged opportunities, scan Swap events in the next N blocks

It never signs or sends anything.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from amm.pool import OutOfLoadedRange
from collector.blocks import BlockListener, Head
from collector.cex import Quote, make_feed
from collector.chain import SWAP_TOPIC, Chain, decode_swap_log
from collector.records import build_rows, inventory_flows
from config import load_config, log, setup_logging
from storage.db import Store
from strategy import competition
from strategy.costs import DEX_BUY, DEX_SELL, CostParams, DexQuoter, gas_cost_usd
from strategy.engine import Market, StrategyParams, evaluate
from strategy.inventory import Inventory

logger = logging.getLogger("collector")


class Collector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.run_id = "live-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.store = Store(cfg.storage.path)
        self.chain = Chain(cfg.rpc_url, cfg)
        self.feed = make_feed(cfg)
        self.cp = CostParams.from_cfg(cfg)
        self.sp = StrategyParams.from_cfg(cfg)
        self.inv = Inventory.from_cfg(cfg)
        self.head: Head | None = None
        self.new_head = asyncio.Event()
        self.l1_price_wei = 0
        self.n_evals = 0
        self._quote_buf: list[tuple] = []
        self._last_quote_saved = 0.0

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        meta = await self.chain.resolve_pool()
        self.base_dec = meta.token0.decimals if meta.base_is_token0 else meta.token1.decimals
        self.quote_dec = meta.token1.decimals if meta.base_is_token0 else meta.token0.decimals
        self.store.new_run(self.run_id, "live", meta.address, dict(self.cfg, rpc_url="<redacted>",
                                                                   rpc_ws_url="<redacted>"))
        log(logger, "collector starting (SHADOW MODE: no orders, no transactions)", run_id=self.run_id,
            venue=self.cfg.cex.venue, symbol=self.cfg.cex.symbol, pool=meta.address)
        self.feed.listeners.append(self._on_quote)
        tasks = [
            self.feed.run(), self._track_heads(), self._evaluator(), self._l1_refresher(),
            self._competition_loop(), self._flush_quotes_loop(),
        ]
        await asyncio.gather(*tasks)

    # ------------------------------------------------------------------ #
    def _on_quote(self, q: Quote) -> None:
        if q.ts - self._last_quote_saved >= self.cfg.cex.quote_store_interval_ms / 1000:
            self._last_quote_saved = q.ts
            self._quote_buf.append((q.ts, q.bid, q.ask, q.bid_qty, q.ask_qty))

    async def _flush_quotes_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            if self._quote_buf:
                buf, self._quote_buf = self._quote_buf, []
                self.store.add_quotes(self.run_id, buf)

    async def _track_heads(self) -> None:
        listener = BlockListener(self.chain, self.cfg.rpc_ws_url, self.cfg.collector.poll_interval_s)
        async for h in listener.heads():
            self.head = h
            self.new_head.set()

    async def _l1_refresher(self) -> None:
        while True:
            try:
                self.l1_price_wei = await self.chain.l1_price_per_byte_wei()
            except Exception as e:
                log(logger, "ArbGasInfo read failed", logging.WARNING, error=repr(e)[:200])
            await asyncio.sleep(self.cfg.gas.l1_refresh_s)

    # ------------------------------------------------------------------ #
    async def _evaluator(self) -> None:
        last_eval = 0.0
        await self.feed.connected.wait()
        while True:
            await self.new_head.wait()
            self.new_head.clear()
            now = time.time()
            if now - last_eval < self.cfg.collector.min_eval_interval_s:
                continue
            head, q = self.head, self.feed.latest
            if q is None or (now - q.ts) * 1000 > self.cfg.cex.max_quote_age_ms:
                log(logger, "stale CEX quote, skipping block", logging.WARNING, block=head.number)
                continue
            last_eval = now
            try:
                await self._evaluate(head, q)
            except Exception as e:
                log(logger, "evaluation failed", logging.ERROR, block=head.number, error=repr(e)[:300])

    async def _evaluate(self, head: Head, q: Quote) -> None:
        cfg = self.cfg
        t0 = time.time()
        pool = await self.chain.load_pool_state(head.number, cfg.dex.tick_window)
        dq = DexQuoter(pool, self.chain.meta.base_is_token0, self.base_dec, self.quote_dec)
        mkt = Market(q.bid, q.ask, q.bid_qty, q.ask_qty)
        gas_usd = gas_cost_usd(self.cp, head.base_fee, self.l1_price_wei, q.mid)
        evals = evaluate(dq, mkt, gas_usd, self.cp, self.sp, self.inv)

        # Inventory moves at decision time, so the next block already sees the drained balances.
        for ev in evals.values():
            if ev.taken:
                dex_usdc, cex_usdc = inventory_flows(ev)
                self.inv.apply(ev.direction, ev.best.size, dex_usdc, cex_usdc)
                log(logger, "OPPORTUNITY (simulated)", block=head.number, direction=ev.direction,
                    size=round(ev.best.size, 4), net_usd=round(ev.best.net_usd, 2),
                    net_bps=round(ev.best.net_bps, 2), cap=ev.cap_reason)
        inv_snap = self.inv.balances()
        self.n_evals += 1
        if cfg.dex.quoter_check_every_n_evals and self.n_evals % cfg.dex.quoter_check_every_n_evals == 1:
            asyncio.create_task(self._quoter_check(pool, dq))

        # Latency model: wait, then re-price the CEX leg at the quote seen `latency_ms` after the decision.
        lat = cfg.strategy.latency_ms / 1000
        if lat > 0:
            await asyncio.sleep(max(0.0, t0 + lat - time.time()) + 0.01)
        qd = self.feed.quote_at(t0 + lat) or q
        row, dirs, opps = build_rows(
            self.run_id, t0, head.number, head.timestamp, q.ts, mkt, dq.mid, pool, head.base_fee,
            self.l1_price_wei, gas_usd, evals, self.cp, (qd.bid, qd.ask), inv_snap)
        self.store.add_evaluation(row, dirs, opps)
        if self.n_evals % 30 == 1:
            log(logger, "evaluated", block=head.number, cex_bid=q.bid, cex_ask=q.ask, dex_mid=round(dq.mid, 4),
                spread_buy_bps=round(row["spread_buy_bps"], 2), spread_sell_bps=round(row["spread_sell_bps"], 2),
                best_net_buy=_r(evals[DEX_BUY]), best_net_sell=_r(evals[DEX_SELL]),
                gas_usd=round(gas_usd, 4), ticks=len(pool.liquidity_net), eval_ms=int((time.time() - t0) * 1000))

    # ------------------------------------------------------------------ #
    async def _quoter_check(self, pool, dq: DexQuoter) -> None:
        """Cross-check local quotes against QuoterV2 at the same block; log mismatches."""
        cfg, meta = self.cfg, self.chain.meta
        b, qt = cfg.dex.base_token, cfg.dex.quote_token
        for size in cfg.dex.quoter_check_sizes:
            for direction in (DEX_BUY, DEX_SELL):
                raw = int(size * 10**self.base_dec)
                try:
                    if direction == DEX_BUY:
                        onchain, _ = await self.chain.quoter_exact_output(qt, b, raw, pool.block)
                        local = pool.swap(not meta.base_is_token0, -raw).amount_in()
                    else:
                        onchain, _ = await self.chain.quoter_exact_input(b, qt, raw, pool.block)
                        local = pool.swap(meta.base_is_token0, raw).amount_out()
                except OutOfLoadedRange:
                    continue
                except Exception as e:
                    log(logger, "quoter check failed", logging.WARNING, error=repr(e)[:200])
                    continue
                diff_bps = abs(local - onchain) / max(onchain, 1) * 1e4
                ok = diff_bps <= cfg.dex.quoter_tolerance_bps
                self.store.add_quoter_check((self.run_id, time.time(), pool.block, direction, size,
                                             str(local), str(onchain), diff_bps, int(ok)))
                lvl = logging.INFO if ok else logging.ERROR
                log(logger, "quoter parity" if ok else "QUOTER MISMATCH", lvl, block=pool.block,
                    direction=direction, size=size, local=local, onchain=onchain, diff_bps=diff_bps)

    # ------------------------------------------------------------------ #
    async def _competition_loop(self) -> None:
        n = self.cfg.collector.competition_blocks
        base_is_token0 = None
        while True:
            await asyncio.sleep(self.cfg.collector.competition_check_interval_s)
            try:
                if self.head is None or self.chain.meta is None:
                    continue
                base_is_token0 = self.chain.meta.base_is_token0
                ready = self.store.unchecked_opportunities(self.run_id, self.head.number - n)
                if not ready:
                    continue
                lo, hi = ready[0]["block"], ready[-1]["block"] + n
                logs = await self.chain.get_logs(lo + 1, hi, [SWAP_TOPIC])
                swaps = [decode_swap_log(lg) for lg in logs]
                # Block timestamps for the "how fast" metric (Arbitrum has 1 s timestamp resolution).
                blocks_needed = sorted({s["block"] for s in swaps})
                ts_map = {}
                for bn in blocks_needed[:50]:
                    ts_map[bn] = (await self.chain.get_block(bn))["timestamp"]
                for s in swaps:
                    s["ts"] = ts_map.get(s["block"])
                self.store.add_swaps(swaps)
                for o in ready:
                    window = [s for s in swaps if o["block"] < s["block"] <= o["block"] + n]
                    res = competition.check(o["direction"], int(o["sqrt_before"]), int(o["sqrt_after"]), window,
                                            base_is_token0, o["block"], ts_map, o["ts"], self.base_dec)
                    self.store.set_competition(o["id"], res)
            except Exception as e:
                log(logger, "competition check failed", logging.WARNING, error=repr(e)[:200])


def _r(ev):
    return round(ev.best.net_usd, 2) if ev.best else None


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.logging.level)
    try:
        asyncio.run(Collector(cfg).start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
