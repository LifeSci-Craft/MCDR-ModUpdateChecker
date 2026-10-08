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
always maps to the same name, and a file already there with the right hash is left alone.

**The size is bounded twice.** Once against the size Modrinth declares (a cheap pre-flight)
and again against the bytes that actually arrive, because the declared size is upstream data
and the limit exists to protect the disk.

The module does not import MCDR: like everything else here except the entry module, it is
directly unit-testable.
"""

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

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
    """
    text = str(raw or "")
    text = text.replace("\\", "/").split("/")[-1]
    text = _ILLEGAL.sub("_", text)

    # Split the extension off before filtering, so "my mod .jar" does not lose its ".jar" to
    # the trailing-space strip, and so a name that is only an extension does not survive.
    stem, dot, extension = text.rpartition(".")
    if dot and extension.strip().lower() == "jar":
        stem = stem
    else:
        # Not a .jar name at all: keep whatever it was as the stem and impose the extension.
        stem = text
    stem = _safe_stem(stem, fallback)
    return "{}.jar".format(stem)


def resolve_folder(base: Union[str, Path], name: str) -> Tuple[Optional[Path], str]:
    """The download folder inside ``base``. Returns ``(path, reason)``; ``None`` means no.

    ``name`` is a **single folder name, not a path**, and that is a safety property rather
    than a convenience: it makes it impossible to configure this plugin into writing outside
    its own data folder, for example into ``server/mods``. A value containing a separator, a
    drive letter or ``..`` is rejected with a reason the caller can put in the log.
    """
    text = str(name or "").strip()
    if not text:
        return None, "empty"
    normalised = text.replace("\\", "/")
    if "/" in normalised:
        return None, "contains-a-separator"
    if normalised in (".", "..") or normalised.startswith(".."):
        return None, "relative"
    if _ILLEGAL.search(normalised):
        return None, "illegal-characters"
    if normalised.upper() in _RESERVED_NAMES:
        return None, "reserved-name"
    if Path(normalised).is_absolute() or re.match(r"^[A-Za-z]:", normalised):
        return None, "absolute"

    folder = Path(base).expanduser() / normalised
    # Defence in depth. The checks above already make escape impossible, but this is the
    # assertion that keeps that true if the checks are ever edited: whatever we are about to
    # write to must be directly inside ``base``.
    try:
        base_resolved = Path(base).expanduser().resolve()
        parent_resolved = folder.resolve().parent
    except OSError:
        return None, "unresolvable"
    if parent_resolved != base_resolved:
        return None, "escapes-base"

    return folder, ""


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
    """

    VERSION = 1

    def __init__(self, path: Optional[Union[str, Path]], logger: Optional[Any] = None) -> None:
        self.path: Optional[Path] = Path(path) if path is not None else None
        self.logger = logger
        self._records: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._debug("download ledger unreadable, starting empty")
            return
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            return
        records = data.get("mods")
        if not isinstance(records, dict):
            return
        for key, record in records.items():
            if isinstance(record, dict) and isinstance(record.get("file"), str):
                self._records[str(key)] = dict(record)

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
                self._records.pop(key, None)
        return dropped

    # -- writing -----------------------------------------------------------------------

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
        """
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
        return True

    def approved_keys(self) -> List[str]:
        """Every key an admin has authorised for the next install, in a stable order."""
        return sorted(key for key, record in self._records.items() if record.get("approved"))

    def forget(self, key: str) -> None:
        self._records.pop(key, None)

    def save(self) -> None:
        if self.path is None:
            return
        payload = {"version": self.VERSION, "mods": self._records}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as error:
            self._debug("could not write the download ledger: {}".format(error))


def _sha1_of(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


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
            if path.is_file() and _sha1_of(path) == expected:
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
        try:
            return _sha1_of(path)
        except OSError as error:
            self._log("debug", "could not hash {}: {}".format(path, error))
            return ""

    def _retire_older_download(self, entry: UpdateEntry) -> str:
        """Delete this mod's previously downloaded file, if it is now for an older build.

        Returns the name that was removed, or ``""``. This is what stops the folder filling up
        with every version a mod has ever been through: the ledger knows the old file belongs
        to *this* mod, so it can be removed deliberately rather than left to accumulate.

        Two conditions are required before deleting anything, and both matter:

        * the recorded hash must differ from the build we now want — otherwise the file on disk
          is the file we want, and re-downloading it would be wasted traffic;
        * the bytes on disk must still match what was recorded — so a file the admin replaced
          with something of their own is left alone. Deleting a file we are not certain we
          wrote would be the worst possible bug in a plugin that only otherwise creates files.
        """
        if self.ledger is None:
            return ""
        key = entry_key(entry)
        record = self.ledger.get(key)
        if not record:
            return ""
        name = str(record.get("file", ""))
        if not name:
            return ""
        path = self.options.folder / name
        if not path.is_file():
            # Already gone; the ledger is just stale.
            self.ledger.forget(key)
            return ""

        recorded = str(record.get("sha1", "")).lower()
        wanted = (entry.download_sha1 or "").lower()
        if recorded and recorded == wanted:
            # The file on disk is the build we want. Not ours to delete.
            return ""
        if recorded and self._read_hash(path) != recorded:
            # Different content from what we wrote. Someone else's file now — leave it.
            self._log(
                "debug",
                "{} no longer matches what was recorded, leaving it alone".format(path.name),
            )
            return ""

        try:
            path.unlink()
        except OSError as error:
            self._log("debug", "could not remove the older download {}: {}".format(path, error))
            return ""

        self.ledger.forget(key)
        self._log(
            "info",
            "removed the older download {} (superseded by {})".format(
                name, entry.latest_version or "a newer build"
            ),
        )
        return name

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
        if self.ledger is not None:
            recorded = self.ledger.file_of(entry_key(entry))
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
        if self.ledger is not None:
            recorded = self.ledger.file_of(entry_key(entry))
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
        part = target.with_name(target.name + ".part")
        outcome = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                self._log(
                    "debug",
                    "retrying the download of {} (attempt {}/{})".format(
                        entry.name, attempt, attempts
                    ),
                )
            outcome, retryable = self._transfer_once(entry, target, part)
            if outcome.status == STATUS_DOWNLOADED:
                return outcome
            if not retryable or attempt == attempts:
                break
            delay = min(_RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1)), _RETRY_BACKOFF_CAP)
            time.sleep(delay)

        if attempts > 1 and outcome is not None and outcome.status == STATUS_FAILED:
            # Say how hard it was tried, otherwise "download failed" reads like a single attempt
            # and the admin has no way to tell a flaky link from a file that is not there.
            outcome.detail = "{} (after {} attempts)".format(outcome.detail or "failed", attempts)
        return outcome  # type: ignore[return-value]

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

    @staticmethod
    def _discard(path: Path) -> None:
        """Remove a partial file. A half-written jar is worse than none: it looks complete."""
        try:
            path.unlink()
        except OSError:
            pass

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
            # Before choosing a name: drop this mod's older download, if the ledger says the
            # file sitting there is one of ours for a version that has now been superseded.
            self._retire_older_download(entry)

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
                self._remember(entry, target)
            outcomes.append(outcome)

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
