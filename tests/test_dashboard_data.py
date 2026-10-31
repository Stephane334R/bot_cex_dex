"""The dashboard's sensitivity recompute must agree with the collector under as-run assumptions."""

import time

import pytest

from app.data import Assumptions, explode_curves, load_run, recompute
from collector.records import build_rows
from storage.db import Store
from strategy.costs import CostParams, DexQuoter
from strategy.engine import Market, StrategyParams, evaluate
from tests.test_strategy import make_pool


def test_recompute_matches_stored_optimum(tmp_path):
    store = Store(str(tmp_path / "t.sqlite"))
    store.new_run("r", "test", "0xpool", {})
    cp = CostParams(cex_fee_bps=2, haircut_bps=0.5)
    sp = StrategyParams(max_notional_usd=1e6, grid_points=64, assume_deep_liquidity=True)
    pool = make_pool()
    dq = DexQuoter(pool, True)
    t0 = time.time()
    for i, bid in enumerate([2008.0, 2012.0, 1999.0, 1985.0]):
        mkt = Market(bid, bid + 0.1, 1e9, 1e9)
        evals = evaluate(dq, mkt, 0.3, cp, sp, None)
        row, dirs, opps = build_rows("r", t0 + i, 100 + i, int(t0) + i, t0 + i, mkt, dq.mid, pool, 10**7, 0, 0.3,
                                     evals, cp, (mkt.bid, mkt.ask), None)
        store.add_evaluation(row, dirs, opps)

    d = load_run(store.con, "r")
    best = recompute(explode_curves(d["ev"], d["dirs"]), d["quotes"],
                     Assumptions(cex_fee_bps=2, haircut_bps=0.5, gas_mult=1.0, latency_ms=0, threshold_usd=1.0,
                                 deep_cex=True))
    stored = d["dirs"].set_index(["eval_id", "direction"])["net"]
    for r in best.itertuples():
        s = stored.loc[(r.eval_id, r.direction)]
        assert r.net <= s + 1e-6  # the grid can't beat the golden-section optimum
        assert r.net == pytest.approx(s, abs=max(0.02 * abs(s), 0.05))  # ...but is close to it
    assert best["taken"].sum() == len(d["opps"])
