from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from datetime import datetime, timedelta
import os

app = Flask(__name__)
CORS(app)

# Tradier API Configuration
# Set these as environment variables or replace with your keys
TRADIER_API_KEY = os.environ.get('TRADIER_API_KEY', 'YOUR_SANDBOX_KEY_HERE')
TRADIER_BASE_URL = os.environ.get('TRADIER_BASE_URL', 'https://sandbox.tradier.com/v1')

# For production, change to:
# TRADIER_BASE_URL = 'https://api.tradier.com/v1'

HEADERS = {
    'Authorization': f'Bearer {TRADIER_API_KEY}',
    'Accept': 'application/json'
}


def get_stock_quote(ticker_symbol):
    """
    Get stock quote data from Tradier
    Returns: price, volume, description
    """
    try:
        url = f"{TRADIER_BASE_URL}/markets/quotes"
        params = {'symbols': ticker_symbol}
        
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        
        data = response.json()
        
        # Handle single quote response
        if 'quotes' in data and 'quote' in data['quotes']:
            quote = data['quotes']['quote']
            
            return {
                'price': float(quote.get('last', 0)),
                'volume': float(quote.get('volume', 0)),
                'description': quote.get('description', ''),
                'symbol': quote.get('symbol', ticker_symbol)
            }
        
        return None
        
    except Exception as e:
        print(f"⚠️  Error fetching quote for {ticker_symbol}: {str(e)}")
        return None


def get_options_expirations(ticker_symbol):
    """
    Get available options expiration dates
    Returns: list of expiration date strings
    """
    try:
        url = f"{TRADIER_BASE_URL}/markets/options/expirations"
        params = {'symbol': ticker_symbol}
        
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        
        data = response.json()
        
        if 'expirations' in data and 'date' in data['expirations']:
            expirations = data['expirations']['date']
            # Ensure it's a list (single date returns string)
            if isinstance(expirations, str):
                expirations = [expirations]
            return expirations
        
        return []
        
    except Exception as e:
        print(f"⚠️  Error fetching expirations for {ticker_symbol}: {str(e)}")
        return []


def get_near_money_put_iv(ticker_symbol, current_price):
    """
    Get implied volatility from near-the-money put options (7-14 days out)
    Targets puts around 70% of current price (~30 delta)
    """
    try:
        # Get available expiration dates
        expirations = get_options_expirations(ticker_symbol)
        
        if not expirations:
            print(f"   No options expirations found for {ticker_symbol}")
            return None
        
        # Filter for expirations 7-14 days out
        target_date = datetime.now() + timedelta(days=7)
        end_date = datetime.now() + timedelta(days=14)
        
        suitable_expirations = []
        for exp_str in expirations:
            exp_date = datetime.strptime(exp_str, '%Y-%m-%d')
            if target_date <= exp_date <= end_date:
                suitable_expirations.append(exp_str)
        
        # If no options in 7-14 day range, use nearest expiration
        if not suitable_expirations:
            suitable_expirations = [expirations[0]]
        
        # Get options chain for the nearest suitable expiration
        target_expiration = suitable_expirations[0]
        
        url = f"{TRADIER_BASE_URL}/markets/options/chains"
        params = {
            'symbol': ticker_symbol,
            'expiration': target_expiration
        }
        
        response = requests.get(url, headers=HEADERS, params=params)
        response.raise_for_status()
        
        data = response.json()
        
        if 'options' not in data or 'option' not in data['options']:
            print(f"   No options chain data for {ticker_symbol}")
            return None
        
        options = data['options']['option']
        
        # Ensure options is a list
        if not isinstance(options, list):
            options = [options]
        
        # Filter for puts only
        puts = [opt for opt in options if opt.get('option_type') == 'put']
        
        if not puts:
            print(f"   No put options found for {ticker_symbol}")
            return None
        
        # Find puts near 70% of current price (target ~30 delta)
        target_strike = current_price * 0.70
        
        # Calculate distance to target strike for each put
        for put in puts:
            put['distance'] = abs(put.get('strike', 0) - target_strike)
        
        # Sort by distance and take the 3 nearest
        puts_sorted = sorted(puts, key=lambda x: x['distance'])
        nearest_puts = puts_sorted[:3]
        
        # Get average IV from nearest puts
        iv_values = []
        for put in nearest_puts:
            greeks = put.get('greeks', {})
            if greeks and 'mid_iv' in greeks:
                iv_values.append(float(greeks['mid_iv']))
        
        if iv_values:
            avg_iv = sum(iv_values) / len(iv_values)
            return round(avg_iv * 100, 2)  # Convert to percentage
        
        print(f"   No IV data found in options chain for {ticker_symbol}")
        return None
        
    except Exception as e:
        print(f"⚠️  Error getting IV for {ticker_symbol}: {str(e)}")
        return None


def get_ticker_data(ticker_symbol):
    """
    Get comprehensive ticker data for wheel strategy screening
    """
    print(f"📊 Fetching data for {ticker_symbol}...")
    
    try:
        # Get stock quote
        quote = get_stock_quote(ticker_symbol)
        
        if not quote:
            return {
                'ticker': ticker_symbol.upper(),
                'error': 'Unable to fetch quote data'
            }
        
        current_price = quote['price']
        volume_raw = quote['volume']
        
        # Get implied volatility
        print(f"   Getting IV for {ticker_symbol}...")
        iv = get_near_money_put_iv(ticker_symbol, current_price)
        
        # Convert volume to millions
        volume = round(volume_raw / 1e6, 2)
        
        print(f"   ✓ {ticker_symbol}: Price=${current_price:.2f}, IV={iv}%, Vol={volume}M")
        
        return {
            'ticker': ticker_symbol.upper(),
            'price': round(current_price, 2),
            'implied_volatility': iv if iv is not None else 'N/A',
            'description': quote['description'],
            'volume': volume
        }
        
    except Exception as e:
        print(f"   ❌ Error processing {ticker_symbol}: {str(e)}")
        return {
            'ticker': ticker_symbol.upper(),
            'error': str(e)
        }


@app.route('/api/wheel-screener', methods=['GET', 'POST'])
def wheel_screener():
    """
    Endpoint to screen stocks for wheel options strategy
    Accepts tickers via GET query params or POST JSON body
    """
    try:
        # Handle GET request with query parameters
        if request.method == 'GET':
            tickers_param = request.args.get('tickers', '')
            if tickers_param:
                tickers = [t.strip() for t in tickers_param.split(',')]
            else:
                # Default tickers if none provided
                tickers = ['SOFI', 'F', 'BAC', 'PFE', 'KO']
        
        # Handle POST request with JSON body
        else:
            data = request.get_json()
            if not data or 'tickers' not in data:
                return jsonify({
                    'error': 'Request must include "tickers" list in JSON body'
                }), 400
            
            tickers = data['tickers']
            
            if not isinstance(tickers, list):
                return jsonify({
                    'error': '"tickers" must be a list'
                }), 400
        
        if len(tickers) == 0:
            return jsonify({
                'error': 'Tickers list cannot be empty'
            }), 400
        
        print(f"\n🔍 Processing {len(tickers)} tickers...")
        
        # Process each ticker
        results = []
        for ticker in tickers:
            if isinstance(ticker, str):
                ticker_data = get_ticker_data(ticker.strip())
                results.append(ticker_data)
        
        print(f"\n✅ Successfully processed {len(results)} tickers\n")
        
        return jsonify({
            'success': True,
            'count': len(results),
            'timestamp': datetime.now().isoformat(),
            'data': results
        }), 200
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({
            'error': f'Internal server error: {str(e)}'
        }), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    """
    Health check endpoint
    """
    return jsonify({
        'status': 'healthy',
        'service': 'Wheel Options Screener API',
        'data_source': 'Tradier',
        'timestamp': datetime.now().isoformat()
    }), 200


if __name__ == '__main__':
    print(f"\n🚀 Starting Wheel Screener API with Tradier")
    print(f"📡 API Base URL: {TRADIER_BASE_URL}")
    print(f"🔑 API Key: {TRADIER_API_KEY[:10]}...")
    print(f"\n")
    
    app.run(debug=True, host='0.0.0.0', port=5000)
