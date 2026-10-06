"""Credential storage: the OS keychain via `keyring`, else a chmod-600 JSON file.

The record also says which login flow issued the tokens (`is_pkce`): tidalapi
refreshes PKCE and device-flow tokens against different TIDAL client ids, so a
record that loses the flag dies hours later looking like a random logout.
Records written before the flag are device-flow, so the migration is a
defaulted read and nobody has to log in again.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from ticli.utils import testhooks

logger = logging.getLogger(__name__)

SERVICE_NAME = "ticli"

# 1 (implicit): device-flow tokens, no is_pkce field. 2: is_pkce always present.
TOKEN_VERSION = 2
FALLBACK_DIR = Path.home() / ".config" / SERVICE_NAME
FALLBACK_FILE = FALLBACK_DIR / "session.json"

keyring = None
if not testhooks.enabled():
    try:
        import keyring
        # Verify the backend isn't the fail-open "null" backend
        _backend = keyring.get_keyring()
        _backend_name = type(_backend).__name__
        if "fail" in _backend_name.lower() or "null" in _backend_name.lower():
            keyring = None
            logger.debug("keyring backend is %s — falling back to file", _backend_name)
    except Exception:
        keyring = None


def _ensure_fallback_dir() -> None:
    FALLBACK_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)


def _migrate(data: dict) -> dict:
    """Bring a stored record up to TOKEN_VERSION without invalidating a working session."""
    record = dict(data)
    record["is_pkce"] = bool(record.get("is_pkce", False))
    record["version"] = TOKEN_VERSION
    return record


def save_tokens(data: dict) -> None:
    """Persist OAuth tokens securely."""
    payload = json.dumps(_migrate(data))

    if keyring is not None:
        try:
            keyring.set_password(SERVICE_NAME, "oauth_session", payload)
            _delete_fallback_file()
            return
        except Exception as e:
            logger.warning("keyring.set_password failed, falling back to file: %s", e)

    _ensure_fallback_dir()
    FALLBACK_FILE.write_text(payload)
    os.chmod(FALLBACK_FILE, 0o600)


def load_tokens() -> Optional[dict]:
    """Load stored OAuth tokens, migrated (`is_pkce` is always a bool), or None."""
    if keyring is not None:
        try:
            raw = keyring.get_password(SERVICE_NAME, "oauth_session")
            if raw:
                return _load_record(raw)
        except Exception as e:
            logger.debug("keyring.get_password failed: %s", e)

    if FALLBACK_FILE.exists():
        try:
            return _load_record(FALLBACK_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Failed to read fallback token file: %s", e)

    return None


def _load_record(raw: str) -> Optional[dict]:
    """Parse one stored payload; anything but a JSON object with an access token
    counts as nothing stored."""
    data = json.loads(raw)
    if not isinstance(data, dict) or not data.get("access_token"):
        return None
    return _migrate(data)


def delete_tokens() -> None:
    """Remove stored OAuth tokens from all backends."""
    if keyring is not None:
        try:
            keyring.delete_password(SERVICE_NAME, "oauth_session")
        except Exception:
            pass
    _delete_fallback_file()


def _delete_fallback_file() -> None:
    try:
        if FALLBACK_FILE.exists():
            # Overwrite before unlinking for slightly better security
            FALLBACK_FILE.write_bytes(b"\x00" * len(FALLBACK_FILE.read_bytes()))
            FALLBACK_FILE.unlink()
    except OSError:
        pass
