"""Mod Update Checker — an MCDR plugin that tells a server admin which mods are stale.

Fabric has no native notion of "is this mod out of date". The loader reads ``mods/``,
launches, and never asks whether a newer build exists — so the answer has to come from
outside, by comparing each jar against Modrinth, where a mod can be identified by the exact
bytes of its file and so needs no guesswork.

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Sequence, Set, Tuple, Type

from mcdreforged.api.all import (
    CommandSource,
    GreedyText,
    Literal,
    PluginServerInterface,
    RAction,
    RColor,
    RText,
    RTextList,
    Serializable,
)

from . import i18n
from .checker import USER_AGENT, CheckOptions, Checker
from .installer import (
    STATUS_INSTALLED as INSTALL_INSTALLED,
    InstallOptions,
    install_pending,
)
from .downloads import (
    STATUS_ALREADY_PRESENT,
    STATUS_DOWNLOADED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    DownloadLedger,
    DownloadOptions,
    DownloadOutcome,
    Downloader,
    classify_downloaded,
    resolve_folder as resolve_download_folder_path,
)
from .report import (
    ALL_STATUSES,
    CHAT_PAGE_LINES,
    Report,
    entry_detail_rows,
    render_full,
    render_index,
    render_summary,
)
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
#: Which downloaded file belongs to which mod. Kept next to the other state rather than inside
#: the download folder, so that folder stays nothing but jars.
DOWNLOAD_LEDGER_FILE_NAME = "download-manifest.json"

#: What the last stop installed. Read at the next startup so the admin is told what changed
#: under their feet while the server was down, and kept until an admin has been told in game.
INSTALL_REPORT_FILE_NAME = "last-install.json"

#: Both spellings are registered so an admin does not have to guess which one is canonical.
ROOT_LITERALS = ("!!modupdate", "!!muc")

#: Used for the title bar when the plugin metadata cannot be read.
_FALLBACK_TITLE = "Mod Update Checker"

#: Width the help and status title bars aim to fill. Roughly one line of the vanilla chat
#: window at the default font size; going wider wraps, which looks worse than a short bar.
_TITLE_WIDTH = 53
#: Never fewer than this many ``=`` on each side, however long the plugin name gets.
_TITLE_BAR_MIN = 4
#: Left column of the help page, in characters. Derived from the literals themselves so it
#: cannot drift from the commands it has to line up: the longest registered prefix, then the
#: longest subcommand actually printed (``status``; a subcommand that takes an argument is
#: shown without it).
_HELP_COMMAND_WIDTH = max(len(name) for name in ROOT_LITERALS) + 1 + len("status")

#: Cap on how many mods a one-shot notification lists.
#:
#: Smaller than a command reply's page budget on purpose: a notification arrives unasked, in
#: the middle of whatever the player was doing, and half a screen of chat nobody requested is
#: worse than a short message that names the command to run. The listing is one command away.
NOTIFY_MAX_UPDATES = 6
#: How many download outcomes to print individually before summarising the rest. The download
#: folder is for a human to look at, so the lines are worth printing — but not two hundred
#: of them on a big modpack.
DOWNLOAD_LOG_LIMIT = 20


class _Grouped(Serializable):
    """A config section whose subsections are rebuilt on every construction.

    MCDR materialises a nested default with ``copy.copy``, which is shallow: two ``Config``
    objects would *share the contents* of every nested list, and appending to one would change
    the other — and the class attribute, and therefore every config built afterwards. A
    section that owns a list has to therefore build it again per instance.

    ``_NESTED`` names the subsections to rebuild. Nothing else needs to be listed: a scalar or
    a list declared directly on a class is already copied field-by-field.
    """

    #: Field name -> the class to construct for it.
    _NESTED: ClassVar[Dict[str, Type[Serializable]]] = {}

    def __init__(self) -> None:
        super().__init__()
        for name, section in type(self)._NESTED.items():
            setattr(self, name, section())


class ServerConfig(_Grouped):
    """What to look at: which folder, for which loader and game version."""

    mods_directory: str = ""
    """mods 目录。留空 = 服务端工作目录下的 ``mods``。相对路径按服务端目录解析。"""

    loader: str = "fabric"
    """要过滤的加载器：fabric / quilt / neoforge / forge。"""

    mc_version: str = "auto"
    """Minecraft 版本。``auto`` = 从服务端输出、日志、Mod 元数据依次推断。"""


class CheckConfig(_Grouped):
    """When a check runs, and what counts as an update."""

    on_server_start: bool = True
    """服务端启动完成后自动检查一次，结果写进控制台日志。"""

    start_delay_seconds: int = 60
    """开服后延迟多少秒再检查，避免和 Mod 加载抢资源。"""

    interval_hours: int = 0
    """定时检查间隔（小时）。``0`` = 关闭定时检查。"""

    include_beta: bool = False
    """是否把 beta 版本也算作「可用更新」。默认只认正式版。"""

    include_alpha: bool = False
    """是否把 alpha 版本也算作「可用更新」。"""

    ignored_mods: List[str] = []
    """**完全不做更新检测**的 Mod。可填 mod id、jar 文件名，或去掉 ``.jar`` 的文件名，
    不区分大小写，忽略空格与符号（``Fabric-API`` 与 ``fabricapi`` 等价）。

    适合：自己写的 Mod、打算长期固定在某个版本的 Mod、明确不需要更新提醒的 Mod。
    被列出的 Mod **不会产生任何网络查询**，也不会出现在「有更新」或「已下载」列表里；
    报告中仅标注为「已忽略」，便于你确认配置确实生效了。"""


class ReportConfig(_Grouped):
    """How a result reaches you. The checks below never change what is scanned."""

    updates_only: bool = True
    """自动检查时只在「有需要处理的项」时才输出完整提醒，否则只留一行。"""

    on_admin_join: bool = True
    """管理员上线时把结果单独发给他（必要时先查一次）。"""

    admin_permission: int = 3
    """多少权限等级算「管理员」，即上面那条通知的收件人。MCDR 等级 3 = admin，2 = helper。"""

    reuse_report_minutes: int = 1440
    """管理员上线时，多久以内的上次检查结果可以直接复用而不重新查。

    ``0`` = 每次都重新检查。默认 24 小时：一位管理员上线时想要的是**立刻看到结论**，而不是等一次
    完整扫描；而且几个管理员接连上线时，这个窗口能避免反复打接口。"""

    in_game: bool = False
    """发现更新时，是否在游戏内**广播**给在线管理员。默认关闭，避免打扰玩家。"""

    in_game_permission: int = 3
    """游戏内广播的最低 MCDR 权限等级。

    与上面的 ``admin_permission`` 是**两件事**：那个决定「谁算管理员」，这个决定「谁能收到广播」。
    分开是因为一个服可以既想让管理员上线时收到结果，又不想让广播打扰到所有人。"""

    write_file: bool = True
    """把每次检查的结果写成 JSON / 文本文件，便于外部脚本或事后排查。"""


class ModrinthConfig(_Grouped):
    enabled: bool = True
    """是否查询 Modrinth。"""

    api_base: str = ""
    """API 地址。留空 = 官方 ``https://api.modrinth.com/v2``；可改成镜像。"""


class SourcesConfig(_Grouped):
    """Where updates are looked up."""

    _NESTED: ClassVar[Dict[str, Type[Serializable]]] = {"modrinth": ModrinthConfig}

    modrinth: ModrinthConfig = ModrinthConfig()


class DownloadConfig(_Grouped):
    """The optional half: fetching newer builds into the plugin's own folder.

    Off by default, and it never writes to ``mods/`` — see the README.
    """

    enabled: bool = False
    """发现更新时，自动把新版本从 Modrinth 下载到插件数据文件夹的子文件夹里。

    只是**下载**，不会装进 ``mods/``——把没看过的 jar 直接塞进运行中的服务端，正是本插件
    想避免的事。下载下来的文件由你自行检查后手动替换。"""

    folder_name: str = "downloads"
    """下载到哪个子文件夹。这里填的是**单个文件夹名，不是路径**（如 ``downloads``）。

    刻意不允许填路径：这样无论如何配置都不可能写到插件数据文件夹之外，也就不可能被配置成
    直接写进 ``server/mods``。"""

    max_size_mb: int = 128
    """单个文件的大小上限（MB）。超过就跳过并说明原因。"""

    install_on_stop: bool = False
    """服务端停止后，把已下载的新版本装进 ``mods/``，旧 jar 改名为 ``<原名>.old`` 保留。

    **默认关闭，而且这是本插件唯一会改动 ``mods/`` 的功能。** 开启后：

    * 只在**服务端已经停止**之后动手（不会在运行中替换 jar）；
    * 只处理**插件自己下载过**的文件（清单里记着的那几个），手动放进 mods/ 的东西一概不碰；
    * 旧 jar **只改名、不删除**——更新出问题时改回名字就能回退；
    * 安装前校验哈希，对不上就不装；
    * 目标文件名已被占用时**跳过并说明**，绝不覆盖任何文件。

    ``mods/`` 里那个 jar 的文件名会被保留为「你的中括号备注 + 上游发布的文件名」，
    例如 ``[锂-性能优化]Lithium.jar`` 更新后会变成 ``[锂-性能优化]lithium-fabric-0.15.0.jar``。

    它**不依赖上面的 ``enabled``**：那只管「要不要去抓新的」，这只管「抓下来的要不要装」。"""

    retries: int = 3
    """下载失败后**额外**重试几次。总尝试次数 = 1 + 该值（默认 3 → 最多尝试 4 次）。

    与 ``network.retries`` 同一套语义。会重试的：传输中断、5xx、空响应、超过大小限制、哈希不符
    （传输过程中被损坏是哈希不符最常见的原因，重试是标准做法）。不会重试的：404、401/403
    （文件不在或没权限，重试改变不了结果，反复打一个 403 只会招来封禁）、本地写盘失败。
    设为 ``0`` 即不重试。"""


class CacheConfig(_Grouped):
    enabled: bool = True
    """缓存「某个哈希属于哪个项目」，避免每次开服重复解析。"""

    ttl_hours: int = 24
    """识别缓存的有效期（小时）。``0`` = 永不过期；要彻底关闭请用 ``enabled``。"""


class NetworkConfig(_Grouped):
    """HTTP tuning. The defaults are deliberately conservative."""

    _NESTED: ClassVar[Dict[str, Type[Serializable]]] = {"cache": CacheConfig}

    timeout_seconds: int = 20
    """单次 HTTP 请求超时（秒）。"""

    retries: int = 3
    """单次查询失败重试次数（含 429 限流与网络错误）。"""

    concurrent_requests: int = 4
    """并发请求数。调大更快也更容易触发上游限流。"""

    requests_per_minute: int = 240
    """自限速：每分钟最多多少次请求。Modrinth 官方上限是 300，留出余量。"""

    cache: CacheConfig = CacheConfig()


class Config(_Grouped):
    """The plugin's config file.

    Deliberately shallow at the top: the three settings almost everybody touches — on/off,
    language, and who may run the command — sit at the root, and everything else is grouped by
    the feature it belongs to. ``check`` and ``download`` in particular are separate sections,
    because they are separate decisions: what to look for, and whether to fetch it.
    """

    _NESTED: ClassVar[Dict[str, Type[Serializable]]] = {
        "server": ServerConfig,
        "check": CheckConfig,
        "report": ReportConfig,
        "sources": SourcesConfig,
        "download": DownloadConfig,
        "network": NetworkConfig,
    }

    enabled: bool = True
    """总开关。关掉后只保留命令，不做任何自动检查。"""

    language: str = i18n.AUTO
    """消息语言。``auto`` = 跟随 MCDR 的 language 设置；也可写 zh_cn / en_us。"""

    command_permission_level: int = 3
    """执行 ``!!modupdate`` 所需的最低 MCDR 权限等级。"""

    server: ServerConfig = ServerConfig()
    """扫描对象：目录、加载器、游戏版本。"""

    check: CheckConfig = CheckConfig()
    """更新检测：什么时候查、什么算更新、哪些 Mod 不查。"""

    report: ReportConfig = ReportConfig()
    """结果怎么送到你手上：控制台、报告文件、游戏内通知。"""

    sources: SourcesConfig = SourcesConfig()
    """去哪里查更新（目前只有 Modrinth）。"""

    download: DownloadConfig = DownloadConfig()
    """可选：把新版本下载到插件自己的文件夹（默认关闭，且从不写入 mods/）。"""

    network: NetworkConfig = NetworkConfig()
    """HTTP 超时、重试、并发与限速、识别缓存。"""


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


#: Where each option used to live, before the config was grouped into sections. Kept so an
#: admin who already had a config file can be told what happened to it, rather than watching
#: their settings quietly revert.
#:
#: This matters because MCDR says nothing about keys it does not recognise: it accepts them,
#: loads the defaults for everything else, and rewrites the file without them. From the
#: outside that is indistinguishable from the config having been reset for no reason.
_LEGACY_FLAT_OPTIONS: Dict[str, str] = {
    "mods_directory": "server.mods_directory",
    "loader": "server.loader",
    "mc_version": "server.mc_version",
    "check_on_server_start": "check.on_server_start",
    "start_check_delay_seconds": "check.start_delay_seconds",
    "check_interval_hours": "check.interval_hours",
    "include_beta": "check.include_beta",
    "include_alpha": "check.include_alpha",
    "ignored_mods": "check.ignored_mods",
    "notify_on_updates_only": "report.updates_only",
    "check_on_admin_join": "report.on_admin_join",
    "admin_join_permission": "report.admin_permission",
    "admin_join_max_report_age_minutes": "report.reuse_report_minutes",
    "notify_in_game": "report.in_game",
    "notify_in_game_permission": "report.in_game_permission",
    "write_report_file": "report.write_file",
    "use_modrinth": "sources.modrinth.enabled",
    "modrinth_api_base": "sources.modrinth.api_base",
    "download_updates": "download.enabled",
    "download_folder_name": "download.folder_name",
    "download_max_size_mb": "download.max_size_mb",
    "download_retries": "download.retries",
    "http_timeout_seconds": "network.timeout_seconds",
    "http_retries": "network.retries",
    "concurrent_requests": "network.concurrent_requests",
    "requests_per_minute": "network.requests_per_minute",
    "use_resolve_cache": "network.cache.enabled",
    "resolve_cache_ttl_hours": "network.cache.ttl_hours",
}


def _warn_about_flat_legacy_options(
    server: PluginServerInterface, raw: Dict[str, Any]
) -> None:
    """Say so if the file still uses the old flat option names.

    Only a warning: the file is left alone and MCDR regenerates it in the new shape, so the
    plugin still starts. The point is that the admin is told which options moved where, instead
    of finding that their settings appear to have been forgotten.
    """
    found = [name for name in _LEGACY_FLAT_OPTIONS if name in raw]
    if not found:
        return
    moves = ", ".join(
        "{} -> {}".format(name, _LEGACY_FLAT_OPTIONS[name]) for name in sorted(found)
    )
    server.logger.warning(tr("console.config_flat_legacy", count=len(found), moves=moves))


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
            _warn_about_flat_legacy_options(server, raw)
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
        include_beta=config.check.include_beta,
        include_alpha=config.check.include_alpha,
        use_modrinth=config.sources.modrinth.enabled,
        modrinth_base=config.sources.modrinth.api_base,
        ignored_mods=list(config.check.ignored_mods),
        timeout=max(1.0, float(config.network.timeout_seconds)),
        retries=max(0, int(config.network.retries)),
        workers=max(1, int(config.network.concurrent_requests)),
        requests_per_minute=max(0, int(config.network.requests_per_minute)),
        use_cache=bool(config.network.cache.enabled),
        cache_ttl_hours=max(0.0, float(config.network.cache.ttl_hours)),
    )


def _scan_current(server: PluginServerInterface, config: Config) -> Tuple[ScanResult, Any]:
    """Scan the mods folder and resolve the server context. Reads only."""
    working_directory = _working_directory(server)
    directory = resolve_mods_directory(working_directory, config.server.mods_directory)
    scan = scan_mods(directory, logger=server.logger)

    information_version = None
    try:
        information_version = server.get_server_information().version
    except Exception:  # noqa: BLE001 - offline servers have nothing to report
        information_version = None

    context = detect_server_context(
        working_directory=working_directory,
        configured_version=config.server.mc_version,
        configured_loader=config.server.loader,
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
            if config.network.cache.enabled
            else None
        )
        report = checker.run(scan, context, cache_path=cache_path)
        _last_report = report

        # Before the notification, not after it. What the admin reads has to describe the state
        # they are in when they read it: a build that gets fetched a moment later would
        # otherwise be announced as "not yet downloaded" and then downloaded, which makes the
        # report wrong the instant it is printed. The cost is waiting for the transfers, so a
        # line saying how many are starting goes out first.
        _reconcile_downloads(server, report, config)

        _notify(server, report, source=source, announce_clean=announce_clean,
                broadcast=broadcast)

        if config.report.write_file:
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

    if broadcast and _config.report.in_game and report.has_updates:
        _notify_in_game(server, report)


def _sync_download_state(
    server: PluginServerInterface, report: Report, config: Config
) -> Tuple[Optional[Path], Optional[DownloadLedger]]:
    """Load the ledger, prune it, and move already-fetched builds out of the update list.

    Runs whether or not downloading is currently enabled. The download folder says what has
    been fetched, and that stays true after the option is switched off — an admin who turns it
    off should still be told that the file is sitting there waiting, rather than being told
    again that an update exists.
    """
    folder, reason = resolve_download_folder(server, config)
    if folder is None:
        if config.download.enabled:
            server.logger.warning(
                tr("download.bad_folder", name=config.download.folder_name, reason=reason)
            )
        return None, None

    # Recorded on the report so the summary can name the folder: "ready to be installed"
    # without saying where is only half an answer.
    if config.download.enabled or folder.is_dir():
        report.download_folder = str(folder)

    ledger = DownloadLedger(Path(server.get_data_folder()) / DOWNLOAD_LEDGER_FILE_NAME,
                            logger=server.logger)
    if folder.is_dir():
        if ledger.prune(folder):
            # Records for files that are no longer there: the admin installed them, or removed
            # them. Either way the bookkeeping should follow.
            ledger.save()
    classify_downloaded(report.entries, folder, ledger)
    return folder, ledger


def _reconcile_downloads(
    server: PluginServerInterface, report: Report, config: Config
) -> None:
    """Bring the download folder and the report into agreement, fetching what is missing.

    Wholly self-contained. The check has already succeeded by the time this runs, so nothing in
    here — a misconfigured folder, an unreachable host, a full disk, a bug in the summary
    formatting — may turn a successful check into a reported failure.
    """
    http: Optional[HttpClient] = None
    try:
        folder, ledger = _sync_download_state(server, report, config)
        if folder is None or not config.download.enabled:
            return

        waiting = report.updates
        if waiting:
            server.logger.info(tr("download.starting", count=len(waiting)))

        options = DownloadOptions(
            folder=folder,
            max_bytes=max(1, int(config.download.max_size_mb)) * 1024 * 1024,
            retries=max(0, int(config.download.retries)),
        )
        http = _make_http_client(config)
        outcomes = Downloader(http, options, logger=server.logger, ledger=ledger).run(
            report.entries
        )
        # Anything that just arrived is no longer an update to fetch; it is ready to install.
        classify_downloaded(report.entries, folder, ledger)
        _log_download_outcomes(server, report, outcomes, folder)
    except Exception as error:  # noqa: BLE001 - see the docstring
        server.logger.warning(tr("download.crashed", error="{}: {}".format(
            type(error).__name__, error)))
    finally:
        if http is not None:
            http.close()


# --------------------------------------------------------------------------------------
# Installing what was downloaded
# --------------------------------------------------------------------------------------


def _mods_folder(server: PluginServerInterface, config: Config) -> Optional[Path]:
    try:
        return resolve_mods_directory(_working_directory(server), config.server.mods_directory)
    except Exception as error:  # noqa: BLE001 - an unusable path is worth reporting, not raising
        server.logger.warning(tr("install.bad_mods_folder", error=str(error)))
        return None


def _install_paths(
    server: PluginServerInterface, config: Config
) -> Tuple[Optional[Path], Optional[Path]]:
    mods = _mods_folder(server, config)
    downloads, _reason = resolve_download_folder(server, config)
    return mods, downloads


def _write_install_report(server: PluginServerInterface, results) -> None:
    """Record what was replaced, for the next startup and the first admin to log in."""
    installed = [item for item in results if item.status == INSTALL_INSTALLED]
    skipped = [item for item in results if item.status != INSTALL_INSTALLED]
    payload = {
        "version": 1,
        "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "logged": False,
        "notified": False,
        "installed": [
            {
                "name": item.name,
                "version": item.version,
                "new_file": item.new_file,
                "replaced_file": item.replaced_file,
                "backup_file": item.backup_file,
            }
            for item in installed
        ],
        "skipped": [
            {"name": item.name, "version": item.version, "status": item.status,
             "detail": item.detail}
            for item in skipped
        ],
    }
    path = Path(server.get_data_folder()) / INSTALL_REPORT_FILE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as error:
        server.logger.warning(tr("install.report_failed", error=str(error)))


def _read_install_report(server: PluginServerInterface) -> Optional[Dict[str, Any]]:
    path = Path(server.get_data_folder()) / INSTALL_REPORT_FILE_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("installed"), list):
        return None
    return data


def _mark_install_reported(server: PluginServerInterface, data: Dict[str, Any], field: str) -> None:
    data[field] = True
    path = Path(server.get_data_folder()) / INSTALL_REPORT_FILE_NAME
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # Losing the flag means the message may be repeated, which is a far smaller problem
        # than failing the startup it is being written during.
        pass


def _install_reason(detail: str) -> str:
    """A short reason code as words, or the text itself when it is not a known code.

    ``translate`` returns the key when it has no entry, which is exactly the signal needed here:
    a failure carries a sentence (``could not move the new jar in: ...``) rather than a code, and
    that sentence is more useful than a missing-key placeholder.
    """
    key = "install.reason." + (detail or "unknown")
    text = tr(key)
    return detail if text == key else text


def _install_summary_lines(data: Dict[str, Any]) -> List[str]:
    """The lines that describe one install batch, for the console and for chat."""
    installed = data.get("installed") or []
    lines = [tr("install.header", count=len(installed), when=str(data.get("at") or ""))]
    for item in installed[:NOTIFY_MAX_UPDATES]:
        lines.append(tr("install.line", name=item.get("name") or "?",
                        version=item.get("version") or "?",
                        old=item.get("backup_file") or "?"))
    if len(installed) > NOTIFY_MAX_UPDATES:
        lines.append(tr("report.and_more", count=len(installed) - NOTIFY_MAX_UPDATES))
    skipped = data.get("skipped") or []
    if skipped:
        lines.append(tr("install.skipped_header", count=len(skipped)))
        for item in skipped[:NOTIFY_MAX_UPDATES]:
            lines.append(tr("install.skipped_line", name=item.get("name") or "?",
                            reason=_install_reason(str(item.get("detail") or "unknown"))))
    return lines


def _install_on_stop(server: PluginServerInterface) -> None:
    """Replace installed jars with the builds fetched for them. Runs once the server is down.

    The event is the whole safety story: ``mods/`` is only written while nothing is reading it.
    Everything else this function does — the ledger as the work list, the hash check, the
    ``.old`` backup, the skip on a name clash — exists so that a mistake here costs a log line
    rather than a modpack.
    """
    if not _config.download.install_on_stop:
        return

    mods, downloads = _install_paths(server, _config)
    if mods is None or downloads is None:
        return
    if not mods.is_dir():
        server.logger.warning(tr("install.no_mods_folder", directory=str(mods)))
        return
    if not downloads.is_dir():
        return

    ledger = DownloadLedger(
        Path(server.get_data_folder()) / DOWNLOAD_LEDGER_FILE_NAME, logger=server.logger
    )
    if not ledger.records():
        return

    results = install_pending(
        ledger, InstallOptions(mods_folder=mods, downloads_folder=downloads),
        logger=None,   # the summary below is the log; per-file lines would repeat it
    )
    _write_install_report(server, results)
    installed = sum(1 for item in results if item.status == INSTALL_INSTALLED)
    if installed:
        # One line here, the list at the next startup. MCDR is shutting down and nobody is
        # reading the console; the batch is written to a file for the moment somebody is.
        server.logger.info(tr("install.done", count=installed))


def _announce_install_reminder(server: PluginServerInterface) -> None:
    """Say what changed while the server was down, once, at startup."""
    data = _read_install_report(server)
    if data is None or data.get("logged"):
        return
    for line in _install_summary_lines(data):
        server.logger.info(line)
    _mark_install_reported(server, data, "logged")


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
    return resolve_download_folder_path(base, config.download.folder_name)


def _make_http_client(config: Config) -> HttpClient:
    """A client for the download host, mirroring the checker's settings.

    Its own client, not the checker's, for two reasons: the checker closes its session when it
    finishes, and the two want different tuning. A JSON call should give up in seconds; a
    multi-megabyte transfer over a slow link should not be cut off at the same timeout.

    ``retries=0`` because :meth:`HttpClient.download` cannot retry a half-written stream. The
    retrying is done by :class:`Downloader`, where the hash accumulator and the partial file can
    be reset per attempt; see its ``_fetch``.
    """
    return HttpClient(
        user_agent=USER_AGENT,
        timeout=max(30.0, float(config.network.timeout_seconds)),
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
    # that were not eligible, the list would otherwise be the bulk of the output.
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


def _detail_link(command: str) -> RText:
    """A ``[详细信息]`` label that runs ``command`` when clicked.

    Clicking is by number rather than by mod id: the number is what the reader sees, and
    ``!!modupdate info 3`` is short enough to type if the chat log has since scrolled past the
    row. The command is spellable by hand, so a player is never stuck without the button.
    """
    return RText(tr("command.list.detail_link"), RColor.aqua).set_click_event(
        RAction.run_command, command
    )


def _reply_index(
    source: CommandSource, report: Report, entries=None, prefix: str = ROOT_LITERALS[0]
) -> None:
    """The numbered listing with a click on every row.

    The rows and their selection come from ``report.render_index``; this only decorates them.
    Keeping the two apart is what stops the chat reply from being the place where the page
    budget is computed — which it was, briefly, and it came out two lines too long.
    """
    head, rows, tail = render_index(report, tr, entries=entries, budget=CHAT_PAGE_LINES)
    source.reply(_coloured_line(head))
    for number, _entry, text in rows:
        source.reply(
            RTextList(
                _coloured_line("  " + text),
                RText("  "),
                _detail_link("{} info {}".format(prefix, number)),
            )
        )
    for line in tail:
        source.reply(_coloured_line(line))


def _reply_detail(source: CommandSource, entry) -> None:
    """One mod's detail: the version change, the links, and its notes.

    Links are labels with an ``open_url`` click and the url on hover, not the url itself —
    which is what lets a mod's detail afford two of them while a listing cannot afford one.
    """
    for label, value, url in entry_detail_rows(entry, tr):
        if not label:
            source.reply(RText(value, RColor.white))
            continue
        rendered = (
            RText(value, RColor.aqua).set_click_event(RAction.open_url, url).set_hover_text(url)
            if url
            else RText(value, RColor.green)
        )
        source.reply(RTextList(RText(label, RColor.gray), rendered))


def _notification_lines(report: Report) -> List[str]:
    """The body of an in-game notification.

    Two sections, because the two situations ask for different things and merging them would
    make the more urgent one invisible: "these need fetching" and "these are fetched, install
    them". A mod appears in exactly one of them, which is what stops an update being announced
    again after it has already been downloaded.
    """
    lines: List[str] = []

    updates = report.updates
    if updates:
        lines.append(tr("check.in_game_header", count=len(updates)))
        for entry in updates[:NOTIFY_MAX_UPDATES]:
            lines.append(tr("line.update", name=entry.name, local=entry.local_version or "?",
                            latest=entry.latest_version or "?"))
        if len(updates) > NOTIFY_MAX_UPDATES:
            lines.append(tr("report.and_more", count=len(updates) - NOTIFY_MAX_UPDATES))

    pending = report.awaiting_install
    if pending:
        lines.append(tr("check.in_game_awaiting", count=len(pending)))
        for entry in pending[:NOTIFY_MAX_UPDATES]:
            lines.append(tr("line.awaiting_install", name=entry.name,
                            latest=entry.latest_version or "?"))
        if len(pending) > NOTIFY_MAX_UPDATES:
            lines.append(tr("report.and_more", count=len(pending) - NOTIFY_MAX_UPDATES))
        lines.append(tr("check.in_game_awaiting_where"))

    if not lines:
        lines.append(tr("report.no_updates"))
    elif updates or pending:
        # A truncated list with no way onward is a dead end, so the notification says which
        # command carries the rest.
        lines.append(tr("check.in_game_more_hint"))
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
        server, sorted(_online_players), _config.report.in_game_permission
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
    window_minutes = max(0, int(_config.report.reuse_report_minutes))
    age = report.age_seconds() if report is not None else None
    reused = age is not None and 0 < age <= window_minutes * 60

    if not reused:
        # No broadcast here: the admin who just joined is about to get the same figures in
        # their own message, and everyone else online was told when the previous check ran.
        fresh = _run_check(server, announce_clean=not _config.report.updates_only,
                           broadcast=False)
        if fresh is not None:
            report, age, reused = fresh, 0.0, False

    if _stop_event.is_set():
        return

    # What the last stop replaced comes first: it is news about this server's own files, and
    # the admin is the only one who can act on it. Sent once, then marked so the next admin
    # does not get the same list again.
    install = _read_install_report(server)
    if install is not None and not install.get("notified"):
        # The summary carries its own header, so no second one is added here: two headings for
        # one list is how a message starts looking like the plugin talking to itself.
        _tell_player(server, player, _install_summary_lines(install))
        _mark_install_reported(server, install, "notified")

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

    if not (_config.enabled and _config.report.on_admin_join):
        return

    # The permission check is cheap and happens here; the check itself goes to its own thread,
    # because this runs on MCDR's event thread and a check reads and hashes every jar.
    if not _permitted_players(server, [player], _config.report.admin_permission):
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

    interval_hours = max(0, int(_config.check.interval_hours))
    if not _config.enabled or interval_hours <= 0:
        return

    def loop() -> None:
        server.logger.info(tr("console.interval_enabled", hours=interval_hours))
        while not _stop_event.wait(interval_hours * 3600):
            _run_check(server, announce_clean=not _config.report.updates_only)

    _scheduler_thread = threading.Thread(
        target=loop, name="mod_update_checker_scheduler", daemon=True
    )
    _scheduler_thread.start()


def _schedule_startup_check(server: PluginServerInterface) -> None:
    """Run one check a little while after the server finishes starting."""
    delay = max(0, int(_config.check.start_delay_seconds))
    if delay:
        server.logger.info(tr("console.check_scheduled", seconds=delay))

    def once() -> None:
        if _stop_event.wait(delay):
            return
        _run_check(server, announce_clean=not _config.report.updates_only)

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


def _show_list(source: CommandSource, prefix: str = ROOT_LITERALS[0]) -> None:
    """``list`` — the numbered index. Detail is one click away, not inline.

    It used to print every mod's project page, download url and notes inline: five to seven
    lines each, so even a small server's output ran past a chat page and the useful rows were
    the ones that scrolled off.
    """
    if _last_report is None:
        source.reply(tr("command.no_report_yet"))
        return
    _reply_index(source, _last_report, prefix=prefix)


def _show_filtered(source: CommandSource, status: str, prefix: str = ROOT_LITERALS[0]) -> None:
    """``list <状态>`` — the same index, filtered. Numbers keep their full-list meaning.

    Numbering over the filtered set would be more natural to read, but it would make a click
    ambiguous: the same number would mean different mods depending on which command produced
    the row. Keeping one numbering means a number always identifies a mod.
    """
    wanted = (status or "").strip().lower()
    if wanted not in ALL_STATUSES:
        source.reply(tr("command.unknown_status", value=status, options=", ".join(ALL_STATUSES)))
        return
    if _last_report is None:
        source.reply(tr("command.no_report_yet"))
        return
    _reply_index(source, _last_report, entries=_last_report.by_status(wanted), prefix=prefix)


def _show_info(source: CommandSource, target: str) -> None:
    """``info <编号|mod id|文件名>`` — one mod's version change, links and notes."""
    if _last_report is None:
        source.reply(tr("command.no_report_yet"))
        return
    text = (target or "").strip()
    if not text:
        source.reply(tr("command.info.usage"))
        return
    entry = _last_report.entry_by_handle(text)
    if entry is None:
        source.reply(tr("command.info.unknown", value=text))
        return
    _reply_detail(source, entry)


def _field(label: str, value: str, value_colour: Any = RColor.green) -> RTextList:
    """``Label: value`` with the label in aqua.

    The trailing ``: `` is part of the translated label rather than added here, so the
    punctuation is translatable and the two languages can disagree about it.

    Shared with the help page's ``command -- description`` rows so the two screens look like
    they come from the same plugin: aqua is always the thing you act on (a command, a label),
    white or green is the content, gray is an aside.
    """
    return RTextList(RText(label, RColor.aqua), RText(value, value_colour))


def _show_status(source: CommandSource) -> None:
    """What the plugin currently thinks the server is, and what it is configured to do.

    One rich message rather than a line per ``reply`` call: the lines are a single screen, and
    building them as one ``RTextList`` is what lets colours and the title bar survive. (A
    ``server.logger`` call cannot do this — MCDR's log formatter stringifies its argument and
    ``RTextBase.__str__`` drops the colour, which is why the console paths log plain strings.)
    """
    server = _server
    if server is None:
        return
    scan, context = _scan_current(server, _config)

    modrinth_state = (
        tr("command.status.enabled") if _config.sources.modrinth.enabled else tr("command.status.disabled")
    )
    parts = RTextList(
        _title_line(server),
        "\n",
        _field(
            tr("command.status.server_label"),
            tr("command.status.server", version=context.mc_version or "?",
               loader=context.loader, source=context.mc_version_source),
            RColor.white,
        ),
        "\n",
        _field(
            tr("command.status.mods_dir_label"),
            tr("command.status.mods_dir", directory=scan.directory, count=len(scan.mods)),
            RColor.white,
        ),
        "\n",
        _field(tr("command.status.upstream_label"),
               tr("command.status.upstream", modrinth=modrinth_state),
               RColor.white),
        # The setting that changes files on this server belongs on the page that describes
        # what the plugin is doing, not only in the config file.
        "\n",
        _field(
            tr("command.status.install_label"),
            tr("command.status.install_on") if _config.download.install_on_stop
            else tr("command.status.install_off"),
            RColor.yellow if _config.download.install_on_stop else RColor.gray,
        ),
    )
    if _config.check.ignored_mods:
        parts.append("\n")
        parts.append(
            _field(
                tr("command.status.ignored_label"),
                tr("command.status.ignored", count=len(_config.check.ignored_mods),
                   names=", ".join(_config.check.ignored_mods[:8])),
                RColor.white,
            )
        )
    parts.append("\n")
    if _last_report is None:
        parts.append(_field(tr("command.status.last_report_label"),
                            tr("command.status.never_checked"), RColor.gray))
    else:
        parts.append(
            _field(
                tr("command.status.last_report_label"),
                tr("command.status.last_report", when=_last_report.generated_at,
                   actionable=_last_report.actionable_count),
                RColor.white,
            )
        )
    parts.append("\n")
    parts.append(
        _field(
            tr("command.status.scheduling_label"),
            tr("command.status.scheduling",
               on_start=tr("command.status.yes") if _config.check.on_server_start
               else tr("command.status.no"),
               hours=_config.check.interval_hours),
            RColor.white,
        )
    )
    source.reply(parts)


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


def _plugin_title(server: Optional[PluginServerInterface]) -> Tuple[str, str]:
    """``(name, version)`` from the plugin metadata, falling back to a constant.

    Reading the metadata is not allowed to raise: it is only used to draw a title bar, and a
    missing version is worth far less than a command that errors out. The fallback name is the
    plugin id so it is at least recognisable.
    """
    getter = getattr(server, "get_self_metadata", None)
    if getter is None:
        return _FALLBACK_TITLE, ""
    try:
        metadata = getter()
    except Exception:  # noqa: BLE001 - a title is not worth failing a command over
        return _FALLBACK_TITLE, ""
    name = str(getattr(metadata, "name", "") or _FALLBACK_TITLE)
    version = str(getattr(metadata, "version", "") or "")
    return name, version


def _title_line(server: Optional[PluginServerInterface] = None) -> RTextList:
    """``========  Mod Update Checker v1.5.0  ========``

    The bar is sized to the name so the two sides stay even, with a floor so a very long or
    very short name still looks deliberate. Name and version are coloured differently because
    the version is the part an admin is usually looking for when they report a problem.
    """
    name, version = _plugin_title(server)
    core = "{} v{}".format(name, version) if version else name
    bars = max(_TITLE_BAR_MIN, (_TITLE_WIDTH - len(core) - 4) // 2)
    rule = "=" * bars
    line = RTextList(RText(rule, RColor.gold), "  ", RText(name, RColor.aqua))
    if version:
        line.append(RText(" v" + version, RColor.yellow))
    line.append("  ")
    line.append(RText(rule, RColor.gold))
    return line


def _help_line(description: str, command: str, action: Any) -> RTextList:
    """One ``!!muc <subcommand>  -- description`` row, left column padded to line up.

    The separator carries its own leading space, so the column width is the longest command
    itself and the descriptions all start at the same place — no command needs a gap jammed
    against it, and the longest one is not pushed out of alignment by one.

    The click event goes on the command only: the padding that makes the column straight is not
    part of what gets typed when the row is clicked.
    """
    padding = " " * max(0, _HELP_COMMAND_WIDTH - len(command))
    click = command + (" " if action is RAction.suggest_command else "")
    return RTextList(
        RText(command, RColor.aqua).set_click_event(action, click),
        RText(padding + " -- ", RColor.gray),
        RText(description, RColor.white),
    )


def _show_help(source: CommandSource, prefix: str = ROOT_LITERALS[0]) -> None:
    """The landing page: what this plugin's commands are, and what each one does.

    ``prefix`` is the spelling actually typed, so ``!!muc help`` lists ``!!muc ...`` and not
    the other alias. The other spelling is mentioned once in the usage line instead.

    Every row is clickable: the command runs when clicked, except ``list``, which suggests
    rather than runs — it works bare, but the useful form takes a status filter, so the input
    box is filled in ready for one instead of firing the unfiltered listing.
    """
    other = next((name for name in ROOT_LITERALS if name != prefix), prefix)
    rows = RTextList()
    # Built with explicit ``RTextList`` calls rather than by looping over a tuple of keys: the
    # catalogue invariant collects key literals from their call sites, so a key reached through
    # a variable would look unused and be reported as a stale entry.
    rows.append(_title_line(_server))
    rows.append("\n")
    rows.append(RText(tr("command.help.usage", command=prefix, alias=other), RColor.gray))
    rows.append("\n")
    rows.append(RText(tr("command.help.permission", level=_config.command_permission_level),
                      RColor.yellow))
    rows.append("\n")
    rows.append(_help_line(tr("command.help.entry_summary"), prefix, RAction.run_command))
    rows.append("\n")
    rows.append(_help_line(tr("command.help.entry_check"), prefix + " check",
                           RAction.run_command))
    rows.append("\n")
    rows.append(_help_line(tr("command.help.entry_list"), prefix + " list",
                           RAction.run_command))
    rows.append("\n")
    rows.append(_help_line(tr("command.help.entry_info"), prefix + " info",
                           RAction.suggest_command))
    rows.append("\n")
    rows.append(_help_line(tr("command.help.entry_status"), prefix + " status",
                           RAction.run_command))
    rows.append("\n")
    rows.append(_help_line(tr("command.help.entry_reload"), prefix + " reload",
                           RAction.run_command))
    rows.append("\n")
    rows.append(_help_line(tr("command.help.entry_help"), prefix + " help",
                           RAction.run_command))
    source.reply(rows)


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
        .then(
            Literal("help").runs(
                lambda source: _show_help(source, prefix)
            )
        )
        .then(Literal("check").runs(_trigger_check))
        .then(
            Literal("list")
            .runs(lambda source: _show_list(source, prefix))
            .then(
                GreedyText("status").runs(
                    lambda source, context: _show_filtered(source, context["status"], prefix)
                )
            )
        )
        .then(
            Literal("info")
            .runs(lambda source: source.reply(tr("command.info.usage")))
            .then(
                GreedyText("target").runs(
                    lambda source, context: _show_info(source, context["target"])
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
            _working_directory(server), _config.server.mods_directory
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

    if _config.check.ignored_mods:
        server.logger.info(tr("console.ignored_mods", count=len(_config.check.ignored_mods),
                              names=", ".join(_config.check.ignored_mods[:8])))

    _log_cache_summary(server, _config)

    _start_interval_scheduler(server)


def _log_cache_summary(server: PluginServerInterface, config: Config) -> None:
    """Say how many cached identifications are on disk, if there are any.

    Worth one line at startup: it is the difference between "the plugin is asking Modrinth
    about 40 mods again" and "the plugin remembered", and an admin chasing a slow check
    wants to know which it is without reading the config.
    """
    if not config.network.cache.enabled:
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
    if not _config.enabled:
        return
    # What the last stop replaced. Announced here rather than at install time because there is
    # nobody listening while the server is down.
    _announce_install_reminder(server)
    if _config.check.on_server_start:
        _schedule_startup_check(server)


def on_server_stop(server: PluginServerInterface, server_return_code: int) -> None:
    """The one moment ``mods/`` may be written: nothing is reading it any more."""
    # A pending startup check would otherwise fire against a stopped server and report
    # nonsense; the event is also the natural point to drop stale player state.
    _online_players.clear()

    if not _config.enabled:
        return
    try:
        _install_on_stop(server)
    except Exception as error:  # noqa: BLE001 - a failed install must not break the shutdown
        server.logger.warning(tr("install.crashed", error="{}: {}".format(
            type(error).__name__, error)))

