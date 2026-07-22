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
        # the bot only for it to crash again), we drop into an infinite sleep.
        # This keeps the process alive but halts all trading.
        while True:
            time.sleep(60)

def engage_kill_switch():
    """
    Creates the HALT file to trigger the kill switch.
    """
    halt_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "HALT")
    with open(halt_file_path, "w") as f:
        f.write("HALTED")
    print(f"Kill switch engaged. Created {halt_file_path}")
    log_system_event('kill_switch', f"Kill switch manually engaged. Created {halt_file_path}")
