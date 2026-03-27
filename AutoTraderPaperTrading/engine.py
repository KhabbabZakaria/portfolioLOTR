"""
AutoTrader Paper Trading Engine
All strategy logic ported from the JS frontend to Python.
"""
import math
import statistics
from dataclasses import dataclass, field
from typing import Optional


# ─── Strategy helpers ─────────────────────────────────────────────────────────

def calc_rsi(closes: list[float], period: int) -> float:
    if len(closes) < period + 1:
        return 50.0
    ag = al = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            ag += d
        else:
            al -= d
    ag /= period
    al /= period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def std_dev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return statistics.stdev(values)


def signal_for(closes: list[float], strategy: str, params: dict) -> int:
    """Returns 1 (buy), -1 (sell/exit), or 0 (hold)."""
    if strategy == "rsi":
        period = params.get("rsiP", 14)
        os_    = params.get("rsiOs", 30)
        ob_    = params.get("rsiOb", 70)
        if len(closes) < period + 1:
            return 0
        r = calc_rsi(closes, period)
        return 1 if r < os_ else (-1 if r > ob_ else 0)

    if strategy == "ma":
        fast = params.get("maF", 9)
        slow = params.get("maSl", 21)
        if len(closes) < slow + 1:
            return 0
        fn_ = avg(closes[-fast:])
        sn_ = avg(closes[-slow:])
        fp_ = avg(closes[-fast - 1:-1])
        sp_ = avg(closes[-slow - 1:-1])
        return 1 if fp_ < sp_ and fn_ > sn_ else (-1 if fp_ > sp_ and fn_ < sn_ else 0)

    if strategy == "bb":
        period = params.get("bbP", 20)
        mult   = params.get("bbS", 2.0)
        if len(closes) < period:
            return 0
        window = closes[-period:]
        mid    = avg(window)
        sd     = std_dev(window)
        price  = closes[-1]
        return 1 if price < mid - mult * sd else (-1 if price > mid + mult * sd else 0)

    return 0


# ─── Lane (per-stock state) ───────────────────────────────────────────────────

@dataclass
class Position:
    price: float
    shares: int
    cash_after_buy: float
    cost: float
    stop: float
    target: float


@dataclass
class Trade:
    num: int
    ticker: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    shares: int
    pnl: float
    pct: float
    reason: str
    win: bool


@dataclass
class Lane:
    ticker: str
    strategy: str = "rsi"
    params: dict = field(default_factory=lambda: {
        "rsiP": 14, "rsiOs": 30, "rsiOb": 70,
        "maF": 9,   "maSl": 21,
        "bbP": 20,  "bbS": 2.0,
    })
    vwap_filter: bool = True
    capital: float = 10000.0
    weight: float = 100.0

    # runtime state
    cash: float = 0.0
    position: Optional[Position] = None
    closes: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    equity: list = field(default_factory=list)
    returns: list = field(default_factory=list)

    cum_tpv: float = 0.0
    cum_vol: float = 0.0
    vwap: float = 0.0
    current_date: Optional[str] = None
    trades_today: int = 0
    trading_disabled: bool = False
    consecutive_losses: int = 0
    pause_until: Optional[float] = None   # epoch ms
    last_exit_time: Optional[float] = None
    start_of_day_equity: float = 0.0
    peak_equity: float = 0.0
    max_dd: float = 0.0

    def reset(self, capital: float):
        self.cash = capital
        self.capital = capital
        self.position = None
        self.closes = []
        self.trades = []
        self.equity = []
        self.returns = []
        self.cum_tpv = 0.0
        self.cum_vol = 0.0
        self.vwap = 0.0
        self.current_date = None
        self.trades_today = 0
        self.trading_disabled = False
        self.consecutive_losses = 0
        self.pause_until = None
        self.last_exit_time = None
        self.start_of_day_equity = capital
        self.peak_equity = capital
        self.max_dd = 0.0


# ─── Bar processor (mirrors processBar in JS) ─────────────────────────────────

def process_bar(lane: Lane, bar: dict, cfg: dict) -> dict:
    """
    bar = { t: "2024-01-02 09:31", o, h, l, c, v }
    cfg = { posSize, stopLoss, takeProfit, commission, slippage, maxTrades }
    returns { port_val, buy_pt, sell_pt, trade }
    """
    c, h, l, v = bar["c"], bar["h"], bar["l"], bar.get("v", 0)
    lane.closes.append(c)

    current_date = bar["t"][:10]
    if lane.current_date is None:
        lane.current_date = current_date
        lane.start_of_day_equity = lane.capital

    if current_date != lane.current_date:
        lane.current_date = current_date
        lane.trades_today = 0
        last_eq = lane.equity[-1] if lane.equity else lane.capital
        lane.start_of_day_equity = last_eq
        lane.trading_disabled = False
        lane.consecutive_losses = 0
        lane.pause_until = None
        lane.cum_tpv = 0.0
        lane.cum_vol = 0.0
        lane.vwap = 0.0

    # VWAP
    tp = (h + l + c) / 3
    lane.cum_tpv += tp * v
    lane.cum_vol  += v
    if lane.cum_vol > 0:
        lane.vwap = lane.cum_tpv / lane.cum_vol

    bar_time = bar["t"][11:16]   # "HH:MM"
    in_ntz   = bar_time < "09:45" or bar_time >= "15:45"

    import time as _time
    from datetime import datetime
    try:
        now_ts = datetime.strptime(bar["t"], "%Y-%m-%d %H:%M").timestamp() * 1000
    except Exception:
        now_ts = _time.time() * 1000

    buy_pt  = None
    sell_pt = None
    new_trade = None

    commission = cfg.get("commission", 0.0)
    slippage   = cfg.get("slippage", 0.0)

    # ── EOD exit at 15:45 ──────────────────────────────────────────────────────
    if bar_time == "15:45" and lane.position:
        pnl = (c - lane.position.price) * lane.position.shares
        if pnl > 0:
            proceeds = lane.position.shares * c - commission
            lane.cash = lane.position.cash_after_buy + proceeds
            real_pnl  = proceeds - lane.position.cost
            lane.returns.append((c - lane.position.price) / lane.position.price)
            t = Trade(
                num=len(lane.trades) + 1, ticker=lane.ticker,
                entry_time=bar["t"], exit_time=bar["t"],
                entry_price=lane.position.price, exit_price=round(c, 4),
                shares=lane.position.shares, pnl=round(real_pnl, 2),
                pct=round((c - lane.position.price) / lane.position.price * 100, 3),
                reason="END-OF-DAY", win=True,
            )
            lane.consecutive_losses = 0 if real_pnl >= 0 else lane.consecutive_losses + 1
            if lane.consecutive_losses >= 3:
                lane.pause_until = now_ts + 3_600_000
                lane.consecutive_losses = 0
            lane.last_exit_time = now_ts
            lane.trades.append(t)
            lane.position = None
            sell_pt = c
            new_trade = t

    sig = signal_for(lane.closes, lane.strategy, lane.params)

    # ── Exit ──────────────────────────────────────────────────────────────────
    if lane.position and not in_ntz:
        reason = None
        exit_price = c
        if   l <= lane.position.stop:   reason = "STOP-LOSS";   exit_price = lane.position.stop - slippage
        elif h >= lane.position.target: reason = "TAKE-PROFIT"; exit_price = lane.position.target
        elif sig == -1:                 reason = "SIGNAL";       exit_price = c

        if reason:
            proceeds  = lane.position.shares * exit_price - commission
            lane.cash = lane.position.cash_after_buy + proceeds
            pnl       = proceeds - lane.position.cost
            lane.returns.append((exit_price - lane.position.price) / lane.position.price)
            t = Trade(
                num=len(lane.trades) + 1, ticker=lane.ticker,
                entry_time=bar["t"], exit_time=bar["t"],
                entry_price=lane.position.price, exit_price=round(exit_price, 4),
                shares=lane.position.shares, pnl=round(pnl, 2),
                pct=round((exit_price - lane.position.price) / lane.position.price * 100, 3),
                reason=reason, win=pnl > 0,
            )
            lane.consecutive_losses = 0 if pnl >= 0 else lane.consecutive_losses + 1
            if lane.consecutive_losses >= 3:
                lane.pause_until = now_ts + 3_600_000
                lane.consecutive_losses = 0
            lane.last_exit_time = now_ts
            lane.trades.append(t)
            lane.position = None
            sell_pt = exit_price
            new_trade = t

    # ── Daily drawdown guard ──────────────────────────────────────────────────
    port_now = lane.cash + (lane.position.shares * c if lane.position else 0)
    if lane.start_of_day_equity > 0:
        if (port_now - lane.start_of_day_equity) / lane.start_of_day_equity * 100 <= -2:
            lane.trading_disabled = True

    blocked = (
        lane.trading_disabled
        or lane.trades_today >= cfg.get("maxTrades", 4)
        or (lane.pause_until and now_ts < lane.pause_until)
        or (lane.last_exit_time and now_ts - lane.last_exit_time < 300_000)
    )
    vwap_ok = (not lane.vwap_filter) or (c > lane.vwap)

    # ── Entry ─────────────────────────────────────────────────────────────────
    if (not lane.position and sig == 1 and lane.cash > 10
            and not in_ntz and not blocked and vwap_ok):
        entry_px = c + slippage
        shares   = math.floor(lane.cash * (cfg["posSize"] / 100) / entry_px)
        if shares > 0:
            cost = shares * entry_px + commission
            if cost <= lane.cash:
                lane.position = Position(
                    price=entry_px, shares=shares,
                    cash_after_buy=lane.cash - cost, cost=cost,
                    stop=entry_px * (1 - cfg["stopLoss"] / 100),
                    target=entry_px * (1 + cfg["takeProfit"] / 100),
                )
                lane.cash = lane.position.cash_after_buy
                lane.trades_today += 1
                buy_pt = entry_px

    port_val = lane.cash + (lane.position.shares * c if lane.position else 0)
    lane.equity.append(port_val)
    if port_val > lane.peak_equity:
        lane.peak_equity = port_val
    if lane.peak_equity > 0:
        dd = (lane.peak_equity - port_val) / lane.peak_equity * 100
        if dd > lane.max_dd:
            lane.max_dd = dd

    return {"port_val": port_val, "buy_pt": buy_pt, "sell_pt": sell_pt, "trade": new_trade}