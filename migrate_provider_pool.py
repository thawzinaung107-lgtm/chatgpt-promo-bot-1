"""Migrate legacy encrypted provider_pool.enc data into the database.

Example:
    python migrate_provider_pool.py --legacy provider_pool.enc --database sqlite:///bot.db --user-id 123456789
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from provider_pool import ProviderPoolError, ProviderPoolStore, ProviderProfile


def derived_key(bot_token: str, purpose: str) -> str:
    digest = hashlib.sha256((purpose + bot_token).encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def read_legacy_profiles(path: str | Path, legacy_key: str) -> tuple[dict, str]:
    try:
        encrypted = Path(path).read_bytes()
        raw = json.loads(Fernet(legacy_key.encode()).decrypt(encrypted).decode("utf-8"))
    except (OSError, InvalidToken, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProviderPoolError("Could not decrypt or parse the legacy provider pool") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("providers"), dict):
        raise ProviderPoolError("Legacy provider pool has an invalid structure")
    return raw["providers"], str(raw.get("active", ""))


def migrate(legacy_path: str, database_url: str, user_id: int, legacy_key: str, database_key: str) -> int:
    providers, active = read_legacy_profiles(legacy_path, legacy_key)
    store = ProviderPoolStore(database_url, database_key)
    imported = 0
    for name, raw in providers.items():
        profile_data = dict(raw)
        profile_data["name"] = name
        profile = ProviderProfile.from_dict(profile_data)
        store.upsert(user_id, profile)
        imported += 1
    if active and store.get(user_id, active) is not None:
        store.activate(user_id, active)
    return imported


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate encrypted provider_pool.enc into the database")
    parser.add_argument("--legacy", default=os.getenv("PROVIDER_POOL_PATH", "provider_pool.enc"))
    parser.add_argument("--database", default=os.getenv("DATABASE_URL", "sqlite:///bot.db"))
    parser.add_argument("--user-id", type=int, required=True, help="Telegram user ID that owns the legacy profiles")
    parser.add_argument("--legacy-key", default="", help="Legacy Fernet key; defaults to PROVIDER_STORE_KEY or the old BOT_TOKEN-derived key")
    parser.add_argument("--database-key", default="", help="Database Fernet key; defaults to API_KEY_ENCRYPTION_KEY or the new BOT_TOKEN-derived key")
    args = parser.parse_args()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    legacy_key = args.legacy_key or os.getenv("PROVIDER_STORE_KEY", "").strip() or derived_key(bot_token, "telegram-provider-pool:")
    database_key = args.database_key or os.getenv("API_KEY_ENCRYPTION_KEY", "").strip() or os.getenv("PROVIDER_STORE_KEY", "").strip() or derived_key(bot_token, "telegram-provider-database:")
    if not legacy_key or not database_key:
        raise SystemExit("Set BOT_TOKEN or provide --legacy-key and --database-key.")
    try:
        count = migrate(args.legacy, args.database, args.user_id, legacy_key, database_key)
    except ProviderPoolError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Migrated {count} provider profile(s) for Telegram user {args.user_id}.")


if __name__ == "__main__":
    main()
