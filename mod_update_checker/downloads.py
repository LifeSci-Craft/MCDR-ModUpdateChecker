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
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .report import STATUS_UPDATE_AVAILABLE, UpdateEntry
from .upstream import UpstreamError

__all__ = [
    "STATUS_DOWNLOADED",
    "STATUS_ALREADY_PRESENT",
    "STATUS_SKIPPED",
    "STATUS_FAILED",
    "DownloadOptions",
    "DownloadOutcome",
    "Downloader",
    "safe_jar_name",
    "resolve_folder",
]

STATUS_DOWNLOADED = "downloaded"
STATUS_ALREADY_PRESENT = "already_present"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

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
    #: Modrinth's own project, for the advisory in the log. Cosmetic.
    source_name: str = "modrinth"

    @property
    def max_megabytes(self) -> float:
        return self.max_bytes / (1024.0 * 1024.0)


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


def _sha1_of(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(_CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


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
        fallback_name: Callable[[UpdateEntry], str] = None,  # type: ignore[assignment]
    ) -> None:
        self.http = http
        self.options = options
        self.logger = logger
        self._fallback_name = fallback_name or self._default_fallback

    @staticmethod
    def _default_fallback(entry: UpdateEntry) -> str:
        stem = _safe_stem(entry.mod_id or entry.file_name, "mod")
        return "{}.jar".format(stem)

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
                skip(entry, "no-download-url")
                continue
            # CurseForge's download URLs need the API key and are not reliably direct, so this
            # stage is Modrinth-only. Said explicitly rather than silently doing nothing.
            if entry.platform and entry.platform != "modrinth":
                skip(entry, "not-modrinth")
                continue
            if not entry.download_sha1:
                skip(entry, "no-hash-to-verify")
                continue
            if entry.download_size and entry.download_size > self.options.max_bytes:
                skip(entry, "declared-too-large")
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

    def _choose_target(self, entry: UpdateEntry) -> Tuple[Optional[Path], bool, str]:
        """``(path, already_present, reason)``. ``path`` is ``None`` when nothing can be used.

        The hash decides everything, which is what makes a re-run cheap and non-destructive:
        the same expected hash always resolves to the same name, and a file already sitting
        there with that hash is left untouched.

        Two names can be in play. The plain one comes from upstream; the hash-suffixed one is
        what an earlier run used when the plain name was already taken by a *different* build.
        Both are checked for a matching hash, so a second run recognises its own earlier work
        either way round.
        """
        expected = entry.download_sha1.lower()
        name = safe_jar_name(entry.download_filename, self._fallback_name(entry))
        plain = self.options.folder / name
        suffixed = plain.with_name(
            "{}.{}{}".format(plain.stem, expected[:8], plain.suffix)
        )

        for path in (plain, suffixed):
            if path.exists() and self._read_hash(path) == expected:
                return path, True, ""

        # Neither holds the file we want. Prefer the upstream name; if that is occupied by a
        # different build, keep both rather than destroying the older one an admin may need
        # for a rollback.
        if not plain.exists():
            return plain, False, ""
        if not suffixed.exists():
            return suffixed, False, ""
        # Both names are taken by other content, and the suffixed name is derived from the
        # expected hash — so this would mean two different files sharing a name and an 8-hex
        # prefix of a hash. Refuse rather than overwrite anything.
        return None, False, "name-conflict"

    # -- writing -----------------------------------------------------------------------

    def _fetch(self, entry: UpdateEntry, target: Path) -> DownloadOutcome:
        """Download, verify and move into place. Anything less is a failure."""
        expected_sha1 = entry.download_sha1.lower()
        expected_sha512 = (entry.download_sha512 or "").lower()
        sha1 = hashlib.sha1()
        sha512 = hashlib.sha512()
        written = 0

        # Same folder as the target, so the final move is a rename and therefore atomic.
        part = target.with_name(target.name + ".part")
        try:
            with open(part, "wb") as handle:
                def sink(chunk: bytes) -> None:
                    nonlocal written
                    written += len(chunk)
                    sha1.update(chunk)
                    sha512.update(chunk)
                    handle.write(chunk)

                self.http.download(entry.download_url, sink, max_bytes=self.options.max_bytes)
        except UpstreamError as error:
            self._discard(part)
            return self._failed(entry, "upstream: {}".format(error))
        except OSError as error:
            self._discard(part)
            return self._failed(entry, "could not write to the folder: {}".format(error))

        if written <= 0:
            self._discard(part)
            return self._failed(entry, "the file was empty")

        got = sha1.hexdigest()
        if got != expected_sha1:
            self._discard(part)
            return self._failed(
                entry,
                "hash mismatch: expected {}, got {}".format(expected_sha1[:12], got[:12]),
            )
        if expected_sha512 and sha512.hexdigest() != expected_sha512:
            self._discard(part)
            return self._failed(entry, "sha512 mismatch")

        try:
            os.replace(part, target)
        except OSError as error:
            self._discard(part)
            return self._failed(entry, "could not put the file in place: {}".format(error))

        self._log(
            "debug", "downloaded {} -> {} ({} bytes)".format(entry.name, target, written)
        )
        return DownloadOutcome(
            file_name=entry.file_name,
            mod_id=entry.mod_id,
            name=entry.name,
            status=STATUS_DOWNLOADED,
            path=str(target),
            bytes_written=written,
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
            outcomes.append(self._fetch(entry, target))
        return outcomes
