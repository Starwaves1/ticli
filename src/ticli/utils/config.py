"""Persistent user settings for Ticli.

Preferences live in `~/.config/ticli/config.json`, deliberately separate from
`player_state.json`: state is machine-owned and disposable (delete it, lose your
queue), config is user-owned and hand-editable. Written through on every edit,
so a crash never loses a setting.

A single SETTINGS_SPEC table drives defaults, load-time validation and the
settings page rendering — a future setting (artwork toggle, cache budget, ...)
is one new row here. Keys this build doesn't know about are preserved verbatim
on save, so an older ticli can never silently eat a newer build's settings.

A `hidden` row is in the table but not on the page (SETTINGS_ROWS is what the
page lists): it is defaulted, coerced, clamped and written exactly like the
rest, and only the surface it is *edited* on is elsewhere. Volume is the one —
it lives on the [v] overlay, because it is the setting you reach for in the
middle of a track.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_VERSION = 5

CONFIG_DIR = Path.home() / ".config" / "ticli"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Ascending, and named the way TIDAL's own player names its tiers — Low /
# High / Max, plus the 320k AAC rung TIDAL files under Low's bitrate dropdown,
# surfaced here as MEDIUM. These are ticli's names, *not* tidalapi's wire
# values: QUALITY_MAP in player.py is the single translation between the two,
# and QUALITY_RANK plus every persisted `granted`/tracker tier stay in
# tidalapi's spelling. Do not "re-align" these names to tidalapi — three of
# the four differ on purpose, and the difference is load-bearing.
QUALITY_CHOICES = ["LOW", "MEDIUM", "HIGH", "MAX"]

# What each tier actually streams. Sourced from tidalapi: Quality.low_96k /
# low_320k are AAC (media.py parses their DASH codecs as mp4a.40.5 / mp4a.40.2),
# high_lossless is FLAC at the 16-bit/44.1 kHz TIDAL assumes when a stream
# reports no resolution, and hi_res_lossless is FLAC above that — tidalapi
# doesn't pin its ceiling, so the wording stays deliberately open.
QUALITY_MEANINGS = {
    "LOW": "AAC ~96 kbps, lossy",
    "MEDIUM": "AAC ~320 kbps, lossy (TIDAL's Low at 320k)",
    "HIGH": "FLAC 16-bit/44.1 kHz, CD quality",
    "MAX": "FLAC above CD, up to 24-bit/192 kHz",
}

# v1 called every tier one step below what it actually streamed (its "LOW" asked
# for 320k, its "HIGH" for lossless). Renaming the tiers would have silently
# downgraded saved configs, so v1 values are lifted to the name that keeps the
# same stream. Applied once, on load; the next save stamps version 2.
QUALITY_V1_RENAMES = {"LOW": "HIGH", "HIGH": "LOSSLESS"}

# v4 named the tiers after tidalapi's wire values (LOW/HIGH/LOSSLESS/HIRES);
# v5 renames them to match TIDAL's own player. Same streams, new names, so
# every saved value is lifted to the name that keeps the same audio and the
# rename is inaudible. "LOW" is absent because old LOW and new LOW are both
# 96k. The v1 block above runs first, so a v1 config chains: its "LOW" (which
# streamed 320k) becomes "HIGH" there and "MEDIUM" here — still 320k.
QUALITY_V4_RENAMES = {"HIGH": "MEDIUM", "LOSSLESS": "HIGH", "HIRES": "MAX"}

# v2 kept one three-way cache setting; v3 splits it into the two independent
# things it was always describing — an index of your lists, and the tracks
# themselves. FULL meant both, METADATA meant lists only, OFF meant neither.
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
        # Wider than any bar was allowed to be before, because it is a ceiling
        # now rather than a size: on a 200-column terminal a 120-cap bar leaves
        # half the pane empty, and nothing here can overflow a narrow one.
        "max": 200,
        "step": 2,
        "desc": "Widest the progress bar gets. It always shrinks to fit the window.",
    },
    {
        "key": "volume",
        "label": "Volume",
        "kind": "int",
        # A setting like any other — defaulted, coerced, clamped and saved
        # through this same table — but not a row on the settings page. It is
        # the one setting you reach for in the middle of a track, so it is
        # edited on the [v] overlay instead, over whatever is on screen.
        "hidden": True,
        "default": 100,
        "min": 0,
        # The loudest any supported backend can go, not what the running one
        # can: the real ceiling is discovered from the backend at runtime (see
        # AudioPlayer.volume_ceiling) and clamped against on load and on edit,
        # so a config written next to mpv is safe next to ffplay.
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
]

DEFAULTS = {spec["key"]: spec["default"] for spec in SETTINGS_SPEC}

# What the settings page actually lists, in order. Everything else about a
# hidden setting is unchanged: only where it is edited moved.
SETTINGS_ROWS = [spec for spec in SETTINGS_SPEC if not spec.get("hidden")]


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
    if spec["kind"] == "bool":
        if isinstance(value, bool):
            return value
        # A hand-edited config may say "true" / 0; anything else isn't an answer
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
    return value


def cycle_value(spec: dict, value, step: int):
    """Value one step away, for the settings page: choices wrap around,
    numbers step by spec['step'] and stop at their bounds."""
    current = coerce(spec, value)
    if spec["kind"] == "bool":
        # Two values: either direction is the other one
        return not current
    if spec["kind"] == "choice":
        index = spec["choices"].index(current)
        return spec["choices"][(index + step) % len(spec["choices"])]
    if spec["kind"] == "int":
        return coerce(spec, current + step * spec.get("step", 1))
    return current


def display_value(spec: dict, value) -> str:
    """How a value reads on the settings page: booleans as words, sizes with
    the unit the user is actually typing in."""
    if spec["kind"] == "bool":
        return "On" if coerce(spec, value) else "Off"
    unit = spec.get("unit", "")
    return f"{value}{'' if unit == '%' else ' '}{unit}" if unit else str(value)


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
    if version < 3:
        # One cache choice became two booleans, and MB became GB. Both are
        # written back into cfg — load_config reads the migrated dict, not the
        # raw file, so what is set here is what the user ends up with.
        mode = cfg.get("cache_mode")
        if isinstance(mode, str):
            metadata, songs = CACHE_MODE_V2_SPLIT.get(
                mode.upper(), (True, True))
            cfg["cache_metadata"] = metadata
            cfg["cache_songs"] = songs
        cfg.pop("cache_mode", None)
        megabytes = cfg.pop("cache_budget_mb", None)
        if isinstance(megabytes, (int, float)) and not isinstance(megabytes, bool):
            # Round rather than truncate: a 1.5 GB budget becomes 2 GB, and any
            # budget the user actually set stays at least 1 GB rather than 0
            gigabytes = round(megabytes / 1024)
            cfg["cache_budget_gb"] = gigabytes if gigabytes else (1 if megabytes > 0 else 0)
    if version < 4:
        # The bar was laid out at exactly this many columns and wrapped when
        # the window was narrower; it is derived from the width now and this
        # is only its ceiling. Same number, so a wide window looks unchanged —
        # what the user set is what the bar still grows to.
        columns = cfg.pop("progress_bar_width", None)
        if isinstance(columns, (int, float)) and not isinstance(columns, bool):
            cfg["progress_bar_max"] = int(columns)
    if version < 5:
        # The tidalapi-style tier names give way to TIDAL's own. Value-
        # preserving like v1: without this, a saved "HIRES" would coerce to
        # the default (a silent downgrade) and a saved "HIGH" would keep its
        # spelling but change its meaning from 320k AAC to FLAC — a silent
        # 4x data increase. The version number is what disambiguates the two
        # meanings of "HIGH"; nothing else can.
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
            data = {}
    if not isinstance(data, dict):
        data = {}

    # Unknown keys ride along untouched so save_config can write them back
    cfg = dict(data)
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
