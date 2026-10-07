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

    python tools/probe_upstream.py

Needs an interpreter with ``requests`` (any MCDR environment has it). Read-only apart from one
mod download; no credentials are needed anywhere in it
precisely to document what a key is needed *for*.
"""

import hashlib
import json
import sys

import requests

USER_AGENT = "Pau1am/MCDR-ModUpdateChecker-probe (server admin tool)"
MODRINTH = "https://api.modrinth.com/v2"

#: Big enough to be a plausible jar, small enough that the probe stays quick.
MAX_DOWNLOAD_BYTES = 3_000_000

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


def heading(title: str) -> None:
    print()
    print("=" * 78)
    print("### " + title)


def show(payload, limit: int = 900) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    print(text[:limit] + ("\n...[truncated]" if len(text) > limit else ""))


def demo_headers() -> None:
    print("request headers (Modrinth requires a descriptive User-Agent):")
    print("  " + json.dumps(dict(SESSION.headers), ensure_ascii=False))


def probe_modrinth() -> None:
    """Everything the Modrinth client depends on, verified against the live service."""
    heading("Modrinth — find a small Fabric mod to use as a specimen")
    response = SESSION.get(
        MODRINTH + "/search",
        params={
            "facets": json.dumps([["project_type:mod"], ["categories:fabric"]]),
            "limit": 12,
        },
        timeout=30,
    )
    print("GET /search ->", response.status_code)

    specimen = None
    for hit in response.json().get("hits", []):
        versions = SESSION.get(
            MODRINTH + "/project/{}/version".format(hit["project_id"]),
            params={"loaders": json.dumps(["fabric"])},
            timeout=30,
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

    heading("download it and confirm the digest matches what the API advertises")
    blob = SESSION.get(item["url"], timeout=120).content
    sha1 = hashlib.sha1(blob).hexdigest()
    sha512 = hashlib.sha512(blob).hexdigest()
    print("computed sha1 :", sha1)
    print("advertised    :", item["hashes"]["sha1"])
    print("sha1 matches  :", sha1 == item["hashes"]["sha1"])
    print("sha512 matches:", sha512 == item["hashes"]["sha512"])

    heading("POST /version_files — how does it answer for a hash it does not know?")
    response = SESSION.post(
        MODRINTH + "/version_files",
        json={"hashes": [sha1, "0" * 40, "f" * 40], "algorithm": "sha1"},
        timeout=30,
    )
    print("status:", response.status_code)
    body = response.json()
    print("keys returned:", list(body.keys()))
    print("-> only the hashes it recognises appear; unknown ones are simply absent")

    heading("POST /version_files/update — the batched 'newest compatible' lookup")
    for label, game_versions in (("current release", ["26.3"]), ("impossible version", ["1.7.10"])):
        response = SESSION.post(
            MODRINTH + "/version_files/update",
            json={
                "hashes": [sha1],
                "algorithm": "sha1",
                "loaders": ["fabric"],
                "game_versions": game_versions,
            },
            timeout=30,
        )
        payload = response.json()
        print("  {:20} status {} -> {!r}".format(label, response.status_code, payload)[:160])
    print("  -> no match gives an empty object, which is what the client keys off")

    heading("empty filter arrays — this is the one that silently breaks everything")
    response = SESSION.post(
        MODRINTH + "/version_files/update",
        json={"hashes": [sha1], "algorithm": "sha1", "loaders": ["fabric"], "game_versions": []},
        timeout=30,
    )
    print("  game_versions=[] -> status {} -> {} key(s) returned".format(
        response.status_code, len(response.json())))
    print(
        "  -> whether this means 'no filter' or 'match nothing' is exactly why the client\n"
        "     omits empty arrays entirely instead of sending them"
    )

    heading("GET /projects?ids= — batch metadata")
    response = SESSION.get(
        MODRINTH + "/projects", params={"ids": json.dumps([version["project_id"]])}, timeout=30
    )
    print("status:", response.status_code)
    project = response.json()[0]
    show({key: project.get(key) for key in (
        "id", "slug", "title", "project_type", "source_url", "client_side", "server_side")})

    heading("GET /project/{unknown} and /project/{id}/version with an impossible filter")
    print("unknown project        ->", SESSION.get(MODRINTH + "/project/nope-xyz", timeout=30).status_code)
    response = SESSION.get(
        MODRINTH + "/project/{}/version".format(version["project_id"]),
        params={"loaders": json.dumps(["fabric"]), "game_versions": json.dumps(["1.7.10"])},
        timeout=30,
    )
    print("impossible filter      -> {} {} (an empty list, not a 404)".format(
        response.status_code, response.json()))


def main() -> int:
    demo_headers()
    probe_modrinth()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
