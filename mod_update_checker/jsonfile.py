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
not merely lose its records, it makes the next start read nonsense. The sibling name carries
the process and thread that wrote it, so two writers of one file can never share it.

Nothing here imports MCDR, so it is directly unit-testable.
"""

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional, Union

__all__ = ["json_text", "read_json", "write_json"]


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
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(
        "{}.{}.{}.tmp".format(target.suffix, os.getpid(), threading.get_ident())
    )
    temporary.write_text(json_text(payload, sort_keys), encoding="utf-8")
    os.replace(str(temporary), str(target))


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
