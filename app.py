import json
import hmac
import hashlib
import time
import requests
from datetime import datetime, timedelta
import websocket

# Replace with your Bybit API key and secret
API_KEY = 'your_api_key'
API_SECRET = 'your_api_secret'

# Updated endpoints for Bybit API v5
WS_URL = "wss://stream.bybit.com/v5/public/option"
API_URL = "https://api.bybit.com/v5/order"
POSITION_URL = "https://api.bybit.com/v5/position/list"
KLINE_URL = "https://api.bybit.com/v5/market/kline"

PREMIUM = 10 #Trading Amount
weekly_start_price = None
next_trade_time = datetime.now()

def generate_signature(params, timestamp):
    """Generate signature for Bybit's private API v5."""
    param_str = str(timestamp) + API_KEY + "&".join([f"{k}={v}" for k, v in sorted(params.items())])
    return hmac.new(API_SECRET.encode('utf-8'), param_str.encode('utf-8'), hashlib.sha256).hexdigest()

def get_server_time():
    """Get Bybit server time for timestamp synchronization."""
    try:
        response = requests.get("https://api.bybit.com/v5/market/time")
        if response.status_code == 200:
            return int(response.json()['result']['timeNano'] // 1000000)
        return int(time.time() * 1000)
    except:
        return int(time.time() * 1000)

def on_message(ws, message):
    """Handle WebSocket message."""
    global weekly_start_price

    try:
        data = json.loads(message)
        if 'data' in data and len(data['data']) > 0:
            
            current_sol_price = float(data['data'][0]['price'])
            print(f"Latest SOL Price: {current_sol_price}")

            if datetime.now() >= next_trade_time:
                fetch_weekly_open_price()
                close_open_positions()
                place_strangle_order()
    except json.JSONDecodeError as e:
        print(f"Error parsing message: {e}")
    except Exception as e:
        print(f"Error processing message: {e}")

def on_error(ws, error):
    print(f"WebSocket Error: {error}")

def on_close(ws, close_status_code, close_msg):
    print(f"WebSocket closed. Status code: {close_status_code}, message: {close_msg}")
    print("Attempting to reconnect...")

def on_open(ws):
    # Updated subscription format for v5 WebSocket
    ws.send(json.dumps({
        "op": "subscribe",
        "args": ["tickers.SOLUSDT"]
    }))

def select_strike_prices(weekly_price):
    """Calculate strike prices for the OTM call and put options based on $10 offset."""
    call_strike = round(weekly_price + 10, 2)
    put_strike = round(weekly_price - 10, 2)
    return call_strike, put_strike

def fetch_weekly_open_price():
    """Fetch the open price using v5 API."""
    global weekly_start_price
    try:
        params = {
            'category': 'option',
            'symbol': 'SOLUSDT',
            'interval': 'W',
            'limit': 1
        }
        
        response = requests.get(KLINE_URL, params=params)
        
        if response.status_code == 200:
            data = response.json()
            if data['result']['list']:
                weekly_start_price = float(data['result']['list'][0][1])  # Open price
                print(f"Weekly open price for SOL: {weekly_start_price}")
            else:
                print("No kline data available")
        else:
            print(f"Error fetching weekly open price: {response.text}")

    except requests.RequestException as e:
        print(f"Error fetching weekly candle data: {e}")

def place_option_order(symbol, side, strike_price, premium_limit):
    """Place an options order using v5 API."""
    timestamp = get_server_time()
    
    params = {
        'category': 'option',
        'symbol': symbol,
        'side': side,
        'orderType': 'Limit',
        'price': str(premium_limit),
        'qty': '1',
        'timeInForce': 'GTC',
        'strikePrice': str(strike_price)
    }
    
    headers = {
        'X-BAPI-API-KEY': API_KEY,
        'X-BAPI-TIMESTAMP': str(timestamp),
        'X-BAPI-SIGN': generate_signature(params, timestamp),
        'Content-Type': 'application/json'
    }

    try:
        response = requests.post(API_URL, headers=headers, json=params)
        if response.status_code == 200:
            result = response.json()
            if result['retCode'] == 0:
                print(f"Order placed: {symbol} {side} strike: {strike_price} premium: {premium_limit}")
                return result
            else:
                print(f"Order placement failed: {result['retMsg']}")
                return None
        else:
            print(f"Order failed: {response.text}")
            return None
    except requests.RequestException as e:
        print(f"Request Exception: {e}")
        return None

def close_open_positions():
    """Get and close open positions using v5 API."""
    timestamp = get_server_time()
    
    # First get open positions
    params = {
        'category': 'option',
        'symbol': 'SOLUSDT'
    }
    
    headers = {
        'X-BAPI-API-KEY': API_KEY,
        'X-BAPI-TIMESTAMP': str(timestamp),
        'X-BAPI-SIGN': generate_signature(params, timestamp),
    }

    try:
        response = requests.get(POSITION_URL, headers=headers, params=params)
        if response.status_code == 200:
            positions = response.json()['result']['list']
            
            # Close each open position
            for position in positions:
                if float(position['size']) > 0:
                    close_params = {
                        'category': 'option',
                        'symbol': position['symbol'],
                        'side': 'Sell' if position['side'] == 'Buy' else 'Buy',
                        'orderType': 'Market',
                        'qty': position['size'],
                        'timeInForce': 'IOC',
                    }
                    
                    close_headers = {
                        'X-BAPI-API-KEY': API_KEY,
                        'X-BAPI-TIMESTAMP': str(get_server_time()),
                        'X-BAPI-SIGN': generate_signature(close_params, get_server_time()),
                        'Content-Type': 'application/json'
                    }
                    
                    close_response = requests.post(API_URL, headers=close_headers, json=close_params)
                    if close_response.status_code == 200:
                        print(f"Closed position for {position['symbol']}")
                    else:
                        print(f"Failed to close position: {close_response.text}")
        else:
            print(f"Failed to fetch positions: {response.text}")
    except requests.RequestException as e:
        print(f"Request Exception: {e}")

def place_strangle_order():
    """Place a strangle options order."""
    global next_trade_time, weekly_start_price

    if weekly_start_price:
        call_strike, put_strike = select_strike_prices(weekly_start_price)

        # Create the option symbols according to Bybit's format
        expiry = (datetime.now() + timedelta(days=7)).strftime("%Y%m%d")
        call_symbol = f"SOL-{expiry}-{call_strike}-C"
        put_symbol = f"SOL-{expiry}-{put_strike}-P"

        # Place the call and put option orders
        place_option_order(call_symbol, "Buy", call_strike, PREMIUM)
        place_option_order(put_symbol, "Buy", put_strike, PREMIUM)

        print(f"Strangle order placed at strikes {call_strike} (call) and {put_strike} (put) with ${PREMIUM} premium each")

        # Set the next trade time to the following week
        next_trade_time = datetime.now() + timedelta(days=7)

def run_websocket():
    """Initialize WebSocket connection with reconnection logic."""
    websocket.enableTrace(True)  # Enable for debugging
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            print(f"WebSocket error: {e}")
        
        print("Reconnecting WebSocket in 5 seconds...")
        time.sleep(5)

if __name__ == "__main__":
    run_websocket()