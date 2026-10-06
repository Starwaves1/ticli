"""Persistent user settings: `~/.config/ticli/config.json`, user-owned and
hand-editable (unlike machine-owned `player_state.json`), written through on
every edit.

SETTINGS_SPEC drives defaults, load-time validation and the settings page: a
new setting is one new row. Unknown keys are preserved verbatim on save, so an
older ticli never eats a newer build's settings. A `hidden` row is defaulted,
coerced, clamped and saved like the rest but is not in SETTINGS_ROWS (the page
list); volume lives on the [v] overlay.
"""

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_VERSION = 5

CONFIG_DIR = Path.home() / ".config" / "ticli"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Ascending, named like TIDAL's own player (MEDIUM is the 320k AAC rung under
# Low's dropdown). These are ticli's names, not tidalapi's wire values:
# QUALITY_MAP in player.py translates, and QUALITY_RANK plus every persisted
# `granted`/tracker tier stay in tidalapi's spelling. Do not "re-align" them.
QUALITY_CHOICES = ["LOW", "MEDIUM", "HIGH", "MAX"]

# What each tier streams, per tidalapi: low_96k / low_320k are AAC, high_lossless
# is FLAC at the 16/44.1 TIDAL assumes when a stream reports no resolution, and
# hi_res_lossless is FLAC above that (tidalapi doesn't pin its ceiling).
QUALITY_MEANINGS = {
    "LOW": "AAC ~96 kbps, lossy",
    "MEDIUM": "AAC ~320 kbps, lossy (TIDAL's Low at 320k)",
    "HIGH": "FLAC 16-bit/44.1 kHz, CD quality",
    "MAX": "FLAC above CD, up to 24-bit/192 kHz",
}

# v1 called every tier one step below what it streamed (its "LOW" was 320k, its
# "HIGH" lossless); lift v1 values to the name that keeps the same stream.
QUALITY_V1_RENAMES = {"LOW": "HIGH", "HIGH": "LOSSLESS"}

# v4 used tidalapi's wire values (LOW/HIGH/LOSSLESS/HIRES); v5 uses TIDAL's
# player names. Same streams, so values are lifted to keep the same audio. "LOW"
# is absent (96k in both). The v1 block runs first, so a v1 "LOW" (320k) becomes
# "HIGH" there and "MEDIUM" here.
QUALITY_V4_RENAMES = {"HIGH": "MEDIUM", "LOSSLESS": "HIGH", "HIRES": "MAX"}

# v2's three-way cache setting became v3's two booleans (metadata, songs).
CACHE_MODE_V2_SPLIT = {
    "OFF": (False, False),
    "METADATA": (True, False),
    "FULL": (True, True),
}

SETTINGS_SPEC: list[dict] = [
    {
        "key": "quality",
        "label": "Quality",
        "kind": "choice",
        "default": "HIGH",
        "choices": QUALITY_CHOICES,
        "value_desc": QUALITY_MEANINGS,
        "desc": "Stream quality. Applies from the next track. --quality overrides it for one run.",
    },
    {
        "key": "page_size",
        "label": "Songs per page",
        "kind": "int",
        "default": 15,
        "min": 5,
        "max": 40,
        "step": 1,
        "desc": "Most rows per page in a list. A short window shows fewer.",
    },
    {
        "key": "progress_bar_max",
        "label": "Progress bar width",
        "kind": "int",
        "default": 50,
        "min": 20,
        "max": 200,
        "step": 2,
        "desc": "Widest the progress bar gets. It always shrinks to fit the window.",
    },
    {
        "key": "volume",
        "label": "Volume",
        "kind": "int",
        "hidden": True,
        "default": 100,
        "min": 0,
        # The loudest any backend can go; the running backend's real ceiling is
        # AudioPlayer.volume_ceiling.
        "max": 250,
        "step": 5,
        "unit": "%",
        "desc": "Playback volume. Instant on mpv; ffplay takes it from the next track.",
    },
    {
        "key": "show_artwork",
        "label": "Album art",
        "kind": "bool",
        "default": True,
        "desc": "Show the album cover as pixel art. Needs a 256-colour terminal and room for it.",
    },
    {
        "key": "cache_metadata",
        "label": "Cache metadata",
        "kind": "bool",
        "default": True,
        "desc": "Keep playlists and track lists on disk. This is what makes lists open instantly.",
    },
    {
        "key": "cache_songs",
        "label": "Cache songs",
        "kind": "bool",
        "default": True,
        "desc": "Keep every track you play on disk, so playing it again never touches the network.",
    },
    {
        "key": "cache_budget_gb",
        "label": "Cache budget",
        "kind": "int",
        "default": 2,
        "min": 0,
        "max": 64,
        "step": 1,
        "unit": "GB",
        "desc": "Disk the cache may use, in GB. Over budget, the least recently used files go first.",
    },
    # Protected rows (ADR-0007): changed only by TUI keypresses, never by a command.
    {
        "key": "allow_ai_control",
        "label": "Allow AI control",
        "kind": "bool",
        "protected": True,
        "default": True,
        "desc": "Let AI agents control ticli. Off, they can only read what is on disk.",
    },
    {
        "key": "allow_dangerous_commands",
        "label": "Allow dangerous commands",
        "kind": "bool",
        "protected": True,
        "default": False,
        "desc": "Let agents delete playlists, remove tracks, delete downloads, clear the cache or log out.",
    },
    {
        "key": "ai_control_key",
        "label": "AI control key",
        "kind": "secret",
        "protected": True,
        "default": None,
        "desc": "A key agents must pass to control ticli. Enter to type one; an empty key clears it.",
    },
]

DEFAULTS = {spec["key"]: spec["default"] for spec in SETTINGS_SPEC}

SETTINGS_ROWS = [spec for spec in SETTINGS_SPEC if not spec.get("hidden")]

PROTECTED_KEYS = frozenset(spec["key"] for spec in SETTINGS_SPEC if spec.get("protected"))

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


def hash_ai_key(key: str):
    """The stored form of an AI control key; an empty key means none."""
    if not key:
        return None
    salt = os.urandom(16)
    digest = hashlib.scrypt(key.encode(), salt=salt, **_SCRYPT)
    return {"salt": salt.hex(), "hash": digest.hex()}


def ai_key_matches(stored, key) -> bool:
    if not stored or not isinstance(key, str) or not key:
        return False
    try:
        salt = bytes.fromhex(stored["salt"])
        want = bytes.fromhex(stored["hash"])
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(hashlib.scrypt(key.encode(), salt=salt, **_SCRYPT), want)


def get_spec(key: str) -> dict:
    for spec in SETTINGS_SPEC:
        if spec["key"] == key:
            return spec
    raise KeyError(key)


def coerce(spec: dict, value):
    """A usable value: invalid falls back to the default, out of range is clamped."""
    if spec["kind"] == "choice":
        if not isinstance(value, str):
            return spec["default"]
        upper = value.upper()
        return upper if upper in spec["choices"] else spec["default"]
    if spec["kind"] == "bool":
        if isinstance(value, bool):
            return value
        # A hand-edited config may say "true" / 0
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        if isinstance(value, int):
            return bool(value)
        return spec["default"]
    if spec["kind"] == "int":
        # bool is an int subclass — True/False are not meaningful sizes
        if isinstance(value, bool):
            return spec["default"]
        try:
            number = int(value)
        except (TypeError, ValueError):
            return spec["default"]
        return max(spec["min"], min(spec["max"], number))
    if spec["kind"] == "secret":
        ok = (isinstance(value, dict)
              and all(isinstance(value.get(f), str) and value.get(f) for f in ("salt", "hash")))
        return {"salt": value["salt"], "hash": value["hash"]} if ok else None
    return value


def cycle_value(spec: dict, value, step: int):
    """Value one step away: choices wrap, numbers step and stop at their bounds."""
    current = coerce(spec, value)
    if spec["kind"] == "bool":
        return not current
    if spec["kind"] == "choice":
        index = spec["choices"].index(current)
        return spec["choices"][(index + step) % len(spec["choices"])]
    if spec["kind"] == "int":
        return coerce(spec, current + step * spec.get("step", 1))
    return current


def display_value(spec: dict, value) -> str:
    """How a value reads on the settings page."""
    if spec["kind"] == "bool":
        return "On" if coerce(spec, value) else "Off"
    if spec["kind"] == "secret":
        return "Set" if coerce(spec, value) else "Not set"
    unit = spec.get("unit", "")
    return f"{value}{'' if unit == '%' else ' '}{unit}" if unit else str(value)


def _migrate(cfg: dict) -> dict:
    """Bring an older config up to CONFIG_VERSION. A migration may rename a
    value, never change what the user hears."""
    try:
        version = int(cfg.get("version", CONFIG_VERSION))
    except (TypeError, ValueError):
        version = CONFIG_VERSION
    if version < 2:
        quality = cfg.get("quality")
        if isinstance(quality, str):
            cfg["quality"] = QUALITY_V1_RENAMES.get(quality.upper(), quality)
    if version < 3:
        mode = cfg.get("cache_mode")
        if isinstance(mode, str):
            metadata, songs = CACHE_MODE_V2_SPLIT.get(
                mode.upper(), (True, True))
            cfg["cache_metadata"] = metadata
            cfg["cache_songs"] = songs
        cfg.pop("cache_mode", None)
        megabytes = cfg.pop("cache_budget_mb", None)
        if isinstance(megabytes, (int, float)) and not isinstance(megabytes, bool):
            # Round, but a budget the user set stays at least 1 GB
            gigabytes = round(megabytes / 1024)
            cfg["cache_budget_gb"] = gigabytes if gigabytes else (1 if megabytes > 0 else 0)
    if version < 4:
        columns = cfg.pop("progress_bar_width", None)
        if isinstance(columns, (int, float)) and not isinstance(columns, bool):
            cfg["progress_bar_max"] = int(columns)
    if version < 5:
        # Without this a saved "HIRES" would coerce to the default and a saved
        # "HIGH" would silently change from 320k AAC to FLAC. Only the version
        # number disambiguates the two meanings of "HIGH".
        quality = cfg.get("quality")
        if isinstance(quality, str):
            cfg["quality"] = QUALITY_V4_RENAMES.get(quality.upper(), quality)
    cfg["version"] = CONFIG_VERSION
    return cfg


def load_config() -> dict:
    """Load settings. Missing or corrupt file → defaults, never raises."""
    data = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            logger.debug("Failed to read config, using defaults: %s", e)
    if not isinstance(data, dict):
        data = {}

    # Unknown keys ride along so save_config can write them back
    cfg = _migrate(dict(data))
    for spec in SETTINGS_SPEC:
        cfg[spec["key"]] = coerce(spec, cfg.get(spec["key"], spec["default"]))
    return cfg


def save_config(cfg: dict) -> None:
    """Persist settings. Best effort — a failed write must never kill the TUI."""
    data = {k: v for k, v in cfg.items() if k not in DEFAULTS}
    data["version"] = CONFIG_VERSION
    for spec in SETTINGS_SPEC:
        data[spec["key"]] = coerce(spec, cfg.get(spec["key"], spec["default"]))
    try:
        _write_config_file(data)
    except OSError as e:
        logger.warning("Failed to save config: %s", e)


def _write_config_file(data: dict) -> None:
    """Temp + rename, never torn."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_FILE)
