#!/usr/bin/env python3
"""Bootstrap CLI — create the first BinBrain API key.

Usage:
    python scripts/create_api_key.py --name "bootstrap"

Reads DATABASE_URL from environment or .env file.
"""
import argparse
import hashlib
import os
import secrets
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def main():
    parser = argparse.ArgumentParser(description="Create a BinBrain API key")
    parser.add_argument("--name", required=True, help="Human label for this key")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    engine = create_engine(db_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    try:
        raw_key = "bb_" + secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

        db.execute(
            text("INSERT INTO api_keys (key_hash, name) VALUES (:key_hash, :name)"),
            {"key_hash": key_hash, "name": args.name},
        )
        db.commit()

        print(f"\nYour API key: {raw_key}")
        print("Save this — it will not be shown again.\n")
    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    main()
