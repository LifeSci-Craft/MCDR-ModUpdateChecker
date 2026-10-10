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
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from .cleanup import Backup, restored_name
from .jsonfile import json_text
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
    "normalise_name",
    "display_width",
    "UpdateEntry",
    "Report",
    "DetailRow",
    "SummarySection",
    "summarise",
    "render_summary",
    "render_full",
    "render_entry_lines",
    "render_index_row",
    "index_row_fields",
    "entry_row_fields",
    "chat_row_fields",
    "chat_cell_widths",
    "status_tag",
    "STATUS_ICONS",
    "text_quarters",
    "char_quarters",
    "GLYPH_QUARTERS",
    "QUARTERS_PER_LETTER",
    "CJK_QUARTERS",
    "ELLIPSIS",
    "CHAT_PREFIX_QUARTERS",
    "ROW_FIELD_NUMBER",
    "ROW_FIELD_NAME",
    "ROW_FIELD_BODY",
    "ROW_FIELD_NOTE",
    "ROW_FIELD_MARK",
    "has_version_label",
    "version_summary",
    "VERSION_OLD",
    "VERSION_ARROW",
    "VERSION_NEW",
    "VERSION_CURRENT",
    "VERSION_UPSTREAM",
    "VERSION_PLAIN",
    "render_index",
    "render_pager",
    "render_tally",
    "action_row",
    "entry_detail_rows",
    "index_page",
    "CHAT_PAGE_LINES",
    "entry_from_scan",
    "entry_from_backup",
    "format_size",
    "STATUS_OLD_BACKUP",
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
#: A ``.old`` backup left in ``mods/`` by an install. Not a mod and not a check result: it is
#: a file this plugin created and has not cleaned up. It is a status rather than a side list
#: so that one mechanism — numbering, handle lookup, ``list <状态>``, the detail view — covers
#: it, and because ``!!muc delete 7`` has to be as unambiguous as ``!!muc install 7``.
STATUS_OLD_BACKUP = "old_backup"

ALL_STATUSES: Tuple[str, ...] = (
    STATUS_UPDATE_AVAILABLE,
    STATUS_AWAITING_INSTALL,
    STATUS_NO_COMPATIBLE_BUILD,
    STATUS_LOCAL_AHEAD,
    STATUS_UNRESOLVED,
    STATUS_ERROR,
    STATUS_NOT_A_MOD,
    STATUS_OLD_BACKUP,
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
#:
#: ``update_available`` and ``awaiting_install`` deliberately share a rank, and that is the
#: whole reason this is written out by hand instead of being derived from :data:`ALL_STATUSES`.
#: They are one mod in two states — first the newer build exists, then it has been fetched —
#: and the listing's number is the handle an admin types at the *next* command. Separate ranks
#: meant that ``!!muc download 1`` moved that mod below every other update and renumbered the
#: list, so the ``!!muc install 1`` the plugin had just suggested referred to a different mod.
#: Sharing a rank leaves the position untouched: inside a rank the tie-break is the name, and
#: fetching a file does not change a mod's name.
#:
#: Everything else keeps its status as its rank, and ``tests/test_checker.py`` asserts this
#: table covers exactly :data:`ALL_STATUSES` — a status left out of it would silently sort last.
UPDATE_ENTRY_ORDER: Dict[str, int] = {
    STATUS_UPDATE_AVAILABLE: 0,
    STATUS_AWAITING_INSTALL: 0,
    STATUS_NO_COMPATIBLE_BUILD: 1,
    STATUS_LOCAL_AHEAD: 2,
    STATUS_UNRESOLVED: 3,
    STATUS_ERROR: 4,
    STATUS_NOT_A_MOD: 5,
    # Backups sit above the two "nothing to do" groups and below everything that is about a
    # mod's own state. They are not a problem with the server; they are the only rows in the
    # listing that the reader may decide to act on without touching a mod at all.
    STATUS_OLD_BACKUP: 6,
    STATUS_UP_TO_DATE: 7,
    STATUS_IGNORED: 8,
}

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

class DetailRow(NamedTuple):
    """One row of a mod's detail view.

    ``label`` is empty for the heading row. At most one of ``url`` and ``command`` is set: a
    ``url`` opens in the browser, a ``command`` runs when the value is clicked, and both are
    empty for a row that is only text. Keeping the click target beside the value rather than
    inside it is what lets one row list feed both renderers — the chat one makes the value
    clickable, the plain one just joins label and value.
    """

    label: str
    value: str
    url: str = ""
    command: str = ""

Translator = Callable[..., str]

#: Everything that is not a lowercase letter or a digit, for :func:`normalise_name`.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise_name(text: str) -> str:
    """Lowercase, strip everything that is not alphanumeric. Used to compare names loosely.

    Two callers need exactly this comparison and they must not disagree about it:
    ``check.ignored_mods``, where ``Fabric-API`` and ``fabricapi`` are the same entry, and the
    handle lookup, where a reader types ``fabric api`` for a mod listed as ``Fabric-API``.

    It lives here rather than in ``checker.py`` — where it used to — because ``checker`` imports
    ``report`` and not the other way round, so a shared helper has to sit on this side.
    ``checker`` re-exports the name, so its callers are unaffected.
    """
    return _NON_ALNUM.sub("", (text or "").lower())


def _literal_names(entry: "UpdateEntry") -> Set[str]:
    """Every exact spelling of a mod, lowercased. Empty ones dropped.

    Annotated as a string because ``UpdateEntry`` is declared further down the module and the
    annotation would otherwise be evaluated before it exists.
    """
    names = {entry.mod_id.lower(), entry.name.lower(), entry.file_name.lower()}
    names.add(Path(entry.file_name).stem.lower())
    return {name for name in names if name}


def _normalised_names(entry: "UpdateEntry") -> Set[str]:
    """The same spellings run through :func:`normalise_name`."""
    return {normalise_name(name) for name in _literal_names(entry)} - {""}


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
    #: Size on disk and age of a ``.old`` backup. Only ``old_backup`` entries carry them, and
    #: the age is a snapshot taken when the check ran — every decision about *deleting* one is
    #: made from the directory itself, never from a report that may be a day old.
    size_bytes: int = 0
    age_days: int = 0
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
        return cls(
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
            size_bytes=_integer(data.get("size_bytes")),
            age_days=_integer(data.get("age_days")),
            released_at=_text(data.get("released_at")),
            release_channel=_text(data.get("release_channel")),
            error=_text(data.get("error")),
            notes=_keyed_args(data.get("notes")),
        )


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

    @property
    def backups(self) -> List[UpdateEntry]:
        """The ``.old`` files in ``mods/``, as of this report.

        A group query like the others so that ``list <状态>``, the completion candidates and
        the cleanup plan all read the same set. What they must **not** be used for is deciding
        what to delete: that is always re-derived from the directory, because a report can be
        up to a day old and a file listing cannot.
        """
        return self.by_status(STATUS_OLD_BACKUP)

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

    def resolve_handle(self, handle: str) -> Tuple[Optional[UpdateEntry], str]:
        """The entry a click or a typed argument refers to, plus why it could not be found.

        Four ways to spell a mod are accepted, because all four are what the reader actually has
        in front of them:

        * the listing's number, which is what the clickable row carries;
        * the **display name** the row shows — the one spelling a reader is most likely to copy,
          and the one this used to be missing;
        * the mod id;
        * the jar's file name, with or without ``.jar``.

        The first three are matched literally (case-insensitively) before anything fuzzy is
        tried, so a precise handle can never lose to a lenient comparison. Only then is the
        forgiving form applied — the same one ``check.ignored_mods`` uses, where ``Fabric-API``
        and ``fabricapi`` are the same thing — and it has to land on **exactly one** entry.
        Two mods that normalise to the same string is not a lookup to guess at: guessing wrong
        means downloading and installing the wrong jar, so the ambiguity is reported instead.

        A handle that is **the beginning of** exactly one mod's name is accepted too, which is
        the closest thing to tab completion this game allows for chat commands (see
        ``ambiguity_candidates``). The prefix is only tried after the exact forms, so ``lith``
        can never shadow a mod actually named ``lith``; and it still has to be unique, for the
        same reason the lenient form does.

        ``reason`` is ``""`` on success, and otherwise one of ``"empty"``, ``"out-of-range"``,
        ``"ambiguous"`` or ``"unknown"`` — kept apart so the caller can say which, since "that
        number does not exist" and "two mods answer to that name" need different next actions.
        """
        text = (handle or "").strip()
        if not text:
            return None, "empty"

        if text.isdigit():
            number = int(text)
            for index, entry in self.indexed_entries():
                if index == number:
                    return entry, ""
            return None, "out-of-range"

        wanted = text.lower()
        for entry in self.entries:
            if wanted in _literal_names(entry):
                return entry, ""

        normalised = normalise_name(text)
        if normalised:
            matches = self._lenient_matches(normalised)
            if len(matches) == 1:
                return matches[0], ""
            if matches:
                return None, "ambiguous"

        return None, "unknown"

    def _lenient_matches(self, normalised: str) -> List[UpdateEntry]:
        """Entries a forgiving spelling could mean: exact hits first, then prefix hits.

        The two are never mixed. If any entry matches exactly — itself, its id, its file name,
        punctuation aside — then nothing else is considered, because widening a hit that was
        already found is how a precise handle starts losing to a coincidence. Only when there
        is no exact match at all does "starts with" get a say, and then it has to be unique for
        the caller to act on it.
        """
        exact = [
            entry for entry in self.entries if normalised in _normalised_names(entry)
        ]
        if exact:
            return exact
        return [
            entry
            for entry in self.entries
            if any(name.startswith(normalised) for name in _normalised_names(entry))
        ]

    def ambiguity_candidates(self, handle: str) -> List[UpdateEntry]:
        """The entries a handle could have meant, for the message that lists them.

        Also the in-game substitute for tab completion: the caller turns each of these into a
        clickable name that fills the command in, which is what the completion list would have
        offered. Computed by the same helper ``resolve_handle`` uses, so the names in the
        message cannot drift from the names that actually matched.
        """
        normalised = normalise_name(handle or "")
        if not normalised:
            return []
        return self._lenient_matches(normalised)

    def entry_by_handle(self, handle: str) -> Optional[UpdateEntry]:
        """The entry for ``handle``, or ``None``.

        The forgiving form of :meth:`resolve_handle` for callers that only need the answer.
        """
        return self.resolve_handle(handle)[0]

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
        """The file form of a report: the same text every other stored file is written with."""
        return json_text(self.to_dict())

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


#: What each piece of a listing row *is*. A renderer colours by these roles and the console
#: joins them, so "what a row says" cannot drift apart from "what colour it is".
#:
#: The roles exist because the whole row used to be one string in one colour, chosen by "does
#: this need somebody" — which made the **number** yellow on the rows that needed action and
#: gray on the rest, so a column of handles changed colour from row to row. A column of handles
#: should read as a column; the part that changes colour should be the status, which is the part
#: that means something.
ROW_FIELD_NUMBER = "number"
ROW_FIELD_NAME = "name"
#: The facts about this jar: its versions, its size, or its file name.
ROW_FIELD_BODY = "body"
#: The ``[状态: ✔]`` tag — the words and the closing bracket, which never change colour.
ROW_FIELD_NOTE = "note"
#: The icon inside that tag, on its own so it can be coloured: it is the one piece of the cell
#: that says *which* state this is, so it is the piece the status colour belongs on.
ROW_FIELD_MARK = "mark"

#: What each piece of the ``[版本]`` tooltip *is*. Pieces rather than one string for the same
#: reason the row roles exist: the colour is chosen per piece from this vocabulary, and nothing
#: re-derives "which number is the old one" from the text.
VERSION_OLD = "old"
VERSION_ARROW = "arrow"
VERSION_NEW = "new"
VERSION_CURRENT = "current"
VERSION_UPSTREAM = "upstream"
VERSION_PLAIN = "plain"

#: The statuses whose row facts *are* versions, and so move behind the ``[版本]`` label.
#:
#: The other two keep their facts on the row: an old backup shows a size and an age, and an
#: unidentified jar shows its file name — neither is a version, and a label that says 版本 over
#: either of them would be lying about what is inside.
VERSION_LABEL_STATUSES: Tuple[str, ...] = (
    STATUS_UPDATE_AVAILABLE,
    STATUS_AWAITING_INSTALL,
    STATUS_UP_TO_DATE,
    STATUS_LOCAL_AHEAD,
    STATUS_NO_COMPATIBLE_BUILD,
    STATUS_IGNORED,
    STATUS_ERROR,
)


def has_version_label(entry: UpdateEntry) -> bool:
    """Whether this row's facts are versions — i.e. whether the chat shows ``[版本]``."""
    return entry.status in VERSION_LABEL_STATUSES


def version_summary(entry: UpdateEntry, tr: Translator) -> List[Tuple[str, str]]:
    """The tooltip behind ``[版本]``: ``(role, text)`` pieces, in reading order.

    An available update reads ``old >>> new``; a single version — the one waiting to be
    installed — is just itself; the two-version states that are not updates read
    ``local / upstream``. Missing numbers print as ``?``, exactly as the plain text does.

    Returned as pieces because the colours differ per piece, and they are *reader* colours
    (red old, green new) that live with the other UI colours in the entry module — this
    function only says what each piece means.
    """
    local = entry.local_version or "?"
    latest = entry.latest_version or "?"
    if entry.status == STATUS_UPDATE_AVAILABLE:
        return [
            (VERSION_OLD, local),
            (VERSION_ARROW, tr("version.arrow")),
            (VERSION_NEW, latest),
        ]
    if entry.status == STATUS_AWAITING_INSTALL:
        return [(VERSION_NEW, latest)]
    if entry.status == STATUS_UP_TO_DATE:
        return [(VERSION_CURRENT, local)]
    return [
        (VERSION_CURRENT, local),
        (VERSION_PLAIN, tr("version.separator")),
        (VERSION_UPSTREAM, latest),
    ]


def entry_status_note(entry: UpdateEntry, tr: Translator) -> str:
    """The words that say what state this mod is in, and how sure the plugin is.

    The caveat rides along only when it is *noteworthy*: saying "matched by hash" on every row
    would bury the rows where the plugin was not certain, which are the rows that need reading.
    """
    note = tr(_status_key(entry.status))
    if entry.matched_by in MATCHED_BY_NOTEWORTHY:
        note += tr("line.note_separator") + tr("matched_by." + entry.matched_by)
    return note


def _entry_body(entry: UpdateEntry, tr: Translator) -> str:
    """The facts about one jar — versions, size or file name — and **never** its status.

    Two of these templates used to repeat the status (``{name}  {version}（已是最新）``), and the
    note group added it again on the same row: a reader saw ``（已是最新）  (已是最新)`` and asked
    why it said the same thing twice. The status has exactly one home now — the note group —
    which is also what lets a renderer colour it.
    """
    if entry.status == STATUS_UPDATE_AVAILABLE:
        return tr("line.update", local=entry.local_version or "?",
                  latest=entry.latest_version or "?")
    if entry.status == STATUS_AWAITING_INSTALL:
        return tr("line.awaiting_install", latest=entry.latest_version or "?")
    if entry.status == STATUS_OLD_BACKUP:
        return tr("line.old_backup", size=format_size(entry.size_bytes), days=entry.age_days)
    if entry.status == STATUS_UP_TO_DATE:
        return tr("line.up_to_date", version=entry.local_version or "?")
    if entry.status in (STATUS_UNRESOLVED, STATUS_NOT_A_MOD):
        return tr("line.unidentified", file=entry.file_name)
    return tr("line.generic", local=entry.local_version or "?",
              latest=entry.latest_version or "?")


def entry_row_fields(entry: UpdateEntry, tr: Translator,
                     verbose: bool = True) -> List[Tuple[str, str]]:
    """``(role, text)`` for one row, without its number: name, facts, and the status note.

    This is the **plain** form — what the console, the log and the stored report print. The
    chat gets :func:`chat_row_fields` instead: same roles, but the facts collapse to a label
    and the status becomes a bracketed tag, because in the chat a hover can carry what the
    plain form has to spell out.

    ``verbose`` off leaves the note out: a summary's heading already says what the group means
    ("these have updates and have not been downloaded"), so repeating it on every row is noise.
    In a mixed listing the rows are of all kinds at once and the status is the only thing that
    tells them apart.
    """
    fields = [(ROW_FIELD_NAME, entry.name), (ROW_FIELD_BODY, "  " + _entry_body(entry, tr))]
    if verbose:
        fields.append(
            (ROW_FIELD_NOTE, tr("line.note_group", text=entry_status_note(entry, tr)))
        )
    return fields


def index_row_fields(number: Optional[int], entry: UpdateEntry, tr: Translator,
                     verbose: bool = True) -> List[Tuple[str, str]]:
    """``(role, text)`` for one numbered row of the plain form, in printing order."""
    fields: List[Tuple[str, str]] = []
    if number is not None:
        fields.append((ROW_FIELD_NUMBER, "[{}] ".format(number)))
    return fields + entry_row_fields(entry, tr, verbose)


# --------------------------------------------------------------------------------------
# The game's font, measured
# --------------------------------------------------------------------------------------
#
# Padding a column with spaces only lines it up if the widths are right, and the widths the game
# draws with are not the widths ``display_width`` assumes. That function counts one column per
# ASCII character and two per Han character; the font behind the reader's screenshot draws
# ``i``/``j``/``l``/``t``/``f``/``I``/``1`` at three quarters of a letter, ``.`` and both brackets
# at half, and a Han character at **2¼** — not 2. Half of the raggedness in ``!!muc list`` came
# from that last number on its own.
#
# These numbers are measured, not guessed: ``bench/measure_list_columns.py`` reads the starting
# column of every cell out of a screenshot, ``bench/read_band_runs.py`` prints the advance of
# every glyph, and the table below is what that client actually does. It is still a *model* — a
# space is one width and the glyphs are another, so no padding can land closer than half a space
# to its target. ``display_width`` stays for the console and the logs, where the font really is
# fixed-width and counting characters is exact.

#: Glyphs that are not one letter wide, in quarter-letters. Everything else is
#: :data:`QUARTERS_PER_LETTER`, except Han characters — see :data:`CJK_QUARTERS`. The ``!`` is
#: the odd one out: it was fitted years before the rest, by the help page's model scanner, and
#: it is in here because ``!!muc`` starts with two of them.
GLYPH_QUARTERS: Dict[str, int] = {
    "i": 3, "j": 3, "l": 3, "t": 3, "f": 3, "I": 3, "1": 3,
    ".": 2, "[": 2, "]": 2,
    "!": 1,
}

QUARTERS_PER_LETTER = 4

#: A Han character in the same unit: 2¼ letters, measured. ``display_width`` says two, which is
#: right for a terminal and wrong for the game — and a name is one of the few places a Han
#: character shows up in an otherwise Latin row.
CJK_QUARTERS = 9

#: How wide one of :data:`STATUS_ICONS` is, in quarter-letters. A guess until the next
#: screenshot: they come out of the font's symbol sheet, which no table of ours covers, so the
#: number is one letter for now and ``bench/measure_list_columns.py`` is what will pin it down
#: (the icons stand in fixed places, so one screenshot measures all ten).
ICON_QUARTERS = 4

#: What a name cut to fit ends with. Three characters, so it belongs in the table above.
ELLIPSIS = "..."


def char_quarters(char: str) -> int:
    """One character's advance in the game's font, in quarter-letters.

    The icons of :data:`STATUS_ICONS` are checked *before* the East Asian rule and counted as
    one letter: they live in the font's symbol sheet, not in a Han font, but half of them carry
    an emoji presentation — ``❌``, ``⏪`` — and ``east_asian_width`` calls those Wide, which
    would have made the status column jump by a letter depending on the mod's state.
    """
    width = GLYPH_QUARTERS.get(char)
    if width is not None:
        return width
    if char in _ICON_CHARS:
        return ICON_QUARTERS
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return CJK_QUARTERS
    return QUARTERS_PER_LETTER


def text_quarters(text: str) -> int:
    """``text`` in quarter-letters, for the player's font. See :data:`GLYPH_QUARTERS`."""
    return sum(char_quarters(char) for char in text)


#: The icon that stands for each status inside the ``[状态: ✔]`` tag that closes every row's
#: columns. Every one of them is a character vanilla Minecraft ships in its symbol sheet
#: (``nonlatin_european.png``): a character the font does not have is drawn as an empty box,
#: which is worse than no icon at all, so this stays inside that set instead of reaching for
#: whatever reads best. ``⚠`` and ``♻`` are *not* in it — which is why the reader's suggestion
#: became ``❌`` and ``⏪``.
STATUS_ICONS: Dict[str, str] = {
    STATUS_UPDATE_AVAILABLE: "↑",
    STATUS_AWAITING_INSTALL: "↓",
    STATUS_NO_COMPATIBLE_BUILD: "❌",
    STATUS_LOCAL_AHEAD: "☆",
    STATUS_UNRESOLVED: "?",
    STATUS_ERROR: "✘",
    STATUS_NOT_A_MOD: "○",
    STATUS_OLD_BACKUP: "⏪",
    STATUS_UP_TO_DATE: "✔",
    STATUS_IGNORED: "—",
}

#: The icon characters themselves, for :func:`char_quarters` — see there for why they cannot go
#: through the East Asian rule.
_ICON_CHARS = frozenset(STATUS_ICONS.values())

#: What is drawn for a status that has no icon of its own — one added to the enum without coming
#: back to the table above. ``?`` doubles as ``unresolved``'s own icon, which reads correctly
#: here too: the honest answer to "which state is this" is "I don't know".
FALLBACK_ICON = "?"


def status_tag(tr: Translator, status: str) -> str:
    """The row's status cell: ``[状态: ✔]``.

    The same two words on every row, plus one icon that says which state this mod is in — the
    reader asked for exactly that after the abbreviations (``[已是最新]``, ``[旧版备份]``) turned
    the column into a wall of prose. What the state *means* is one hover away.
    """
    return tr("line.status_tag", word=tr("line.status_word"),
              icon=STATUS_ICONS.get(status, FALLBACK_ICON))


#: The chat row's number+name cell, in **quarter-letters** — 24 letters. The name is truncated
#: to fit and padded to the full width, so the columns after it start at the same place on every
#: row, which is what "aligned" means here. The number itself is never padded (a rule this
#: project has been asked for twice); instead the **cell** is, so ten-row pages do not push
#: two-digit rows out of line.
CHAT_PREFIX_QUARTERS = 24 * QUARTERS_PER_LETTER


def _pad_cell(text: str, width: int) -> str:
    """The spaces that bring ``text`` to ``width`` quarter-letters — within half a space.

    Split out of :func:`_fit_cell` because the status tag is drawn as three pieces (the words,
    the icon, the closing bracket) and only the last of them can carry the padding: the pieces
    have to be coloured apart, and a colour needs a piece of its own.
    """
    spaces = int((width - text_quarters(text)) / QUARTERS_PER_LETTER + 0.5)
    return " " * max(0, spaces)


def _fit_cell(text: str, width: int) -> str:
    """Truncate ``text`` to ``width`` quarter-letters, then pad it as close as the font allows.

    Truncation is marked with ``...`` and always leaves the ellipsis room, so a name cut at an
    odd half still ends mid-cell rather than overflowing it. The full text stays reachable
    through the cell's hover — which is the trade this whole row design makes.

    The padding is whole spaces, so the cell lands within half a space of ``width``. That is the
    floor for a proportional font and it is worth saying plainly rather than claiming a
    precision the client cannot draw.
    """
    if text_quarters(text) > width:
        kept = ""
        used = 0
        for char in text:
            char_width = char_quarters(char)
            if used + char_width + text_quarters(ELLIPSIS) > width:
                break
            kept += char
            used += char_width
        text = kept + ELLIPSIS
    return text + _pad_cell(text, width)


def _measure_cells(tr: Translator) -> Tuple[int, int]:
    """Measure the two fixed cells. Split out so :func:`chat_cell_widths` can cache the answer."""
    body = max(text_quarters(tr(key))
               for key in ("line.version_label", "line.backup_label", "line.file_label"))
    status = max(text_quarters(status_tag(tr, name)) for name in ALL_STATUSES)
    return body, status


#: ``chat_cell_widths`` is called once **per row**, and what it returns depends on the catalogue
#: rather than on the row: without this, drawing a ten-row page re-measured thirteen labels and
#: a dozen strings eleven times. Keyed by the translator's language, and only for translators
#: that say what they are — a stand-in (a test's lambda) is measured every time, which is the
#: safe direction.
_CELL_WIDTHS: Dict[str, Tuple[int, int]] = {}


def chat_cell_widths(tr: Translator) -> Tuple[int, int]:
    """``(body, status)`` cell widths, in quarter-letters, for the current language.

    Derived from the labels themselves rather than kept as constants: the English status tags
    are much longer than the Chinese ones, and a hardcoded width would either misalign one
    language or waste a column in the other. A new status widens every row — visibly, and in one
    place.

    Memoised per language. The catalogue cannot change while the plugin runs, so the only way
    to get a stale answer is for the cache key to be wrong — hence the ``language`` attribute
    :func:`i18n.make_translator` puts on the callable it returns, and the no-cache fallback
    above for anything that does not carry one.
    """
    language = getattr(tr, "language", None)
    if language is None:
        return _measure_cells(tr)
    widths = _CELL_WIDTHS.get(language)
    if widths is None:
        widths = _CELL_WIDTHS[language] = _measure_cells(tr)
    return widths


def _row_body_label(entry: UpdateEntry, tr: Translator) -> str:
    """What the middle cell of a chat row is: the version handle, or a handle for the facts.

    Rows whose facts really are versions keep ``[版本]``. The other two statuses have facts
    that are not a version, and a label that claimed otherwise would be lying about what the
    tooltip holds — so an old backup gets ``[备份]`` (size and age inside) and an unidentified
    jar gets ``[文件]`` (its file name inside).
    """
    if has_version_label(entry):
        return tr("line.version_label")
    if entry.status == STATUS_OLD_BACKUP:
        return tr("line.backup_label")
    return tr("line.file_label")


def chat_row_fields(number: Optional[int], entry: UpdateEntry, tr: Translator,
                    verbose: bool = True) -> List[Tuple[str, str]]:
    """``(role, text)`` for one row as the **chat** draws it: padded columns and bracketed tags.

    Three cells, each padded to a fixed width: ``[3] Sodium        [版本]  [状态: ↑]``. The long
    facts move into the cells' tooltips (the version numbers, the backup's size and age, the
    jar's file name) and the status becomes a tag instead of a parenthetical — the reader asked
    for both, because the parentheticals made every row a sentence.

    The console keeps :func:`index_row_fields`: a log line has no hover, so there the facts have
    to stay on the row, and there is no mouse to aim at a tag either.
    """
    fields: List[Tuple[str, str]] = []
    if number is None:
        fields.append((ROW_FIELD_NAME, _fit_cell(entry.name, CHAT_PREFIX_QUARTERS)))
    else:
        prefix = "[{}] ".format(number)
        fields.append((ROW_FIELD_NUMBER, prefix))
        fields.append((ROW_FIELD_NAME,
                       _fit_cell(entry.name,
                                 CHAT_PREFIX_QUARTERS - text_quarters(prefix))))
    body_width, status_width = chat_cell_widths(tr)
    fields.append((ROW_FIELD_BODY,
                   "  " + _fit_cell(_row_body_label(entry, tr), body_width)))
    if verbose:
        # The status tag comes out as three pieces — words, icon, closing bracket — so the icon
        # can carry the status colour while the words stay the one colour every row shares. The
        # padding rides on the last piece, so the cell still ends where the next column starts.
        tag = status_tag(tr, entry.status)
        icon = STATUS_ICONS.get(entry.status, FALLBACK_ICON)
        head, marker, tail = tag.partition(icon)
        if not marker:  # a catalogue that dropped {icon}: say it, do not draw a wrong cell
            fields.append((ROW_FIELD_NOTE, "  " + _fit_cell(tag, status_width)))
        else:
            fields.append((ROW_FIELD_NOTE, "  " + head))
            fields.append((ROW_FIELD_MARK, icon))
            fields.append((ROW_FIELD_NOTE, tail + _pad_cell(tag, status_width)))
    return fields


def entry_line_text(entry: UpdateEntry, tr: Translator) -> str:
    """``Name  facts`` as plain text — what the console, the log and the notices print."""
    return "".join(text for _role, text in entry_row_fields(entry, tr, verbose=False))


def _entry_parts(entry: UpdateEntry, tr: Translator, verbose: bool) -> Tuple[str, str]:
    """``(description, project link)`` for one entry, for the console's aligned columns.

    The link is empty unless visiting the project page is the next step — see
    :data:`LINKED_STATUSES`. What is *not* here any more is the download URL: it is eighty-odd
    characters of opaque ids and version strings, it made every line wrap, and it is still in
    the JSON report for anything that wants to fetch a file automatically.
    """
    description = "".join(text for _role, text in entry_row_fields(entry, tr, verbose=verbose))
    link = entry.project_url if entry.status in LINKED_STATUSES else ""
    return description, link


def display_width(text: str) -> int:
    """How many columns ``text`` takes in a fixed-width font.

    ``len`` counts characters, but the game's font draws a Chinese character twice as wide as
    an ``A`` — and every row this plugin prints ends in a Chinese status note, so a column
    padded by character count was out by however many ideographs the note happened to contain.
    On the shipped language that is every row, which is how the project links came out ragged.

    East Asian Wide and Fullwidth count as two columns; everything else as one. Ambiguous-width
    characters (``→``, ``±``) are counted as one, which is how the default font draws them.
    """
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text)


def _pad_to(text: str, width: int) -> str:
    """``text`` followed by however many spaces bring it to ``width`` *columns*."""
    return text + " " * max(0, width - display_width(text))


def render_entry_lines(
    entries: Sequence[UpdateEntry], tr: Translator, verbose: bool = False
) -> List[str]:
    """A group of entries, with their project links lined up in one column.

    Alignment is computed over the group rather than fixed, so the column sits right after the
    longest description it actually has to clear. Without it the links start at a different
    place on every row, which is exactly the ragged look a list of URLs produces.
    """
    parts = [_entry_parts(entry, tr, verbose) for entry in entries]
    width = max((display_width(description) for description, link in parts if link), default=0)
    lines: List[str] = []
    for description, link in parts:
        if link:
            lines.append("  " + _pad_to(description, width) + _LINK_GAP + link)
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


@dataclass
class SummarySection:
    """One group of the summary, before it is laid out.

    ``trailing`` holds the lines that come after its rows: the "and N more" line first, when
    the group was truncated, then whatever the group itself adds (which part is new, where the
    fetched files went). Kept apart from the rows so a renderer that decorates rows — with a
    number and a click — does not have to work out which lines those are by looking at them.
    """

    heading: str
    entries: List[UpdateEntry]
    trailing: List[str] = field(default_factory=list)
    #: What state every entry in this group is in — the group *is* "the mods in this state", so a
    #: renderer colours the heading by it. It is the counterpart of the note group on a listing
    #: row: a grouped screen has no per-row status text, so the status colour goes on the
    #: smallest thing that still says what the state is, which is the heading above the rows.
    status: str = ""


def summarise(report: Report, tr: Translator, max_updates: int = 12):
    """``(context, blocks, closing)`` — the summary, before it is laid out.

    Split rather than returned as one list of strings so the two renderers can decorate the
    rows differently without disagreeing about what the sections are: the console and the
    report file want one block of text, and the in-game reply wants a clickable button on every
    row. Both callers walk the same blocks in the same order, so the headings, their order and
    the "and N more" arithmetic cannot drift apart.

    A block is a :class:`SummarySection` or a plain line — the latter for the "nothing to do"
    sentence, which belongs where it was written: above the blocked group, not after it.
    """
    context = tr("report.header", version=report.server.describe(),
                 source=report.server.mc_version_source)
    blocks: List[Any] = []

    def add_section(entries, header_key, extra=None, status=""):
        if not entries:
            return
        # Sorted exactly as the listing sorts them. The sections are cut out of the same set of
        # entries the numbers come from, so a section left in the order the scan happened to
        # produce printed its numbers out of order — ``[2]`` above ``[1]`` — and a reader who
        # noticed would have no way to tell that from a bug in the numbering.
        ordered = sorted(entries, key=lambda item: item.sort_key)
        trailing: List[str] = []
        # Only when rows were actually held back. The full listing calls this with
        # ``max_updates=0`` on purpose, to get the headings without the rows — and the
        # "and N more" line then claimed N items had been shown and N were missing, right
        # above the section that lists all of them.
        if max_updates and len(ordered) > max_updates:
            trailing.append(tr("report.and_more", count=len(ordered) - max_updates))
        if extra:
            trailing.append(extra)
        blocks.append(SummarySection(tr(header_key, count=len(ordered)),
                                     ordered[:max_updates], trailing, status))

    updates = report.updates
    pending = report.awaiting_install

    # The new-since line is appended to the update section rather than placed above it, so
    # "there are five" is read before "two of them are new" — the other order makes the second
    # sentence look like a correction of the first.
    add_section(
        updates,
        "report.updates_found",
        tr("report.new_since_last", count=len(report.new_since_last),
           names=", ".join(report.new_since_last[:6]))
        if report.new_since_last
        else None,
        STATUS_UPDATE_AVAILABLE,
    )
    add_section(
        pending,
        "report.awaiting_install_found",
        tr("report.awaiting_install_hint", folder=report.download_folder)
        if report.download_folder
        else None,
        STATUS_AWAITING_INSTALL,
    )
    if not updates and not pending:
        blocks.append(tr("report.no_updates"))

    add_section(report.blocked, "report.blocked_found",
                status=STATUS_NO_COMPATIBLE_BUILD)

    closing = [render_tally(report, tr)]
    for key, args in report.upstream_notes:
        closing.append(tr(key, **args))
    for key, args in report.advisories:
        closing.append(tr(key, **args))
    return context, blocks, closing


def render_summary(report: Report, tr: Translator, max_updates: int = 12) -> List[str]:
    """The short form as plain lines: what needs attention, plus a tally.

    The two groups are kept apart rather than merged into one "has an update" list, because the
    next action differs completely: one needs fetching (or could not be fetched), the other
    needs copying into ``mods/``. A single list would leave an admin re-reading a mod they
    fetched yesterday, wondering whether the download had worked.

    This is the form the console log and the report file get, which is why a row still ends
    with the project url: a log line cannot be clicked, so the url is the only way to reach the
    page from there.
    """
    context, blocks, closing = summarise(report, tr, max_updates)
    lines = [context]
    for block in blocks:
        if isinstance(block, SummarySection):
            lines.append(block.heading)
            lines.extend(render_entry_lines(block.entries, tr, verbose=False))
            lines.extend(block.trailing)
        else:
            lines.append(block)
    lines.extend(closing)
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


def render_index_row(number: Optional[int], entry: UpdateEntry, tr: Translator,
                     verbose: bool = True) -> str:
    """One row of the compact listing: ``[3] Sodium  1.0.0 -> 1.1.0  (可更新)``.

    No links and no notes. That is the whole point: with a project page, a download url and two
    or three notes per mod, a seven-mod server already ran past a screenful, and the detail is
    worth reading for exactly one mod at a time — the one the reader is about to act on.

    The number is printed as it is, with **no padding**. Two other answers were tried and both
    were wrong, which is worth recording so a third attempt does not repeat them: a constant
    width of two produced ``[ 1]`` on a five-mod server, where the extra character is simply a
    gap after the bracket, and deriving the width from the largest number on the page still
    padded every single-digit row on a twenty-mod server — the reader reported it twice. The
    column is not aligned any more, and that is the deliberate trade: one stray space across
    nineteen rows is worse than ``[9]`` and ``[10]`` starting a character apart.

    ``number`` is ``None`` only for a caller that has an entry but no place in the listing. The
    row is then printed without a handle rather than with a made-up one, because ``[0]`` is a
    number that looks typeable and resolves to nothing.

    ``verbose`` adds the status in parentheses, for a listing that mixes statuses. A summary
    section does not pass it: the heading above the rows already says what the group means.

    The pieces come from :func:`index_row_fields`, and joining them here is what keeps the
    console and the chat showing the same row: the chat colours those same pieces instead of
    joining them.
    """
    return "".join(text for _role, text in index_row_fields(number, entry, tr, verbose))


def index_page(indexed, page: int, size: int):
    """``(rows_on_this_page, page, pages)`` for a listing cut into pages of ``size``.

    ``page`` is clamped into the valid range rather than refused: the buttons that carry a page
    number are stale the moment the listing shrinks, and "the number you clicked no longer
    exists" is a worse answer than showing the last page — which the pager line then states, so
    the reader is not misled about where they are.

    The order is the listing's own, which already puts the mods that need attention first; the
    previous truncating version had to re-sort to guarantee that, and pagination gets it for
    free. Nothing is dropped either — every entry is on some page.
    """
    pages = max(1, (len(indexed) + size - 1) // size)
    page = min(max(1, page), pages)
    return indexed[(page - 1) * size: page * size], page, pages


#: Lines a listing spends on something other than a row, reserved before the rows are chosen:
#: the title bar, the server context, the section title, the tally, the hint, the pager, and
#: the closing rule.
#:
#: The arithmetic lives here, with the listing, rather than at the call site. An earlier version
#: kept it in the chat renderer and reserved three lines instead of five; the listing then came
#: out two lines past the budget, which is exactly the failure this whole change exists to fix.
#: It went to six when every screen gained the title bar, to seven when the listing gained
#: a pager, and to eight when every screen gained the closing rule — the constant has to be
#: raised by whatever the screen adds, or the budget silently stops being a budget.
_INDEX_FIXED_LINES = 8


def render_index(
    report: Report, tr: Translator, entries=None, page: int = 1, budget: int = CHAT_PAGE_LINES
):
    """``(rows, tail, page_info)`` for one page of the numbered listing.

    Split rather than joined because the two callers need different things from the same rows:
    the chat renderer attaches a click to each one, the console and the tests do not. Both need
    the *same* selection, so the selection cannot live in the renderer that decorates them —
    and the row carries its own number and entry, so a click cannot end up on a different mod
    than the text it was attached to.

    The line above the rows is not returned: it is the server context, and each renderer draws
    it in its own vocabulary — the screens use the same labelled field the status screen does.
    The rows and the tail are the part that must not be computed twice. The pager line is not
    returned either: it names commands, and the command spelling belongs to the caller that
    knows which prefix the reader typed, so it is built by :func:`render_pager` there.

    ``entries`` narrows the listing to a subset (the status filter) while keeping the numbers
    from the full list, so a number means one mod whichever command produced the row.

    ``page_info`` is ``(page, pages)``, or ``None`` when there is nothing to show.
    """
    indexed = report.indexed_entries()
    if entries is not None:
        wanted = {id(entry) for entry in entries}
        indexed = [(number, entry) for number, entry in indexed if id(entry) in wanted]

    if not indexed:
        return [], [tr("report.no_mods")], None

    shown, page, pages = index_page(indexed, page, max(1, budget - _INDEX_FIXED_LINES))
    rows = [
        (number, entry, render_index_row(number, entry, tr))
        for number, entry in shown
    ]

    tail = [
        tr("report.index_title", count=len(indexed)),
        render_tally(report, tr),
        tr("report.index_hint"),
    ]
    return rows, tail, (page, pages)


def render_pager(page: int, pages: int, tr: Translator, command_for: Callable[[int], str]) -> str:
    """The plain-text pager line, for a reader who cannot click on it.

    The chat renderer draws the same numbers as two buttons; this is the form the console gets,
    which has no clicks at all — ``[上一页]`` with nothing behind it would be decoration. The
    commands are spelled out instead, which is the only way a console reader can turn the page.

    ``command_for`` builds the command for a target page, so the filter the listing was made
    with is carried along: paging must not silently widen "the ones waiting to be installed"
    into "everything".
    """
    parts = [tr("command.list.page", page=page, pages=pages)]
    if page > 1:
        parts.append(tr("command.list.prev_command", command=command_for(page - 1)))
    if page < pages:
        parts.append(tr("command.list.next_command", command=command_for(page + 1)))
    return "  ".join(parts)


def action_row(
    entry: UpdateEntry,
    number: Optional[int],
    prefix: str,
    tr: Translator,
    delete_allowed: bool = True,
) -> Optional[DetailRow]:
    """The one action this mod affords right now, as a clickable row, or ``None``.

    ``update_available`` offers the fetch and ``awaiting_install`` offers the install, never
    both: the second is what the first produces, and offering a step the mod is not ready for
    is how a command comes to answer with an error. Anything else has nothing to offer, and a
    button that would only produce an error is worse than no button.

    ``delete_allowed`` — ``cleanup.allow_delete``, read by the caller — is the one case where
    the button is replaced rather than dropped: the admin learns the option exists and that it
    is off, which a missing button cannot say. The value is the caller's to supply because this
    module has no config; the *sentence* belongs here, next to the status it is about.

    The command is spelled out rather than the number alone, so it can be typed by hand if the
    chat log has scrolled past the row — and so ``prefix`` is the alias the reader actually
    used. It lives here rather than beside the command tree because the decision it encodes is
    about the *status*, and the statuses are defined in this module.
    """
    if number is None:
        return None
    if entry.status == STATUS_UPDATE_AVAILABLE and entry.download_url and entry.download_sha1:
        return DetailRow(
            tr("detail.action_label"),
            tr("command.detail.download"),
            "",
            "{} download {}".format(prefix, number),
        )
    if entry.status == STATUS_AWAITING_INSTALL:
        return DetailRow(
            tr("detail.action_label"),
            tr("command.detail.install"),
            "",
            "{} install {}".format(prefix, number),
        )
    if entry.status == STATUS_OLD_BACKUP:
        if not delete_allowed:
            # No command and no url, so the row renders as plain text: an explanation rather
            # than a button that the very next keystroke would refuse.
            return DetailRow(
                tr("detail.action_label"), tr("detail.delete_locked"), "", ""
            )
        return DetailRow(
            tr("detail.action_label"),
            tr("command.detail.delete"),
            "",
            "{} delete {}".format(prefix, number),
        )
    return None


def entry_detail_rows(
    entry: UpdateEntry, tr: Translator, action: Optional[DetailRow] = None
) -> List[DetailRow]:
    """One mod's detail view, row by row.

    The project page is the only link left. There used to be a second one — a "click to
    download" that opened the file's url in the browser — and it sat directly above the button
    that asks the plugin to fetch the same file. Two ways to download, one of them bypassing
    the hash check the plugin performs, and the reader has to decide between them every time.
    The button stayed and the raw link went, taking the mod's ``download_url`` out of the chat
    entirely: it is still in the JSON report for anything that wants to fetch a file itself.

    ``action`` — built by :func:`action_row` — is placed where that link used to be, which is
    the spot a reader already looks at for "how do I get this".

    The notes are whole sentences, so they carry no label; every other row is ``label: value``.
    """
    rows: List[DetailRow] = [DetailRow("", entry.name)]

    rows.append(DetailRow(tr("detail.status_label"), tr(_status_key(entry.status))))
    if entry.status == STATUS_OLD_BACKUP:
        # No versions to compare: a backup is a file this plugin set aside, and the two facts
        # that decide what happens to it are how big it is and how long it has been sitting
        # there. ``restored_name`` is shown because it is the answer to "a backup of what".
        rows.append(DetailRow(tr("detail.file_label"), entry.file_name))
        rows.append(DetailRow(tr("detail.backup_of_label"), restored_name(entry.file_name)))
        rows.append(DetailRow(tr("detail.size_label"), format_size(entry.size_bytes)))
        rows.append(DetailRow(tr("detail.age_label"), tr("detail.age_days", days=entry.age_days)))
        if action is not None:
            rows.append(action)
        for key, args in entry.notes:
            rows.append(DetailRow("", tr(key, **args)))
        return rows
    if entry.latest_version and entry.latest_version != entry.local_version:
        rows.append(DetailRow(tr("detail.version_label"),
                              tr("detail.version", local=entry.local_version or "?",
                                 latest=entry.latest_version)))
    elif entry.latest_version:
        # One version, no arrow. An arrow needs two different ends, and "1.0.0 -> 1.0.0" reads
        # like a change that did not happen — on the mods that are already current, which is
        # most of them. The value goes in as it is rather than through the catalogue for the
        # same reason the heading row prints the mod's name: it is data, not prose.
        rows.append(DetailRow(tr("detail.version_label"), entry.latest_version))
    elif entry.local_version:
        rows.append(DetailRow(tr("detail.version_label"),
                              tr("detail.local_version", local=entry.local_version)))

    if entry.project_url:
        rows.append(DetailRow(tr("detail.project_label"), tr("detail.open_page"),
                              entry.project_url))
    if action is not None:
        rows.append(action)

    for key, args in entry.notes:
        rows.append(DetailRow("", tr(key, **args)))
    if entry.error:
        rows.append(DetailRow("", tr("report.error_detail", error=entry.error)))
    return rows


def entry_from_backup(backup: Backup) -> UpdateEntry:
    """The entry for one ``.old`` file, as the listing and the delete command see it.

    ``name`` is the file name, not a mod's display name: for a backup the file name *is* the
    identity — it is what the admin types at ``!!muc delete``, and it is the only thing about
    the file that says which mod it came from. ``mod_id`` stays empty for the same reason: a
    backup has no metadata to read an id out of, and inventing one from the name would put a
    guess where a handle is expected.
    """
    return UpdateEntry(
        mod_id="",
        name=backup.file_name,
        file_name=backup.file_name,
        status=STATUS_OLD_BACKUP,
        size_bytes=int(backup.size_bytes),
        age_days=int(backup.age_days),
    )


def format_size(count: int) -> str:
    """Bytes as something a human reads at a glance.

    Lives here, beside the renderers that use it, rather than in the entry module: ``report``
    is the module the pure-rendering half of the plugin lives in, and the size of a backup has
    to be worded by the same function that words the size of a download.
    """
    if count >= 1024 * 1024:
        return "{:.1f} MB".format(count / (1024.0 * 1024.0))
    if count >= 1024:
        return "{:.0f} KB".format(count / 1024.0)
    return "{} B".format(count)


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
