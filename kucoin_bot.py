# libraries
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import os
from dotenv import load_dotenv
from telegram import Update
from kucoin_api import KucoinAPI
import websockets
import json
from collections import defaultdict
import time
import asyncio
from telegram.error import NetworkError

# kill previous syncs
asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())

# load environment
load_dotenv()

# Init Telegram API
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID"))

# Init Kucoin API
key = os.getenv("KUCOIN_API_KEY","")
secret = os.getenv("KUCOIN_API_SECRET","")
passphrase = os.getenv("KUCOIN_API_PASSPHRASE","")
kucoin_api = KucoinAPI(key, secret, passphrase)

# Init Kucoin streaming
id = kucoin_api.live_stream_id()['data']['token']
WS_URL = "wss://ws-api-spot.kucoin.com?token={}".format(id)

# Global dictionary to store user data, initialized with 3
user_data = defaultdict(lambda: {"rr": 1.5,
                                 "risk": 1,
                                 "fee": 0.001,
                                 "fee_buffer": 0.00000001,
                                 "tp_type": 'ideal',
                                 'Entry ID': None,
                                 'Take Profit ID': None,
                                 'Stop Loss ID': None
                                })

# store price monitoring tasks
price_monitoring_tasks = {}

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
    tptype = user_data[update.effective_user.id].get('tp_type', "Not set")

    # Price position size and take profit
    pricer_res = kucoin_api.pricer(side="buy", stopLoss=SL, RR=RR, Risk=Risk, f=f, tp_type=tptype)
    if pricer_res is None:
        await update.message.reply_text("Pricer empty, didn't execute")
        return
        
    n = pricer_res['size']
    V = pricer_res['funds']
    P = pricer_res['price']
    TP = pricer_res['takeProfit']
    M = pricer_res['balanceBefore']
    await update.message.reply_text(f"Balance before trade: {M}")

    # enter
    if float(M) < float(V) * (1+f):
        entryId = kucoin_api.place_order_v3(side='buy', funds=f"{V:.6f}", auto_borrow=True)['data']['orderId']
        time.sleep(4)
        n = float(kucoin_api.get_order_info(entryId)['data']['dealSize'])
        leveraged=True
    else:
        entryId = kucoin_api.place_order_v1(side='buy', size=f"{n:.8f}")['data']['orderId'] # entry without leverage
        time.sleep(1)
        stopLossId = kucoin_api.stop_order_v1(side='sell', size=f"{n:.8f}", stop='loss', stopPrice=f"{SL:.8f}")['data']['orderId']
        user_data[update.effective_user.id]['Stop Loss ID'] = stopLossId # save ID
        time.sleep(1)
        takeProfitId = kucoin_api.stop_order_v1(side='sell', size=f"{n:.8f}", stop='entry', stopPrice=f"{TP:.8f}")['data']['orderId'] # take profit
        user_data[update.effective_user.id]['Take Profit ID'] = takeProfitId # save ID
        leveraged = False

    await update.message.reply_text(f"Bought {n} BTC at {round(P,0)} \n Stop Loss at {round(SL,0)} \n Take Profit at {round(TP,0)}") # send message
    user_data[update.effective_user.id]['Entry ID'] = entryId # save ID

    # Start price monitoring task
    task = asyncio.create_task(process_buy(
        update, TP, SL, n, update.effective_user.id, leveraged
    ))
    price_monitoring_tasks[update.effective_user.id] = task

    await update.message.reply_text("Monitoring price updates.")
   

# price monitoring task
async def process_buy(update, TP, SL, n, user_id, leveraged):
    try:
        async with websockets.connect(WS_URL) as websocket:
            await websocket.send(json.dumps({
                "id": int(os.getenv('KUCOIN_STREAMING_ID',"")),
                "type": "subscribe",
                "topic": "/market/ticker:BTC-USDT",
                "response": True
            }))

            while True:
                data = json.loads(await websocket.recv())
                if "data" in data:
                    price = float(data['data']['price'])
        
                    if price >= TP:
                        if leveraged:
                            kucoin_api.place_order_v3(side='sell', size =f"{n:.8f}")
                            await update.message.reply_text(f"Price hit take profit \n Please 'close all' manually!")
                        else:
                            await update.message.reply_text(f"Price hit take profit.")
                            stopLossId = user_data[update.effective_user.id].get('Stop Loss ID', "Not set")
                            kucoin_api.cancel_order(stopLossId)
                            user_data[user_id]['Stop Loss ID'] = None
                            user_data[user_id]['Take Profit ID'] = None
                    
                        break
                    
                    elif price <= SL:
                        if leveraged:
                            kucoin_api.place_order_v3(side='sell', size =f"{n:.8f}")
                            await update.message.reply_text(f"Price hit stop loss \n Please 'close all' manually!")
                        else:
                            await update.message.reply_text(f"Price hit stop loss.")
                            takeProfitId = user_data[update.effective_user.id].get('Take Profit ID', "Not set")
                            kucoin_api.cancel_order(takeProfitId)
                            user_data[user_id]['Take Profit ID'] = None
                            user_data[user_id]['Stop Loss ID'] = None

                        break
    finally:
        price_monitoring_tasks.pop(user_id, None)

# Enter a short trade
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
    fee_buffer = user_data[update.effective_user.id].get('fee_buffer', "Not set")
    tptype = user_data[update.effective_user.id].get('tp_type', "Not set")

    # Price position size and take profit
    pricer_res = kucoin_api.pricer(side="sell", stopLoss=SL, RR=RR, Risk=Risk, f=f, tp_type=tptype)
    if pricer_res is None:
        await update.message.reply_text("Pricer empty, didn't execute")
        return
    
    n = pricer_res['size']
    V = pricer_res['funds']
    P = pricer_res['price']
    TP = pricer_res['takeProfit']
    M = pricer_res['balanceBefore']
    await update.message.reply_text(f"Balance before trade: {M}")

    # enter
    entryId = kucoin_api.place_order_v3(side='sell', size=f"{n:.8f}")['data']['orderId'] # entry
    time.sleep(2)
    takeProfitId = kucoin_api.stop_order_v1(stopPrice=f"{TP:.8f}", stop='loss', side='buy', size=f"{n+fee_buffer:.8f}")['data']['orderId'] # take profit
    time.sleep(2)
    stopLossId = kucoin_api.stop_order_v1(stopPrice=f"{SL:.8f}", stop='entry', side='buy', size=f"{n+fee_buffer:.8f}")['data']['orderId'] # stop loss
    time.sleep(2)

    user_data[update.effective_user.id]['Entry ID'] = entryId # save ID
    user_data[update.effective_user.id]['Stop Loss ID'] = stopLossId # save ID
    user_data[update.effective_user.id]['Take Profit ID'] = takeProfitId # save ID
        
    await update.message.reply_text(f"Sold {n} BTC at {round(P,0)} \n Stop Loss at {round(SL,0)} \n Take Profit at {round(TP,0)}") # send message
    user_data[update.effective_user.id]['Entry ID'] = entryId # save ID

    # Start price monitoring task
    task = asyncio.create_task(process_sell(
        update, TP, SL, update.effective_user.id
    ))
    price_monitoring_tasks[update.effective_user.id] = task

    await update.message.reply_text("Monitoring price updates.")



async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("Unauthorized user.")
        return
    
    # Get target price from command argument
    target = float(context.args[0])
    
    # Start monitoring task
    task = asyncio.create_task(process_alert(update, target, update.effective_user.id))
    price_monitoring_tasks[update.effective_user.id] = task
    
    await update.message.reply_text(f"Monitoring for price crossing ${target}")

async def process_alert(update, target, user_id):
    try:
        async with websockets.connect(WS_URL) as websocket:
            await websocket.send(json.dumps({
                "id": int(os.getenv('KUCOIN_STREAMING_ID',"")),
                "type": "subscribe",
                "topic": "/market/ticker:BTC-USDT",
                "response": True
            }))
 
            last_price = None
            while True:
                data = json.loads(await websocket.recv())
                if "data" in data:
                    current_price = float(data['data']['price'])
                    
                    if last_price is not None:
                        # Check for crossing above target
                        if last_price < target and current_price >= target:
                            await update.message.reply_text(f"Price crossed above ${target}!")
                            break
                        # Check for crossing below target
                        elif last_price > target and current_price <= target:
                            await update.message.reply_text(f"Price dropped below ${target}!")
                            break
                    
                    last_price = current_price

    finally:
        price_monitoring_tasks.pop(user_id, None)


# price monitoring task
async def process_sell(update, TP, SL, user_id):
    try:
        async with websockets.connect(WS_URL) as websocket:
            await websocket.send(json.dumps({
                "id": int(os.getenv('KUCOIN_STREAMING_ID',"")),
                "type": "subscribe",
                "topic": "/market/ticker:BTC-USDT",
                "response": True
            }))

            while True:
                data = json.loads(await websocket.recv())
                if "data" in data:
                    price = float(data['data']['price'])

                    takeProfitId = user_data[update.effective_user.id].get('Take Profit ID', "Not set")
                    stopLossId = user_data[update.effective_user.id].get('Stop Loss ID', "Not set")
        
                    if price <= TP:
                        await update.message.reply_text(f"Price hit take profit \n Please 'close all' manually!")
                        kucoin_api.cancel_order(stopLossId)
                        user_data[update.effective_user.id]['Stop Loss ID'] = None
                        user_data[update.effective_user.id]['Take Profit ID'] = None
                        break
                    elif price >= SL:
                        await update.message.reply_text(f"Price hit stop loss \n Please 'close all' manually!")
                        kucoin_api.cancel_order(takeProfitId)
                        user_data[update.effective_user.id]['Take Profit ID'] = None
                        user_data[update.effective_user.id]['Stop Loss ID'] = None
                    break
    finally:
        price_monitoring_tasks.pop(user_id, None)



async def kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Cancel price monitoring task if exists
    if update.effective_user.id in price_monitoring_tasks:
        price_monitoring_tasks[update.effective_user.id].cancel()
        price_monitoring_tasks.pop(update.effective_user.id)
        await update.message.reply_text("Stopped monitoring price.")

# kill all active positions, repay debt / sell btc
async def close(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("Unauthorized user.")
        return
    
    stopLossId = user_data[update.effective_user.id].get('Stop Loss ID', "Not set")
    takeProfitId = user_data[update.effective_user.id].get('Take Profit ID', "Not set")

    if takeProfitId is not None:
        kucoin_api.cancel_order(takeProfitId)
        await update.message.reply_text("Cancelled take profit.")

    
    if stopLossId is not None:
        kucoin_api.cancel_order(stopLossId)
        await update.message.reply_text("Cancelled stop loss.")
    
    BTC_assets = kucoin_api.get_account_info(quoteCurrency="BTC")['data']['assets'][0]['baseAsset']['available']
    BTC_liability = kucoin_api.get_account_info(quoteCurrency="BTC")['data']['assets'][0]['baseAsset']['liability']

    if float(BTC_liability) > 0:
        kucoin_api.place_order_v1(side='buy', size=BTC_liability)
        await update.message.reply_text(f"Bought {BTC_liability} BTC.")

    if float(BTC_assets) > 0:
        kucoin_api.place_order_v1(side='sell', size=BTC_assets)
        await update.message.reply_text(f"Sold {BTC_assets} BTC.")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("Unauthorized user.")
        return
    
    account_info = kucoin_api.get_account_info(quoteCurrency="BTC")

    BTC_balance = account_info['data']['assets'][0]['baseAsset']
    await update.message.reply_text(f"BTC Balance:\n {BTC_balance}")

    USDT_balance = account_info['data']['assets'][0]['quoteAsset']
    await update.message.reply_text(f"USDT Balance:\n {USDT_balance}")

# stop listening for price updates
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global listening
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("Unauthorized user.")
        return

    listening = False
    await update.message.reply_text("Stopped listening.")

async def lastprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        await update.message.reply_text("Unauthorized user.")
        return
    
    # Start price monitoring task
    task = asyncio.create_task(process_lastprice(update, update.effective_user.id))
    price_monitoring_tasks[update.effective_user.id] = task
    
    await update.message.reply_text("Starting price stream...")


async def process_lastprice(update, user_id):
    try:
        async with websockets.connect(WS_URL) as websocket:
            await websocket.send(json.dumps({
                "id": int(os.getenv('KUCOIN_STREAMING_ID',"")),
                "type": "subscribe",
                "topic": "/market/ticker:BTC-USDT",
                "response": True
            }))

            last_price = None
            while True:
                data = json.loads(await websocket.recv())
                if "data" in data:
                    current_price = float(data['data']['price'])
                    
                    # Only send message if price changed
                    if last_price != current_price:
                        await update.message.reply_text(f"BTC Price: ${round(current_price,1)}")
                        last_price = current_price

    finally:
        price_monitoring_tasks.pop(user_id, None)

# Add to main()

# initialise Telegram bot
app = ApplicationBuilder().token(BOT_TOKEN).build()

# add handlers
app.add_handler(CommandHandler("config", config))
app.add_handler(CommandHandler("write", set_value))
app.add_handler(CommandHandler("read", get_value))
app.add_handler(CommandHandler("buy", buy))
app.add_handler(CommandHandler("sell", sell))
app.add_handler(CommandHandler("stop", stop))
app.add_handler(CommandHandler("kill", kill))
app.add_handler(CommandHandler('balance', balance))
app.add_handler(CommandHandler('alert', alert))
app.add_handler(CommandHandler("lastprice", lastprice))
app.add_handler(CommandHandler("close", close))

async def error_handler(update, context):
    if isinstance(context.error, NetworkError):
        print("Network error occurred:", context.error)
    else:
        print("Unhandled error:", context.error)

app.add_error_handler(error_handler)

#app.run_polling()

app.run_polling(drop_pending_updates=True)
