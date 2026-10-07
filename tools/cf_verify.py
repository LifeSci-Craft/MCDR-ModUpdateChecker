#!/usr/bin/env python3
"""Check CurseForge fingerprint matching against the live API, using your own key.

The plugin's CurseForge support cannot be verified end to end without an API key, and there
is no way around that: every CurseForge endpoint rejects anonymous callers. What the test
suite does instead is prove the MurmurHash2 implementation agrees with an independent port;
what it cannot prove is that CurseForge agrees with *that*.

This script closes the loop. It is also the thing to run when the plugin reports every
CurseForge mod as unresolved: it says whether the fingerprints are wrong, whether the key is
wrong, or whether those particular jars simply are not on CurseForge.

Usage::

    # fingerprints of everything in the server's mods folder
    python tools/cf_verify.py --api-key '$2a$10$....' --mods-dir /path/to/server/mods

    # read the key and the mods directory out of the plugin's own config instead
    python tools/cf_verify.py --config /path/to/MCDR/config/mod_update_checker/config.json

A key is free: https://console.curseforge.com  (an account is required, no payment).

Exit codes: 0 = at least one jar matched, 1 = nothing matched or the request failed.
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from mod_update_checker.curseforge import CurseForgeClient  # noqa: E402
from mod_update_checker.scanner import iter_mod_jars, scan_mods  # noqa: E402
from mod_update_checker.upstream import HttpClient, UpstreamError  # noqa: E402

USER_AGENT = "Pau1am/MCDR-ModUpdateChecker (fingerprint verification tool)"


def load_from_config(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as error:
        raise SystemExit("could not read the config at {}: {}".format(path, error))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--api-key", default=os.environ.get("CURSEFORGE_API_KEY", ""))
    parser.add_argument("--mods-dir", default="")
    parser.add_argument("--config", default="", help="the plugin's config.json to read from")
    parser.add_argument("--limit", type=int, default=0, help="only inspect the first N jars")
    args = parser.parse_args()

    mods_dir = args.mods_dir
    api_key = args.api_key
    if args.config:
        config = load_from_config(Path(args.config).expanduser())
        api_key = api_key or str(config.get("curseforge_api_key") or "")
        mods_dir = mods_dir or str(config.get("mods_directory") or "")

    if not api_key:
        raise SystemExit(
            "no API key. Pass --api-key, set CURSEFORGE_API_KEY, or use --config.\n"
            "A free key is available at https://console.curseforge.com"
        )
    if not mods_dir:
        raise SystemExit("no mods directory. Pass --mods-dir (or --config to read it).")

    directory = Path(mods_dir).expanduser()
    jars, _disabled = iter_mod_jars(directory)
    if not jars:
        raise SystemExit("no jars found in {}".format(directory))
    if args.limit:
        jars = jars[: args.limit]

    print("Scanning {} jar(s) in {}".format(len(jars), directory))
    scan = scan_mods(directory)
    if args.limit:
        wanted = {path.name for path in jars}
        scan.mods = [mod for mod in scan.mods if mod.file_name in wanted]

    by_name = {mod.file_name: mod for mod in scan.mods}
    fingerprints = [mod.fingerprint for mod in scan.mods if mod.fingerprint]

    http = HttpClient(user_agent=USER_AGENT, timeout=30, retries=2)
    client = CurseForgeClient(http, api_key=api_key)
    try:
        try:
            matched = client.files_by_fingerprints(fingerprints)
        except UpstreamError as error:
            print("\nThe request failed: {}".format(error))
            print(
                "A 401 or 403 here means the key itself was rejected — check it at\n"
                "https://console.curseforge.com, and note that the key must be sent as the\n"
                "x-api-key header (this tool does that for you)."
            )
            return 1

        print()
        print("Matched {} of {} fingerprint(s).".format(len(matched), len(fingerprints)))
        print()

        unmatched = []
        for mod in scan.mods:
            found = matched.get(mod.fingerprint)
            if found is None:
                unmatched.append(mod)
                print("  UNMATCHED  {:<40} {:<18} {}".format(
                    mod.file_name, mod.mod_id or "?", mod.version or "?"))
                continue
            print("  matched    {:<40} {:<18} {}".format(
                mod.file_name, mod.mod_id or "?", mod.version or "?"))
            print("             curseforge modId={} fileId={} slug={}".format(
                found.mod_id, found.id, getattr(found, "file_name", "")))

        if unmatched:
            print()
            print("{} jar(s) are not on CurseForge.".format(len(unmatched)))
            print(
                "That is normal and not a bug: many mods are published only on Modrinth, and\n"
                "a jar built from source or re-signed will never match by fingerprint. The\n"
                "plugin reports those as unresolved rather than guessing."
            )
        if not matched:
            print()
            print(
                "Nothing matched at all. Before assuming the fingerprint code is wrong, check\n"
                "that these jars really are on CurseForge — test one by searching for it at\n"
                "https://www.curseforge.com/minecraft/mc-mods"
            )
            return 1
        return 0
    finally:
        http.close()


if __name__ == "__main__":
    sys.exit(main())
