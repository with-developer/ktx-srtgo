"""Persistent settings for KTXgo, split by how sensitive each value is.

macOS prompts for the keychain password once per *item*, not once per run, so
storing every setting as its own item meant a dozen prompts on startup. This
module keeps that to at most one:

- Preferences (stations, last-used search conditions) are not secret and live
  in a plain JSON file.
- Secrets (credentials, card, telegram token) stay in the keychain but share a
  single item holding a JSON object, read once and cached for the process.

Legacy per-key items are migrated on first use. They are copied rather than
moved, so the old items remain as a fallback; nothing reads them afterwards.
"""

from __future__ import annotations

import json
from typing import Final

import keyring

from .config import DATA_DIR

PREFS_PATH: Final = DATA_DIR / "prefs.json"

SECRET_SERVICE: Final = "KTX"
SECRET_ITEM: Final = "ktxgo-secrets"

_LEGACY_SERVICE: Final = "KTX"
_LEGACY_PREF_KEYS: Final = (
    "departure",
    "arrival",
    "date",
    "time",
    "adults",
    "train_types",
    "seat",
    "auto_pay",
    "smart_ticket",
    "station",
)
_LEGACY_SECRET_KEYS: Final = (
    "id",
    "pass",
    "card_number",
    "card_password",
    "birthday",
    "card_expire",
    "waitlist_alert_phone",
)
# Telegram credentials lived under their own service name before the move.
_LEGACY_TELEGRAM_KEYS: Final = {
    "telegram_token": "token",
    "telegram_chat_id": "chat_id",
}

_prefs: dict[str, str] | None = None
_secrets: dict[str, str] | None = None


def _coerce_mapping(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():  # pyright: ignore[reportUnknownVariableType]
        if value is None:
            continue
        out[str(key)] = str(value)
    return out


def _legacy_get(service: str, key: str) -> str | None:
    try:
        return keyring.get_password(service, key)
    except Exception:
        return None


# ----------------------------------------------------------------------
# Preferences (plain file)
# ----------------------------------------------------------------------


def _read_prefs_file() -> dict[str, str]:
    try:
        return _coerce_mapping(json.loads(PREFS_PATH.read_text()))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_prefs_file(data: dict[str, str]) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        PREFS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        PREFS_PATH.chmod(0o600)
    except OSError:
        pass


def _migrate_legacy_prefs() -> dict[str, str]:
    migrated: dict[str, str] = {}
    for key in _LEGACY_PREF_KEYS:
        value = _legacy_get(_LEGACY_SERVICE, key)
        if value:
            migrated[key] = value
    return migrated


def _load_prefs() -> dict[str, str]:
    global _prefs
    if _prefs is None:
        data = _read_prefs_file()
        if not data:
            data = _migrate_legacy_prefs()
            if data:
                _write_prefs_file(data)
        _prefs = data
    return _prefs


def get_pref(key: str) -> str | None:
    return _load_prefs().get(key)


def set_pref(key: str, value: object) -> None:
    prefs = _load_prefs()
    prefs[key] = str(value)
    _write_prefs_file(prefs)


# ----------------------------------------------------------------------
# Secrets (single keychain item)
# ----------------------------------------------------------------------


def _write_secrets_item(data: dict[str, str]) -> None:
    try:
        keyring.set_password(
            SECRET_SERVICE, SECRET_ITEM, json.dumps(data, ensure_ascii=False)
        )
    except Exception:
        pass


def _migrate_legacy_secrets() -> dict[str, str]:
    migrated: dict[str, str] = {}
    for key in _LEGACY_SECRET_KEYS:
        value = _legacy_get(_LEGACY_SERVICE, key)
        if value:
            migrated[key] = value
    for new_key, legacy_key in _LEGACY_TELEGRAM_KEYS.items():
        value = _legacy_get("telegram", legacy_key)
        if value:
            migrated[new_key] = value
    return migrated


def _load_secrets() -> dict[str, str]:
    global _secrets
    if _secrets is None:
        blob = _legacy_get(SECRET_SERVICE, SECRET_ITEM)
        data: dict[str, str] = {}
        if blob:
            try:
                data = _coerce_mapping(json.loads(blob))
            except json.JSONDecodeError:
                data = {}
        if not data:
            data = _migrate_legacy_secrets()
            if data:
                _write_secrets_item(data)
        _secrets = data
    return _secrets


def get_secret(key: str) -> str | None:
    return _load_secrets().get(key)


def set_secret(key: str, value: str) -> None:
    secrets = _load_secrets()
    secrets[key] = value
    _write_secrets_item(secrets)
