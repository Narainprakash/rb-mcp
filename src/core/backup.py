"""
Online SQLite backups.

Uses sqlite3's backup API rather than copying the file. The database runs in
WAL mode and is written to continuously, so `cp` can capture a torn state that
is missing recent transactions sitting in the WAL - or is outright unusable.
The backup API takes a consistent snapshot without stopping the service.

Snapshots contain live trade history and dashboard password hashes, so they are
written 0600 and the directory is gitignored.

Takes db_path as an argument rather than importing it, so src.core.db can call
this during migration without a circular import.
"""
import os
import sqlite3
from datetime import datetime

BACKUP_PREFIX = "hermes_mt"


def prune_backups(dest_dir, retention):
    """Keeps the newest `retention` snapshots, deletes the rest."""
    if not retention or retention < 1:
        return []
    snapshots = sorted(
        (f for f in os.listdir(dest_dir)
         if f.startswith(BACKUP_PREFIX) and f.endswith(".db")),
        reverse=True,
    )
    removed = []
    for stale in snapshots[retention:]:
        try:
            os.remove(os.path.join(dest_dir, stale))
            removed.append(stale)
        except OSError as e:
            print(f"WARNING: could not remove old backup {stale}: {e}")
    return removed


def backup_database(db_path, dest_dir, retention=14, label=None):
    """Writes a consistent snapshot and prunes old ones. Returns its path."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found at {db_path}")

    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{label}" if label else ""
    dest = os.path.join(dest_dir, f"{BACKUP_PREFIX}-{stamp}{suffix}.db")

    source = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    try:
        os.chmod(dest, 0o600)  # contains password hashes
    except OSError:
        pass  # not meaningful on Windows

    prune_backups(dest_dir, retention)
    return dest
