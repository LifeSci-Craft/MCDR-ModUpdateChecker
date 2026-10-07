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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .serverinfo import ServerContext

__all__ = [
    "STATUS_UP_TO_DATE",
    "STATUS_UPDATE_AVAILABLE",
    "STATUS_LOCAL_AHEAD",
    "STATUS_NO_COMPATIBLE_BUILD",
    "STATUS_UNRESOLVED",
    "STATUS_ERROR",
    "STATUS_NOT_A_MOD",
    "STATUS_IGNORED",
    "ALL_STATUSES",
    "ACTIONABLE_STATUSES",
    "UPDATE_ENTRY_ORDER",
    "UpdateEntry",
    "Report",
    "render_summary",
    "render_full",
    "render_entry_line",
    "render_statuses",
    "entry_from_scan",
    "mc_mismatch_note",
]

STATUS_UP_TO_DATE = "up_to_date"
STATUS_UPDATE_AVAILABLE = "update_available"
STATUS_LOCAL_AHEAD = "local_ahead"
STATUS_NO_COMPATIBLE_BUILD = "no_compatible_build"
STATUS_UNRESOLVED = "unresolved"
STATUS_ERROR = "error"
STATUS_NOT_A_MOD = "not_a_mod"
STATUS_IGNORED = "ignored"

ALL_STATUSES: Tuple[str, ...] = (
    STATUS_UPDATE_AVAILABLE,
    STATUS_NO_COMPATIBLE_BUILD,
    STATUS_LOCAL_AHEAD,
    STATUS_UNRESOLVED,
    STATUS_ERROR,
    STATUS_NOT_A_MOD,
    STATUS_UP_TO_DATE,
    STATUS_IGNORED,
)

#: Statuses that warrant doing something. Everything else is information.
ACTIONABLE_STATUSES: Tuple[str, ...] = (
    STATUS_UPDATE_AVAILABLE,
    STATUS_NO_COMPATIBLE_BUILD,
)

#: Display order for a full listing: most urgent first, then the noise, then the good news.
UPDATE_ENTRY_ORDER: Dict[str, int] = {status: index for index, status in enumerate(ALL_STATUSES)}

Translator = Callable[..., str]


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


@dataclass
class Report:
    """Everything one check run found."""

    generated_at: str
    server: ServerContext
    mods_directory: str
    entries: List[UpdateEntry] = field(default_factory=list)
    total_jars: int = 0
    unidentified: List[Tuple[str, str]] = field(default_factory=list)
    disabled_jars: List[str] = field(default_factory=list)
    duplicate_ids: Dict[str, List[str]] = field(default_factory=dict)
    advisories: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    upstream_notes: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    duration_seconds: float = 0.0

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
    def blocked(self) -> List[UpdateEntry]:
        return self.by_status(STATUS_NO_COMPATIBLE_BUILD)

    def counts(self) -> Dict[str, int]:
        tally = {status: 0 for status in ALL_STATUSES}
        for entry in self.entries:
            tally[entry.status] = tally.get(entry.status, 0) + 1
        return tally

    @property
    def actionable_count(self) -> int:
        return len(self.updates) + len(self.blocked)

    @property
    def has_updates(self) -> bool:
        return bool(self.updates)

    def sorted_entries(self) -> List[UpdateEntry]:
        return sorted(self.entries, key=lambda entry: entry.sort_key)

    # -- serialisation -----------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "mods_directory": self.mods_directory,
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
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def _status_key(status: str) -> str:
    return "status." + status


def render_entry_line(entry: UpdateEntry, tr: Translator, verbose: bool) -> str:
    """One line: ``name  local -> latest  [status]``."""
    if entry.status == STATUS_UPDATE_AVAILABLE:
        core = tr(
            "line.update",
            name=entry.name,
            local=entry.local_version or "?",
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

    parts = [tr(_status_key(entry.status))]
    if verbose and entry.platform:
        parts.append(entry.platform)
    if verbose and entry.matched_by:
        parts.append(tr("matched_by." + entry.matched_by))
    if entry.project_url:
        parts.append(entry.project_url)
    if entry.download_url:
        parts.append(entry.download_url)
    return "{}  ({})".format(core, ", ".join(parts))


def render_summary(report: Report, tr: Translator, max_updates: int = 12) -> List[str]:
    """The short form: what needs attention, plus a tally. Used for the console notification."""
    lines: List[str] = []
    lines.append(
        tr("report.header", version=report.server.describe(), source=report.server.mc_version_source)
    )

    updates = report.updates
    if updates:
        lines.append(tr("report.updates_found", count=len(updates)))
        for entry in updates[:max_updates]:
            lines.append("  " + render_entry_line(entry, tr, verbose=False))
        if len(updates) > max_updates:
            lines.append(tr("report.and_more", count=len(updates) - max_updates))
    else:
        lines.append(tr("report.no_updates"))

    blocked = report.blocked
    if blocked:
        lines.append(tr("report.blocked_found", count=len(blocked)))
        for entry in blocked[:max_updates]:
            lines.append("  " + render_entry_line(entry, tr, verbose=False))

    tally = report.counts()
    lines.append(
        tr(
            "report.tally",
            total=len(report.entries),
            up_to_date=tally.get(STATUS_UP_TO_DATE, 0),
            update=tally.get(STATUS_UPDATE_AVAILABLE, 0),
            blocked=tally.get(STATUS_NO_COMPATIBLE_BUILD, 0),
            unresolved=tally.get(STATUS_UNRESOLVED, 0)
            + tally.get(STATUS_NOT_A_MOD, 0)
            + tally.get(STATUS_ERROR, 0),
        )
    )

    for key, args in report.upstream_notes:
        lines.append(tr(key, **args))
    for key, args in report.advisories:
        lines.append(tr(key, **args))
    return lines


def render_full(report: Report, tr: Translator) -> List[str]:
    """The long form: every mod, its notes, and the diagnostics. Used by the command."""
    lines = list(render_summary(report, tr, max_updates=0))

    lines.append(tr("report.all_mods"))
    for entry in report.sorted_entries():
        lines.append("  " + render_entry_line(entry, tr, verbose=True))
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


def render_statuses(tr: Translator) -> Sequence[Tuple[str, str]]:
    """``(status, human label)`` pairs, for help text."""
    return [(status, tr(_status_key(status))) for status in ALL_STATUSES]


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
