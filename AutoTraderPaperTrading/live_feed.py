"""
live_feed.py — Alpaca live market data (1-minute bars)

Strategy:
  - On startup, fetch the last N bars of history so indicators
    (RSI, MA, BB) have enough warmup data before trading begins.
  - Then poll Alpaca's /v2/stocks/{symbol}/bars endpoint every 60s
    for the latest completed 1-minute bar.
  - Uses IEX feed (free). If you have an Alpaca subscription, change
    feed="iex" to feed="iex".
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
    Looks back up to 5 calendar days to ensure we get enough bars
    even when called before market open (no intraday data yet today).
    Returns list of bar dicts sorted oldest → newest.
    """
    client, StockBarsRequest, TimeFrame = _make_client()

    # Always look back 5 calendar days to cross weekends and pre-market gaps.
    # Alpaca will only return bars from actual trading sessions.
    end   = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
    start = end - timedelta(days=5)

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


def fetch_new_bars(symbol: str, since: str | None) -> list[dict]:
    """
    Fetch all completed 1-minute bars newer than `since` (a "YYYY-MM-DD HH:MM" string).
    Returns list sorted oldest -> newest, skipping any bar whose timestamp <= since.
    Looks back 30 minutes to catch up after gaps.
    """
    client, StockBarsRequest, TimeFrame = _make_client()

    end   = datetime.now(tz=timezone.utc) - timedelta(seconds=30)
    start = end - timedelta(minutes=30)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed="iex",
        limit=30,
    )
    try:
        bars_response = client.get_stock_bars(req)
        raw = bars_response.data.get(symbol, [])
        bars = [_bar_to_dict(b, symbol) for b in raw]
        if since:
            bars = [b for b in bars if b["t"] > since]
        return bars
    except Exception as e:
        logger.error(f"fetch_new_bars({symbol}): {e}")
        return []


def _get_clock():
    """Fetch Alpaca's market clock. Returns clock object or None on error."""
    try:
        from alpaca.trading.client import TradingClient
        api_key    = os.getenv("ALPACA_API_KEY", "")
        api_secret = os.getenv("ALPACA_API_SECRET", "")
        client = TradingClient(api_key, api_secret, paper=True)
        return client.get_clock()
    except Exception as e:
        logger.warning(f"Could not fetch Alpaca clock: {e}")
        return None


def is_market_open() -> bool:
    """Check if NYSE is currently open using Alpaca's clock API.
    Correctly handles holidays and early-close days.
    Falls back to simple time check if API is unreachable."""
    clock = _get_clock()
    if clock is not None:
        return bool(clock.is_open)
    # Fallback: simple ET time check
    now_et = datetime.now(tz=ET)
    if now_et.weekday() >= 5:
        return False
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et < market_close


def seconds_until_market_open() -> float:
    """Return seconds until next NYSE open using Alpaca's clock API.
    Falls back to simple calculation if API is unreachable."""
    clock = _get_clock()
    if clock is not None:
        next_open = clock.next_open
        now_utc   = datetime.now(tz=timezone.utc)
        if hasattr(next_open, 'tzinfo') and next_open.tzinfo is None:
            next_open = next_open.replace(tzinfo=timezone.utc)
        return max(0.0, (next_open - now_utc).total_seconds())
    # Fallback
    now_et = datetime.now(tz=ET)
    candidate = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if candidate <= now_et or now_et.weekday() >= 5:
        candidate += timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    return max(0.0, (candidate - now_et).total_seconds())