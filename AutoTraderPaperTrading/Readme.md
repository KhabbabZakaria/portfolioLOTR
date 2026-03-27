# AutoTrader — Paper Trading with Alpaca

A Python port of the JavaScript backtester with live signal forwarding to
Alpaca's paper trading API.

---

## How to get your Alpaca Paper Trading API keys

1. Go to **https://app.alpaca.markets** and sign in (or sign up — it's free).
2. In the top-left corner, make sure you are on the **Paper** account
   (there is a toggle between "Live" and "Paper" — always use Paper for testing).
3. Click **Overview** in the left sidebar.
4. Scroll down to **Your API Keys** and click **Generate New Key**.
5. Copy the **API Key ID** (starts with `PK…`) and the **Secret Key**.
   The secret is only shown once — save it now.
6. Paste both into your `.env` file:

```
ALPACA_API_KEY=PKxxxxxxxxxxxxxxxxxxxxxxxx
ALPACA_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> **The paper trading base URL** is `https://paper-api.alpaca.markets`.
> The `alpaca-py` SDK handles this automatically when you pass `paper=True`
> to `TradingClient`. You never need to set the URL manually.

---

## Project structure

```
paper_trader/
├── app.py              ← Flask backend (start this)
├── engine.py           ← Trading logic (RSI / MA Cross / Bollinger + VWAP)
├── alpaca_bridge.py    ← Alpaca paper trading wrapper
├── requirements.txt
├── .env                ← Your API keys go here
└── templates/
    └── index.html      ← UI (same dark theme as the original)
```

Your **existing data feed service** (`data_api/app.py`, port 5010) must be
running separately — the paper trader fetches bars from it just like before.

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Fill in .env with your Alpaca paper keys
nano .env

# 3. Start your data feed (in a separate terminal)
cd ../data_api && python app.py

# 4. Start the paper trader
python app.py
# → http://localhost:5100
```

---

## How signals are sent to Alpaca

When you toggle **"Send signals to Alpaca"** in the UI and click Start:

| Engine event | What happens |
|---|---|
| BUY signal fires | `bridge.place_buy(ticker, shares)` → market order on Alpaca paper |
| SELL / Stop-Loss / Take-Profit | `bridge.close_position(ticker)` → closes the position on Alpaca paper |
| End-of-day exit | `bridge.close_position(ticker)` |

All orders are **market orders with `TimeInForce.DAY`**.

The Signal Log panel in the UI shows each order with ✓ (sent) or ✗ (failed).
The Trade Log shows an **SENT** badge on trades that had a matching Alpaca order.

---

## Strategies

All three strategies from the original JS backtester are implemented in `engine.py`:

- **RSI** — buy when RSI < oversold, sell when RSI > overbought  
- **MA Cross** — buy on fast MA crossing above slow MA, sell on crossunder  
- **Bollinger Bands** — buy when price touches lower band, sell at upper band  

Each strategy can have the **VWAP filter** enabled: entries are only taken
when price is above the intraday VWAP.

---

## Risk rules (same as original)

- **Daily drawdown circuit breaker**: if the day's loss exceeds 2%, trading stops for that stock that day.  
- **Consecutive loss pause**: 3 losses in a row → 1-hour cooldown.  
- **Re-entry cooldown**: 5-minute wait after any exit before re-entering.  
- **No-trade zones**: no entries before 09:45 or after 15:45 ET.  
- **EOD exit**: profitable positions are closed at 15:45 ET.