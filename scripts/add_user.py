#!/usr/bin/env python
"""
Create a dashboard user, or reset an existing user's password.

    venv/bin/python scripts/add_user.py

Prompts for the password without echoing it. The alternative - pasting a
generated hash into a `sqlite3 "INSERT ..."` command - puts the plaintext
password in your shell history and makes it easy to store the wrong literal
by accident.

Passwords are hashed with werkzeug (scrypt); the plaintext is never stored,
logged, or printed.
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash  # noqa: E402

from src.core.db import get_connection, init_db, log_system_event  # noqa: E402

MIN_PASSWORD_LENGTH = 12


def prompt_password():
    """Reads a password twice, without echo, and checks they match."""
    while True:
        password = getpass.getpass("Password (not shown): ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(f"  Too short - use at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        if password != getpass.getpass("Confirm password: "):
            print("  Passwords did not match. Try again.")
            continue
        return password


def existing_user(username):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT id, username, is_active FROM users WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.close()


def main():
    init_db()  # safe on an existing database; applies any pending migrations

    username = input("Username: ").strip()
    if not username:
        print("A username is required.")
        return 1

    user = existing_user(username)
    if user:
        print(f"\nUser '{username}' already exists (id {user['id']}).")
        if input("Reset their password? [y/N]: ").strip().lower() != "y":
            print("Nothing changed.")
            return 0

    password = prompt_password()
    password_hash = generate_password_hash(password)

    conn = get_connection()
    try:
        if user:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (password_hash, username),
            )
            action = "password reset"
        else:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_active) VALUES (?, ?, 1)",
                (username, password_hash),
            )
            action = "created"
        conn.commit()
    finally:
        conn.close()

    log_system_event('system', f"Dashboard user '{username}' {action}")
    print(f"\nUser '{username}' {action}.")

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT id, username, is_active, robinhood_account_id FROM users ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    print("\nCurrent users:")
    for row in rows:
        state = "active" if row["is_active"] else "INACTIVE"
        account = row["robinhood_account_id"] or "(no Robinhood account set)"
        print(f"  {row['id']}: {row['username']:20} {state:8} {account}")

    if not user:
        print("\nNext steps for a new trading user - see README section 9:")
        print("  1. Per-user risk limits: dashboard Settings, or the user_configs table")
        print("  2. Their own Discord webhook, so tenants do not share a channel")
        print("  3. Their Robinhood agentic account number, for live order routing")
        print("     (leave unset while paper trading - it is not needed for quotes)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
