"""
New-block listener for Arbitrum.

Prefers a WebSocket `eth_subscribe("newHeads")` (set RPC_WS_URL, e.g. Alchemy).
Falls back to polling `eth_getBlockByNumber("latest")` over HTTP. Arbitrum makes
a block about every 250 ms, so the poller only reports the newest block and
skips any in between (only the latest state matters for pricing).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator

import websockets

from config import log

logger = logging.getLogger("blocks")


@dataclass(frozen=True)
class Head:
    number: int
    timestamp: int  # block timestamp (seconds; Arbitrum has 1 s resolution)
    base_fee: int  # wei per L2 gas
    received: float  # local time we learned about it


class BlockListener:
    def __init__(self, chain, ws_url: str | None, poll_interval: float = 0.25):
        self.chain = chain
        self.ws_url = ws_url
        self.poll_interval = poll_interval

    async def heads(self) -> AsyncIterator[Head]:
        if self.ws_url:
            async for h in self._ws_heads():
                yield h
        else:
            log(logger, "no RPC_WS_URL set: polling for new blocks", interval_s=self.poll_interval)
            async for h in self._poll_heads():
                yield h

    async def _ws_heads(self) -> AsyncIterator[Head]:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.ws_url, open_timeout=10, ping_interval=20) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                                              "params": ["newHeads"]}))
                    ack = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if "error" in ack:
                        raise RuntimeError(ack["error"])
                    log(logger, "subscribed to newHeads", sub_id=ack.get("result"))
                    backoff = 1.0
                    while True:
                        # Arbitrum never pauses for 30 s; a silent socket is a dead socket.
                        msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
                        r = msg.get("params", {}).get("result")
                        if r:
                            yield Head(int(r["number"], 16), int(r["timestamp"], 16),
                                       int(r.get("baseFeePerGas", "0x0"), 16), time.time())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(logger, "newHeads websocket failed; polling until it reconnects", logging.WARNING,
                    error=repr(e)[:200], backoff_s=backoff)
                # Keep the bot alive on HTTP polling while we wait to reconnect.
                deadline = time.time() + backoff
                async for h in self._poll_heads(until=deadline):
                    yield h
                backoff = min(backoff * 2, 60.0)

    async def _poll_heads(self, until: float | None = None) -> AsyncIterator[Head]:
        last = -1
        while until is None or time.time() < until:
            try:
                b = await self.chain.get_block("latest")
                if b["number"] > last:
                    last = b["number"]
                    yield Head(b["number"], b["timestamp"], b.get("baseFeePerGas", 0), time.time())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(logger, "block poll failed", logging.WARNING, error=repr(e)[:200])
                await asyncio.sleep(1.0)
            await asyncio.sleep(self.poll_interval)
