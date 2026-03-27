"""
Alpaca Paper Trading Bridge
Sends BUY/SELL signals from the engine to Alpaca's paper trading API.
Uses alpaca-py (the modern SDK).

Set in .env:
    ALPACA_API_KEY=PKxxxxxxxxxxxxxxxx
    ALPACA_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    ALPACA_PAPER=true   (always true for paper trading)
"""
import os
import logging
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("alpaca_bridge")

# ── Try to import alpaca-py ───────────────────────────────────────────────────
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
    from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False
    logger.warning("alpaca-py not installed. Run: pip install alpaca-py")


class AlpacaBridge:
    """
    Thin wrapper around the Alpaca paper trading REST API.
    All orders go to paper trading (BASE_URL = paper.alpaca.markets).
    """

    PAPER_URL = "https://paper-api.alpaca.markets"

    def __init__(self):
        self.api_key    = os.getenv("ALPACA_API_KEY", "")
        self.api_secret = os.getenv("ALPACA_API_SECRET", "")
        self.client: Optional[object] = None
        self.connected  = False
        self._last_error = ""

        if not ALPACA_AVAILABLE:
            self._last_error = "alpaca-py not installed"
            return

        if not self.api_key or not self.api_secret:
            self._last_error = "ALPACA_API_KEY / ALPACA_API_SECRET not set in .env"
            return

        self._connect()

    def _connect(self):
        try:
            # paper=True routes to paper-api.alpaca.markets automatically
            self.client    = TradingClient(self.api_key, self.api_secret, paper=True)
            account        = self.client.get_account()
            self.connected = True
            logger.info(f"Alpaca paper account connected — buying power: ${account.buying_power}")
        except Exception as e:
            self._last_error = str(e)
            logger.error(f"Alpaca connection failed: {e}")

    # ── Public API ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return connection status + account summary."""
        if not self.connected or not self.client:
            return {"connected": False, "error": self._last_error}
        try:
            acct = self.client.get_account()
            return {
                "connected":      True,
                "account_number": acct.account_number,
                "buying_power":   float(acct.buying_power),
                "portfolio_value":float(acct.portfolio_value),
                "cash":           float(acct.cash),
                "paper":          True,
            }
        except Exception as e:
            return {"connected": False, "error": str(e)}

    def place_buy(self, symbol: str, shares: int) -> dict:
        """Market buy `shares` of `symbol`."""
        return self._order(symbol, shares, OrderSide.BUY)

    def place_sell(self, symbol: str, shares: int) -> dict:
        """Market sell `shares` of `symbol`."""
        return self._order(symbol, shares, OrderSide.SELL)

    def close_position(self, symbol: str) -> dict:
        """Liquidate the entire position in `symbol`."""
        if not self.connected or not self.client:
            return {"success": False, "error": "not connected"}
        try:
            resp = self.client.close_position(symbol)
            return {"success": True, "order_id": resp.id}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_positions(self) -> list:
        if not self.connected or not self.client:
            return []
        try:
            return [
                {
                    "symbol":   p.symbol,
                    "qty":      int(p.qty),
                    "avg_cost": float(p.avg_entry_price),
                    "market_value": float(p.market_value),
                    "unrealized_pl": float(p.unrealized_pl),
                }
                for p in self.client.get_all_positions()
            ]
        except Exception:
            return []

    def get_recent_orders(self, limit: int = 50) -> list:
        if not self.connected or not self.client:
            return []
        try:
            req    = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit)
            orders = self.client.get_orders(req)
            return [
                {
                    "id":         str(o.id),
                    "symbol":     o.symbol,
                    "side":       o.side.value,
                    "qty":        float(o.qty or 0),
                    "filled_qty": float(o.filled_qty or 0),
                    "status":     o.status.value,
                    "created_at": str(o.created_at),
                }
                for o in orders
            ]
        except Exception:
            return []

    # ── Internal ──────────────────────────────────────────────────────────────

    def _order(self, symbol: str, shares: int, side) -> dict:
        if not self.connected or not self.client:
            return {"success": False, "error": "not connected"}
        if shares <= 0:
            return {"success": False, "error": "shares must be > 0"}
        try:
            req   = MarketOrderRequest(
                symbol=symbol,
                qty=shares,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            order = self.client.submit_order(req)
            logger.info(f"Order submitted: {side.value} {shares}x {symbol} → {order.id}")
            return {
                "success":  True,
                "order_id": str(order.id),
                "symbol":   symbol,
                "side":     side.value,
                "qty":      shares,
                "status":   order.status.value,
            }
        except Exception as e:
            logger.error(f"Order failed {side.value} {symbol}: {e}")
            return {"success": False, "error": str(e)}


# Singleton — imported by app.py
bridge = AlpacaBridge()