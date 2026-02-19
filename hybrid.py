#!/usr/bin/env python3
"""
══════════════════════════════════════════════════════════════════════════════
 ██╗  ██╗██╗   ██╗██████╗ ██████╗ ██╗██████╗ 
 ██║  ██║╚██╗ ██╔╝██╔══██╗██╔══██╗██║██╔══██╗
 ███████║ ╚████╔╝ ██████╔╝██████╔╝██║██║  ██║
 ██╔══██║  ╚██╔╝  ██╔══██╗██╔══██╗██║██║  ██║
 ██║  ██║   ██║   ██████╔╝██║  ██║██║██████╔╝
 ╚═╝  ╚═╝   ╚═╝   ╚═════╝ ╚═╝  ╚═╝╚═╝╚═════╝ 
══════════════════════════════════════════════════════════════════════════════
 HYBRID - Swing Sniper Brain + Simple Beast Muscle
 "Buy cheap, hedge smart, let the math print"
══════════════════════════════════════════════════════════════════════════════

STRATEGY (Whale-Inspired):
1. Watch the 5-min window for cheap sides (under 35¢)
2. Side gets cheap → FOK sweep it (grab stale liquidity)
3. Other side gets cheap too → buy it (arbitrage lock)
4. Only one side bought at 30s left → FOK+buffer hedge
5. Don't predict direction — buy cheap, let payout math work

THE EDGE:
- 5-min markets reprice slowly
- Market makers leave stale asks on the book
- FOK sweeps grab those before anyone else
- Buy at 10-35¢, payout is $1 = massive R/R
"""

import os
import sys
import json
import time
import logging
import requests
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify

# ══════════════════════════════════════════════════════════════════════════════
# FLASK APP
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

BET_AMOUNT = float(os.getenv('BET_AMOUNT', '5'))

# Buy threshold — buy a side when it's this cheap or less
BUY_THRESHOLD = float(os.getenv('BUY_THRESHOLD', '0.35'))

# Hedge settings
HEDGE_TIME = int(os.getenv('HEDGE_TIME', '30'))           # Hedge at 30s remaining
HEDGE_PRICE_BUFFER = float(os.getenv('HEDGE_PRICE_BUFFER', '0.15'))  # 15% buffer on hedge FOK
HEDGE_MULTIPLIER = float(os.getenv('HEDGE_MULTIPLIER', '1.5'))       # Aggressive 1.5x hedge
SMART_HEDGE_FLOOR = float(os.getenv('SMART_HEDGE_FLOOR', '0.25'))    # Only hedge if buy price > 25¢

# Kill switch & cooldown
KILL_SWITCH_LOSS = float(os.getenv('KILL_SWITCH_LOSS', '15'))        # Stop after losing $15
COOLDOWN_MINUTES = int(os.getenv('COOLDOWN_MINUTES', '15'))          # Pause 15 min after cold streak
NO_WIN_TIMEOUT = int(os.getenv('NO_WIN_TIMEOUT', '30'))              # Trigger cooldown after 30 min no wins

# Timing
CHECK_INTERVAL = float(os.getenv('CHECK_INTERVAL', '0.5'))
STOP_BEFORE_END = int(os.getenv('STOP_BEFORE_END', '10'))

# Polymarket
PRIVATE_KEY = os.getenv('PRIVATE_KEY', '')
FUNDER_ADDRESS = os.getenv('FUNDER_ADDRESS', '')
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID = 137

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL STATE
# ══════════════════════════════════════════════════════════════════════════════

clob_client = None
client_ready = False
executor = ThreadPoolExecutor(max_workers=4)
bot_running = False

position = {
    'up_bought': False,
    'down_bought': False,
    'up_price': 0,
    'down_price': 0,
    'up_shares': 0,
    'down_shares': 0,
    'hedged': False,
}

stats = {
    'windows': 0,
    'wins': 0,
    'losses': 0,
    'hedges': 0,
    'arbitrages': 0,
    'skips': 0,
    'total_profit': 0,
    'session_start': 0,
    'last_win_time': 0,
    'killed': False,
    'cooldown_until': 0,
}

# ══════════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def home():
    return jsonify({
        'bot': 'Hybrid',
        'status': 'running' if bot_running else 'stopped',
        'strategy': 'Buy cheap sides, hedge smart',
        'position': position,
        'stats': stats,
    })

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

# ══════════════════════════════════════════════════════════════════════════════
# CLIENT
# ══════════════════════════════════════════════════════════════════════════════

def init_client():
    global clob_client, client_ready
    
    if not PRIVATE_KEY:
        log.error("❌ No PRIVATE_KEY set!")
        return False
    
    try:
        from py_clob_client.client import ClobClient
        
        log.info("🔑 Initializing client...")
        
        client = ClobClient(
            host=CLOB_HOST,
            chain_id=CHAIN_ID,
            key=PRIVATE_KEY,
            signature_type=1,
            funder=FUNDER_ADDRESS if FUNDER_ADDRESS else None
        )
        
        creds = client.derive_api_key()
        if creds:
            client.set_api_creds(creds)
            clob_client = client
            client_ready = True
            log.info("✅ Client authenticated!")
            return True
        
    except Exception as e:
        log.error(f"❌ Client init failed: {e}")
    
    return False

# ══════════════════════════════════════════════════════════════════════════════
# MARKET DATA
# ══════════════════════════════════════════════════════════════════════════════

def get_current_window():
    now = datetime.now(timezone.utc)
    window_min = (now.minute // 5) * 5
    window_start = now.replace(minute=window_min, second=0, microsecond=0)
    window_end = window_start + timedelta(minutes=5)
    seconds_elapsed = (now - window_start).total_seconds()
    seconds_remaining = (window_end - now).total_seconds()
    return window_start, window_end, seconds_elapsed, seconds_remaining


def get_market_data(window_start: datetime):
    """Get market tokens and prices"""
    try:
        ts = int(window_start.timestamp())
        slug = f"btc-updown-5m-{ts}"
        
        r = requests.get(f"{GAMMA_API}/markets?slug={slug}", timeout=3)
        if r.ok and r.json():
            m = r.json()[0]
            cid = m['conditionId']
            
            r2 = requests.get(f"{CLOB_HOST}/markets/{cid}", timeout=3)
            if r2.ok:
                data = r2.json()
                tokens = data.get('tokens', [])
                
                result = {'slug': slug, 'condition_id': cid}
                
                for t in tokens:
                    outcome = t.get('outcome', '').lower()
                    token_id = t['token_id']
                    
                    # Use clob_client.get_price for REAL book price (not stale API price)
                    try:
                        if client_ready:
                            price = float(clob_client.get_price(token_id, side="BUY"))
                        else:
                            price = float(t.get('price', 0.5))
                    except:
                        price = float(t.get('price', 0.5))
                    
                    if 'up' in outcome:
                        result['up_token'] = token_id
                        result['up_price'] = price
                    elif 'down' in outcome:
                        result['down_token'] = token_id
                        result['down_price'] = price
                
                if 'up_token' in result and 'down_token' in result:
                    return result
    except Exception as e:
        log.debug(f"Market fetch error: {e}")
    return None

# ══════════════════════════════════════════════════════════════════════════════
# ORDER PLACEMENT
# ══════════════════════════════════════════════════════════════════════════════

def place_fok_order(token_id: str, amount: float):
    """Standard FOK market order — for entries on cheap sides"""
    if not client_ready:
        return False, "Client not ready", 0
    
    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        
        amount = round(amount, 2)
        if amount < 1:
            amount = 1.0
        
        market_order = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=BUY,
        )
        
        start = time.time()
        signed_order = clob_client.create_market_order(market_order)
        resp = clob_client.post_order(signed_order, OrderType.FOK)
        elapsed = (time.time() - start) * 1000
        
        if resp and resp.get('success') and resp.get('status') == 'matched':
            taking_amount = float(resp.get('takingAmount', 0))
            log.info(f"   📝 FOK matched: {taking_amount:.1f} shares for ${amount}")
            return True, f"{elapsed:.0f}ms", taking_amount
        else:
            log.warning(f"   ⚠️ FOK not matched: {resp}")
            return False, "FOK not matched", 0
            
    except Exception as e:
        return False, f"FOK Error: {str(e)}", 0


def place_fok_buffered_order(token_id: str, amount: float):
    """FOK with price buffer — for hedge orders where we MUST fill"""
    if not client_ready:
        return False, "Client not ready", 0
    
    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        
        buffered_amount = round(amount * (1 + HEDGE_PRICE_BUFFER), 2)
        if buffered_amount < 1:
            buffered_amount = 1.0
        
        log.info(f"   💰 FOK hedge: ${amount:.2f} + {HEDGE_PRICE_BUFFER:.0%} buffer = ${buffered_amount:.2f}")
        
        market_order = MarketOrderArgs(
            token_id=token_id,
            amount=buffered_amount,
            side=BUY,
        )
        
        start = time.time()
        signed_order = clob_client.create_market_order(market_order)
        resp = clob_client.post_order(signed_order, OrderType.FOK)
        elapsed = (time.time() - start) * 1000
        
        if resp and resp.get('success') and resp.get('status') == 'matched':
            taking_amount = float(resp.get('takingAmount', 0))
            log.info(f"   📝 FOK+buffer matched: {taking_amount:.1f} shares for ${buffered_amount}")
            return True, f"{elapsed:.0f}ms", taking_amount
        else:
            log.warning(f"   ⚠️ FOK+buffer not matched: {resp}")
            return False, "FOK+buffer not matched", 0
            
    except Exception as e:
        return False, f"FOK+buffer Error: {str(e)}", 0

# ══════════════════════════════════════════════════════════════════════════════
# MAIN TRADING LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def reset_position():
    global position
    position = {
        'up_bought': False,
        'down_bought': False,
        'up_price': 0,
        'down_price': 0,
        'up_shares': 0,
        'down_shares': 0,
        'hedged': False,
    }


def trade_window(window_start: datetime, window_end: datetime):
    """Trade a single 5-minute window"""
    global position, stats
    
    reset_position()
    stats['windows'] += 1
    
    log.info("")
    log.info("═" * 60)
    log.info(f"🎯 WINDOW: {window_start.strftime('%H:%M:%S')} - {window_end.strftime('%H:%M:%S')} UTC")
    log.info("═" * 60)
    
    # Get market
    market = get_market_data(window_start)
    if not market:
        log.warning("   ⚠️ Could not get market")
        return
    
    log.info(f"   📍 {market['slug']}")
    log.info(f"   🎯 Hunting for sides under {BUY_THRESHOLD:.0%}...")
    
    last_log = 0
    
    while True:
        _, _, seconds_elapsed, seconds_remaining = get_current_window()
        
        # Window ending
        if seconds_remaining < STOP_BEFORE_END:
            break
        
        # New window started (shouldn't happen but safety check)
        current_start, _, _, _ = get_current_window()
        if current_start != window_start:
            break
        
        # Refresh market prices
        market = get_market_data(window_start)
        if not market:
            time.sleep(CHECK_INTERVAL)
            continue
        
        up_price = market['up_price']
        down_price = market['down_price']
        
        # Log every 10 seconds
        if int(seconds_elapsed) - last_log >= 10:
            status = ""
            if position['up_bought'] and position['down_bought']:
                total = position['up_price'] + position['down_price']
                status = f" | 💰 BOTH SIDES ({total:.0%} total)"
            elif position['up_bought']:
                status = f" | ✅ UP @ {position['up_price']:.0%}"
            elif position['down_bought']:
                status = f" | ✅ DOWN @ {position['down_price']:.0%}"
            
            log.info(f"   [{seconds_elapsed:5.0f}s] UP: {up_price:.0%} | DOWN: {down_price:.0%}{status}")
            last_log = int(seconds_elapsed)
        
        # ═══ ENTRY: BUY CHEAP SIDES ═══
        
        # Buy UP if cheap
        if not position['up_bought'] and up_price <= BUY_THRESHOLD:
            log.info(f"   🔥 UP is CHEAP at {up_price:.0%}! Sweeping...")
            
            success, msg, shares = place_fok_order(market['up_token'], BET_AMOUNT)
            
            if success:
                position['up_bought'] = True
                position['up_price'] = up_price
                position['up_shares'] = shares
                log.info(f"   ✅ BOUGHT {shares:.1f} UP @ {up_price:.0%} ({msg})")
                
                # Check if we just completed an arbitrage
                if position['down_bought']:
                    total = position['up_price'] + position['down_price']
                    log.info("")
                    log.info("   🎰 ════════════════════════════════════════")
                    log.info(f"   🎰  BOTH SIDES LOCKED!")
                    log.info(f"   🎰  UP: {position['up_price']:.0%} + DOWN: {position['down_price']:.0%} = {total:.0%}")
                    if total < 1.0:
                        log.info(f"   🎰  GUARANTEED PROFIT: {(1-total)*100:.1f}%!")
                        stats['arbitrages'] += 1
                    else:
                        log.info(f"   🎰  HEDGED (small cost: {(total-1)*100:.1f}%)")
                    log.info("   🎰 ════════════════════════════════════════")
                    log.info("")
            else:
                log.error(f"   ❌ UP order failed: {msg}")
        
        # Buy DOWN if cheap
        if not position['down_bought'] and down_price <= BUY_THRESHOLD:
            log.info(f"   🔥 DOWN is CHEAP at {down_price:.0%}! Sweeping...")
            
            success, msg, shares = place_fok_order(market['down_token'], BET_AMOUNT)
            
            if success:
                position['down_bought'] = True
                position['down_price'] = down_price
                position['down_shares'] = shares
                log.info(f"   ✅ BOUGHT {shares:.1f} DOWN @ {down_price:.0%} ({msg})")
                
                # Check if we just completed an arbitrage
                if position['up_bought']:
                    total = position['up_price'] + position['down_price']
                    log.info("")
                    log.info("   🎰 ════════════════════════════════════════")
                    log.info(f"   🎰  BOTH SIDES LOCKED!")
                    log.info(f"   🎰  UP: {position['up_price']:.0%} + DOWN: {position['down_price']:.0%} = {total:.0%}")
                    if total < 1.0:
                        log.info(f"   🎰  GUARANTEED PROFIT: {(1-total)*100:.1f}%!")
                        stats['arbitrages'] += 1
                    else:
                        log.info(f"   🎰  HEDGED (small cost: {(total-1)*100:.1f}%)")
                    log.info("   🎰 ════════════════════════════════════════")
                    log.info("")
            else:
                log.error(f"   ❌ DOWN order failed: {msg}")
        
        # ═══ SMART HEDGE: ONLY PROTECT EXPENSIVE BUYS ═══
        
        one_side_only = (position['up_bought'] != position['down_bought'])
        
        if one_side_only and not position['hedged'] and seconds_remaining <= HEDGE_TIME:
            
            if position['up_bought'] and not position['down_bought']:
                our_side = 'UP'
                our_price = position['up_price']
                our_shares = position['up_shares']
                hedge_side = 'DOWN'
                hedge_token = market['down_token']
                hedge_price = down_price
            else:
                our_side = 'DOWN'
                our_price = position['down_price']
                our_shares = position['down_shares']
                hedge_side = 'UP'
                hedge_token = market['up_token']
                hedge_price = up_price
            
            # SMART HEDGE: Only hedge if we paid more than 25¢
            # Cheap buys (under 25¢) are lottery tickets — accept the loss
            # Expensive buys (over 25¢) hurt more — protect them
            if our_price > SMART_HEDGE_FLOOR:
                hedge_shares = our_shares * HEDGE_MULTIPLIER
                hedge_amount = hedge_shares * hedge_price
                
                log.info(f"   ⏰ {seconds_remaining:.0f}s LEFT! {our_side} @ {our_price:.0%} > {SMART_HEDGE_FLOOR:.0%} floor — HEDGING!")
                log.info(f"   🔥 HEDGE: Buying {hedge_shares:.1f} {hedge_side} @ {hedge_price:.0%} (1.5x aggressive)")
                
                success, msg, shares = place_fok_buffered_order(hedge_token, hedge_amount)
                
                if success:
                    position['hedged'] = True
                    if hedge_side == 'DOWN':
                        position['down_bought'] = True
                        position['down_price'] = hedge_price
                        position['down_shares'] = shares
                    else:
                        position['up_bought'] = True
                        position['up_price'] = hedge_price
                        position['up_shares'] = shares
                    
                    stats['hedges'] += 1
                    
                    total = position['up_price'] + position['down_price']
                    
                    entry_cost = our_shares * our_price
                    hedge_cost = shares * hedge_price
                    total_spent = entry_cost + hedge_cost
                    
                    if hedge_side == 'DOWN':
                        if_up_wins = (our_shares * 1.0) - total_spent
                        if_down_wins = (shares * 1.0) - total_spent
                    else:
                        if_up_wins = (shares * 1.0) - total_spent
                        if_down_wins = (our_shares * 1.0) - total_spent
                    
                    log.info(f"   🛡️ HEDGED! Total: {total:.0%}")
                    log.info(f"   📊 If UP wins: ${if_up_wins:+.2f}")
                    log.info(f"   📊 If DOWN wins: ${if_down_wins:+.2f}")
                else:
                    log.error(f"   ❌ Hedge failed: {msg}")
            else:
                log.info(f"   ⏰ {seconds_remaining:.0f}s LEFT! {our_side} @ {our_price:.0%} ≤ {SMART_HEDGE_FLOOR:.0%} — cheap lottery ticket, no hedge 🎲")
        
        # If both sides bought, just monitor
        if position['up_bought'] and position['down_bought']:
            time.sleep(1)
        else:
            time.sleep(CHECK_INTERVAL)
    
    # ═══ WINDOW END — RESULTS ═══
    log.info("")
    log.info("   ─────────────────────────────────────")
    
    if position['up_bought'] or position['down_bought']:
        if position['up_bought'] and position['down_bought']:
            total = position['up_price'] + position['down_price']
            
            if total < 1.0:
                # Arbitrage — guaranteed profit regardless of outcome
                # Profit based on smaller side (min shares)
                min_shares = min(position['up_shares'], position['down_shares'])
                profit = min_shares * (1.0 - total)
                stats['wins'] += 1
                stats['total_profit'] += profit
                stats['last_win_time'] = time.time()
                log.info(f"   💰 ARBITRAGE! UP {position['up_price']:.0%} + DOWN {position['down_price']:.0%} = {total:.0%}")
                log.info(f"   💰 Guaranteed profit: ${profit:.2f}")
            else:
                # Hedged — small loss or breakeven
                cost = (total - 1.0) * min(position['up_shares'], position['down_shares'])
                stats['total_profit'] -= cost
                if position['hedged']:
                    stats['hedges'] += 0  # Already counted
                log.info(f"   🛡️ HEDGED: {total:.0%} total (cost: ${cost:.2f})")
        
        elif position['up_bought']:
            # Only UP — need to check if we won
            market = get_market_data(window_start)
            if market and market['up_price'] >= 0.5:
                profit = (1.0 - position['up_price']) * position['up_shares']
                stats['wins'] += 1
                stats['total_profit'] += profit
                stats['last_win_time'] = time.time()
                log.info(f"   ✅ WON! UP @ {position['up_price']:.0%} → ${profit:.2f} profit")
            else:
                loss = position['up_price'] * position['up_shares']
                stats['losses'] += 1
                stats['total_profit'] -= loss
                log.info(f"   ❌ LOST! UP @ {position['up_price']:.0%} → ${loss:.2f} loss")
        
        elif position['down_bought']:
            # Only DOWN
            market = get_market_data(window_start)
            if market and market['down_price'] >= 0.5:
                profit = (1.0 - position['down_price']) * position['down_shares']
                stats['wins'] += 1
                stats['total_profit'] += profit
                stats['last_win_time'] = time.time()
                log.info(f"   ✅ WON! DOWN @ {position['down_price']:.0%} → ${profit:.2f} profit")
            else:
                loss = position['down_price'] * position['down_shares']
                stats['losses'] += 1
                stats['total_profit'] -= loss
                log.info(f"   ❌ LOST! DOWN @ {position['down_price']:.0%} → ${loss:.2f} loss")
    else:
        stats['skips'] += 1
        log.info("   ⏭️ No cheap sides this window")
    
    log.info(f"   📈 STATS: {stats['wins']}W / {stats['losses']}L / {stats['hedges']}H / {stats['arbitrages']}A | ${stats['total_profit']:.2f}")


def main_loop():
    global bot_running
    
    log.info("🎯 Starting Hybrid...")
    bot_running = True
    stats['session_start'] = time.time()
    stats['last_win_time'] = time.time()
    
    last_window = None
    
    while bot_running:
        try:
            # ═══ KILL SWITCH CHECK ═══
            if stats['total_profit'] <= -KILL_SWITCH_LOSS:
                if not stats['killed']:
                    stats['killed'] = True
                    log.info("")
                    log.info("   🛑 ════════════════════════════════════════")
                    log.info(f"   🛑  KILL SWITCH: Lost ${abs(stats['total_profit']):.2f}")
                    log.info(f"   🛑  Limit was ${KILL_SWITCH_LOSS}. Shutting down.")
                    log.info(f"   🛑  Bankroll preserved. Come back tomorrow.")
                    log.info("   🛑 ════════════════════════════════════════")
                    log.info("")
                time.sleep(60)
                continue
            
            # ═══ COOLDOWN CHECK ═══
            now = time.time()
            
            # Check if we're in cooldown
            if stats['cooldown_until'] > now:
                remaining = int(stats['cooldown_until'] - now)
                if remaining % 60 == 0 and remaining > 0:
                    log.info(f"   ❄️ Cooldown: {remaining // 60}m remaining...")
                time.sleep(10)
                continue
            
            # Check if we need to enter cooldown (no wins in 30 min)
            minutes_since_win = (now - stats['last_win_time']) / 60
            if stats['wins'] > 0 and minutes_since_win >= NO_WIN_TIMEOUT:
                stats['cooldown_until'] = now + (COOLDOWN_MINUTES * 60)
                log.info("")
                log.info(f"   ❄️ No wins in {NO_WIN_TIMEOUT} min. Cooling down for {COOLDOWN_MINUTES} min...")
                log.info(f"   ❄️ Market might be dead. Will resume at {datetime.fromtimestamp(stats['cooldown_until'], tz=timezone.utc).strftime('%H:%M:%S')} UTC")
                log.info("")
                stats['last_win_time'] = now  # Reset so we don't immediately re-trigger
                continue
            
            window_start, window_end, _, _ = get_current_window()
            
            if window_start != last_window:
                last_window = window_start
                trade_window(window_start, window_end)
            
            time.sleep(0.5)
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(1)


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

def start_bot():
    log.info("")
    log.info("╔" + "═" * 58 + "╗")
    log.info("║                                                          ║")
    log.info("║     ⚡ HYBRID - Cheap Side Sniper + Smart Hedge ⚡       ║")
    log.info("║     'Buy cheap, hedge smart, let the math print'        ║")
    log.info("║                                                          ║")
    log.info("╠" + "═" * 58 + "╣")
    log.info(f"║   Bet Amount:      ${BET_AMOUNT}".ljust(59) + "║")
    log.info(f"║   Buy Threshold:   ≤ {BUY_THRESHOLD:.0%} (cheap side trigger)".ljust(59) + "║")
    log.info(f"║   Hedge at:        {HEDGE_TIME}s remaining".ljust(59) + "║")
    log.info(f"║   Hedge Buffer:    {HEDGE_PRICE_BUFFER:.0%} (FOK overshoot)".ljust(59) + "║")
    log.info(f"║   Hedge Size:      {HEDGE_MULTIPLIER}x aggressive".ljust(59) + "║")
    log.info("╠" + "═" * 58 + "╣")
    log.info("║   STRATEGY:                                              ║")
    log.info("║   • Scan for cheap sides (≤ 35¢)                         ║")
    log.info("║   • FOK sweep stale liquidity                            ║")
    log.info("║   • Both sides cheap? → Arbitrage lock                   ║")
    log.info(f"║   • Smart Hedge: only if buy > {SMART_HEDGE_FLOOR:.0%}".ljust(59) + "║")
    log.info(f"║   • Kill Switch: stops after -${KILL_SWITCH_LOSS:.0f}".ljust(59) + "║")
    log.info(f"║   • Cooldown: {COOLDOWN_MINUTES}m pause after {NO_WIN_TIMEOUT}m no wins".ljust(59) + "║")
    log.info("║   • 🔥 Buy at 10-35¢, payout $1 = massive R/R 🔥        ║")
    log.info("╚" + "═" * 58 + "╝")
    log.info("")
    
    if not init_client():
        log.error("❌ Failed to initialize client")
        return
    
    thread = threading.Thread(target=main_loop, daemon=True)
    thread.start()
    log.info("🚀 Hybrid started!")


if __name__ == "__main__":
    start_bot()
    
    port = int(os.getenv('PORT', 5000))
    log.info(f"🌐 Starting server on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=False)
