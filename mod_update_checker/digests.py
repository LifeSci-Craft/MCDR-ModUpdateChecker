"""The digest Modrinth identifies a jar by.

A jar is looked up by the **SHA-1 of its exact bytes**. That is the only digest computed here,
and the narrowness is deliberate: an earlier version also produced a SHA-512 "to verify with",
and nothing ever read it. The download path verifies against the SHA-512 the *API* publishes
for the file it just fetched — a different number, computed at a different time, for a
different file — so the local one was pure work on every scan of every folder.

It was not free. Hashing 240 MiB (120 jars of 2 MiB, warm page cache) takes 0.54–0.62 s with
both digests and 0.30–0.40 s with one, over three runs of ``bench/scan_bench.py``: 45–48% of
the scan was being spent on a number nobody looked at. That script now adds the second digest
back on the same files, so the difference can be re-measured rather than taken on trust.

The size falls out of the same pass, so the file is never read twice.

The check that calls this runs on its own thread, scheduled (by default) sixty seconds after
the server finishes starting — deliberately not on the plugin-loading thread, which is where
it would stall ``!!MCDR reload plugin`` and MCDR's own startup for as long as it takes to read
the whole folder. See ``_schedule_startup_check``.

Nothing here imports MCDR or ``requests``, so it is directly unit-testable.
"""

import hashlib
from typing import IO, Tuple

__all__ = ["digests_of_file", "READ_CHUNK"]

#: Read size. Large enough that per-call overhead disappears, small enough that hashing a big
#: modpack never holds more than this much of it in memory at once.
READ_CHUNK = 1 << 20


def digests_of_file(handle: IO[bytes]) -> Tuple[str, int]:
    """Return ``(sha1_hex, size)`` for an open binary file.

    Reads from the current position to the end and leaves the handle there. Memory use is
    bounded by :data:`READ_CHUNK` however large the file is.

    ``hashlib`` releases the GIL for a buffer this size, which is why a scan is one of the few
    CPU-bound jobs here that threads can genuinely speed up.
    """
    digest = hashlib.sha1()
    size = 0

    while True:
        block = handle.read(READ_CHUNK)
        if not block:
            break
        size += len(block)
        digest.update(block)

    return digest.hexdigest(), size
