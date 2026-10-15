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
  in place, the old one is moved back, so a failed install leaves the server as it was. The new
  jar is copied to a sibling of its destination and then renamed over it, so ``mods/`` never
  contains a half-written jar under a real mod name — not even for an instant, and not even when
  the download folder is on another volume.

Renaming: admins annotate jars (``[锂-性能优化]Lithium.jar``). That prefix is theirs, so it is
carried onto the new file — losing it would make the folder harder to read, which is the
opposite of the point.
"""

import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .digests import digests_of_file
from .downloads import DownloadLedger, is_safe_component, safe_jar_name

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

#: Reasons a later run would reach again and again with the same answer — an admin who deleted
#: the old jar, a name something else holds, a file whose bytes no longer match the hash that was
#: verified. Kept on the record as ``failed_at``/``failed_reason`` so :func:`pending_records`
#: stops offering them and the log stops repeating one line per stop cycle, while the ledger still
#: shows what was attempted and why. The rest (a missing download, a failed copy) can genuinely
#: change between runs and stay pending.
_PERMANENT_REASONS = frozenset(
    (REASON_NOT_IN_MODS, REASON_NAME_TAKEN, REASON_BACKUP_NAMES_TAKEN, REASON_HASH_MISMATCH)
)

#: One install at a time, per process. Two installs racing over one ``mods/`` folder is what the
#: backup-name reservation below exists to survive, but serialising them is the cheaper guarantee:
#: the ``.old`` name is chosen and taken in one step, and no second installer can slip between the
#: two halves of that step.
_INSTALL_LOCK = threading.RLock()


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

    **The name is taken, not merely chosen.** The loop creates each candidate with
    ``open(..., "x")`` and returns the one that succeeds, so another installer — or an admin
    renaming a file by hand in the same moment — cannot be handed the same name. Bare
    ``exists()`` probing followed by a later ``os.replace`` was a TOCTOU window, and the loser of
    that race overwrote a backup that the docstring promised was never overwritten. The
    placeholder file is what the caller renames the old jar onto.
    """
    base = Path(path).with_name(Path(path).name + _BACKUP_SUFFIX)
    for candidate in [base] + [
        base.with_name("{}.{}".format(base.name, index))
        for index in range(2, _BACKUP_ATTEMPTS + 2)
    ]:
        try:
            # ``os.O_CREAT | os.O_EXCL``: atomic claim, and a no-op rather than a window when two
            # callers reach the same name at once. ``open()`` rather than ``Path.touch`` because
            # only the exclusive flag makes the claim itself the test.
            descriptor = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        except OSError:
            # A folder we cannot create in, or a name the filesystem refuses: not a name to hand
            # out. The next candidate may still work, so this is a ``continue`` like the clash.
            continue
        os.close(descriptor)
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

    A name that is not a bare file name is refused before it is joined to ``mods/``. The
    fallback below would only ever look at ``*.jar`` inside the folder anyway, but ``mods /
    "../../x.jar"`` is a path, not a name, and this function must not be the place where such a
    value is taken on trust.
    """
    if not recorded:
        return None
    ok, _reason = is_safe_component(recorded)
    if not ok:
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

    A record closed out by a reason that cannot improve (``failed_reason``; see
    :data:`_PERMANENT_REASONS`) is not a pending record. Without this, a mod whose old jar the
    admin deleted was re-attempted and re-logged on every single stop, which is also why the
    log could never be used to tell "waiting" from "given up". Editing ``failed_reason`` out of
    the ledger — or deleting the record — is how an admin asks for another try.
    """
    keys = ledger.approved_keys() if approved_only else sorted(ledger.records())
    return [
        key
        for key in keys
        if str((ledger.get(key) or {}).get("file") or "")
        and not (ledger.get(key) or {}).get("failed_reason")
    ]


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


def _copy_into_place(source: Path, destination: Path) -> Tuple[bool, str]:
    """Put ``source`` at ``destination`` without ever leaving a half-written file there.

    The copy goes to a sibling of the destination first and is then renamed over it, so
    ``destination`` is either the old file or the complete new one at every instant. That the
    two are siblings is the whole point: a same-directory rename is atomic, while the
    ``shutil.move`` this replaced fell back to a copy when the download folder was on another
    volume — and a copy interrupted half-way (a full disk, a killed server, an antivirus
    quarantine) left a truncated jar *under the new mod's own name*, which the next server start
    would load. The old jar had already been moved aside by then, so the admin was left with a
    broken mod and a repair nobody had been told about.

    Returns ``(ok, detail)``. ``detail`` is empty on success, and otherwise explains the failure
    together with the name of the temporary file when one may still be on disk.
    """
    temporary = destination.with_name(destination.name + ".new")
    try:
        shutil.copy2(str(source), str(temporary))
    except (OSError, shutil.Error) as error:
        residue = _remove_temporary(temporary)
        detail = "could not copy the new jar in: {}".format(error)
        if residue:
            detail = "{} (a partial file may remain: {})".format(detail, temporary.name)
        return False, detail
    try:
        os.replace(str(temporary), str(destination))
    except OSError as error:
        residue = _remove_temporary(temporary)
        detail = "could not put the new jar in place: {}".format(error)
        if residue:
            detail = "{} (a partial file may remain: {})".format(detail, temporary.name)
        return False, detail
    return True, ""


def _remove_temporary(temporary: Path) -> bool:
    """Delete a copy-in-progress. ``True`` when a file is still there afterwards."""
    try:
        temporary.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


def _release_reserved_name(backup: Path) -> None:
    """Give back the placeholder :func:`backup_name` created, when the rename onto it failed.

    Without this an aborted install leaves a zero-byte ``x.jar.old`` behind, which the next run
    would treat as a real backup (and the cleanup feature would eventually delete as if it were
    one). Missing is not a problem: the rename may have succeeded before the call that failed.
    """
    try:
        backup.unlink()
    except OSError:
        pass


def _install_one(record: Dict[str, Any], options: InstallOptions) -> InstallResult:
    """Install one recorded download, or explain why it was not installed."""
    with _INSTALL_LOCK:
        return _install_one_locked(record, options)


def _install_one_locked(record: Dict[str, Any], options: InstallOptions) -> InstallResult:
    """The install itself. Callers hold :data:`_INSTALL_LOCK`, so the backup decision is final."""
    name = str(record.get("name") or record.get("local") or "")
    version = str(record.get("version") or "")

    # The ledger is hand-editable, and both names below become paths inside a folder this plugin
    # manages. A name that is a path in disguise is skipped rather than joined: otherwise a
    # record saying ``../server.properties`` would move a file from *outside* the download
    # folder into ``mods/``, and rename the mod it was supposedly replacing out of the way while
    # doing it. The download stage only ever writes bare names, so nothing legitimate is lost.
    recorded_file = str(record.get("file") or "")
    file_ok, file_reason = is_safe_component(recorded_file)
    if not file_ok:
        return _skip(record, "unsafe-name: {}".format(file_reason or "file"))
    local_name = str(record.get("local") or "")
    local_ok, local_reason = is_safe_component(local_name)
    if not local_ok:
        return _skip(record, "unsafe-name: {}".format(local_reason or "local"))

    source = options.downloads_folder / recorded_file

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
        _release_reserved_name(backup)
        return InstallResult(name=name, status=STATUS_FAILED, version=version,
                             detail="could not set the old jar aside: {}".format(error))

    # Stamp the backup with the moment it became a backup. A rename keeps the timestamp of the
    # file being renamed, so without this ``sodium.jar.old`` would carry the date ``sodium.jar``
    # was put in the folder — possibly years earlier — and the cleanup feature, which ages
    # backups by exactly this timestamp, would read every backup on the server as long expired
    # on the first run after upgrading. The file appeared under *this* name now; that is what
    # its timestamp should say. A failure here is not worth aborting an install over, and the
    # worst case is a backup that reads as older than it is.
    try:
        os.utime(str(backup), None)
    except OSError:
        pass

    ok, detail = _copy_into_place(source, destination)
    if not ok:
        try:
            os.replace(backup, installed)
        except OSError:
            # Rollback failed too. Say so loudly: this is the one outcome the admin has to fix
            # by hand, and the backup name is how they do it.
            return InstallResult(
                name=name, status=STATUS_FAILED, version=version,
                backup_file=backup.name,
                detail="move-failed-and-rollback-failed: {}".format(detail),
            )
        return InstallResult(name=name, status=STATUS_FAILED, version=version,
                             detail=detail)

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

    Two things happen to a record that is done with. On success the **consumed download is
    deleted**: the build now lives in ``mods/`` and the previous version beside it as ``.old``,
    so a third copy in the download folder is one nothing will ever read again, and leaving it
    there made the folder grow by one jar per update forever — the note in the code said as much
    while ``forget`` only dropped the bookkeeping. On a reason that cannot improve (see
    :data:`_PERMANENT_REASONS`) the record is *closed* rather than retried, so
    ``download.install_on_stop`` does not re-attempt it and re-log it on every stop.
    """
    results: List[InstallResult] = []
    installed = False
    for key in pending_records(ledger, options.approved_only):
        record = ledger.get(key) or {}
        result = _install_one(record, options)
        results.append(result)
        if result.status == STATUS_INSTALLED:
            installed = True
            _remove_consumed_download(ledger, options, record, logger)
            ledger.forget(key)
            ledger.save()
        elif result.status == STATUS_SKIPPED and result.detail in _PERMANENT_REASONS:
            ledger.update(
                key,
                failed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                failed_reason=result.detail,
            )
            ledger.save()
        _log(logger, result)
    if installed:
        # Once per run rather than once per jar, and only when something was actually installed:
        # a stop with nothing to do must not go walking the folder.
        _sweep_consumed_downloads(ledger, options, logger)
    return results


def _remove_consumed_download(
    ledger: DownloadLedger,
    options: InstallOptions,
    record: Dict[str, Any],
    logger: Optional[Any],
) -> None:
    """Delete the download an install just consumed. A failure is a debug line, not an error."""
    name = str(record.get("file") or "")
    if not name:
        return
    ok, _reason = is_safe_component(name)
    if not ok:
        return
    path = options.downloads_folder / name
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        # Windows refuses while another handle is open, which is exactly the case for a jar the
        # server is reading. The directory sweep below gets it on a later run.
        _debug(logger, "could not remove the installed download {}: {}".format(path, error))


def _sweep_consumed_downloads(
    ledger: DownloadLedger, options: InstallOptions, logger: Optional[Any] = None
) -> List[str]:
    """Delete leftover downloads this plugin provably wrote, once nothing claims them.

    The other half of the same promise. A crash between the install and the ledger save, or a
    ``forget`` whose file was locked on Windows, leaves a jar in the download folder with no
    record — and a file with no record is invisible to every other code path here, so it stays
    forever. Two conditions are required before one is deleted, and both matter:

    * it must be a bare ``.jar``/``.part`` name inside the folder (never a path, and never
      something the admin keeps there by hand);
    * its SHA-1 must be one the ledger recorded for a download, so the file is *self-proving* to
      have come from this plugin. A jar whose bytes this plugin has no record of is left alone:
      deleting a file that might be the admin's own is the one mistake this module must not make.

    Returns the names removed, for the tests and the log.
    """
    folder = options.downloads_folder
    if not folder.is_dir():
        return []
    tracked = set()
    known: Dict[str, str] = {}
    for key in ledger.records():
        item = ledger.get(key) or {}
        name = str(item.get("file") or "")
        if not name:
            continue
        ok, _reason = is_safe_component(name)
        if ok:
            tracked.add(name)
        digest = str(item.get("sha1") or "").lower()
        if digest:
            known[digest] = name

    if not known:
        return []

    removed: List[str] = []
    try:
        candidates = sorted(folder.iterdir())
    except OSError as error:
        _debug(logger, "could not list the download folder {}: {}".format(folder, error))
        return []
    for path in candidates:
        if not path.is_file() or path.name in tracked:
            continue
        if path.suffix.lower() not in (".jar", ".part"):
            continue
        try:
            digest = _sha1_of(path)
        except OSError:
            continue
        if digest not in known:
            continue
        try:
            path.unlink()
        except OSError as error:
            _debug(logger, "could not remove the leftover download {}: {}".format(path, error))
            continue
        removed.append(path.name)
    if removed:
        _debug(
            logger,
            "removed {} leftover download(s) this plugin had already installed: {}".format(
                len(removed), ", ".join(removed)
            ),
        )
    return removed


def _debug(logger: Optional[Any], message: str) -> None:
    handler = getattr(logger, "debug", None) if logger else None
    if handler is not None:
        handler(message)


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
