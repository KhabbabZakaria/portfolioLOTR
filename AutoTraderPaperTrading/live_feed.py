"""
live_feed.py — Alpaca live market data (1-minute bars)

Strategy:
  - On startup, fetch the last N bars of history so indicators
    (RSI, MA, BB) have enough warmup data before trading begins.
  - Then poll Alpaca's /v2/stocks/{symbol}/bars endpoint every 60s
    for the latest completed 1-minute bar.
  - Uses IEX feed (free). If you have an Alpaca subscription, change
    feed="iex" to feed="sip".
"""

import os
import logging
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("live_feed")

ET = ZoneInfo("America/New_York")

# How many historical bars to pre-load for indicator warmup
WARMUP_BARS = 50


def _make_client():
    """Create an Alpaca StockHistoricalDataClient (no stream needed for 1-min polling)."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        api_key    = os.getenv("ALPACA_API_KEY", "")
        api_secret = os.getenv("ALPACA_API_SECRET", "")
        if not api_key or not api_secret:
            raise ValueError("ALPACA_API_KEY / ALPACA_API_SECRET not set in .env")
        client = StockHistoricalDataClient(api_key, api_secret)
        return client, StockBarsRequest, TimeFrame
    except ImportError:
        raise ImportError("alpaca-py not installed. Run: pip install alpaca-py")


def _bar_to_dict(bar, symbol: str) -> dict:
    """Convert an Alpaca Bar object to our internal bar format."""
    ts = bar.timestamp
    if hasattr(ts, "astimezone"):
        ts_et = ts.astimezone(ET)
    else:
        ts_et = datetime.fromtimestamp(ts.timestamp(), tz=ET)
    return {
        "t": ts_et.strftime("%Y-%m-%d %H:%M"),
        "o": float(bar.open),
        "h": float(bar.high),
        "l": float(bar.low),
        "c": float(bar.close),
        "v": int(bar.volume),
    }


def fetch_warmup_bars(symbol: str, n: int = WARMUP_BARS) -> list[dict]:
    """
    Fetch the last `n` completed 1-minute bars for warmup.
    Returns list of bar dicts sorted oldest → newest.
    """
    client, StockBarsRequest, TimeFrame = _make_client()

    end   = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    start = end - timedelta(minutes=n * 3)   # extra buffer for gaps/weekends

    from alpaca.data.requests import StockBarsRequest
    req  = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed="iex",
        limit=n,
    )
    bars_response = client.get_stock_bars(req)
    raw = bars_response.data.get(symbol, [])
    bars = [_bar_to_dict(b, symbol) for b in raw]
    bars = bars[-n:]   # keep only last n
    logger.info(f"Warmup: loaded {len(bars)} bars for {symbol}")
    return bars


def fetch_latest_bar(symbol: str) -> dict | None:
    """
    Fetch the single most recently completed 1-minute bar.
    Called every 60 seconds by the polling loop.
    """
    client, StockBarsRequest, TimeFrame = _make_client()

    end   = datetime.now(tz=timezone.utc) - timedelta(seconds=30)
    start = end - timedelta(minutes=3)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed="iex",
        limit=2,
    )
    try:
        bars_response = client.get_stock_bars(req)
        raw = bars_response.data.get(symbol, [])
        if not raw:
            return None
        bar = _bar_to_dict(raw[-1], symbol)
        return bar
    except Exception as e:
        logger.error(f"fetch_latest_bar({symbol}): {e}")
        return None


def is_market_open() -> bool:
    """Check if US equity market is currently open (simple time check)."""
    now_et = datetime.now(tz=ET)
    # Mon–Fri only
    if now_et.weekday() >= 5:
        return False
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et < market_close


def seconds_until_market_open() -> float:
    """Return seconds until next market open (for sleeping)."""
    now_et = datetime.now(tz=ET)
    # Find next weekday at 9:30
    candidate = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if candidate <= now_et or now_et.weekday() >= 5:
        candidate += timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    return max(0.0, (candidate - now_et).total_seconds())