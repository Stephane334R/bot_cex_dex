"""
Streamlit dashboard. Reads SQLite only and never touches the RPC.

    streamlit run app/dashboard.py      (or: make app)

All PnL shown here is a SIMULATED UPPER BOUND (see README, "Why real PnL would be lower").
"""

from __future__ import annotations

import json
import sys
from dataclasses import astuple
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data import (  # noqa: E402
    DEX_BUY, DEX_SELL, Assumptions, capital_used, explode_curves, load_run, load_runs, recompute, size_curve,
)
from config import load_config  # noqa: E402
from storage.db import connect  # noqa: E402

st.set_page_config(page_title="CEX–DEX Arb Shadow Bot", layout="wide")
CFG = load_config()
DIR_LABEL = {DEX_BUY: "A: buy DEX / sell CEX", DEX_SELL: "B: buy CEX / sell DEX"}
COLORS = {DEX_BUY: "#2a9d8f", DEX_SELL: "#e76f51"}


@st.cache_resource
def _con():
    return connect(CFG.storage.path, readonly=True)


@st.cache_data(ttl=5)
def _runs():
    return load_runs(_con())


@st.cache_data(ttl=5)
def _run(run_id: str):
    d = load_run(_con(), run_id)
    d["long"] = explode_curves(d["ev"], d["dirs"]) if len(d["ev"]) else pd.DataFrame()
    return d


@st.cache_data(ttl=30, show_spinner=False)
def _recompute(run_id: str, n_evals: int, assumptions: tuple):
    """Cached what-if recompute; n_evals is part of the key so live runs refresh as data grows."""
    d = _run(run_id)
    return recompute(d["long"], d["quotes"], Assumptions(*assumptions))


if not Path(CFG.storage.path).exists():
    st.error(f"No database at {CFG.storage.path}. Run `make collect` or `make backfill` first.")
    st.stop()

# ------------------------------------------------------------------------------------- sidebar
runs = _runs()
if runs.empty:
    st.warning("No runs yet. Start the collector (`make collect`) or run a backfill (`make backfill`).")
    st.stop()

st.sidebar.title("Run")
labels = {r.run_id: f"{r.run_id}  ({r.mode}, {r.n_evals} evals)" for r in runs.itertuples()}
run_id = st.sidebar.selectbox("Select run", list(labels), format_func=labels.get)
run_cfg = json.loads(runs.set_index("run_id").loc[run_id, "config"])
refresh = st.sidebar.toggle("Auto-refresh (10 s)", value=runs.set_index("run_id").loc[run_id, "mode"] == "live")

st.sidebar.title("Sensitivity")
st.sidebar.caption("Recomputes PnL from stored evaluations (size curves + CEX quotes).")
a = Assumptions(
    cex_fee_bps=st.sidebar.slider("CEX taker fee (bps)", 0.0, 15.0, float(run_cfg["cex"]["taker_fee_bps"]), 0.5),
    gas_mult=st.sidebar.slider("Gas multiplier ×", 0.0, 50.0, 1.0, 0.5),
    latency_ms=st.sidebar.slider("Latency (ms)", 0, 5000, int(run_cfg["strategy"]["latency_ms"]), 50),
    threshold_usd=st.sidebar.slider("Threshold (USD)", 0.0, 50.0, float(run_cfg["strategy"]["threshold_usd"]), 0.5),
    threshold_bps=st.sidebar.slider("Threshold (bps)", 0.0, 20.0, float(run_cfg["strategy"]["threshold_bps"]), 0.5),
    haircut_bps=st.sidebar.slider("Slippage haircut (bps)", 0.0, 10.0,
                                  float(run_cfg["strategy"]["slippage_haircut_bps"]), 0.25),
    deep_cex=st.sidebar.toggle("Assume deep CEX liquidity", value=bool(run_cfg["cex"]["assume_deep_liquidity"])),
)
competition_n = run_cfg["collector"]["competition_blocks"]


# ------------------------------------------------------------------------------------- body
def kpi_row(opps: pd.DataFrame, ev: pd.DataFrame):
    c = st.columns(6)
    pnl = opps["net"].sum() if len(opps) else 0.0
    hours = max((ev["ts"].iloc[-1] - ev["ts"].iloc[0]) / 3600, 1e-9) if len(ev) > 1 else np.nan
    hit = (opps["net_delayed"] > 0).mean() * 100 if len(opps) and opps["net_delayed"].notna().any() else np.nan
    eth = ev["bid"].iloc[-1] if len(ev) else 0
    c[0].metric("Cumulative simulated PnL", f"${pnl:,.2f}", help="Simulated upper bound, as-run assumptions")
    c[1].metric("Opportunities", f"{len(opps):,}", f"{len(opps) / hours:.1f}/h" if hours == hours else None)
    c[2].metric("Hit rate after latency", "–" if hit != hit else f"{hit:.0f}%",
                help="Share of opportunities still profitable when the CEX leg is re-priced after the latency")
    c[3].metric("Median net edge", f"{opps['net_bps'].median():.2f} bps" if len(opps) else "–")
    c[4].metric("Capital used", f"${capital_used(opps, eth):,.0f}", help="Peak drawdown of each venue/asset bucket")
    c[5].metric("Evaluations", f"{len(ev):,}", f"{hours:.2f} h" if hours == hours else None)


def price_chart(ev: pd.DataFrame):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.62, 0.38], vertical_spacing=0.04)
    fig.add_trace(go.Scatter(x=ev["time"], y=ev["bid"], name="CEX bid", line=dict(width=1, color="#457b9d")), 1, 1)
    fig.add_trace(go.Scatter(x=ev["time"], y=ev["ask"], name="CEX ask", line=dict(width=1, color="#a8dadc")), 1, 1)
    fig.add_trace(go.Scatter(x=ev["time"], y=ev["dex_mid"], name="DEX mid (pool price)",
                             line=dict(width=1.5, color="#e63946")), 1, 1)
    fig.add_trace(go.Scatter(x=ev["time"], y=ev["spread_buy_bps"], name="bid − DEX (A)",
                             line=dict(width=1, color=COLORS[DEX_BUY])), 2, 1)
    fig.add_trace(go.Scatter(x=ev["time"], y=ev["spread_sell_bps"], name="DEX − ask (B)",
                             line=dict(width=1, color=COLORS[DEX_SELL])), 2, 1)
    be = run_cfg["dex"]["fee_tier"] / 100 + a.cex_fee_bps + a.haircut_bps
    fig.add_hline(y=be, line=dict(dash="dot", color="grey"), row=2, col=1,
                  annotation_text=f"fee breakeven ≈ {be:.1f} bps", annotation_position="top left")
    fig.update_yaxes(title_text="USDC per ETH", row=1, col=1)
    fig.update_yaxes(title_text="spread (bps)", row=2, col=1)
    fig.update_layout(height=520, margin=dict(t=20, b=10), legend=dict(orientation="h", y=1.06))
    st.plotly_chart(fig, width="stretch")


def opportunities_table(opps: pd.DataFrame):
    if opps.empty:
        st.info("No opportunities above the threshold in this run (with as-run assumptions).")
        return
    t = opps[["time", "block", "direction", "size", "notional", "gross", "dex_fee", "cex_fee", "gas", "haircut",
              "net", "net_bps", "net_delayed", "closed_by_others", "closed_after_blocks", "gap_closed_frac"]].copy()
    t["direction"] = t["direction"].map(DIR_LABEL)
    t["closed_by_others"] = t["closed_by_others"].map({1: "yes", 0: "no"}).fillna("pending")
    t = t.rename(columns={"size": "size (ETH)", "net_delayed": "net after latency",
                          "closed_by_others": f"closed by others ≤{competition_n} blk"})
    st.dataframe(t.sort_values("time", ascending=False), width="stretch", height=320, hide_index=True,
                 column_config={c: st.column_config.NumberColumn(format="%.2f") for c in
                                ["notional", "gross", "dex_fee", "cex_fee", "gas", "haircut", "net", "net_bps",
                                 "net after latency"]} | {"size (ETH)": st.column_config.NumberColumn(format="%.4f"),
                                                          "gap_closed_frac": st.column_config.NumberColumn(format="%.2f")})
    chk = opps[opps["closed_by_others"].notna()]
    if len(chk):
        closed = chk[chk["closed_by_others"] == 1]
        c = st.columns(3)
        c[0].metric("Taken by others", f"{len(closed) / len(chk) * 100:.0f}%", help=f"within {competition_n} blocks")
        c[1].metric("Median blocks to close", f"{closed['closed_after_blocks'].median():.0f}" if len(closed) else "–")
        c[2].metric("Median seconds to close", f"{closed['closed_after_s'].median():.1f}"
                    if len(closed) and closed["closed_after_s"].notna().any() else "–")


def pnl_charts(opps: pd.DataFrame):
    c1, c2 = st.columns(2)
    fig = go.Figure()
    if len(opps):
        for d in (DEX_BUY, DEX_SELL):
            o = opps[opps["direction"] == d]
            fig.add_trace(go.Scatter(x=o["time"], y=o["net"].cumsum(), name=DIR_LABEL[d], line=dict(color=COLORS[d])))
        fig.add_trace(go.Scatter(x=opps["time"], y=opps["net"].cumsum(), name="total", line=dict(color="black")))
        if opps["net_delayed"].notna().any():
            fig.add_trace(go.Scatter(x=opps["time"], y=opps["net_delayed"].fillna(0).cumsum(), name="total after latency",
                                     line=dict(color="black", dash="dot")))
    fig.update_layout(title="Cumulative simulated PnL (USD, upper bound)", height=360, margin=dict(t=40, b=10))
    c1.plotly_chart(fig, width="stretch")
    fig = go.Figure()
    if len(opps):
        for d in (DEX_BUY, DEX_SELL):
            fig.add_trace(go.Histogram(x=opps.loc[opps["direction"] == d, "net_bps"], name=DIR_LABEL[d],
                                       marker_color=COLORS[d], opacity=0.75, nbinsx=40))
    fig.update_layout(title="Net edge of opportunities (bps)", barmode="overlay", height=360, margin=dict(t=40, b=10))
    c2.plotly_chart(fig, width="stretch")


def sensitivity(d):
    n = len(d["ev"])
    best = _recompute(run_id, n, astuple(a))
    if best.empty:
        st.info("Nothing to recompute (no size curves feasible under these assumptions).")
        return
    tk = best[best["taken"]]
    c = st.columns(5)
    c[0].metric("Opportunities", f"{len(tk):,}")
    c[1].metric("PnL at decision", f"${tk['net'].sum():,.2f}")
    c[2].metric("PnL after latency", f"${tk['net_realized'].sum():,.2f}",
                f"{(tk['net_realized'].sum() - tk['net'].sum()):,.2f}" if len(tk) else None)
    c[3].metric("Still profitable after latency", f"{(tk['net_realized'] > 0).mean() * 100:.0f}%" if len(tk) else "–")
    c[4].metric("Median size (ETH)", f"{tk['size'].median():.3f}" if len(tk) else "–")

    c1, c2 = st.columns(2)
    if len(tk):
        lat_loss = tk["net_realized"].sum() - tk["net"].sum()
        steps = [("Gross edge", tk["gross"].sum()), ("DEX fee", -tk["dex_fee"].sum()),
                 ("CEX fee", -tk["cex_fee"].sum()), ("Gas", -tk["gas"].sum()), ("Haircut", -tk["haircut"].sum()),
                 ("Latency", lat_loss)]
        fig = go.Figure(go.Waterfall(
            x=[s[0] for s in steps] + ["Realized"], y=[s[1] for s in steps] + [0],
            measure=["absolute"] + ["relative"] * (len(steps) - 1) + ["total"],
            connector=dict(line=dict(color="grey"))))
        fig.update_layout(title="Where the edge goes (taken trades, USD)", height=380, margin=dict(t=40, b=10))
        c1.plotly_chart(fig, width="stretch")
    # Edge survival: how many opportunities remain as the CEX fee rises.
    fees = np.arange(0, 15.5, 0.5)
    surv = []
    for f in fees:
        b = _recompute(run_id, n, astuple(Assumptions(**{**a.__dict__, "cex_fee_bps": float(f)})))
        bt = b[b["taken"]] if len(b) else b
        surv.append((len(bt), bt["net_realized"].sum() if len(bt) else 0.0))
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Bar(x=fees, y=[s[0] for s in surv], name="# opportunities", marker_color="#a8dadc"))
    fig.add_trace(go.Scatter(x=fees, y=[s[1] for s in surv], name="PnL after latency", line=dict(color="black")),
                  secondary_y=True)
    fig.update_layout(title="Edge survival vs CEX fee tier (other sliders fixed)", height=380, margin=dict(t=40, b=10),
                      xaxis_title="CEX taker fee (bps)")
    c2.plotly_chart(fig, width="stretch")


def size_explorer(d):
    long, ev = d["long"], d["ev"]
    if long.empty:
        return
    opps = d["opps"]
    choices = (opps.sort_values("net", ascending=False)["eval_id"].drop_duplicates().tolist()[:200]
               + ev["id"].iloc[::-1].tolist()[:200])
    choices = list(dict.fromkeys(choices))
    blocks = ev.set_index("id")["block"]
    eid = st.selectbox("Evaluation (best opportunities first, then most recent blocks)", choices,
                       format_func=lambda i: f"block {blocks.get(i)}  (eval {i})")
    cv = size_curve(long, eid, a)
    fig = go.Figure()
    for dname in (DEX_BUY, DEX_SELL):
        c = cv[cv["direction"] == dname]
        if c.empty:
            continue
        fig.add_trace(go.Scatter(x=c["size"], y=c["net"], mode="lines+markers", name=DIR_LABEL[dname],
                                 line=dict(color=COLORS[dname])))
        i = c["net"].idxmax()
        fig.add_trace(go.Scatter(x=[c.loc[i, "size"]], y=[c.loc[i, "net"]], mode="markers", showlegend=False,
                                 marker=dict(size=14, symbol="star", color=COLORS[dname])))
        book = c["bid_qty"].iloc[0] if dname == DEX_BUY else c["ask_qty"].iloc[0]
        fig.add_vline(x=book, line=dict(dash="dot", color=COLORS[dname]),
                      annotation_text="CEX top of book", annotation_position="top")
    fig.add_hline(y=0, line=dict(color="grey"))
    fig.update_layout(xaxis_type="log", xaxis_title="size (ETH, log)", yaxis_title="net PnL (USD)", height=420,
                      margin=dict(t=20, b=10))
    st.plotly_chart(fig, width="stretch")
    row = d["dirs"][d["dirs"]["eval_id"] == eid][["direction", "size", "net", "net_bps", "cap", "cap_reason"]]
    st.caption("Collector's golden-section optimum (as-run assumptions):")
    st.dataframe(row, hide_index=True, width="stretch")


@st.fragment(run_every="10s" if refresh else None)
def body():
    d = _run(run_id)
    ev, opps = d["ev"], d["opps"]
    st.title("CEX–DEX arbitrage shadow bot: ETH/USDC, Binance vs Uniswap v3 (Arbitrum)")
    st.warning("All PnL is **simulated and an upper bound**. No orders were placed and no transactions were sent.",
               icon="⚠️")
    if ev.empty:
        st.info("Run has no evaluations yet.")
        return
    mode = runs.set_index("run_id").loc[run_id, "mode"]
    if mode == "backfill":
        st.caption("Backfill run: CEX data are 1 s klines with a synthetic spread, not real top of book. "
                   "See README for the limitations.")
    kpi_row(opps, ev)
    st.subheader("Prices")
    price_chart(ev)
    st.subheader("Opportunities (as-run assumptions)")
    opportunities_table(opps)
    pnl_charts(opps)
    st.subheader("Sensitivity: how much edge survives realistic assumptions")
    sensitivity(d)
    st.subheader("Size curve explorer")
    size_explorer(d)
    chk = d["checks"]
    if len(chk):
        st.caption(f"QuoterV2 parity checks: {int(chk['ok'].sum())}/{len(chk)} within tolerance, "
                   f"max diff {chk['diff_bps'].max():.4f} bps")


body()
