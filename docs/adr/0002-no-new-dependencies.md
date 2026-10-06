# No new Python dependencies; ffmpeg stays optional

Runtime dependencies are click, rich and tidalapi (keyring optional), and `requests` is used only because tidalapi already brings it. Where a library would be the obvious choice ticli carries a small stdlib implementation instead: a DC-only JPEG decoder for cover art, a direct MP4/FLAC tag writer for downloads, plain streaming GETs for caching. ffmpeg arrives only with ffplay and mpv users may not have it, so anything that uses ffmpeg degrades (no artwork, never a failure) when it's absent.

## Considered options

- **Pillow** for artwork and **mutagen** for tags: rejected under this rule; the stdlib versions proved feasible and fast.
- **requests-cache / diskcache**: rejected on its own merits too — HTTP-layer caching inside tidalapi's session would cache OAuth and signed stream-URL responses with no per-endpoint control.
- **A pyobjc helper for macOS media keys**: unnecessary, since mpv registers with MPRemoteCommandCenter itself. Linux MPRIS was skipped because it needs a dependency.
