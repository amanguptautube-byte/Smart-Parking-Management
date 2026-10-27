"""Create an operator account (run once per operator).

Usage:
    python create_operator.py                 # prompts for username/password
    python create_operator.py demo_operator   # prompts for password only
"""
import getpass
import sqlite3
import sys

from werkzeug.security import generate_password_hash

import database as db


def main():
    db.init_db()
    username = sys.argv[1] if len(sys.argv) > 1 else input("Username: ").strip()
    if len(username) < 3:
        sys.exit("Username must be at least 3 characters.")
    password = getpass.getpass("Password (min 8 chars): ")
    if len(password) < 8:
        sys.exit("Password must be at least 8 characters.")
    if password != getpass.getpass("Confirm password: "):
        sys.exit("Passwords do not match.")

    conn = db.get_conn()
    try:
        db.create_operator(conn, username, generate_password_hash(password))
        print(f"Operator '{username}' created. You can now log in.")
    except sqlite3.IntegrityError:
        sys.exit(f"Operator '{username}' already exists.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
