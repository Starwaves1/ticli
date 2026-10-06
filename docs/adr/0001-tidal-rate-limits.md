# TIDAL's bot detection is the hard limit on request rate

An agent once made 53 sequential `playbackinfo` calls in under 3 seconds. TIDAL escalated from `429` to `401 subStatus 4006` ("Session does not have streaming privileges") and then blocked the owner's IP at the edge; the API cleared in 60–90 s, the edge block took longer, and the cost was the owner's music stopping. So nothing in ticli retries a 429 or 4006, no held-down key fans out into requests, and the `ticli agent` surface paces every call through `utils/throttle.py`, whose trip on 429/4006 stays set until a human runs `ticli agent unblock`. The same limit is why download size estimates are duration × nominal bitrate rather than one request per track.

During development the limit is stricter than the code's: build against `tests/fakes.py` and read tidalapi's installed source instead of probing; if a live request is unavoidable, at most one per 15 s, and on any 429, 401/4006 or bot-detection page stop everything and report.

## Considered options

- **Rate rules as prose only.** Rejected after an agent that hadn't read them fired ~30 unthrottled requests building a playlist; enforcement moved into code.
- **An auto-expiring trip.** Rejected: a wrong guess costs the owner's music, not one failed request.
- **Fail fast instead of block-and-wait.** Rejected: slow the over-eager caller down, don't fail it.
