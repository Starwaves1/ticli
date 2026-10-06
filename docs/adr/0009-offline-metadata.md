# Cached metadata never expires; it is held to its own 100 MB cap

To be a real offline player, everything the user has opened (library, album track lists, artist pages, recent searches) stays on disk and paints first, with its age shown, until a live fetch replaces it. The 30-day expiry goes: stale-but-labelled beats nothing when offline. Size is bounded instead by a 100 MB metadata cap, separate from the audio budget, evicting least-recently-opened lists first and the user's own library last. Built in stage 4.
