"""Turning "here are the jars" into "here is what needs updating".

The run happens in four stages, cheapest and most reliable first, and each stage only ever
deals with what the previous ones could not answer:

1. **Modrinth by SHA-1.** One or two batched requests identify every jar whose bytes exist
   on Modrinth and return the newest build for the configured loader and game version. This
   alone resolves the large majority of a Fabric server.
2. **CurseForge by MurmurHash2 fingerprint.** The same idea for jars that came from
   CurseForge, which repackages releases into different bytes, so their SHA-1 is not on
   Modrinth. Needs an API key; skipped cleanly without one.
3. **Name search on both platforms.** Some jars are built from source, re-signed, or simply
   old. Matching the mod id against the project slug is a guess, so it is only accepted on an
   exact (normalised) slug or title match, and every entry resolved this way is labelled
   ``matched_by=name`` in the report. A wrong guess that leads an admin to overwrite a good
   jar is worse than an honest "unresolved".
4. **Advisories.** Duplicate mod ids, client-only mods sitting in a server folder, and mods
   whose declared Minecraft range excludes the running version. None of these is an update,
   but all three explain far more breakage than a stale jar does.

A jar is identified in the report by its **file name**, not its mod id, because two jars of
the same mod in one folder is a real and common situation that would otherwise be
impossible to report.

One design note on freshness: the resolve cache stores only *identifications* (which project
a hash belongs to), never the "is there a newer version" answer. Identification is stable
for the life of a file; the update answer is the whole point of the check and must be fresh
every run.
"""

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from .curseforge import CfFile, CurseForgeClient, RELEASE_TYPES
from .modrinth import ModrinthClient, ModrinthProject, ModrinthVersion
from .report import (
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
    entry_from_scan,
    mc_mismatch_note,
)
from .scanner import ScanResult, ScannedMod
from .serverinfo import ServerContext
from .upstream import HttpClient, RateLimiter, UpstreamError
from .versioning import compare

__all__ = ["CheckOptions", "Checker", "ResolveCache", "USER_AGENT", "normalise_name"]

_LOGGER = logging.getLogger(__name__)

#: Modrinth requires a descriptive User-Agent and uses it to contact an abusive client.
USER_AGENT = "Pau1am/MCDR-ModUpdateChecker (+https://github.com/Pau1am/MCDR-ModUpdateChecker)"

#: Fallback per-mod query budget, so one misbehaving upstream cannot stall a whole run.
_MAX_WORKERS = 8

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise_name(text: str) -> str:
    """Lowercase, strip everything that is not alphanumeric. Used for slug comparison."""
    return _NON_ALNUM.sub("", (text or "").lower())


@dataclass
class CheckOptions:
    """Everything the checker needs, already validated by the config layer."""

    loader: str = "fabric"
    mc_version: Optional[str] = None
    include_beta: bool = False
    include_alpha: bool = False
    use_modrinth: bool = True
    use_curseforge: bool = True
    modrinth_base: str = ""
    curseforge_base: str = ""
    curseforge_api_key: str = ""
    ignored_mods: Sequence[str] = field(default_factory=list)
    timeout: float = 20.0
    retries: int = 3
    workers: int = 4
    requests_per_minute: int = 240
    use_cache: bool = True
    cache_ttl_hours: float = 24.0

    @property
    def release_types(self) -> List[int]:
        """CurseForge ``FileReleaseType`` values to accept."""
        types = [RELEASE_TYPES["release"]]
        if self.include_beta:
            types.append(RELEASE_TYPES["beta"])
        if self.include_alpha:
            types.append(RELEASE_TYPES["alpha"])
        return types

    def modrinth_channels(self) -> Tuple[str, ...]:
        """Modrinth ``version_type`` values to accept, newest-stable-first."""
        channels = ["release"]
        if self.include_beta:
            channels.append("beta")
        if self.include_alpha:
            channels.append("alpha")
        return tuple(channels)


class ResolveCache:
    """``sha1 -> identification`` on disk, so a restart does not re-resolve everything.

    Only the *identity* of a file is cached, never whether an update exists — see the
    module docstring. Negative results are cached too, and that is the important part: a jar
    that is not on any platform costs the most requests to prove (a per-project fallback
    plus two searches), and it will still not be on any platform tomorrow.
    """

    VERSION = 1

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

    def _load(self) -> Dict[str, Any]:
        if not self.enabled or not self.path or not self.path.is_file():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("version") != self.VERSION:
            return {}
        records = data.get("records")
        return records if isinstance(records, dict) else {}

    def get(self, sha1: str) -> Optional[Dict[str, Any]]:
        """A cached record, or ``None`` when absent or stale.

        A TTL of zero means "never expires" rather than "always stale": switching the cache
        off is what ``use_resolve_cache`` is for, and having two settings that both disable
        it would only make the config harder to reason about.
        """
        if not self.enabled or not sha1:
            return None
        with self._lock:
            record = self._data.get(sha1)
        if not isinstance(record, dict):
            return None
        if self.ttl_seconds and time.time() - float(record.get("at") or 0) > self.ttl_seconds:
            return None
        return record

    def put(self, sha1: str, payload: Dict[str, Any]) -> None:
        if not self.enabled or not sha1:
            return
        record = dict(payload)
        record["at"] = time.time()
        with self._lock:
            self._data[sha1] = record

    def save(self) -> None:
        if not self.enabled or not self.path:
            return
        with self._lock:
            payload = {"version": self.VERSION, "records": self._data}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        except OSError as error:
            _LOGGER.warning("could not write resolve cache %s: %s", self.path, error)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Checker:
    """Runs one check and returns a :class:`Report`.

    Not reusable across runs: it owns an HTTP session, which is closed by :meth:`close`.
    """

    def __init__(self, options: CheckOptions, logger: Optional[Any] = None) -> None:
        self.options = options
        self.logger = logger or _LOGGER
        self._http: Optional[HttpClient] = None
        self._modrinth: Optional[ModrinthClient] = None
        self._curseforge: Optional[CurseForgeClient] = None
        self._cache = ResolveCache(None, options.cache_ttl_hours, enabled=False)

    # -- lifecycle ---------------------------------------------------------------------

    def _setup(self, cache_path: Union[str, Path, None]) -> None:
        from .curseforge import DEFAULT_BASE_URL as CF_BASE
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
        self._curseforge = CurseForgeClient(
            self._http,
            api_key=self.options.curseforge_api_key,
            base_url=self.options.curseforge_base or CF_BASE,
        )
        self._cache = ResolveCache(
            cache_path, self.options.cache_ttl_hours, enabled=self.options.use_cache
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
    ) -> Report:
        started = time.monotonic()
        self._setup(cache_path)

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

        if not self.options.use_curseforge:
            report.upstream_notes.append(("note.curseforge_disabled_by_config", {}))
        elif not (self.options.curseforge_api_key or "").strip():
            report.upstream_notes.append(("note.curseforge_no_key", {}))

        ignored = {normalise_name(value) for value in self.options.ignored_mods if value}
        active: List[ScannedMod] = []
        for mod in scan.mods:
            entry = entries[mod.file_name]
            if ignored and (
                normalise_name(mod.mod_id) in ignored
                or normalise_name(mod.file_name) in ignored
                or normalise_name(Path(mod.file_name).stem) in ignored
            ):
                entry.status = STATUS_IGNORED
                continue
            active.append(mod)

        try:
            remaining = self._stage_modrinth(active, entries, report, server)
            if remaining:
                remaining = self._stage_curseforge(remaining, entries, report, server)
            if remaining:
                self._stage_name_search(remaining, entries, report, server)
        finally:
            self._cache.save()
            self.close()

        self._collect_advisories(scan, server, report, entries)

        report.entries = list(entries.values())
        report.unidentified = [
            (name, self._unidentified_reason(entry))
            for name, entry in entries.items()
            if entry.status in (STATUS_UNRESOLVED, STATUS_NOT_A_MOD, STATUS_ERROR)
        ]
        report.duration_seconds = time.monotonic() - started
        return report

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
            return list(mods)

        local: Dict[str, Tuple[ScannedMod, ModrinthVersion]] = {}
        for mod in mods:
            version = known.get(mod.sha1)
            if version is not None:
                local[mod.file_name] = (mod, version)

        # 1b. The newest build matching loader (+ game version) for those same files.
        #
        # A failure here is tracked separately from "the answer was empty", and that
        # distinction is load-bearing. If the batched call fails, every hash is absent from
        # the result, and treating absence as an answer would make the plugin state, about
        # every single mod, that its project publishes no build for this loader. That is a
        # confident false claim produced by a transient 503.
        latest_by_hash: Dict[str, ModrinthVersion] = {}
        latest_failed = False
        if local:
            game_versions = [server.mc_version] if server.mc_version else []
            try:
                latest_by_hash = self._modrinth.latest_from_hashes(
                    [mod.sha1 for mod, _ in local.values()],
                    loaders=[self.options.loader],
                    game_versions=game_versions,
                )
            except UpstreamError as error:
                latest_failed = True
                report.upstream_notes.append(
                    ("note.modrinth_unavailable", {"error": str(error)})
                )

        # 1c. Titles for the project pages.
        project_ids = [version.project_id for _, version in local.values()]
        projects: Dict[str, ModrinthProject] = {}
        if project_ids:
            try:
                projects = self._modrinth.projects(project_ids)
            except UpstreamError as error:
                report.upstream_notes.append(
                    ("note.modrinth_unavailable", {"error": str(error)})
                )

        # 1d. A hash absent from step 1b means the project publishes nothing for this
        #     loader/game-version pair, but the endpoint cannot say which of the two is
        #     missing. One extra per-project question tells them apart, and the two cases
        #     deserve different advice, so it is asked.
        answered = set(latest_by_hash.keys())
        unanswered_projects = sorted(
            {
                version.project_id
                for mod, version in local.values()
                if version.project_id and mod.sha1 not in answered
            }
        )
        context, context_failures = self._loader_versions_for(unanswered_projects)

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
                return project_id, self._modrinth.project_versions(  # type: ignore[union-attr]
                    project_id, loaders=[self.options.loader]
                )
            except UpstreamError as error:
                self.logger.debug("project %s lookup failed: %s", project_id, error)
                return project_id, None

        workers = min(_MAX_WORKERS, max(1, self.options.workers), len(project_ids))
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
        entry.matched_by = "hash"
        entry.project_url = project.page_url if project else ""
        if project is not None:
            _prefer_name(entry, project.title)

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

        entry.status = STATUS_NO_COMPATIBLE_BUILD
        newest = versions[0]
        entry.latest_version = newest.version_number
        entry.released_at = newest.date_published
        entry.release_channel = newest.version_type
        entry.add_note(
            "note.no_build_for_game_version",
            version=server.mc_version or "?",
            newest=newest.version_number,
            targets=", ".join(newest.game_versions[-6:]) or "?",
        )

    # -- stage 2: CurseForge -----------------------------------------------------------

    def _stage_curseforge(
        self,
        mods: Sequence[ScannedMod],
        entries: Dict[str, UpdateEntry],
        report: Report,
        server: ServerContext,
    ) -> List[ScannedMod]:
        assert self._curseforge is not None
        if not self.options.use_curseforge or not self._curseforge.enabled:
            return list(mods)

        fingerprints = [mod.fingerprint for mod in mods if mod.fingerprint]
        if not fingerprints:
            return list(mods)

        try:
            matched = self._curseforge.files_by_fingerprints(fingerprints)
        except UpstreamError as error:
            report.upstream_notes.append(
                ("note.curseforge_unavailable", {"error": str(error)})
            )
            return list(mods)

        by_fingerprint: Dict[int, Tuple[ScannedMod, CfFile]] = {}
        for mod in mods:
            file = matched.get(mod.fingerprint)
            if file is not None:
                by_fingerprint[mod.fingerprint] = (mod, file)

        projects: Dict[int, Any] = {}
        mod_ids = [file.mod_id for _, file in by_fingerprint.values() if file.mod_id]
        if mod_ids:
            try:
                projects = self._curseforge.mods(mod_ids)
            except UpstreamError as error:
                report.upstream_notes.append(
                    ("note.curseforge_unavailable", {"error": str(error)})
                )

        latest_files, latest_failures = self._curseforge_latest_files(by_fingerprint, server)

        leftover: List[ScannedMod] = []
        for mod in mods:
            pair = by_fingerprint.get(mod.fingerprint)
            if pair is None:
                leftover.append(mod)
                continue
            _, local_file = pair
            entry = entries[mod.file_name]
            project = projects.get(local_file.mod_id)
            self._apply_curseforge(
                mod,
                entry,
                local_file,
                project,
                latest_files.get(local_file.mod_id),
                local_file.mod_id in latest_failures,
                server,
            )
        return leftover

    def _curseforge_latest_files(
        self,
        by_fingerprint: Dict[int, Tuple[ScannedMod, CfFile]],
        server: ServerContext,
    ) -> Tuple[Dict[int, Optional[CfFile]], Set[int]]:
        """Newest compatible file per project id, plus the ids whose lookup failed.

        Failure is tracked rather than folded into ``None`` for the same reason as on the
        Modrinth side: "this project has no Fabric build" and "the request failed" must not
        reach the admin as the same sentence.
        """
        assert self._curseforge is not None
        loader_type = self._curseforge.loader_type_for(self.options.loader)
        release_types = self.options.release_types
        wanted = sorted({file.mod_id for _, file in by_fingerprint.values() if file.mod_id})
        if not wanted:
            return {}, set()

        def newest(mod_id: int) -> Tuple[int, Optional[CfFile], bool]:
            try:
                files = self._curseforge.mod_files(  # type: ignore[union-attr]
                    mod_id,
                    game_version=server.mc_version,
                    loader_type=loader_type,
                    release_types=release_types,
                )
                if files:
                    return mod_id, files[0], False
                if server.mc_version:
                    # Nothing for our game version: ask again unfiltered so the report can
                    # name what the project *does* publish.
                    fallback = self._curseforge.mod_files(  # type: ignore[union-attr]
                        mod_id, loader_type=loader_type, release_types=release_types
                    )
                    return mod_id, (fallback[0] if fallback else None), False
                return mod_id, None, False
            except UpstreamError as error:
                self.logger.debug("curseforge files for %s failed: %s", mod_id, error)
                return mod_id, None, True

        workers = min(_MAX_WORKERS, max(1, self.options.workers), len(wanted))
        files: Dict[int, Optional[CfFile]] = {}
        failures: Set[int] = set()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for mod_id, found, failed in pool.map(newest, wanted):
                files[mod_id] = found
                if failed:
                    failures.add(mod_id)
        return files, failures

    def _apply_curseforge(
        self,
        mod: ScannedMod,
        entry: UpdateEntry,
        local_file: CfFile,
        project: Any,
        latest_file: Optional[CfFile],
        latest_failed: bool,
        server: ServerContext,
    ) -> None:
        entry.platform = "curseforge"
        entry.matched_by = "fingerprint"
        if project is not None:
            entry.project_url = project.page_url
            _prefer_name(entry, project.name)

        if latest_file is None:
            if latest_failed:
                _mark_undetermined(entry, "curseforge")
                return
            entry.status = STATUS_NO_COMPATIBLE_BUILD
            entry.add_note("note.no_build_for_loader", loader=self.options.loader)
            return

        entry.latest_version = latest_file.display_name or latest_file.file_name
        entry.release_channel = latest_file.release_channel
        entry.released_at = latest_file.file_date
        if latest_file.download_url:
            entry.download_url = latest_file.download_url
        else:
            entry.add_note("note.no_api_download")
        if entry.project_url and latest_file.id:
            entry.add_note("note.latest_file", file=latest_file.file_name)

        # CurseForge file rows carry no version string, so the decision is made on file
        # identity first and upload timestamps second. Comparing the *display names* as
        # versions would be nonsense — "11.68.0.1086 for Fabric 1.19.2" is not a version.
        if latest_file.id == local_file.id or (
            local_file.fingerprint and latest_file.fingerprint == local_file.fingerprint
        ):
            entry.status = STATUS_UP_TO_DATE
            return

        local_stamp = _parse_timestamp(local_file.file_date)
        latest_stamp = _parse_timestamp(latest_file.file_date)
        if local_stamp is not None and latest_stamp is not None and local_stamp != latest_stamp:
            entry.status = (
                STATUS_UPDATE_AVAILABLE if local_stamp < latest_stamp else STATUS_LOCAL_AHEAD
            )
            return

        # Fallback for an unparseable timestamp: compare the strings. CurseForge emits one
        # consistent UTC format so this agrees with the branch above in practice, but it is
        # only correct for strings in a single format, which is why it is the fallback.
        local_date = local_file.file_date or ""
        latest_date = latest_file.file_date or ""
        if local_date and latest_date and local_date != latest_date:
            if local_date < latest_date:
                entry.status = STATUS_UPDATE_AVAILABLE
            else:
                entry.status = STATUS_LOCAL_AHEAD
            return

        if not latest_file.supports_game_version(server.mc_version or ""):
            entry.status = STATUS_NO_COMPATIBLE_BUILD
            entry.add_note(
                "note.no_build_for_game_version",
                version=server.mc_version or "?",
                newest=entry.latest_version,
                targets=", ".join(latest_file.game_versions[-6:]) or "?",
            )
            return

        entry.status = STATUS_UPDATE_AVAILABLE
        entry.add_note("note.different_build_same_date")

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

        This is the one stage whose failures are worth remembering: proving that a jar is on
        neither platform costs three requests (a search on each, plus a version lookup), and
        the answer will not have changed by tomorrow. So a negative verdict is cached, and the
        TTL is what bounds how long a newly published mod stays invisible.

        Only a *established* negative verdict is cached. If an enabled platform could not be
        asked at all, "neither platform knows this jar" is not something that was learned, and
        caching it would hide the mod from every check for the whole TTL — a transient outage
        promoted into a day-long blind spot.
        """
        for mod in mods:
            entry = entries[mod.file_name]
            if entry.status in (STATUS_IGNORED, STATUS_ERROR):
                continue
            if not mod.identified:
                # A valid jar with no mod metadata that no hash matched stays "not a mod".
                # Searching for it by file name would only ever produce a false positive.
                continue

            cached = self._cache.get(mod.sha1) if mod.sha1 else None
            if cached is not None and cached.get("resolved") is False:
                entry.status = STATUS_UNRESOLVED
                entry.add_note("note.cached_unresolved")
                continue

            query = mod.mod_id or Path(mod.file_name).stem
            if not query:
                entry.status = STATUS_UNRESOLVED
                continue

            asked_modrinth = bool(self.options.use_modrinth)
            by_modrinth = (
                self._match_on_modrinth(mod, entry, query, server) if asked_modrinth else False
            )
            if by_modrinth:
                continue

            asked_curseforge = bool(self.options.use_curseforge and self._curseforge.enabled)
            by_curseforge = (
                self._match_on_curseforge(mod, entry, query, server)
                if asked_curseforge
                else False
            )
            if by_curseforge:
                continue

            entry.status = STATUS_UNRESOLVED
            incomplete = (asked_modrinth and by_modrinth is None) or (
                asked_curseforge and by_curseforge is None
            )
            if incomplete:
                entry.add_note("note.search_incomplete")
            elif mod.sha1:
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

        entry.platform = "modrinth"
        entry.matched_by = "name"
        entry.project_url = hit.page_url
        _prefer_name(entry, hit.title)
        entry.add_note("note.matched_by_name", slug=hit.slug)
        if not versions:
            entry.status = STATUS_NO_COMPATIBLE_BUILD
            entry.add_note("note.no_build_for_loader", loader=self.options.loader)
            return True

        compatible = [
            version
            for version in versions
            if not server.mc_version or server.mc_version in version.game_versions
        ]
        newest = (compatible or versions)[0]
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
                targets=", ".join((versions[0].game_versions or [])[-6:]) or "?",
            )
            return True

        self._decide_by_string(entry)
        return True

    def _match_on_curseforge(
        self, mod: ScannedMod, entry: UpdateEntry, query: str, server: ServerContext
    ) -> Optional[bool]:
        """``True`` resolved, ``False`` asked and not found, ``None`` could not ask."""
        assert self._curseforge is not None
        if not self._curseforge.enabled:
            return False
        try:
            results = self._curseforge.search(query)
        except UpstreamError as error:
            self.logger.debug("curseforge search for %s failed: %s", query, error)
            return None

        wanted = normalise_name(query)
        accepted = [
            project
            for project in results
            if normalise_name(project.slug) == wanted or normalise_name(project.name) == wanted
        ]
        if not accepted:
            return False

        project = accepted[0]
        entry.platform = "curseforge"
        entry.matched_by = "name"
        entry.project_url = project.page_url
        _prefer_name(entry, project.name)
        entry.add_note("note.matched_by_name", slug=project.slug)
        files = self._curseforge.mod_files(
            project.id,
            game_version=server.mc_version,
            loader_type=self._curseforge.loader_type_for(self.options.loader),
            release_types=self.options.release_types,
        )
        if not files:
            entry.status = STATUS_NO_COMPATIBLE_BUILD
            entry.add_note("note.no_build_for_loader", loader=self.options.loader)
            return True

        newest = files[0]
        entry.latest_version = newest.display_name or newest.file_name
        entry.release_channel = newest.release_channel
        entry.released_at = newest.file_date
        if newest.download_url:
            entry.download_url = newest.download_url
        else:
            entry.add_note("note.no_api_download")

        if entry.local_version and newest.file_name.startswith(entry.local_version):
            entry.status = STATUS_UP_TO_DATE
            return True
        self._decide_by_string(entry)
        return True

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

    @staticmethod
    def _unidentified_reason(entry: UpdateEntry) -> str:
        if entry.error:
            return entry.error
        if entry.status == STATUS_NOT_A_MOD:
            return "not-a-mod"
        return ""


def _parse_timestamp(text: str) -> Optional[float]:
    """Best-effort epoch seconds for an ISO-8601 timestamp, or ``None``.

    Used instead of comparing the strings directly: string comparison is only correct while
    both sides share one format and one UTC offset, and "correct by accident" is the kind of
    thing that breaks the day an upstream changes how it writes a date.
    """
    if not text:
        return None
    cleaned = str(text).strip()
    if cleaned.endswith(("Z", "z")):
        # ``datetime.fromisoformat`` only learned to accept a bare ``Z`` in 3.11, and MCDR
        # still supports 3.8.
        cleaned = cleaned[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
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
