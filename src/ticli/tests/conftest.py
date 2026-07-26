"""Suite-wide safety rails.

One rule, applied to every test in the package rather than per module: the
download folder defaults to `~/Music/Ticli`, which is the owner's own music.
Nothing in the suite may write there, and a test that forgets to redirect it
must not be the thing that finds out. `DOWNLOAD_ROOT` is read at call time by
`downloads.download_dir()`, so pointing it at tmp_path here is enough — no
test needs to know.
"""

import pytest

from ticli.utils import downloads as downloads_mod


@pytest.fixture(autouse=True)
def never_the_real_music_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(downloads_mod, "DOWNLOAD_ROOT", tmp_path / "Music" / "Ticli")
