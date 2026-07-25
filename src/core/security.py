import os
import sys
import time
from src.core.db import get_connection, log_system_event

def check_kill_switch():
    """
    Checks for the existence of the HALT file in the root directory.
    If it exists, raises an exception to immediately halt operations.
    """
    halt_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "HALT")
    if os.path.exists(halt_file_path):
        print(f"CRITICAL: Kill switch activated! HALT file found at {halt_file_path}")
        
        # Prevent logging spam during systemd restart loops
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT event_type FROM system_events ORDER BY timestamp DESC LIMIT 1")
            row = cursor.fetchone()
            conn.close()
            if not row or row['event_type'] != 'kill_switch':
                log_system_event('kill_switch', f"Kill switch activated. HALT file found at {halt_file_path}")
        except Exception as e:
            print(f"Failed to log kill switch event: {e}")

        # To prevent a systemd crash loop (where systemctl continuously restarts
        # the bot only for it to crash again), we wait in place instead of exiting.
        # This keeps the process alive but halts all trading. We re-check the file
        # each pass so removing HALT (rm HALT / resume_trading) actually resumes
        # the loops without needing a service restart.
        while os.path.exists(halt_file_path):
            time.sleep(5)

        print("HALT file removed. Resuming operations.")
        try:
            log_system_event('resume', 'HALT file removed. Trading resumed.')
        except Exception as e:
            print(f"Failed to log resume event: {e}")

def check_secret_file_permissions():
    """Warns if .env is readable beyond its owner.

    Spec 9.2 requires mode 600 on secrets. Warn rather than refuse to start:
    the file being over-permissive is a problem, but a bot that will not run
    manages no open positions, which is a bigger one.
    """
    if os.name == 'nt':
        return  # POSIX mode bits aren't meaningful on Windows
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env")
    if not os.path.exists(env_path):
        return
    mode = os.stat(env_path).st_mode & 0o777
    if mode & 0o077:
        message = f".env is group/world readable (mode {oct(mode)}). Run: chmod 600 {env_path}"
        print(f"WARNING: {message}")
        try:
            log_system_event('security_warning', message)
        except Exception:
            pass


def trading_halted():
    """True when the HALT_TRADING file exists.

    The granular halt from spec 9.1.4: stop opening new positions while the
    poller and the exit monitor keep running. The full HALT file also stops
    exit management, which can be worse than doing nothing when positions are
    open, so this is usually the switch you actually want.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "HALT_TRADING")
    return os.path.exists(path)

def engage_kill_switch():
    """
    Creates the HALT file to trigger the kill switch.
    """
    halt_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "HALT")
    with open(halt_file_path, "w") as f:
        f.write("HALTED")
    print(f"Kill switch engaged. Created {halt_file_path}")
    log_system_event('kill_switch', f"Kill switch manually engaged. Created {halt_file_path}")
