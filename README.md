# CEX–DEX Arbitrage Shadow Bot: ETH/USDC, Binance vs Uniswap v3 (Arbitrum)

A **paper-trading (shadow) bot** that, on every Arbitrum block, answers:

> *If I bought on one venue and sold on the other right now, at the optimal size, what would I net after all costs?*

It watches the **Binance `ETHUSDC` top of book** and the **Uniswap v3 WETH/USDC 0.05% pool on Arbitrum**. It prices
the DEX leg with an **exact integer port of the Uniswap v3 swap math (tick crossing included)** that matches the
on-chain QuoterV2 **to the wei**. For each direction it finds the PnL-maximising size and logs every evaluation and
every *would-have-traded* opportunity. It then checks whether someone else closed the gap within the next few blocks.
A Streamlit dashboard shows everything and recomputes PnL live under different assumptions.

> ⚠️ **Shadow mode only.** The code holds no private keys and has no signing or transaction-sending code paths.
> It only reads (`eth_call`, `eth_getLogs`, public market-data websockets).
> **All PnL is a simulated upper bound.** See [Why real PnL would be lower](#why-real-pnl-would-be-lower).

---

## Contents
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Verified addresses](#verified-addresses)
- [The math: Uniswap v3 swaps](#the-math-uniswap-v3-swaps)
- [Cost model and sizing](#cost-model-and-sizing)
- [Realism and validation](#realism-and-validation)
- [Replay / backfill mode](#replay--backfill-mode)
- [Dashboard](#dashboard)
- [Tests](#tests)
- [Findings (template)](#findings-template)
- [Why real PnL would be lower](#why-real-pnl-would-be-lower)

---

## Quick start

```bash
make setup                 # venv + requirements, creates .env from .env.example
make test                  # offline tests (math, QuoterV2 parity fixture, strategy, replay)
make backfill HOURS=3      # replay the last 3 h so the dashboard has data right away
make collect               # live collector (separate terminal, Ctrl-C to stop)
make app                   # dashboard on http://localhost:8501
```

- `.env`: `RPC_URL` (Arbitrum HTTP RPC; the public `https://arb1.arbitrum.io/rpc` works) and optionally
  `RPC_WS_URL` (Alchemy/Infura websocket) for `newHeads`. Without it the collector polls for blocks every 250 ms.
- Every parameter (pair, fee tier, fees, thresholds, latency, gas units, max notional, starting inventory…) is in
  [`config.yaml`](config.yaml).
- If Binance is geo-blocked: set `cex.venue: bybit` (`symbol: ETHUSDC`) or `okx` (`symbol: ETH-USDC`).

Python 3.11+. Dependencies: `web3` (async), `websockets`, `pandas`, `plotly`, `streamlit`, `python-dotenv`, `pytest`.

## Architecture

```
                 ┌───────────────────────── collector process (asyncio) ─────────────────────────┐
 Binance WS ───► │ cex.py  bookTicker → latest quote + 2 min history ───────────┐                │
 (Bybit/OKX)     │                                                              ▼                │
                 │ blocks.py  newHeads WS │ HTTP polling ──► evaluator ── strategy/engine.py     │
 Arbitrum RPC ◄──┤ chain.py   Multicall3 snapshot @block: slot0, liquidity,      │ (size, costs,   │
                 │            tickBitmap, ticks; QuoterV2; ArbGasInfo; logs      │  decision,      │
                 │                                                               │  inventory)     │
                 │ latency model: wait latency_ms, re-price CEX leg ─────────────┤                 │
                 │ competition: Swap events in next N blocks ────────────────────┤                 │
                 └───────────────────────────────────────────────────────────────┼─────────────────┘
                                                                                 ▼
                                                                   storage/  SQLite (WAL)
                                                                                 ▲ read-only
                                                     app/  Streamlit dashboard ──┘  (never touches RPC)
```

```
amm/         Uniswap v3 math: pure functions, exact integers (v3_math.py), swap loop with tick crossing (pool.py)
collector/   chain.py (RPC), cex.py (feeds), blocks.py (heads), main.py (live), backfill.py (replay), records.py
strategy/    costs.py (cost model, DEX legs), engine.py (sizing + decision), inventory.py, competition.py
storage/     db.py: runs, cex_quotes, evaluations, eval_directions, opportunities, swaps_observed, quoter_checks
app/         dashboard.py (UI), data.py (load + vectorised what-if recompute)
tests/       math, QuoterV2 parity (fixture + live), strategy, CEX parsers, replay, dashboard recompute
scripts/     make_quoter_fixture.py
```

The collector and the dashboard are **separate processes** that communicate only through SQLite in WAL mode.

## Verified addresses

Nothing is hardcoded from memory without a check. The pool is **resolved at startup** with
`UniswapV3Factory.getPool(WETH, USDC, 500)` and logged. Every address below was checked on-chain (contract code
present, `symbol()`/`decimals()`, and `QuoterV2.factory() == Factory`, `QuoterV2.WETH9() == WETH`):

| What | Address |
|---|---|
| Uniswap v3 Factory | `0x1F98431c8aD98523631AE4a59f267346ea31F984` |
| QuoterV2 | `0x61fFE014bA17989E743c5F6cB21bF9697530B21e` |
| WETH (18) | `0x82aF49447D8a07e3bd95BD0d56f35241523fBab1` |
| USDC native (6) | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` (USDC.e is `0xFF97…5CC8`, a different pool) |
| → resolved WETH/USDC 0.05% pool | `0xC6962004f452bE9203591991D15f6b388e09E8D0` (token0 = WETH, tickSpacing = 10) |
| Multicall3 | `0xcA11bde05977b3631167028862bE2a173976CA11` |
| ArbGasInfo precompile | `0x000000000000000000000000000000000000006C` |

## The math: Uniswap v3 swaps

`amm/v3_math.py` is an operation-by-operation port of `FullMath`, `TickMath`, `SqrtPriceMath` and `SwapMath` from
`Uniswap/v3-core`, with **the same rounding direction on every division**. `amm/pool.py` ports the `swap()` loop.

**State.** The pool stores `sqrtPriceX96 = √P · 2⁹⁶` (Q64.96), where `P = token1/token0` in raw units (USDC-wei per
WETH-wei, so human price = `P · 10¹²`), plus the active liquidity `L` and the current tick, with
`P = 1.0001^tick`.

**Within one tick range** (constant `L`), moving the price from `√Pa` to `√Pb` requires:

```
Δtoken1 = L · (√Pb − √Pa)                       (getAmount1Delta)
Δtoken0 = L · (√Pb − √Pa) / (√Pa · √Pb)         (getAmount0Delta)
```

Given an input amount, the next price is:

```
token1 in:  √P' = √P + Δy / L                                (rounds down)
token0 in:  √P' = L·√P / (L + Δx·√P)                         (rounds up)
```

**The fee** (500 pips = 0.05%) is taken from the input before it moves the price:
`amountIn_effective = amountIn · (1 − fee)`. The fee is charged on the input token, step by step.

**Tick crossing.** Liquidity is piecewise constant between *initialized ticks*. The swap loop:

1. finds the next initialized tick in the swap direction within the current 256-tick bitmap word
   (`nextInitializedTickWithinOneWord`, so word boundaries create step boundaries exactly as on-chain);
2. runs `computeSwapStep` towards that tick's price (or the price limit);
3. if the step reached the tick, it **crosses** it: `L += liquidityNet` going up (`L −= liquidityNet` going down);
4. repeats until the amount is exhausted.

Large sizes therefore see the real liquidity profile instead of a constant-product approximation.

**Data loading.** At each evaluated block, `chain.load_pool_state` reads, in one or two Multicall3 `eth_call`s
**pinned to that block**: `slot0`, `liquidity`, the `tickBitmap` words around the current tick, and `ticks(t)` for
every initialized tick within `±tick_window` (600 ticks ≈ ±6%). If a quote would need ticks beyond the loaded window,
the simulator raises `OutOfLoadedRange` instead of silently mispricing, and the sizing caps the size at the window edge.

**Parity with QuoterV2.** Local quotes equal `QuoterV2.quoteExactInputSingle` / `quoteExactOutputSingle` to the
wei, including the post-swap `sqrtPriceX96`. The tests cover 15 sizes from 0.01 ETH to 1.5 M USDC (crossing 10+
initialized ticks), both directions, exact-in and exact-out. The collector also re-checks against QuoterV2 live every
`quoter_check_every_n_evals` evaluations and logs any mismatch above `quoter_tolerance_bps` (so far: 0.0 bps).

## Cost model and sizing

Sizes are in **ETH** for both legs, so the round trip is flat in ETH and PnL is in USDC.

| Direction | DEX leg | CEX leg |
|---|---|---|
| **A** `dex_buy` | buy ETH: `exactOutput` USDC→WETH | sell ETH at the **bid** |
| **B** `dex_sell` | sell ETH: `exactInput` WETH→USDC | buy ETH at the **ask** |

```
net = gross edge − DEX fee − CEX taker fee − gas − slippage/latency haircut

DEX fee     inside the swap math (reported separately for attribution)
CEX fee     notional × taker_fee_bps            (default 10 bps = Binance VIP0)
gas (USD)   (swap_gas_units × L2 baseFee × multiplier + l1_calldata_bytes × L1 price/byte) × ETHUSD
haircut     notional × slippage_haircut_bps
```

**Arbitrum gas** has two parts: L2 execution (`baseFeePerGas` from the block × ~130k gas for a single-hop swap) and
**L1 data posting**, priced by the `ArbGasInfo.getPricesInWei()` precompile (wei per calldata byte; refreshed every
60 s) × an assumed ~250 compressed calldata bytes. Since EIP-4844 blobs the L1 component is often close to zero, and
the total cost of a swap is around $0.01. On Arbitrum, gas is not what kills the edge; fees are.

**Optimal size.** For a fixed snapshot, net(q) is concave: the CEX leg is linear in q, the DEX cost is convex (price
impact), and gas is constant. `strategy/engine.py` runs a 32-point **geometric scan** on `[min_size, cap]` and then
**golden-section refinement** around the best point. A test checks it against a 4000-point brute force. The cap is the
tightest of: `max_notional_usd`, **CEX top-of-book size** (unless `assume_deep_liquidity: true`), the loaded tick
window, and **available inventory** (ETH on the selling venue, USDC on the buying venue).

**Decision.** An opportunity is "taken" if `net_usd > threshold_usd` and `net_bps > threshold_bps`.

**Break-even.** The pool price has to deviate from the CEX touch by more than about
`DEX fee (5 bps) + CEX fee (10 bps) + haircut (1 bps) ≈ 16 bps` before *any* size is profitable.

## Realism and validation

- **Latency model.** The decision uses the CEX quote at block arrival. The collector then waits `latency_ms` and
  **re-prices the CEX leg with the bid/ask actually observed after the delay** (same size, same DEX fill), and stores
  `net_delayed`. The dashboard's latency slider repeats this for any latency from the stored quote stream.
- **Competition check.** For each opportunity, the collector reads the pool's `Swap` events in the next
  `competition_blocks` blocks (default 20 ≈ 5 s). The gap counts as **closed by others** if a same-direction swap
  pushed the pool price to the price our optimal trade would have reached. It records the blocks/seconds to close, the
  fraction of the gap closed, and the competitors' volume.
- **Inventory.** Simulated balances per venue (`inventory:` in config). Direction A consumes USDC on the DEX and ETH on
  the CEX; B the reverse. Sizes are capped by balances, a direction **stops when its inventory runs out**, and the run
  reports the **capital actually used** (peak drawdown of each venue/asset bucket).
- **QuoterV2 parity**, live and in tests (see above).
- **Replay validation** (backfill): the reconstructed historical state is compared with `eth_call` at blocks the RPC
  can still serve (see below).

## Replay / backfill mode

```bash
make backfill HOURS=3          # python -m collector.backfill --hours 3 --step 5
```

Public RPCs are usually **not archive nodes** (here `eth_call` fails for state more than a few hours old), but
`eth_getLogs` works over long ranges. The backfill therefore **rebuilds exact historical pool state from events**:

- **tick map**: snapshot `liquidityNet`/`liquidityGross` at the head block, then walk backwards undoing every
  `Mint`/`Burn`;
- **price state**: each `Swap` event carries the post-swap `sqrtPriceX96`, `tick` and `liquidity`. State at block *b*
  = the last swap at or before *b*, plus later in-range `Mint`/`Burn` effects on `liquidity`.

**Validation:** at 4 recent blocks the rebuilt `slot0`, `liquidity`, tick map and QuoterV2 quotes all match
`eth_call` exactly (logged as `replay validation`). The competition check uses the same swap logs.

**Limitations of this mode (be explicit about them):**
- CEX data are **Binance 1 s klines**: no real bid/ask (close ± `synthetic_half_spread_bps`), **no top-of-book size**
  (`assumed_top_qty`), and 1 s granularity, so sub-second latency effects cannot be resolved and intra-second spikes
  are smoothed out. Opportunities will be **under- or over-counted** compared with live tick data.
- The DEX side is exact at the sampled blocks, but samples are every `step_s` seconds, not every block.
- Base fee comes from anchors every ~5 minutes; the L1 price per byte is taken at the head block.
- The time→block mapping is interpolated between anchors (Arbitrum block times vary).
- Ticks outside `tick_window + extra_tick_window` around the head price are not loaded; samples where the price
  drifted beyond that are skipped.

## Dashboard

`make app` opens a dashboard that reads SQLite only (auto-refresh every 10 s for live runs):

1. **KPIs**: cumulative simulated PnL, number of opportunities (and per hour), hit rate after latency, median net edge
   (bps), capital used.
2. **Prices**: CEX bid/ask vs DEX pool price, with both directional spreads in bps and the fee break-even line.
3. **Opportunities table**: time, block, direction, size, gross, DEX fee, CEX fee, gas, haircut, net, net after
   latency, *closed by others within N blocks*, plus the share taken by others and the median time to close.
4. **Cumulative PnL** (by direction, before and after latency) and a **net edge histogram**.
5. **Sensitivity panel**: sidebar sliders for CEX fee, gas multiplier, latency, thresholds, haircut and deep-CEX
   **recompute PnL from stored evaluations live**. Each evaluation stores its size curve (DEX quote at 32 sizes), so
   the optimal size is re-picked under the new costs. Includes a waterfall of where the edge goes and an edge-survival
   curve vs CEX fee tier.
6. **Size curve explorer**: net PnL vs trade size for any evaluated block, with the optimum and the CEX top-of-book cap.

## Tests

```bash
make test           # offline: 50 tests
make test-network   # live QuoterV2 parity at a fresh block
```

- `test_v3_math.py`: TickMath vs Solidity constants and float, rounding direction, swap step, closed-form single range,
  **tick crossing up and down vs a piecewise computation**, exact-in/exact-out consistency, out-of-range guard.
- `test_quoter_parity.py`: **wei-exact parity with QuoterV2** on a recorded on-chain snapshot (offline) and live.
- `test_strategy.py`: cost breakdown, gas (L1 + L2), **optimal size vs brute force**, caps (top of book, notional,
  inventory), inventory drain, competition detection.
- `test_backfill_replay.py`: event replay reconstructs state exactly.
- `test_dashboard_data.py`: the dashboard recompute agrees with the collector's optimum.
- `test_cex_parsers.py`: Binance/Bybit/OKX message parsing.

---

## Findings (template)

*To fill in after running the collector for a meaningful period (ideally ≥ 24 h, covering both quiet and volatile
hours). The dashboard has every number needed.*

| Metric | Value | Notes |
|---|---|---|
| Observation window | … h live (+ … h backfill) | dates, market regime |
| Evaluations | … | at ~1 per second |
| Median \|CEX − DEX\| deviation | … bps | how tightly the pool tracks Binance |
| Opportunities per hour > 0 bps net | … | as-run assumptions (10 bps CEX fee) |
| Opportunities per hour > 5 bps net | … | |
| Same, at a 2 bps CEX fee (MM tier) | … | sensitivity panel |
| Typical opportunity lifetime | … blocks / … s | competition check |
| Share closed by competitors within 20 blocks | … % | |
| Size where the edge disappears | … ETH (median) | size curve explorer |
| Median optimal size / binding cap | … ETH / … | top of book vs price impact |
| PnL at decision vs after 250 ms latency | $… vs $… | |
| Capital used | $… | vs starting inventory |
| Main conclusion | … | e.g. "edge only exists below X bps CEX fees and during volatility spikes" |

## Why real PnL would be lower

- **Competition for the same flow.** Arbitrum has no public mempool. The sequencer orders transactions first come,
  first served, so CEX–DEX arbitrage is a pure **latency race** (co-location with the sequencer, dedicated feeds,
  Timeboost express-lane auctions). Professional searchers close most gaps within a block or two; the competition check
  measures this, but our simulated fill still assumes we would have won.
- **Latency.** Our CEX feed arrives over the public internet and the DEX state over a public RPC. Between seeing the
  block and landing a transaction, both prices move. The latency model only re-prices the CEX leg; the DEX leg can
  also be front-run or partially filled by someone else.
- **CEX fee tiers.** The default 10 bps taker fee (retail VIP0) alone exceeds most deviations. Real desks pay far less,
  which is exactly why they win the trades a retail setup cannot see.
- **Inventory and rebalancing.** Profits accumulate as ETH on one venue and USDC on the other. Rebalancing needs
  withdrawals (CEX withdrawal fees, Arbitrum bridge times or fast-bridge fees) and ties up capital that isn't
  earning. None of that is charged here.
- **Adverse selection.** Gaps appear when the CEX moves first. When our CEX hedge executes, the market has
  often moved further in the same direction (that's why the gap existed), so the hedge fills worse than the touch.
- **Top-of-book depth and hidden costs.** The CEX leg is priced at the touch, capped by its displayed size. Partial
  fills, queue position, API rate limits and failed or reverted DEX transactions (which still pay gas) are not modelled.
- **Model scope.** A single pool and fee tier are considered, with no routing across pools or aggregators, and no
  funding or borrowing costs for the inventory.

---

*Stretch goal not implemented yet: porting `amm/` and the sizing to Rust with PyO3 bindings and benchmarking it
against Python. The current Python sizing takes about 5–15 ms per block.*
