"""Find — and optionally remove — the ``.old`` backups left behind in ``mods/``.

Installing an update never deletes the jar it replaces; it renames it to ``<name>.jar.old``
(``.old.2``, ``.old.3`` … if that name is taken). That is the rollback path, and it is worth
keeping for a while. It is not worth keeping forever: a mods folder that has been updated
monthly for a year is carrying a year of superseded jars nobody will ever rename back.

So the plugin can report them, and — only after an admin says so — remove them.

Two rules stand between that sentence and somebody's ``mods/`` folder. The first is a config
switch and lives upstream (``cleanup.allow_delete``, off by default: the plugin removes nothing
at all until an admin turns it on, and the reminder sits behind it too). The second is this
module's, and it is the one that decides *what*: **only names this plugin itself creates are
ever touched.** :func:`is_backup_name` is that rule, and every write path goes through it,
including the one that deletes: the name is checked again at the moment of removal, not only
when the list was built. Everything else in ``mods/`` — hand-installed jars, ``.disabled``
files, someone else's ``.bak`` — is invisible to this module, and a request to delete one of
those is refused rather than honoured.

Ages come from the file's modification time, which is only meaningful because the installer
:func:`~os.utime`\\s a backup the moment it creates it: a rename preserves the timestamp of the
file being renamed, so an untouched ``.old`` would carry the date the *jar* was put in the
folder — possibly years earlier — and every backup on the server would look expired on the day
this feature shipped.
"""

import os
import re
import stat
import time
from pathlib import Path
from typing import Iterable, List, NamedTuple, Optional, Sequence, Union

__all__ = [
    "BACKUP_SUFFIX",
    "Backup",
    "is_backup_name",
    "list_backups",
    "total_bytes",
    "expired",
    "remove_backups",
    "Removal",
]

#: Appended to the jar being replaced. Mirrors ``installer._BACKUP_SUFFIX``; the two are
#: asserted equal in the tests rather than imported into each other, because the installer
#: having its own copy is what makes it independent of this module's existence.
BACKUP_SUFFIX = ".old"

#: A backup of a jar: ``x.jar.old``, ``x.jar.old.2`` … and nothing else.
#:
#: The ``.jar`` is required. A file called ``notes.old`` or ``config.yml.old`` is somebody
#: else's, and "it ends in .old" is not a reason to offer it for deletion.
_JAR_BACKUP = re.compile(r"\.jar\.old(?:\.\d+)?$", re.IGNORECASE)

#: The suffix chain that makes a name a backup, used to strip it back to the jar it replaced.
_STRIP = re.compile(r"\.old(?:\.\d+)?$", re.IGNORECASE)


class Backup(NamedTuple):
    """One ``.old`` file in ``mods/``, as of the moment it was listed."""

    file_name: str
    size_bytes: int
    #: Whole days since the backup was created, floored at zero. Clamped because a file with a
    #: timestamp in the future — a clock that moved, a copy that brought its own mtime — would
    #: otherwise report a negative age and sort before everything else.
    age_days: int


class Removal(NamedTuple):
    """The outcome of one requested deletion."""

    file_name: str
    removed: bool
    #: Empty when removed; otherwise a short reason code for the caller to word.
    detail: str = ""


def is_backup_name(name: str) -> bool:
    """Whether ``name`` is one of the backups this plugin creates.

    The only predicate in the codebase allowed to authorise deleting something from ``mods/``.
    """
    return bool(_JAR_BACKUP.search(str(name or "")))


def restored_name(backup_name: str) -> str:
    """What the backup's owner was called before it became a backup.

    Not used to act on anything — it is the answer to "what is this a backup *of*", which both
    the listing and the status screen want to show.
    """
    text = str(backup_name or "")
    return _STRIP.sub("", text) if is_backup_name(text) else text


def age_in_days(modified: float, now: Optional[float] = None) -> int:
    """Whole days between ``modified`` and now, floored at zero."""
    moment = time.time() if now is None else now
    return max(0, int((moment - modified) // 86400))


def list_backups(folder: Union[str, Path], now: Optional[float] = None) -> List[Backup]:
    """Every backup in ``folder``, oldest first.

    A directory whose name happens to match, or a file that cannot be ``stat``-ed, is skipped
    rather than raising: this runs during a check, and one odd file in ``mods/`` must not be
    able to fail the report the check exists to produce.

    Sorted by age rather than by name because the reader's question is "which of these have
    been sitting here longest" — and the ordering is also what makes the numbering stable
    between two runs with no files added or removed.
    """
    root = Path(folder)
    try:
        names = list(os.listdir(str(root)))
    except OSError:
        return []

    found: List[Backup] = []
    for name in names:
        if not is_backup_name(name):
            continue
        try:
            info = (root / name).stat()
        except OSError:
            continue
        # Asked of the stat that was just taken, rather than with ``os.path.isfile``: a second
        # call is a second trip to the filesystem for an answer already in hand, and this loop
        # runs over every entry of ``mods/``. It is the same question either way — a directory
        # named ``something.jar.old`` is not a backup.
        if not stat.S_ISREG(info.st_mode):
            continue
        found.append(Backup(file_name=name, size_bytes=int(info.st_size),
                            age_days=age_in_days(info.st_mtime, now)))
    found.sort(key=lambda item: (-item.age_days, item.file_name.lower()))
    return found


def total_bytes(backups: Iterable[Backup]) -> int:
    return sum(backup.size_bytes for backup in backups)


def expired(backups: Iterable[Backup], max_age_days: int) -> List[Backup]:
    """The backups old enough to be offered for removal.

    ``0`` means "everything, however new" rather than "nothing": the switch that turns this
    feature off is ``cleanup.enabled``, and a threshold of zero days is a legitimate way to say
    "I never want to keep backups". Reading it as "off" as well would make two settings mean
    one thing, and the one that loses is the one an admin wrote down.
    """
    threshold = max(0, int(max_age_days))
    return [backup for backup in backups if backup.age_days >= threshold]


def remove_backups(
    folder: Union[str, Path], backups: Sequence[Backup]
) -> List[Removal]:
    """Delete exactly these backups. One :class:`Removal` per name asked for.

    Everything that can go wrong is reported instead of raised, because this runs from the
    stop event's thread while MCDR is shutting the server down: an exception here would take
    out the rest of the shutdown for a file that failed to delete.

    The name is re-checked here even though the caller built the list from
    :func:`list_backups`. That is not redundancy: the list is a snapshot, and between building
    it and acting on it an admin can rename something. The check that guards the deletion has
    to be the one that runs at the deletion.
    """
    root = Path(folder)
    results: List[Removal] = []
    for backup in backups:
        name = backup.file_name
        if not is_backup_name(name):
            results.append(Removal(name, False, "not-a-backup"))
            continue
        target = root / name
        if not target.exists():
            results.append(Removal(name, False, "already-gone"))
            continue
        try:
            os.remove(str(target))
        except OSError as error:
            results.append(Removal(name, False, "unlink-failed: {}".format(error)))
            continue
        results.append(Removal(name, True))
    return results
