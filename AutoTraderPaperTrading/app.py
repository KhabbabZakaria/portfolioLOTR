"""
AutoTrader — True Paper Trading
================================
Live flow:
  1. On START → fetch warmup bars from Alpaca (last 50 x 1-min bars per ticker)
  2. Run engine on warmup bars to prime indicators (no orders sent)
  3. Every 60s → fetch the latest completed 1-min bar from Alpaca
  4. Run engine on that bar → if BUY/SELL signal fires, send order to Alpaca paper account
  5. UI polls /api/state every 3s to refresh charts and stats

No CSV files. No data_api. Just Alpaca.
"""

import os
import json
import time
import threading
import logging
from datetime import datetime

from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv

from engine import Lane, process_bar
from alpaca_bridge import bridge
from live_feed import (
    fetch_warmup_bars, fetch_latest_bar,
    is_market_open, seconds_until_market_open,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("autotrader")

app = Flask(__name__)
CORS(app)

# ── Persistence: save/load trader config so restarts don't lose state ──────────
_PERSIST_FILE = os.path.join(os.path.dirname(__file__), ".trader_state.json")

def _save_config(body: dict):
    """Persist the /api/start payload so we can auto-restore after a crash."""
    try:
        with open(_PERSIST_FILE, "w") as f:
            json.dump(body, f)
    except Exception as e:
        logger.warning(f"Could not save trader state: {e}")

def _clear_config():
    """Remove persisted config (called on user-initiated stop)."""
    try:
        if os.path.exists(_PERSIST_FILE):
            os.remove(_PERSIST_FILE)
    except Exception as e:
        logger.warning(f"Could not clear trader state: {e}")

def _load_config() -> dict | None:
    """Return saved config dict if it exists, else None."""
    try:
        if os.path.exists(_PERSIST_FILE):
            with open(_PERSIST_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load trader state: {e}")
    return None

# ── Global state ──────────────────────────────────────────────────────────────
state = {
    "running":        False,
    "status":         "idle",       # idle | warming | waiting | live | done | error
    "portfolio":      [],           # Lane objects
    "cfg":            {},
    "equity":         [],
    "peak_equity":    0.0,
    "max_dd":         0.0,
    "log":            [],
    "alpaca_orders":  [],
    "send_to_alpaca": False,
    "last_bar_time":  {},           # ticker → last bar timestamp (dedup)
}

_lock   = threading.Lock()
_thread = None


# ── Logging helper ────────────────────────────────────────────────────────────
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


# ── Core: process one bar through engine + optionally fire Alpaca order ───────
def handle_bar(lane: Lane, bar: dict, cfg: dict, send_alpaca: bool):
    result = process_bar(lane, bar, cfg)

    if not send_alpaca:
        return result

    # BUY — only send if Alpaca doesn't already hold this ticker
    if result["buy_pt"] and lane.position:
        shares = lane.position.shares
        existing_pos = bridge.get_positions()
        already_held = any(p["symbol"] == lane.ticker.upper() and p["qty"] > 0
                           for p in existing_pos)
        if already_held:
            log_event(f"[SKIP] BUY {lane.ticker} — Alpaca already holds a position, skipping duplicate order", "error")
        else:
            resp  = bridge.place_buy(lane.ticker, shares)
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

    # SELL
    if result["sell_pt"] and result["trade"]:
        resp  = bridge.close_position(lane.ticker)
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


# ── Build lanes + cfg from a persisted/request body dict ──────────────────────
def _build_lanes_and_cfg(body: dict):
    capital = float(body.get("capital", 10000))
    stocks  = body.get("stocks", [])
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
    return lanes, cfg, capital


# ── Warmup + reset + sync helper (called every trading day) ──────────────────
def _warmup_and_reset(portfolio, cfg) -> bool:
    """
    Fetch warmup bars, reset daily state, sync existing Alpaca positions.
    Retries up to 3 times on failure. Returns True on success, False on failure.
    """
    set_status("warming")
    log_event("Fetching warmup bars from Alpaca (priming indicators)…")
    for lane in portfolio:
        success = False
        for attempt in range(1, 4):
            try:
                warmup_bars = fetch_warmup_bars(lane.ticker, n=50)
                log_event(f"  {lane.ticker}: {len(warmup_bars)} warmup bars loaded")
                for bar in warmup_bars:
                    process_bar(lane, bar, cfg)
                success = True
                break
            except Exception as e:
                import traceback
                logger.error(f"Warmup traceback:\n{traceback.format_exc()}")
                log_event(f"Warmup attempt {attempt}/3 failed for {lane.ticker}: {type(e).__name__}: {e}", "error")
                if attempt < 3:
                    log_event(f"  Retrying in 30s…")
                    time.sleep(30)
        if not success:
            log_event(f"Warmup failed for {lane.ticker} after 3 attempts — trader stopped.", "error")
            set_status("error")
            return False

    log_event("Warmup complete — indicators primed, ready to trade.")

    # Reset daily financial state (keep closes[] for indicators)
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

    # Sync existing Alpaca positions so we don't double-buy after a restart
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


# ── Background thread: runs forever until user clicks STOP ───────────────────
def run_live():
    with _lock:
        portfolio   = state["portfolio"]
        cfg         = state["cfg"]
        send_alpaca = state["send_to_alpaca"]

    tickers = [l.ticker for l in portfolio]
    log_event(f"Starting live paper trading for: {', '.join(tickers)}")

    # ── Outer loop: one iteration = one trading day ───────────────────────────
    while True:
        with _lock:
            if not state["running"]:
                break

        # ── Warmup ────────────────────────────────────────────────────────────
        ok = _warmup_and_reset(portfolio, cfg)
        if not ok:
            with _lock:
                state["running"] = False
            break

        # ── Wait for market open ──────────────────────────────────────────────
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

        # ── Inner loop: one iteration = one 1-minute bar ──────────────────────
        while True:
            with _lock:
                if not state["running"]:
                    break

            # Market closed → end of trading day
            if not is_market_open():
                log_event("Market closed for the day.", "done")
                set_status("eod")

                # Close all open positions EOD
                if send_alpaca:
                    for lane in portfolio:
                        if lane.position:
                            resp = bridge.close_position(lane.ticker)
                            log_event(
                                f"[ALPACA] EOD close {lane.ticker} — "
                                f"{'✓' if resp['success'] else resp.get('error','')}",
                                "sell" if resp.get("success") else "error"
                            )

                # Sleep until next market open (overnight)
                secs = seconds_until_market_open()
                hrs  = int(secs // 3600)
                mins = int((secs % 3600) // 60)
                log_event(f"Sleeping overnight — next open in {hrs}h {mins}m…")
                set_status("waiting")

                if not _sleep_interruptible(int(secs) - 120):
                    # User hit STOP during overnight sleep
                    log_event("Stopped during overnight sleep.")
                    return

                # 2 minutes before open — break inner loop to re-warmup
                log_event("2 minutes to market open — running warmup for new day…")
                break   # → back to outer loop (re-warmup)

            # Fetch and process latest bar for each ticker
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

            # Update combined equity snapshot
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

            # Sleep until next minute boundary
            now   = time.time()
            sleep = 60 - (now % 60) + 2
            log_event(f"Next bar in {sleep:.0f}s…")
            if not _sleep_interruptible(int(sleep)):
                break   # user hit STOP

    log_event("Live trading thread exited.")


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/alpaca/status")
def alpaca_status():
    return jsonify(bridge.status())


@app.route("/api/alpaca/positions")
def alpaca_positions():
    return jsonify({"positions": bridge.get_positions()})


@app.route("/api/alpaca/orders")
def alpaca_orders():
    with _lock:
        local = list(state["alpaca_orders"])
    live = bridge.get_recent_orders(limit=20)
    return jsonify({"local_signals": local, "alpaca_orders": live})


def _start_from_body(body: dict) -> list:
    """Shared logic for starting the trader from a config body. Returns lanes."""
    global _thread
    lanes, cfg, capital = _build_lanes_and_cfg(body)

    with _lock:
        state["portfolio"]     = lanes
        state["cfg"]           = cfg
        state["equity"]        = []
        state["peak_equity"]   = capital
        state["max_dd"]        = 0.0
        state["log"]           = []
        state["alpaca_orders"] = []
        state["last_bar_time"] = {}
        state["send_to_alpaca"]= body.get("sendToAlpaca", False)
        state["running"]       = True
        state["status"]        = "starting"

    _thread = threading.Thread(target=run_live, daemon=True)
    _thread.start()
    return lanes


@app.route("/api/start", methods=["POST"])
def start():
    with _lock:
        if state["running"]:
            return jsonify({"error": "Already running"}), 400

    body = request.json or {}
    if not body.get("stocks"):
        return jsonify({"error": "No stocks provided"}), 400

    _save_config(body)
    lanes = _start_from_body(body)
    return jsonify({"ok": True, "tickers": [l.ticker for l in lanes]})


@app.route("/api/stop", methods=["POST"])
def stop():
    _clear_config()   # user explicitly stopped — don't auto-restore on next restart
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


@app.route("/api/state")
def get_state():
    # Pull live data from Alpaca — the source of truth
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

            # Real position from Alpaca
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

            # Real equity = starting capital + closed P&L + open unrealized
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
        })


if __name__ == "__main__":
    port = int(os.getenv("traderPort", 5100))
    print(f"\n  AutoTrader Paper Trading  →  http://localhost:{port}")
    alpaca = bridge.status()
    if alpaca.get("connected"):
        print(f"  Alpaca paper account       →  CONNECTED")
        print(f"  Buying power               →  ${float(alpaca['buying_power']):,.2f}")
        print(f"  Portfolio value            →  ${float(alpaca['portfolio_value']):,.2f}")
    else:
        print(f"  Alpaca                     →  NOT CONNECTED: {alpaca.get('error')}")
        print(f"  → Make sure ALPACA_API_KEY and ALPACA_API_SECRET are set in .env")

    # ── Auto-restore: if server crashed while trader was running, restart it ──
    saved = _load_config()
    if saved:
        tickers = [s["ticker"].upper() for s in saved.get("stocks", [])]
        print(f"\n  [AUTO-RESTORE] Resuming trader for: {', '.join(tickers)}")
        print(f"  (Server was restarted while trader was active — restoring automatically)")
        _start_from_body(saved)

    print()
    app.run(debug=False, port=port)