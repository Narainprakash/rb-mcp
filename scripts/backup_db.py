#!/usr/bin/env python
"""
Takes a database snapshot. Intended for a systemd timer or cron - see the
Backups section of the README.

    venv/bin/python scripts/backup_db.py

Safe to run while the services are up: it uses SQLite's online backup API, not
a file copy.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.backup import backup_database
from src.core.config import system_config
from src.core.db import DB_PATH, log_system_event

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ops = system_config.settings.get("ops", {})
    dest_dir = ops.get("backup_dir") or os.path.join(PROJECT_ROOT, "backups")
    if not os.path.isabs(dest_dir):
        dest_dir = os.path.join(PROJECT_ROOT, dest_dir)
    retention = ops.get("backup_retention", 14)

    try:
        path = backup_database(DB_PATH, dest_dir, retention=retention, label="scheduled")
    except Exception as e:
        # Exit non-zero so the timer surfaces the failure in systemctl status
        # rather than silently never producing a backup.
        print(f"ERROR: backup failed: {e}", file=sys.stderr)
        try:
            log_system_event("error", f"Scheduled database backup failed: {e}")
        except Exception:
            pass
        return 1

    size_mb = os.path.getsize(path) / (1024 * 1024)
    print(f"Backup written: {path} ({size_mb:.1f} MB, keeping {retention})")
    try:
        log_system_event("system", f"Database backup written to {os.path.basename(path)}")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
