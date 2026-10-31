"""
Read-side helpers for the dashboard: load tables and recompute PnL under
user-chosen assumptions from stored evaluations (no RPC, no live data).

The recompute uses the per-evaluation size curve (DEX quote at a geometric grid
of sizes). For each evaluation and direction it re-picks the best grid size
under the new fees/gas, decides with the quote seen at decision time, and
realizes PnL with the CEX quote seen `latency_ms` later.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

import numpy as np
import pandas as pd

DEX_BUY, DEX_SELL = "dex_buy", "dex_sell"


def q(con: sqlite3.Connection, sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)


def load_runs(con) -> pd.DataFrame:
    return q(con, "SELECT r.run_id, r.mode, r.started, r.pool, r.config, "
                  "(SELECT COUNT(*) FROM evaluations e WHERE e.run_id=r.run_id) AS n_evals "
                  "FROM runs r ORDER BY started DESC")


def load_run(con, run_id: str) -> dict[str, pd.DataFrame]:
    ev = q(con, "SELECT * FROM evaluations WHERE run_id=? ORDER BY ts", (run_id,))
    dirs = q(con, "SELECT * FROM eval_directions WHERE run_id=?", (run_id,))
    opps = q(con, "SELECT o.*, d.dex_quote_usd FROM opportunities o "
                  "LEFT JOIN eval_directions d ON d.eval_id=o.eval_id AND d.direction=o.direction "
                  "WHERE o.run_id=? ORDER BY o.ts", (run_id,))
    quotes = q(con, "SELECT ts, bid, ask FROM cex_quotes WHERE run_id=? ORDER BY ts", (run_id,))
    checks = q(con, "SELECT * FROM quoter_checks WHERE run_id=?", (run_id,))
    for df in (ev, opps, quotes):
        if len(df):
            df["time"] = pd.to_datetime(df["ts"], unit="s")
    return dict(ev=ev, dirs=dirs, opps=opps, quotes=quotes, checks=checks)


def capital_used(opps: pd.DataFrame, eth_usd: float) -> float:
    """Peak drawdown per venue/asset bucket from the opportunity flows, valued in USD."""
    if opps.empty:
        return 0.0
    bal = dict(cex_base=0.0, cex_quote=0.0, dex_base=0.0, dex_quote=0.0)
    low = dict(bal)
    for r in opps.itertuples():
        if r.direction == DEX_BUY:
            bal["dex_quote"] -= r.dex_quote_usd
            bal["dex_base"] += r.size
            bal["cex_base"] -= r.size
            bal["cex_quote"] += r.notional - r.cex_fee
        else:
            bal["cex_quote"] -= r.notional + r.cex_fee
            bal["cex_base"] += r.size
            bal["dex_base"] -= r.size
            bal["dex_quote"] += r.dex_quote_usd
        for k, v in bal.items():
            low[k] = min(low[k], v)
    return -(low["cex_base"] + low["dex_base"]) * eth_usd - low["cex_quote"] - low["dex_quote"]


@dataclass
class Assumptions:
    cex_fee_bps: float = 10.0
    gas_mult: float = 1.0
    latency_ms: float = 250.0
    threshold_usd: float = 1.0
    threshold_bps: float = 0.0
    haircut_bps: float = 1.0
    deep_cex: bool = False


def explode_curves(ev: pd.DataFrame, dirs: pd.DataFrame) -> pd.DataFrame:
    """Long table: one row per (evaluation, direction, grid size)."""
    rows = []
    for r in dirs[["eval_id", "direction", "curve"]].itertuples(index=False):
        pts = json.loads(r.curve) if r.curve else []
        for size, dex_usd, fee_usd in pts:
            rows.append((r.eval_id, r.direction, size, dex_usd, fee_usd))
    long = pd.DataFrame(rows, columns=["eval_id", "direction", "size", "dex_usd", "dex_fee"])
    cols = ["id", "ts", "block", "bid", "ask", "bid_qty", "ask_qty", "gas_usd"]
    return long.merge(ev[cols].rename(columns={"id": "eval_id"}), on="eval_id")


def net_for(direction: np.ndarray, size, dex_usd, bid, ask, gas, a: Assumptions):
    buy = direction == DEX_BUY
    px = np.where(buy, bid, ask)
    notional = size * px
    cex_fee = notional * a.cex_fee_bps / 1e4
    hair = notional * a.haircut_bps / 1e4
    g = gas * a.gas_mult
    net = np.where(buy, notional - dex_usd, dex_usd - notional) - cex_fee - hair - g
    return net, notional, cex_fee, hair, g


def recompute(long: pd.DataFrame, quotes: pd.DataFrame, a: Assumptions) -> pd.DataFrame:
    """Best size per (evaluation, direction) under assumptions `a`, plus latency-realized PnL."""
    if long.empty:
        return pd.DataFrame()
    df = long.copy()
    if not a.deep_cex:
        book = np.where(df["direction"] == DEX_BUY, df["bid_qty"], df["ask_qty"])
        df = df[df["size"] <= book]
    if df.empty:
        return pd.DataFrame()
    net, notional, cex_fee, hair, g = net_for(df["direction"].values, df["size"].values, df["dex_usd"].values,
                                              df["bid"].values, df["ask"].values, df["gas_usd"].values, a)
    df = df.assign(net=net, notional=notional, cex_fee=cex_fee, haircut=hair, gas=g)
    best = df.loc[df.groupby(["eval_id", "direction"])["net"].idxmax()].copy()
    best["net_bps"] = best["net"] / best["notional"] * 1e4
    best["taken"] = (best["net"] > a.threshold_usd) & (best["net_bps"] > a.threshold_bps)
    best["gross"] = best["net"] + best["dex_fee"] + best["cex_fee"] + best["gas"] + best["haircut"]

    # Latency: CEX leg re-priced at the last quote seen at ts + latency.
    if len(quotes) and a.latency_ms > 0:
        best = best.sort_values("ts")
        best["ts_delayed"] = best["ts"] + a.latency_ms / 1000
        qd = quotes[["ts", "bid", "ask"]].rename(columns={"ts": "ts_delayed", "bid": "bid_d", "ask": "ask_d"})
        best = pd.merge_asof(best, qd.sort_values("ts_delayed"), on="ts_delayed", direction="backward")
        best["bid_d"] = best["bid_d"].fillna(best["bid"])
        best["ask_d"] = best["ask_d"].fillna(best["ask"])
    else:
        best["bid_d"], best["ask_d"] = best["bid"], best["ask"]
    best["net_realized"], *_ = net_for(best["direction"].values, best["size"].values, best["dex_usd"].values,
                                       best["bid_d"].values, best["ask_d"].values, best["gas_usd"].values, a)
    return best.sort_values("ts").reset_index(drop=True)


def size_curve(long: pd.DataFrame, eval_id: int, a: Assumptions) -> pd.DataFrame:
    df = long[long["eval_id"] == eval_id].copy()
    if df.empty:
        return df
    df["net"], *_ = net_for(df["direction"].values, df["size"].values, df["dex_usd"].values,
                            df["bid"].values, df["ask"].values, df["gas_usd"].values, a)
    return df
