import os
import sys

def check_kill_switch():
    """
    Checks for the existence of the HALT file in the root directory.
    If it exists, raises an exception to immediately halt operations.
    """
    halt_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "HALT")
    if os.path.exists(halt_file_path):
        print(f"CRITICAL: Kill switch activated! HALT file found at {halt_file_path}")
        # In a real async/loop environment, you might raise a custom exception 
        # that the main loop catches to cleanly shut down, or just exit.
        sys.exit(1)

def engage_kill_switch():
    """
    Creates the HALT file to trigger the kill switch.
    """
    halt_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "HALT")
    with open(halt_file_path, "w") as f:
        f.write("HALTED")
    print(f"Kill switch engaged. Created {halt_file_path}")
