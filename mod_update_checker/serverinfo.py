"""Working out which Minecraft version and loader the server is running.

The update check needs two facts, and neither is written down anywhere convenient:

* **Minecraft version** — not in ``server.properties`` (it only holds ``level-name`` and
  friends). It has to come from the server's own output, and this module tries, in order:
  a configuration override, MCDR's parsed :class:`ServerInformation` (which is populated
  once the server has printed ``Starting minecraft server version …``), the startup lines
  in ``logs/latest.log``, and finally a vote among the ``depends.minecraft`` ranges declared
  by the installed mods.

  A wrong guess is worse than no guess here: filtering Modrinth by the wrong game version
  silently reports every mod as "no compatible build". So each source records where the
  answer came from, and the report prints it, which lets an admin spot a bad autodetect
  immediately and override it in the config.

* **Loader** — assumed from the configuration (``fabric`` by default), but cross-checked
  against what the jars on disk actually are, because a folder of Forge jars on a "Fabric"
  server is exactly the kind of mix-up worth surfacing.

No MCDR import: callers pass in the pieces they can get.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .scanner import ScanResult
from .versioning import covers

__all__ = [
    "ServerContext",
    "MC_VERSION_SOURCES",
    "read_log_head",
    "parse_log_text",
    "detect_loader_from_mods",
    "detect",
    "mod_supports_server_version",
    "sanitize_version",
]

#: Where an answered ``mc_version`` came from, most trustworthy first. Printed in reports.
MC_VERSION_SOURCES = ("config", "server_info", "log", "mods", "unknown")

#: Startup lines that name the game version, in priority order. Fabric and Quilt announce
#: the version with the loader on one line; vanilla/Forge print the older sentence, which
#: MCDR itself parses for ``ServerInformation``.
_LOG_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    (
        re.compile(
            r"Loading Minecraft (?P<game>[0-9][\w.\-+]*) with (?P<loader>Fabric|Quilt|NeoForge|Forge) Loader (?P<version>[\w.\-+]+)",
            re.IGNORECASE,
        ),
        "loader",
    ),
    (
        re.compile(r"Starting minecraft server version (?P<game>[\w.\-+]+)", re.IGNORECASE),
        "vanilla",
    ),
    (
        re.compile(r"Loading Minecraft (?P<game>[0-9][\w.\-+]*)", re.IGNORECASE),
        "plain",
    ),
)

#: How much of ``latest.log`` to read. Startup lines are at the head; a long-running
#: server's log can be hundreds of megabytes and none of it is needed.
LOG_HEAD_BYTES = 256 * 1024

#: Leading version-looking token of a string. MCDR's ``ServerInformation.version`` is
#: documented as holding things like ``"1.17 Release Candidate 1"``, not just ``"1.17"``,
#: so the raw value cannot be used as a game version without this.
_VERSION_TOKEN = re.compile(r"^[\w.+\-]+")


def sanitize_version(text: Optional[str]) -> Optional[str]:
    """The version part of a server-reported version string, or ``None``."""
    if not text:
        return None
    match = _VERSION_TOKEN.match(str(text).strip())
    return match.group(0) if match else None


@dataclass
class ServerContext:
    """The Minecraft version / loader pair the check will run against."""

    mc_version: Optional[str] = None
    mc_version_source: str = "unknown"
    loader: str = "fabric"
    loader_source: str = "config"
    loader_version: Optional[str] = None
    mod_loader_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return bool(self.mc_version)

    def describe(self) -> str:
        """A one-line summary for the report header."""
        version = self.mc_version or "?"
        if self.loader_version:
            return "{} / {} {}".format(version, self.loader.capitalize(), self.loader_version)
        return "{} / {}".format(version, self.loader.capitalize())


def read_log_head(path: Path, limit: int = LOG_HEAD_BYTES) -> str:
    """Read the beginning of a log file, tolerating any encoding and a missing file."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def parse_log_text(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(mc_version, loader_name, loader_version)`` found in server output.

    Every pattern is tried against the whole text and the *first* pattern that matched
    anywhere wins, rather than the first line that matched: a log may open with a
    launcher's unrelated output before Fabric's banner appears.
    """
    for pattern, kind in _LOG_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        game = match.group("game")
        if kind == "loader":
            return game, match.group("loader").lower(), match.group("version")
        return game, None, None
    return None, None, None


def detect_loader_from_mods(scan: ScanResult) -> Dict[str, int]:
    """Count identified mods per loader."""
    counts: Dict[str, int] = {}
    for mod in scan.mods:
        if mod.metadata and mod.metadata.loader:
            counts[mod.metadata.loader] = counts.get(mod.metadata.loader, 0) + 1
    return counts


def _vote_mc_version(mods: Iterable) -> Optional[str]:
    """Guess the game version from the mods' own ``depends.minecraft`` ranges.

    Only used when nothing better is available. The most frequently declared *lowest*
    bound wins, because that is the version a range such as ``>=1.21 <1.22`` was built for;
    a range with no usable bound is ignored rather than defaulted.
    """
    votes: Dict[str, int] = {}
    for mod in mods:
        if not (mod.metadata and mod.metadata.mc_range):
            continue
        spec = mod.metadata.mc_range
        for candidate in _candidate_versions(spec):
            votes[candidate] = votes.get(candidate, 0) + 1
    if not votes:
        return None
    best = max(votes.items(), key=lambda item: (item[1], item[0]))
    return best[0] if best[1] > 0 else None


def _candidate_versions(spec) -> List[str]:
    """Every version-ish token an expression mentions, in declaration order."""
    texts: Sequence[str]
    if isinstance(spec, str):
        texts = [spec]
    elif spec is None:
        texts = []
    else:
        texts = [str(item) for item in spec]
    found: List[str] = []
    for text in texts:
        for token in re.findall(r"\d+(?:\.\d+)+", text):
            if token not in found:
                found.append(token)
    return found


def detect(
    working_directory: str,
    configured_version: str = "",
    configured_loader: str = "fabric",
    server_information_version: Optional[str] = None,
    scan: Optional[ScanResult] = None,
) -> ServerContext:
    """Resolve the server context, recording where each answer came from.

    :param working_directory: the Minecraft server folder (MCDR's ``working_directory``).
    :param configured_version: an explicit override; ``auto`` or empty means "detect it".
    :param configured_loader: the loader the admin says they run.
    :param server_information_version: ``server.get_server_information().version``, if the
        server is up and MCDR has parsed that line.
    :param scan: the mod scan, used both for the loader cross-check and the last-resort
        version vote.
    """
    context = ServerContext(loader=(configured_loader or "fabric").lower())

    override = (configured_version or "").strip()
    if override and override.lower() != "auto":
        context.mc_version = override
        context.mc_version_source = "config"

    loader_from_mods = detect_loader_from_mods(scan) if scan else {}
    context.mod_loader_counts = loader_from_mods

    if scan is not None and loader_from_mods:
        observed = max(loader_from_mods.items(), key=lambda item: (item[1], item[0]))[0]
        if observed != context.loader:
            context.loader = observed
            context.loader_source = "mods"
        else:
            context.loader_source = "config+mods"

    if not context.mc_version and server_information_version:
        detected = sanitize_version(server_information_version)
        if detected:
            context.mc_version = detected
            context.mc_version_source = "server_info"

    if not context.mc_version:
        log_text = read_log_head(Path(working_directory) / "logs" / "latest.log")
        game, loader_name, loader_version = parse_log_text(log_text)
        if game:
            context.mc_version = game
            context.mc_version_source = "log"
        if loader_name:
            context.loader = loader_name
            context.loader_source = "log"
        if loader_version:
            context.loader_version = loader_version
    elif context.loader_version is None:
        # The version is already known but the loader banner may still tell us the loader
        # version, which is nice to show next to a "your mods were built for 26.3" note.
        log_text = read_log_head(Path(working_directory) / "logs" / "latest.log")
        _, loader_name, loader_version = parse_log_text(log_text)
        if loader_version:
            context.loader_version = loader_version
        if loader_name and context.loader_source in ("config", "config+mods"):
            context.loader = loader_name
            context.loader_source = "log"

    if not context.mc_version and scan is not None:
        voted = _vote_mc_version(scan.mods)
        if voted:
            context.mc_version = voted
            context.mc_version_source = "mods"

    return context


def mod_supports_server_version(mod, mc_version: Optional[str]) -> Optional[bool]:
    """Does a mod's declared Minecraft range admit the server's version?

    ``None`` means "no opinion": either the mod declares nothing, or the server version is
    unknown. Callers use that to decide whether a warning is warranted, so a missing answer
    must not be turned into a false alarm.
    """
    if not mc_version or mod.metadata is None or mod.metadata.mc_range is None:
        return None
    try:
        return covers(mod.metadata.mc_range, mc_version)
    except Exception:  # noqa: BLE001 - a bizarre range must not fail the run
        return None
