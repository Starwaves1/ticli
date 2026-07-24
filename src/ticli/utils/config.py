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

CONFIG_VERSION = 1

CONFIG_DIR = Path.home() / ".config" / "ticli"
CONFIG_FILE = CONFIG_DIR / "config.json"

QUALITY_CHOICES = ["LOW", "HIGH", "LOSSLESS", "HIRES"]

SETTINGS_SPEC: list[dict] = [
    {
        "key": "quality",
        "label": "Quality",
        "kind": "choice",
        "default": "HIGH",
        "choices": QUALITY_CHOICES,
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
    for spec in SETTINGS_SPEC:
        cfg[spec["key"]] = coerce(spec, data.get(spec["key"], spec["default"]))
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
