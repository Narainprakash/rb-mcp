import requests
import subprocess
from src.core.config import get_user_config, system_config
from src.core.db import log_system_event, get_connection

def get_active_user_ids():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE is_active = 1")
        return [row['id'] for row in cursor.fetchall()]
    finally:
        conn.close()

def send_discord_message(user_id: int, event_type: str, message: str):
    """
    Sends a message to the user's Discord webhook if the event_type is enabled.
    Each user may set their own notifications.discord_webhook_url so tenants
    don't share a channel; the system-wide DISCORD_WEBHOOK_URL is the fallback.
    """
    config = get_user_config(user_id)
    enabled_events = config.notifications.get('events_enabled', [])
    if event_type not in enabled_events:
        return # Event type not enabled for notification

    webhook_url = config.notifications.get('discord_webhook_url') or system_config.discord_webhook_url
    if not webhook_url:
        return # Webhook not configured

    payload = {
        "content": message
    }

    try:
        response = requests.post(webhook_url, json=payload)
        response.raise_for_status()
    except Exception as e:
        print(f"Failed to send Discord notification for User {user_id}: {e}")

def send_whatsapp_message(user_id: int, event_type: str, message: str):
    """
    Sends a message via the local Hermes WhatsApp bridge using the 'hermes send' CLI
    to all configured whatsapp_trade_targets for this user.
    """
    config = get_user_config(user_id)
    enabled_events = config.notifications.get('events_enabled', [])
    if event_type not in enabled_events:
        return # Event type not enabled for notification
        
    targets = config.notifications.get('whatsapp_trade_targets', ["whatsapp"])
    for target in targets:
        try:
            subprocess.run(["hermes", "send", "--to", target, message], check=True, capture_output=True)
        except Exception as e:
            print(f"Failed to send WhatsApp notification to {target} (User {user_id}): {e}")

def forward_raw_tweet(raw_text: str):
    """
    Forwards the exact raw tweet text to all configured whatsapp_forward_targets for ALL active users.
    """
    for user_id in get_active_user_ids():
        config = get_user_config(user_id)
        targets = config.notifications.get('whatsapp_forward_targets', ["whatsapp"])
        for target in targets:
            try:
                subprocess.run(["hermes", "send", "--to", target, raw_text], check=True, capture_output=True)
            except Exception as e:
                print(f"Failed to forward raw tweet to {target} (User {user_id}): {e}")

def notify_alert(ticker: str, expiry: str, strike: float, option_type: str, price: float, style: str, action: str = "BTO"):
    """Global alert, notifies all active users if enabled."""
    style_str = f" ({style})" if style else ""
    msg = f"ALERT — {action} ${ticker} {expiry} {strike}{option_type} @ {price:.2f}{style_str}"
    for user_id in get_active_user_ids():
        send_discord_message(user_id, "alert", msg)
        send_whatsapp_message(user_id, "alert", msg)

def notify_add(ticker, expiry, strike, option_type, price):
    """Global alert for ADD signal."""
    msg = f"ADD — ${ticker} {expiry} {strike}{option_type} AVG {price:.2f}"
    for user_id in get_active_user_ids():
        send_discord_message(user_id, "add", msg)
        send_whatsapp_message(user_id, "add", msg)

def notify_review_needed(reason):
    """Global alert for parse failures."""
    msg = f"REVIEW NEEDED — couldn't confidently parse tweet: \"{reason}\""
    for user_id in get_active_user_ids():
        send_discord_message(user_id, "review", msg)
        send_whatsapp_message(user_id, "review", msg)

# User-specific convenience functions
def notify_executed_live(user_id: int, quantity, ticker, strike, option_type, price):
    msg = f"EXECUTED — {quantity}x ${ticker} {strike}{option_type} @ {price:.2f} (paper: false)"
    send_discord_message(user_id, "executed", msg)
    send_whatsapp_message(user_id, "executed", msg)

def notify_executed_paper(user_id: int, quantity, ticker, strike, option_type, price):
    msg = f"PAPER TRADE — {quantity}x ${ticker} {strike}{option_type} @ {price:.2f}"
    send_discord_message(user_id, "paper", msg)
    send_whatsapp_message(user_id, "paper", msg)

def notify_limit_sell_placed(user_id: int, quantity, ticker, strike, option_type, price, pct):
    msg = f"LIMIT SELL PLACED — {quantity}x ${ticker} {strike}{option_type} @ {price:.2f} ({pct}% target)"
    send_discord_message(user_id, "limit_sell_placed", msg)
    send_whatsapp_message(user_id, "limit_sell_placed", msg)

def notify_limit_buy_placed(user_id: int, quantity, ticker, strike, option_type, price, pct):
    msg = f"LIMIT BUY PLACED — {quantity}x ${ticker} {strike}{option_type} @ {price:.2f} ({pct}% discount)"
    send_discord_message(user_id, "limit_buy_placed", msg)
    send_whatsapp_message(user_id, "limit_buy_placed", msg)

def notify_limit_sell_filled(user_id: int, quantity, ticker, strike, option_type, price, pnl_dollars, pnl_pct):
    msg = f"SOLD — {quantity}x ${ticker} {strike}{option_type} @ {price:.2f} — P/L: ${pnl_dollars:.2f} (+{pnl_pct:.2f}%)"
    send_discord_message(user_id, "limit_sell_filled", msg)
    send_whatsapp_message(user_id, "limit_sell_filled", msg)

def notify_skipped(user_id: int, ask, recommended, tolerance):
    msg = f"SKIPPED — ask {ask:.2f} exceeds {recommended:.2f} +{tolerance}% tolerance"
    send_discord_message(user_id, "skipped", msg)
    send_whatsapp_message(user_id, "skipped", msg)

def notify_kill_switch():
    """Global kill switch notification."""
    msg = "KILL SWITCH ACTIVE — all trading halted"
    for user_id in get_active_user_ids():
        send_discord_message(user_id, "kill_switch", msg)
        send_whatsapp_message(user_id, "kill_switch", msg)
    log_system_event('kill_switch', msg)

def notify_error(component, error_msg, user_id=None):
    msg = f"ERROR — [{component}] — [{error_msg}]"
    if user_id:
        send_discord_message(user_id, "error", msg)
        send_whatsapp_message(user_id, "error", msg)
    else:
        for uid in get_active_user_ids():
            send_discord_message(uid, "error", msg)
            send_whatsapp_message(uid, "error", msg)
    log_system_event('error', msg, user_id=user_id)

