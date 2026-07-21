import requests
from src.core.config import config

def send_discord_message(event_type: str, message: str):
    """
    Sends a message to the Discord webhook if the event_type is enabled in config.
    """
    if not config.discord_webhook_url:
        return # Webhook not configured
    
    enabled_events = config.notifications.get('events_enabled', [])
    if event_type not in enabled_events:
        return # Event type not enabled for notification
        
    payload = {
        "content": message
    }
    
    try:
        response = requests.post(config.discord_webhook_url, json=payload)
        response.raise_for_status()
    except Exception as e:
        print(f"Failed to send Discord notification: {e}")

def send_whatsapp_message(event_type: str, message: str):
    """
    Sends a message via the local Hermes WhatsApp bridge using the 'hermes send' CLI.
    """
    import subprocess
    
    enabled_events = config.notifications.get('events_enabled', [])
    if event_type not in enabled_events:
        return # Event type not enabled for notification
        
    try:
        # We use the default 'whatsapp' target which maps to the Home Channel in Hermes
        subprocess.run(["hermes", "send", "--to", "whatsapp", message], check=True, capture_output=True)
    except Exception as e:
        print(f"Failed to send WhatsApp notification: {e}")

# Convenience functions for specific event types
def notify_alert(action, ticker, expiry, strike, option_type, price, style):
    style_str = f" ({style})" if style else ""
    msg = f"ALERT — {action} ${ticker} {expiry} {strike}{option_type} @ {price}{style_str}"
    send_discord_message("alert", msg)
    send_whatsapp_message("alert", msg)

def notify_add(ticker, expiry, strike, option_type, price):
    msg = f"ADD — ${ticker} {expiry} {strike}{option_type} AVG {price}"
    send_discord_message("add", msg)
    send_whatsapp_message("add", msg)

def notify_review_needed(reason):
    msg = f"REVIEW NEEDED — couldn't confidently parse tweet: \"{reason}\""
    send_discord_message("review", msg)
    send_whatsapp_message("review", msg)

def notify_executed_live(quantity, ticker, strike, option_type, price):
    msg = f"EXECUTED — {quantity}x ${ticker} {strike}{option_type} @ {price} (paper: false)"
    send_discord_message("executed", msg)
    send_whatsapp_message("executed", msg)

def notify_executed_paper(quantity, ticker, strike, option_type, price):
    msg = f"PAPER TRADE — {quantity}x ${ticker} {strike}{option_type} @ {price}"
    send_discord_message("paper", msg)
    send_whatsapp_message("paper", msg)

def notify_limit_sell_placed(quantity, ticker, strike, option_type, price, pct):
    msg = f"LIMIT SELL PLACED — {quantity}x ${ticker} {strike}{option_type} @ {price} ({pct}% target)"
    send_discord_message("limit_sell_placed", msg)
    send_whatsapp_message("limit_sell_placed", msg)

def notify_limit_buy_placed(quantity, ticker, strike, option_type, price, pct):
    msg = f"LIMIT BUY PLACED — {quantity}x ${ticker} {strike}{option_type} @ {price} ({pct}% discount)"
    send_discord_message("limit_buy_placed", msg)
    send_whatsapp_message("limit_buy_placed", msg)

def notify_limit_sell_filled(quantity, ticker, strike, option_type, price, pnl_dollars, pnl_pct):
    msg = f"SOLD — {quantity}x ${ticker} {strike}{option_type} @ {price} — P/L: ${pnl_dollars} (+{pnl_pct}%)"
    send_discord_message("limit_sell_filled", msg)
    send_whatsapp_message("limit_sell_filled", msg)

def notify_skipped(ask, recommended, tolerance):
    msg = f"SKIPPED — ask {ask} exceeds {recommended} +{tolerance}% tolerance"
    send_discord_message("skipped", msg)
    send_whatsapp_message("skipped", msg)

def notify_kill_switch():
    msg = "KILL SWITCH ACTIVE — all trading halted"
    send_discord_message("kill_switch", msg)
    send_whatsapp_message("kill_switch", msg)

def notify_error(component, error_msg):
    msg = f"ERROR — [{component}] — [{error_msg}]"
    send_discord_message("error", msg)
    send_whatsapp_message("error", msg)
