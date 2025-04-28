# libraries
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import os
from dotenv import load_dotenv
from telegram import Update
import websockets
import json
from collections import defaultdict
import time
import asyncio
from telegram.error import NetworkError
import logging
from binance.spot import Spot as Client
from binance.lib.utils import config_logging
from binance.error import ClientError
import requests, websocket, threading, time

# load environment
load_dotenv()

MAIN_LOOP = asyncio.get_event_loop()

# Init Telegram API
BOT_TOKEN = os.getenv("BINANCE_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID"))

# Init Binance API
config_logging(logging, logging.DEBUG)
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# Global dictionary to store user data, initialized with 3
user_data = defaultdict(lambda: {"rr": 1.5,
                                 "risk": 1,
                                 "fee": 0.001,
                                 "rr_type": 'before_fees'
                                })

# store price monitoring tasks
active_streams = {}

################# CONFIG #################

# read dictionary values
async def config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("Unauthorized user.")
        return

    # get stored values 
    user_config = user_data[update.effective_user.id]

    if not user_config:
        await update.message.reply_text("No variables set.")
        return

    config_text = "\n".join([f"{key} = {value}" for key, value in user_config.items()])
    await update.message.reply_text(f"Your configuration:\n{config_text}")

# set value
async def set_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("Unauthorized user.")
        return

    if len(context.args) != 2:
        await update.message.reply_text("Usage: /write <variable_name> <value>")
        return

    variable_name, variable_value = context.args[0], context.args[1]

    # Set or update the variable in user_data
    user_data[update.effective_user.id][variable_name] = float(variable_value)
    await update.message.reply_text(f"Set {variable_name} = {variable_value}")

# get value
async def get_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("Unauthorized user.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /read <variable_name>")
        return

    variable_name = context.args[0]

    # Retrieve variable or return "not set"
    value = user_data[update.effective_user.id].get(variable_name, "Not set")
    await update.message.reply_text(f"{variable_name} = {value}")

######################### PRICER #########################

def pricer(side, stopLoss, RR=1.5, Risk=1, f=0.001, rr_type='before_fees'):

        # trade param
        if isinstance(stopLoss, str):
            SL = float(stopLoss)
        else:
            SL = stopLoss

        # get current price
        try:
            P = float(client.ticker_price("BTCUSDT")['price'])
        except ClientError as error:
            logging.error(
                "Found error. status: {}, error code: {}, error message: {}".format(
                    error.status_code, error.error_code, error.error_message
            ))
            return None

        # determine direction
        d = 1 if side == "buy" else -1 if side == "sell" else None
        if d is None:
            logging.error("Select buy or sell")
            return None

        if (side=='buy' and SL > P) or (side=='sell' and SL < P):
            logging.error('stop loss and order direction inconsistent')
            return None

        # get current account balance
        try:
            account_info = client.isolated_margin_account(symbols="BTCUSDT")
            btc_balance = account_info['assets'][0]['baseAsset']['netAsset']
            cash_balance = account_info['assets'][0]['quoteAsset']['netAsset']
        except ClientError as error:
            logging.error(
                "Found error. status: {}, error code: {}, error message: {}".format(
                error.status_code, error.error_code, error.error_message
            ))
            return None

        # compute n
        n = Risk / (SL*(f-d) + P*(f + d))

        # compute position size in USDT terms
        V = n*P

        # compute take profit
        if rr_type=='before_fees':
            TP = P + RR*(P-SL)
        elif rr_type=='after_fees':
            TP = (Risk * RR + n*P*(f+d))/(n*(d-f))

        return {
            'price': P,
            'cryptoBalanceBefore': btc_balance,
            'cashBalanceBefore': cash_balance,
            'takeProfit': str(round(TP,0)),
            'size': str(round(n,5)),
            'funds': str(round(V,2))
        }

###################### TRADING  ######################

# Enter a long trade
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("Unauthorized user.")
        return

    # extract stop loss from message
    SL = float(context.args[0])

    # extract config data
    RR = user_data[update.effective_user.id].get('rr', "Not set")
    Risk = user_data[update.effective_user.id].get('risk', "Not set")
    f = user_data[update.effective_user.id].get('fee', "Not set")
    rr_type = user_data[update.effective_user.id].get('rr_type', "Not set")

    # Price position size and take profit
    pricer_res = pricer(side="buy", stopLoss=SL, RR=RR, Risk=Risk, f=f, rr_type=rr_type)
    if pricer_res is None:
        await update.message.reply_text("Pricer empty, didn't execute")
        return
    
    n = pricer_res['size']
    V = pricer_res['funds']
    P = pricer_res['price']
    TP = pricer_res['takeProfit']

    # Enter Order
    try:
         # Entry Market Order
        entry_order = client.new_margin_order(
            symbol="BTCUSDT",
            side="BUY",
            type="MARKET",
            quantity=n,
            sideEffectType="AUTO_BORROW_REPAY",
            isIsolated=True
        )
        logging.info(entry_order)
    except ClientError as error:
        logging.error(
            "Found error. status: {}, error code: {}, error message: {}".format(
                error.status_code, error.error_code, error.error_message
            )
        )
    
    # wait
    time.sleep(4)

    # Add Stop Loss and Take Profit
    try:
        oco_order = client.new_margin_oco_order(
                symbol="BTCUSDT",
                side="SELL",
                quantity=entry_order['executedQty'],
                price=TP, # limit price - take profit
                stopPrice=str(SL), # marketprice - stop loss
                sideEffectType="AUTO_BORROW_REPAY",
                isIsolated=True
            )
        logging.info(oco_order)
    except ClientError as error:
        logging.error(
            "Found error. status: {}, error code: {}, error message: {}".format(
                error.status_code, error.error_code, error.error_message
            )
        )

    # notify user

    fills = [float(fill['price']) for fill in entry_order['fills']]
    fill_price = sum(fills)/len(fills)

    for order in oco_order['orderReports']:
        if order['type']=='STOP_LOSS':
            SL_executed = order['stopPrice']
        else:
            TP_executed = order['price']

    await update.message.reply_text(f"BTC balance before: {pricer_res['cryptoBalanceBefore']}\nCash balance before: {pricer_res['cashBalanceBefore']}\nBought {entry_order['executedQty']} BTC at {round(float(fill_price),0)}\nStop Loss at {round(float(SL_executed),0)}\nTake Profit at {round(float(TP_executed),0)}") # send message

    # start bot listening
    symbol = 'BTCUSDT'

    lk = requests.post('https://api.binance.com/sapi/v1/userDataStream/isolated',
        headers={'X-MBX-APIKEY': BINANCE_API_KEY}, params={'symbol': symbol}).json()['listenKey']

    def on_msg(ws, msg):
        d = json.loads(msg)
        if d.get('e') == 'executionReport' and d.get('X') == 'FILLED':
            executed_price = round(max([float(d['p']), float(d['P']), float(d['L'])]),0)
            order_type = 'Take Profit' if d['o']=='LIMIT_MAKER' else 'Stop Loss'
            asyncio.run_coroutine_threadsafe(
                update.message.reply_text(f'{order_type} hit at {executed_price}'),
                MAIN_LOOP
            )
            time.sleep(4)
            try:
                account_info = client.isolated_margin_account(symbols="BTCUSDT")
                btc_balance = account_info['assets'][0]['baseAsset']['netAsset']
                cash_balance = account_info['assets'][0]['quoteAsset']['netAsset']
                print(f'New crypto balance: {btc_balance}, new cash balance: {cash_balance}')
            except ClientError:
                pass
            
            ws.close()

    def keep_alive():
        while True:
            requests.put('https://api.binance.com/sapi/v1/userDataStream/isolated',
                headers={'X-MBX-APIKEY': BINANCE_API_KEY}, params={'symbol': symbol, 'listenKey': lk})
            time.sleep(1800)

    threading.Thread(target=keep_alive, daemon=True).start()
    def start_stream():
        ws_app = websocket.WebSocketApp(
            f"wss://stream.binance.com:9443/ws/{lk}",
            on_message=on_msg
        )
        active_streams[update.effective_user.id] = ws_app
        ws_app.run_forever()

    threading.Thread(target=start_stream, daemon=True).start()


# sell trade
async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("Unauthorized user.")
        return

    # extract stop loss from message
    SL = float(context.args[0])

    # extract config data
    RR = user_data[update.effective_user.id].get('rr', "Not set")
    Risk = user_data[update.effective_user.id].get('risk', "Not set")
    f = user_data[update.effective_user.id].get('fee', "Not set")
    rr_type = user_data[update.effective_user.id].get('rr_type', "Not set")

    # Price position size and take profit
    pricer_res = pricer(side="sell", stopLoss=SL, RR=RR, Risk=Risk, f=f, rr_type=rr_type)
    if pricer_res is None:
        await update.message.reply_text("Pricer empty, didn't execute")
        return
    
    n = pricer_res['size']
    V = pricer_res['funds']
    P = pricer_res['price']
    TP = pricer_res['takeProfit']

    # Enter Order
    try:
         # Entry Market Order
        entry_order = client.new_margin_order(
            symbol="BTCUSDT",
            side="SELL",
            type="MARKET",
            quantity=n,
            sideEffectType="AUTO_BORROW_REPAY",
            isIsolated=True
        )
        logging.info(entry_order)
    except ClientError as error:
        logging.error(
            "Found error. status: {}, error code: {}, error message: {}".format(
                error.status_code, error.error_code, error.error_message
            )
        )
    
    # wait
    time.sleep(4)

    # Add Stop Loss and Take Profit
    try:
        oco_order = client.new_margin_oco_order(
                symbol="BTCUSDT",
                side="BUY",
                quantity=entry_order['executedQty'],
                price=TP, # limit price - take profit
                stopPrice=str(SL), # marketprice - stop loss
                sideEffectType="AUTO_BORROW_REPAY",
                isIsolated=True
            )
        logging.info(oco_order)
    except ClientError as error:
        logging.error(
            "Found error. status: {}, error code: {}, error message: {}".format(
                error.status_code, error.error_code, error.error_message
            )
        )

    # notify user

    fills = [float(fill['price']) for fill in entry_order['fills']]
    fill_price = sum(fills)/len(fills)

    for order in oco_order['orderReports']:
        if order['type']=='STOP_LOSS':
            SL_executed = order['stopPrice']
        else:
            TP_executed = order['price']

    await update.message.reply_text(f"BTC balance before: {pricer_res['cryptoBalanceBefore']}\nCash balance before: {pricer_res['cashBalanceBefore']}\nBought {entry_order['executedQty']} BTC at {round(float(fill_price),0)}\nStop Loss at {round(float(SL_executed),0)}\nTake Profit at {round(float(TP_executed),0)}") # send message

    # start bot listening
    symbol = 'BTCUSDT'

    lk = requests.post('https://api.binance.com/sapi/v1/userDataStream/isolated',
        headers={'X-MBX-APIKEY': BINANCE_API_KEY}, params={'symbol': symbol}).json()['listenKey']

    def on_msg(ws, msg):
        d = json.loads(msg)
        if d.get('e') == 'executionReport' and d.get('X') == 'FILLED':
            executed_price = round(max([float(d['p']), float(d['P']), float(d['L'])]),0)
            order_type = 'Take Profit' if d['o']=='LIMIT_MAKER' else 'Stop Loss'
            asyncio.run_coroutine_threadsafe(
                update.message.reply_text(f'{order_type} hit at {executed_price}'),
                MAIN_LOOP
            )
            time.sleep(4)
            try:
                account_info = client.isolated_margin_account(symbols="BTCUSDT")
                btc_balance = account_info['assets'][0]['baseAsset']['netAsset']
                cash_balance = account_info['assets'][0]['quoteAsset']['netAsset']
                print(f'New crypto balance: {btc_balance}, new cash balance: {cash_balance}')
            except ClientError:
                pass
            
            ws.close()

    def keep_alive():
        while True:
            requests.put('https://api.binance.com/sapi/v1/userDataStream/isolated',
                headers={'X-MBX-APIKEY': BINANCE_API_KEY}, params={'symbol': symbol, 'listenKey': lk})
            time.sleep(1800)

    threading.Thread(target=keep_alive, daemon=True).start()
    def start_stream():
        ws_app = websocket.WebSocketApp(
            f"wss://stream.binance.com:9443/ws/{lk}",
            on_message=on_msg
        )
        active_streams[update.effective_user.id] = ws_app
        ws_app.run_forever()

    threading.Thread(target=start_stream, daemon=True).start()

# kill monitoring tasks
async def kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stopped = False

    # Stop all active WebSocket streams
    for uid, ws in list(active_streams.items()):
        try:
            ws.close()
            stopped = True
        except:
            pass
        active_streams.pop(uid, None)

    if stopped:
        await update.message.reply_text("All Binance streams stopped.")
    else:
        await update.message.reply_text("No active streams to stop.")


####################### TELEGRAM BOT #######################

# initialise Telegram bot
app = ApplicationBuilder().token(BOT_TOKEN).build()

# add handlers
app.add_handler(CommandHandler("config", config))
app.add_handler(CommandHandler("write", set_value))
app.add_handler(CommandHandler("read", get_value))
app.add_handler(CommandHandler("buy", buy))
app.add_handler(CommandHandler("kill", kill))

if __name__ == "__main__":
    app.run_polling()

    

    


       
