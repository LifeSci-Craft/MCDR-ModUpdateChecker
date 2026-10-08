"""The admin's own answer to "which project is this jar?".

Two kinds of jar defeat the normal identification, and both are common on a real server:

* a jar **built from source or re-signed**, whose bytes Modrinth has never seen, so the
  exact-hash lookup cannot find it;
* a fork whose mod id no longer matches any project slug, so the name search has nothing
  exact to match on either.

Both end up reported as ``unresolved``, which is the honest answer and a documented
limitation. This module is the way out of it that costs nothing: the admin writes down what
they already know about their own files, and the check stops having to guess.

The file is **read, never written** by the plugin. It is the admin's document; generating it
would only produce something they then have to overwrite. A file that cannot be understood
is reported and then ignored — never repaired, never deleted, and never a reason to fail a
check.

Shape (every part optional; a bare ``{}`` is valid and means "no mappings")::

    {
      "version": 1,
      "by_sha1":   {"<40 hex characters>": "<project slug or id>"},
      "by_mod_id": {"<mod id>":              "<project slug or id>"}
    }

``by_mod_id`` is the more durable of the two: recompiling a mod changes its bytes but not its
id, which is exactly the case this file exists for. ``by_sha1`` wins when both are present,
because bytes identify one specific file while an id identifies everything that ever used it.

No MCDR import: this module takes a path and returns strings, so it is unit-testable.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from .downloads import is_safe_component

__all__ = ["ProjectMap", "MAP_FILE_NAME", "resolve_map_file"]

#: Default file name, inside the plugin's data folder.
MAP_FILE_NAME = "project-map.json"

#: Only this layout is understood. A file declaring anything else is left alone — a future
#: version may give those keys a different meaning, and guessing at them is how a plugin
#: silently reads a file it does not understand.
VERSION = 1

#: A SHA-1 is forty lowercase hex characters. Anything else is a typo, and a typo silently
#: ignored is a mapping the admin believes is in force but is not — so the count of rejected
#: entries is kept and reported.
_SHA1_LENGTH = 40
_HEX = set("0123456789abcdef")


def _is_sha1(text: str) -> bool:
    return len(text) == _SHA1_LENGTH and all(character in _HEX for character in text)


def resolve_map_file(base: Union[str, Path], name: str) -> Tuple[Optional[Path], str]:
    """The mapping file inside ``base``. Returns ``(path, reason)``; ``None`` means no.

    ``name`` is a **single file name, not a path**, for the same reason
    ``download.folder_name`` is: it makes it impossible to configure the plugin into reading
    somewhere it has no business reading, and it keeps the answer to "where does that setting
    look?" a single folder. An empty name means the feature is off, which is not an error.
    """
    text = str(name or "").strip()
    if not text:
        return None, "disabled"
    ok, reason = is_safe_component(text)
    if not ok:
        return None, reason
    return Path(base).expanduser() / text, ""


class ProjectMap:
    """``sha1``/``mod_id`` -> project, read once per check and never written.

    Every method is defensive. The file is hand-edited, so a trailing comma or a key with the
    wrong shape has to be a warning rather than a failed check — the same rule the download
    ledger follows.
    """

    VERSION = VERSION

    def __init__(self, path: Optional[Union[str, Path]], logger: Optional[Any] = None) -> None:
        self.path: Optional[Path] = Path(path) if path is not None else None
        self.logger = logger
        self._by_sha1: Dict[str, str] = {}
        self._by_mod_id: Dict[str, str] = {}
        #: Entries dropped for being unusable, so the warning can say how many.
        self.rejected = 0
        #: ``""`` when the file was read or absent; a reason when it could not be understood.
        self.error = ""
        self._load()

    # -- reading -----------------------------------------------------------------------

    def _debug(self, message: str) -> None:
        handler = getattr(self.logger, "debug", None) if self.logger else None
        if handler is not None:
            handler(message)

    @staticmethod
    def _section(data: Dict[str, Any], name: str) -> Dict[str, str]:
        """One ``{key: project}`` block, with unusable rows counted and dropped."""
        block = data.get(name)
        if block is None:
            return {}
        if not isinstance(block, dict):
            raise ValueError('"{}" must be an object'.format(name))
        return {str(key): value for key, value in block.items()}

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as error:
            self.error = "{}: {}".format(type(error).__name__, error)
            return
        try:
            data = json.loads(raw)
        except ValueError as error:
            self.error = "{}: {}".format(type(error).__name__, error)
            return
        if not isinstance(data, dict):
            self.error = "the file must contain a JSON object"
            return

        declared = data.get("version", self.VERSION)
        if declared != self.VERSION:
            self.error = "unsupported version {!r}".format(declared)
            return

        try:
            sha1_block = self._section(data, "by_sha1")
            mod_id_block = self._section(data, "by_mod_id")
        except ValueError as error:
            self.error = str(error)
            return

        for key, value in sha1_block.items():
            project = _project_of(value)
            normalised = key.strip().lower()
            if not project or not _is_sha1(normalised):
                self.rejected += 1
                continue
            self._by_sha1[normalised] = project

        for key, value in mod_id_block.items():
            project = _project_of(value)
            normalised = key.strip().lower()
            if not project or not normalised:
                self.rejected += 1
                continue
            self._by_mod_id[normalised] = project

    # -- queries -----------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def __len__(self) -> int:
        return len(self._by_sha1) + len(self._by_mod_id)

    @property
    def hashes(self) -> int:
        """How many mappings are keyed by SHA-1."""
        return len(self._by_sha1)

    @property
    def mod_ids(self) -> int:
        """How many mappings are keyed by mod id."""
        return len(self._by_mod_id)

    @property
    def loaded(self) -> bool:
        """True when at least one usable mapping was read."""
        return len(self) > 0

    def lookup(self, sha1: str = "", mod_id: str = "") -> str:
        """The project this file belongs to, or ``""`` when the map does not say.

        The hash is tried first: it names one specific file, while a mod id names everything
        that ever carried it — including a fork, which is the wrong answer if the admin has
        written down both.
        """
        digest = (sha1 or "").strip().lower()
        if digest:
            found = self._by_sha1.get(digest)
            if found:
                return found
        identifier = (mod_id or "").strip().lower()
        if identifier:
            return self._by_mod_id.get(identifier, "")
        return ""


def _project_of(value: Any) -> str:
    """Accept only a non-empty string as a project reference.

    Deliberately unvalidated beyond that: a slug and a project id are both accepted by the
    API, and this plugin has no business deciding which the admin meant. A value that turns
    out not to exist is reported when it is used, which is where it is actionable.
    """
    if not isinstance(value, str):
        return ""
    return value.strip()
