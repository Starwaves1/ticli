"""Persistent user settings for Ticli.

Preferences live in `~/.config/ticli/config.json`, deliberately separate from
`player_state.json`: state is machine-owned and disposable (delete it, lose your
queue), config is user-owned and hand-editable. Written through on every edit,
so a crash never loses a setting.

A single SETTINGS_SPEC table drives defaults, load-time validation and the
settings page rendering — a future setting (artwork toggle, cache budget, ...)
is one new row here. Keys this build doesn't know about are preserved verbatim
on save, so an older ticli can never silently eat a newer build's settings.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_VERSION = 2

CONFIG_DIR = Path.home() / ".config" / "ticli"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Ascending, and named exactly like tidalapi's Quality values so the setting
# name, the stream label and what TIDAL actually sends can't drift apart.
QUALITY_CHOICES = ["LOW", "HIGH", "LOSSLESS", "HIRES"]

# What each tier actually streams. Sourced from tidalapi: Quality.low_96k /
# low_320k are AAC (media.py parses their DASH codecs as mp4a.40.5 / mp4a.40.2),
# high_lossless is FLAC at the 16-bit/44.1 kHz TIDAL assumes when a stream
# reports no resolution, and hi_res_lossless is FLAC above that — tidalapi
# doesn't pin its ceiling, so the wording stays deliberately open.
QUALITY_MEANINGS = {
    "LOW": "AAC ~96 kbps, lossy",
    "HIGH": "AAC ~320 kbps, lossy",
    "LOSSLESS": "FLAC 16-bit/44.1 kHz, CD quality",
    "HIRES": "FLAC above CD, hi-res where the master allows",
}

# v1 called every tier one step below what it actually streamed (its "LOW" asked
# for 320k, its "HIGH" for lossless). Renaming the tiers would have silently
# downgraded saved configs, so v1 values are lifted to the name that keeps the
# same stream. Applied once, on load; the next save stamps version 2.
QUALITY_V1_RENAMES = {"LOW": "HIGH", "HIGH": "LOSSLESS"}

# What each cache tier actually stores. Worth spelling out: only one of them
# can reach the gigabyte budget, and only on the ffplay backend — mpv streams
# straight from TIDAL and never writes a track to disk for us to keep.
CACHE_MODE_MEANINGS = {
    "OFF": "nothing on disk, every list waits on the network",
    "METADATA": "playlists and track lists, a few MB",
    "FULL": "metadata plus played tracks (ffplay backend)",
}

SETTINGS_SPEC: list[dict] = [
    {
        "key": "quality",
        "label": "Quality",
        "kind": "choice",
        "default": "LOSSLESS",
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
        "desc": "Rows per page in search, browse, queue and playlist lists.",
    },
    {
        "key": "progress_bar_width",
        "label": "Progress bar width",
        "kind": "int",
        "default": 50,
        "min": 20,
        "max": 120,
        "step": 2,
        "desc": "Width of the player progress bar, in characters.",
    },
    {
        "key": "volume",
        "label": "Volume",
        "kind": "int",
        "default": 100,
        "min": 0,
        "max": 100,
        "step": 5,
        "desc": "Playback volume. Instant on mpv; ffplay takes it from the next track.",
    },
    {
        "key": "cache_mode",
        "label": "Cache",
        "kind": "choice",
        "default": "METADATA",
        "choices": ["OFF", "METADATA", "FULL"],
        "value_desc": CACHE_MODE_MEANINGS,
        "desc": "What Ticli keeps on disk. Metadata is what makes playlists open instantly.",
    },
    {
        "key": "cache_budget_mb",
        "label": "Cache budget",
        "kind": "int",
        "default": 1024,
        "min": 0,
        "max": 8192,
        "step": 256,
        "desc": "Disk the cache may use, in MB. Over budget, the least recently used files go first.",
    },
]

DEFAULTS = {spec["key"]: spec["default"] for spec in SETTINGS_SPEC}


def get_spec(key: str) -> dict:
    """Look up a setting's spec row. Raises KeyError for unknown settings."""
    for spec in SETTINGS_SPEC:
        if spec["key"] == key:
            return spec
    raise KeyError(key)


def coerce(spec: dict, value):
    """Return a usable value for a setting — anything invalid falls back to
    its default, anything out of range is clamped. Callers of load_config()
    therefore never need defensive checks at the use site."""
    if spec["kind"] == "choice":
        if not isinstance(value, str):
            return spec["default"]
        upper = value.upper()
        return upper if upper in spec["choices"] else spec["default"]
    if spec["kind"] == "int":
        # bool is an int subclass — True/False are not meaningful sizes
        if isinstance(value, bool):
            return spec["default"]
        try:
            number = int(value)
        except (TypeError, ValueError):
            return spec["default"]
        return max(spec["min"], min(spec["max"], number))
    return value


def cycle_value(spec: dict, value, step: int):
    """Value one step away, for the settings page: choices wrap around,
    numbers step by spec['step'] and stop at their bounds."""
    current = coerce(spec, value)
    if spec["kind"] == "choice":
        index = spec["choices"].index(current)
        return spec["choices"][(index + step) % len(spec["choices"])]
    if spec["kind"] == "int":
        return coerce(spec, current + step * spec.get("step", 1))
    return current


def _migrate(cfg: dict) -> dict:
    """Bring an older config up to CONFIG_VERSION. Value-preserving by design:
    a migration may rename a value, never change what the user hears."""
    try:
        version = int(cfg.get("version", CONFIG_VERSION))
    except (TypeError, ValueError):
        version = CONFIG_VERSION
    if version < 2:
        quality = cfg.get("quality")
        if isinstance(quality, str):
            cfg["quality"] = QUALITY_V1_RENAMES.get(quality.upper(), quality)
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
            data = {}
    if not isinstance(data, dict):
        data = {}

    # Unknown keys ride along untouched so save_config can write them back
    cfg = dict(data)
    cfg["version"] = data.get("version", CONFIG_VERSION)
    cfg = _migrate(cfg)
    # Read back out of cfg, not data — migration may have rewritten a value
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
    """Atomically write the config file (temp + rename, never torn)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_FILE)
