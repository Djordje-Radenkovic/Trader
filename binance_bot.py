# ============ Imports ============
import os, json, time, threading, asyncio, logging, requests, websocket
from dotenv import load_dotenv
from collections import defaultdict
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from binance.spot import Spot as Client
from binance.lib.utils import config_logging
from binance.error import ClientError
import math

# ============ Load Environment Variables ============
load_dotenv()
BOT_TOKEN = os.getenv("BINANCE_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID"))
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

# ============ Global Variables ============
MAIN_LOOP = asyncio.get_event_loop()
config_logging(logging, logging.INFO)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
user_data = defaultdict(lambda: {"rr": 1.5, "risk": 1, "fee": 0.001, "rr_type": "before_fees"})
active_streams = {}

# ============ Informative Commands ============

async def config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("Unauthorized user.")
    user_config = user_data[update.effective_user.id]
    config_text = "\n".join([f"{k} = {v}" for k, v in user_config.items()])
    await update.message.reply_text(f"Your configuration:\n{config_text}")

async def set_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("Unauthorized user.")
    if len(context.args) != 2:
        return await update.message.reply_text("Usage: /write <variable> <value>")
    user_data[update.effective_user.id][context.args[0]] = float(context.args[1])
    await update.message.reply_text(f"Set {context.args[0]} = {context.args[1]}")

async def get_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("Unauthorized user.")
    if len(context.args) != 1:
        return await update.message.reply_text("Usage: /read <variable>")
    value = user_data[update.effective_user.id].get(context.args[0], "Not set")
    await update.message.reply_text(f"{context.args[0]} = {value}")

async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("Unauthorized user.")
    account_info = client.isolated_margin_account(symbols="BTCUSDT")
    btc_balance = account_info['assets'][0]['baseAsset']['netAsset']
    cash_balance = account_info['assets'][0]['quoteAsset']['netAsset']
    await update.message.reply_text(f"BTC  balance: {btc_balance}\nCash balance: {cash_balance}")

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("Unauthorized user.")
    await update.message.reply_text("balance\nclose\nkill\nsell [stop loss]\nbuy [stop loss]\nread [variable]\nwrite [variable] [new value]\nconfig")

# ============ Trade Calculation ============

def pricer(side, SL, RR, Risk, f, rr_type):
    try:
        P = float(client.ticker_price("BTCUSDT")['price'])
        d = 1 if side == "buy" else -1
        if (side == 'buy' and SL > P) or (side == 'sell' and SL < P): return None
        info = client.isolated_margin_account(symbols="BTCUSDT")['assets'][0]
        n = Risk / (SL*(f-d) + P*(f+d))
        V = n * P
        TP = P + RR*(P - SL) if rr_type == 'before_fees' else (Risk * RR + n*P*(f+d)) / (n*(d-f))
        return {"price": P, "cryptoBalanceBefore": info['baseAsset']['netAsset'], "cashBalanceBefore": info['quoteAsset']['netAsset'], "takeProfit": f"{TP:.0f}", "size": f"{n:.5f}", "funds": f"{V:.2f}"}
    except:
        return None

# ============ Buy/Sell Commands ============

async def trade(update: Update, context: ContextTypes.DEFAULT_TYPE, side):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("Unauthorized user.")
    SL = float(context.args[0])
    config = user_data[update.effective_user.id]
    result = pricer(side, SL, config['rr'], config['risk'], config['fee'], config['rr_type'])
    if not result:
        return await update.message.reply_text("Pricer empty, didn't execute")

    try:
        order = client.new_margin_order(symbol="BTCUSDT", side=side.upper(), type="MARKET", quantity=result['size'], sideEffectType="AUTO_BORROW_REPAY", isIsolated=True)
        time.sleep(4)
        oco = client.new_margin_oco_order(symbol="BTCUSDT", side="SELL" if side=="buy" else "BUY", quantity=order['executedQty'], price=result['takeProfit'], stopPrice=str(SL), sideEffectType="AUTO_BORROW_REPAY", isIsolated=True)
        fills = [float(fill['price']) for fill in order['fills']]
        avg_price = sum(fills) / len(fills)
        for o in oco['orderReports']:
            if o['type'] == 'STOP_LOSS': SL_exec = o['stopPrice']
            else: TP_exec = o['price']
        await update.message.reply_text(f"BTC before: {result['cryptoBalanceBefore']}\nCash before: {result['cashBalanceBefore']}\nBought {order['executedQty']} BTC at {avg_price:.0f}\nSL: {SL_exec} | TP: {TP_exec}")
    except:
        return await update.message.reply_text("Binance order error")

    symbol = 'BTCUSDT'
    lk = requests.post('https://api.binance.com/sapi/v1/userDataStream/isolated', headers={'X-MBX-APIKEY': BINANCE_API_KEY}, params={'symbol': symbol}).json()['listenKey']

    def on_msg(ws, msg):
        d = json.loads(msg)
        if d.get('e') == 'executionReport' and d.get('X') == 'FILLED':
            price = round(max([float(d['p']), float(d['P']), float(d['L'])]), 0)
            order_type = 'Take Profit' if d['o'] == 'LIMIT_MAKER' else 'Stop Loss'
            account_info = client.isolated_margin_account(symbols="BTCUSDT")
            btc_balance = account_info['assets'][0]['baseAsset']['netAsset']
            cash_balance = account_info['assets'][0]['quoteAsset']['netAsset']
            exposure = float(btc_balance) * price
            asyncio.run_coroutine_threadsafe(update.message.reply_text(f"{order_type} hit at {price}\nnew crypto balance: {btc_balance}\nnew cash balance: {cash_balance}\nStill USDT {exposure} of exposure\nPlease 'close all' at earliest convenience."), MAIN_LOOP)
            ws.close()

    def keep_alive():
        while True:
            requests.put('https://api.binance.com/sapi/v1/userDataStream/isolated', headers={'X-MBX-APIKEY': BINANCE_API_KEY}, params={'symbol': symbol, 'listenKey': lk})
            time.sleep(1800)

    threading.Thread(target=keep_alive, daemon=True).start()
    ws_app = websocket.WebSocketApp(f"wss://stream.binance.com:9443/ws/{lk}", on_message=on_msg)
    active_streams[update.effective_user.id] = ws_app
    threading.Thread(target=ws_app.run_forever, daemon=True).start()

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade(update, context, "buy")

async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await trade(update, context, "sell")

# ============ Kill WebSocket Streams ============

async def kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("Unauthorized user.")
    
    for uid, ws in list(active_streams.items()):
        try: ws.close()
        except: pass
        active_streams.pop(uid, None)
    await update.message.reply_text("All Binance streams stopped.")

async def close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return await update.message.reply_text("Unauthorized user.")
    
    # cancel open OCO orders
    open_orders = client.get_margin_open_oco_orders(isIsolated=True, symbol="BTCUSDT")
    if len(open_orders) != 0:
        client.margin_open_orders_cancellation("BTCUSDT",isIsolated=True)
        await update.message.reply_text("Closed open OCO orders")

    time.sleep(2)

    # get outstanding BTC balance
    account_info = client.isolated_margin_account(symbols="BTCUSDT")
    btc_balance = float(account_info['assets'][0]['baseAsset']['netAsset'])

    # flatten current BTC balance
    amount_to_close = str(math.floor(abs(btc_balance) * 10**5) / 10**5)
    if btc_balance < -0.00001:
        client.new_margin_order(symbol="BTCUSDT", side='BUY', type="MARKET", quantity=amount_to_close, sideEffectType="AUTO_BORROW_REPAY", isIsolated=True)
        await update.message.reply_text("Closed outstanding BTC balance")
    elif btc_balance > 0.00001:
        client.new_margin_order(symbol="BTCUSDT", side='SELL', type="MARKET", quantity=amount_to_close, sideEffectType="AUTO_BORROW_REPAY", isIsolated=True)
        await update.message.reply_text("Closed outstanding BTC balance")

# ============ Launch Bot ============

app = ApplicationBuilder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("config", config))
app.add_handler(CommandHandler("write", set_value))
app.add_handler(CommandHandler("read", get_value))
app.add_handler(CommandHandler("buy", buy))
app.add_handler(CommandHandler("sell", sell))
app.add_handler(CommandHandler("kill", kill))
app.add_handler(CommandHandler('close', close))
app.add_handler(CommandHandler('balance', get_balance))
app.add_handler(CommandHandler('menu', menu))

if __name__ == "__main__":
    app.run_polling()


# to do:
# menu offunctions
# close all
# deploy on render (IP address issue)