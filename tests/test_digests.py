"""How a jar's identity is computed.

One pass reads the file once and produces the digest a lookup needs plus the size. That "one
pass" is the property worth testing: the obvious implementation reads the file once per
digest, and this runs over every jar in the folder.

There used to be a second digest here — a SHA-512 nothing consumed. It is gone, and the test
that would have kept it honest is gone with it: a number no caller reads cannot be verified by
asserting it equals ``hashlib``, only by asserting nobody computes it.

The digest itself is checked against ``hashlib`` applied to the whole buffer, so a streaming
bug (a dropped final block, an off-by-one in the chunk loop) cannot pass.
"""

import hashlib
from pathlib import Path

import pytest

from mod_update_checker.digests import READ_CHUNK, digests_of_file


def _digests_of(path: Path):
    with open(path, "rb") as handle:
        return digests_of_file(handle)


#: Sizes chosen around the read-chunk boundary. Labelled by size: pytest would otherwise
#: build an id from the bytes themselves and print a megabyte of "x" on failure.
_PAYLOADS = [
        b"",
        b"\x00",
        b"PK\x03\x04",
        b"a" * 10,
        b"z" * 4096,
        bytes(range(256)),
        # Around the read-chunk boundary in both directions: the point of a streaming
        # implementation is the seam between blocks, so the interesting sizes are the ones
        # that land on it.
        b"x" * (READ_CHUNK - 1),
        b"x" * READ_CHUNK,
        b"x" * (READ_CHUNK + 1),
        b"x" * (READ_CHUNK * 2),
    b"x" * (READ_CHUNK * 2 + 17),
]


@pytest.mark.parametrize("payload", _PAYLOADS, ids=lambda data: "{}B".format(len(data)))
def test_digests_match_hashlib_on_the_whole_buffer(tmp_path, payload):
    path = tmp_path / "payload.bin"
    path.write_bytes(payload)

    sha1, size = _digests_of(path)

    assert sha1 == hashlib.sha1(payload).hexdigest()
    assert size == len(payload)


def test_an_empty_file_is_a_valid_input(tmp_path):
    path = tmp_path / "empty.jar"
    path.write_bytes(b"")

    sha1, size = _digests_of(path)

    assert size == 0
    assert sha1 == hashlib.sha1(b"").hexdigest()


def test_the_read_is_bounded_by_the_chunk_size(tmp_path):
    """Memory use must not scale with the file.

    Watched on the file object itself, which is what the implementation controls: a ``read()``
    with no argument, or one asking for the whole file, would show up here as a single large
    request.
    """
    payload = b"q" * (READ_CHUNK * 3 + 7)
    path = tmp_path / "big.bin"
    path.write_bytes(payload)

    requested = []

    class Watched:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            requested.append(size)
            return self._handle.read(size)

    with open(path, "rb") as handle:
        sha1, size = digests_of_file(Watched(handle))

    assert size == len(payload)
    assert sha1 == hashlib.sha1(payload).hexdigest()
    assert requested, "nothing was read"
    assert all(0 < size <= READ_CHUNK for size in requested), requested


def test_a_modpack_scale_file_still_hashes_correctly(tmp_path):
    """40 MB is an unremarkable mod jar or pack, and must go through the streaming path."""
    payload = bytes(range(256)) * (160 * 1024)
    path = tmp_path / "large.jar"
    path.write_bytes(payload)

    sha1, size = _digests_of(path)

    assert size == len(payload)
    assert sha1 == hashlib.sha1(payload).hexdigest()


def test_the_digest_is_lowercase_hex_of_the_expected_length(tmp_path):
    """The lookup sends this as-is, so the shape matters as much as the value."""
    path = tmp_path / "shape.jar"
    path.write_bytes(b"some jar bytes")

    sha1, size = _digests_of(path)

    assert len(sha1) == 40 and sha1 == sha1.lower()
    assert size == len(b"some jar bytes")
