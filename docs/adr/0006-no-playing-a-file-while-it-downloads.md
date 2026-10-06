# A track is not played from the file it is being cached into

With `cache_songs` on, a first play fetches the track twice: the player streams it while `_start_download` writes the cache copy. Playing the copy as it grows doesn't work: mpv and ffplay both stop at the write frontier (a regular file's `read()` at EOF returns 0 and neither tail-follows) and exit 0, indistinguishable from a finished track. `--cache=yes` makes it worse, sparse preallocation decodes garbage, and a FIFO can't seek. The way to fetch each song once is to prefetch the *next* track's bytes before it starts.

## Considered options

- **Switching to the local copy mid-track.** Works (mpv ~40 ms gap, ffplay ~0.37 s) but saves only 16–27% of bytes, less on worse links. Rejected.
- **`mpv --stream-record`.** mpv-only, rides on the player process, and records from the seek point. Rejected.
- **A local HTTP proxy.** A new listening socket and thread in the playback path. Rejected.
