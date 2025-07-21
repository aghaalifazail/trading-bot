# Live Test Harness v2 — ChoCH + EMA + SMC Strategy + /status Bot + Shared Capital + Leverage
import os
import time
import pandas as pd
from datetime import datetime, timezone
import requests
import ccxt
import numpy as np
from collections import defaultdict
import threading

# === CONFIG ===
TELEGRAM_TOKEN = "8113167037:AAE1YVWW29wjfkq_CRVKfjOlHaVvcaaoV54"
TELEGRAM_CHAT_ID = "6866669317"
SYMBOLS = ["SOL/USDT", "BTC/USDT", "ETH/USDT"]
TIMEFRAME = '1h'
capital = 500.0  # Shared capital pool
risk_pct = 0.01
LEVERAGE = 50  # Leverage multiplier
ATR_PERIOD = 14
MAX_BARS = 50
EQUITY_LOG_FILE = "live_equity_log.csv"
FEE_RATE = 0.00075
SLIPPAGE = 0.001
TRAIL_ATR_MULT = 1.0

positions = {}
equity_log = []
trade_history = []
last_summary_sent = defaultdict(lambda: None)

exchange = ccxt.binance({"enableRateLimit": True})

# === Telegram ===
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg})

def send_summary():
    now = datetime.now(timezone.utc)
    daily_summary = {}
    for row in trade_history:
        symbol = row['symbol']
        if (now - row['time']).total_seconds() < 86400:
            daily_summary.setdefault(symbol, []).append(row)
    if not daily_summary:
        return
    msg = "\n📊 *Daily PnL Summary*\n"
    for symbol, trades in daily_summary.items():
        pnl = sum([t['pnl'] for t in trades])
        wins = sum([1 for t in trades if t['pnl'] > 0])
        msg += f"\n{symbol}: PnL: {pnl:.2f}, Trades: {len(trades)}, Win%: {wins/len(trades)*100:.1f}%"
    send_telegram(msg)

# === /status Command Bot ===
def check_bot_commands():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    last_checked = 0
    while True:
        try:
            res = requests.get(url).json()
            if "result" in res:
                for update in res["result"]:
                    if update["update_id"] <= last_checked:
                        continue
                    last_checked = update["update_id"]
                    msg = update.get("message", {}).get("text", "")
                    chat_id = str(update.get("message", {}).get("chat", {}).get("id", ""))
                    if msg.strip() == "/status" and chat_id == TELEGRAM_CHAT_ID:
                        send_status()
        except Exception as e:
            print(f"Bot check error: {e}")
        time.sleep(10)

def send_status():
    msg = f"\n📊 *Live Bot Status*\n\nTotal Capital: ${capital:.2f}"
    for sym in SYMBOLS:
        if sym in positions:
            p = positions[sym]
            msg += f"\n{sym} ↳ {p['side'].upper()} @ {p['entry']:.2f}, SL: {p['sl']:.2f}, Qty: {p['qty']:.4f}, Bars: {p['bars']}"
    send_telegram(msg)

# === Fetch OHLCV ===
def get_ohlcv(symbol):
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=200)
        df = pd.DataFrame(data, columns=["time", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        df.set_index("time", inplace=True)
        return df.astype(float)
    except Exception as e:
        send_telegram(f"⚠️ Failed to fetch data for {symbol}: {e}")
        return None

# === Strategy Logic ===
def run_strategy(df):
    df['ema12'] = df['close'].ewm(span=12).mean()
    df['ema21'] = df['close'].ewm(span=21).mean()
    df['ema_signal'] = np.where(df['ema12'] > df['ema21'], 1, -1)
    df['atr'] = df['high'].rolling(ATR_PERIOD).max() - df['low'].rolling(ATR_PERIOD).min()
    df['swing_high'] = (df['high'] > df['high'].shift(1)) & (df['high'] > df['high'].shift(-1))
    df['swing_low'] = (df['low'] < df['low'].shift(1)) & (df['low'] < df['low'].shift(-1))
    df['choch'] = None
    prev_low, prev_high = None, None
    for i in range(2, len(df)):
        if df['swing_low'].iloc[i]:
            if prev_low is not None and df['low'].iloc[i] > prev_low:
                df.iloc[i, df.columns.get_loc('choch')] = 'bullish'
            prev_low = df['low'].iloc[i]
        if df['swing_high'].iloc[i]:
            if prev_high is not None and df['high'].iloc[i] < prev_high:
                df.iloc[i, df.columns.get_loc('choch')] = 'bearish'
            prev_high = df['high'].iloc[i]

    df['ifvg'] = False
    df['order_block'] = None
    for i in range(2, len(df)):
        if df['low'].iloc[i-2] > df['high'].iloc[i-1]:
            df.iloc[i-1, df.columns.get_loc('ifvg')] = True
        if df['close'].iloc[i-2] < df['open'].iloc[i-2] and df['close'].iloc[i-1] > df['open'].iloc[i-1]:
            df.iloc[i-2, df.columns.get_loc('order_block')] = 'bullish'
        elif df['close'].iloc[i-2] > df['open'].iloc[i-2] and df['close'].iloc[i-1] < df['open'].iloc[i-1]:
            df.iloc[i-2, df.columns.get_loc('order_block')] = 'bearish'

    df['atr_ma'] = df['atr'].rolling(50).mean()
    df['volatility_high'] = df['atr'] > df['atr_ma']
    return df

# === Main Loop ===
def run():
    global capital
    threading.Thread(target=check_bot_commands, daemon=True).start()

# === Heartbeat Every 2 Hours ===
def heartbeat():
    while True:
        send_telegram("✅ Bot is still active and monitoring.")
        time.sleep(7200)

threading.Thread(target=heartbeat, daemon=True).start()

    while True:
        now = datetime.now(timezone.utc)
        for symbol in SYMBOLS:
            df = get_ohlcv(symbol)
            if df is None: continue
            df = run_strategy(df)
            price = df['close'].iloc[-1]
            atr = df['atr'].iloc[-1]
            choch = df['choch'].iloc[-1]
            vol_high = df['volatility_high'].iloc[-1]

            entry_long = choch == 'bullish' and df['ema_signal'].iloc[-1] == 1 and (df['ifvg'].iloc[-1] or df['order_block'].iloc[-1] == 'bullish')
            entry_short = choch == 'bearish' and df['ema_signal'].iloc[-1] == -1 and (df['ifvg'].iloc[-1] or df['order_block'].iloc[-1] == 'bearish')

            if symbol not in positions:
                if entry_long or entry_short:
                    side = 'long' if entry_long else 'short'
                    stop = atr
                    risk_amt = capital * risk_pct
                    base_size = (risk_amt / stop) * LEVERAGE if stop != 0 else 0
                    qty = base_size if vol_high else base_size * 0.5
                    sl = price - stop if side == 'long' else price + stop
                    trail_price = price
                    positions[symbol] = {"side": side, "entry": price, "sl": sl, "qty": qty, "bars": 0, "trail": trail_price}
                    send_telegram(f"{symbol} {side.upper()} ENTRY @ {price:.2f} | SL: {sl:.2f} | Size: {qty:.4f}")

            else:
                pos = positions[symbol]
                pos['bars'] += 1

                if pos['side'] == "long":
                    pos['trail'] = max(pos['trail'], price)
                    pos['sl'] = max(pos['sl'], pos['trail'] - atr * TRAIL_ATR_MULT)
                else:
                    pos['trail'] = min(pos['trail'], price)
                    pos['sl'] = min(pos['sl'], pos['trail'] + atr * TRAIL_ATR_MULT)

                exit_reason = None
                if pos['side'] == "long" and price <= pos['sl']:
                    exit_reason = "SL hit"
                elif pos['side'] == "short" and price >= pos['sl']:
                    exit_reason = "SL hit"
                elif pos['bars'] >= MAX_BARS:
                    exit_reason = "Time"
                elif (pos['side'] == "long" and choch == 'bearish') or (pos['side'] == "short" and choch == 'bullish'):
                    exit_reason = "ChoCH"

                if exit_reason:
                    slippage_price = price * (1 - SLIPPAGE) if pos['side'] == "long" else price * (1 + SLIPPAGE)
                    pnl = (slippage_price - pos['entry']) * pos['qty'] if pos['side'] == "long" else (pos['entry'] - slippage_price) * pos['qty']
                    pnl -= abs(slippage_price * pos['qty']) * FEE_RATE
                    capital += pnl
                    send_telegram(f"EXIT {symbol} {pos['side'].upper()} @ {price:.2f} | PnL: {pnl:.2f} | Reason: {exit_reason}")
                    equity_log.append({"time": now, "symbol": symbol, "balance": capital})
                    trade_history.append({"time": now, "symbol": symbol, "pnl": pnl})
                    pd.DataFrame(equity_log).to_csv(EQUITY_LOG_FILE, index=False)
                    positions.pop(symbol)

        if last_summary_sent['day'] != now.date():
            send_summary()
            last_summary_sent['day'] = now.date()

        time.sleep(3600)

if __name__ == "__main__":
    run()


