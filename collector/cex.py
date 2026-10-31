"""
CEX top-of-book feeds behind one interface.

Binance `bookTicker` is the default. Bybit and OKX adapters exist for machines
where Binance is geo-blocked. Every adapter only produces `Quote` objects (best
bid/ask + sizes, stamped with local receive time) and reconnects with
exponential backoff.
"""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import websockets

from config import log

logger = logging.getLogger("cex")


@dataclass(frozen=True)
class Quote:
    venue: str
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float
    ts: float  # local receive time (unix seconds). Exchange timestamps are not comparable across venues.

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)


class CexFeed:
    venue = "base"
    url = ""
    ping_interval: float | None = 20.0

    def __init__(self, symbol: str, history_s: float = 120.0):
        self.symbol = symbol
        self.latest: Quote | None = None
        self.history_s = history_s
        self._hist: deque[Quote] = deque()
        self._hist_ts: deque[float] = deque()
        self.listeners: list[Callable[[Quote], None]] = []
        self.connected = asyncio.Event()

    # -- adapter hooks --------------------------------------------------- #
    def subscribe_msg(self) -> dict | None:
        return None

    def parse(self, msg: str) -> Quote | None:
        raise NotImplementedError

    async def keepalive(self, ws) -> None:
        """Application-level pings for venues that need them (Bybit/OKX)."""
        return None

    # -- shared machinery ------------------------------------------------ #
    def _push(self, q: Quote) -> None:
        if q.bid <= 0 or q.ask <= 0 or q.bid >= q.ask:
            return  # crossed/empty book: ignore rather than evaluate on garbage
        self.latest = q
        self._hist.append(q)
        self._hist_ts.append(q.ts)
        cutoff = q.ts - self.history_s
        while self._hist_ts and self._hist_ts[0] < cutoff:
            self._hist.popleft()
            self._hist_ts.popleft()
        for fn in self.listeners:
            fn(q)

    def quote_at(self, ts: float) -> Quote | None:
        """Latest quote received at or before `ts` (what a trader would see at that instant)."""
        i = bisect.bisect_right(self._hist_ts, ts) - 1
        return self._hist[i] if i >= 0 else None

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, open_timeout=10, ping_interval=self.ping_interval,
                                              max_queue=1024) as ws:
                    sub = self.subscribe_msg()
                    if sub:
                        await ws.send(json.dumps(sub))
                    log(logger, "cex feed connected", venue=self.venue, symbol=self.symbol)
                    self.connected.set()
                    backoff = 1.0
                    ka = asyncio.create_task(self.keepalive(ws))
                    try:
                        async for msg in ws:
                            q = self.parse(msg)
                            if q:
                                self._push(q)
                    finally:
                        ka.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, server closes, etc.
                log(logger, "cex feed disconnected, reconnecting", logging.WARNING,
                    venue=self.venue, error=repr(e)[:200], backoff_s=backoff)
            self.connected.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class BinanceFeed(CexFeed):
    venue = "binance"

    def __init__(self, symbol: str, **kw):
        super().__init__(symbol, **kw)
        self.url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@bookTicker"

    def parse(self, msg: str) -> Quote | None:
        d = json.loads(msg)
        if "b" not in d:
            return None
        return Quote(self.venue, float(d["b"]), float(d["a"]), float(d["B"]), float(d["A"]), time.time())


class BybitFeed(CexFeed):
    venue = "bybit"
    url = "wss://stream.bybit.com/v5/public/spot"
    ping_interval = None

    def subscribe_msg(self) -> dict:
        return {"op": "subscribe", "args": [f"orderbook.1.{self.symbol}"]}

    async def keepalive(self, ws) -> None:
        while True:
            await asyncio.sleep(20)
            await ws.send(json.dumps({"op": "ping"}))

    def parse(self, msg: str) -> Quote | None:
        d = json.loads(msg)
        data = d.get("data")
        if not data or not data.get("b") or not data.get("a"):
            return None
        (bp, bq), (ap, aq) = data["b"][0], data["a"][0]
        return Quote(self.venue, float(bp), float(ap), float(bq), float(aq), time.time())


class OkxFeed(CexFeed):
    venue = "okx"
    url = "wss://ws.okx.com:443/ws/v5/public"
    ping_interval = None

    def subscribe_msg(self) -> dict:
        return {"op": "subscribe", "args": [{"channel": "bbo-tbt", "instId": self.symbol}]}

    async def keepalive(self, ws) -> None:
        while True:
            await asyncio.sleep(20)
            await ws.send("ping")

    def parse(self, msg: str) -> Quote | None:
        if msg == "pong":
            return None
        d = json.loads(msg)
        data = d.get("data")
        if not data or not data[0].get("bids") or not data[0].get("asks"):
            return None
        b, a = data[0]["bids"][0], data[0]["asks"][0]
        return Quote(self.venue, float(b[0]), float(a[0]), float(b[1]), float(a[1]), time.time())


FEEDS = {"binance": BinanceFeed, "bybit": BybitFeed, "okx": OkxFeed}


def make_feed(cfg) -> CexFeed:
    try:
        cls = FEEDS[cfg.cex.venue]
    except KeyError:
        raise ValueError(f"unknown cex venue {cfg.cex.venue!r}; choose from {list(FEEDS)}") from None
    return cls(cfg.cex.symbol)
