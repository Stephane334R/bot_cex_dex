"""
Replay/backfill mode: produce a dashboard-ready run for a past window without
waiting days.

    python -m collector.backfill [--hours 3] [--step 5] [--no-validate]     (or: make backfill)

How historical pool state is obtained
-------------------------------------
Public RPCs are usually *not* archive nodes (eth_call at a block more than a few
hours old fails), but eth_getLogs works over long ranges. So we rebuild exact
historical state from events:

  * Tick map (liquidityNet / liquidityGross per tick): snapshot it at the head
    block H, then walk backwards undoing every Mint/Burn after block b.
  * Price state (sqrtPriceX96, tick, active liquidity): every Swap event carries
    the post-swap sqrtPriceX96, tick and liquidity. State at block b = the last
    Swap at or before b, plus the effect of later in-range Mint/Burn on liquidity.

That is exact, not an approximation. `--validate` checks it against eth_call
(slot0, liquidity, QuoterV2) at recent blocks the RPC can still serve.

CEX side: Binance 1 s klines. Limitations: no real bid/ask (close ± a synthetic
half-spread), no top-of-book size (configurable assumption), 1 s granularity, so
latency below 1 s cannot be resolved. See README.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import logging
import time
import urllib.parse
import urllib.request

from amm.pool import PoolState, build_bitmap
from collector.chain import BURN_TOPIC, MINT_TOPIC, SWAP_TOPIC, Chain, decode_mint_burn_log, decode_swap_log
from collector.records import build_rows, inventory_flows
from config import load_config, log, setup_logging
from storage.db import Store
from strategy import competition
from strategy.costs import DEX_BUY, CostParams, DexQuoter, gas_cost_usd
from strategy.engine import Market, StrategyParams, evaluate
from strategy.inventory import Inventory

logger = logging.getLogger("backfill")


# ----------------------------------------------------------------------------- CEX history
def binance_klines_1s(symbol: str, start_ms: int, end_ms: int) -> list[tuple[float, float]]:
    """[(open_time_s, close_price)] for 1 s klines in [start, end)."""
    out, cur = [], start_ms
    while cur < end_ms:
        qs = urllib.parse.urlencode(dict(symbol=symbol, interval="1s", startTime=cur, endTime=end_ms, limit=1000))
        with urllib.request.urlopen(f"https://api.binance.com/api/v3/klines?{qs}", timeout=20) as r:
            rows = json.loads(r.read())
        if not rows:
            break
        out += [(k[0] / 1000, float(k[4])) for k in rows]
        cur = rows[-1][0] + 1000
    return out


# ----------------------------------------------------------------------------- state replay
class Replayer:
    """Exact historical pool state from a head snapshot + Swap/Mint/Burn events."""

    def __init__(self, head_state: PoolState, gross: dict[int, int], events: list[dict]):
        self.head = head_state
        self.events = sorted(events, key=lambda e: (e["block"], e["log_index"]))
        self.keys = [(e["block"], e["log_index"]) for e in self.events]
        # Forward pass: running price state after each event (None until the first Swap).
        self.price_after: list[tuple[int, int, int] | None] = []
        cur = None
        for e in self.events:
            if e["kind"] == "swap":
                cur = (e["sqrt_price_x96"], e["tick"], e["liquidity"])
            elif cur is not None and e["tick_lower"] <= cur[1] < e["tick_upper"]:
                d = e["amount"] if e["kind"] == "mint" else -e["amount"]
                cur = (cur[0], cur[1], cur[2] + d)
            self.price_after.append(cur)
        # Backward pass state (mutated as we walk to older blocks).
        self.net = dict(head_state.liquidity_net)
        self.gross = dict(gross)
        self._undo_ptr = len(self.events)  # events[_undo_ptr:] have been undone
        self._last_block = None

    def state_at(self, block: int, tick_window: int) -> PoolState | None:
        """Pool state at the END of `block`. Must be called with non-increasing blocks."""
        if self._last_block is not None and block > self._last_block:
            raise ValueError("state_at must be called with non-increasing blocks")
        self._last_block = block
        # Undo Mint/Burn strictly after `block`.
        while self._undo_ptr > 0 and self.events[self._undo_ptr - 1]["block"] > block:
            self._undo_ptr -= 1
            e = self.events[self._undo_ptr]
            if e["kind"] in ("mint", "burn") and e["amount"]:
                s = 1 if e["kind"] == "mint" else -1  # undo => subtract a mint, add back a burn
                for t, sign_net in ((e["tick_lower"], 1), (e["tick_upper"], -1)):
                    self.net[t] = self.net.get(t, 0) - s * sign_net * e["amount"]
                    self.gross[t] = self.gross.get(t, 0) - s * e["amount"]
        i = bisect.bisect_right(self.keys, (block, 1 << 62)) - 1
        ps = self.price_after[i] if i >= 0 else None
        if ps is None:
            if i == len(self.events) - 1 or not self.events:
                ps = (self.head.sqrt_price_x96, self.head.tick, self.head.liquidity)
            else:
                return None  # no Swap seen yet before this block in the fetched range
        sqrt_p, tick, liq = ps
        ts = self.head.tick_spacing
        lo = max(self.head.tick_lo, ((tick - tick_window) // ts) * ts)
        hi = min(self.head.tick_hi, -((-(tick + tick_window)) // ts) * ts)
        if lo >= tick or hi <= tick:
            return None  # price drifted outside the snapshot window
        init = [t for t, g in self.gross.items() if g > 0 and self.head.tick_lo <= t <= self.head.tick_hi]
        net = {t: self.net.get(t, 0) for t in init}
        return PoolState(sqrt_p, tick, liq, self.head.fee, ts, build_bitmap(init, ts), net, lo, hi, block)


# ----------------------------------------------------------------------------- helpers
async def block_at_ts(chain: Chain, ts: float, hi_block: int, hi_ts: int) -> int:
    """Binary search the last block with timestamp <= ts."""
    lo = max(1, hi_block - int((hi_ts - ts) / 0.2) - 10_000)
    hi = hi_block
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if (await chain.get_block(mid))["timestamp"] <= ts:
            lo = mid
        else:
            hi = mid - 1
    return lo


async def fetch_events(chain: Chain, start: int, end: int, chunk: int) -> tuple[list[dict], list[dict]]:
    events, swaps = [], []
    for a in range(start, end + 1, chunk):
        b = min(end, a + chunk - 1)
        logs = await chain.get_logs(a, b, [SWAP_TOPIC, MINT_TOPIC, BURN_TOPIC])
        for lg in logs:
            t0 = "0x" + bytes(lg["topics"][0]).hex()
            ts = lg.get("blockTimestamp")
            ts = int(ts, 16) if isinstance(ts, str) else ts
            if t0 == SWAP_TOPIC:
                s = decode_swap_log(lg)
                s["ts"] = ts
                swaps.append(s)
                events.append(dict(kind="swap", **s))
            else:
                events.append(decode_mint_burn_log(lg))
        log(logger, "fetched logs", from_block=a, to_block=b, n=len(logs))
    return events, swaps


async def validate(chain: Chain, rep_factory, head_block: int, cfg, n: int = 4) -> list[dict]:
    """Compare replayed state with eth_call at recent blocks (inside the RPC's state window)."""
    meta = chain.meta
    rep = rep_factory()
    results = []
    blocks = sorted({head_block - k for k in (40, 250, 500, 800)[:n]}, reverse=True)
    for b in blocks:
        st = rep.state_at(b, cfg.dex.tick_window)
        try:
            onchain = await chain.load_pool_state(b, cfg.dex.tick_window)
        except Exception as e:
            log(logger, "validation block not served by RPC (not an archive node?)", logging.WARNING,
                block=b, error=repr(e)[:120])
            continue
        if st is None:
            continue
        same_state = (st.sqrt_price_x96, st.tick, st.liquidity) == (onchain.sqrt_price_x96, onchain.tick,
                                                                    onchain.liquidity)
        net_ok = all(st.liquidity_net.get(t, 0) == v for t, v in onchain.liquidity_net.items())
        quotes_ok = True
        for size in cfg.dex.quoter_check_sizes:
            raw = int(size * 1e18)
            q_on, _ = await chain.quoter_exact_input(cfg.dex.base_token, cfg.dex.quote_token, raw, b)
            q_loc = st.swap(meta.base_is_token0, raw).amount_out()
            quotes_ok &= q_on == q_loc
        r = dict(block=b, slot0_and_liquidity_match=same_state, tick_map_match=net_ok, quoter_match=quotes_ok)
        log(logger, "replay validation", **r)
        results.append(r)
    return results


# ----------------------------------------------------------------------------- main
async def run(hours: float, step_s: float, do_validate: bool) -> str:
    cfg = load_config()
    chain = Chain(cfg.rpc_url, cfg)
    meta = await chain.resolve_pool()
    bf = cfg.backfill
    base_dec = meta.token0.decimals if meta.base_is_token0 else meta.token1.decimals
    quote_dec = meta.token1.decimals if meta.base_is_token0 else meta.token0.decimals

    head_block = await chain.block_number() - 5
    head = await chain.get_block(head_block)
    t_end = head["timestamp"]
    t_start = t_end - hours * 3600
    window = cfg.dex.tick_window + bf.extra_tick_window
    head_state, gross = await chain.load_pool_state(head_block, window, with_gross=True)
    log(logger, "head snapshot", block=head_block, tick=head_state.tick, ticks=len(gross), window=window)

    start_block = await block_at_ts(chain, t_start, head_block, t_end)
    margin_block = await block_at_ts(chain, t_start - 1800, start_block, int(t_start))  # find a prior Swap
    events, swaps = await fetch_events(chain, margin_block, head_block, bf.logs_chunk_blocks)
    log(logger, "events", swaps=len(swaps), total=len(events), start_block=start_block, head_block=head_block)

    def make_rep():
        return Replayer(head_state, gross, events)

    if do_validate:
        res = await validate(chain, make_rep, head_block, cfg)
        if res and not all(all(v for k, v in r.items() if k != "block") for r in res):
            log(logger, "REPLAY VALIDATION FAILED", logging.ERROR, results=res)

    # Block anchors for time->block mapping and base fee (Arbitrum base fee moves slowly).
    anchors = []
    n_anchor = max(2, int(hours * 12) + 1)  # one every ~5 min
    for k in range(n_anchor):
        b = start_block + (head_block - start_block) * k // (n_anchor - 1)
        blk = await chain.get_block(b)
        anchors.append((blk["timestamp"], b, blk.get("baseFeePerGas", 0)))
    a_ts = [a[0] for a in anchors]

    def block_for(ts: float) -> tuple[int, int]:
        i = max(0, min(len(anchors) - 2, bisect.bisect_right(a_ts, ts) - 1))
        (t0, b0, f0), (t1, b1, _) = anchors[i], anchors[i + 1]
        b = b0 + int((b1 - b0) * (ts - t0) / max(t1 - t0, 1))
        return max(b0, min(b, head_block)), f0

    l1_price = await chain.l1_price_per_byte_wei()
    # Fallback if the RPC doesn't return blockTimestamp in logs: interpolate between anchors.
    a_blk = [a[1] for a in anchors]
    for s in swaps:
        if s.get("ts") is None:
            j = max(0, min(len(anchors) - 2, bisect.bisect_right(a_blk, s["block"]) - 1))
            (t0, b0, _), (t1, b1, _) = anchors[j], anchors[j + 1]
            s["ts"] = t0 + (t1 - t0) * (s["block"] - b0) / max(b1 - b0, 1)

    # CEX klines.
    kl = binance_klines_1s(cfg.cex.symbol if cfg.cex.venue == "binance" else "ETHUSDC",
                           int(t_start * 1000) - 5000, int(t_end * 1000))
    k_ts = [k[0] for k in kl]
    hs = bf.synthetic_half_spread_bps / 1e4

    def cex_at(ts: float) -> tuple[float, float] | None:
        i = bisect.bisect_right(k_ts, ts - 1) - 1  # last *completed* second
        if i < 0:
            return None
        c = kl[i][1]
        return c * (1 - hs), c * (1 + hs)

    run_id = "backfill-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(t_start))
    store = Store(cfg.storage.path)
    store.new_run(run_id, "backfill", meta.address, dict(cfg, rpc_url="<redacted>", rpc_ws_url="<redacted>"),
                  notes=f"{hours}h window, step {step_s}s, blocks {start_block}-{head_block}")
    store.add_quotes(run_id, [(t + 1, c * (1 - hs), c * (1 + hs), bf.assumed_top_qty, bf.assumed_top_qty)
                              for t, c in kl])
    store.add_swaps(swaps)

    cp, sp = CostParams.from_cfg(cfg), StrategyParams.from_cfg(cfg)
    inv = Inventory.from_cfg(cfg)
    lat = cfg.strategy.latency_ms / 1000

    # Evaluate oldest -> newest for inventory, but replay needs newest -> oldest: build states first.
    grid = [t_start + i * step_s for i in range(int((t_end - t_start) / step_s))]
    rep = make_rep()
    states = {}
    for ts in reversed(grid):
        b, fee = block_for(ts)
        stt = rep.state_at(b, cfg.dex.tick_window)
        if stt is not None:
            states[ts] = (b, fee, stt)
    log(logger, "states rebuilt", samples=len(states))

    n_opp = 0
    swaps_sorted = sorted(swaps, key=lambda s: (s["block"], s["log_index"]))
    sw_blocks = [s["block"] for s in swaps_sorted]
    block_ts = {s["block"]: s["ts"] for s in swaps_sorted if s.get("ts")}
    for i, ts in enumerate(grid):
        if ts not in states:
            continue
        b, base_fee, pool = states[ts]
        qa = cex_at(ts)
        qd = cex_at(ts + lat)
        if qa is None:
            continue
        mkt = Market(qa[0], qa[1], bf.assumed_top_qty, bf.assumed_top_qty)
        dq = DexQuoter(pool, meta.base_is_token0, base_dec, quote_dec)
        gas_usd = gas_cost_usd(cp, base_fee, l1_price, 0.5 * (qa[0] + qa[1]))
        evals = evaluate(dq, mkt, gas_usd, cp, sp, inv)
        for ev in evals.values():
            if ev.taken:
                dx, cx = inventory_flows(ev)
                inv.apply(ev.direction, ev.best.size, dx, cx)
        row, dirs, opps = build_rows(run_id, ts, b, int(ts), ts, mkt, dq.mid, pool, base_fee, l1_price, gas_usd,
                                     evals, cp, qd or qa, inv.balances())
        store.add_evaluation(row, dirs, opps)
        # Competition check straight away: all future swaps are already known.
        n = cfg.collector.competition_blocks
        for o in opps:
            n_opp += 1
            lo_i = bisect.bisect_right(sw_blocks, b)
            hi_i = bisect.bisect_right(sw_blocks, b + n)
            res = competition.check(o["direction"], int(o["sqrt_before"]), int(o["sqrt_after"]),
                                    swaps_sorted[lo_i:hi_i], meta.base_is_token0, b, block_ts, ts, base_dec)
            oid = store.con.execute("SELECT MAX(id) FROM opportunities WHERE run_id=? AND direction=?",
                                    (run_id, o["direction"])).fetchone()[0]
            store.set_competition(oid, res)
        if i % 200 == 0:
            log(logger, "progress", done=i, of=len(grid), opportunities=n_opp,
                best_buy=round(evals[DEX_BUY].best.net_usd, 2) if evals[DEX_BUY].best else None)
    log(logger, "backfill done", run_id=run_id, evaluations=len(states), opportunities=n_opp,
        capital_used_usd=round(inv.capital_used_usd(kl[-1][1]), 0))
    return run_id


def main() -> None:
    cfg = load_config()
    setup_logging(cfg.logging.level)
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--hours", type=float, default=cfg.backfill.hours)
    p.add_argument("--step", type=float, default=cfg.backfill.step_s, help="seconds between evaluations")
    p.add_argument("--no-validate", action="store_true", help="skip the eth_call replay validation")
    a = p.parse_args()
    asyncio.run(run(a.hours, a.step, not a.no_validate))


if __name__ == "__main__":
    main()
