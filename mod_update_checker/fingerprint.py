"""CurseForge's file fingerprint, and the digests Modrinth wants.

CurseForge identifies a dropped-in jar by a 32-bit MurmurHash2 of the file with four
whitespace bytes removed first. The exact recipe — seed ``1``, skip ``0x09 0x0A 0x0D
0x20``, fold to ``uint32`` — is what the official docs specify and what every independent
client implements, and it is *not* the same as a plain MurmurHash2 over the raw bytes,
which is why it needs its own tested implementation.

Modrinth, by contrast, wants a plain SHA-1 or SHA-512 over the untouched bytes.

One implementation note worth stating plainly, because it is a trap: MurmurHash2 folds the
*length of the input* into its initial state (``h = seed ^ len``). The length that matters
here is the length **after** the whitespace bytes are gone, so the hash cannot be fed
chunk-by-chunk — the total would not be known until the file ended. Every client in the
wild therefore normalises first and hashes second, and so does this one.

Nothing here imports MCDR or ``requests``, so it is directly unit-testable.
"""

import hashlib
from typing import IO, Tuple, Union

__all__ = [
    "CF_SEED",
    "CF_IGNORED_BYTES",
    "strip_cf_whitespace",
    "curseforge_fingerprint",
    "digests_of_file",
]

#: MurmurHash2 seed used by CurseForge.
CF_SEED = 1

#: Bytes CurseForge strips before hashing: tab, line feed, carriage return, space.
CF_IGNORED_BYTES = frozenset((0x09, 0x0A, 0x0D, 0x20))

_MASK32 = 0xFFFFFFFF
_MULTIPLIER = 0x5BD1E995
_SHIFT = 24

_READ_CHUNK = 1 << 20


def strip_cf_whitespace(data: bytes) -> bytes:
    """Drop the bytes CurseForge ignores, keeping everything else in order.

    Returns ``data`` itself when there was nothing to drop, which is the common case for
    already-compressed payloads and avoids a pointless full copy.
    """
    if not any(byte in CF_IGNORED_BYTES for byte in data):
        return data
    return bytes(byte for byte in data if byte not in CF_IGNORED_BYTES)


def _murmur2_cf(blob: Union[bytes, bytearray]) -> int:
    """MurmurHash2 with CurseForge's seed over an already-normalised buffer.

    Accepts a ``bytearray`` as well as ``bytes`` — indexing and slicing behave identically, so
    the caller can hand over the buffer it just built instead of making a second full-size
    copy of it.
    """
    length = len(blob)
    if length == 0:
        return 0

    multiplier = _MULTIPLIER
    h = (CF_SEED ^ length) & _MASK32

    index = 0
    remaining = length
    while remaining >= 4:
        k = blob[index] | (blob[index + 1] << 8) | (blob[index + 2] << 16) | (
            blob[index + 3] << 24
        )
        k = (k * multiplier) & _MASK32
        k ^= k >> _SHIFT
        k = (k * multiplier) & _MASK32
        h = (h * multiplier) & _MASK32
        h ^= k
        index += 4
        remaining -= 4

    # The 1..3 byte tail goes in least-significant byte first, exactly as the reference
    # implementation's fall-through switch does.
    if remaining == 3:
        h ^= blob[index + 2] << 16
    if remaining >= 2:
        h ^= blob[index + 1] << 8
    if remaining >= 1:
        h ^= blob[index]
        h = (h * multiplier) & _MASK32

    h ^= h >> 13
    h = (h * multiplier) & _MASK32
    h ^= h >> 15
    return h & _MASK32


def curseforge_fingerprint(data: bytes) -> int:
    """The CurseForge fingerprint of ``data`` as an unsigned 32-bit integer.

    An empty input hashes to ``0``, matching CurseForge.
    """
    return _murmur2_cf(strip_cf_whitespace(data))


def digests_of_file(handle: IO[bytes]) -> Tuple[str, str, int, int]:
    """Return ``(sha1_hex, sha512_hex, fingerprint, size)`` for an open binary file.

    All three digests come out of a **single pass** over the file. Each one is needed by a
    different upstream service, and a big modpack's ``mods/`` folder runs to hundreds of
    megabytes, so re-reading once per digest is not acceptable.

    The whitespace-free copy that the fingerprint needs is held in memory while the file is
    read — see the module docstring for why it cannot be avoided. It is one copy of one file,
    and it is hashed straight from that buffer rather than copied again.
    """
    sha1 = hashlib.sha1()
    sha512 = hashlib.sha512()
    normalized = bytearray()
    size = 0

    while True:
        block = handle.read(_READ_CHUNK)
        if not block:
            break
        size += len(block)
        sha1.update(block)
        sha512.update(block)
        normalized += strip_cf_whitespace(block)

    return sha1.hexdigest(), sha512.hexdigest(), _murmur2_cf(normalized), size
