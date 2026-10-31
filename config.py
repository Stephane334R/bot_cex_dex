"""Config loading (config.yaml + .env) and structured logging setup."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent


class Cfg(dict):
    """dict with attribute access, recursively."""

    def __getattr__(self, k: str) -> Any:
        try:
            v = self[k]
        except KeyError as e:
            raise AttributeError(k) from e
        return Cfg(v) if isinstance(v, dict) and not isinstance(v, Cfg) else v


def load_config(path: str | os.PathLike | None = None) -> Cfg:
    load_dotenv(ROOT / ".env")
    path = Path(path or os.environ.get("CONFIG", ROOT / "config.yaml"))
    with open(path) as f:
        cfg = Cfg(yaml.safe_load(f))
    cfg["rpc_url"] = os.environ.get("RPC_URL", "https://arb1.arbitrum.io/rpc")
    cfg["rpc_ws_url"] = os.environ.get("RPC_WS_URL") or None
    sp = Path(cfg["storage"]["path"])
    cfg["storage"]["path"] = str(sp if sp.is_absolute() else ROOT / sp)
    return cfg


class JsonFormatter(logging.Formatter):
    """One JSON object per line: easy to grep, easy to ship to a log stack."""

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "lvl": record.levelname,
            "mod": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if extra:
            out.update(extra)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    for noisy in ("websockets", "web3", "urllib3", "asyncio", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log(logger: logging.Logger, msg: str, level: int = logging.INFO, **data: Any) -> None:
    logger.log(level, msg, extra={"data": data})
