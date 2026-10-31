"""
SQLite storage (WAL mode, so the dashboard can read while the collector writes).

Tables
  runs             one row per collector/backfill run (mode, config snapshot, pool)
  cex_quotes       downsampled CEX top of book (feeds the latency slider)
  evaluations      one row per evaluated block: market snapshot + gas
  eval_directions  per evaluation and direction: optimal size, PnL breakdown, size curve
  opportunities    evaluations whose net PnL cleared the threshold ("would have traded")
  swaps_observed   pool Swap events, used by the competition check
  quoter_checks    local-vs-QuoterV2 parity checks done while running
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, mode TEXT, started REAL, pool TEXT, config TEXT, notes TEXT
);
CREATE TABLE IF NOT EXISTS cex_quotes (
    run_id TEXT, ts REAL, bid REAL, ask REAL, bid_qty REAL, ask_qty REAL
);
CREATE INDEX IF NOT EXISTS ix_cex_quotes ON cex_quotes(run_id, ts);
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, ts REAL, block INTEGER, block_ts INTEGER, cex_ts REAL,
    bid REAL, ask REAL, bid_qty REAL, ask_qty REAL,
    dex_mid REAL, sqrt_price_x96 TEXT, tick INTEGER, liquidity TEXT,
    base_fee_wei INTEGER, l1_price_wei INTEGER, gas_usd REAL,
    spread_buy_bps REAL,  -- (CEX bid - DEX mid) / DEX mid: >0 favours buying on the DEX
    spread_sell_bps REAL  -- (DEX mid - CEX ask) / CEX ask: >0 favours selling on the DEX
);
CREATE INDEX IF NOT EXISTS ix_eval_run ON evaluations(run_id, ts);
CREATE TABLE IF NOT EXISTS eval_directions (
    eval_id INTEGER, run_id TEXT, direction TEXT,
    size REAL, notional REAL, dex_quote_usd REAL, gross REAL, dex_fee REAL, cex_fee REAL,
    gas REAL, haircut REAL, net REAL, net_bps REAL,
    cap REAL, cap_reason TEXT, taken INTEGER,
    bid_delayed REAL, ask_delayed REAL, net_delayed REAL,
    sqrt_after TEXT, curve TEXT,
    PRIMARY KEY (eval_id, direction)
);
CREATE INDEX IF NOT EXISTS ix_evdir_run ON eval_directions(run_id);
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_id INTEGER, run_id TEXT, ts REAL, block INTEGER, direction TEXT,
    size REAL, notional REAL, gross REAL, dex_fee REAL, cex_fee REAL, gas REAL, haircut REAL,
    net REAL, net_bps REAL, net_delayed REAL,
    sqrt_before TEXT, sqrt_after TEXT, inventory TEXT,
    checked INTEGER DEFAULT 0, closed_by_others INTEGER, closed_after_blocks INTEGER,
    closed_after_s REAL, gap_closed_frac REAL, others_swaps INTEGER, others_volume_base REAL
);
CREATE INDEX IF NOT EXISTS ix_opp_run ON opportunities(run_id, ts);
CREATE TABLE IF NOT EXISTS swaps_observed (
    block INTEGER, log_index INTEGER, tx_hash TEXT, ts REAL,
    amount0 TEXT, amount1 TEXT, sqrt_price_x96 TEXT, liquidity TEXT, tick INTEGER,
    PRIMARY KEY (block, log_index)
);
CREATE TABLE IF NOT EXISTS quoter_checks (
    run_id TEXT, ts REAL, block INTEGER, direction TEXT, size REAL,
    local_quote TEXT, onchain_quote TEXT, diff_bps REAL, ok INTEGER
);
"""


def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.executescript(SCHEMA)
    return con


class Store:
    def __init__(self, path: str):
        self.con = connect(path)

    def new_run(self, run_id: str, mode: str, pool: str, config: dict, notes: str = "") -> None:
        self.con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
                         (run_id, mode, time.time(), pool, json.dumps(config, default=str), notes))
        self.con.commit()

    def add_quotes(self, run_id: str, rows: list[tuple]) -> None:
        self.con.executemany("INSERT INTO cex_quotes VALUES (?,?,?,?,?,?)", [(run_id, *r) for r in rows])
        self.con.commit()

    def add_evaluation(self, row: dict, directions: list[dict], opportunities: list[dict]) -> int:
        cur = self.con.execute(
            f"INSERT INTO evaluations ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        eval_id = cur.lastrowid
        for d in directions:
            d = {"eval_id": eval_id, **d}
            self.con.execute(
                f"INSERT INTO eval_directions ({','.join(d)}) VALUES ({','.join('?' * len(d))})", list(d.values()))
        for o in opportunities:
            o = {"eval_id": eval_id, **o}
            self.con.execute(
                f"INSERT INTO opportunities ({','.join(o)}) VALUES ({','.join('?' * len(o))})", list(o.values()))
        self.con.commit()
        return eval_id

    def add_swaps(self, swaps: list[dict]) -> None:
        self.con.executemany(
            "INSERT OR IGNORE INTO swaps_observed VALUES (?,?,?,?,?,?,?,?,?)",
            [(s["block"], s["log_index"], s["tx_hash"], s.get("ts"), str(s["amount0"]), str(s["amount1"]),
              str(s["sqrt_price_x96"]), str(s["liquidity"]), s["tick"]) for s in swaps])
        self.con.commit()

    def unchecked_opportunities(self, run_id: str, max_block: int) -> list[dict]:
        cur = self.con.execute(
            "SELECT id, block, ts, direction, sqrt_before, sqrt_after FROM opportunities "
            "WHERE run_id=? AND checked=0 AND block<=? ORDER BY block", (run_id, max_block))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def swaps_between(self, lo: int, hi: int) -> list[dict]:
        cur = self.con.execute("SELECT * FROM swaps_observed WHERE block>? AND block<=? ORDER BY block, log_index",
                               (lo, hi))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def set_competition(self, opp_id: int, res: dict) -> None:
        self.con.execute(
            "UPDATE opportunities SET checked=1, closed_by_others=?, closed_after_blocks=?, closed_after_s=?, "
            "gap_closed_frac=?, others_swaps=?, others_volume_base=? WHERE id=?",
            (res["closed"], res["after_blocks"], res["after_s"], res["frac"], res["swaps"], res["volume_base"], opp_id))
        self.con.commit()

    def add_quoter_check(self, row: tuple) -> None:
        self.con.execute("INSERT INTO quoter_checks VALUES (?,?,?,?,?,?,?,?,?)", row)
        self.con.commit()
