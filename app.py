"""
Portfolio + AutoTrader — Unified Flask App
==========================================
Routes:
  /              → Portfolio homepage
  /classic       → LOTR-themed classic portfolio
  /about         → About page
  /portfolio     → Work experience
  /moreworks     → Additional works

  /secret        → Secret login (password: Thenightsky123@)
  /autotrader    → AutoTrader Paper Trading (requires secret auth)
  /autotrader/api/...  → AutoTrader API endpoints (requires secret auth)
"""

import os
import sys
import time
import threading
import logging
from datetime import datetime
from functools import wraps

# Add AutoTrader directory to path so we can import its modules
_autotrader_dir = os.path.join(os.path.dirname(__file__), 'AutoTraderPaperTrading')
sys.path.insert(0, _autotrader_dir)

from flask import Flask, render_template, redirect, url_for, request, session, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

# Load .env from AutoTrader dir if present, else fall back to repo root
_env_path = os.path.join(_autotrader_dir, '.env')
if not os.path.exists(_env_path):
    _env_path = os.path.join(os.path.dirname(__file__), '.env')
load_dotenv(_env_path)

try:
    from engine import Lane, process_bar
    from alpaca_bridge import bridge
    from live_feed import (
        fetch_warmup_bars, fetch_latest_bar,
        is_market_open, seconds_until_market_open,
    )
    AUTOTRADER_AVAILABLE = True
except ImportError as e:
    AUTOTRADER_AVAILABLE = False
    logger.warning(f"AutoTrader modules unavailable (run with venv): {e}")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("autotrader")

app = Flask(__name__)
app.secret_key = 'portfolioLOTR-secret-key-2024'
CORS(app)

SECRET_PASSWORD = "Thenightsky123@"

# ── AutoTrader global state ────────────────────────────────────────────────────
state = {
    "running":        False,
    "status":         "idle",
    "portfolio":      [],
    "cfg":            {},
    "equity":         [],
    "peak_equity":    0.0,
    "max_dd":         0.0,
    "log":            [],
    "alpaca_orders":  [],
    "send_to_alpaca": False,
    "last_bar_time":  {},
}

_lock   = threading.Lock()
_thread = None


# ── AutoTrader helpers ─────────────────────────────────────────────────────────
def log_event(msg: str, level: str = "info"):
    entry = {"t": datetime.now().strftime("%H:%M:%S"), "msg": msg, "level": level}
    with _lock:
        state["log"].append(entry)
        if len(state["log"]) > 500:
            state["log"].pop(0)
    logger.info(msg)


def set_status(s: str):
    with _lock:
        state["status"] = s


def handle_bar(lane: Lane, bar: dict, cfg: dict, send_alpaca: bool):
    result = process_bar(lane, bar, cfg)

    if not send_alpaca:
        return result

    if result["buy_pt"] and lane.position:
        shares = lane.position.shares
        existing_pos = bridge.get_positions()
        already_held = any(p["symbol"] == lane.ticker.upper() and p["qty"] > 0
                           for p in existing_pos)
        if already_held:
            log_event(f"[SKIP] BUY {lane.ticker} — already held, skipping duplicate order", "error")
        else:
            resp = bridge.place_buy(lane.ticker, shares)
            entry = {
                "t": bar["t"], "ticker": lane.ticker,
                "side": "BUY", "shares": shares, "price": result["buy_pt"],
                **resp,
            }
            with _lock:
                state["alpaca_orders"].append(entry)
            status = "✓" if resp["success"] else f"✗ {resp.get('error','')}"
            log_event(f"[ALPACA] BUY {shares}× {lane.ticker} @ ~${result['buy_pt']:.2f} — {status}",
                      "buy" if resp["success"] else "error")

    if result["sell_pt"] and result["trade"]:
        resp = bridge.close_position(lane.ticker)
        entry = {
            "t": bar["t"], "ticker": lane.ticker,
            "side": "SELL", "shares": result["trade"].shares,
            "price": result["sell_pt"], "reason": result["trade"].reason,
            **resp,
        }
        with _lock:
            state["alpaca_orders"].append(entry)
        status = "✓" if resp["success"] else f"✗ {resp.get('error','')}"
        log_event(f"[ALPACA] SELL {result['trade'].shares}× {lane.ticker} @ ~${result['sell_pt']:.2f} "
                  f"({result['trade'].reason}) — {status}",
                  "sell" if resp["success"] else "error")

    return result


# ── Warmup + reset + Alpaca sync (runs at start of each trading day) ──────────
def _warmup_and_reset(portfolio, cfg) -> bool:
    """Fetch warmup bars, reset daily state, sync existing Alpaca positions.
    Returns True on success, False on failure."""
    set_status("warming")
    log_event("Fetching warmup bars from Alpaca (priming indicators)…")
    for lane in portfolio:
        try:
            warmup_bars = fetch_warmup_bars(lane.ticker, n=50)
            log_event(f"  {lane.ticker}: {len(warmup_bars)} warmup bars loaded")
            for bar in warmup_bars:
                process_bar(lane, bar, cfg)
        except Exception as e:
            import traceback
            logger.error(f"Warmup traceback:\n{traceback.format_exc()}")
            log_event(f"Warmup failed for {lane.ticker}: {type(e).__name__}: {e}", "error")
            set_status("error")
            return False

    log_event("Warmup complete — indicators primed, ready to trade.")

    for lane in portfolio:
        lane.cash               = lane.capital
        lane.position           = None
        lane.equity             = []
        lane.trades             = []
        lane.returns            = []
        lane.trades_today       = 0
        lane.trading_disabled   = False
        lane.consecutive_losses = 0
        lane.pause_until        = None
        lane.last_exit_time     = None
        lane.start_of_day_equity= lane.capital
        lane.peak_equity        = lane.capital
        lane.max_dd             = 0.0
        lane.cum_tpv            = 0.0
        lane.cum_vol            = 0.0
        lane.vwap               = 0.0
        lane.current_date       = None

    existing = {p["symbol"]: p for p in bridge.get_positions()}
    for lane in portfolio:
        ap = existing.get(lane.ticker.upper())
        if ap:
            from engine import Position
            qty      = int(ap["qty"])
            avg_cost = float(ap["avg_cost"])
            lane.position = Position(
                price          = avg_cost,
                shares         = qty,
                cash_after_buy = lane.cash - avg_cost * qty,
                cost           = avg_cost * qty,
                stop           = avg_cost * (1 - cfg.get("stopLoss", 2) / 100),
                target         = avg_cost * (1 + cfg.get("takeProfit", 3) / 100),
            )
            lane.cash = lane.position.cash_after_buy
            log_event(f"  {lane.ticker}: existing position synced — {qty} shares @ ${avg_cost:.2f}")

    return True


def _sleep_interruptible(seconds: int) -> bool:
    """Sleep for `seconds`, waking every second to check if stopped.
    Returns False if stopped mid-sleep, True if completed normally."""
    for _ in range(seconds):
        with _lock:
            if not state["running"]:
                return False
        time.sleep(1)
    return True


# ── Background thread: runs until user clicks STOP (multi-day loop) ───────────
def run_live():
    with _lock:
        portfolio   = state["portfolio"]
        cfg         = state["cfg"]
        send_alpaca = state["send_to_alpaca"]

    tickers = [l.ticker for l in portfolio]
    log_event(f"Starting live paper trading for: {', '.join(tickers)}")

    # Outer loop: one iteration = one trading day
    while True:
        with _lock:
            if not state["running"]:
                break

        ok = _warmup_and_reset(portfolio, cfg)
        if not ok:
            with _lock:
                state["running"] = False
            break

        if not is_market_open():
            secs = seconds_until_market_open()
            hrs  = int(secs // 3600)
            mins = int((secs % 3600) // 60)
            log_event(f"Market closed. Waiting {hrs}h {mins}m until 9:30 ET…")
            set_status("waiting")
            while not is_market_open():
                with _lock:
                    if not state["running"]:
                        log_event("Stopped while waiting for market open.")
                        return
                time.sleep(30)

        log_event("Market is open — trading day started.", "info")
        set_status("live")
        with _lock:
            state["last_bar_time"] = {}   # reset bar dedup for new day

        # Inner loop: one iteration = one 1-minute bar
        while True:
            with _lock:
                if not state["running"]:
                    break

            if not is_market_open():
                log_event("Market closed for the day.", "done")
                set_status("eod")

                if send_alpaca:
                    for lane in portfolio:
                        if lane.position:
                            resp = bridge.close_position(lane.ticker)
                            log_event(
                                f"[ALPACA] EOD close {lane.ticker} — "
                                f"{'✓' if resp['success'] else resp.get('error','')}",
                                "sell" if resp.get("success") else "error"
                            )

                secs = seconds_until_market_open()
                hrs  = int(secs // 3600)
                mins = int((secs % 3600) // 60)
                log_event(f"Sleeping overnight — next open in {hrs}h {mins}m…")
                set_status("waiting")

                if not _sleep_interruptible(int(secs) - 120):
                    log_event("Stopped during overnight sleep.")
                    return

                log_event("2 minutes to market open — running warmup for new day…")
                break   # back to outer loop (re-warmup)

            for lane in portfolio:
                try:
                    bar = fetch_latest_bar(lane.ticker)
                    if bar is None:
                        continue
                    with _lock:
                        last = state["last_bar_time"].get(lane.ticker)
                    if bar["t"] == last:
                        continue
                    with _lock:
                        state["last_bar_time"][lane.ticker] = bar["t"]
                    log_event(f"[BAR] {lane.ticker} {bar['t']}  C:${bar['c']}  V:{bar['v']:,}")
                    handle_bar(lane, bar, cfg, send_alpaca)
                except Exception as e:
                    log_event(f"Error processing {lane.ticker}: {e}", "error")

            with _lock:
                combined = sum(
                    (l.equity[-1] if l.equity else l.capital)
                    for l in state["portfolio"]
                )
                state["equity"].append(combined)
                if combined > state["peak_equity"]:
                    state["peak_equity"] = combined
                if state["peak_equity"] > 0:
                    dd = (state["peak_equity"] - combined) / state["peak_equity"] * 100
                    if dd > state["max_dd"]:
                        state["max_dd"] = dd

            now   = time.time()
            sleep = 60 - (now % 60) + 2
            log_event(f"Next bar in {sleep:.0f}s…")
            if not _sleep_interruptible(int(sleep)):
                break   # user hit STOP

    log_event("Live trading thread exited.")


# ── Auth decorator ─────────────────────────────────────────────────────────────
def require_secret(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not AUTOTRADER_AVAILABLE:
            return "AutoTrader unavailable — run the app with the project venv (./venv/bin/python3 app.py)", 503
        if not session.get('autotrader_auth'):
            return redirect(url_for('secret_login'))
        return f(*args, **kwargs)
    return decorated


# ── Portfolio routes ───────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/classic')
def classic():
    return render_template('lotr_index.html')


@app.route('/about')
def about():
    return render_template('about.html')


@app.route('/portfolio')
def portfolio():
    return render_template('portfolio.html')


@app.route('/moreworks')
def portfolio2():
    return render_template('portfolio2.html')


# ── Secret auth routes ─────────────────────────────────────────────────────────
@app.route('/secret', methods=['GET', 'POST'])
def secret_login():
    if session.get('autotrader_auth'):
        return redirect(url_for('autotrader_index'))
    error = False
    if request.method == 'POST':
        if request.form.get('password') == SECRET_PASSWORD:
            session['autotrader_auth'] = True
            return redirect(url_for('autotrader_index'))
        error = True
    return render_template('secret.html', error=error)


@app.route('/secret/logout')
def secret_logout():
    session.pop('autotrader_auth', None)
    return redirect(url_for('index'))


# ── AutoTrader routes (session-protected) ─────────────────────────────────────
@app.route('/autotrader/')
@app.route('/autotrader')
@require_secret
def autotrader_index():
    return render_template('autotrader.html')


@app.route('/autotrader/api/alpaca/status')
@require_secret
def autotrader_alpaca_status():
    return jsonify(bridge.status())


@app.route('/autotrader/api/alpaca/positions')
@require_secret
def autotrader_alpaca_positions():
    return jsonify({"positions": bridge.get_positions()})


@app.route('/autotrader/api/alpaca/orders')
@require_secret
def autotrader_alpaca_orders():
    with _lock:
        local = list(state["alpaca_orders"])
    live = bridge.get_recent_orders(limit=20)
    return jsonify({"local_signals": local, "alpaca_orders": live})


@app.route('/autotrader/api/start', methods=['POST'])
@require_secret
def autotrader_start():
    global _thread
    with _lock:
        if state["running"]:
            return jsonify({"error": "Already running"}), 400

    body    = request.json or {}
    capital = float(body.get("capital", 10000))
    stocks  = body.get("stocks", [])
    if not stocks:
        return jsonify({"error": "No stocks provided"}), 400

    lanes = []
    for s in stocks:
        weight    = s.get("weight", 100 / len(stocks))
        allocated = capital * weight / 100
        lane = Lane(
            ticker=s["ticker"].upper(),
            strategy=s.get("strategy", "rsi"),
            params=s.get("params", {}),
            vwap_filter=s.get("vwapFilter", True),
            capital=allocated,
        )
        lane.reset(allocated)
        lanes.append(lane)

    cfg = {
        "posSize":    body.get("posSize",    20),
        "stopLoss":   body.get("stopLoss",    2),
        "takeProfit": body.get("takeProfit",  3),
        "commission": body.get("commission",  0),
        "slippage":   body.get("slippage",    0),
        "maxTrades":  body.get("maxTrades",   4),
    }

    with _lock:
        state["portfolio"]      = lanes
        state["cfg"]            = cfg
        state["equity"]         = []
        state["peak_equity"]    = capital
        state["max_dd"]         = 0.0
        state["log"]            = []
        state["alpaca_orders"]  = []
        state["last_bar_time"]  = {}
        state["send_to_alpaca"] = body.get("sendToAlpaca", False)
        state["running"]        = True
        state["status"]         = "starting"

    _thread = threading.Thread(target=run_live, daemon=True)
    _thread.start()
    return jsonify({"ok": True, "tickers": [l.ticker for l in lanes]})


@app.route('/autotrader/api/stop', methods=['POST'])
@require_secret
def autotrader_stop():
    with _lock:
        state["running"] = False
        state["status"]  = "idle"
        portfolio   = list(state["portfolio"])
        send_alpaca = state["send_to_alpaca"]

    if send_alpaca:
        for lane in portfolio:
            if lane.position:
                resp = bridge.close_position(lane.ticker)
                log_event(f"[ALPACA] Closing {lane.ticker} on stop — {'✓' if resp['success'] else resp.get('error','')}",
                          "sell" if resp.get("success") else "error")

    log_event("Stopped by user.")
    return jsonify({"ok": True})


@app.route('/autotrader/api/state')
@require_secret
def autotrader_state():
    alpaca_account   = bridge.status()
    alpaca_positions = {p["symbol"]: p for p in bridge.get_positions()}

    with _lock:
        portfolio = state["portfolio"]
        lanes_out = []
        for lane in portfolio:
            trades_out = [
                {
                    "num":        t.num,
                    "ticker":     t.ticker,
                    "exitTime":   t.exit_time,
                    "entryPrice": t.entry_price,
                    "exitPrice":  t.exit_price,
                    "shares":     t.shares,
                    "pnl":        t.pnl,
                    "pct":        t.pct,
                    "reason":     t.reason,
                    "win":        t.win,
                }
                for t in lane.trades
            ]

            ap = alpaca_positions.get(lane.ticker.upper())
            real_position = None
            if ap:
                real_position = {
                    "price":         ap["avg_cost"],
                    "shares":        ap["qty"],
                    "market_value":  ap["market_value"],
                    "unrealized_pl": ap["unrealized_pl"],
                    "stop":   lane.position.stop   if lane.position else None,
                    "target": lane.position.target if lane.position else None,
                }

            closed_pnl  = sum(t.pnl for t in lane.trades)
            unreal_pnl  = ap["unrealized_pl"] if ap else 0.0
            real_equity = lane.capital + closed_pnl + unreal_pnl

            lanes_out.append({
                "ticker":   lane.ticker,
                "strategy": lane.strategy,
                "capital":  lane.capital,
                "equity":   real_equity,
                "cash":     lane.cash,
                "position": real_position,
                "trades":   trades_out,
                "maxDD":    lane.max_dd,
                "vwap":     lane.vwap,
                "prices":   lane.closes[-120:],
            })

        total_capital = sum(l.capital for l in portfolio) if portfolio else 1

        if alpaca_account.get("connected"):
            total_equity = float(alpaca_account.get("portfolio_value", total_capital))
            buying_power = float(alpaca_account.get("buying_power", 0))
            cash         = float(alpaca_account.get("cash", 0))
        else:
            total_equity = state["equity"][-1] if state["equity"] else total_capital
            buying_power = 0.0
            cash         = 0.0

        open_positions = list(alpaca_positions.values())

        return jsonify({
            "running":        state["running"],
            "status":         state["status"],
            "portfolio":      lanes_out,
            "total_equity":   total_equity,
            "total_capital":  total_capital,
            "buying_power":   buying_power,
            "cash":           cash,
            "open_positions": open_positions,
            "max_dd":         state["max_dd"],
            "log":            state["log"][-50:],
            "alpaca_orders":  state["alpaca_orders"][-20:],
            "send_to_alpaca": state["send_to_alpaca"],
        })


if __name__ == '__main__':
    port = int(os.getenv("PORT", 5000))
    print(f"\n  Portfolio  →  http://localhost:{port}")
    print(f"  Secret     →  http://localhost:{port}/secret")
    alpaca = bridge.status()
    if alpaca.get("connected"):
        print(f"  Alpaca     →  CONNECTED (${float(alpaca.get('portfolio_value', 0)):,.2f})")
    else:
        print(f"  Alpaca     →  NOT CONNECTED: {alpaca.get('error')}")
    print()
    app.run(debug=False, port=port)
