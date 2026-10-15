"""How this plugin reads and writes the JSON files it keeps for itself.

One module rather than a few lines in each writer, because the shape of these files is a
decision and not a detail. ``last_report.json``, ``resolve-cache.json``, the download ledger
and the install report are all opened by hand when a server is behaving oddly — by an admin
looking at what the plugin actually recorded, by whoever they ask for help. Four dialects of
"pretty JSON" would make each of them look like a different plugin's.

The write path is also where the crash-safety property lives. Every writer here goes through
:func:`write_json`, which puts the bytes in a sibling ``.tmp`` first and then renames it over
the target: a process killed mid-write leaves either the old file or the new one, never half
of a new one. That matters most for the ones read back at startup — a truncated cache does
not merely lose its records, it makes the next start read nonsense. Writes to one target are
serialised and the rename is retried, because two concurrent renames of one destination are
what Windows actually refuses (see :func:`write_json`).

Nothing here imports MCDR, so it is directly unit-testable.
"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

__all__ = ["json_text", "read_json", "write_json", "write_text"]

#: One lock per *directory*, so two writers of different files never wait for each other while two
#: writers of one folder always do. Keyed on the folder rather than on the target file because the
#: failure is not about the destination alone: measured with the lock held only around the rename,
#: 9 of 1600 writes in 8 threads still needed a retry, and the rename is the operation Windows
#: refuses with ``WinError 5`` while another thread is creating its temporary file in the same
#: directory. Serialising the whole write — create, write, rename — is what removes the race.
#:
#: Measured before any of this existed (8 threads x 200 writes of one file): 0 failures while
#: writing the temporary file, 652 failures in ``os.replace``.
_WRITE_LOCKS: Dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()

#: How many times a rename is retried, and how long to wait before each retry. Kept as a safety
#: net for interference this module cannot see — a virus scanner or an indexer holding the target
#: open — because with the directory serialised a retry should never be needed at all; the
#: concurrency test asserts exactly that.
_REPLACE_ATTEMPTS = 3
_REPLACE_BACKOFF = 0.05


def _write_lock_for(target: Path) -> threading.Lock:
    """The lock that serialises writes into ``target``'s directory.

    Keyed by the normalised absolute folder so that two spellings of one path (``a/../x.json``
    and ``x.json``) share a lock rather than taking different ones and racing anyway.
    """
    folder = target.parent
    try:
        key = os.path.normcase(str(folder.resolve()))
    except OSError:  # pragma: no cover - resolve() can fail on a path that does not exist yet
        key = os.path.normcase(os.path.abspath(str(folder)))
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


def _replace_with_retry(temporary: Path, target: Path) -> None:
    """``os.replace(temporary, target)``, retrying a failed rename a few times.

    Raises the last ``OSError`` when every attempt fails. The caller removes its own temporary
    file in that case, so a permanently unwritable target does not litter the directory.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(str(temporary), str(target))
            return
        except OSError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF * (attempt + 1))


def json_text(payload: Any, sort_keys: bool = False) -> str:
    """The plugin's JSON, as text: indented, with non-ASCII left as it is.

    ``ensure_ascii=False`` is the one that matters to a reader. These files carry mod names and
    Chinese messages, and escaping them turns a file somebody is meant to read into a wall of
    ``\\u4e2d`` — which is exactly the file they opened because something was wrong.

    ``sort_keys`` is for the download ledger, which is a manifest: its keys are mod ids, they
    arrive in whatever order the checks ran, and an unsorted file shows a diff on every save
    even when nothing changed.
    """
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=sort_keys)


def write_json(path: Union[str, Path], payload: Any, sort_keys: bool = False) -> None:
    """Write ``payload`` to ``path`` so a reader sees either the old file or the new one.

    Raises ``OSError`` on a file that cannot be written, like any other write: the callers
    differ in whether that is worth a warning (the cache) or worth nothing at all (a flag that
    only prevents a repeated message), and swallowing it here would take that choice away.

    The temporary name is **unique per call**, not derived from the target alone. Two writers of
    the same file used to share ``<name>.tmp``: one of them could rename it away while the other
    was still writing it, and the loser's ``os.replace`` then either raised
    ``FileNotFoundError`` or — worse — put the *other* writer's bytes in place. The clash is not
    hypothetical here: the download ledger is written from MCDR's command thread and from the
    check thread at the same time, which is exactly the case ``DownloadLedger`` is careful about.
    ``tempfile.mkstemp`` supplies the name rather than ``pid`` + ``threading.get_ident()``,
    because a thread ident is recycled once a thread exits (measured: 4000 sequential threads
    produced 1931 distinct idents), so it is not the uniqueness it looks like.

    Uniqueness of the name is necessary but **not sufficient**: what actually fails is two
    concurrent ``os.replace`` calls aimed at one destination. Windows refuses the second one with
    ``WinError 5`` rather than letting it win, so the loser's save is silently lost — the cache
    entry or the approval state it was writing never lands. Writes to one target are therefore
    serialised on a per-path lock held across the write *and* the rename, and the rename itself is
    retried a few times with a short backoff. Callers keep seeing one of two states on disk; they
    just no longer contend for it.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload_text = json_text(payload, sort_keys)
    with _write_lock_for(target):
        handle, temporary_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload_text)
            _replace_with_retry(temporary, target)
        except BaseException:
            # Either the write or every rename attempt failed: take our own temporary file away
            # before the error reaches the caller, so a failing target cannot fill the folder
            # with orphans. The old target file, if any, is untouched.
            try:
                temporary.unlink()
            except OSError:
                pass
            raise


def write_text(path: Union[str, Path], text: str) -> None:
    """Write ``text`` to ``path`` with the same old-file-or-new-file guarantee as JSON.

    For the one non-JSON file the plugin keeps: the human-readable copy of the last report, next
    to the JSON one. It would have been simpler to leave that one on a plain ``open(..., "w")``,
    but then the two files written side by side would have different crash behaviour, and the
    reader has no way to tell which of them they are looking at.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock_for(target):
        handle, temporary_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.name + ".", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
            _replace_with_retry(temporary, target)
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise


def read_json(path: Union[str, Path]) -> Optional[Any]:
    """Parse the JSON file at ``path``, or ``None`` when there is nothing usable there.

    ``None`` covers missing, unreadable and malformed alike. Every caller starts from "there
    may not be a file yet" — a cache on a fresh install, a ledger on a server that has never
    downloaded anything — and for all of them "no file" and "a file I cannot parse" call for
    the same thing: carry on with an empty one. A caller that needs to tell those apart (the
    config, which quarantines a file it cannot read) checks for itself first.

    What the parsed value should *be* is also the caller's question: a list where a dict was
    expected is answered by an ``isinstance`` check at the call site, not here, because only
    the caller knows which shape it needs.

    ``utf-8-sig`` rather than plain ``utf-8``: a hand-edited file saved from a Windows editor
    often carries a byte-order mark, and ``json`` rejects one — ``Unexpected UTF-8 BOM``. The
    codec strips it when it is there and behaves exactly like ``utf-8`` when it is not, so the
    plugin writes its own files plain and reads both spellings. Non-UTF-8 encodings (GBK, ANSI)
    still fail, which is why callers must treat ``None`` as "unusable" rather than "empty".
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None
