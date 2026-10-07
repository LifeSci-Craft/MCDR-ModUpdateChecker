"""Mod Update Checker — an MCDR plugin that tells a server admin which mods are stale.

Fabric has no native notion of "is this mod out of date". The loader reads ``mods/``,
launches, and never asks whether a newer build exists — so the answer has to come from
outside, by comparing each jar against the two places mods actually live: Modrinth and
CurseForge.

This module is the MCDR-facing shell: configuration, commands, scheduling, notification, and
the translation of a :class:`~mod_update_checker.report.Report` into console lines and
in-game chat. All of the actual work lives in sibling modules that do not import MCDR at all,
which is what makes the logic testable without booting a server.

Nothing here ever modifies ``mods/``. The plugin reports; the admin decides.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence, Set, Tuple

from mcdreforged.api.all import (
    CommandSource,
    GreedyText,
    Literal,
    PluginServerInterface,
    RColor,
    RText,
    Serializable,
)

from . import i18n
from .checker import USER_AGENT, CheckOptions, Checker
from .downloads import (
    STATUS_ALREADY_PRESENT,
    STATUS_DOWNLOADED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    DownloadOptions,
    DownloadOutcome,
    Downloader,
    resolve_folder as resolve_download_folder_path,
)
from .report import ALL_STATUSES, Report, render_entry_line, render_full, render_summary
from .upstream import HttpClient
from .scanner import (
    ScanResult,
    iter_mod_jars,
    resolve_mods_directory,
    scan_mods,
)
from .serverinfo import detect as detect_server_context

CONFIG_FILE_NAME = "config.json"
REPORT_FILE_NAME = "last_report.json"
REPORT_TEXT_FILE_NAME = "last_report.txt"
CACHE_FILE_NAME = "resolve-cache.json"

#: Both spellings are registered so an admin does not have to guess which one is canonical.
ROOT_LITERALS = ("!!modupdate", "!!muc")

#: Cap on how many mods are listed in a one-shot notification, so a server with 150 stale
#: mods does not dump 150 lines into the chat every restart. The full list is one command
#: away.
NOTIFY_MAX_UPDATES = 15
#: How many download outcomes to print individually before summarising the rest. The download
#: folder is for a human to look at, so the lines are worth printing — but not two hundred
#: of them on a big modpack.
DOWNLOAD_LOG_LIMIT = 20


class Config(Serializable):
    language: str = i18n.AUTO
    """消息语言。``auto`` = 跟随 MCDR 的 language 设置；也可写 zh_cn / en_us。"""

    enabled: bool = True
    """总开关。关掉后只保留命令，不做任何自动检查。"""

    mods_directory: str = ""
    """mods 目录。留空 = 服务端工作目录下的 ``mods``。相对路径按服务端目录解析。"""

    loader: str = "fabric"
    """要过滤的加载器：fabric / quilt / neoforge / forge。"""

    mc_version: str = "auto"
    """Minecraft 版本。``auto`` = 从服务端输出、日志、Mod 元数据依次推断。"""

    check_on_server_start: bool = True
    """服务端启动完成后自动检查一次。"""

    start_check_delay_seconds: int = 60
    """开服后延迟多少秒再检查，避免和 Mod 加载抢资源。"""

    check_interval_hours: int = 0
    """定时检查间隔（小时）。``0`` = 关闭定时检查。"""

    notify_on_updates_only: bool = True
    """自动检查时只在「有需要处理的项」时才输出完整提醒，否则只留一行。"""

    notify_in_game: bool = False
    """是否在游戏内向在线管理员发 tellraw。默认关闭，避免打扰玩家。"""

    notify_in_game_permission: int = 3
    """游戏内提醒的最低 MCDR 权限等级。"""

    check_on_admin_join: bool = True
    """管理员上线时自动检查一次，并把结果发给他。"""

    admin_join_permission: int = 3
    """多少权限等级算「管理员」。MCDR 等级 3 = admin，2 = helper。"""

    admin_join_max_report_age_minutes: int = 30
    """管理员上线时，多久以内的上次检查结果可以直接复用而不重新查。

    ``0`` = 每次都重新检查。默认复用是为了两件事：管理员一进服**马上**就能看到结果，
    而不用等一次完整扫描；以及避免几位管理员接连上线时反复打接口。"""

    write_report_file: bool = True
    """把每次检查的结果写成 JSON / 文本文件，便于外部脚本或事后排查。"""

    download_updates: bool = False
    """发现更新时，自动把新版本从 Modrinth 下载到插件数据文件夹的子文件夹里。默认关闭。

    只是**下载**，不会装进 ``mods/``——把没看过的 jar 直接塞进运行中的服务端，正是本插件
    想避免的事。下载下来的文件由你自行检查后手动替换。"""

    download_folder_name: str = "downloads"
    """下载到哪个子文件夹。这里填的是**单个文件夹名，不是路径**（如 ``downloads``）。

    刻意不允许填路径：这样无论如何配置都不可能写到插件数据文件夹之外，也就不可能被配置成
    直接写进 ``server/mods``。"""

    download_max_size_mb: int = 128
    """单个文件的大小上限（MB）。超过就跳过并说明原因。"""

    include_beta: bool = False
    """是否把 beta 版本也算作「可用更新」。默认只认正式版。"""

    include_alpha: bool = False
    """是否把 alpha 版本也算作「可用更新」。"""

    use_modrinth: bool = True
    """启用 Modrinth 查询（不需要 API key，按文件哈希精确匹配）。"""

    use_curseforge: bool = True
    """启用 CurseForge 查询（需要 API key，按指纹精确匹配）。"""

    modrinth_api_base: str = ""
    """Modrinth API 地址。留空 = 官方 ``https://api.modrinth.com/v2``；可改成镜像。"""

    curseforge_api_base: str = ""
    """CurseForge API 地址。留空 = 官方 ``https://api.curseforge.com/v1``。"""

    curseforge_api_key: str = ""
    """CurseForge API key（https://console.curseforge.com 免费申请）。留空则跳过 CurseForge。"""

    ignored_mods: List[str] = []
    """要忽略的 Mod：可填 mod id、jar 文件名或去掉扩展名的文件名（不区分大小写）。"""

    http_timeout_seconds: int = 20
    """单次 HTTP 请求超时（秒）。"""

    http_retries: int = 3
    """失败重试次数（含 429 限流与网络错误）。"""

    concurrent_requests: int = 4
    """并发请求数。调大更快也更容易触发上游限流。"""

    requests_per_minute: int = 240
    """自限速：每分钟最多多少次请求。Modrinth 官方上限是 300，留出余量。"""

    use_resolve_cache: bool = True
    """缓存「某个哈希属于哪个项目」，避免每次开服重复解析。"""

    resolve_cache_ttl_hours: int = 24
    """识别缓存的有效期（小时）。0 表示永不过期；要彻底关闭请用 ``use_resolve_cache``。"""

    command_permission_level: int = 3
    """执行 ``!!modupdate`` 所需的最低 MCDR 权限等级。"""


# --------------------------------------------------------------------------------------
# Module state
#
# MCDR reloads a plugin by constructing a fresh module, so anything that should survive
# ``!!MCDR reload plugin`` is deliberately kept in module globals and carried over in
# ``on_load`` — most importantly the last report, which an admin will be looking at.
# --------------------------------------------------------------------------------------

_config: Config = Config.get_default()
_translator = None
_server: Optional[PluginServerInterface] = None
_last_report: Optional[Report] = None
_online_players: Set[str] = set()
_check_lock = threading.Lock()
_stop_event = threading.Event()
_scheduler_thread: Optional[threading.Thread] = None


def tr(key: str, **kwargs: Any) -> str:
    """Translate in the plugin's configured language."""
    if _translator is None:
        return i18n.translate(key, **kwargs)
    return _translator(key, **kwargs)


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


def _config_path(server: PluginServerInterface) -> str:
    return os.path.join(server.get_data_folder(), CONFIG_FILE_NAME)


def _load_config(server: PluginServerInterface) -> Config:
    """Load the config, keeping a backup if the file had to be rebuilt.

    MCDR's default ``failure_policy='regen'`` silently replaces an unparseable config with
    defaults. Silent is the problem: an admin who fat-fingered a comma would see their
    settings vanish with no explanation, so the old file is preserved and the reason logged.
    """
    path = _config_path(server)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise ValueError("config root must be a JSON object")
        except (OSError, ValueError) as error:
            backup = "{}.broken.{}".format(path, time.strftime("%Y%m%d-%H%M%S"))
            try:
                os.replace(path, backup)
            except OSError:
                backup = path
            server.logger.warning(
                tr("console.config_invalid", backup=backup) + " ({})".format(error)
            )

    config = server.load_config_simple(
        CONFIG_FILE_NAME, target_class=Config, echo_in_console=False
    )
    return config if isinstance(config, Config) else Config.get_default()


def _apply_language(server: PluginServerInterface, config: Config) -> None:
    """Pick a language and warn once if the configured one is not shipped."""
    mcdr_language = None
    getter = getattr(server, "get_mcdr_language", None)
    if callable(getter):
        try:
            mcdr_language = getter()
        except Exception:  # noqa: BLE001 - an older MCDR simply has no such API
            mcdr_language = None

    choice = i18n.resolve(config.language, mcdr_language)
    global _translator
    _translator = i18n.make_translator(choice.language)
    if choice.note_key:
        server.logger.warning(tr(choice.note_key, **choice.note_args))


def _check_options(config: Config, mc_version: Optional[str], loader: str) -> CheckOptions:
    return CheckOptions(
        loader=loader,
        mc_version=mc_version,
        include_beta=config.include_beta,
        include_alpha=config.include_alpha,
        use_modrinth=config.use_modrinth,
        use_curseforge=config.use_curseforge,
        modrinth_base=config.modrinth_api_base,
        curseforge_base=config.curseforge_api_base,
        curseforge_api_key=config.curseforge_api_key,
        ignored_mods=list(config.ignored_mods),
        timeout=max(1.0, float(config.http_timeout_seconds)),
        retries=max(0, int(config.http_retries)),
        workers=max(1, int(config.concurrent_requests)),
        requests_per_minute=max(0, int(config.requests_per_minute)),
        use_cache=bool(config.use_resolve_cache),
        cache_ttl_hours=max(0.0, float(config.resolve_cache_ttl_hours)),
    )


def _scan_current(server: PluginServerInterface, config: Config) -> Tuple[ScanResult, Any]:
    """Scan the mods folder and resolve the server context. Reads only."""
    working_directory = _working_directory(server)
    directory = resolve_mods_directory(working_directory, config.mods_directory)
    scan = scan_mods(directory, logger=server.logger)

    information_version = None
    try:
        information_version = server.get_server_information().version
    except Exception:  # noqa: BLE001 - offline servers have nothing to report
        information_version = None

    context = detect_server_context(
        working_directory=working_directory,
        configured_version=config.mc_version,
        configured_loader=config.loader,
        server_information_version=information_version,
        scan=scan,
    )
    return scan, context


def _working_directory(server: PluginServerInterface) -> str:
    """MCDR's configured Minecraft server folder."""
    try:
        mcdr_config = server.get_mcdr_config()
    except Exception:  # noqa: BLE001 - fall back to MCDR's own directory
        return "."
    if isinstance(mcdr_config, dict):
        value = mcdr_config.get("working_directory")
        if isinstance(value, str) and value:
            return value
    return "."


# --------------------------------------------------------------------------------------
# Running a check
# --------------------------------------------------------------------------------------


def _run_check(
    server: PluginServerInterface,
    source: Optional[CommandSource] = None,
    announce_clean: bool = True,
    broadcast: bool = True,
) -> Optional[Report]:
    """Scan, query and report. Safe to call from any thread; only one runs at a time."""
    global _last_report
    if not _check_lock.acquire(blocking=False):
        if source is not None:
            source.reply(tr("command.check_already_running"))
        return None

    try:
        config = _config
        scan, context = _scan_current(server, config)
        if not os.path.isdir(scan.directory):
            message = tr("console.no_mods_directory", directory=scan.directory)
            if source is not None:
                source.reply(message)
            else:
                server.logger.warning(message)
            return None

        checker = Checker(
            _check_options(config, context.mc_version, context.loader), logger=server.logger
        )
        # ``Path`` rather than the ``os.path.join`` result: the cache is a filesystem object
        # and every method on it uses the pathlib API. Passing a bare string here is what
        # made the shipped default config crash on the first check, so the type is made
        # explicit at the boundary rather than left to whatever the join returned.
        cache_path = (
            Path(server.get_data_folder()) / CACHE_FILE_NAME
            if config.use_resolve_cache
            else None
        )
        report = checker.run(scan, context, cache_path=cache_path)
        _last_report = report

        _notify(server, report, source=source, announce_clean=announce_clean,
                broadcast=broadcast)

        # After the report, not before: a fetch can take a while, and the admin should not
        # have to wait for it to see what was found. Its outcome is logged as its own block,
        # and the report files are written afterwards so they carry the download notes too.
        if config.download_updates:
            _download_updated_mods(server, report, config)

        if config.write_report_file:
            _write_report_files(server, report)
        return report
    except Exception as error:  # noqa: BLE001 - a check must never take the server down
        server.logger.exception("mod update check failed")
        message = tr("check.finished_failed", error="{}: {}".format(type(error).__name__, error))
        if source is not None:
            source.reply(message)
        else:
            server.logger.error(message)
        return None
    finally:
        _check_lock.release()


def _write_report_files(server: PluginServerInterface, report: Report) -> None:
    """Persist the report as JSON (for scripts) and as text (for a human)."""
    folder = server.get_data_folder()
    targets = (
        (REPORT_FILE_NAME, report.to_json()),
        (REPORT_TEXT_FILE_NAME, "\n".join(render_full(report, tr)) + "\n"),
    )
    for name, content in targets:
        path = os.path.join(folder, name)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as error:
            server.logger.warning("could not write {}: {}".format(path, error))
    server.logger.info(tr("console.report_saved", path=os.path.join(folder, REPORT_FILE_NAME)))


def _notify(
    server: PluginServerInterface,
    report: Report,
    source: Optional[CommandSource] = None,
    announce_clean: bool = True,
    broadcast: bool = True,
) -> None:
    """Print the result to the console, to the invoker, and optionally into the game."""
    lines = render_summary(report, tr)

    # The console is the primary channel: an automatic check the admin did not trigger still
    # has to be visible in the log. When the check is configured to speak up only about
    # problems, a clean run is reduced to a single line — silence would be indistinguishable
    # from a check that never ran.
    if report.actionable_count or announce_clean:
        # Plain strings, not RText: MCDR's log formatter runs the message through ``str()``
        # and ``RTextBase.__str__`` returns plain text, so colour logged here would be
        # silently discarded. See ``_coloured_line`` — colour is applied on the reply path,
        # which is the one that actually renders it.
        for line in lines:
            server.logger.info(line)
    else:
        server.logger.info(tr("check.finished_clean"))

    if source is not None:
        _reply_lines(source, lines)

    if broadcast and _config.notify_in_game and report.has_updates:
        _notify_in_game(server, report)


def _download_updated_mods(
    server: PluginServerInterface, report: Report, config: Config
) -> None:
    """Fetch the newer builds, if the admin asked for that.

    Runs inside the check, on the check's own thread, and after the report has been announced.
    That order matters: the admin sees the findings immediately instead of waiting for a few
    hundred megabytes to arrive, and the download progress then shows up as its own log lines
    rather than being folded into the report they already read.

    Wholly self-contained. The check has already succeeded by the time this runs, so nothing in
    here — a misconfigured folder, an unreachable host, a full disk, a bug in the summary
    formatting — may turn a successful check into a reported failure.
    """
    http: Optional[HttpClient] = None
    try:
        folder, reason = resolve_download_folder(server, config)
        if folder is None:
            server.logger.warning(
                tr("download.bad_folder", name=config.download_folder_name, reason=reason)
            )
            return

        options = DownloadOptions(
            folder=folder,
            max_bytes=max(1, int(config.download_max_size_mb)) * 1024 * 1024,
        )
        http = _make_http_client(config)
        outcomes = Downloader(http, options, logger=server.logger).run(report.entries)
        _log_download_outcomes(server, report, outcomes, folder)
    except Exception as error:  # noqa: BLE001 - see the docstring
        server.logger.warning(tr("download.crashed", error="{}: {}".format(
            type(error).__name__, error)))
    finally:
        if http is not None:
            http.close()


def resolve_download_folder(
    server: PluginServerInterface, config: Config
) -> Tuple[Optional[Path], str]:
    """Where the downloads go, or ``None`` plus a reason.

    The folder is always a single named subfolder of the plugin's own data folder — never a
    path, and never inside the server directory. That is a deliberate constraint rather than a
    limitation: it makes it impossible to configure this plugin into writing jars straight into
    ``server/mods``, which would load code nobody has reviewed.
    """
    try:
        base = Path(server.get_data_folder())
    except Exception as error:  # noqa: BLE001 - an unusable data folder is worth reporting
        return None, "no-data-folder: {}".format(error)
    return resolve_download_folder_path(base, config.download_folder_name)


def _make_http_client(config: Config) -> HttpClient:
    """A client for the download host, mirroring the checker's settings.

    Its own client, not the checker's, for two reasons: the checker closes its session when it
    finishes, and the two want different tuning. A JSON call should give up in seconds; a
    multi-megabyte transfer over a slow link should not be cut off at the same timeout.
    ``retries=0`` because :meth:`HttpClient.download` does not retry mid-stream — a partial
    transfer is discarded and the next check tries again.
    """
    return HttpClient(
        user_agent=USER_AGENT,
        timeout=max(30.0, float(config.http_timeout_seconds)),
        retries=0,
        logger=None,
    )


def _log_download_outcomes(
    server: PluginServerInterface,
    report: Report,
    outcomes: Sequence[DownloadOutcome],
    folder: Path,
) -> None:
    """Say what landed, what was already there, and what did not work — and why."""
    if not outcomes:
        return

    counts = {status: 0 for status in (STATUS_DOWNLOADED, STATUS_ALREADY_PRESENT,
                                      STATUS_SKIPPED, STATUS_FAILED)}
    by_file: Dict[str, DownloadOutcome] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
        by_file[outcome.file_name] = outcome

    server.logger.info(
        tr(
            "download.summary",
            downloaded=counts[STATUS_DOWNLOADED],
            existing=counts[STATUS_ALREADY_PRESENT],
            failed=counts[STATUS_FAILED],
            skipped=counts[STATUS_SKIPPED],
            folder=str(folder),
        )
    )

    shown = 0
    for outcome in outcomes:
        if outcome.status == STATUS_SKIPPED:
            continue
        if shown >= DOWNLOAD_LOG_LIMIT:
            server.logger.info(
                tr("download.and_more", count=len(outcomes) - shown)
            )
            break
        shown += 1
        if outcome.status == STATUS_DOWNLOADED:
            server.logger.info(
                tr("download.line_done", name=outcome.name, size=_format_size(outcome.bytes_written),
                   path=outcome.path)
            )
        elif outcome.status == STATUS_ALREADY_PRESENT:
            server.logger.info(tr("download.line_existing", name=outcome.name, path=outcome.path))
        else:
            server.logger.warning(
                tr("download.line_failed", name=outcome.name, reason=outcome.detail)
            )

    # Skips are summarised rather than listed one by one: on a server where most mods are only
    # on CurseForge, the list would otherwise be the bulk of the output.
    skipped_reasons = sorted({outcome.detail for outcome in outcomes
                              if outcome.status == STATUS_SKIPPED})
    if skipped_reasons:
        server.logger.info(tr("download.skipped_reasons", reasons=", ".join(skipped_reasons)))

    for outcome in outcomes:
        entry = _entry_for(report, outcome.file_name)
        if entry is None:
            continue
        if outcome.status == STATUS_DOWNLOADED:
            entry.add_note("note.downloaded", path=outcome.path)
        elif outcome.status == STATUS_ALREADY_PRESENT:
            entry.add_note("note.download_already_present", path=outcome.path)
        elif outcome.status == STATUS_FAILED:
            entry.add_note("note.download_failed", reason=outcome.detail)
        else:
            entry.add_note("note.download_skipped", reason=outcome.detail)


def _entry_for(report: Report, file_name: str):
    for entry in report.entries:
        if entry.file_name == file_name:
            return entry
    return None


def _format_size(count: int) -> str:
    """Bytes as something a human reads at a glance."""
    if count >= 1024 * 1024:
        return "{:.1f} MB".format(count / (1024.0 * 1024.0))
    if count >= 1024:
        return "{:.0f} KB".format(count / 1024.0)
    return "{} B".format(count)


def _coloured_line(line: str) -> RText:
    """Colour a summary line for a command *reply*.

    Only for ``source.reply``. MCDR does render an RText there — ``StdoutReplier.reply`` calls
    ``to_colored_text()`` for the console, and a player receives it as a chat component.

    The same RText must NOT be handed to ``server.logger.info``, which is why the console path
    above logs plain strings on purpose. Logging an RText looks like it colours the output and
    quietly does not, so the asymmetry is deliberate rather than an oversight.
    """
    if "->" in line:
        colour = RColor.yellow
    elif line.startswith("  "):
        colour = RColor.gray
    else:
        colour = RColor.white
    return RText(line, colour)


def _reply_lines(source: CommandSource, lines) -> None:
    for line in lines:
        source.reply(_coloured_line(line))


def _notification_lines(report: Report) -> List[str]:
    """The body of an in-game notification, in both the broadcast and the on-join case."""
    if not report.has_updates:
        return [tr("report.no_updates")]
    lines = [tr("check.in_game_header", count=len(report.updates))]
    for entry in report.updates[:NOTIFY_MAX_UPDATES]:
        lines.append(tr("line.update", name=entry.name, local=entry.local_version or "?",
                        latest=entry.latest_version or "?"))
    if len(report.updates) > NOTIFY_MAX_UPDATES:
        lines.append(tr("report.and_more", count=len(report.updates) - NOTIFY_MAX_UPDATES))
    return lines


def _tell_player(server: PluginServerInterface, player: str, lines: List[str]) -> None:
    """Send a few lines to one player.

    ``server.tell`` rather than a hand-built ``tellraw``: it goes through the active handler's
    own "send message" command (so it is right for whatever handler the server runs, not just
    the vanilla-derived ones), it escapes the payload, and it uses the receiving player's
    preferred language. Delivery is best-effort — the player may have disconnected while a
    check was running.
    """
    if not _server_running(server):
        return
    try:
        server.tell(player, RText("\n".join(lines), RColor.yellow))
    except Exception as error:  # noqa: BLE001 - a failed message must not break anything
        server.logger.debug("could not message {}: {}".format(player, error))


def _notify_in_game(server: PluginServerInterface, report: Report) -> None:
    """Send a short notification to the online players who are allowed to see it.

    Only players already tracked as online from join/leave events are targeted, and only
    those whose MCDR permission level is high enough — broadcasting to everybody would
    train players to ignore the message.
    """
    if not (_server_running(server) and _online_players):
        return

    lines = _notification_lines(report)
    permitted = _permitted_players(
        server, sorted(_online_players), _config.notify_in_game_permission
    )
    for name in permitted:
        _tell_player(server, name, lines)


def _permitted_players(
    server: PluginServerInterface, players: Sequence[str], required: int
) -> List[str]:
    """The players among ``players`` whose MCDR permission level reaches ``required``.

    The threshold is a parameter rather than read from the config inside, because the two
    callers genuinely mean different things: ``notify_in_game_permission`` is "who may be
    told about updates", while ``admin_join_permission`` is "who counts as an admin worth
    waking up for". Folding them into one setting would make the option that is no longer
    read look like it still works.

    A permission lookup can throw for a player MCDR does not know about (a name that never
    joined, or a permission file mid-edit), so each one is guarded individually: one
    unresolvable name must not stop the others from being told.
    """
    threshold = max(0, int(required))
    permitted: List[str] = []
    for name in players:
        try:
            if server.get_permission_level(name) >= threshold:
                permitted.append(name)
        except Exception as error:  # noqa: BLE001 - an unknown player is simply skipped
            server.logger.debug("no permission level for {}: {}".format(name, error))
    return permitted


def _admin_join_worker(server: PluginServerInterface, player: str) -> None:
    """Check if needed, then message the admin who just came online.

    Two decisions worth stating, because neither is obvious:

    * **A recent report is reused rather than re-run.** The point is for the admin to *learn*
      about mod updates when they log in, so an answer from ten minutes ago serves that goal
      better than a fresh scan: it arrives instantly instead of after a full read of ``mods/``,
      and three admins logging in together do not each fire a round of API calls. The window
      is ``admin_join_max_report_age_minutes``; set it to ``0`` to always re-check.
    * **A check that could not start still answers.** If another check holds the lock, the
      admin gets the previous report with its age, which beats silence.
    """
    if _stop_event.is_set():
        return

    report = _last_report
    window_minutes = max(0, int(_config.admin_join_max_report_age_minutes))
    age = report.age_seconds() if report is not None else None
    reused = age is not None and 0 < age <= window_minutes * 60

    if not reused:
        # No broadcast here: the admin who just joined is about to get the same figures in
        # their own message, and everyone else online was told when the previous check ran.
        fresh = _run_check(server, announce_clean=not _config.notify_on_updates_only,
                           broadcast=False)
        if fresh is not None:
            report, age, reused = fresh, 0.0, False

    if _stop_event.is_set():
        return
    if report is None:
        _tell_player(server, player, [tr("check.admin_join_no_report")])
        return

    lines = [tr("check.admin_join_header", version=report.server.describe())]
    lines.extend(_notification_lines(report))
    if reused:
        lines.append(tr("check.admin_join_reused", minutes=int((age or 0) // 60)))
    _tell_player(server, player, lines)


def on_player_joined(server: PluginServerInterface, player: str, info: Any) -> None:
    _online_players.add(player)

    if not (_config.enabled and _config.check_on_admin_join):
        return

    # The permission check is cheap and happens here; the check itself goes to its own thread,
    # because this runs on MCDR's event thread and a check reads and hashes every jar.
    if not _permitted_players(server, [player], _config.admin_join_permission):
        return

    threading.Thread(
        target=_admin_join_worker,
        args=(server, player),
        name="mod_update_checker_admin_join",
        daemon=True,
    ).start()


def on_player_left(server: PluginServerInterface, player: str) -> None:
    _online_players.discard(player)


def _server_running(server: PluginServerInterface) -> bool:
    probe = getattr(server, "is_server_running", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------------------


def _start_interval_scheduler(server: PluginServerInterface) -> None:
    """One daemon thread that owns the periodic check.

    A daemon thread rather than MCDR's ``schedule_task``: the loop has to be cancellable on
    unload, and it must not accumulate another thread every time the plugin is reloaded.

    The after-startup check is *not* here — it lives in ``on_server_startup`` so that
    restarting the server through MCDR triggers it again, which is exactly when an admin
    wants to know whether the mods they just swapped in are current.
    """
    global _scheduler_thread
    # Cleared here, not only in on_load: ``!!modupdate reload`` reuses this module and stops
    # the old thread first, so a stale set event would make the new loop exit immediately and
    # silently kill interval checking until the next MCDR restart.
    _stop_event.clear()

    interval_hours = max(0, int(_config.check_interval_hours))
    if not _config.enabled or interval_hours <= 0:
        return

    def loop() -> None:
        server.logger.info(tr("console.interval_enabled", hours=interval_hours))
        while not _stop_event.wait(interval_hours * 3600):
            _run_check(server, announce_clean=not _config.notify_on_updates_only)

    _scheduler_thread = threading.Thread(
        target=loop, name="mod_update_checker_scheduler", daemon=True
    )
    _scheduler_thread.start()


def _schedule_startup_check(server: PluginServerInterface) -> None:
    """Run one check a little while after the server finishes starting."""
    delay = max(0, int(_config.start_check_delay_seconds))
    if delay:
        server.logger.info(tr("console.check_scheduled", seconds=delay))

    def once() -> None:
        if _stop_event.wait(delay):
            return
        _run_check(server, announce_clean=not _config.notify_on_updates_only)

    threading.Thread(
        target=once, name="mod_update_checker_startup_check", daemon=True
    ).start()


def _stop_scheduler() -> None:
    global _scheduler_thread
    _stop_event.set()
    thread = _scheduler_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)
    _scheduler_thread = None


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def _has_permission(source: CommandSource) -> bool:
    try:
        return source.has_permission(max(0, int(_config.command_permission_level)))
    except Exception:  # noqa: BLE001 - a source without permissions is denied
        return False


def _denied(_source: CommandSource) -> str:
    return tr("command.permission_denied", level=_config.command_permission_level)


def _show_summary(source: CommandSource) -> None:
    if _last_report is None:
        source.reply(tr("command.no_report_yet"))
        return
    _reply_lines(source, render_summary(_last_report, tr))


def _show_full(source: CommandSource) -> None:
    if _last_report is None:
        source.reply(tr("command.no_report_yet"))
        return
    _reply_lines(source, render_full(_last_report, tr))


def _show_filtered(source: CommandSource, status: str) -> None:
    wanted = (status or "").strip().lower()
    if wanted not in ALL_STATUSES:
        source.reply(tr("command.unknown_status", value=status, options=", ".join(ALL_STATUSES)))
        return
    if _last_report is None:
        source.reply(tr("command.no_report_yet"))
        return
    entries = _last_report.by_status(wanted)
    source.reply(tr("report.header", version=_last_report.server.describe(),
                    source=_last_report.server.mc_version_source))
    source.reply("  [{}] {}".format(wanted, len(entries)))
    for entry in sorted(entries, key=lambda item: item.name.lower()):
        source.reply(_coloured_line(render_entry_line(entry, tr, verbose=True)))


def _show_status(source: CommandSource) -> None:
    server = _server
    if server is None:
        return
    scan, context = _scan_current(server, _config)
    source.reply(tr("command.status.header"))
    source.reply(tr("command.status.server", version=context.mc_version or "?",
                    loader=context.loader, source=context.mc_version_source))
    source.reply(tr("command.status.mods_dir", directory=scan.directory,
                    count=len(scan.mods)))
    if _config.use_modrinth:
        modrinth_state = tr("command.status.enabled")
    else:
        modrinth_state = tr("command.status.disabled")
    if not _config.use_curseforge:
        curseforge_state = tr("command.status.disabled")
    elif (_config.curseforge_api_key or "").strip():
        curseforge_state = tr("command.status.enabled")
    else:
        curseforge_state = tr("command.status.disabled_no_key")
    source.reply(tr("command.status.upstream", modrinth=modrinth_state,
                    curseforge=curseforge_state))
    if _last_report is None:
        source.reply(tr("command.status.never_checked"))
    else:
        source.reply(tr("command.status.last_report", when=_last_report.generated_at,
                        actionable=_last_report.actionable_count))
    source.reply(tr(
        "command.status.scheduling",
        on_start=tr("command.status.yes") if _config.check_on_server_start
        else tr("command.status.no"),
        hours=_config.check_interval_hours,
    ))


def _reload_config(source: CommandSource) -> None:
    server = _server
    if server is None:
        return
    global _config
    _config = _load_config(server)
    _apply_language(server, _config)
    _stop_scheduler()
    _start_interval_scheduler(server)
    source.reply(tr("console.config_reloaded"))


def _show_help(source: CommandSource) -> None:
    source.reply(tr("command.help.title"))
    for key in (
        "command.help.summary",
        "command.help.check",
        "command.help.list",
        "command.help.status",
        "command.help.reload",
        "command.help.help",
    ):
        source.reply(tr(key))


def _trigger_check(source: CommandSource) -> None:
    source.reply(tr("command.check_started"))
    threading.Thread(
        target=lambda: _run_check(_server, source=source),  # type: ignore[arg-type]
        name="mod_update_checker_manual",
        daemon=True,
    ).start()


def _command_tree(prefix: str):
    """The whole command tree for one root literal.

    Built per prefix rather than from ``Literal(ROOT_LITERALS)``. Both work — ``Literal`` has
    accepted ``str or Iterable[str]`` since before 2.13 (only its type annotation says
    otherwise) — but one tree per alias keeps the help message and the registered command
    trivially in step, and makes a failure on one alias distinguishable from the other.
    """
    return (
        Literal(prefix)
        .requires(_has_permission, _denied)
        .runs(_show_summary)
        .then(Literal("help").runs(_show_help))
        .then(Literal("check").runs(_trigger_check))
        .then(
            Literal("list")
            .runs(_show_full)
            .then(
                GreedyText("status").runs(
                    lambda source, context: _show_filtered(source, context["status"])
                )
            )
        )
        .then(Literal("status").runs(_show_status))
        .then(Literal("reload").runs(_reload_config))
    )


def _register_commands(server: PluginServerInterface) -> None:
    for prefix in ROOT_LITERALS:
        server.register_help_message(
            prefix, tr("help.modupdate"), permission=_config.command_permission_level
        )
        server.register_command(_command_tree(prefix))


# --------------------------------------------------------------------------------------
# MCDR lifecycle
#
# These are plain module-level functions named after the events, which is MCDR's documented
# convention: it discovers them by name on the entry module and registers them itself.
#
# They must NOT also be passed to ``server.register_event_listener``. During plugin loading
# that call is only *staged*, and it is then registered alongside the by-name discovery — so
# doing both registers every handler twice and fires each event twice. (The staging is why it
# looks harmless: no error, no warning, just a doubled post-startup check.)
# --------------------------------------------------------------------------------------


def on_load(server: PluginServerInterface, prev_module: Any) -> None:
    global _config, _last_report, _server, _online_players

    _server = server

    # A reload constructs a fresh module and hands the old one over here. Two things have to
    # be carried across, and one has to be *stopped*:
    #
    #   * the last report, because the admin is probably looking at it;
    #   * which players are online, because join/leave events are not replayed;
    #   * the previous module's scheduler thread. MCDR's reload path calls ``plugin.reload()``
    #     without dispatching the unloaded event (verified in MCDR's plugin_manager), so
    #     ``on_unload`` never runs and the old thread would otherwise keep firing checks
    #     against this server forever, once more on every reload.
    if prev_module is not None:
        carried = getattr(prev_module, "_last_report", None)
        if isinstance(carried, Report):
            _last_report = carried
        previous_players = getattr(prev_module, "_online_players", None)
        if isinstance(previous_players, set):
            _online_players = previous_players
        previous_stop = getattr(prev_module, "_stop_scheduler", None)
        if callable(previous_stop):
            try:
                previous_stop()
            except Exception as error:  # noqa: BLE001 - the old module is already gone
                server.logger.debug("could not stop the previous scheduler: {}".format(error))

    _stop_event.clear()
    _config = _load_config(server)
    _apply_language(server, _config)

    _register_commands(server)

    if not _config.enabled:
        server.logger.info("[Mod Update Checker] disabled by config")
        return

    # Count the jars by listing the directory — deliberately NOT by scanning them.
    #
    # `scan_mods` reads and hashes every jar, and this runs on MCDR's plugin-loading thread,
    # so using it here would block `!!MCDR reload plugin` and MCDR's own startup for as long
    # as it takes to read the whole `mods/` folder: seconds on a real modpack, against
    # milliseconds for the handful of tiny jars a test builds. A count is all this line wants,
    # and a directory listing gives it for free. The hashing belongs in the check, which runs
    # on its own thread.
    try:
        directory = resolve_mods_directory(
            _working_directory(server), _config.mods_directory
        )
        if not os.path.isdir(directory):
            server.logger.warning(
                tr("console.no_mods_directory", directory=str(directory))
            )
        else:
            jars, _disabled = iter_mod_jars(directory)
            server.logger.info(
                tr("console.loaded", count=len(jars), directory=str(directory))
            )
    except Exception as error:  # noqa: BLE001 - a broken mods folder is not fatal
        server.logger.warning("could not inspect the mods folder: {}".format(error))

    if _config.use_curseforge and not (_config.curseforge_api_key or "").strip():
        server.logger.info(tr("console.curseforge_hint"))

    _log_cache_summary(server, _config)

    _start_interval_scheduler(server)


def _log_cache_summary(server: PluginServerInterface, config: Config) -> None:
    """Say how many cached identifications are on disk, if there are any.

    Worth one line at startup: it is the difference between "the plugin is asking Modrinth
    about 40 mods again" and "the plugin remembered", and an admin chasing a slow check
    wants to know which it is without reading the config.
    """
    if not config.use_resolve_cache:
        return
    path = os.path.join(server.get_data_folder(), CACHE_FILE_NAME)
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        records = payload.get("records") or {}
    except (OSError, ValueError):
        return
    if records:
        server.logger.info(tr("console.cache_summary", records=len(records), path=path))


def on_unload(server: PluginServerInterface) -> None:
    _stop_scheduler()


def on_server_startup(server: PluginServerInterface) -> None:
    if _config.enabled and _config.check_on_server_start:
        _schedule_startup_check(server)


def on_server_stop(server: PluginServerInterface, server_return_code: int) -> None:
    # A pending startup check would otherwise fire against a stopped server and report
    # nonsense; the event is also the natural point to drop stale player state.
    _online_players.clear()

