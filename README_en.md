# Mod Update Checker

**Language / 语言:** **English** | [简体中文](README.md)

> An MCDReforged plugin that checks the server's `mods/` folder for builds matching the running
> Minecraft version and loader. Update information comes from **Modrinth**.

[![CI](https://github.com/LifeSci-Craft/MCDR-ModUpdateChecker/actions/workflows/ci.yml/badge.svg)](https://github.com/LifeSci-Craft/MCDR-ModUpdateChecker/actions/workflows/ci.yml)
[![MCDR](https://img.shields.io/badge/MCDR-2.13.0%2B-blue)](https://mcdreforged.com/)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

| | |
|---|---|
| Plugin ID | `mod_update_checker` |
| Commands | `!!modupdate` / `!!muc` |
| Requires | MCDR **2.13.0+** (tested on 2.13.0 · 2.14.1 · 2.15.0 · 2.15.7 · 2.16.0) |
| License | MIT |

**Check and report only, by default**: nothing is downloaded and `mods/` is never touched. Both
downloading into the plugin folder and installing into `mods/` need an explicit opt-in or an
explicit command; installation happens only after the server has stopped, and the previous jar is
kept as a backup. No API key, no extra dependencies.

> Building or modifying the plugin, or looking for the decision rules and how they are verified?
> See [`README-dev.md`](README-dev.md) (Chinese).

## Contents

- [Features](#features)
- [Installation](#installation)
- [Commands](#commands)
- [Status](#status)
- [Configuration](#configuration)
- [Notes](#notes)
- [Credits](#credits)
- [License](#license)

## Features

- Identifies each jar by its SHA-1 and looks it up on Modrinth; when a hash is unknown it falls
  back to a project-slug search, and the report labels that as an approximate match
- Every mod lands in one of the states below (updatable / up to date / no compatible build /
  unresolved), and anything that needs action is always listed first
- Diagnoses the usual accidents along the way: the same mod installed twice, a required
  dependency that is missing, a client-only mod sitting in a server folder
- The check runs on its own background thread — 60 seconds after startup by default, or on a
  schedule — and hash lookups are batched (a hundred mods is usually a handful of requests)
- Optional download into the plugin folder, with hash verification (`download.enabled`)
- Optional install-on-stop into `mods/`, keeping the replaced jar as `.old`
  (`download.install_on_stop`)
- Old backups are visible and cleanable (see the `cleanup` options below), and by default they
  are shown but never deleted
- Console reporting by default; admin-on-join notices and in-game broadcasts are opt-in
- English and Simplified Chinese

A listing in game looks like this (the Chinese screenshot form is in [`README.md`](README.md)):

```
============  Mod Update Checker v1.6.2  ============
server: 26.3 / Fabric (version source: server_info)
[1] Lithium                 [version]  [status: up]  [details]
[2] Sodium                 [version]  [status: up]  [details]
[3] Iris                    [version]  [status: ok]  [details]
3 mod(s).
Checked 3 jar(s): 1 up to date, 2 with updates not yet downloaded, ...
one mod's detail (version change and links): !!muc info <number>.
=======  hover [version] [status], click [details]  =======
```

## Installation

1. Put `ModUpdateChecker-v*.mcdr` into MCDR's `plugins/` folder;
2. Run `!!MCDR reload plugin` (or restart MCDR);
3. The first load writes `config/mod_update_checker/config.json`; the defaults work as they are;
4. Run `!!muc check` for an immediate check, or wait for the automatic one 60 seconds after
   startup.

No extra Python dependencies: `requests` ships with MCDR and everything else is the standard
library.

## Commands

MCDR permission level 3 is required (configurable). `!!modupdate` and `!!muc` are equivalent; the
bare command opens the help page.

| Command | Effect |
|---|---|
| `!!modupdate` (= `!!modupdate help`) | Help page |
| `!!modupdate check` | Run a check now |
| `!!modupdate list [status] [page]` | List mods, optionally filtered by status, with paging |
| `!!modupdate summary` | Summary of the last check |
| `!!modupdate info <number / mod name>` | One mod's detail |
| `!!modupdate download <number / mod name / all>` | Fetch new builds; `all` = everything pending |
| `!!modupdate install <number / mod name / all>` | Schedule an install at the next stop (the file must be downloaded) |
| `!!modupdate delete <file name / number / all>` | Delete old backups (needs `cleanup.allow_delete`) |
| `!!modupdate cleanup` | Delete every expired old backup (same gate) |
| `!!modupdate confirm` | Confirm the previous `download` / `install` / `delete` / `cleanup` |
| `!!modupdate status` | Server, configuration and last-check state |
| `!!modupdate reload` | Reload the config file |

A target can be a **number** (from `list`), a **mod name** (a unique prefix is enough), a **mod
id**, or a **jar file name**; the console completes them with Tab.

`download` / `install` / `delete` / `cleanup` are two-step: the plan is shown first and carried
out by `!!modupdate confirm` (valid for 120 seconds, and only the requester can confirm it).

## Status

| Icon | Meaning |
|---|---|
| `✔` | Up to date (`up_to_date`) |
| `↑` | A newer build is available (`update_available`) — `!!modupdate download <number>` |
| `↓` | Downloaded, waiting to be installed (`awaiting_install`) — `!!modupdate install <number>` |
| `❌` | No build for this loader or game version (`no_compatible_build`) |
| `☆` | The local build is newer than upstream (`local_ahead`) |
| `?` | Modrinth does not know the jar (`unresolved`) — see `sources.manual_map` below |
| `○` | Not a mod (`not_a_mod`: a library, a datapack) |
| `✘` | Unreadable, or the lookup failed (`error`) |
| `⏪` | An old backup (`old_backup`) |
| `—` | Ignored through `check.ignored_mods` (`ignored`) |

A hash match is exact; a name-based match is a guess, and the report says so.

## Configuration

`config/mod_update_checker/config.json`. Options added by a newer version are filled in at
defaults and the file is rewritten. Out-of-range numbers are **clamped in memory** with one
console notice (the file itself is never edited, so the notice repeats until you fix it); the
range is stated next to each option. The tables in [`README.md`](README.md) list every option with
its full path, and the essentials are:

| Option | Default | Notes |
|---|---|---|
| `enabled` | `true` | Master switch; with it off only the commands remain |
| `language` | `"auto"` | `auto` follows MCDR; `zh_cn` / `en_us` pin it |
| `command_permission_level` | `3` | Minimum MCDR permission level (0–4) |
| `server.mods_directory` | `""` | Blank = `mods` under the server directory |
| `server.loader` | `"fabric"` | `fabric` / `quilt` / `neoforge` / `forge` |
| `server.mc_version` | `"auto"` | `auto` infers it; **set it explicitly when the guess is wrong** |
| `check.on_server_start` | `true` | One automatic check after startup |
| `check.start_delay_seconds` | `60` | Wait before that check (0–3600) |
| `check.interval_hours` | `0` | Periodic check interval; `0` = off (0–720) |
| `check.include_beta` / `check.include_alpha` | `false` | Count pre-releases as updates |
| `check.ignored_mods` | `[]` | Mods that are never checked |
| `report.updates_only` | `true` | One line when an automatic check finds nothing |
| `report.on_admin_join` | `true` | Notify an admin when they join |
| `report.admin_permission` | `3` | Who counts as an admin (0–4) |
| `report.reuse_report_minutes` | `1440` | How old a report may be to be reused; `0` = always re-check |
| `report.in_game` | `false` | Broadcast an update to online admins |
| `report.in_game_permission` | `3` | Minimum level for that broadcast (0–4) |
| `report.write_file` | `true` | Write `last_report.json` / `last_report.txt` |
| `sources.modrinth.enabled` | `true` | Query Modrinth |
| `sources.modrinth.api_base` | `""` | Blank = the official endpoint; a mirror works, and `HTTP_PROXY` / `HTTPS_PROXY` are honoured |
| `sources.manual_map` | `"project-map.json"` | Hand-written map in the plugin data folder (`""` disables it) |
| `download.enabled` | `false` | Download new builds into the plugin folder |
| `download.folder_name` | `"downloads"` | Sub-folder name (a name, not a path) |
| `download.max_size_mb` | `128` | Per-file size limit (1–8192) |
| `download.install_on_stop` | `false` | Install the downloads after the server stops |
| `download.retries` | `3` | Download retries (total attempts = 1 + this) |
| `cleanup.allow_delete` | `false` | Master switch for every deletion |
| `cleanup.enabled` | `false` | Remind an admin that backups expired |
| `cleanup.max_age_days` | `30` | Age at which a backup counts as expired; `0` = immediately (0–3650) |
| `network.timeout_seconds` | `20` | Per-request timeout (1–300) |
| `network.retries` | `3` | Retries on failure, including 429 |
| `network.concurrent_requests` | `4` | Concurrency (1–32) |
| `network.requests_per_minute` | `240` | Self-imposed rate limit (Modrinth allows 300/min; 0–1000) |
| `network.cache.enabled` | `true` | Remember "upstream does not know this jar" |
| `network.cache.ttl_hours` | `24` | How long that verdict lasts; `0` = never expires |

A jar you compiled or repacked yourself (so upstream cannot recognise its hash) can be mapped by
hand in `project-map.json`, for example `{"by_mod_id": {"mycustommod": "lithium"}}`: `by_mod_id`
matches the mod id (recommended — it survives a rebuild) and `by_sha1` matches the exact bytes.
The value is a project slug or id; run `!!modupdate reload` afterwards.

With `install_on_stop` on, the install always happens **after the server has stopped** and only
for files this plugin downloaded: the old jar is renamed to `<name>.jar.old` (`.old.2` next, never
overwriting a backup), the download is hash-verified before anything moves, a failure rolls back,
and an admin's bracket prefix such as `[Lithium]` is carried onto the new file name.

Old backups stay in the listing with status `⏪` and are never deleted until
`cleanup.allow_delete` is on; `delete` and `cleanup` both require `!!modupdate confirm`.

## Notes

- A repacked jar may not be recognised (its hash changed); map it with `project-map.json`.
- Jars nested inside other jars are not checked separately, so the "missing dependency"
  advisory can be a false positive when the dependency is bundled inside another jar.
- When Modrinth is unreachable the check still completes, the report says so, and no mod is
  reported as up to date on the strength of a failed request. A mirror or a system proxy is
  configurable.
- The game version is inferred. After a major upgrade, a wave of `no_compatible_build` usually
  means the inference is wrong: check `!!modupdate status` first, and set `server.mc_version`
  explicitly.
- An unreadable `mods/` folder (permissions, another program holding it) is reported as such by
  `!!modupdate status`, and the check is skipped with a reason rather than reported as
  "0 jars, all good".

## Credits

- [alex3236](https://github.com/alex3236) — maintainer of the MCDR plugin catalogue: reviewed this
  plugin's catalogue entry and helped tighten the `description` and the README.

## License

[MIT](LICENSE)
