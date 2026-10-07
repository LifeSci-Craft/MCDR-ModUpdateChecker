"""The digests Modrinth identifies a jar by.

A jar is looked up by the **SHA-1 of its exact bytes**, and verified against the **SHA-512** the
API publishes alongside it. The size falls out of the same pass.

All of them are computed in **one pass** over the file. A modpack's ``mods/`` folder runs to
hundreds of megabytes, and re-reading it once per digest would be a self-inflicted stall on
every check — and this runs while MCDR is loading plugins, so it would stall server start too.

Nothing here imports MCDR or ``requests``, so it is directly unit-testable.
"""

import hashlib
from typing import IO, Tuple

__all__ = ["digests_of_file", "READ_CHUNK"]

#: Read size. Large enough that per-call overhead disappears, small enough that hashing a big
#: modpack never holds more than this much of it in memory at once.
READ_CHUNK = 1 << 20


def digests_of_file(handle: IO[bytes]) -> Tuple[str, str, int]:
    """Return ``(sha1_hex, sha512_hex, size)`` for an open binary file.

    Reads from the current position to the end and leaves the handle there. Memory use is
    bounded by :data:`READ_CHUNK` however large the file is.
    """
    sha1 = hashlib.sha1()
    sha512 = hashlib.sha512()
    size = 0

    while True:
        block = handle.read(READ_CHUNK)
        if not block:
            break
        size += len(block)
        sha1.update(block)
        sha512.update(block)

    return sha1.hexdigest(), sha512.hexdigest(), size
