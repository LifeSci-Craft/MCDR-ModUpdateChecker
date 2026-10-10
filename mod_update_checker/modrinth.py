"""Modrinth API client (``/v2``).

Modrinth is the better of the two upstreams for this job, and it is the one that needs no
credentials:

* it can be asked about a jar by **exact SHA-1**, which is the only way to identify a mod
  whose file name tells you nothing;
* ``POST /version_files/update`` answers "what is the newest build of these files that
  supports loader L on game version G" for a whole batch of hashes in one request, which is
  what keeps a check of a 200-mod server down to a handful of calls rather than hundreds;
* ``GET /projects?ids=[...]`` returns titles and links for a whole batch too.

Everything is chunked by :data:`HASH_CHUNK` because the API does not document an upper
bound on how many hashes one request may carry, and a request that is refused for being too
large would fail a whole check run rather than one mod.

No MCDR import; the client takes an :class:`~mod_update_checker.upstream.HttpClient`.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .upstream import HttpClient

__all__ = [
    "DEFAULT_BASE_URL",
    "HASH_CHUNK",
    "ModrinthFile",
    "ModrinthVersion",
    "ModrinthProject",
    "ModrinthHit",
    "ModrinthClient",
]

DEFAULT_BASE_URL = "https://api.modrinth.com/v2"

#: Hashes per batch request. Conservative on purpose — see the module docstring.
HASH_CHUNK = 100

#: Projects per ``GET /projects?ids=`` request.
ID_CHUNK = 100


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


@dataclass
class ModrinthFile:
    """One downloadable file of a version."""

    url: str
    filename: str
    primary: bool = False
    size: int = 0
    sha1: str = ""
    sha512: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModrinthFile":
        hashes = data.get("hashes") or {}
        return cls(
            url=str(data.get("url", "")),
            filename=str(data.get("filename", "")),
            primary=bool(data.get("primary", False)),
            size=int(data.get("size") or 0),
            sha1=str(hashes.get("sha1", "") or ""),
            sha512=str(hashes.get("sha512", "") or ""),
        )


@dataclass
class ModrinthVersion:
    """A published version of a project."""

    id: str
    project_id: str
    version_number: str
    name: str = ""
    version_type: str = "release"
    loaders: Sequence[str] = field(default_factory=list)
    game_versions: Sequence[str] = field(default_factory=list)
    date_published: str = ""
    status: str = ""
    files: Sequence[ModrinthFile] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModrinthVersion":
        return cls(
            id=str(data.get("id", "")),
            project_id=str(data.get("project_id", "")),
            version_number=str(data.get("version_number", "")),
            name=str(data.get("name", "") or ""),
            version_type=str(data.get("version_type", "release") or "release"),
            loaders=list(data.get("loaders") or []),
            game_versions=list(data.get("game_versions") or []),
            date_published=str(data.get("date_published", "") or ""),
            status=str(data.get("status", "") or ""),
            files=[ModrinthFile.from_dict(item) for item in (data.get("files") or [])],
        )

    @property
    def primary_file(self) -> Optional[ModrinthFile]:
        """The file a user should download: the flagged primary, else the first one."""
        for item in self.files:
            if item.primary:
                return item
        return self.files[0] if self.files else None

    def page_url(self, slug: str = "") -> str:
        project = slug or self.project_id
        return "https://modrinth.com/mod/{}/version/{}".format(project, self.id)

    def hashes(self) -> List[str]:
        out: List[str] = []
        for item in self.files:
            for value in (item.sha1, item.sha512):
                if value and value not in out:
                    out.append(value)
        return out

    def supports(self, mc_version: str) -> bool:
        return mc_version in self.game_versions


@dataclass
class ModrinthProject:
    """Project metadata, as returned by ``GET /projects``."""

    id: str
    slug: str = ""
    title: str = ""
    project_type: str = "mod"
    description: str = ""
    icon_url: str = ""
    source_url: str = ""
    game_versions: Sequence[str] = field(default_factory=list)
    loaders: Sequence[str] = field(default_factory=list)
    client_side: str = ""
    server_side: str = ""
    downloads: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModrinthProject":
        return cls(
            id=str(data.get("id") or data.get("project_id") or ""),
            slug=str(data.get("slug", "") or ""),
            title=str(data.get("title", "") or ""),
            project_type=str(data.get("project_type", "mod") or "mod"),
            description=str(data.get("description", "") or ""),
            icon_url=str(data.get("icon_url", "") or ""),
            source_url=str(data.get("source_url", "") or ""),
            game_versions=list(data.get("game_versions") or []),
            loaders=list(data.get("loaders") or []),
            client_side=str(data.get("client_side", "") or ""),
            server_side=str(data.get("server_side", "") or ""),
            downloads=int(data.get("downloads") or 0),
        )

    @property
    def page_url(self) -> str:
        return "https://modrinth.com/mod/{}".format(self.slug or self.id)


@dataclass
class ModrinthHit:
    """One result of a search. A search returns a slimmer shape than a project lookup."""

    project_id: str
    slug: str = ""
    title: str = ""
    description: str = ""
    author: str = ""
    project_type: str = "mod"
    loaders: Sequence[str] = field(default_factory=list)
    versions: Sequence[str] = field(default_factory=list)
    downloads: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ModrinthHit":
        return cls(
            project_id=str(data.get("project_id", "") or ""),
            slug=str(data.get("slug", "") or ""),
            title=str(data.get("title", "") or ""),
            description=str(data.get("description", "") or ""),
            author=str(data.get("author", "") or ""),
            project_type=str(data.get("project_type", "mod") or "mod"),
            loaders=list(data.get("loaders") or []),
            versions=list(data.get("versions") or []),
            downloads=int(data.get("downloads") or 0),
        )

    @property
    def page_url(self) -> str:
        return "https://modrinth.com/mod/{}".format(self.slug or self.project_id)


class ModrinthClient:
    """Thin, typed wrapper over the endpoints this plugin needs."""

    name = "modrinth"

    def __init__(self, http: HttpClient, base_url: str = DEFAULT_BASE_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    # -- version lookups ---------------------------------------------------------------

    def versions_from_hashes(
        self, hashes: Sequence[str], algorithm: str = "sha1"
    ) -> Dict[str, ModrinthVersion]:
        """Map each hash that Modrinth knows to the version it belongs to.

        Unknown hashes are simply absent from the result — that is how Modrinth answers,
        and it is also how "this jar is not on Modrinth" is detected.
        """
        found: Dict[str, ModrinthVersion] = {}
        unique = [value for value in dict.fromkeys(hashes) if value]
        for batch in _chunks(unique, HASH_CHUNK):
            payload = self.http.post_json(
                "{}/version_files".format(self.base_url),
                {"hashes": list(batch), "algorithm": algorithm},
            )
            if not isinstance(payload, dict):
                continue
            for key, value in payload.items():
                if isinstance(value, dict):
                    found[key] = ModrinthVersion.from_dict(value)
        return found

    def latest_from_hashes(
        self,
        hashes: Sequence[str],
        loaders: Sequence[str] = (),
        game_versions: Sequence[str] = (),
        algorithm: str = "sha1",
    ) -> Dict[str, ModrinthVersion]:
        """Newest build per hash that matches the loader / game-version filters.

        A hash with no matching build is absent from the result. At least one filter must
        be supplied, otherwise the endpoint just repeats the plain lookup.
        """
        if not loaders and not game_versions:
            return self.versions_from_hashes(hashes, algorithm=algorithm)

        # Empty arrays are omitted rather than sent. An empty filter is at best redundant
        # and at worst read as "match nothing", which would report every mod as having no
        # compatible build — a failure that looks exactly like a real answer.
        body: Dict[str, Any] = {"hashes": [], "algorithm": algorithm}
        if loaders:
            body["loaders"] = list(loaders)
        if game_versions:
            body["game_versions"] = list(game_versions)
        found: Dict[str, ModrinthVersion] = {}
        unique = [value for value in dict.fromkeys(hashes) if value]
        for batch in _chunks(unique, HASH_CHUNK):
            body["hashes"] = list(batch)
            payload = self.http.post_json(
                "{}/version_files/update".format(self.base_url), body
            )
            if not isinstance(payload, dict):
                continue
            for key, value in payload.items():
                if isinstance(value, dict):
                    found[key] = ModrinthVersion.from_dict(value)
        return found

    def project_versions(
        self,
        project_id: str,
        loaders: Optional[Sequence[str]] = None,
        game_versions: Optional[Sequence[str]] = None,
    ) -> List[ModrinthVersion]:
        """All versions of a project, newest first, optionally filtered.

        Filters are sent to the server because the alternative — fetching everything and
        filtering locally — can mean hundreds of kilobytes for a project like Fabric API.
        """
        params: Dict[str, Any] = {}
        if loaders:
            params["loaders"] = json.dumps(list(loaders))
        if game_versions:
            params["game_versions"] = json.dumps(list(game_versions))
        payload = self.http.get_json(
            "{}/project/{}/version".format(self.base_url, project_id),
            params=params or None,
            allow_404=True,
        )
        if not isinstance(payload, list):
            return []
        return [
            ModrinthVersion.from_dict(item)
            for item in payload
            if isinstance(item, dict)
        ]

    # -- project lookups ---------------------------------------------------------------

    def projects(self, ids: Sequence[str]) -> Dict[str, ModrinthProject]:
        """Fetch many projects by id. Missing ids are absent from the result.

        ``allow_404=True`` deliberately: a single deleted or mistyped id in the batch would
        otherwise fail the whole request, and the caller would lose the titles and links for
        every other project in that chunk over one bad entry.
        """
        found: Dict[str, ModrinthProject] = {}
        unique = [value for value in dict.fromkeys(ids) if value]
        for batch in _chunks(unique, ID_CHUNK):
            payload = self.http.get_json(
                "{}/projects".format(self.base_url),
                params={"ids": json.dumps(list(batch))},
                allow_404=True,
            )
            if not isinstance(payload, list):
                continue
            for item in payload:
                if isinstance(item, dict) and item.get("id"):
                    project = ModrinthProject.from_dict(item)
                    found[project.id] = project
        return found

    def project(self, id_or_slug: str) -> Optional[ModrinthProject]:
        """One project by id or slug, or ``None`` when it does not exist."""
        payload = self.http.get_json(
            "{}/project/{}".format(self.base_url, id_or_slug), allow_404=True
        )
        if isinstance(payload, dict) and payload.get("id"):
            return ModrinthProject.from_dict(payload)
        return None

    def search(
        self,
        query: str,
        loaders: Sequence[str] = (),
        project_type: str = "mod",
        limit: int = 10,
    ) -> List[ModrinthHit]:
        """Full-text search, used only as a last-resort identification fallback."""
        facets: List[List[str]] = []
        if project_type:
            facets.append(["project_type:{}".format(project_type)])
        for loader in loaders:
            facets.append(["categories:{}".format(loader)])
        params: Dict[str, Any] = {"query": query, "limit": max(1, int(limit))}
        if facets:
            params["facets"] = json.dumps(facets)
        payload = self.http.get_json("{}/search".format(self.base_url), params=params)
        if not isinstance(payload, dict):
            return []
        return [
            ModrinthHit.from_dict(item)
            for item in (payload.get("hits") or [])
            if isinstance(item, dict)
        ]

    # -- tags --------------------------------------------------------------------------

    def release_game_versions(self) -> List[str]:
        """Every Minecraft release version Modrinth knows, newest first.

        Used to sanity-check a configured game version. Not on the hot path of a check.
        """
        payload = self.http.get_json("{}/tag/game_version".format(self.base_url))
        if not isinstance(payload, list):
            return []
        return [
            str(item.get("version"))
            for item in payload
            if isinstance(item, dict)
            and item.get("version_type") == "release"
            and item.get("version")
        ]
