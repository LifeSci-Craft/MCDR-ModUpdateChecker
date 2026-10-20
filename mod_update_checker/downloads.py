"""Fetch updated builds into a folder, so an admin can collect them without a browser.

This is the only part of the plugin that writes files, and the file name comes from a remote
API — so the rules are stated here rather than left to be inferred from the code:

**Nothing ever touches the server's ``mods/`` directory.** The destination is a subfolder of
the plugin's own data folder, and the subfolder is configured as a *single name*, not a path
(see :func:`resolve_folder`). Downloading jars straight into a running server would mean
loading code that no one has looked at, which is the opposite of what this plugin is for.

**A file is only written if its hash can be verified.** Modrinth publishes a SHA-1 and a
SHA-512 for every file, so there is no reason to accept a jar we cannot check. If the hash is
missing, the entry is skipped and said to be skipped.

**An existing file is never overwritten with different content.** If the name is taken by a
different file, the new one lands under a name derived from its own hash, so the older build
stays available for a rollback. Re-running is therefore idempotent: the same expected hash
always maps to the same name, and a file already there with the right hash is left alone. The
same promise governs this mod's *own* earlier download: a superseded file is removed only once
its replacement has been verified and moved into place, so a failed download leaves the older
build exactly where it was.

**The size is bounded twice.** Once against the size Modrinth declares (a cheap pre-flight)
and again against the bytes that actually arrive, because the declared size is upstream data
and the limit exists to protect the disk.

The module does not import MCDR: like everything else here except the entry module, it is
directly unit-testable.
"""

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .jsonfile import read_json, write_json
from .report import STATUS_AWAITING_INSTALL, STATUS_UPDATE_AVAILABLE, UpdateEntry
from .upstream import NotFound, Unauthorised, UpstreamError

__all__ = [
    "STATUS_DOWNLOADED",
    "STATUS_AWAITING_INSTALL",
    "STATUS_ALREADY_PRESENT",
    "STATUS_SKIPPED",
    "STATUS_FAILED",
    "DownloadOptions",
    "DownloadOutcome",
    "Downloader",
    "DownloadLedger",
    "safe_jar_name",
    "resolve_folder",
    "is_safe_component",
    "entry_key",
    "classify_downloaded",
]

STATUS_DOWNLOADED = "downloaded"
STATUS_ALREADY_PRESENT = "already_present"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

#: Why an entry was not fetched. Short codes rather than sentences, because the same code is
#: reported from two places — the batch log line and a reply to ``!!muc download`` — and each
#: wants its own wording. The sentence for each lives in the catalogue under
#: ``download.reason.<code>``; ``tests/test_i18n.py`` checks both directions, so a code with no
#: sentence (which would reach a player verbatim) and a sentence with no code (a leftover) both
#: fail the build.
REASON_NO_URL = "no-download-url"
REASON_NO_HASH = "no-hash-to-verify"
REASON_TOO_LARGE = "declared-too-large"
REASON_NAME_CONFLICT = "name-conflict"

#: Characters that are illegal in a Windows file name, plus control characters. Kept as a
#: single class because the same filter is applied to the stem and to the configured folder
#: name, and the two must not drift apart.
_ILLEGAL = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')

#: Device names Windows refuses to create, with or without an extension. ``CON.jar`` is not a
#: writable file name on Windows, which is exactly the kind of thing a remote API could hand
#: us without meaning to.
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM{}".format(index) for index in range(1, 10)]
    + ["LPT{}".format(index) for index in range(1, 10)]
)

#: Long enough for real mod file names, short enough to survive every filesystem. The
#: extension is added on top.
_MAX_STEM = 120

_CHUNK = 65536

#: Backoff between download attempts: doubling, capped. Short on purpose — a mod jar is a
#: handful of megabytes and the whole thing is re-verifiable, so there is nothing to be gained
#: by waiting a minute; the point is only to not hammer a host that just refused us.
_RETRY_BACKOFF_SECONDS = 0.25
_RETRY_BACKOFF_CAP = 2.0

#: One lock for the whole process, not one per :class:`DownloadLedger` instance. The instances
#: are the problem rather than the solution: every caller builds its own (the check thread, the
#: stop-event install, ``!!muc download``, ``!!muc install``, the status page), and each one
#: holds its own snapshot of the file. A per-instance lock would let two of them write the same
#: target concurrently, which is the very thing being prevented. Re-entrant, because
#: :meth:`DownloadLedger.save` is also called from paths that already hold it.
_LEDGER_LOCK = threading.RLock()

#: Counter behind the per-writer ``.part`` names, guarded by :data:`_PART_LOCK`. Together with the
#: pid it replaces ``<target>.part``, which was derived from the target name alone: the check
#: thread's download phase and a manual ``!!muc download all`` are two different
#: :class:`Downloader` objects pointed at one folder, and a fixed name meant the second writer's
#: ``_discard`` could delete the first one's half-written file (Windows refuses the delete while
#: it is open, which is why the symptom was a mysterious hash mismatch rather than a clean error).
_PART_LOCK = threading.Lock()
_PART_COUNTER = 0


def _unique_part_name(target: Path) -> Path:
    """A ``.part`` path for ``target`` that no other writer in this process will pick."""
    global _PART_COUNTER
    with _PART_LOCK:
        _PART_COUNTER += 1
        serial = _PART_COUNTER
    return target.with_name(
        "{}.{}.{}.part".format(target.name, os.getpid(), serial)
    )



def _safe_stem(raw: str, fallback: str) -> str:
    """A bare, writable stem with no separators and no directory meaning.

    Order matters: strip directory components *first*, so ``../../etc/passwd`` becomes
    ``passwd`` before anything else looks at it, and only then remove the characters that are
    illegal on disk. Doing it the other way round would turn ``../`` into ``.._`` and leave a
    name that still starts with dots.
    """
    text = str(raw or "")
    # Both separators, because a name from a remote API is not guaranteed to be posix and a
    # single stray backslash on Windows is a directory traversal.
    text = text.replace("\\", "/").split("/")[-1]
    text = _ILLEGAL.sub("_", text)
    # Windows silently drops trailing dots and spaces, which turns "evil." into "evil" and
    # makes the name we wrote and the name on disk disagree.
    text = text.strip().strip(".").strip()
    # A leading dot would hide the file on unix; leading dashes are awkward on a command line.
    text = text.lstrip(".-").strip()
    text = re.sub(r"\s+", " ", text)[:_MAX_STEM].strip()
    if not text or text.upper() in _RESERVED_NAMES:
        text = fallback
    return text


def safe_jar_name(raw: str, fallback: str = "mod") -> str:
    """Turn an upstream file name into something safe to create inside a known folder.

    The fallback is used when the name is unusable — empty, only dots, or a Windows device
    name — so a caller always gets a name it can write. It is assumed to be safe already
    (the downloader builds it from the mod id).

    The extension is taken off **only when it really is a ``.jar``**, and is still filtered
    through :func:`_safe_stem` as part of the name otherwise. That distinction is the whole
    reason this is fiddly: a name that is not a jar (``mod.zip``) has to keep what it was and
    merely gain the extension, while a name that is only an extension (``.jar``) has to fall
    back rather than collapse into ``.jar``. Stripping unconditionally would turn a directory
    called ``mod.zip`` into ``mod.jar`` and lose the fact that it was not a jar at all.
    """
    text = str(raw or "")
    text = text.replace("\\", "/").split("/")[-1]
    text = _ILLEGAL.sub("_", text)
    # Windows silently drops trailing spaces and dots, so ``"x.jar "`` and ``"x.jar."`` are both
    # the same file as ``"x.jar"`` — and the suffix test below has to see the name the disk would.
    text = text.strip().rstrip(".")
    stem = text[: -len(".jar")] if text.lower().endswith(".jar") else text
    return "{}.jar".format(_safe_stem(stem, fallback))


def is_safe_component(name: str) -> Tuple[bool, str]:
    """Whether ``name`` may be used as a single file or folder name inside a managed folder.

    ``(True, "")`` or ``(False, reason)``. The reason is a short code rather than a sentence,
    because two callers report it in two different places — the download folder and the local
    project map — and each wants its own wording.

    One definition, used by both, on purpose. Two copies of "is this name a path in
    disguise?" would eventually disagree, and the disagreement would be the half that forgot
    to check for a drive letter.
    """
    text = str(name or "").strip()
    if not text:
        return False, "empty"
    normalised = text.replace("\\", "/")
    if "/" in normalised:
        return False, "contains-a-separator"
    if normalised in (".", "..") or normalised.startswith(".."):
        return False, "relative"
    if _ILLEGAL.search(normalised):
        return False, "illegal-characters"
    if normalised.upper() in _RESERVED_NAMES:
        return False, "reserved-name"
    if Path(normalised).is_absolute() or re.match(r"^[A-Za-z]:", normalised):
        return False, "absolute"
    return True, ""


def resolve_folder(base: Union[str, Path], name: str) -> Tuple[Optional[Path], str]:
    """The download folder inside ``base``. Returns ``(path, reason)``; ``None`` means no.

    ``name`` is a **single folder name, not a path**, and that is a safety property rather
    than a convenience: it makes it impossible to configure this plugin into writing outside
    its own data folder, for example into ``server/mods``. A value containing a separator, a
    drive letter or ``..`` is rejected with a reason the caller can put in the log.
    """
    ok, reason = is_safe_component(name)
    if not ok:
        return None, reason

    normalised = str(name or "").strip().replace("\\", "/")
    return Path(base).expanduser() / normalised, ""


@dataclass
class DownloadOptions:
    """Where to put files and how much of them to accept."""

    folder: Path
    max_bytes: int = 128 * 1024 * 1024
    #: *Extra* attempts after the first one fails, so the total is ``1 + retries``. Follows the
    #: same convention as ``network.retries``, so the two do not read differently.
    retries: int = 3


@dataclass
class DownloadOutcome:
    """What happened to one entry."""

    file_name: str
    mod_id: str
    name: str
    status: str
    path: str = ""
    bytes_written: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_DOWNLOADED, STATUS_ALREADY_PRESENT)


def entry_key(entry: UpdateEntry) -> str:
    """The identity a downloaded file is filed under.

    ``mod_id`` first: it is the upstream project's own identity, so it survives the jar being
    renamed in ``mods/``. A jar with no metadata has no mod id, and those do reach this code
    when they are matched by name — for them the local file name is the only identity there is.
    The ``file:`` prefix keeps the two spaces from ever colliding.
    """
    if entry.mod_id:
        return entry.mod_id
    return "file:{}".format(entry.file_name)


class DownloadLedger:
    """Which downloaded file belongs to which mod, so a stale one can be recognised and removed.

    Without this, the folder is just a pile of jars. The file name is not an identity — a new
    version almost always has a different one, which is exactly the case that matters: if
    ``sodium-0.5.0.jar`` is sitting there and 0.6.0 comes out, names alone cannot tell that the
    old file is *this* mod's earlier download rather than something the admin put there.

    Kept outside the download folder (next to the resolve cache) so that emptying the folder
    does not lose the bookkeeping, and the folder itself stays nothing but jars.

    Every method is defensive: the file is hand-editable, and a plugin must not fail a check
    over a malformed JSON it wrote itself in an earlier version.

    **Writes are targeted, not wholesale.** Several instances of this class exist at once — the
    check thread and MCDR's command thread each build their own — and each starts from its own
    snapshot of the file. Writing that snapshot back would silently erase whatever the other
    instance had written in the meantime: an admin typing ``!!muc install 3`` would be told the
    download was authorised, and the check's next save would drop the mark, so the stop-time
    install would find nothing and do nothing. So ``save`` re-reads the file, applies only the
    keys *this* instance changed, and writes the merged result. A deletion is applied because
    it was asked for, never merely because another instance's snapshot did not mention the key.
    """

    VERSION = 1

    def __init__(self, path: Optional[Union[str, Path]], logger: Optional[Any] = None) -> None:
        self.path: Optional[Path] = Path(path) if path is not None else None
        self.logger = logger
        self._records: Dict[str, Dict[str, Any]] = {}
        #: Keys this instance has changed since its last save, and how. ``None`` means "delete
        #: it"; a dict is the record to store. Only these keys are ever written back.
        self._dirty: Dict[str, Optional[Dict[str, Any]]] = {}
        self._load()

    def _load(self) -> None:
        self._records = self._read_records()

    def _read_records(self) -> Dict[str, Dict[str, Any]]:
        """The usable records currently on disk, or ``{}`` when there are none."""
        if self.path is None or not self.path.is_file():
            return {}
        data = read_json(self.path)
        if data is None:
            self._debug("download ledger unreadable, starting empty")
            return {}
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            return {}
        records = data.get("mods")
        if not isinstance(records, dict):
            return {}
        usable: Dict[str, Dict[str, Any]] = {}
        for key, record in records.items():
            if isinstance(record, dict) and _usable_record(record):
                usable[str(key)] = dict(record)
        return usable

    def _debug(self, message: str) -> None:
        handler = getattr(self.logger, "debug", None) if self.logger else None
        if handler is not None:
            handler(message)

    # -- reading -----------------------------------------------------------------------

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        return self._records.get(key)

    def file_of(self, key: str) -> str:
        record = self._records.get(key)
        return str(record.get("file", "")) if record else ""

    def records(self) -> List[str]:
        """Every key with a record. A list, not the dict, so a caller cannot mutate the
        bookkeeping by accident — forgetting a record is :meth:`forget`'s job."""
        return list(self._records)

    def prune(self, folder: Union[str, Path]) -> List[str]:
        """Drop records whose file is no longer there. Returns the keys dropped.

        A record for a file that is gone means the admin installed it (or deleted it), so the
        bookkeeping should follow rather than keep claiming a download that is not on disk.
        """
        base = Path(folder)
        dropped: List[str] = []
        for key, record in list(self._records.items()):
            if not (base / str(record.get("file", ""))).is_file():
                dropped.append(key)
                self._mark_deleted(key)
        return dropped

    # -- writing -----------------------------------------------------------------------

    def _mark_changed(self, key: str, record: Optional[Dict[str, Any]]) -> None:
        """Remember that this instance owns ``key`` now, and what it should become."""
        self._dirty[key] = dict(record) if record is not None else None

    def _mark_deleted(self, key: str) -> None:
        self._records.pop(key, None)
        self._mark_changed(key, None)

    def record(
        self,
        key: str,
        file_name: str,
        sha1: str,
        version: str,
        at: str,
        installed_file: str = "",
        name: str = "",
    ) -> None:
        """File the download under ``key``.

        ``installed_file`` is the jar this build is meant to replace, as the scanner saw it —
        which means it already carries any renaming the admin did. ``name`` is the display name,
        for the log. Both are needed by the install stage: without them it would know a file had
        been fetched but not which mod it was for, and matching on names alone is how the wrong
        jar gets replaced.

        An existing approval survives, but only while it still describes *this* file. Every
        check re-records what is on disk, and an admin who typed ``!!muc install 3`` must not
        have that approval dropped by the next unrelated check — while an approval for a build
        that has since been replaced by a different one must not carry over to bytes nobody
        agreed to install.

        A ``file_name`` that is not a bare file name is refused outright rather than stored.
        Nothing in the plugin passes one, so the only way to reach this is a caller that has
        gone wrong — and a record pointing outside the download folder is a much worse outcome
        than a refused write. :meth:`_load` applies the same rule to the file on disk.
        """
        ok, reason = is_safe_component(file_name)
        if not ok:
            self._debug(
                "refusing to record {} for {}: {}".format(file_name, key, reason)
            )
            return
        digest = (sha1 or "").lower()
        previous = self._records.get(key)
        keep_approval = bool(
            previous
            and previous.get("approved")
            and previous.get("file") == file_name
            and previous.get("sha1") == digest
        )
        record = {
            "file": file_name,
            "sha1": digest,
            "version": version or "",
            "at": at,
            "local": installed_file or "",
            "name": name or "",
        }
        if keep_approval:
            record["approved"] = True
        self._records[key] = record
        self._mark_changed(key, record)

    def approve(self, key: str) -> bool:
        """Mark this download as authorised for the next install. ``False`` if unknown.

        The authorisation is per *record*, not a global switch, because the installer runs over
        the whole ledger: an admin who approves one mod on a server that has five downloads
        waiting must get exactly the one they named, not all five.
        """
        record = self._records.get(key)
        if record is None:
            return False
        record["approved"] = True
        self._mark_changed(key, record)
        return True

    def approved_keys(self) -> List[str]:
        """Every key an admin has authorised for the next install, in a stable order."""
        return sorted(key for key, record in self._records.items() if record.get("approved"))

    def forget(self, key: str) -> None:
        self._mark_deleted(key)

    def update(self, key: str, **fields: Any) -> bool:
        """Add or replace fields on a record. ``False`` when there is no such record.

        For annotations the *install* stage makes about a record it will not act on again — a
        permanent skip reason and when it was decided. Kept here rather than as a second write
        path so the read-modify-write discipline of :meth:`save` covers it too: several
        instances are alive at once, and one that edited a record directly would lose the
        others' changes when it saved.
        """
        record = self._records.get(key)
        if record is None:
            return False
        record.update(fields)
        self._mark_changed(key, record)
        return True

    def save(self) -> None:
        """Merge this instance's changes into the file and write it back.

        Read-modify-write rather than "write what I have in memory": the other instances are
        alive at the same time and their changes are just as real. Under one process-wide lock,
        so two savers cannot interleave their read and their write.
        """
        if self.path is None:
            return
        with _LEDGER_LOCK:
            merged = self._read_records()
            for key, record in self._dirty.items():
                if record is None:
                    merged.pop(key, None)
                else:
                    merged[key] = dict(record)
            in_memory = self._records
            self._records = merged
            try:
                write_json(self.path, {"version": self.VERSION, "mods": merged}, sort_keys=True)
            except OSError as error:
                self._records = in_memory
                self._debug("could not write the download ledger: {}".format(error))
                return
            # Only now are the changes on disk; until this point they are still pending, so a
            # failed write is retried by the next save rather than silently forgotten.
            self._dirty = {}


def _sha1_of(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _safe_sha1(path: Path) -> str:
    """The SHA-1 of ``path``, or ``""`` when it cannot be read.

    One definition, because the two callers want opposite-sounding things from the same
    failure. :meth:`Downloader._read_hash` uses it to say "not the bytes we recorded", and
    :func:`classify_downloaded` to say "not the build we wanted" — both of which are correct
    answers for a file that is locked, denied or broken. What must not happen is the exception
    escaping: a jar the antivirus happens to be holding open is not a reason to abandon a check.
    """
    try:
        return _sha1_of(path)
    except OSError:
        return ""


def _safe_name_of(record: Dict[str, Any], field: str) -> str:
    """One of a record's file names, or ``""`` when it is missing or not a bare file name.

    The ledger is hand-editable, so ``file`` can say ``../../server.properties``. That value is
    used to build a path inside a managed folder, which makes it an input worth checking at the
    door rather than at each of the three places that later join it to a folder.
    """
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        return ""
    ok, _reason = is_safe_component(value)
    return value if ok else ""


def _usable_record(record: Dict[str, Any]) -> bool:
    """Whether a record from the file may be used at all.

    A record with no file name, or one that is a path in disguise, is dropped: it describes a
    file this plugin did not download, and acting on it could move a file from outside the
    download folder into ``mods/``. The remaining fields are read defensively wherever they are
    used, so nothing else here needs to reject a record.
    """
    return bool(_safe_name_of(record, "file"))


def classify_downloaded(
    entries: Sequence[UpdateEntry],
    folder: Union[str, Path],
    ledger: Optional[DownloadLedger],
) -> List[UpdateEntry]:
    """Move entries whose newer build is already on disk from update to install.

    This is what stops a mod being announced as an update on every start after it has been
    fetched. The admin has already acted; the remaining step is theirs, and it is a different
    sentence — "this is ready" rather than "this needs downloading".

    The file's contents are checked against the expected hash, not merely its presence. A jar
    that exists but is truncated, from an interrupted copy, or replaced by hand is not something
    to tell an admin to install; leaving it as ``update_available`` makes the download stage
    deal with it, which is the right outcome.

    Works whether or not downloading is currently enabled — the folder says what has been
    fetched, and that stays true after the feature is switched off. Returns the entries it
    reclassified.
    """
    base = Path(folder)
    moved: List[UpdateEntry] = []
    for entry in entries:
        if entry.status != STATUS_UPDATE_AVAILABLE:
            continue
        expected = (entry.download_sha1 or "").lower()
        if not expected:
            continue

        names: List[str] = []
        if ledger is not None:
            recorded = ledger.file_of(entry_key(entry))
            if recorded:
                names.append(recorded)
        # The name is not guaranteed to be in the ledger: a file could have been placed there
        # by an earlier version of this plugin (before the ledger existed) or by hand.
        names.append(safe_jar_name(entry.download_filename, entry.fallback_file_name()))

        for name in names:
            path = base / name
            # ``_safe_sha1`` rather than a bare call: a jar that cannot be read right now — a
            # share conflict, an ACL, an antivirus scan — means "not the build we wanted", and
            # leaving the entry as an update hands it to the download stage, which is the right
            # answer. Letting the ``OSError`` out would abort the phase that produced it, and in
            # the manual-download path would report a *failed download* for a file that is
            # already on disk and already verified.
            if path.is_file() and _safe_sha1(path) == expected:
                entry.status = STATUS_AWAITING_INSTALL
                moved.append(entry)
                break
    return moved


class Downloader:
    """Copies newer builds into ``options.folder``, one at a time.

    Sequential rather than pooled, deliberately. A check fans out over a thread pool because
    API calls are small and latency-bound; downloads are large and bandwidth-bound, and
    running twenty of them at once would be a worse citizen than letting them queue. It also
    keeps the "is this name taken?" decision single-threaded, which is what makes it correct.

    Sequential *within this instance* — the entry layer keeps more than one alive at a time (the
    check thread's download phase, a manual ``download all``), which is exactly why the temporary
    file name is unique per writer rather than derived from the target. Two instances pointed at
    one folder can still race for the same *target* name; :meth:`_choose_target` already refuses
    to overwrite a file whose bytes differ instead of trusting that they cannot.
    """

    def __init__(
        self,
        http: Any,
        options: DownloadOptions,
        logger: Optional[Any] = None,
        ledger: Optional[DownloadLedger] = None,
    ) -> None:
        self.http = http
        self.options = options
        self.logger = logger
        self.ledger = ledger

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        handler = getattr(self.logger, level, None)
        if handler is not None:
            handler(message)

    # -- selection ---------------------------------------------------------------------

    def eligible(self, entries: Sequence[UpdateEntry]) -> Tuple[List[UpdateEntry], List[DownloadOutcome]]:
        """Split the entries into those worth fetching and those that are skipped, and why.

        Skips are returned rather than dropped, because "nothing was downloaded" and "nothing
        needed downloading" look identical in a log otherwise, and the second one is what an
        admin will assume when the first is true.
        """
        wanted: List[UpdateEntry] = []
        skipped: List[DownloadOutcome] = []

        def skip(entry: UpdateEntry, reason: str) -> None:
            skipped.append(
                DownloadOutcome(
                    file_name=entry.file_name,
                    mod_id=entry.mod_id,
                    name=entry.name,
                    status=STATUS_SKIPPED,
                    detail=reason,
                )
            )

        for entry in entries:
            if entry.status != STATUS_UPDATE_AVAILABLE:
                continue
            if not entry.download_url:
                skip(entry, REASON_NO_URL)
                continue
            if not entry.download_sha1:
                skip(entry, REASON_NO_HASH)
                continue
            if entry.download_size and entry.download_size > self.options.max_bytes:
                skip(entry, REASON_TOO_LARGE)
                continue
            wanted.append(entry)
        return wanted, skipped

    # -- targets -----------------------------------------------------------------------

    def _read_hash(self, path: Path) -> str:
        digest = _safe_sha1(path)
        if not digest:
            self._log("debug", "could not hash {}".format(path))
        return digest

    def _recorded_name(self, entry: UpdateEntry) -> str:
        """The file the ledger says belongs to this mod, when it is a usable bare name.

        The ledger is hand-editable, and this value is joined to the download folder in two
        places below. ``_safe_name_of`` is the same door check the record is given on the way in
        and on the way out of the file, so a value that is a path is dropped here rather than
        being used to look somewhere else.
        """
        if self.ledger is None:
            return ""
        return _safe_name_of({"file": self.ledger.file_of(entry_key(entry))}, "file")

    def _retire_older_download(self, entry: UpdateEntry) -> Optional[Path]:
        """The older download this entry supersedes, still on disk. ``None`` when there is none.

        Nothing is removed here, and that ordering is the point. The module promises that the
        older build "stays available for a rollback", and a build that has already been deleted
        cannot be rolled back to: if the new download then fails — a 404, a dropped stream, a
        killed server — the folder is left with neither the old file nor the new one, and the
        admin is told only that a download failed. So the decision is made here and the deletion
        happens after the replacement is in place, in :meth:`_retire_now`.

        Two conditions are required before a file is ever offered up, and both matter:

        * the recorded hash must differ from the build we now want — otherwise the file on disk
          is the file we want, and re-downloading it would be wasted traffic;
        * the bytes on disk must still match what was recorded — so a file the admin replaced
          with something of their own is left alone. Deleting a file we are not certain we
          wrote would be the worst possible bug in a plugin that only otherwise creates files.
        """
        if self.ledger is None:
            return None
        key = entry_key(entry)
        record = self.ledger.get(key)
        if not record:
            return None
        name = _safe_name_of(record, "file")
        if not name:
            return None
        path = self.options.folder / name
        if not path.is_file():
            # Already gone; the ledger is just stale.
            self.ledger.forget(key)
            return None

        recorded = str(record.get("sha1", "")).lower()
        wanted = (entry.download_sha1 or "").lower()
        if recorded and recorded == wanted:
            # The file on disk is the build we want. Not ours to delete.
            return None
        if recorded and self._read_hash(path) != recorded:
            # Different content from what we wrote. Someone else's file now — leave it.
            self._log(
                "debug",
                "{} no longer matches what was recorded, leaving it alone".format(path.name),
            )
            return None
        return path

    def _retire_now(self, entry: UpdateEntry, path: Optional[Path]) -> None:
        """Remove a superseded download, now that its replacement is safely in place."""
        if path is None or self.ledger is None:
            return
        try:
            path.unlink()
        except OSError as error:
            self._log("debug", "could not remove the older download {}: {}".format(path, error))
            return

        self.ledger.forget(entry_key(entry))
        self._log(
            "info",
            "removed the older download {} (superseded by {})".format(
                path.name, entry.latest_version or "a newer build"
            ),
        )

    def _choose_target(self, entry: UpdateEntry) -> Tuple[Optional[Path], bool, str]:
        """``(path, already_present, reason)``. ``path`` is ``None`` when nothing can be used.

        The content hash decides everything, which is what makes a re-run cheap and
        non-destructive: the same expected hash always resolves to a file that is already
        there, and no network traffic is needed to work that out.

        Three names can be in play. The ledger's is the one we chose last time for this mod; the
        plain one comes from upstream; the hash-suffixed one is what an earlier run fell back to
        when the plain name was taken by a *different* build. All three are checked, so a second
        run recognises its own earlier work whichever name it ended up under.
        """
        expected = (entry.download_sha1 or "").lower()
        plain = self.options.folder / safe_jar_name(
            entry.download_filename, entry.fallback_file_name()
        )
        suffixed = plain.with_name("{}.{}{}".format(plain.stem, expected[:8], plain.suffix))

        candidates: List[Path] = []
        recorded = self._recorded_name(entry)
        if recorded:
            candidates.append(self.options.folder / recorded)
        candidates.extend([plain, suffixed])

        for path in candidates:
            if path.exists() and self._read_hash(path) == expected:
                return path, True, ""

        # Nothing on disk holds the build we want. Reuse the name we used last time if it is
        # free, so a re-download replaces our own file instead of multiplying names; then the
        # upstream name; then the hash-suffixed one rather than destroying a different build an
        # admin may need for a rollback.
        if recorded:
            path = self.options.folder / recorded
            if not path.exists():
                return path, False, ""
        if not plain.exists():
            return plain, False, ""
        if not suffixed.exists():
            return suffixed, False, ""
        # Two different files sharing a name and an 8-hex prefix of a hash. Refuse rather than
        # overwrite anything.
        return None, False, REASON_NAME_CONFLICT

    # -- writing -----------------------------------------------------------------------

    def _fetch(self, entry: UpdateEntry, target: Path) -> DownloadOutcome:
        """Fetch the file, retrying a failed attempt up to ``options.retries`` times.

        The retry lives here rather than in :meth:`HttpClient.download` because a retry has to
        restart the *verification*, not the transfer: the hash accumulator and the byte count
        are per-attempt, and the partial file has to go. Doing it at that level would mean
        asking the sink to rewind, which is why the HTTP layer deliberately does not retry.

        What is retried and what is not:

        * **Transport errors, 5xx, an empty body, a body over the limit** — retried. These are
          the transient failures a retry exists for.
        * **A hash mismatch** — also retried. Corruption in transit is the commonest cause, and
          a second attempt is the standard remedy; if the upstream is simply serving the wrong
          bytes then every attempt fails the same way and the mismatch is what gets reported,
          so the cost is a little bandwidth for a real chance of recovery.
        * **404 and 401/403** — not retried. The file is gone or we are not allowed to have it;
          repeating the request cannot change either, and hammering a 403 is how a host decides
          to block you.
        * **A write error on our own disk** — not retried. The disk will still be full.

        The last outcome is what gets returned, so the reported reason is the one the final
        attempt produced.
        """
        attempts = max(1, int(self.options.retries) + 1)
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._log(
                    "debug",
                    "retrying the download of {} (attempt {}/{})".format(
                        entry.name, attempt, attempts
                    ),
                )
            # A fresh name per attempt, so a retry can never touch a partial file another writer
            # (or the previous attempt of this one) still has open.
            outcome, retryable = self._transfer_once(entry, target, _unique_part_name(target))
            if outcome.status == STATUS_DOWNLOADED:
                return outcome
            if retryable and attempt < attempts:
                delay = min(_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)), _RETRY_BACKOFF_CAP)
                time.sleep(delay)
                continue
            if attempt > 1 and outcome.status == STATUS_FAILED:
                # Say how hard it was tried, otherwise "download failed" reads like a single
                # attempt and the admin cannot tell a flaky link from a file that is not there.
                outcome.detail = "{} (after {} attempts)".format(
                    outcome.detail or "failed", attempt
                )
            return outcome

    def _transfer_once(
        self, entry: UpdateEntry, target: Path, part: Path
    ) -> Tuple[DownloadOutcome, bool]:
        """One transfer. Returns the outcome and whether another attempt is worth making."""
        expected_sha1 = (entry.download_sha1 or "").lower()
        expected_sha512 = (entry.download_sha512 or "").lower()
        # Per-attempt state: a retry must verify what *it* fetched, not the concatenation of
        # every attempt so far.
        sha1 = hashlib.sha1()
        sha512 = hashlib.sha512()
        written = 0

        # Same folder as the target, so the final move is a rename and therefore atomic.
        self._discard(part)
        try:
            with open(part, "wb") as handle:

                def sink(chunk: bytes) -> None:
                    nonlocal written
                    written += len(chunk)
                    sha1.update(chunk)
                    sha512.update(chunk)
                    handle.write(chunk)

                self.http.download(entry.download_url, sink, max_bytes=self.options.max_bytes)
        except (NotFound, Unauthorised) as error:
            self._discard(part)
            return self._failed(entry, "upstream: {}".format(error)), False
        except UpstreamError as error:
            self._discard(part)
            return self._failed(entry, "upstream: {}".format(error)), True
        except OSError as error:
            self._discard(part)
            return self._failed(entry, "could not write to the folder: {}".format(error)), False

        if written <= 0:
            self._discard(part)
            return self._failed(entry, "the file was empty"), True

        got = sha1.hexdigest()
        if got != expected_sha1:
            self._discard(part)
            return (
                self._failed(
                    entry,
                    "hash mismatch: expected {}, got {}".format(expected_sha1[:12], got[:12]),
                ),
                True,
            )
        if expected_sha512 and sha512.hexdigest() != expected_sha512:
            self._discard(part)
            return self._failed(entry, "sha512 mismatch"), True

        try:
            os.replace(part, target)
        except OSError as error:
            # The file is downloaded and correct; only the rename failed. Retrying would fetch
            # it all over again to hit the same wall.
            self._discard(part)
            return (
                self._failed(entry, "could not put the file in place: {}".format(error)),
                False,
            )

        self._log(
            "debug", "downloaded {} -> {} ({} bytes)".format(entry.name, target, written)
        )
        return (
            DownloadOutcome(
                file_name=entry.file_name,
                mod_id=entry.mod_id,
                name=entry.name,
                status=STATUS_DOWNLOADED,
                path=str(target),
                bytes_written=written,
            ),
            False,
        )

    def _failed(self, entry: UpdateEntry, detail: str) -> DownloadOutcome:
        self._log("warning", "download of {} failed: {}".format(entry.name, detail))
        return DownloadOutcome(
            file_name=entry.file_name,
            mod_id=entry.mod_id,
            name=entry.name,
            status=STATUS_FAILED,
            detail=detail,
        )

    def _discard(self, path: Path) -> None:
        """Remove a partial file. A half-written jar is worse than none: it looks complete.

        A failure to remove it is logged rather than passed over. It cannot be made to matter
        here — the file is a ``.part`` and nothing reads one — but "the leftover is still there"
        is the fact that explains a later hash mismatch, and a silent ``pass`` is what turns that
        into a mystery.
        """
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            self._log("debug", "could not remove the partial file {}: {}".format(path, error))

    # -- entry point -------------------------------------------------------------------

    def run(self, entries: Sequence[UpdateEntry]) -> List[DownloadOutcome]:
        wanted, outcomes = self.eligible(entries)
        if not wanted:
            return outcomes

        # Created only now, so enabling the feature on a server with nothing to fetch does not
        # leave an empty folder behind.
        try:
            self.options.folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            for entry in wanted:
                outcomes.append(
                    self._failed(entry, "could not create the folder: {}".format(error))
                )
            return outcomes

        for entry in wanted:
            # Before choosing a name: work out whether this mod's older download is superseded.
            # Deciding it here keeps ``_choose_target`` looking at a folder that still holds the
            # old file, which is what lets it reuse our own name rather than invent a new one.
            superseded = self._retire_older_download(entry)
            try:
                target, present, reason = self._choose_target(entry)
                if target is None:
                    outcomes.append(
                        DownloadOutcome(
                            file_name=entry.file_name,
                            mod_id=entry.mod_id,
                            name=entry.name,
                            status=STATUS_FAILED,
                            detail=reason,
                        )
                    )
                    continue
                if present:
                    # The file on disk is already the build we want, so there is nothing left to
                    # supersede and nothing to fetch — but the record still has to be refreshed.
                    self._retire_now(entry, superseded)
                    self._remember(entry, target)
                    outcomes.append(
                        DownloadOutcome(
                            file_name=entry.file_name,
                            mod_id=entry.mod_id,
                            name=entry.name,
                            status=STATUS_ALREADY_PRESENT,
                            path=str(target),
                        )
                    )
                    continue

                outcome = self._fetch(entry, target)
                if outcome.status == STATUS_DOWNLOADED:
                    # Only now: the replacement is in place, so the rollback point can go.
                    self._retire_now(entry, superseded)
                    self._remember(entry, target)
                outcomes.append(outcome)
            finally:
                # One save attempt per mod, whatever happened — including the case where the old
                # file was dropped above merely because it had already vanished. A crash between
                # the file and its record is what leaves an unclaimed jar behind.
                if self.ledger is not None:
                    self.ledger.save()

        return outcomes

    def _remember(self, entry: UpdateEntry, path: Path) -> None:
        """File what is on disk against this mod, so a later run can find it again.

        Written after every file rather than once at the end: a crash or a killed server would
        otherwise leave jars on disk that nothing claims, and an unclaimed jar is precisely the
        one a later version would fail to recognise and clean up.
        """
        if self.ledger is None:
            return
        self.ledger.record(
            entry_key(entry),
            path.name,
            entry.download_sha1 or "",
            entry.latest_version or "",
            datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            installed_file=entry.file_name,
            name=entry.name,
        )
        self.ledger.save()
