# Optimal Execution Trading Bot (Binance + KuCoin)

A simple Telegram bot that allows you to **send trade orders via message** and **automatically execute them** on **Binance** and **KuCoin**, with smart risk management.

## Features
- ✅ Trade directly by messaging the bot
- ✅ Automatically calculates position size based on **pre-defined target risk**
- ✅ Sets **Risk-Reward (RR) targets** automatically
- ✅ Minimizes **slippage** and **execution fees**
- ✅ Supports **both Binance and KuCoin**
- ✅ Fast and lightweight for real-time trading

## How It Works
- You first set a target RR for your strategy, and how much you're willing to lose if wrong.
- When your strategy's entry conditions are met, you send a trade command (e.g., Buy/Sell + stop loss)
- The bot calculates:
  - Position size
  - Take-profit target given slippage and fees.
- It then places the order automatically via exchange APIs.

## Requirements
- Python 3.8+
- Telegram Bot API token
- Binance API key and secret
- KuCoin API key, secret, and passphrase

## Setup
1. Clone this repo
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Configure your API keys in the config file
4. Run the bot:
   ```bash
   python bot.py
   ```

## Disclaimer
Use at your own risk. Always test with small amounts first.

## To Do:
Obtain KUCOIN_STREAMING_ID via API call.



