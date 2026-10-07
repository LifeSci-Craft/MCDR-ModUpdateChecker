"""CurseForge API client (``/v1``).

CurseForge differs from Modrinth in ways that shape this module:

* **Every** endpoint needs an API key. Verified directly against the live service: the
  public host answers ``403 "API Key missing or invalid"`` for project lookups with no key
  and ``401`` for fingerprint lookups, and the website's own ``/api/v1/`` endpoints sit
  behind a Cloudflare interstitial. There is therefore no unauthenticated fallback, and the
  client reports itself as disabled rather than failing every mod one by one.
* A jar is identified by its MurmurHash2 **fingerprint**
  (:mod:`mod_update_checker.fingerprint`), not by a cryptographic digest.
* Version information arrives in two shapes that must be joined: ``POST /v1/mods`` gives
  ``latestFilesIndexes`` (ids, no download URL) while ``GET /v1/mods/{id}/files`` gives real
  file rows including ``downloadUrl``.

The last point matters for honesty in the report: an author can opt out of third-party
distribution, in which case ``downloadUrl`` comes back ``null``. This client never
reconstructs a CDN link in that case, because such a link would bypass the author's choice
and may not even resolve. The admin gets the project page instead.

No MCDR import; the client takes an :class:`~mod_update_checker.upstream.HttpClient`.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .upstream import HttpClient

__all__ = [
    "DEFAULT_BASE_URL",
    "MINECRAFT_GAME_ID",
    "MOD_LOADER_TYPES",
    "RELEASE_TYPES",
    "CfFile",
    "CfMod",
    "CurseForgeClient",
]

DEFAULT_BASE_URL = "https://api.curseforge.com/v1"

#: CurseForge's numeric id for Minecraft.
MINECRAFT_GAME_ID = 432

#: ``ModLoaderType`` enum. Fabric is 4, NeoForge 6 — see the CurseForge API docs.
MOD_LOADER_TYPES: Dict[str, int] = {
    "forge": 1,
    "cauldron": 2,
    "liteloader": 3,
    "fabric": 4,
    "quilt": 5,
    "neoforge": 6,
}

#: ``FileReleaseType`` enum: 1 release, 2 beta, 3 alpha.
RELEASE_TYPES: Dict[str, int] = {"release": 1, "beta": 2, "alpha": 3}
_RELEASE_TYPE_NAMES = {value: key for key, value in RELEASE_TYPES.items()}

#: CurseForge's loader names as they appear inside a file's ``gameVersions`` array.
_LOADER_LABELS: Dict[int, str] = {
    1: "forge",
    2: "cauldron",
    3: "liteloader",
    4: "fabric",
    5: "quilt",
    6: "neoforge",
}

_FINGERPRINT_CHUNK = 500
_MOD_ID_CHUNK = 100


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


@dataclass
class CfFile:
    """One uploaded file of a CurseForge project."""

    id: int
    mod_id: int = 0
    file_name: str = ""
    display_name: str = ""
    release_type: int = 1
    file_date: str = ""
    file_length: int = 0
    download_url: Optional[str] = None
    game_versions: Sequence[str] = field(default_factory=list)
    fingerprint: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CfFile":
        return cls(
            id=int(data.get("id") or 0),
            mod_id=int(data.get("modId") or 0),
            file_name=str(data.get("fileName", "") or ""),
            display_name=str(data.get("displayName", "") or ""),
            release_type=int(data.get("releaseType") or 1),
            file_date=str(data.get("fileDate", "") or ""),
            file_length=int(data.get("fileLength") or 0),
            download_url=data.get("downloadUrl") or None,
            game_versions=[str(item) for item in (data.get("gameVersions") or [])],
            fingerprint=int(data.get("fileFingerprint") or 0),
        )

    @property
    def release_channel(self) -> str:
        return _RELEASE_TYPE_NAMES.get(self.release_type, "release")

    def page_url(self, slug: str = "") -> str:
        """The file's page on curseforge.com — the slug is required to build it."""
        if not slug:
            return "https://www.curseforge.com/minecraft/mc-mods"
        return "https://www.curseforge.com/minecraft/mc-mods/{}/files/{}".format(slug, self.id)

    def supports_game_version(self, version: str) -> bool:
        return version in self.game_versions

    def supports_loader(self, loader: str) -> bool:
        """Whether the file lists ``loader`` among its game versions.

        A file's ``gameVersions`` mixes Minecraft versions and loader names, so the check
        is a plain membership test rather than a field read.
        """
        return loader.lower() in {item.lower() for item in self.game_versions}


@dataclass
class CfMod:
    """A CurseForge project."""

    id: int
    name: str = ""
    slug: str = ""
    summary: str = ""
    website_url: str = ""
    source_url: str = ""
    download_count: int = 0
    latest_files: Sequence[CfFile] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CfMod":
        links = data.get("links") if isinstance(data.get("links"), dict) else {}
        return cls(
            id=int(data.get("id") or 0),
            name=str(data.get("name", "") or ""),
            slug=str(data.get("slug", "") or ""),
            summary=str(data.get("summary", "") or ""),
            website_url=str(
                links.get("websiteUrl")
                or (
                    "https://www.curseforge.com/minecraft/mc-mods/{}".format(
                        data.get("slug", "")
                    )
                    if data.get("slug")
                    else ""
                )
            ),
            source_url=str(links.get("sourceUrl") or ""),
            download_count=int(data.get("downloadCount") or 0),
            latest_files=[
                CfFile.from_dict(item)
                for item in (data.get("latestFiles") or [])
                if isinstance(item, dict)
            ],
        )

    @property
    def page_url(self) -> str:
        if self.website_url:
            return self.website_url
        return "https://www.curseforge.com/minecraft/mc-mods/{}".format(self.slug or self.id)


class CurseForgeClient:
    """Thin, typed wrapper over the endpoints this plugin needs."""

    name = "curseforge"

    def __init__(
        self,
        http: HttpClient,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        game_id: int = MINECRAFT_GAME_ID,
    ) -> None:
        self.http = http
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")
        self.game_id = int(game_id)

    @property
    def enabled(self) -> bool:
        """False without a key: the service cannot be queried at all."""
        return bool(self.api_key)

    @property
    def _headers(self) -> Dict[str, str]:
        return {"x-api-key": self.api_key}

    def _data(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            return payload.get("data")
        return None

    # -- identification ----------------------------------------------------------------

    def files_by_fingerprints(
        self, fingerprints: Sequence[int]
    ) -> Dict[int, CfFile]:
        """Map each fingerprint CurseForge recognises to the file it belongs to.

        Unrecognised fingerprints are simply absent from the result.

        The response does not echo the fingerprint that was asked about, so the local value
        is recovered from ``file.fileFingerprint``. A match whose fingerprint is missing or
        was not among the ones sent is dropped rather than guessed at: attributing the wrong
        upstream project to a jar would produce a confidently wrong update report, which is
        worse than reporting the mod as unresolved.
        """
        if not self.enabled:
            return {}
        found: Dict[int, CfFile] = {}
        unique = [value for value in dict.fromkeys(int(f) for f in fingerprints) if value]
        queried = set(unique)
        for batch in _chunks(unique, _FINGERPRINT_CHUNK):
            payload = self.http.post_json(
                "{}/fingerprints".format(self.base_url),
                {"fingerprints": list(batch)},
                headers=self._headers,
            )
            data = self._data(payload)
            if not isinstance(data, dict):
                continue
            for match in data.get("exactMatches") or []:
                if not isinstance(match, dict):
                    continue
                file_data = match.get("file")
                if not isinstance(file_data, dict):
                    continue
                file = CfFile.from_dict(file_data)
                if file.fingerprint and file.fingerprint in queried:
                    found[file.fingerprint] = file
        return found

    # -- project lookups ---------------------------------------------------------------

    def mods(self, mod_ids: Sequence[int]) -> Dict[int, CfMod]:
        """Fetch many projects by numeric id."""
        if not self.enabled:
            return {}
        found: Dict[int, CfMod] = {}
        unique = [value for value in dict.fromkeys(int(i) for i in mod_ids) if value]
        for batch in _chunks(unique, _MOD_ID_CHUNK):
            payload = self.http.post_json(
                "{}/mods".format(self.base_url),
                {"modIds": list(batch)},
                headers=self._headers,
            )
            data = self._data(payload)
            if not isinstance(data, list):
                continue
            for item in data:
                if isinstance(item, dict) and item.get("id"):
                    mod = CfMod.from_dict(item)
                    found[mod.id] = mod
        return found

    def mod(self, mod_id: int) -> Optional[CfMod]:
        """One project by id, or ``None`` when it does not exist."""
        if not self.enabled:
            return None
        payload = self.http.get_json(
            "{}/mods/{}".format(self.base_url, int(mod_id)),
            headers=self._headers,
            allow_404=True,
        )
        data = self._data(payload)
        if isinstance(data, dict) and data.get("id"):
            return CfMod.from_dict(data)
        return None

    def search(self, query: str, page_size: int = 10) -> List[CfMod]:
        """Search by slug or name. The identification path when a hash is unknown."""
        if not self.enabled:
            return []
        payload = self.http.get_json(
            "{}/mods/search".format(self.base_url),
            params={
                "gameId": self.game_id,
                "slug": query,
                "pageSize": max(1, int(page_size)),
            },
            headers=self._headers,
        )
        data = self._data(payload)
        if not isinstance(data, list):
            return []
        return [CfMod.from_dict(item) for item in data if isinstance(item, dict)]

    # -- file lookups ------------------------------------------------------------------

    def mod_files(
        self,
        mod_id: int,
        game_version: Optional[str] = None,
        loader_type: Optional[int] = None,
        release_types: Optional[Sequence[int]] = None,
        page_size: int = 100,
    ) -> List[CfFile]:
        """Files of a project, filtered server-side, sorted newest first locally.

        Unlike the ``latestFilesIndexes`` embedded in a project object, these rows carry a
        real ``downloadUrl``, which is the whole reason for the extra request.

        The ordering is done here rather than via the endpoint's ``sortField`` parameter:
        that parameter takes an enum whose numbering is easy to get wrong and whose failure
        mode is a silently mis-sorted list, whereas ``fileDate`` is a plain string and sorting
        it costs nothing.

        Only one page is fetched. That is sound because ``gameVersion`` and ``modLoaderType``
        are applied *before* paging, so the window holds the files for one Minecraft version
        and one loader — a handful for a typical mod, not the project's whole history. An
        explicit sort would still be the belt-and-braces option, but it needs a key to verify
        the enum against and there is none available here; the page size is generous for the
        reasoning to hold.
        """
        if not self.enabled:
            return []
        params: Dict[str, Any] = {"pageSize": max(1, int(page_size))}
        if game_version:
            params["gameVersion"] = game_version
        if loader_type is not None:
            params["modLoaderType"] = int(loader_type)
        payload = self.http.get_json(
            "{}/mods/{}/files".format(self.base_url, int(mod_id)),
            params=params,
            headers=self._headers,
            allow_404=True,
        )
        data = self._data(payload)
        if not isinstance(data, list):
            return []

        allowed = set(release_types) if release_types else set(RELEASE_TYPES.values())
        files = [
            CfFile.from_dict(item)
            for item in data
            if isinstance(item, dict) and int(item.get("releaseType") or 1) in allowed
        ]
        files.sort(key=lambda item: item.file_date, reverse=True)
        return files

    def loader_type_for(self, loader: str) -> Optional[int]:
        """Map a loader name onto CurseForge's ``ModLoaderType``."""
        return MOD_LOADER_TYPES.get((loader or "").lower())

    @staticmethod
    def loader_label(loader: str) -> str:
        """The loader's display name inside a file's ``gameVersions`` array."""
        for value, label in _LOADER_LABELS.items():
            if label == (loader or "").lower():
                return label.capitalize()
        return loader
