#!/usr/bin/env python3
"""Probe the live Modrinth API and print what it actually does.

This started as a throwaway script to answer questions the docs do not, and it is kept
because those questions come back every time an upstream changes something:

* does ``POST /version_files`` include unrecognised hashes in its answer, or omit them?
* what does ``POST /version_files/update`` return when nothing matches the filter — ``{}``, or
  the raw hash with a null version? And does an empty ``game_versions`` array mean "no filter"
  or "match nothing"?
  unauthenticated path worth using?
* is the website's own ``/api/v1/`` reachable, which would make a key unnecessary?

Every one of those answers shapes the client code, and guessing one wrong produces a plugin
that reports "no compatible build" for every mod — a failure that looks exactly like a real
answer. So when something looks wrong in the field, run this first.

Usage::

    python tools/probe_upstream.py [--download-jar] [--timeout SECONDS]

Needs an interpreter with ``requests`` (any MCDR environment has it). Read-only unless
``--download-jar`` is given — the one download is off by default, so ``--help`` and a first look
cost no traffic. No credentials are needed anywhere in it, precisely to document what a key is
needed *for*.

Exit codes are three, because "the probe ran and the upstream is unreachable" and "the probe
itself is broken" call for different next steps: ``0`` all good, ``1`` a network failure that was
reported readably, ``2`` bad arguments. It used to accept no arguments at all — ``--help`` ran the
whole probe against the live API — and to die with a bare traceback when the network was down,
which is the least useful thing a diagnostic tool can do on a machine that has no route to
Modrinth.
"""

import argparse
import hashlib
import json
import sys

USER_AGENT = "LifeSci-Craft/MCDR-ModUpdateChecker-probe (server admin tool)"
MODRINTH = "https://api.modrinth.com/v2"

#: Big enough to be a plausible jar, small enough that the probe stays quick.
MAX_DOWNLOAD_BYTES = 3_000_000

#: ``requests`` is imported inside :func:`_session` rather than at the top so that ``--help``
#: works on an interpreter that does not have it — which is the interpreter a maintainer is most
#: likely to be holding when they run ``--help`` to find out what this tool needs.
SESSION = None


def _session():
    """The shared session, built on first use, or a readable refusal to go on."""
    global SESSION
    if SESSION is not None:
        return SESSION
    try:
        import requests
    except ImportError as error:
        print("this tool needs 'requests': python -m pip install requests")
        print("(any MCDR environment already has it — see tests/README.md for the local copy)")
        raise SystemExit(1) from error
    SESSION = requests.Session()
    SESSION.headers.update({"User-Agent": USER_AGENT})
    return SESSION


def _requests():
    """The ``requests`` module, for the exception type the failure path catches."""
    return sys.modules["requests"]

#: Set by the entry point; used for every request so ``--timeout`` reaches all of them.
TIMEOUT = 30.0

#: Whether to fetch a jar and check its digest. Off unless asked: it is the only request here
#: that moves real bytes, and someone reading ``--help`` should not be sending it.
DOWNLOAD_JAR = False


def _get(url, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT)
    return _session().get(url, **kwargs)


def _post(url, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT)
    return _session().post(url, **kwargs)


def heading(title: str) -> None:
    print()
    print("=" * 78)
    print("### " + title)


def show(payload, limit: int = 900) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    print(text[:limit] + ("\n...[truncated]" if len(text) > limit else ""))


def demo_headers() -> None:
    print("request headers (Modrinth requires a descriptive User-Agent):")
    session = _session()
    print("  " + json.dumps(dict(session.headers), ensure_ascii=False))


def probe_modrinth() -> None:
    """Everything the Modrinth client depends on, verified against the live service."""
    heading("Modrinth — find a small Fabric mod to use as a specimen")
    response = _get(
        MODRINTH + "/search",
        params={
            "facets": json.dumps([["project_type:mod"], ["categories:fabric"]]),
            "limit": 12,
        },
    )
    print("GET /search ->", response.status_code)

    specimen = None
    for hit in response.json().get("hits", []):
        versions = _get(
            MODRINTH + "/project/{}/version".format(hit["project_id"]),
            params={"loaders": json.dumps(["fabric"])},
        )
        if versions.status_code != 200:
            continue
        for version in versions.json():
            for item in version["files"]:
                if (
                    item.get("primary")
                    and item["size"] < MAX_DOWNLOAD_BYTES
                    and version["version_type"] == "release"
                ):
                    specimen = (hit, version, item)
                    break
            if specimen:
                break
        if specimen:
            break

    if specimen is None:
        print("no suitable specimen found; the rest of the Modrinth probe is skipped")
        return

    hit, version, item = specimen
    show({
        "slug": hit["slug"],
        "project_id": hit["project_id"],
        "version_number": version["version_number"],
        "version_type": version["version_type"],
        "loaders": version["loaders"],
        "recent_game_versions": version["game_versions"][-3:],
        "file": item["filename"],
        "size": item["size"],
    })

    if DOWNLOAD_JAR:
        heading("download it and confirm the digest matches what the API advertises")
        blob = _get(item["url"], timeout=max(TIMEOUT, 120.0)).content
        sha1 = hashlib.sha1(blob).hexdigest()
        sha512 = hashlib.sha512(blob).hexdigest()
        print("computed sha1 :", sha1)
        print("advertised    :", item["hashes"]["sha1"])
        print("sha1 matches  :", sha1 == item["hashes"]["sha1"])
        print("sha512 matches:", sha512 == item["hashes"]["sha512"])

        heading("POST /version_files — how does it answer for a hash it does not know?")
        response = _post(
            MODRINTH + "/version_files",
            json={"hashes": [sha1, "0" * 40, "f" * 40], "algorithm": "sha1"},
        )
        print("status:", response.status_code)
        body = response.json()
        print("keys returned:", list(body.keys()))
        print("-> only the hashes it recognises appear; unknown ones are simply absent")

        heading("POST /version_files/update — the batched 'newest compatible' lookup")
        for label, game_versions in (
            ("current release", ["26.3"]), ("impossible version", ["1.7.10"])
        ):
            response = _post(
                MODRINTH + "/version_files/update",
                json={
                    "hashes": [sha1],
                    "algorithm": "sha1",
                    "loaders": ["fabric"],
                    "game_versions": game_versions,
                },
            )
            payload = response.json()
            print("  {:20} status {} -> {!r}".format(label, response.status_code, payload)[:160])
        print("  -> no match gives an empty object, which is what the client keys off")

        heading("empty filter arrays — this is the one that silently breaks everything")
        response = _post(
            MODRINTH + "/version_files/update",
            json={"hashes": [sha1], "algorithm": "sha1", "loaders": ["fabric"],
                  "game_versions": []},
        )
        print("  game_versions=[] -> status {} -> {} key(s) returned".format(
            response.status_code, len(response.json())))
        print(
            "  -> whether this means 'no filter' or 'match nothing' is exactly why the client\n"
            "     omits empty arrays entirely instead of sending them"
        )
    else:
        heading("skipped: the jar download and the hash round-trip")
        print("  pass --download-jar to fetch one and check /version_files against its digest")

    heading("GET /projects?ids= — batch metadata")
    response = _get(
        MODRINTH + "/projects", params={"ids": json.dumps([version["project_id"]])}
    )
    print("status:", response.status_code)
    project = response.json()[0]
    show({key: project.get(key) for key in (
        "id", "slug", "title", "project_type", "source_url", "client_side", "server_side")})

    heading("GET /project/{unknown} and /project/{id}/version with an impossible filter")
    print("unknown project        ->", _get(MODRINTH + "/project/nope-xyz").status_code)
    response = _get(
        MODRINTH + "/project/{}/version".format(version["project_id"]),
        params={"loaders": json.dumps(["fabric"]), "game_versions": json.dumps(["1.7.10"])},
    )
    print("impossible filter      -> {} {} (an empty list, not a 404)".format(
        response.status_code, response.json()))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="probe_upstream.py",
        description=(
            "Probe the live Modrinth API and print what it answers. Read-only unless "
            "--download-jar is given."
        ),
        epilog=(
            "exit codes: 0 the probe ran; 1 the upstream could not be reached (the reason is "
            "printed); 2 bad arguments"
        ),
    )
    parser.add_argument(
        "--download-jar",
        action="store_true",
        help=(
            "also fetch one small jar and check its digest against the API's — the only "
            "request here that transfers real bytes, so it is off by default"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="per-request timeout (default: %(default)s); the jar download uses at least 120",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    global TIMEOUT, DOWNLOAD_JAR
    args = parse_args(argv)
    TIMEOUT = args.timeout
    DOWNLOAD_JAR = args.download_jar

    print("Modrinth base: {}  (timeout {:.0f}s, download-jar: {})".format(
        MODRINTH, TIMEOUT, "yes" if DOWNLOAD_JAR else "no"))
    try:
        demo_headers()
        probe_modrinth()
    except _requests().RequestException as error:
        # A readable conclusion rather than a traceback. This plugin is built for networks where
        # reaching Modrinth is the exception, so "the probe exploded" is the wrong report: what
        # the maintainer needs is which URL failed, how, and the two things worth trying.
        print()
        print("!" * 78)
        print("the upstream could not be reached: {}".format(type(error).__name__))
        print("  url    : {}".format(getattr(error.request, "url", "?")))
        print("  detail : {}".format(error))
        print("  this is a network problem, not a probe bug. Check the proxy/VPN settings for")
        print("  this shell, or raise --timeout, and run it again.")
        return 1
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
