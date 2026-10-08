"""The result of a check run, and how it is rendered.

A check produces one :class:`UpdateEntry` per jar, each carrying a *status* that says what
the admin should conclude — not merely "different versions exist", which is useless:

``up_to_date``          the local jar is the newest build for this loader and game version
``update_available``    a newer build exists and should be installed
``local_ahead``         the local jar is newer than anything published (a development
                        build, or the author removed a release)
``no_compatible_build`` the project exists but publishes nothing for this loader/game
                        version, which is the pre-upgrade situation admins hit most often
``unresolved``          the jar could not be tied to any upstream project
``error``               the lookup for this one mod failed
``not_a_mod``           a valid jar that carries no mod metadata
``ignored``             the admin listed it in ``ignored_mods``

The entry also keeps *why* it was identified (``hash`` vs ``name``): a name-based match is a
guess and the report says so, because acting on a wrong guess costs more than acting on
nothing.

Rendering is separated from the data and takes a ``tr`` callable, so every string still
goes through the plugin's translation catalogue.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .serverinfo import ServerContext

__all__ = [
    "STATUS_UP_TO_DATE",
    "STATUS_UPDATE_AVAILABLE",
    "STATUS_AWAITING_INSTALL",
    "STATUS_LOCAL_AHEAD",
    "STATUS_NO_COMPATIBLE_BUILD",
    "STATUS_UNRESOLVED",
    "STATUS_ERROR",
    "STATUS_NOT_A_MOD",
    "STATUS_IGNORED",
    "ALL_STATUSES",
    "ACTIONABLE_STATUSES",
    "LINKED_STATUSES",
    "UPDATE_ENTRY_ORDER",
    "REPORT_FORMAT",
    "MATCHED_BY_HASH",
    "MATCHED_BY_NAME",
    "MATCHED_BY_MANUAL",
    "MATCHED_BY_NOTEWORTHY",
    "UpdateEntry",
    "Report",
    "render_summary",
    "render_full",
    "render_entry_lines",
    "render_index_row",
    "render_index",
    "render_index_body",
    "render_tally",
    "render_detail",
    "entry_detail_rows",
    "index_selection",
    "CHAT_PAGE_LINES",
    "entry_from_scan",
    "mc_mismatch_note",
]

STATUS_UP_TO_DATE = "up_to_date"
STATUS_UPDATE_AVAILABLE = "update_available"
#: The newer build has been fetched into the plugin's download folder and is waiting to be
#: copied into ``mods/``. Distinct from ``update_available`` on purpose: there is nothing left
#: to download, and telling the admin about an update they have already fetched is the noise
#: this status exists to remove.
STATUS_AWAITING_INSTALL = "awaiting_install"
STATUS_LOCAL_AHEAD = "local_ahead"
STATUS_NO_COMPATIBLE_BUILD = "no_compatible_build"
STATUS_UNRESOLVED = "unresolved"
STATUS_ERROR = "error"
STATUS_NOT_A_MOD = "not_a_mod"
STATUS_IGNORED = "ignored"

ALL_STATUSES: Tuple[str, ...] = (
    STATUS_UPDATE_AVAILABLE,
    STATUS_AWAITING_INSTALL,
    STATUS_NO_COMPATIBLE_BUILD,
    STATUS_LOCAL_AHEAD,
    STATUS_UNRESOLVED,
    STATUS_ERROR,
    STATUS_NOT_A_MOD,
    STATUS_UP_TO_DATE,
    STATUS_IGNORED,
)

#: Statuses that warrant doing something. Everything else is information.
#:
#: ``awaiting_install`` belongs here: there is nothing left to download, but the work is not
#: finished — somebody still has to move the file. Leaving it out would mean the "ready to
#: install" section is suppressed by ``report.updates_only``, which is the one setting most
#: servers run with.
ACTIONABLE_STATUSES: Tuple[str, ...] = (
    STATUS_UPDATE_AVAILABLE,
    STATUS_AWAITING_INSTALL,
    STATUS_NO_COMPATIBLE_BUILD,
)

#: Display order for a full listing: most urgent first, then the noise, then the good news.
UPDATE_ENTRY_ORDER: Dict[str, int] = {status: index for index, status in enumerate(ALL_STATUSES)}

#: Statuses whose next step is on the project page, and only those. An update to fetch, or a
#: project with nothing published for this server — both end with somebody opening a web page.
#:
#: Deliberately excludes ``awaiting_install``: the build is already on disk, so a link adds
#: nothing to the line, and leaving it out is what makes the two groups look different at a
#: glance — one has links because there is still something to go and get.
LINKED_STATUSES: Tuple[str, ...] = (
    STATUS_UPDATE_AVAILABLE,
    STATUS_NO_COMPATIBLE_BUILD,
)

#: Version of the JSON written to ``last_report.json``.
#:
#: Bumped when a change would make an older file mean something different rather than merely
#: be missing a field, because that file is read back after a restart and a mismatch has to
#: mean "run the check again" instead of "interpret it optimistically".
REPORT_FORMAT = 1

#: How an entry was tied to a project: by the bytes of the file, by its name, or by the
#: admin's own mapping file. Only the middle one is a guess, and a line that said "matched by
#: hash" on every row would bury it.
MATCHED_BY_HASH = "hash"
MATCHED_BY_NAME = "name"
#: Tied to a project by ``project-map.json`` — the admin wrote down which project this jar is,
#: so nothing was guessed. Worth showing anyway: it is how the admin confirms their own file
#: was read, and a mapping that quietly stopped applying would otherwise look like a project
#: that had vanished from Modrinth.
MATCHED_BY_MANUAL = "manual"

#: The ``matched_by`` values a reader needs to be told about. ``hash`` is the default and the
#: honest one, so it stays silent.
MATCHED_BY_NOTEWORTHY = (MATCHED_BY_NAME, MATCHED_BY_MANUAL)

#: Blank columns inserted between the description and the project link.
_LINK_GAP = "  "

#: How many lines a chat reply may occupy before the reader has to scroll.
#:
#: Vanilla's chat box shows ten lines at a time and can be scrolled, but a reply whose useful
#: row is two screens down is a reply that gets closed. Every chat-facing listing is budgeted
#: to this, and what does not fit is one command away rather than lost.
CHAT_PAGE_LINES = 18

#: One row of a mod's detail view: ``(label, value, url)``.
#:
#: ``label`` is empty for the heading row, and ``url`` is empty for anything that is not a
#: link. The three parts are separate so the chat renderer can lay out a labelled field and
#: turn the value into a clickable link, while the console renderer just joins them.
DetailRow = Tuple[str, str, str]

Translator = Callable[..., str]


# --------------------------------------------------------------------------------------
# Reading a stored report back
#
# ``last_report.json`` is written every check and read again after a restart, so the "reuse
# yesterday's answer instead of asking again" window is not thrown away by a server restart.
# These coercions are the whole reason that is safe: the file is on disk across versions of
# the plugin, and a field this version does not understand has to degrade to something
# usable rather than raise on plugin load.
# --------------------------------------------------------------------------------------


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _keyed_args(value: Any) -> List[Tuple[str, Dict[str, Any]]]:
    """``[{"key": ..., "args": {...}}]`` back into the tuple form the renderers want.

    Used for an entry's notes and for the report's advisories and upstream notes, which are
    all the same shape: a translation key plus the arguments its placeholders take. One reader
    for all three, because a shape that is written in one place and read in three is exactly
    where a divergence would go unnoticed.
    """
    if not isinstance(value, list):
        return []
    out: List[Tuple[str, Dict[str, Any]]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        key = _text(item.get("key"))
        if not key:
            continue
        args = item.get("args")
        out.append((key, args if isinstance(args, dict) else {}))
    return out


@dataclass
class UpdateEntry:
    """One jar's outcome."""

    mod_id: str
    name: str
    file_name: str
    local_version: str = ""
    latest_version: str = ""
    status: str = STATUS_UNRESOLVED
    platform: str = ""
    matched_by: str = ""
    project_url: str = ""
    download_url: str = ""
    #: Identity of the file behind ``download_url``, as the platform published it. Carried
    #: through the report rather than re-fetched later, because the auto-download feature has
    #: to verify what it fetched against a hash decided by the same lookup that chose the file.
    download_filename: str = ""
    download_sha1: str = ""
    download_sha512: str = ""
    download_size: int = 0
    released_at: str = ""
    release_channel: str = ""
    #: ``(translation key, format args)`` pairs, rendered only when the report is printed.
    notes: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    error: str = ""

    @property
    def actionable(self) -> bool:
        return self.status in ACTIONABLE_STATUSES

    def fallback_file_name(self) -> str:
        """The name this mod's download is filed under when upstream offers nothing usable.

        Defined here rather than in the download module because two places need the same answer:
        the name to write under, and the name to look for when asking whether the build is
        already on disk. Two independent guesses at it would eventually disagree, and the
        symptom would be re-downloading a file that is already sitting there.

        The mod id is used before the local file name: it is the stabler identity, and it is
        already free of spaces, which are the one thing worth avoiding in a file name here.
        """
        raw = self.mod_id or self.file_name
        stem = "".join(character if character.isalnum() or character in "-_." else "-"
                       for character in raw).strip("-.") or "mod"
        # A local file name already ends in ``.jar``; appending another would give "mod.jar.jar".
        if stem.lower().endswith(".jar"):
            stem = stem[:-4]
        return "{}.jar".format(stem[:100] or "mod")

    @property
    def sort_key(self) -> Tuple[int, str]:
        return (UPDATE_ENTRY_ORDER.get(self.status, 99), self.name.lower())

    def add_note(self, key: str, **args: Any) -> "UpdateEntry":
        self.notes.append((key, args))
        return self

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["notes"] = [
            {"key": key, "args": args} for key, args in self.notes
        ]
        return data

    @classmethod
    def from_dict(cls, data: Any) -> Optional["UpdateEntry"]:
        """Rebuild one entry from a stored report, or ``None`` if it is unusable.

        Only used to read back this plugin's own ``last_report.json`` after a restart. It has
        to be forgiving because that file survives upgrades: it was written by an older
        version, or by a newer one that has since been rolled back, and a shape this version
        does not recognise must mean "skip this entry" rather than an exception on load.

        A file name is the one field required, because it is the entry's identity — everything
        else has a sensible default. An unrecognised ``status`` becomes ``unresolved`` rather
        than being printed raw: there is no way to state a status this version does not know,
        and "we cannot say" is the honest one.
        """
        if not isinstance(data, dict):
            return None
        file_name = _text(data.get("file_name"))
        if not file_name:
            return None

        status = _text(data.get("status"))
        entry = cls(
            mod_id=_text(data.get("mod_id")),
            name=_text(data.get("name")) or file_name,
            file_name=file_name,
            local_version=_text(data.get("local_version")),
            latest_version=_text(data.get("latest_version")),
            status=status if status in ALL_STATUSES else STATUS_UNRESOLVED,
            platform=_text(data.get("platform")),
            matched_by=_text(data.get("matched_by")),
            project_url=_text(data.get("project_url")),
            download_url=_text(data.get("download_url")),
            download_filename=_text(data.get("download_filename")),
            download_sha1=_text(data.get("download_sha1")),
            download_sha512=_text(data.get("download_sha512")),
            download_size=_integer(data.get("download_size")),
            released_at=_text(data.get("released_at")),
            release_channel=_text(data.get("release_channel")),
            error=_text(data.get("error")),
            notes=_keyed_args(data.get("notes")),
        )
        return entry


@dataclass
class Report:
    """Everything one check run found."""

    generated_at: str
    server: ServerContext
    mods_directory: str
    #: Where fetched builds are put. Empty when the feature is off and nothing has been
    #: fetched — a path to a folder that does not exist helps nobody, and an entry that is
    #: "waiting to be installed" needs to be able to say *where*.
    download_folder: str = ""
    entries: List[UpdateEntry] = field(default_factory=list)
    total_jars: int = 0
    unidentified: List[Tuple[str, str]] = field(default_factory=list)
    disabled_jars: List[str] = field(default_factory=list)
    duplicate_ids: Dict[str, List[str]] = field(default_factory=dict)
    advisories: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    upstream_notes: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    duration_seconds: float = 0.0
    #: File names of the entries that became ``update_available`` since the previous report.
    #:
    #: Empty when there was no previous report to compare against, which is why nothing is
    #: said when it is empty: "nothing new" and "nothing to compare with" would produce the
    #: same sentence, and only one of them is worth reading.
    #:
    #: It exists because a server checks on every start, so an admin sees the same twelve
    #: updates announced again and again until they act on them. Without this field the
    #: notification cannot tell "still there" from "just appeared", and the second is the one
    #: that is news.
    new_since_last: List[str] = field(default_factory=list)

    # -- queries -----------------------------------------------------------------------

    def age_seconds(self) -> Optional[float]:
        """How long ago this report was produced, or ``None`` if that cannot be told.

        Used to decide whether a report is still worth reusing instead of running the check
        again — an admin logging in does not need a fresh scan if the answer is ten minutes
        old. Returning ``None`` rather than raising keeps a malformed timestamp from breaking
        the caller: the worst case is that the check simply runs again.
        """
        if not self.generated_at:
            return None
        try:
            produced = datetime.fromisoformat(self.generated_at)
        except ValueError:
            return None
        if produced.tzinfo is None:
            produced = produced.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - produced).total_seconds())

    def by_status(self, *statuses: str) -> List[UpdateEntry]:
        wanted = set(statuses)
        return [entry for entry in self.entries if entry.status in wanted]

    @property
    def updates(self) -> List[UpdateEntry]:
        return self.by_status(STATUS_UPDATE_AVAILABLE)

    @property
    def awaiting_install(self) -> List[UpdateEntry]:
        """Builds already fetched into the download folder, not yet in ``mods/``."""
        return self.by_status(STATUS_AWAITING_INSTALL)

    @property
    def blocked(self) -> List[UpdateEntry]:
        return self.by_status(STATUS_NO_COMPATIBLE_BUILD)

    def counts(self) -> Dict[str, int]:
        tally = {status: 0 for status in ALL_STATUSES}
        for entry in self.entries:
            tally[entry.status] = tally.get(entry.status, 0) + 1
        return tally

    @property
    def actionable_count(self) -> int:
        """How many entries need somebody to do something.

        Derived from :data:`ACTIONABLE_STATUSES` rather than listing the statuses again. It
        used to be ``len(self.updates) + len(self.blocked)``, which meant adding a status to
        the tuple had no effect here at all — and the symptom was subtle: the "downloaded,
        waiting to be installed" section silently disappeared, because the automatic check
        gates its output on this number.
        """
        return sum(1 for entry in self.entries if entry.actionable)

    @property
    def has_updates(self) -> bool:
        """Is there anything worth telling someone about in game?

        A build fetched but not yet installed counts: the admin has not finished, and the
        notification is where they find out that the file is waiting for them.
        """
        return bool(self.updates or self.awaiting_install)

    def sorted_entries(self) -> List[UpdateEntry]:
        return sorted(self.entries, key=lambda entry: entry.sort_key)

    def indexed_entries(self) -> List[Tuple[int, UpdateEntry]]:
        """``(number, entry)`` for every mod, numbered the way the listing shows them.

        The number is the handle a player clicks, so the numbering and the listing have to come
        from one definition. Two would eventually disagree, and the failure — a click landing
        on the wrong mod — would be silent.

        The number is the entry's place in the *full* sorted list, which is why a filtered
        listing shows gaps: the number still means the same mod either way.
        """
        return list(enumerate(self.sorted_entries(), start=1))

    def entry_by_handle(self, handle: str) -> Optional[UpdateEntry]:
        """The entry a click or a typed argument refers to.

        Accepts the listing's number, or a mod id, or a file name — a player copying either one
        out of the listing they are looking at should not have to care which form the command
        wanted. Returns ``None`` rather than raising so the caller can say what it did not find.
        """
        text = (handle or "").strip()
        if not text:
            return None
        if text.isdigit():
            number = int(text)
            for index, entry in self.indexed_entries():
                if index == number:
                    return entry
            return None
        wanted = text.lower()
        for entry in self.entries:
            names = {entry.mod_id.lower(), entry.file_name.lower()}
            names.add(Path(entry.file_name).stem.lower())
            if wanted in names:
                return entry
        return None

    # -- serialisation -----------------------------------------------------------------

    def record_new_since(self, previous: Optional["Report"]) -> None:
        """Note which updates were not already there in ``previous``.

        "Update available" is a state, not an event, so a mod that has needed updating for a
        week is reported identically to one published an hour ago. Comparing the two reports
        is the only way to tell them apart, and the comparison is deliberately one-directional
        and name-based:

        * only ``update_available`` entries count. A build that has just been *downloaded*
          changed to ``awaiting_install``, which is the admin's own doing and not news;
        * the identity is the file name, because that is what the two reports can agree on
          without either one re-resolving anything.

        A mod that leaves and comes back — downloaded, then removed from the download folder —
        counts as new again. That is the useful reading: the update is available once more.
        """
        self.new_since_last = []
        if previous is None:
            return
        before = {
            entry.file_name for entry in previous.by_status(STATUS_UPDATE_AVAILABLE)
        }
        self.new_since_last = sorted(
            entry.file_name
            for entry in self.by_status(STATUS_UPDATE_AVAILABLE)
            if entry.file_name not in before
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format": REPORT_FORMAT,
            "generated_at": self.generated_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "mods_directory": self.mods_directory,
            # Written even though it is only a path: an entry that is "waiting to be
            # installed" names a folder, and a stored report that omitted it produced a
            # notification telling the admin the files were somewhere it could not say.
            "download_folder": self.download_folder,
            "server": asdict(self.server),
            "total_jars": self.total_jars,
            "counts": self.counts(),
            "actionable_count": self.actionable_count,
            "entries": [entry.to_dict() for entry in self.sorted_entries()],
            "unidentified": [
                {"file_name": name, "reason": reason} for name, reason in self.unidentified
            ],
            "disabled_jars": list(self.disabled_jars),
            "duplicate_ids": {
                key: sorted(value) for key, value in sorted(self.duplicate_ids.items())
            },
            "advisories": [
                {"key": key, "args": args} for key, args in self.advisories
            ],
            "upstream_notes": [
                {"key": key, "args": args} for key, args in self.upstream_notes
            ],
            "new_since_last": list(self.new_since_last),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Any) -> Optional["Report"]:
        """Rebuild a report from a stored one, or ``None`` when it cannot be trusted.

        ``None`` is always a safe answer: the caller's response is to run the check, which is
        what it would have done anyway. So anything ambiguous — no format marker, a format
        from another version, no timestamp to measure the age against — is rejected rather
        than guessed at, and the whole report is rejected rather than partially loaded,
        because half a report rendered to an admin reads exactly like a full one.
        """
        if not isinstance(data, dict):
            return None
        if data.get("format") != REPORT_FORMAT:
            return None
        generated_at = _text(data.get("generated_at"))
        if not generated_at:
            return None

        report = cls(
            generated_at=generated_at,
            server=ServerContext.from_dict(data.get("server")),
            mods_directory=_text(data.get("mods_directory")),
            download_folder=_text(data.get("download_folder")),
            total_jars=_integer(data.get("total_jars")),
            duration_seconds=_number(data.get("duration_seconds")),
        )

        entries = data.get("entries")
        if isinstance(entries, list):
            for item in entries:
                entry = UpdateEntry.from_dict(item)
                if entry is not None:
                    report.entries.append(entry)

        unidentified = data.get("unidentified")
        if isinstance(unidentified, list):
            for item in unidentified:
                if isinstance(item, dict):
                    report.unidentified.append(
                        (_text(item.get("file_name")), _text(item.get("reason")))
                    )

        disabled = data.get("disabled_jars")
        if isinstance(disabled, list):
            report.disabled_jars = [item for item in disabled if isinstance(item, str)]

        duplicates = data.get("duplicate_ids")
        if isinstance(duplicates, dict):
            report.duplicate_ids = {
                str(key): [item for item in value if isinstance(item, str)]
                for key, value in duplicates.items()
                if isinstance(value, list)
            }

        report.advisories = _keyed_args(data.get("advisories"))
        report.upstream_notes = _keyed_args(data.get("upstream_notes"))

        new_since = data.get("new_since_last")
        if isinstance(new_since, list):
            report.new_since_last = [item for item in new_since if isinstance(item, str)]

        return report

    @classmethod
    def from_json(cls, text: str) -> Optional["Report"]:
        """Parse a stored report. Malformed JSON is not an error, it is "no report"."""
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return None
        return cls.from_dict(data)


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def _status_key(status: str) -> str:
    return "status." + status


def _entry_parts(entry: UpdateEntry, tr: Translator, verbose: bool) -> Tuple[str, str]:
    """``(description, project link)`` for one entry.

    The description is the mod and its versions, plus — only in a mixed listing — how it was
    identified. In the summary the surrounding heading already says what the group means
    ("these have updates and have not been downloaded"), so repeating the status on every row
    is noise; in a full listing the rows are of all kinds at once and the status is the only
    thing that tells them apart.

    The link is empty unless visiting the project page is the next step — see
    :data:`LINKED_STATUSES`. What is *not* here any more is the download URL: it is eighty-odd
    characters of opaque ids and version strings, it made every line wrap, and it is still in
    the JSON report for anything that wants to fetch a file automatically.
    """
    if entry.status == STATUS_UPDATE_AVAILABLE:
        core = tr(
            "line.update",
            name=entry.name,
            local=entry.local_version or "?",
            latest=entry.latest_version or "?",
        )
    elif entry.status == STATUS_AWAITING_INSTALL:
        core = tr(
            "line.awaiting_install",
            name=entry.name,
            latest=entry.latest_version or "?",
        )
    elif entry.status == STATUS_UP_TO_DATE:
        core = tr("line.up_to_date", name=entry.name, version=entry.local_version or "?")
    elif entry.status in (STATUS_UNRESOLVED, STATUS_NOT_A_MOD):
        core = tr("line.unidentified", name=entry.name, file=entry.file_name)
    else:
        core = tr(
            "line.generic",
            name=entry.name,
            local=entry.local_version or "?",
            latest=entry.latest_version or "?",
        )

    notes: List[str] = []
    if verbose:
        notes.append(tr(_status_key(entry.status)))
        # Only what the reader has to know. Saying "matched by hash" on every row would bury
        # the rows where the plugin was not certain, which are the rows that need reading.
        if entry.matched_by in MATCHED_BY_NOTEWORTHY:
            notes.append(tr("matched_by." + entry.matched_by))

    description = core if not notes else "{}  ({})".format(core, ", ".join(notes))
    link = entry.project_url if entry.status in LINKED_STATUSES else ""
    return description, link


def render_entry_lines(
    entries: Sequence[UpdateEntry], tr: Translator, verbose: bool = False
) -> List[str]:
    """A group of entries, with their project links lined up in one column.

    Alignment is computed over the group rather than fixed, so the column sits right after the
    longest description it actually has to clear. Without it the links start at a different
    place on every row, which is exactly the ragged look a list of URLs produces.
    """
    parts = [_entry_parts(entry, tr, verbose) for entry in entries]
    width = max((len(description) for description, link in parts if link), default=0)
    lines: List[str] = []
    for description, link in parts:
        if link:
            lines.append("  " + description.ljust(width) + _LINK_GAP + link)
        else:
            lines.append("  " + description)
    return lines


def render_tally(report: Report, tr: Translator) -> str:
    """The one-line count of what the check found.

    Extracted because the summary and the listing both end with it, and two copies of a
    sentence built from six lookups would eventually disagree about which status counts as
    what — on the line whose entire job is to be the arithmetic.
    """
    tally = report.counts()
    return tr(
        "report.tally",
        total=len(report.entries),
        up_to_date=tally.get(STATUS_UP_TO_DATE, 0),
        update=tally.get(STATUS_UPDATE_AVAILABLE, 0),
        awaiting=tally.get(STATUS_AWAITING_INSTALL, 0),
        blocked=tally.get(STATUS_NO_COMPATIBLE_BUILD, 0),
        unresolved=tally.get(STATUS_UNRESOLVED, 0)
        + tally.get(STATUS_NOT_A_MOD, 0)
        + tally.get(STATUS_ERROR, 0),
    )


def render_summary(report: Report, tr: Translator, max_updates: int = 12) -> List[str]:
    """The short form: what needs attention, plus a tally. Used for the console notification.

    The two groups are kept apart rather than merged into one "has an update" list, because the
    next action differs completely: one needs fetching (or could not be fetched), the other
    needs copying into ``mods/``. A single list would leave an admin re-reading a mod they
    fetched yesterday, wondering whether the download had worked.
    """
    lines: List[str] = []
    lines.append(
        tr("report.header", version=report.server.describe(), source=report.server.mc_version_source)
    )

    def section(entries, header_key, extra=None):
        if not entries:
            return
        lines.append(tr(header_key, count=len(entries)))
        lines.extend(render_entry_lines(entries[:max_updates], tr, verbose=False))
        # Only when rows were actually held back. The full listing calls this with
        # ``max_updates=0`` on purpose, to get the headings without the rows — and the
        # "and N more" line then claimed N items had been shown and N were missing, right
        # above the section that lists all of them.
        if max_updates and len(entries) > max_updates:
            lines.append(tr("report.and_more", count=len(entries) - max_updates))
        if extra:
            lines.append(extra)

    updates = report.updates
    pending = report.awaiting_install

    # Appended to the update section rather than placed above it, so "there are five" is read
    # before "two of them are new" — the other order makes the second sentence look like a
    # correction of the first.
    section(
        updates,
        "report.updates_found",
        tr(
            "report.new_since_last",
            count=len(report.new_since_last),
            names=", ".join(report.new_since_last[:6]),
        )
        if report.new_since_last
        else None,
    )
    section(
        pending,
        "report.awaiting_install_found",
        tr("report.awaiting_install_hint", folder=report.download_folder)
        if report.download_folder
        else None,
    )
    if not updates and not pending:
        lines.append(tr("report.no_updates"))

    section(report.blocked, "report.blocked_found")

    lines.append(render_tally(report, tr))

    for key, args in report.upstream_notes:
        lines.append(tr(key, **args))
    for key, args in report.advisories:
        lines.append(tr(key, **args))
    return lines


def render_full(report: Report, tr: Translator) -> List[str]:
    """The long form: every mod, its notes, and the diagnostics. Used by the command."""
    lines = list(render_summary(report, tr, max_updates=0))

    lines.append(tr("report.all_mods"))
    ordered = report.sorted_entries()
    # Rendered as one block so the project links line up down the whole listing: the column
    # width depends on every row, which is why the notes are appended here rather than inside.
    for entry, rendered in zip(ordered, render_entry_lines(ordered, tr, verbose=True)):
        lines.append(rendered)
        for key, args in entry.notes:
            lines.append("      - " + tr(key, **args))
        if entry.error:
            lines.append("      - " + tr("report.error_detail", error=entry.error))

    if report.unidentified:
        lines.append(tr("report.unidentified_header", count=len(report.unidentified)))
        for file_name, reason in report.unidentified:
            lines.append("  {}  ({})".format(file_name, reason or tr("report.unknown_reason")))

    if report.disabled_jars:
        lines.append(tr("report.disabled_header", count=len(report.disabled_jars)))
        for file_name in report.disabled_jars:
            lines.append("  " + file_name)

    if report.duplicate_ids:
        lines.append(tr("report.duplicates_header", count=len(report.duplicate_ids)))
        for mod_id, files in sorted(report.duplicate_ids.items()):
            lines.append("  {}: {}".format(mod_id, ", ".join(files)))

    lines.append(tr("report.footer", directory=report.mods_directory, seconds=round(report.duration_seconds, 1)))
    return lines


def render_index_row(number: int, entry: UpdateEntry, tr: Translator) -> str:
    """One row of the compact listing: ``[ 3] Sodium  1.0.0 -> 1.1.0  (可更新)``.

    No links and no notes. That is the whole point: with a project page, a download url and two
    or three notes per mod, a seven-mod server already ran past a screenful, and the detail is
    worth reading for exactly one mod at a time — the one the reader is about to act on.
    """
    description = _entry_parts(entry, tr, verbose=True)[0]
    return "[{}] {}".format(str(number).rjust(2), description)


def index_selection(indexed, row_budget: int):
    """``(rows_to_show, how_many_omitted)`` for a listing with a page budget.

    Entries that need doing always make the cut: they are the reason the command was typed, and
    the sort order already puts them first. Only the informational tail is truncated, which is
    the part nobody needs in full — "these two hundred mods are all up to date" does not require
    two hundred lines.

    If even the actionable set overflows the page it is truncated too, because a reply that
    scrolls is the problem being solved. The count of what was left out is the honest answer
    then, rather than a silent omission.

    The omission count is derived from what was actually shown rather than from the arithmetic
    that selected it: an earlier version computed ``len(rest) - room`` and printed
    "another -9 not shown" whenever the tail was shorter than the room available for it.
    """
    actionable = [item for item in indexed if item[1].actionable]
    rest = [item for item in indexed if not item[1].actionable]

    shown = actionable[:row_budget]
    if len(shown) < row_budget:
        shown = shown + rest[: row_budget - len(shown)]
    return shown, len(indexed) - len(shown)


#: Lines a listing spends on something other than a row: the header, the section title, the
#: tally, the hint, and — only when something was left out — the "not shown" line. Reserved up
#: front so the reply fits whether or not it ends up truncated.
#:
#: The arithmetic lives here, with the listing, rather than at the call site. An earlier version
#: kept it in the chat renderer and reserved three lines instead of five; the listing then came
#: out two lines past the budget, which is exactly the failure this whole change exists to fix.
_INDEX_FIXED_LINES = 5


def render_index(
    report: Report, tr: Translator, entries=None, budget: int = CHAT_PAGE_LINES
):
    """``(head, rows, tail)`` for the numbered listing, capped to a page.

    Split rather than joined because the two callers need different things from the same rows:
    the chat renderer attaches a click to each one, the console and the tests do not. Both need
    the *same* selection, so the selection cannot live in the renderer that decorates them —
    and the row carries its own number and entry, so a click cannot end up on a different mod
    than the text it was attached to.

    ``entries`` narrows the listing to a subset (the status filter) while keeping the numbers
    from the full list, so a number means one mod whichever command produced the row.
    """
    head = tr("report.header", version=report.server.describe(),
              source=report.server.mc_version_source)

    indexed = report.indexed_entries()
    if entries is not None:
        wanted = {id(entry) for entry in entries}
        indexed = [(number, entry) for number, entry in indexed if id(entry) in wanted]

    if not indexed:
        return head, [], [tr("report.no_mods")]

    shown, omitted = index_selection(indexed, max(1, budget - _INDEX_FIXED_LINES))
    rows = [(number, entry, render_index_row(number, entry, tr)) for number, entry in shown]

    tail = [tr("report.index_title", count=len(indexed))]
    if omitted:
        tail.append(tr("report.index_truncated", count=omitted))
    tail.append(render_tally(report, tr))
    tail.append(tr("report.index_hint"))
    return head, rows, tail


def render_index_body(report: Report, tr: Translator, entries=None,
                      budget: int = CHAT_PAGE_LINES) -> List[str]:
    """The listing as plain lines, for the console and for anything counting its height."""
    head, rows, tail = render_index(report, tr, entries=entries, budget=budget)
    return [head] + ["  " + text for _number, _entry, text in rows] + tail


def entry_detail_rows(entry: UpdateEntry, tr: Translator) -> List[DetailRow]:
    """``(label, value, url)`` rows for one mod's detail view.

    This is where the links live, and they can afford to: a detail view is about a single mod,
    so two urls cost nothing, and both are offered as clickable labels rather than as text to
    be copied — which is what makes them usable in the game's chat box at all.
    """
    rows: List[DetailRow] = [( "", entry.name, "")]

    rows.append((tr("detail.status_label"), tr(_status_key(entry.status)), ""))
    if entry.latest_version:
        rows.append((tr("detail.version_label"),
                     tr("detail.version", local=entry.local_version or "?",
                        latest=entry.latest_version), ""))
    elif entry.local_version:
        rows.append((tr("detail.version_label"),
                     tr("detail.local_version", local=entry.local_version), ""))

    if entry.project_url:
        rows.append((tr("detail.project_label"), tr("detail.open_page"), entry.project_url))
    if entry.download_url:
        rows.append((tr("detail.download_label"), tr("detail.open_download"),
                     entry.download_url))

    for key, args in entry.notes:
        # Notes are already whole sentences, so they carry their own label rather than being
        # forced into a ``label: value`` shape they do not have.
        rows.append(("", tr(key, **args), ""))
    if entry.error:
        rows.append(("", tr("report.error_detail", error=entry.error), ""))
    return rows


def render_detail(entry: UpdateEntry, tr: Translator) -> List[str]:
    """The detail view as plain lines, for the console and the report file.

    The labels carry their own separator (``状态: ``), the same way the status screen's do, so
    the punctuation stays translatable and the two renderers cannot disagree about it.
    """
    return [
        (label + value) if label else value
        for label, value, _url in entry_detail_rows(entry, tr)
    ]


def entry_from_scan(mod: Any) -> UpdateEntry:
    """Build the skeleton entry for a scanned jar, before any network work happens.

    Kept here so that the checker and the "just list what is installed" command agree on
    how a jar is described.
    """
    entry = UpdateEntry(
        mod_id=mod.mod_id,
        name=mod.name,
        file_name=mod.file_name,
        local_version=mod.version,
    )
    if mod.error:
        entry.status = STATUS_ERROR
        entry.error = mod.error
    elif not mod.identified:
        entry.status = STATUS_NOT_A_MOD
    if mod.metadata:
        entry.add_note("note.declared_mc", range=mod.metadata.mc_range_text)
        if mod.metadata.bundled_jars:
            entry.add_note("note.bundled_jars", count=mod.metadata.bundled_jars)
        if mod.metadata.is_client_only:
            entry.add_note("note.client_only")
    return entry


def mc_mismatch_note(mod: Any, mc_version: Optional[str]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """A note for a mod whose declared range excludes the server's version."""
    from .serverinfo import mod_supports_server_version

    if mod_supports_server_version(mod, mc_version) is False:
        return ("note.mc_mismatch", {"server": mc_version, "range": mod.metadata.mc_range_text})
    return None
