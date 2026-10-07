"""Move the builds that were fetched for a mod into ``mods/``, once the server has stopped.

This is the only part of the plugin that changes ``mods/``, so the rules are narrower than
anywhere else and each one exists to make a specific mistake impossible:

* **Only files this plugin downloaded.** The ledger is the list of work to do; nothing else in
  ``mods/`` is looked at. A jar the admin installed by hand is invisible to this module.
* **Only the records this run is meant to touch.** With ``download.install_on_stop`` on that is
  the whole ledger; with it off it is exactly the ones named by ``!!muc install``. See
  :func:`pending_records`.
* **The server is stopped.** The caller runs this from the stop event. Writing a jar into a
  running server's mods folder is how a modpack breaks itself.
* **The old jar is never deleted**, only renamed to ``<name>.old``. An update that turns out
  to be wrong is then one rename away from being undone, without a backup elsewhere.
* **Nothing is installed without a verified hash.** The download folder is a normal folder; a
  file in it could have been truncated, replaced or edited since it was fetched.
* **A name is never overwritten.** If the new jar wants a name that something else holds, the
  install is skipped and said so, rather than resolving the clash by replacing a file.
* **A failure is rolled back.** The old jar is moved aside first; if the new one cannot be put
  in place, the old one is moved back, so a failed install leaves the server as it was.

Renaming: admins annotate jars (``[锂-性能优化]Lithium.jar``). That prefix is theirs, so it is
carried onto the new file — losing it would make the folder harder to read, which is the
opposite of the point.
"""

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .digests import digests_of_file
from .downloads import DownloadLedger, safe_jar_name

__all__ = [
    "STATUS_INSTALLED",
    "STATUS_SKIPPED",
    "STATUS_FAILED",
    "InstallOptions",
    "InstallResult",
    "install_pending",
    "pending_records",
    "split_prefix",
    "backup_name",
    "target_name",
]

STATUS_INSTALLED = "installed"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

#: Why a downloaded build was not installed. Short codes rather than sentences, because the
#: renderer words them and ``!!muc install`` shows the same ones; the sentence for each lives in
#: the catalogue under ``install.reason.<code>``, and ``tests/test_i18n.py`` checks that set
#: against these constants in both directions — a code with no sentence would reach a player
#: verbatim, and a sentence with no code is a leftover.
REASON_NOT_DOWNLOADED = "not-downloaded"
REASON_NO_HASH = "no-hash-to-verify"
REASON_HASH_MISMATCH = "hash-mismatch"
REASON_NOT_IN_MODS = "not-in-mods"
REASON_NAME_TAKEN = "name-taken"
REASON_BACKUP_NAMES_TAKEN = "backup-names-taken"

#: Leading bracket groups on a file name — where an admin puts their own note. Both the ASCII
#: and the full-width bracket, because a Chinese-language admin uses either.
#:
#: Deliberately just the brackets, with no attempt to guess whether the contents "look like" a
#: note: the failure modes are both cosmetic (a missed prefix loses a label; a false positive
#: prepends one), and a rule that cannot be stated in one sentence is a rule nobody can predict.
_PREFIX = re.compile(r"^(?:\s*(?:\[[^\]]*\]|【[^】]*】))+")

#: Appended to the jar being replaced. Not deleted, renamed — an update can be wrong.
_BACKUP_SUFFIX = ".old"

#: How many numbered backups to try before giving up. ``x.jar.old``, ``x.jar.old.2``, … A name
#: is never taken from another file, so the only cost of running out is a skipped install.
_BACKUP_ATTEMPTS = 9


def split_prefix(file_name: str) -> Tuple[str, str]:
    """``(prefix, rest)`` for a jar name, where the prefix is the admin's own bracket note.

    ``"[锂-性能优化]Lithium-1.2.3.jar"`` → ``("[锂-性能优化]", "Lithium-1.2.3.jar")``.
    A name with no leading brackets comes back with an empty prefix and unchanged rest.
    """
    text = str(file_name or "")
    match = _PREFIX.match(text)
    if match is None:
        return "", text
    prefix = match.group(0)
    return prefix, text[len(prefix):]


def _core_name(file_name: str) -> str:
    """A jar name without the admin's prefix, for comparing two names."""
    return split_prefix(file_name)[1].lower()


def backup_name(path: Union[str, Path]) -> Optional[Path]:
    """The first free ``<name>.old`` (then ``.old.2`` …), or ``None`` if all are taken.

    An existing backup is never overwritten: it is the previous version of a mod, which is
    exactly the file somebody would want back. Numbering means a second update keeps both.
    """
    base = Path(path).with_name(Path(path).name + _BACKUP_SUFFIX)
    if not base.exists():
        return base
    for index in range(2, _BACKUP_ATTEMPTS + 2):
        candidate = base.with_name("{}.{}".format(base.name, index))
        if not candidate.exists():
            return candidate
    return None


def target_name(installed_name: str, upstream_name: str) -> str:
    """The name the new jar gets: the admin's prefix, then the name upstream published.

    The prefix is carried over because it is the admin's own annotation of the file. The rest
    comes from upstream, because that is where the version lives — reconstructing a name from
    the old one would mean guessing how the author spells versions.
    """
    prefix, _rest = split_prefix(installed_name)
    name = safe_jar_name(upstream_name, fallback="mod")
    if prefix and not name.lower().startswith(prefix.strip().lower()):
        name = safe_jar_name(prefix + name, fallback="mod")
    return name


def _sha1_of(path: Path) -> str:
    with open(path, "rb") as handle:
        return digests_of_file(handle)[0]


def _locate_installed(mods: Path, recorded: str) -> Optional[Path]:
    """The jar in ``mods/`` that a ledger record refers to, or ``None``.

    The recorded name first: it is what the scanner saw on disk, so it already contains
    whatever the admin renamed the file to, prefix and all. The fallback covers the admin
    renaming it *after* the check, and accepts a match only when the de-prefixed names are
    identical **and exactly one jar matches** — choosing between several similar names is how
    the wrong file gets replaced.
    """
    if not recorded:
        return None
    exact = mods / recorded
    if exact.is_file():
        return exact

    wanted = _core_name(recorded)
    matches = [path for path in mods.glob("*.jar") if _core_name(path.name) == wanted]
    return matches[0] if len(matches) == 1 else None


@dataclass
class InstallOptions:
    """Where the two folders are, and how much of the ledger to act on."""

    mods_folder: Path
    downloads_folder: Path
    #: Install only the records an admin explicitly authorised with ``!!muc install``.
    #:
    #: The switch exists because the ledger is shared: with ``download.install_on_stop`` on,
    #: every download is meant to be installed, so the whole ledger is the work list. With it
    #: off, the plugin must touch only what it was asked to touch — installing the rest would
    #: turn a per-mod instruction into a blanket one.
    approved_only: bool = False


def pending_records(ledger: DownloadLedger, approved_only: bool) -> List[str]:
    """The ledger keys this run should act on, in a stable order.

    Separated from :func:`install_pending` so the caller can ask "is there anything to do?"
    without doing it — the install runs from the stop event, and a stop with nothing to install
    must not write an install report claiming otherwise.
    """
    keys = ledger.approved_keys() if approved_only else sorted(ledger.records())
    return [key for key in keys if str((ledger.get(key) or {}).get("file") or "")]


@dataclass
class InstallResult:
    """What happened to one pending download.

    ``detail`` carries a short reason code rather than a sentence, so the caller can decide how
    to word it; anything unexpected is appended after a colon and shown as it is.
    """

    name: str
    status: str
    new_file: str = ""
    replaced_file: str = ""
    backup_file: str = ""
    version: str = ""
    detail: str = ""


def _skip(record: Dict[str, Any], reason: str) -> InstallResult:
    return InstallResult(
        name=str(record.get("name") or record.get("local") or ""),
        status=STATUS_SKIPPED,
        version=str(record.get("version") or ""),
        detail=reason,
    )


def _install_one(record: Dict[str, Any], options: InstallOptions) -> InstallResult:
    """Install one recorded download, or explain why it was not installed."""
    name = str(record.get("name") or record.get("local") or "")
    version = str(record.get("version") or "")
    source = options.downloads_folder / str(record.get("file") or "")
    local_name = str(record.get("local") or "")

    if not source.is_file():
        return _skip(record, REASON_NOT_DOWNLOADED)

    expected = str(record.get("sha1") or "").lower()
    if not expected:
        return _skip(record, REASON_NO_HASH)
    try:
        found = _sha1_of(source)
    except OSError as error:
        return InstallResult(name=name, status=STATUS_FAILED, version=version,
                             detail="unreadable: {}".format(error))
    if found != expected:
        return _skip(record, REASON_HASH_MISMATCH)

    installed = _locate_installed(options.mods_folder, local_name)
    if installed is None:
        # No jar to replace means the admin removed it, or renamed it beyond recognition.
        # Adding it back would undo a decision they made, so this is left alone.
        return _skip(record, REASON_NOT_IN_MODS)

    wanted = target_name(installed.name, source.name)
    destination = options.mods_folder / wanted
    if destination.name != installed.name and destination.exists():
        return _skip(record, REASON_NAME_TAKEN)

    backup = backup_name(installed)
    if backup is None:
        return _skip(record, REASON_BACKUP_NAMES_TAKEN)

    try:
        os.replace(installed, backup)
    except OSError as error:
        return InstallResult(name=name, status=STATUS_FAILED, version=version,
                             detail="could not set the old jar aside: {}".format(error))

    try:
        # ``shutil.move`` rather than ``os.replace``: the download folder is configurable and
        # may be on another volume, where a rename across devices fails. A copy is not atomic,
        # which is why the old jar is already aside and rolled back below on failure.
        shutil.move(str(source), str(destination))
    except (OSError, shutil.Error) as error:
        try:
            os.replace(backup, installed)
        except OSError:
            # Rollback failed too. Say so loudly: this is the one outcome the admin has to fix
            # by hand, and the backup name is how they do it.
            return InstallResult(
                name=name, status=STATUS_FAILED, version=version,
                backup_file=backup.name,
                detail="move-failed-and-rollback-failed: {}".format(error),
            )
        return InstallResult(name=name, status=STATUS_FAILED, version=version,
                             detail="could not move the new jar in: {}".format(error))

    return InstallResult(
        name=name,
        status=STATUS_INSTALLED,
        new_file=destination.name,
        replaced_file=installed.name,
        backup_file=backup.name,
        version=version,
    )


def install_pending(
    ledger: DownloadLedger, options: InstallOptions, logger: Optional[Any] = None
) -> List[InstallResult]:
    """Install the downloads this run is allowed to act on. One result per handled record.

    Records are processed in a stable order and the ledger is written after each one, so a
    crash part-way leaves the bookkeeping matching the disk rather than claiming work that was
    already done.

    Which records those are is :func:`pending_records`' decision, and it is the same list the
    caller used to decide whether to run at all — an admin who authorised one mod must not get
    the other four instalments as a side effect of the others merely being downloaded.
    """
    results: List[InstallResult] = []
    for key in pending_records(ledger, options.approved_only):
        record = ledger.get(key) or {}
        result = _install_one(record, options)
        results.append(result)
        if result.status == STATUS_INSTALLED:
            # The file now lives in mods/, and the previous version is kept beside it as
            # ``.old``. Keeping a third copy in the download folder would grow forever.
            ledger.forget(key)
            ledger.save()
        _log(logger, result)
    return results


def _log(logger: Optional[Any], result: InstallResult) -> None:
    handler = getattr(logger, "info", None) if logger else None
    if handler is None:
        return
    if result.status == STATUS_INSTALLED:
        handler(
            "installed {}: {} -> {} (old jar kept as {})".format(
                result.name, result.replaced_file, result.new_file, result.backup_file
            )
        )
    elif result.status == STATUS_SKIPPED:
        handler("not installed, {}: {} ({})".format(result.detail, result.name, result.version))
    else:
        handler("install failed, {}: {} ({})".format(result.detail, result.name, result.version))
