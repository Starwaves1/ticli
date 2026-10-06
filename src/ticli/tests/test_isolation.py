"""The suite-wide redirect in conftest.py, checked at the level that matters:
with every per-test patch undone, nothing points into the owner's real files."""

from pathlib import Path

from ticli import player as player_mod
from ticli.utils import cache as cache_mod
from ticli.utils import config as config_mod
from ticli.utils import credential_store as credential_store_mod
from ticli.utils import downloads as downloads_mod
from ticli.utils import throttle as throttle_mod


def _real_roots():
    home = Path.home()
    return [
        (home / "Library" / "Caches" / "ticli").resolve(),
        (home / ".config" / "ticli").resolve(),
        (home / "Music" / "Ticli").resolve(),
        cache_mod._default_cache_dir().resolve(),
    ]


def _inside(path, root):
    path = Path(path).resolve()
    return path == root or root in path.parents


def test_no_path_constant_points_into_the_real_home(monkeypatch):
    # Undoing the per-test fixtures leaves exactly what a thread that outlives
    # its test would see: the session-wide redirect.
    monkeypatch.undo()
    paths = {
        "cache.CACHE_DIR": cache_mod.CACHE_DIR,
        "downloads.DOWNLOAD_ROOT": downloads_mod.DOWNLOAD_ROOT,
        "downloads.download_dir()": downloads_mod.download_dir(),
        "player.STATE_DIR": player_mod.STATE_DIR,
        "player.STATE_FILE": player_mod.STATE_FILE,
        "throttle.STATE_DIR": throttle_mod.STATE_DIR,
        "config.CONFIG_DIR": config_mod.CONFIG_DIR,
        "config.CONFIG_FILE": config_mod.CONFIG_FILE,
        "credential_store.FALLBACK_DIR": credential_store_mod.FALLBACK_DIR,
        "credential_store.FALLBACK_FILE": credential_store_mod.FALLBACK_FILE,
    }
    for name, path in paths.items():
        assert path is not None, name
        for root in _real_roots():
            assert not _inside(path, root), f"{name} = {path} is inside real {root}"


def test_keychain_is_off_for_the_whole_session(monkeypatch):
    monkeypatch.undo()
    assert credential_store_mod.keyring is None


def test_per_test_state_is_also_isolated():
    assert credential_store_mod.keyring is None
    for root in _real_roots():
        assert not _inside(cache_mod.CACHE_DIR, root)
        assert not _inside(config_mod.CONFIG_DIR, root)
