"""Turning "here are the jars" into "here is what needs updating".

The run happens in three stages, cheapest and most reliable first, and each stage only ever
deals with what the previous ones could not answer:

1. **Modrinth by SHA-1.** One or two batched requests identify every jar whose bytes exist
   on Modrinth and return the newest build for the configured loader and game version. This
   alone resolves the large majority of a Fabric server, and it needs no credentials.
2. **The admin's own map.** Jars the hashes did not resolve are looked up in
   ``project-map.json`` — a file the admin writes to say which project their own build
   belongs to. It runs before the name search because it is a statement rather than a guess,
   and it exists because a jar built from source or re-signed is otherwise unresolvable.
3. **Name search.** Some jars are built from source, re-signed, or simply old, so their bytes
   are not published anywhere. Matching the mod id against the project slug is a guess, so it
   is only accepted on an exact (normalised) slug or title match, and every entry resolved
   this way is labelled ``matched_by=name`` in the report. A wrong guess that leads an admin to
   overwrite a good jar is worse than an honest "unresolved".
4. **Advisories.** Duplicate mod ids, client-only mods sitting in a server folder, missing
   dependencies, and mods whose declared Minecraft range excludes the running version. None
   of these is an update, but all of them explain far more breakage than a stale jar does.

Jars that cannot be identified at all are still reported, as ``unresolved`` — which is a
useful answer in itself, since it means Modrinth has never seen those exact bytes.

A jar is identified in the report by its **file name**, not its mod id, because two jars of
the same mod in one folder is a real and common situation that would otherwise be
impossible to report.

One design note on freshness: the resolve cache stores only the *negative* answer — "this
hash could not be identified" — and never the "is there a newer version" answer. Both halves
of that are deliberate. The update answer is the whole point of the check and must be fresh
every run. And the positive identification is not worth caching: it comes from one batched
request for the entire folder, so remembering it would save a request that costs nothing,
while a stale positive would be a claim about bytes that may have been replaced since.
"""

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from .cleanup import list_backups
from .jsonfile import read_json, write_json
from .modrinth import ModrinthClient, ModrinthProject, ModrinthVersion
from .projectmap import ProjectMap
from .report import (
    MATCHED_BY_HASH,
    MATCHED_BY_MANUAL,
    MATCHED_BY_NAME,
    STATUS_ERROR,
    STATUS_IGNORED,
    STATUS_LOCAL_AHEAD,
    STATUS_NOT_A_MOD,
    STATUS_NO_COMPATIBLE_BUILD,
    STATUS_UNRESOLVED,
    STATUS_UP_TO_DATE,
    STATUS_UPDATE_AVAILABLE,
    Report,
    UpdateEntry,
    entry_from_backup,
    entry_from_scan,
    mc_mismatch_note,
)
from .report import normalise_name as report_normalise_name
from .scanner import ScanResult, ScannedMod, missing_dependencies
from .serverinfo import ServerContext
from .upstream import HttpClient, RateLimiter, UpstreamError
from .versioning import compare

__all__ = ["CheckOptions", "Checker", "ResolveCache", "USER_AGENT", "normalise_name"]

_LOGGER = logging.getLogger(__name__)

#: Modrinth requires a descriptive User-Agent and uses it to contact an abusive client.
USER_AGENT = "LifeSci-Craft/MCDR-ModUpdateChecker (+https://github.com/LifeSci-Craft/MCDR-ModUpdateChecker)"

#: Fallback per-mod query budget, so one misbehaving upstream cannot stall a whole run.
_MAX_WORKERS = 8



def _pool_size(configured: int, count: int) -> int:
    """How many workers to use for ``count`` lookups.

    Capped at :data:`_MAX_WORKERS` so a large configured value cannot turn a check into a
    stampede against a public API, and at ``count`` so a three-mod server does not start eight
    threads to make three calls.
    """
    return max(1, min(_MAX_WORKERS, max(1, int(configured)), count))


def normalise_name(text: str) -> str:
    """Lowercase, strip everything that is not alphanumeric. Used for slug comparison.

    Defined in :mod:`~mod_update_checker.report` and re-exported here, because the report's
    handle lookup needs the same comparison and ``report`` cannot import ``checker``. Two
    copies would eventually disagree, and the disagreement would be a mod the admin excluded
    being looked up anyway — or a name that resolves in one command and not the next.
    """
    return report_normalise_name(text)


@dataclass
class CheckOptions:
    """Everything the checker needs, already validated by the config layer."""

    loader: str = "fabric"
    mc_version: Optional[str] = None
    include_beta: bool = False
    include_alpha: bool = False
    use_modrinth: bool = True
    modrinth_base: str = ""
    #: Mod ids, file names or file stems to leave alone entirely — no lookup, no report entry
    #: beyond ``ignored``. See ``_select_active``.
    ignored_mods: Sequence[str] = field(default_factory=list)
    timeout: float = 20.0
    retries: int = 3
    workers: int = 4
    requests_per_minute: int = 240
    use_cache: bool = True
    cache_ttl_hours: float = 24.0

    def modrinth_channels(self) -> Tuple[str, ...]:
        """Modrinth ``version_type`` values to accept, newest-stable-first."""
        channels = ["release"]
        if self.include_beta:
            channels.append("beta")
        if self.include_alpha:
            channels.append("alpha")
        return tuple(channels)


class ResolveCache:
    """``sha1 -> "this could not be identified"`` on disk, so a restart does not re-ask.

    What is cached is narrow, and the narrowness is the design rather than an oversight: only
    a **negative** identification is stored, and only by :meth:`Checker._identify_by_name` —
    the stage that had to spend a search plus a version lookup to prove a jar is not on the
    platform. That is the expensive answer, and it is the one that will still be the same
    answer tomorrow.

    Nothing about *updates* is cached anywhere, ever. "Is there a newer build" is the entire
    point of a check and has to be asked fresh every time; a remembered answer would turn the
    plugin into a machine for reporting yesterday's news. A positive identification is not
    cached either, for the cheaper reason that it arrives in one batched request for the whole
    folder — there is no cost to save, and a stale positive would be a claim about bytes that
    might since have been replaced.
    """

    VERSION = 1

    #: One lock for the whole process, and one lock per file path inside that table. The
    #: instances are the problem rather than the solution: the entry layer builds several (the
    #: check thread, ``!!muc`` commands, the status page) and each takes its own snapshot of the
    #: file at construction, so whoever saves last used to win and the other run's freshly
    #: learned negative answers were silently dropped — a search plus a version lookup paid
    #: again, within the TTL. :meth:`save` therefore merges into what is on disk under the file's
    #: lock and writes back only *this* instance's changes, the same discipline
    #: :class:`~mod_update_checker.downloads.DownloadLedger` already follows; its
    #: ``_LEDGER_LOCK`` is the pattern being copied here.
    _LOCKS: Dict[str, threading.RLock] = {}
    _LOCKS_GUARD = threading.Lock()

    @classmethod
    def _lock_for(cls, path: Path) -> threading.RLock:
        try:
            key = os.path.normcase(str(path.resolve()))
        except OSError:  # pragma: no cover - resolve() on a path that cannot be resolved
            key = os.path.normcase(os.path.abspath(str(path)))
        with cls._LOCKS_GUARD:
            lock = cls._LOCKS.get(key)
            if lock is None:
                lock = threading.RLock()
                cls._LOCKS[key] = lock
            return lock

    def __init__(
        self, path: Union[str, Path, None], ttl_hours: float, enabled: bool = True
    ) -> None:
        # Accepts ``str`` as well as ``Path``, and coerces once here rather than trusting
        # every caller. The two callers genuinely disagreed — the tests pass a ``Path``, the
        # plugin passes the result of ``os.path.join`` — and the cache only reached the
        # filesystem methods when enabled, so the mismatch survived a green test suite and
        # crashed on the first real check. A single coercion makes both forms correct.
        self.path: Optional[Path] = Path(path) if path is not None else None
        self.ttl_seconds = max(0.0, float(ttl_hours) * 3600.0)
        self.enabled = enabled and self.path is not None
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = self._load()
        #: Records this instance added or replaced since it was built. Only these are written
        #: back by :meth:`save`, so a save cannot revert what another instance learned in the
        #: meantime. A delete is recorded as an explicit ``None`` so that "removed here" is
        #: distinguishable from "not touched here".
        self._dirty: Dict[str, Any] = {}

    def _load(self) -> Dict[str, Any]:
        if not self.enabled or not self.path or not self.path.is_file():
            return {}
        data = read_json(self.path)
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            return {}
        records = data.get("records")
        if not isinstance(records, dict):
            return {}
        # Per-record validation, not just the top level. The file is hand-editable and this
        # plugin has always said so, so one malformed row is something to drop rather than
        # something to trip over on **every** run afterwards — and dropping it here means the
        # next save writes a file that no longer carries it.
        usable: Dict[str, Any] = {}
        for key, record in records.items():
            if isinstance(record, dict) and _cache_timestamp(record) is not None:
                usable[str(key)] = record
        return usable

    def get(self, sha1: str) -> Optional[Dict[str, Any]]:
        """A cached record, or ``None`` when absent or stale.

        A TTL of zero means "never expires" rather than "always stale": switching the cache
        off is what ``network.cache.enabled`` is for, and having two settings that both disable
        it would only make the config harder to reason about.
        """
        if not self.enabled or not sha1:
            return None
        with self._lock:
            record = self._data.get(sha1)
        if not isinstance(record, dict):
            return None
        recorded_at = _cache_timestamp(record)
        if recorded_at is None:
            # A record this code cannot read is treated as no record at all.
            return None
        if self.ttl_seconds and time.time() - recorded_at > self.ttl_seconds:
            return None
        return record

    def put(self, sha1: str, payload: Dict[str, Any]) -> None:
        if not self.enabled or not sha1:
            return
        record = dict(payload)
        record["at"] = time.time()
        with self._lock:
            self._data[sha1] = record
            self._dirty[sha1] = record

    def save(self) -> None:
        """Write this instance's new records back, merging with what is on disk.

        Read-modify-write rather than "write what I have": the file is shared by every instance
        in the process, and a whole-file write from an instance holding an older snapshot is how
        one run's answers get thrown away by the next one's.

        It writes even when this instance learned nothing, because that is what it did before and
        something depends on it: the cross-version matrix and the end-to-end test read
        ``resolve-cache.json`` back after a run to prove the cache is on disk and parses, and a
        run where every mod resolved from the batched lookup legitimately has no new records. An
        "always write" is also the honest reading of the option — ``network.cache.enabled`` says
        the cache is in use, so the file is the evidence of that.
        """
        if not self.enabled or not self.path:
            return
        try:
            with self._lock_for(self.path):
                payload = self._merged_payload()
                write_json(self.path, payload)
            with self._lock:
                self._dirty.clear()
        except OSError as error:
            _LOGGER.warning("could not write resolve cache %s: %s", self.path, error)

    def _merged_payload(self) -> Dict[str, Any]:
        """The file's records with this instance's changes layered on top."""
        merged: Dict[str, Any] = {}
        on_disk = read_json(self.path) if self.path else None
        if isinstance(on_disk, dict) and on_disk.get("version") == self.VERSION:
            records = on_disk.get("records")
            if isinstance(records, dict):
                for key, record in records.items():
                    if isinstance(record, dict) and _cache_timestamp(record) is not None:
                        merged[str(key)] = record
        with self._lock:
            for key, record in self._dirty.items():
                if record is None:
                    merged.pop(key, None)
                else:
                    merged[key] = record
        return {"version": self.VERSION, "records": merged}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _cache_timestamp(record: Dict[str, Any]) -> Optional[float]:
    """When a cached record was written, or ``None`` when the value is not a usable time.

    ``or 0`` alone is not enough: ``float("soon")``, ``float([])`` and ``float({})`` all raise,
    and this is read once per unidentified mod on every run — so one hand-edit used to make
    *every* check fail, with no way back except deleting the file. A record whose timestamp
    cannot be read is treated as a record that is not there.
    """
    try:
        return float(record.get("at") or 0)
    except (TypeError, ValueError):
        return None


class Checker:
    """Runs one check and returns a :class:`Report`.

    Not reusable across runs: it owns an HTTP session, which is closed by :meth:`close`.
    """

    def __init__(self, options: CheckOptions, logger: Optional[Any] = None) -> None:
        self.options = options
        self.logger = logger or _LOGGER
        self._http: Optional[HttpClient] = None
        self._modrinth: Optional[ModrinthClient] = None
        self._cache = ResolveCache(None, options.cache_ttl_hours, enabled=False)
        self._map = ProjectMap(None)
        #: File names whose Modrinth project declares itself server-incompatible. Collected
        #: while stage 1 has the project data in hand and turned into one advisory at the end,
        #: so every advisory in a report is assembled in one place.
        self._server_unsupported: List[str] = []
        #: Set when a request failed in a way that says "the platform as a whole is not
        #: answering", as opposed to one project being missing or one call being unlucky. Every
        #: later stage reads it and gives up rather than repeating the same dead request once per
        #: mod — see :meth:`_stage_modrinth`.
        self._upstream_down = False

    # -- lifecycle ---------------------------------------------------------------------

    def _setup(
        self,
        cache_path: Union[str, Path, None],
        map_path: Union[str, Path, None] = None,
    ) -> None:
        from .modrinth import DEFAULT_BASE_URL as MR_BASE

        limiter = RateLimiter(self.options.requests_per_minute)
        self._http = HttpClient(
            user_agent=USER_AGENT,
            timeout=self.options.timeout,
            retries=self.options.retries,
            rate_limiter=limiter,
            logger=self.logger,
        )
        self._modrinth = ModrinthClient(
            self._http, base_url=self.options.modrinth_base or MR_BASE
        )
        self._cache = ResolveCache(
            cache_path, self.options.cache_ttl_hours, enabled=self.options.use_cache
        )
        self._map = ProjectMap(map_path, logger=self.logger)
        self._server_unsupported = []
        self._upstream_down = False
        # Said out loud rather than swallowed: an unreadable map is a silent loss of the
        # admin's own instructions, and "I wrote that file and nothing changed" is exactly
        # the report a bare ``debug`` line would produce.
        if self._map.path is not None and self._map.error:
            self.logger.warning(
                "ignoring %s: %s", self._map.path, self._map.error
            )
        elif self._map.rejected:
            self.logger.warning(
                "%s: %d entr(ies) were unusable and ignored",
                self._map.path,
                self._map.rejected,
            )

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    # -- entry point -------------------------------------------------------------------

    def run(
        self,
        scan: ScanResult,
        server: ServerContext,
        cache_path: Union[str, Path, None] = None,
        map_path: Union[str, Path, None] = None,
    ) -> Report:
        started = time.monotonic()
        self._setup(cache_path, map_path)

        report = Report(
            generated_at=_now_iso(),
            server=server,
            mods_directory=scan.directory,
            total_jars=len(scan.mods),
            disabled_jars=list(scan.disabled),
        )
        report.duplicate_ids = {
            mod_id: sorted(mod.file_name for mod in mods)
            for mod_id, mods in scan.duplicate_ids().items()
        }

        entries: Dict[str, UpdateEntry] = {}
        for mod in scan.mods:
            entries[mod.file_name] = entry_from_scan(mod)

        active = self._select_active(scan, entries)

        try:
            remaining = self._stage_modrinth(active, entries, report, server)
            if remaining and not self._upstream_down:
                # Before the name search, not after: this stage is the admin telling us the
                # answer, and a guess must not be allowed to pre-empt it.
                remaining = self._stage_manual_map(remaining, entries, server)
            if remaining and not self._upstream_down:
                self._stage_name_search(remaining, entries, report, server)
            # Advisories are collected **inside** the try, before ``close`` runs in the finally.
            # They are not decoration: the missing-dependency advisory asks the platform one more
            # question, and asking it after ``close()`` is using a client that has been torn
            # down. It happens to still work today because ``requests.Session.close`` can be used
            # again, but that is an implementation detail of the library, not a contract — and a
            # crash here would throw away a check that had otherwise fully succeeded.
            self._collect_advisories(scan, server, report, entries)
        finally:
            self._cache.save()
            self.close()

        # The ``.old`` backups an earlier install left behind. Listed after the mods rather than
        # mixed in with them: they are files this plugin created, not jars to identify, and
        # nothing here talks to the platform about them. They become entries so that the
        # listing's number, the handle lookup and ``list <状态>`` cover them without a second
        # mechanism — ``!!muc delete 7`` has to be as unambiguous as ``!!muc install 7``.
        for backup in list_backups(scan.directory):
            entry = entry_from_backup(backup)
            entries[entry.file_name] = entry

        report.entries = list(entries.values())
        report.unidentified = [
            (name, self._unidentified_reason(entry))
            for name, entry in entries.items()
            if entry.status in (STATUS_UNRESOLVED, STATUS_NOT_A_MOD, STATUS_ERROR)
        ]
        report.duration_seconds = time.monotonic() - started
        return report

    def _select_active(
        self, scan: ScanResult, entries: Dict[str, UpdateEntry]
    ) -> List[ScannedMod]:
        """Split the scanned jars into those worth looking up and those the admin excluded.

        An excluded jar is marked ``ignored`` and **never reaches a lookup stage**, which is the
        whole point of the setting: it is for mods the admin does not want updating, or wrote
        themselves. Leaving it in the queue and discarding the answer would still spend the
        request, still show up in the request budget, and still be one bad cache entry away
        from a spurious report.

        Three spellings are accepted — mod id, file name, and file name without ``.jar`` —
        because all three are what an admin actually has in front of them, and both sides are
        normalised (lowercased, non-alphanumerics stripped) so ``Fabric-API`` and ``fabricapi``
        are the same entry.
        """
        if not self.options.ignored_mods:
            return list(scan.mods)

        ignored = {normalise_name(value) for value in self.options.ignored_mods if value}
        active: List[ScannedMod] = []
        for mod in scan.mods:
            if (
                normalise_name(mod.mod_id) in ignored
                or normalise_name(mod.file_name) in ignored
                or normalise_name(Path(mod.file_name).stem) in ignored
            ):
                entries[mod.file_name].status = STATUS_IGNORED
                continue
            active.append(mod)
        return active

    # -- stage 1: Modrinth -----------------------------------------------------------

    def _stage_modrinth(
        self,
        mods: Sequence[ScannedMod],
        entries: Dict[str, UpdateEntry],
        report: Report,
        server: ServerContext,
    ) -> List[ScannedMod]:
        """Identify by SHA-1 and find the newest compatible build. Returns the leftovers."""
        assert self._modrinth is not None
        if not self.options.use_modrinth:
            report.upstream_notes.append(("note.modrinth_disabled_by_config", {}))
            return list(mods)

        hashes = [mod.sha1 for mod in mods if mod.sha1]
        if not hashes:
            return list(mods)

        # 1a. What are these files?
        try:
            known = self._modrinth.versions_from_hashes(hashes)
        except UpstreamError as error:
            report.upstream_notes.append(
                ("note.modrinth_unavailable", {"error": str(error)})
            )
            # The platform as a whole is not answering. Falling through to the name-search stage
            # would send one search per mod — each with its own retries and its own timeout —
            # against a service that has just told us it is unavailable: twelve mods become
            # forty-eight requests over twelve seconds, and two hundred mods become eight hundred
            # requests over half an hour of held check lock. Nothing is learned by any of them
            # either; every one of them fails the same way. The refusal is remembered instead,
            # and each entry is marked "no verdict" below.
            self._upstream_down = True
            for mod in mods:
                entry = entries.get(mod.file_name)
                if entry is not None:
                    _mark_undetermined(entry, "modrinth")
            return []

        local: Dict[str, Tuple[ScannedMod, ModrinthVersion]] = {}
        for mod in mods:
            version = known.get(mod.sha1)
            if version is not None:
                local[mod.file_name] = (mod, version)

        # 1b and 1c. Both need nothing but the identities from 1a, and neither needs the
        #     other, so they share one round trip instead of queueing behind each other.
        #
        #     A failure in 1b is tracked separately from "the answer was empty", and that
        #     distinction is load-bearing. If the batched call fails, every hash is absent from
        #     the result, and treating absence as an answer would make the plugin state, about
        #     every single mod, that its project publishes no build for this loader. That is a
        #     confident false claim produced by a transient 503. 1c has no such trap — an empty
        #     title map just means the report shows file names instead of pretty ones — but it
        #     is fetched on the same round trip because there is no reason to pay for two.
        #
        #     The two failures are collected as values rather than appended to the report from
        #     inside the workers: two threads appending to one list would be a race on ordering
        #     that the report's own tests could not see.
        latest_by_hash: Dict[str, ModrinthVersion] = {}
        projects: Dict[str, ModrinthProject] = {}
        latest_failed = False
        if local:
            game_versions = [server.mc_version] if server.mc_version else []
            modrinth = self._modrinth

            def ask_for_latest() -> Any:
                try:
                    return (
                        modrinth.latest_from_hashes(
                            [mod.sha1 for mod, _ in local.values()],
                            loaders=[self.options.loader],
                            game_versions=game_versions,
                        ),
                        None,
                    )
                except UpstreamError as error:
                    return {}, error

            def ask_for_projects() -> Any:
                try:
                    return modrinth.projects(
                        [version.project_id for _, version in local.values()]
                    ), None
                except UpstreamError as error:
                    return {}, error

            with ThreadPoolExecutor(max_workers=2) as pool:
                latest_job = pool.submit(ask_for_latest)
                projects_job = pool.submit(ask_for_projects)
                latest_by_hash, latest_error = latest_job.result()
                projects, projects_error = projects_job.result()

            latest_failed = latest_error is not None
            for error in (latest_error, projects_error):
                if error is not None:
                    report.upstream_notes.append(
                        ("note.modrinth_unavailable", {"error": str(error)})
                    )

        # 1d. A hash absent from step 1b means the project publishes nothing for this
        #     loader/game-version pair, but the endpoint cannot say which of the two is
        #     missing. One extra per-project question tells them apart, and the two cases
        #     deserve different advice, so it is asked — but only when there is an answer to
        #     act on. If the batched call itself failed, ``_apply_modrinth`` reports "no verdict"
        #     for every one of these mods and never looks at this data, so asking for it would
        #     be N requests (with their retries and rate-limit grants) thrown away, aimed at a
        #     platform that has just refused us.
        if latest_failed:
            context: Dict[str, List[ModrinthVersion]] = {}
            context_failures: Set[str] = set()
        else:
            answered = set(latest_by_hash.keys())
            unanswered_projects = sorted(
                {
                    version.project_id
                    for mod, version in local.values()
                    if version.project_id and mod.sha1 not in answered
                }
            )
            context, context_failures = self._loader_versions_for(unanswered_projects)

            # The batched "newest build" endpoint has no channel filter, so it can hand back a
            # beta for a project whose newest *stable* build is older. Those projects need their
            # version list to answer at all, and they are not in the set above — the batch did
            # answer for them, it just answered something ``check.include_beta`` excludes. Asked
            # in one batch, before the loop, so the count stays a constant instead of one request
            # per affected mod.
            channels = self.options.modrinth_channels()
            needs_channel = sorted(
                {
                    latest_by_hash[mod.sha1].project_id
                    for mod, _version in local.values()
                    if mod.sha1 in latest_by_hash
                    and not _in_channels(latest_by_hash[mod.sha1], channels)
                    and latest_by_hash[mod.sha1].project_id not in context
                }
            )
            if needs_channel:
                extra, extra_failures = self._loader_versions_for(needs_channel)
                context = dict(context, **extra)
                context_failures = context_failures | extra_failures

        leftover: List[ScannedMod] = []
        for mod in mods:
            pair = local.get(mod.file_name)
            if pair is None:
                leftover.append(mod)
                continue
            _, local_version = pair
            entry = entries[mod.file_name]
            project = projects.get(local_version.project_id)
            self._apply_modrinth(
                mod,
                entry,
                local_version,
                latest_by_hash,
                project,
                context,
                context_failures,
                latest_failed,
                server,
            )
        return leftover

    def _loader_versions_for(
        self, project_ids: Sequence[str]
    ) -> Tuple[Dict[str, List[ModrinthVersion]], Set[str]]:
        """Per-project version list filtered by loader only, plus the ids that failed.

        The batched endpoint cannot say whether it found nothing because of the loader or
        because of the game version, so those projects are asked about individually. A project
        whose lookup *fails* is reported in the failure set rather than as an empty list —
        otherwise the caller would tell the admin the project has no build for this loader,
        which is not what happened.

        Fetching these concurrently matters: a hundred mods that all predate the current
        Minecraft version produce a hundred identifiers here, and doing them one at a time
        would turn a two-second check into a two-minute one.
        """
        assert self._modrinth is not None
        if not project_ids:
            return {}, set()

        def fetch(project_id: str) -> Tuple[str, Optional[List[ModrinthVersion]]]:
            try:
                # ``loaders`` only, deliberately: the game-version filter stays off because the
                # answer is *used* to tell "no build for this loader" from "builds exist, but
                # none for your game version" — and the second message names the newest build and
                # what it targets. Filtering by game version here would make that list empty and
                # the message wrong. The size of an unfiltered answer is bounded by
                # ``HttpClient.json_max_bytes`` instead, which is where a body limit belongs.
                return project_id, self._modrinth.project_versions(  # type: ignore[union-attr]
                    project_id, loaders=[self.options.loader]
                )
            except UpstreamError as error:
                self.logger.debug("project %s lookup failed: %s", project_id, error)
                return project_id, None

        workers = _pool_size(self.options.workers, len(project_ids))
        versions: Dict[str, List[ModrinthVersion]] = {}
        failures: Set[str] = set()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for project_id, found in pool.map(fetch, project_ids):
                if found is None:
                    failures.add(project_id)
                else:
                    versions[project_id] = found
        return versions, failures

    def _apply_modrinth(
        self,
        mod: ScannedMod,
        entry: UpdateEntry,
        local_version: ModrinthVersion,
        latest_by_hash: Dict[str, ModrinthVersion],
        project: Optional[ModrinthProject],
        context: Dict[str, List[ModrinthVersion]],
        context_failures: Set[str],
        latest_failed: bool,
        server: ServerContext,
    ) -> None:
        entry.platform = "modrinth"
        entry.matched_by = MATCHED_BY_HASH
        entry.project_url = project.page_url if project else ""
        if project is not None:
            _prefer_name(entry, project.title)
            # Modrinth's own "this does not work on a server" flag. Collected here because
            # this is the only place the project record is in hand, and it costs nothing
            # extra: the batch that fetched every title fetched this in the same response.
            # It catches mods whose own jar declares no ``environment`` at all, which is the
            # half the scanner-side check cannot see.
            if project.server_side == "unsupported":
                self._server_unsupported.append(entry.file_name)

        # Keyed by the hash we sent, which is the local file's SHA-1.
        latest = latest_by_hash.get(mod.sha1)
        if not entry.local_version:
            # The jar's own metadata had no usable version (or the jar carries no metadata
            # and only the hash gave it away). Modrinth knows exactly what this file is, so
            # use that rather than reporting an empty version.
            entry.local_version = local_version.version_number

        if latest is None:
            if latest_failed:
                _mark_undetermined(entry, "modrinth")
                return
            self._mark_no_compatible_build(
                entry, local_version, context, context_failures, server
            )
            return

        channels = self.options.modrinth_channels()
        if not _in_channels(latest, channels):
            # Only versions already fetched for this project are re-examined; the lookup above
            # made sure that list exists for exactly the projects this branch can be reached for,
            # and a channel filter must never cost one request per mod inside the loop.
            chosen = _pick_channel(context.get(local_version.project_id) or [], channels)
            if chosen is None:
                # Nothing in the accepted channels: say "no compatible build" rather than
                # recommending the pre-release the admin explicitly left out. The wording of
                # ``_mark_no_compatible_build`` already tells them a build exists that does not
                # fit, which is exactly the situation.
                self._mark_no_compatible_build(
                    entry, local_version, context, context_failures, server
                )
                return
            latest = chosen

        entry.latest_version = latest.version_number
        entry.release_channel = latest.version_type
        entry.released_at = latest.date_published
        _record_download(entry, latest)

        if local_version.id == latest.id or (
            mod.sha1 and mod.sha1 in latest.hashes()
        ):
            entry.status = STATUS_UP_TO_DATE
            self._note_spelling_difference(entry, local_version)
            return

        if local_version.version_number == latest.version_number:
            entry.status = STATUS_UP_TO_DATE
            entry.add_note("note.same_version_newer_build")
            return

        verdict = compare(entry.local_version, latest.version_number)
        if verdict < 0:
            entry.status = STATUS_UPDATE_AVAILABLE
        elif verdict > 0:
            entry.status = STATUS_LOCAL_AHEAD
        else:
            entry.status = STATUS_UP_TO_DATE
            entry.add_note("note.same_version_newer_build")

    @staticmethod
    def _note_spelling_difference(entry: UpdateEntry, local_version: ModrinthVersion) -> None:
        """Explain the jar's own version string versus Modrinth's spelling of the same build.

        The file's ``fabric.mod.json`` and Modrinth's ``version_number`` are two independent
        labels for one release — Lithium's jar says ``0.26.2+mc26.3`` while Modrinth says
        ``mc26.3-0.26.2-fabric``. The verdict is right either way, because it was decided on
        the file's identity, but a report that shows both spellings with no explanation reads
        like a discrepancy. One line removes the doubt.
        """
        if not entry.local_version or not local_version.version_number:
            return
        if entry.local_version == local_version.version_number:
            return
        entry.add_note(
            "note.same_build_other_spelling",
            local=entry.local_version,
            upstream=local_version.version_number,
        )

    def _mark_no_compatible_build(
        self,
        entry: UpdateEntry,
        local_version: ModrinthVersion,
        context: Dict[str, List[ModrinthVersion]],
        context_failures: Set[str],
        server: ServerContext,
    ) -> None:
        """The project exists, but nothing it publishes fits this server.

        Three distinct outcomes live behind this one batched "no match": the per-project
        follow-up could not be made at all, the project has no build for this loader, or it
        has builds for the loader but none for this game version. Only the last two are
        statements about the project, so a failed lookup must not be dressed up as one.
        """
        if local_version.project_id in context_failures:
            _mark_undetermined(entry, "modrinth")
            return

        versions = context.get(local_version.project_id) or []
        if not versions:
            entry.status = STATUS_NO_COMPATIBLE_BUILD
            entry.add_note("note.no_build_for_loader", loader=self.options.loader)
            return

        channels = self.options.modrinth_channels()
        in_channel = [
            version for version in versions if _in_channels(version, channels)
        ]
        entry.status = STATUS_NO_COMPATIBLE_BUILD
        if not in_channel:
            # Nothing in the accepted channels. Naming the version that *was* found keeps the
            # report answerable ("no release build, only this beta"), and ``release_channel`` is
            # what tells the reader which of the two situations this is.
            newest = versions[0]
            entry.latest_version = newest.version_number
            entry.released_at = newest.date_published
            entry.release_channel = newest.version_type
            entry.add_note(
                "note.prerelease_fallback",
                channel=versions[0].version_type or "release",
                setting=", ".join(channels),
            )
            return

        newest = in_channel[0]
        entry.latest_version = newest.version_number
        entry.released_at = newest.date_published
        entry.release_channel = newest.version_type
        entry.add_note(
            "note.no_build_for_game_version",
            version=server.mc_version or "?",
            newest=newest.version_number,
            targets=", ".join(newest.game_versions[-6:]) or "?",
        )

    # -- stage 2: the admin's own map ---------------------------------------------------

    def _stage_manual_map(
        self,
        mods: Sequence[ScannedMod],
        entries: Dict[str, UpdateEntry],
        server: ServerContext,
    ) -> List[ScannedMod]:
        """Identify what the admin has already identified. Returns the leftovers.

        This is the answer to the plugin's own documented limitation: a jar built from source,
        forked, or re-signed has bytes Modrinth has never seen and a mod id that may match no
        slug, so neither the hash lookup nor the name search can place it. The admin knows
        what it is. ``project-map.json`` is where they say so.

        It sits between the two automatic stages rather than after both, because it is a
        statement while the name search is a guess: letting the guess run first would mean an
        admin's explicit mapping could be silently overridden by an exact-slug coincidence.

        A mod with no metadata is skipped, exactly as the name search skips it — there would
        be no local version to compare against, so the entry could only ever say "this is
        project X" without saying whether X is current, which is not worth a request.
        """
        if not self._map.loaded:
            return list(mods)
        assert self._modrinth is not None

        leftover: List[ScannedMod] = []
        for mod in mods:
            entry = entries[mod.file_name]
            if entry.status in (STATUS_IGNORED, STATUS_ERROR) or not mod.identified:
                leftover.append(mod)
                continue
            target = self._map.lookup(sha1=mod.sha1, mod_id=mod.mod_id)
            if not target:
                leftover.append(mod)
                continue
            # ``False`` means the question could not be asked. Hand it back to the name
            # search rather than reporting a conclusion — a failure is not an answer, and a
            # different endpoint might still work.
            if not self._apply_manual(entry, target, server):
                leftover.append(mod)
        return leftover

    def _apply_manual(
        self, entry: UpdateEntry, target: str, server: ServerContext
    ) -> bool:
        """Resolve one entry against the project the admin named. ``False`` = could not ask.

        A reference that does not exist upstream is reported rather than quietly skipped. The
        admin wrote it, so either it is a typo or the project is gone, and both are things
        they need told — falling through to the name search would hide a broken mapping behind
        a guess that happened to work.
        """
        assert self._modrinth is not None
        try:
            project = self._modrinth.project(target)
        except UpstreamError as error:
            self.logger.debug("project map lookup of %s failed: %s", target, error)
            return False

        if project is None:
            entry.status = STATUS_UNRESOLVED
            entry.add_note("note.manual_map_unknown_project", project=target)
            return True

        try:
            versions = self._modrinth.project_versions(
                project.id, loaders=[self.options.loader]
            )
        except UpstreamError as error:
            self.logger.debug("project map versions for %s failed: %s", target, error)
            return False

        self._settle_by_versions(
            entry,
            versions,
            server,
            project_url=project.page_url,
            title=project.title,
            matched_by=MATCHED_BY_MANUAL,
            note_key="note.matched_by_manual",
            note_args={"project": target},
        )
        return True

    # -- stage 3: name search ----------------------------------------------------------

    def _stage_name_search(
        self,
        mods: Sequence[ScannedMod],
        entries: Dict[str, UpdateEntry],
        report: Report,
        server: ServerContext,
    ) -> None:
        """Last resort: match the mod id against a project slug, exactly.

        A near-match is deliberately *not* accepted. Reporting "Fabric API has an update" for
        a jar that is actually something else would have an admin download the wrong mod,
        which is strictly worse than the report saying it could not tell.

        This is the one stage whose failures are worth remembering: proving that Modrinth does
        not know a jar costs a search plus a version lookup, and the answer will not have
        changed by tomorrow. So a negative verdict is cached, and the TTL is what bounds how
        long a newly published mod stays invisible.

        Only an *established* negative verdict is cached. If the search could not be made at
        all, "Modrinth does not know this jar" is not something that was learned, and caching
        it would hide the mod from every check for the whole TTL — a transient outage promoted
        into a day-long blind spot.

        The lookups run concurrently, for the same reason the per-project follow-up in stage 1
        does: each one is two latency-bound round trips and the count is unbounded. A server
        whose mods were built from source, forked, or re-signed reaches this stage with all of
        them, and a hundred mods asked one at a time is a check measured in minutes rather
        than seconds. Nothing is decided here, so there is nothing for the ordering to carry:
        every mod owns its own entry, and the resolve cache takes a lock around its own state.

        A whole-platform outage never reaches this stage at all: :meth:`_stage_modrinth` records
        it and the run skips the per-mod stages, so the request count stays a constant instead of
        growing with the folder. What is left here is a failure in the *middle* of the stage —
        the platform answering, then stopping — which is bounded by the mod count and by the
        retry budget of each call. Stopping early on the first few failures was considered and
        left out deliberately: "the search failed" is also what a single unfindable mod looks
        like, and a breaker that trips on three of those would mark the remaining mods
        ``error`` for a reason that is not true of them. Distinguishing the two needs a signal
        the search endpoint does not currently give (a status code rather than a raised error),
        and that belongs with the entry layer rather than here.
        """
        # Everything that needs no network is decided first, serially, so the pool is only
        # ever handed work that has to happen.
        queue: List[ScannedMod] = []
        for mod in mods:
            entry = entries[mod.file_name]
            if entry.status in (STATUS_IGNORED, STATUS_ERROR):
                continue
            if not mod.identified:
                # A valid jar with no mod metadata that no hash matched stays "not a mod".
                # Searching for it by file name would only ever produce a false positive.
                continue

            cached = self._cache.get(mod.sha1) if (mod.sha1 and self.options.use_modrinth) else None
            if cached is not None and cached.get("resolved") is False:
                entry.status = STATUS_UNRESOLVED
                entry.add_note("note.cached_unresolved")
                continue

            queue.append(mod)

        if not queue:
            return

        workers = _pool_size(self.options.workers, len(queue))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # ``list`` so the pool is drained: an exception raised by a worker has to reach the
            # caller, and a lazily consumed ``map`` would swallow it until the iterator is read.
            list(pool.map(lambda mod: self._identify_by_name(mod, entries[mod.file_name], server),
                          queue))

    def _identify_by_name(
        self, mod: ScannedMod, entry: UpdateEntry, server: ServerContext
    ) -> None:
        """Try to tie one leftover mod to a project by name, and fill in its entry.

        Written to be safe to run for several mods at once — each call owns exactly one entry,
        and the only shared state it touches is the resolve cache, which locks.
        """
        query = mod.mod_id or Path(mod.file_name).stem
        if not query:
            entry.status = STATUS_UNRESOLVED
            return

        asked_modrinth = bool(self.options.use_modrinth)
        by_modrinth = (
            self._match_on_modrinth(mod, entry, query, server) if asked_modrinth else False
        )
        if by_modrinth:
            return

        entry.status = STATUS_UNRESOLVED
        if by_modrinth is None:
            # Asked, and the answer did not come back. Nothing was learned, so nothing may be
            # cached: a transient outage promoted into a day-long blind spot is the failure this
            # branch exists to avoid.
            entry.add_note("note.search_incomplete")
        elif not asked_modrinth:
            # Not asked at all — the platform is switched off in the config. Also nothing
            # learned, so also nothing cached. Writing ``resolved: false`` here is what used to
            # make every one of these mods stay "unresolved" for a whole TTL after the admin
            # turned Modrinth back on, with a note claiming the platforms did not know the jar.
            entry.add_note("note.modrinth_disabled_by_config")
        elif mod.sha1:
            # Both platforms were asked and neither knew it. That is a fact worth the disk.
            self._cache.put(mod.sha1, {"resolved": False})

    def _match_on_modrinth(
        self, mod: ScannedMod, entry: UpdateEntry, query: str, server: ServerContext
    ) -> Optional[bool]:
        """``True`` resolved, ``False`` asked and not found, ``None`` could not ask."""
        assert self._modrinth is not None
        try:
            hits = self._modrinth.search(query, loaders=[self.options.loader], limit=8)
        except UpstreamError as error:
            self.logger.debug("modrinth search for %s failed: %s", query, error)
            return None

        wanted = normalise_name(query)
        accepted = [
            hit
            for hit in hits
            if normalise_name(hit.slug) == wanted or normalise_name(hit.title) == wanted
        ]
        if not accepted:
            return False

        hit = accepted[0]
        try:
            versions = self._modrinth.project_versions(
                hit.project_id, loaders=[self.options.loader]
            )
        except UpstreamError as error:
            self.logger.debug("modrinth versions for %s failed: %s", hit.slug, error)
            return None

        self._settle_by_versions(
            entry,
            versions,
            server,
            project_url=hit.page_url,
            title=hit.title,
            matched_by=MATCHED_BY_NAME,
            note_key="note.matched_by_name",
            note_args={"slug": hit.slug},
        )
        return True

    def _settle_by_versions(
        self,
        entry: UpdateEntry,
        versions: Sequence[ModrinthVersion],
        server: ServerContext,
        project_url: str,
        title: str,
        matched_by: str,
        note_key: str,
        note_args: Dict[str, Any],
    ) -> None:
        """Fill in one entry from a project's version list, whoever produced that list.

        Shared by the name search and the admin's map because from here on the two are the
        same job: pick the newest build this server can run, compare the two version strings,
        and say plainly when there is no build to compare against. Two copies of that would
        eventually disagree about which of "no build for this loader" and "no build for this
        game version" applies — the pair of answers this stage exists to keep apart.
        """
        entry.platform = "modrinth"
        entry.matched_by = matched_by
        entry.project_url = project_url
        _prefer_name(entry, title)
        entry.add_note(note_key, **note_args)

        if not versions:
            entry.status = STATUS_NO_COMPATIBLE_BUILD
            entry.add_note("note.no_build_for_loader", loader=self.options.loader)
            return

        # Channel first, game version second, and in that order on purpose. A beta that also
        # targets the wrong game version would otherwise be "the newest compatible build" and
        # get recommended despite ``check.include_beta`` being off — the setting an admin
        # changes precisely because they do not want to be told about pre-releases.
        channels = self.options.modrinth_channels()
        in_channel = [version for version in versions if _in_channels(version, channels)]
        accepted = in_channel or list(versions)
        if not in_channel:
            # No version in the accepted channels: fall back rather than claim the project
            # publishes nothing, and say so, or the report would recommend a pre-release with no
            # explanation of why. Turning on ``check.include_beta`` is what removes the note.
            entry.add_note(
                "note.prerelease_fallback",
                channel=versions[0].version_type or "release",
                setting=", ".join(channels),
            )

        compatible = [
            version
            for version in accepted
            if not server.mc_version or server.mc_version in version.game_versions
        ]
        newest = (compatible or accepted)[0]
        entry.latest_version = newest.version_number
        entry.released_at = newest.date_published
        entry.release_channel = newest.version_type
        _record_download(entry, newest)

        if not compatible and server.mc_version:
            entry.status = STATUS_NO_COMPATIBLE_BUILD
            entry.add_note(
                "note.no_build_for_game_version",
                version=server.mc_version,
                newest=entry.latest_version,
                targets=", ".join((newest.game_versions or [])[-6:]) or "?",
            )
            return

        self._decide_by_string(entry)

    @staticmethod
    def _decide_by_string(entry: UpdateEntry) -> None:
        """Compare two version strings that came from different naming schemes."""
        if not entry.local_version or not entry.latest_version:
            entry.status = STATUS_UNRESOLVED
            entry.add_note("note.cannot_compare")
            return
        verdict = compare(entry.local_version, entry.latest_version)
        if verdict < 0:
            entry.status = STATUS_UPDATE_AVAILABLE
        elif verdict > 0:
            entry.status = STATUS_LOCAL_AHEAD
        else:
            entry.status = STATUS_UP_TO_DATE

    # -- advisories --------------------------------------------------------------------

    def _collect_advisories(
        self,
        scan: ScanResult,
        server: ServerContext,
        report: Report,
        entries: Dict[str, UpdateEntry],
    ) -> None:
        """Warnings that explain breakage better than any version comparison."""
        for mod in scan.mods:
            entry = entries.get(mod.file_name)
            if entry is None or entry.status == STATUS_IGNORED:
                continue
            note = mc_mismatch_note(mod, server.mc_version)
            if note is not None and note[0] not in {key for key, _ in entry.notes}:
                entry.add_note(note[0], **note[1])

        client_only = [
            mod.file_name for mod in scan.client_only() if mod.file_name in entries
        ]
        if client_only:
            report.advisories.append(
                ("advisory.client_only", {"count": len(client_only), "files": ", ".join(client_only[:8])})
            )
        # Modrinth's flag rather than the jar's, so the two lists overlap only where both
        # sources agree. Kept separate on purpose: an admin reading two lists can tell which
        # of them came from the mod author's own metadata.
        if self._server_unsupported:
            names = sorted(set(self._server_unsupported))
            report.advisories.append(
                ("advisory.server_side_unsupported",
                 {"count": len(names), "files": ", ".join(names[:8])})
            )
        missing = missing_dependencies(scan)
        still_missing, client_only = self._split_client_only_dependencies(missing)
        if still_missing:
            args: Dict[str, Any] = {
                "count": len(still_missing),
                "files": ", ".join(list(still_missing)[:8]),
            }
            if client_only:
                # Said out loud rather than silently dropped: the reader who saw this line
                # before would otherwise wonder why a dependency disappeared from it.
                report.advisories.append(
                    ("advisory.missing_dependencies_excluded",
                     dict(args, excluded=len(client_only)))
                )
            else:
                report.advisories.append(("advisory.missing_dependencies", args))
        for dep, files in client_only.items():
            for file_name in files:
                entry = entries.get(file_name)
                if entry is not None:
                    entry.add_note("note.client_only_dependency", dep=dep)
        if report.duplicate_ids:
            report.advisories.append(
                ("advisory.duplicates", {"count": len(report.duplicate_ids)})
            )
        if not server.mc_version:
            report.advisories.append(("advisory.unknown_mc_version", {}))
        elif server.mc_version_source == "mods":
            report.advisories.append(
                ("advisory.guessed_mc_version", {"version": server.mc_version})
            )

    def _split_client_only_dependencies(
        self, missing: Dict[str, List[str]]
    ) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
        """Split the missing dependencies by whether a server could run them at all.

        A mod's ``depends`` list is written for the mod, not for the side it is loaded on: a
        jar that runs on both sides can legitimately require a library that only ever exists on
        the client. Listing those as missing sends the admin looking for a jar that would do
        nothing if they found it, and Modrinth's own ``server_side: unsupported`` is exactly
        the statement that the project cannot run on a server.

        Returns ``(still_missing, client_only)``. The second is reported as a note on the mod
        that declared it rather than as an advisory line: there is nothing to do about it, so
        it explains an absence instead of asking for an action.

        Only the direct lookup is tried, and everything unresolved stays missing. A dependency
        whose id does not resolve to a project is one nothing here can say a side about, and
        this advisory has always erred towards over-reporting rather than going quiet.
        """
        if not missing or not self.options.use_modrinth:
            return missing, {}
        assert self._modrinth is not None
        try:
            projects = self._modrinth.projects(list(missing))
        except UpstreamError as error:
            self.logger.debug("could not classify missing dependencies: %s", error)
            return missing, {}

        by_name: Dict[str, ModrinthProject] = {}
        for project in projects.values():
            for name in (project.slug, project.id):
                if name:
                    by_name.setdefault(name.lower(), project)
                    by_name.setdefault(report_normalise_name(name), project)

        still_missing: Dict[str, List[str]] = {}
        client_only: Dict[str, List[str]] = {}
        for dep, files in missing.items():
            project = by_name.get(dep)
            if project is None:
                normalised = report_normalise_name(dep)
                project = by_name.get(normalised) if normalised else None
            if project is not None and project.server_side == "unsupported":
                client_only[dep] = files
            else:
                still_missing[dep] = files
        return still_missing, client_only

    @staticmethod
    def _unidentified_reason(entry: UpdateEntry) -> str:
        if entry.error:
            return entry.error
        if entry.status == STATUS_NOT_A_MOD:
            return "not-a-mod"
        return ""


def _in_channels(version: ModrinthVersion, channels: Sequence[str]) -> bool:
    """Whether a version is published in one of the accepted release channels.

    ``version_type`` is Modrinth's own field (``release``/``beta``/``alpha``) and is the same
    vocabulary as :meth:`CheckOptions.modrinth_channels`. An unrecognised value is accepted
    rather than refused: a channel this plugin has never heard of is not a reason to hide a
    build from the admin, and refusing it would look like "this project has no downloads".
    """
    kind = str(version.version_type or "release").strip().lower()
    if kind not in ("release", "beta", "alpha"):
        return True
    return kind in channels


def _pick_channel(
    versions: Sequence[ModrinthVersion], channels: Sequence[str]
) -> Optional[ModrinthVersion]:
    """The first version of ``versions`` that is in ``channels``, or ``None``.

    The list arrives newest-first from Modrinth, so "first" is "newest" and no second sort is
    needed. ``None`` is a real answer: it means the accepted channels hold nothing, which the
    caller must not confuse with "the project has no versions".
    """
    for version in versions:
        if _in_channels(version, channels):
            return version
    return None


def _record_download(entry: UpdateEntry, version: ModrinthVersion) -> None:
    """Copy the newest build's file identity onto the entry.

    All four fields are set together on purpose. The download feature verifies what it
    fetched against the hash recorded here, so a path that set the URL but not the hash would
    produce a download that could not be checked — and the safe response to an unverifiable
    jar is to refuse it, which would look like a bug rather than a missing field.
    """
    primary = version.primary_file
    if primary is None:
        return
    entry.download_url = primary.url
    entry.download_filename = primary.filename
    entry.download_sha1 = primary.sha1
    entry.download_sha512 = primary.sha512
    entry.download_size = primary.size


def _mark_undetermined(entry: UpdateEntry, platform: str) -> None:
    """Record that a jar could not be judged because an upstream call failed.

    Three outcomes that all look like "no answer" are kept apart on purpose:

    * ``unresolved`` — both platforms answered, neither recognised the jar. A fact.
    * ``error`` for an unreadable file — the jar itself is the problem.
    * this one — the jar is fine and the *answer* is missing.

    Collapsing this into ``unresolved`` would claim knowledge the plugin does not have, and
    collapsing it into ``no_compatible_build`` would state something false about the project:
    that it publishes no build for this loader, when in truth nobody was asked successfully.
    """
    entry.status = STATUS_ERROR
    if not entry.error:
        entry.error = "the {} lookup for this mod failed".format(platform)
    entry.add_note("note.upstream_failed")


def _prefer_name(entry: UpdateEntry, candidate: str) -> None:
    """Adopt an upstream project title only when the local name carries no information.

    A jar's own ``name`` field is usually the real display name and is better than anything
    fetched. When it is empty, or is just a restatement of the file name or the mod id, the
    upstream title is a genuine improvement.
    """
    if not candidate:
        return
    current = entry.name or ""
    if not current or normalise_name(current) in (
        normalise_name(entry.file_name),
        normalise_name(Path(entry.file_name).stem),
        normalise_name(entry.mod_id),
    ):
        entry.name = candidate
