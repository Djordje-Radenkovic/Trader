# libraries
import http.client
import json
from dotenv import load_dotenv
import os
from kucoin_auth import KucoinClient
import requests
from urllib.parse import urlencode
import requests
import uuid
from typing import Dict, Any
import logging

# load environment 
load_dotenv()
    
# Kucoin API class
class KucoinAPI:

    def __init__(self, api_key: str, api_secret: str, passphrase: str):

        self.signer = KucoinClient(api_key, api_secret, passphrase)
        self.session = requests.Session()
        self.host = "api.kucoin.com"
        self.base_url = f"https://{self.host}"

    def _request(self, method, endpoint, params=None, body=None, auth_required=True):

        url = f"{self.base_url}{endpoint}"
        headers = {}

        if params:
            query_string = urlencode(params)
            url += f"?{query_string}"
            endpoint += f"?{query_string}"

        if body:
            body = json.dumps(body)
            headers = {'Content-Type': 'application/json'}
        else:
            headers = {}

        if auth_required:
            payload = method + endpoint + (body or '')
            headers.update(self.signer.headers(payload))

        try:
            response = requests.request(method, url, headers=headers, data=body)
            return response.json()
        
        except requests.exceptions.RequestException as e:
            logging.error(f"Request error: {str(e)}")
            return {"error": str(e)}

    def place_order_v3(self, 
                    side: str,
                    funds: str = None,
                    size: str = None,
                    price: str = None,
                    symbol: str = "BTC-USDT",
                    order_type: str = "market",
                    is_isolated: bool = True,
                    auto_borrow: bool = True,
                    auto_repay: bool = True):
    
        # Prepare order data
        data = {
            "symbol": symbol,
            "side": side,
            "clientOid": str(uuid.uuid4()),
            "type": order_type,
            "isIsolated": is_isolated,
            "autoBorrow": auto_borrow,
            "autoRepay": auto_repay,
        }

        if order_type == 'limit' and price is not None:
            data['price'] = price

        # Add position size
        if size is not None:
            data["size"] = size
        elif funds is not None:
            data["funds"] = funds  
        else:
            raise ValueError("Must provide size for sell orders or funds for buy orders")

        # Make the API request
        return self._request(
            method="POST",
            endpoint="/api/v3/hf/margin/order",
            body=data
        )
    
    def get_order_info(self, orderID: str = None, symbol: str = "BTC-USDT"):
        
        params = {
            "symbol": symbol
        }

        return self._request(
            method="GET",
            endpoint=f"/api/v3/hf/margin/orders/{orderID}",
            params=params,
            auth_required=True
        )
  
    # Last price
    def get_last_price(self, ticker="USDT-BTC"):

        return self._request(
            method="GET",
            endpoint=f"/api/v1/mark-price/{ticker}/current",
            auth_required=False
        )
    
    # isolated margin account info
    def get_account_info(self, symbol="BTC-USDT", quoteCurrency="USDT", queryType="ISOLATED"):
        params = {}

        if quoteCurrency is not None:
            params["quoteCurrency"] = quoteCurrency
        if symbol is not None:
            params["symbol"] = symbol
        if queryType is not None:
            params["queryType"] = queryType

        return self._request(
            method="GET",
            endpoint="/api/v3/isolated/accounts",
            params=params,
            auth_required=True
        )

    def repay(self, size, symbol="BTC-USDT", currency = "USDT", is_isolated=True):

        data = {
            "currency": currency,
            "size": size,
            "symbol": symbol,
            "isIsolated": is_isolated
        }

        print('repay')
        return self._request(
            method="POST",
            endpoint="/api/v3/margin/repay",
            body=data
        )
    
    def stop_order_v1(self, 
                    side: str,
                    size: str,
                    stopPrice: str,
                    price = None,
                    symbol: str = "BTC-USDT",
                    tradeType: str = "MARGIN_ISOLATED_TRADE",
                    order_type: str = "market",
                    stop = None
                    ):
    
        data = {
            "symbol": symbol,
            "side": side,
            "clientOid": str(uuid.uuid4()),
            "type": order_type,
            "stopPrice": stopPrice,
            "tradeType": tradeType,
            "size": size
        }

        if order_type == 'limit' and price is not None:
            data['price'] = price
        
        if stop is not None:
            data['stop'] = stop

        # Make the API request
        return self._request(
            method="POST",
            endpoint="/api/v1/stop-order",
            body=data
        )
    
    def place_order_v1(self, 
                     side: str,
                     symbol: str = "BTC-USDT",
                     type: str = "market",
                     size: str = None,
                     price: str = None,
                     marginModel: str = "isolated",
                     auto_borrow: bool = False,
                     auto_repay: bool = False):
          
        # Prepare order data
        data = {
            "symbol": symbol,
            "side": side,
            "clientOid": str(uuid.uuid4()),
            "type": type,
            "marginModel": marginModel,
            "autoBorrow": auto_borrow,
            "autoRepay": auto_repay,
            "size": size,
            "postOnly": False
        }
        if type == 'limit' and price is not None:
            data['price'] = price

        # Make the API request
        return self._request(
            method="POST",
            endpoint="/api/v1/margin/order",
            body=data
        )
    
    def live_stream_id(self):
        return self._request(
            method='POST',
            endpoint='/api/v1/bullet-public'
        )


    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return self._request(
            method="DELETE",
            endpoint=f"/api/v1/orders/{order_id}"
        )

    def pricer(self, side, stopLoss, RR=1.5, Risk=1, f=0.001, tp_type='ideal'):

        # trade param
        if isinstance(stopLoss, str):
            SL = float(stopLoss)
        else:
            SL = stopLoss

        # get current price
        P = None
        price_request = self.get_last_price(ticker="USDT-BTC")
        P = 1/price_request['data']['value']
        if P is None:
            print('Price not fetched correctly')
            return

        # determine direction
        d = 1 if side == "buy" else -1 if side == "sell" else None
        if d is None:
            print('Select buy or sell')
            return None

        if (side=='buy' and SL > P) or (side=='sell' and SL < P):
            print('stop loss and order direction inconsistent')
            return None

        # get current account balance
        M = self.get_account_info()['data']['totalAssetOfQuoteCurrency']

        # compute n
        n = Risk / (SL*(f-d) + P*(f + d))

        # compute position size in USDT terms
        V = n*P

        # compute take profit
        if tp_type=='ideal':
            TP = P + RR*(P-SL)
        elif tp_type=='real':
            TP = (Risk * RR + n*P*(f+d))/(n*(d-f))

        return {
            'price': P,
            'balanceBefore': M,
            'takeProfit': round(TP,0),
            'size': round(n,8),
            'funds': round(V,2)
        }


if __name__ == '__main__':

    key = os.getenv("KUCOIN_API_KEY","")
    secret = os.getenv("KUCOIN_API_SECRET","")
    passphrase = os.getenv("KUCOIN_API_PASSPHRASE","")

    kucoin_api = KucoinAPI(key, secret, passphrase)