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
from typing import (
    Any,
    ClassVar,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
)

from mcdreforged.api.all import (
    CommandSource,
    GreedyText,
    Literal,
    PluginServerInterface,
    RAction,
    RColor,
    RText,
    RTextBase,
    RTextList,
    Serializable,
)

from . import i18n
from .jsonfile import read_json, write_json
from .checker import USER_AGENT, CheckOptions, Checker
from .installer import (
    STATUS_INSTALLED as INSTALL_INSTALLED,
    InstallOptions,
    install_pending,
    pending_records,
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
    entry_key,
    resolve_folder as resolve_download_folder_path,
    safe_jar_name,
)
from .report import (
    ALL_STATUSES,
    CHAT_PAGE_LINES,
    MATCHED_BY_NOTEWORTHY,
    ROW_FIELD_BODY,
    ROW_FIELD_MARK,
    ROW_FIELD_NAME,
    ROW_FIELD_NOTE,
    ROW_FIELD_NUMBER,
    QUARTERS_PER_LETTER,
    STATUS_AWAITING_INSTALL,
    STATUS_ERROR,
    STATUS_LOCAL_AHEAD,
    STATUS_NO_COMPATIBLE_BUILD,
    STATUS_OLD_BACKUP,
    STATUS_UP_TO_DATE,
    STATUS_UPDATE_AVAILABLE,
    VERSION_ARROW,
    VERSION_CURRENT,
    VERSION_NEW,
    VERSION_OLD,
    VERSION_PLAIN,
    VERSION_UPSTREAM,
    Report,
    SummarySection,
    UpdateEntry,
    action_row,
    chat_row_fields,
    display_width,
    entry_detail_rows,
    entry_line_text,
    has_version_label,
    index_row_fields,
    render_full,
    render_index,
    render_pager,
    render_summary,
    summarise,
    text_quarters,
    version_summary,
)
from .cleanup import Backup, expired, list_backups, remove_backups, total_bytes
from .report import format_size as _format_size
from .upstream import HttpClient
from .projectmap import MAP_FILE_NAME, ProjectMap, resolve_map_file
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
#: 收尾线两侧最少几个 ``=``。3 而不是 4：底线现在要捎一句「悬停 [版本] [状态]，点击 [详细信息]」，
#: 英文那份是 42 列，去掉两侧各 3 个 ``=`` 与两对空格正好等于标题栏的宽度——4 会让它长出两列。
_TITLE_BAR_MIN = 3

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

#: How long a ``!!muc confirm`` stays valid, in seconds.
#:
#: The two-step form exists because both actions write something that is awkward to undo: one
#: spends bandwidth, the other plans a change to ``mods/`` that happens at a moment the admin
#: will not be watching. A confirmation is what turns "the number I typed was the one I meant"
#: into a decision the admin made twice. The window then has to close, or a stray
#: ``!!muc confirm`` typed days later would act on a plan nobody remembers making.
CONFIRM_TIMEOUT_SECONDS = 120

#: The word ``!!muc download`` and ``!!muc install`` take instead of a mod: ``all``.
#:
#: A reserved word rather than a lookup, deliberately — it has to mean the whole list even on
#: a server that happens to ship a mod named "all". Such a mod is still reachable by its
#: number, and a number is what the listing offers anyway.
ALL_TARGET = "all"

#: How many mods a bulk plan lists by name before collapsing the rest into "and N more".
#:
#: The plan exists so the admin can see what they are about to approve; a forty-mod server
#: cannot show forty rows without pushing the question off the screen, and a number plus the
#: count is enough to decide with.
BULK_PLAN_ROWS = 8

#: 清理计划最多列几行，其余折成计数。理由同 ``BULK_PLAN_ROWS``。
CLEANUP_PLAN_ROWS = 8

#: 待确认的删除操作有哪几种。名字写清楚是因为它们只差一点：
#: ``delete`` 一个文件、``delete_expired`` 全部过期的（``cleanup``）、``delete_all`` 全部。
#: 确认时必须按 **同一个** kind 重新推导集合——用错了就等于让管理员批准一个他没见过的集合。
_KIND_DELETE = "delete"
_KIND_DELETE_EXPIRED = "delete_expired"
_KIND_DELETE_ALL = "delete_all"

#: 每一种「一次删一批」的操作。确认门禁、重列计划的提示语都按这张表走。
_BULK_DELETE_KINDS = (_KIND_DELETE_EXPIRED, _KIND_DELETE_ALL)
_DELETE_KINDS = (_KIND_DELETE,) + _BULK_DELETE_KINDS

#: How many of an ambiguous handle's candidates are named — in the sentence, and as buttons.
#:
#: A handle that matches thirty mods is a handle that did not narrow anything down, and thirty
#: buttons is not a completion list, it is a wall. The count in the sentence still says how
#: many there were, so the reader knows the list was cut.
AMBIGUOUS_NAMES = 6


class Notice(NamedTuple):
    """One line of an in-game message: the text, and what the line is *for*.

    The role decides the colour, and it is decided where the sentence is built — never guessed
    from the text in the delivery layer. That was the lesson of v1.3.0's screens: colouring by
    "does this line contain an arrow" coloured a line by what it happened to contain rather
    than by what it meant, and made the same mod two colours on two screens. The roles are the
    same vocabulary the screens use, so the two stay one system:

    * ``heading`` — a section heading: white;
    * ``action``  — a row that needs somebody to do something: yellow;
    * ``done``    — a row about something already finished: gray, like the screens' rows that
      need nothing;
    * ``hint``    — a footnote or a command to type: gray.
    """

    text: str
    role: str


#: Role → colour. An unknown role falls back to yellow, which is loud rather than invisible.
_NOTICE_COLOURS = {
    "heading": RColor.white,
    "action": RColor.yellow,
    "done": RColor.gray,
    "hint": RColor.gray,
    # 「有更新」与「有问题」用与列表行同一套颜色：blue = 有得更新（含已下载待安装），
    # red = 要人看一眼（无适配、跳过、出错）。
    "update": RColor.blue,
    "blocked": RColor.red,
}


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

    manual_map: str = MAP_FILE_NAME
    """自己写的「哪个 jar 对应哪个项目」清单的文件名，放在插件数据文件夹里。

    用来解决「自己编译、重新打包或改签名的 Mod 查不到」这个已知限制：哈希对不上、名称也猜不中，
    这类 jar 本来只能报 ``unresolved``。在这里写下它们的归属，就不需要再猜。

    填**文件名，不是路径**（如 ``project-map.json``）——与 ``download.folder_name`` 同样的理由，
    这样它无论如何配置都只会在插件数据文件夹里，不会读到别处去。填 ``""`` 即关闭。

    这个文件**只读不写**，格式见 README。改完直接 ``!!modupdate reload`` 或等下次检查即可生效。"""


class CleanupConfig(_Grouped):
    """The other optional half: what to do about the ``.old`` backups left in ``mods/``.

    Separate from ``download`` on purpose. That section answers "shall the plugin fetch and
    install newer builds"; this one answers "shall it tell me about the files the last install
    left behind, and may it remove them". A server can very reasonably want the first and not
    the second, and the second is the only one that ever deletes anything.

    Two switches, and the order matters: :attr:`allow_delete` is the one that decides whether
    anything may be removed at all, and :attr:`enabled` — the reminder — sits behind it, because
    a reminder whose next step is refused is just noise.
    """

    allow_delete: bool = False
    """是否允许删除类指令真的删东西（``!!muc delete`` / ``!!muc cleanup`` / 确认它们）。

    **默认关闭，而且关着的时候插件不会删除 ``mods/`` 里的任何文件。**

    关着的时候这两条指令**仍然存在**，但只会告诉你这个选项关着、以及去哪里打开——空着的
    回绝读起来像 bug，而这里必须让人一眼看出「不是坏了，是有个开关没开」。详情页里那个
    ``[删除此备份]`` 也会相应地变成一行说明，而不是一个点下去只会报错的按钮。

    它**同时是 ``enabled`` 的前置开关**：这个关着时，到期提醒也不会出现——提醒的下一步
    （``!!muc confirm``）在那种状态下必然被拒绝，说一句做不到的话就是废话。"""

    enabled: bool = False
    """是否主动提醒清理已过期的备份。**默认关闭，而且要先打开 ``allow_delete``。**

    关着的时候插件仍然**看得见**这些文件：它们照样出现在 ``!!muc list``、可以用
    ``!!muc list old_backup`` 筛选、``!!muc status`` 也会报个数。这个开关只决定插件会不会
    自己开口提醒你——和 ``download`` 的分工一样：自动行为要开关，手动命令不要。"""

    max_age_days: int = 30
    """备份存在多少天之后算「过期」。``0`` = 任何备份都立刻算过期。

    年龄取自文件的修改时间，而插件在**创建备份的那一刻**会把这个时间戳刷新成当下——
    否则改名会保留原 jar 的时间戳，每个备份看起来都比实际老得多。"""


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
        "cleanup": CleanupConfig,
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

    cleanup: CleanupConfig = CleanupConfig()
    """可选：清理安装时留下的 ``.old`` 备份（默认关闭，只提醒不自动删）。"""

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

#: The ``!!muc download`` / ``!!muc install`` waiting for a ``!!muc confirm``.
#:
#: Kept in module state rather than per-source, because ``confirm`` is typed as its own command
#: and has nothing to correlate with. Guarded by its own lock: commands run on MCDR's task
#: executor, and the two-step form means two of them arrive at unrelated moments.
_pending_action: Optional[Dict[str, Any]] = None
_pending_lock = threading.Lock()


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
) -> bool:
    """Say so if the file still uses the old flat option names.

    Only a warning: the file is left alone and MCDR regenerates it in the new shape, so the
    plugin still starts. The point is that the admin is told which options moved where, instead
    of finding that their settings appear to have been forgotten.

    Returns whether it warned, so the caller can stay quiet about the same rebuild — this
    message already names every option that moved, and repeating it as a list of added keys
    would be the same news twice.
    """
    found = [name for name in _LEGACY_FLAT_OPTIONS if name in raw]
    if not found:
        return False
    moves = ", ".join(
        "{} -> {}".format(name, _LEGACY_FLAT_OPTIONS[name]) for name in sorted(found)
    )
    server.logger.warning(tr("console.config_flat_legacy", count=len(found), moves=moves))
    return True


def _leaf_paths(node: Any, prefix: str = "") -> List[str]:
    """Every scalar in a config dict, as a ``section.option`` dotted path.

    Walked rather than described, because "is this file complete" has to be answerable two
    levels down: ``download.install_on_stop`` sits inside a section, and a check that only
    looked at the root would call such a file complete.
    """
    paths: List[str] = []
    if not isinstance(node, dict):
        return paths
    for key, value in node.items():
        path = "{}.{}".format(prefix, key) if prefix else str(key)
        if isinstance(value, dict):
            paths.extend(_leaf_paths(value, path))
        else:
            paths.append(path)
    return paths


def _file_leaf_paths(path: str) -> Optional[List[str]]:
    """The same, read from a file: ``None`` when nothing readable is there."""
    data = read_json(path)
    return _leaf_paths(data) if isinstance(data, dict) else None


def _report_config_file_state(
    server: PluginServerInterface,
    config: Config,
    path: str,
    existed_before: bool,
    leaves_before: Optional[List[str]],
    legacy_rebuilt: bool,
) -> None:
    """One line about the file itself — created, completed, or refusing to change.

    MCDR heals this file silently: options the file does not have are filled in from the class
    defaults and the whole file is written back, with nothing said about it. Silent is the
    wrong default, and this is the case that proves it — a user's file predated
    ``download.install_on_stop``, so the option was not in it. It was *supposed* to appear on
    the next load, and nothing anywhere said whether it had; the user reinstalled the plugin,
    looked again, and still could not find it. Whether a file was brought up to date is not a
    question an admin should have to answer by reading source.

    Three outcomes are worth a line, and the rest of the time there is nothing to say:

    * there was no file and one has just been created — say where it is;
    * the file was missing options and now has them — name them;
    * the file is still missing options after the load — a warning, because that is not
      supposed to happen, and the path plus the names are the two facts needed to find out why.
    """
    after = _file_leaf_paths(path)
    if not existed_before:
        if after is not None:
            server.logger.info(tr("console.config_created", path=path, count=len(after)))
        return
    if legacy_rebuilt:
        return  # the flat-legacy warning already described this rebuild, option by option
    if leaves_before is None or after is None:
        return
    added = sorted(set(after) - set(leaves_before))
    if added:
        server.logger.info(
            tr(
                "console.config_completed",
                count=len(added),
                names=", ".join(added[:8]),
                path=path,
            )
        )
        return
    missing = sorted(set(_leaf_paths(config.serialize())) - set(after))
    if missing:
        server.logger.warning(
            tr(
                "console.config_incomplete",
                count=len(missing),
                names=", ".join(missing[:8]),
                path=path,
            )
        )


def _load_config(server: PluginServerInterface) -> Config:
    """Load the config, keeping a backup if the file had to be rebuilt — and say what the
    file itself did.

    MCDR's default ``failure_policy='regen'`` silently replaces an unparseable config with
    defaults. Silent is the problem: an admin who fat-fingered a comma would see their
    settings vanish with no explanation, so the old file is preserved and the reason logged.

    The same silence covers the other two things MCDR does to this file — creating it when it
    is missing, and filling in options it does not have and writing the result back. Both are
    reported here (see :func:`_report_config_file_state`); the language is applied first so
    that report is written in the language the file just asked for.
    """
    path = _config_path(server)
    existed = os.path.isfile(path)
    leaves_before: Optional[List[str]] = None
    legacy_rebuilt = False
    if existed:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                raise ValueError("config root must be a JSON object")
            leaves_before = _leaf_paths(raw)
            legacy_rebuilt = _warn_about_flat_legacy_options(server, raw)
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
    if not isinstance(config, Config):
        config = Config.get_default()

    # Applying the language lives here rather than at the two call sites because the report
    # below is the first thing on the console that has to be readable, and the language
    # setting is part of what was just read.
    _apply_language(server, config)
    _report_config_file_state(server, config, path, existed, leaves_before, legacy_rebuilt)
    return config


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


def _scan_current(
    server: PluginServerInterface, config: Config, hashes: bool = True
) -> Tuple[ScanResult, Any]:
    """Scan the mods folder and resolve the server context. Reads only.

    :param hashes: ``False`` skips reading jar bytes entirely. Every caller that runs a *check*
    wants them; the ones describing the folder do not, and this is the switch that keeps
    ``!!muc status`` from hashing a whole modpack to print a count.
    """
    working_directory = _working_directory(server)
    directory = resolve_mods_directory(working_directory, config.server.mods_directory)
    scan = scan_mods(directory, logger=server.logger, hashes=hashes)

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
        # Captured before this run replaces it: the difference between the two is the only
        # thing that can tell "this update is new" from "this update is still waiting".
        previous = _last_report
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
        report = checker.run(
            scan, context, cache_path=cache_path, map_path=_manual_map_path(server, config)
        )
        _last_report = report

        # Before the notification, not after it. What the admin reads has to describe the state
        # they are in when they read it: a build that gets fetched a moment later would
        # otherwise be announced as "not yet downloaded" and then downloaded, which makes the
        # report wrong the instant it is printed. The cost is waiting for the transfers, so a
        # line saying how many are starting goes out first.
        fetched, fetched_bytes = _reconcile_downloads(server, report, config)

        # Its own line, right after the transfers it is about, and before the summary: an admin
        # who stepped away while the files were coming down needs exactly one sentence saying
        # they arrived and what to do next, and it reads in the wrong order if it ends up below
        # a state description it is not part of.
        if fetched:
            _announce_download_complete(server, fetched, fetched_bytes, broadcast=broadcast)

        # After the download reconciliation, so the comparison describes the report as it will
        # be read: a build that was fetched just now is no longer "an update available".
        report.record_new_since(previous)

        _notify(server, report, source=source, announce_clean=announce_clean,
                broadcast=broadcast)

        # 在 Mod 汇总之后：这是家务，不是这次检查的结论。它自己带开关（cleanup.enabled），
        # 不看 report.updates_only —— 那项管的是「没问题就别啰嗦」，而备份是另一回事。
        _announce_cleanup(server, broadcast=broadcast)

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


def _manual_map_path(
    server: PluginServerInterface, config: Config
) -> Optional[Path]:
    """The admin's mapping file, or ``None`` when it is switched off or unusable.

    A rejected value warns and then disables the feature rather than failing the check: the
    cost of ignoring it is that a few jars stay ``unresolved``, which is what they would have
    been anyway, while the cost of refusing to check would be the whole plugin.
    """
    path, reason = resolve_map_file(
        Path(server.get_data_folder()), config.sources.manual_map
    )
    if path is None:
        if reason != "disabled":
            server.logger.warning(
                tr("console.manual_map_rejected", value=config.sources.manual_map)
            )
        return None
    return path


def _load_previous_report(
    server: PluginServerInterface, config: Config
) -> Optional[Report]:
    """Read back the report the last run wrote, so a restart does not discard the window.

    Gated on ``report.write_file``, which is the setting whose entire meaning is "keep the
    last result on disk". With it off, a report file left over from when it was on is not
    something to start reading — the admin asked for results not to be persisted.

    Anything unusable gives ``None``, and the caller's response to ``None`` is to run the
    check, which is what it would have done anyway. That is what makes it safe to be strict
    about the format marker: the worst case is one extra check after an upgrade.
    """
    if not config.report.write_file:
        return None
    path = os.path.join(server.get_data_folder(), REPORT_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return Report.from_json(handle.read())
    except (OSError, UnicodeDecodeError):
        return None


def _report_still_applies(
    report: Report, config: Config, server: PluginServerInterface
) -> bool:
    """Whether a stored report still describes the server in front of us.

    The age window alone used to be enough, because the report only ever lived in memory. Now
    that it survives a restart, "produced within the last day" is no longer the same as "still
    true": the admin could have swapped the mods folder, changed loader, or upgraded Minecraft
    in between, and reusing the old answer would then describe a server that no longer exists.

    Two of those are checkable for free, and they are the two that matter:

    * the mods directory the report was produced from has to be the one configured now;
    * ``server.mc_version`` has to match when it is *pinned*. With the default ``auto`` there
      is nothing cheap to compare against — detection reads the log and, failing that, every
      jar — so an auto-detected version change can still be missed. The README already tells
      an admin upgrading a major version to pin the version explicitly, and this is one more
      reason that advice pays off.
    """
    expected = str(
        resolve_mods_directory(
            _working_directory(server), config.server.mods_directory
        )
    )
    if report.mods_directory != expected:
        return False

    configured_loader = (config.server.loader or "").strip().lower()
    if configured_loader and report.server.loader != configured_loader:
        return False

    pinned = (config.server.mc_version or "").strip()
    if pinned and pinned.lower() != i18n.AUTO and report.server.mc_version != pinned:
        return False
    return True


def _write_report_files(server: PluginServerInterface, report: Report,
                        announce: bool = True) -> None:
    """Persist the report as JSON (for scripts) and as text (for a human).

    ``announce=False`` for the write that follows a deletion: the "报告已写入" line belongs to
    "a check just finished", and printing it after a command makes it read as though something
    was checking behind the admin's back.
    """
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
    if announce:
        server.logger.info(
            tr("console.report_saved", path=os.path.join(folder, REPORT_FILE_NAME))
        )


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
        # silently discarded. So is every click: the log line is text. The screens in
        # ``_reply_summary`` are where the colour and the buttons are, because that is the
        # path that actually renders them.
        for line in lines:
            server.logger.info(line)
    else:
        server.logger.info(tr("check.finished_clean"))

    if source is not None:
        _reply_summary(source, report)

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
    if folder.is_dir() and ledger.prune(folder):
        # Records for files that are no longer there: the admin installed them, or removed
        # them. Either way the bookkeeping should follow.
        ledger.save()
    classify_downloaded(report.entries, folder, ledger)
    return folder, ledger


def _reconcile_downloads(
    server: PluginServerInterface, report: Report, config: Config
) -> Tuple[int, int]:
    """Bring the download folder and the report into agreement, fetching what is missing.

    Wholly self-contained. The check has already succeeded by the time this runs, so nothing in
    here — a misconfigured folder, an unreachable host, a full disk, a bug in the summary
    formatting — may turn a successful check into a reported failure.

    Returns ``(count, bytes)`` for the files that were **newly** fetched, which is what the
    completion notice is about: a file that was already sitting in the folder is not news, and
    announcing it again on every run is how the notice would become noise.
    """
    http: Optional[HttpClient] = None
    fetched_count = 0
    fetched_bytes = 0
    try:
        folder, ledger = _sync_download_state(server, report, config)
        if folder is None or not config.download.enabled:
            return 0, 0

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
        arrived = [outcome for outcome in outcomes if outcome.status == STATUS_DOWNLOADED]
        fetched_count = len(arrived)
        fetched_bytes = sum(outcome.bytes_written or 0 for outcome in arrived)
    except Exception as error:  # noqa: BLE001 - see the docstring
        server.logger.warning(tr("download.crashed", error="{}: {}".format(
            type(error).__name__, error)))
    finally:
        if http is not None:
            http.close()
    return fetched_count, fetched_bytes


def _announce_download_complete(
    server: PluginServerInterface, count: int, size: int, broadcast: bool = True
) -> None:
    """Say that files landed during a check, and what the admin can do about them.

    Written for the moment, not for the state: the summary that follows describes where things
    stand, while this says something *finished* — and the one thing a reader needs from that is
    the next command, which the summary does not spell out in the same breath. It also names
    the automatic setting when it is on, because on such a server "install them" is already
    handled and telling the admin to do it would be telling them to do nothing.

    The in-game half is deliberately **not** gated on ``report.in_game``. That setting governs
    announcements that an update *exists* — information that is equally true tomorrow, and that
    the admin also gets at their next login. This is news that this server's own files changed
    a moment ago, the same category as the summary of what the last stop installed, which
    reaches the same people the same way. The audience is still the permission-gated one; a
    player who cannot act on it is not told.
    """
    server.logger.info(tr("download.complete", count=count, size=_format_size(size)))
    if _config.download.install_on_stop:
        server.logger.info(tr("download.complete_automatic"))
    else:
        server.logger.info(
            tr("download.complete_manual", command=ROOT_LITERALS[0] + " install")
        )

    if not broadcast or not (_server_running(server) and _online_players):
        return

    lines = [Notice(tr("download.complete_in_game", count=count, size=_format_size(size)),
                    "heading")]
    lines.append(
        Notice(
            tr("download.complete_in_game_automatic")
            if _config.download.install_on_stop
            else tr("download.complete_in_game_manual"),
            "hint",
        )
    )
    for name in _permitted_players(
        server, sorted(_online_players), _config.report.in_game_permission
    ):
        _tell_player(server, name, lines)


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
        write_json(path, payload)
    except OSError as error:
        server.logger.warning(tr("install.report_failed", error=str(error)))


def _read_install_report(server: PluginServerInterface) -> Optional[Dict[str, Any]]:
    path = Path(server.get_data_folder()) / INSTALL_REPORT_FILE_NAME
    if not path.is_file():
        return None
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("installed"), list):
        return None
    return data


def _mark_install_reported(server: PluginServerInterface, data: Dict[str, Any], field: str) -> None:
    data[field] = True
    path = Path(server.get_data_folder()) / INSTALL_REPORT_FILE_NAME
    try:
        write_json(path, data)
    except OSError:
        # Losing the flag means the message may be repeated, which is a far smaller problem
        # than failing the startup it is being written during.
        pass


def _reason_text(family: str, code: str) -> str:
    """A short reason code as words, or the text itself when it is not a known code.

    ``translate`` returns the key when it has no entry, which is exactly the signal needed here:
    a failure carries a sentence (``could not move the new jar in: ...``) rather than a code, and
    that sentence is more useful than a missing-key placeholder.

    The family is named by the caller rather than guessed, because the two stages overlap:
    ``no-hash-to-verify`` means "the downloader would not accept this file" in one and "the
    installer will not install it" in the other, and they are different sentences. Trying both
    prefixes would print the wrong one half the time, depending only on the order they were
    tried in.
    """
    text = str(code or "")
    key = family + (text or "unknown")
    translated = tr(key)
    return text if translated == key else translated


def _install_summary_lines(data: Dict[str, Any]) -> List[Notice]:
    """The lines that describe one install batch, for the console and for chat.

    Roles rather than colours, because the two readers disagree about colour: the console drops
    it entirely (``server.logger`` stringifies its argument, and ``RTextBase.__str__`` returns
    plain text), while chat paints by role. The role is the one thing both can share.

    A replaced mod is ``done``: nothing about it needs anybody any more. Anything that *failed*
    to install is ``blocked`` — red, the same colour the listing uses for a mod that needs
    looking at. Until v1.5.0 both were the same yellow, so a clean batch looked exactly as
    alarming as a broken one.
    """
    installed = data.get("installed") or []
    lines = [
        Notice(
            tr("install.header", count=len(installed), when=str(data.get("at") or "")),
            "heading",
        )
    ]
    for item in installed[:NOTIFY_MAX_UPDATES]:
        lines.append(
            Notice(
                tr("install.line", name=item.get("name") or "?",
                   version=item.get("version") or "?",
                   old=item.get("backup_file") or "?"),
                "done",
            )
        )
    if len(installed) > NOTIFY_MAX_UPDATES:
        lines.append(
            Notice(tr("report.and_more", count=len(installed) - NOTIFY_MAX_UPDATES), "hint")
        )
    skipped = data.get("skipped") or []
    if skipped:
        # ``blocked``（红）：装不上是「要人看一眼」，与列表里红色行同一个意思。
        lines.append(Notice(tr("install.skipped_header", count=len(skipped)), "blocked"))
        for item in skipped[:NOTIFY_MAX_UPDATES]:
            lines.append(
                Notice(
                    tr("install.skipped_line", name=item.get("name") or "?",
                       reason=_reason_text("install.reason.",
                                           str(item.get("detail") or "unknown"))),
                    "blocked",
                )
            )
    return lines


def _install_on_stop(server: PluginServerInterface) -> None:
    """Replace installed jars with the builds fetched for them. Runs once the server is down.

    The event is the whole safety story: ``mods/`` is only written while nothing is reading it.
    Everything else this function does — the ledger as the work list, the hash check, the
    ``.old`` backup, the skip on a name clash — exists so that a mistake here costs a log line
    rather than a modpack.

    Two ways in, and the ledger decides which records each covers:

    * ``download.install_on_stop`` — every download this plugin made;
    * ``!!muc install <编号>`` — exactly the ones an admin authorised, which is what makes the
      command work whether or not the automatic setting is on.

    When the automatic setting *is* on, the two are the same instruction and the per-record
    flag is not consulted: an admin who switches the feature on has already said "install what
    you fetch", and honouring only the explicitly named ones would silently contradict that.
    """
    automatic = bool(_config.download.install_on_stop)

    mods, downloads = _install_paths(server, _config)
    if mods is None or downloads is None:
        return

    ledger = DownloadLedger(
        Path(server.get_data_folder()) / DOWNLOAD_LEDGER_FILE_NAME, logger=server.logger
    )
    options = InstallOptions(
        mods_folder=mods, downloads_folder=downloads, approved_only=not automatic
    )
    # Asked before anything is written, so a stop with nothing to install leaves no
    # install report behind for the next start to announce.
    if not pending_records(ledger, options.approved_only):
        return
    if not mods.is_dir():
        server.logger.warning(tr("install.no_mods_folder", directory=str(mods)))
        return
    if not downloads.is_dir():
        return

    results = install_pending(
        ledger, options,
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
    for notice in _install_summary_lines(data):
        server.logger.info(notice.text)
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


def _download_tally(outcomes: Sequence[DownloadOutcome]) -> Dict[str, int]:
    """How many outcomes landed in each bucket, by status. Every bucket is present, even empty.

    One function rather than the same four lines at both places that report a batch — the
    console summary and the reply to a manual ``download all``. They quote the same four
    numbers, and two copies of "which statuses count as what" is how they would eventually
    disagree about it.
    """
    tally = {status: 0 for status in (STATUS_DOWNLOADED, STATUS_ALREADY_PRESENT,
                                      STATUS_SKIPPED, STATUS_FAILED)}
    for outcome in outcomes:
        tally[outcome.status] = tally.get(outcome.status, 0) + 1
    return tally


def _log_download_outcomes(
    server: PluginServerInterface,
    report: Report,
    outcomes: Sequence[DownloadOutcome],
    folder: Path,
) -> None:
    """Say what landed, what was already there, and what did not work — and why."""
    if not outcomes:
        return

    counts = _download_tally(outcomes)
    by_file: Dict[str, DownloadOutcome] = {}
    for outcome in outcomes:
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
    #
    # Ran through ``_reason_text`` because a skip arrives as a code (``no-download-url``), and
    # a code is not something to put in front of an admin. Failures are not: those already
    # carry a sentence, and translating one would flatten the detail that makes it useful.
    skipped_reasons = sorted({_reason_text("download.reason.", outcome.detail) for outcome in outcomes
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
            entry.add_note("note.download_skipped",
                           reason=_reason_text("download.reason.", outcome.detail))


def _entry_for(report: Report, file_name: str):
    for entry in report.entries:
        if entry.file_name == file_name:
            return entry
    return None


# --------------------------------------------------------------------------------------
# In-game screens
#
# Every command that prints more than a sentence opens with the same title bar and then draws
# its own body, so the six of them read as one plugin's output rather than six. The colour
# vocabulary is the same on every screen, and small enough to hold in the head:
#
#   gold    the title bar, and nothing else
#   aqua    anything the reader can act on — a button, a command, a field's label
#   white   content: a mod's name, a version, a section heading, a field's value
#   yellow  a row that needs somebody to do something about it
#   gray    the rest: a hint, a footnote, a row that needs nothing
#
# The rows used to be coloured by guessing from their text — ``"->" in line`` meant yellow —
# which coloured a row by what it happened to contain rather than by what it meant, and made
# the same mod a different colour on two different screens.
# --------------------------------------------------------------------------------------


def _title(source: CommandSource, server: Optional[PluginServerInterface] = None) -> None:
    """Open a screen. Every screen starts here, which is what makes them look alike."""
    source.reply(_title_line(server or _server))


def _context_field(report: Report) -> RTextList:
    """``服务端: 26.3 / Fabric（版本来源：server_info）`` — the line under the title bar.

    Built from the same two catalogue entries the status screen uses, so the screens that show
    it cannot drift apart. The log keeps its own form (``report.header``, prefixed with the
    plugin's badge) because a log line has to name the plugin while a screen already has the
    title bar for that.
    """
    server = report.server
    return _field(
        tr("command.status.server_label"),
        tr("command.status.server", version=server.mc_version or "?",
           loader=server.loader, source=server.mc_version_source),
        RColor.white,
    )


#: 一种状态用什么颜色。**唯一的一份**：列表行的状态图标、``[状态]`` 浮窗的第一行、汇总屏的分节
#: 标题都查这里；通知里按状态上色的行在 ``_NOTICE_COLOURS``，用的是同一套颜色。
#:
#: 绿 = 已是最新，蓝 = 有得更新（含已下载待安装），红 = 有问题（无适配、查询出错），其余一律
#: 灰——它们是信息而不是「要做什么」：本地比上游新、无法定位上游、不是 Mod、被忽略、旧版备份。
#: （用户 2026-10-10 定稿：打勾绿、上下箭头蓝。）
_STATUS_COLOURS = {
    STATUS_UP_TO_DATE: RColor.green,
    STATUS_UPDATE_AVAILABLE: RColor.blue,
    STATUS_AWAITING_INSTALL: RColor.blue,
    STATUS_NO_COMPATIBLE_BUILD: RColor.red,
    STATUS_ERROR: RColor.red,
}


def _status_colour(status: str) -> Any:
    return _STATUS_COLOURS.get(status, RColor.gray)


#: 列表行里每一块怎么画。角色由 ``index_row_fields`` 给，颜色由这张表定：**号码永远黄、
#: 名称永远白**，``[版本]`` 是绿的（见 ``_row_field_colour``）、``[详细信息]`` 是 aqua，
#: 状态格 ``[状态: ✔]`` 的**文字部分永远黄**——而里面的**图标**查 ``_STATUS_COLOURS``
#: （打叉红、打勾绿、有更新与待安装蓝……），所以它不在这张表里，走的是 ``_row_field_colour`` 的第一支。
#:
#: 以前整行只有一个颜色（「要人管就黄、否则灰」），于是号码的颜色会随行而变——读者看到的就是
#: 「为什么 1 号是黄的、其他都是灰的」。一列号码就该看起来像一列；会变颜色的是状态。
_ROW_FIELD_COLOURS = {
    ROW_FIELD_NUMBER: RColor.yellow,
    ROW_FIELD_NAME: RColor.white,
    ROW_FIELD_BODY: RColor.white,
    ROW_FIELD_NOTE: RColor.yellow,
}

#: ``[版本]`` 浮窗里每一块怎么画（角色由 ``version_summary`` 给）：**红=要被换掉的那个、
#: 绿=新版本、黄=你手上的版本、灰=上游那个但不构成更新**，箭头与分隔符是白的。
#: 红绿在这里是「换」的两端（红=被换下的、绿=换上的），与状态词表各自独立——状态色见 ``_STATUS_COLOURS``。
_VERSION_COLOURS = {
    VERSION_OLD: RColor.red,
    VERSION_ARROW: RColor.white,
    VERSION_NEW: RColor.green,
    VERSION_CURRENT: RColor.yellow,
    VERSION_UPSTREAM: RColor.gray,
    VERSION_PLAIN: RColor.white,
}


def _version_hover(entry: UpdateEntry) -> RTextList:
    """``[版本]`` 的浮窗：按 ``_VERSION_COLOURS`` 上色的几段，拼成一个 tooltip。"""
    tooltip = RTextList()
    for kind, text in version_summary(entry, tr):
        tooltip.append(RText(text, _VERSION_COLOURS[kind]))
    return tooltip


def _row_field_colour(entry: UpdateEntry, role: str) -> Any:
    if role == ROW_FIELD_MARK:
        # 图标是这一格里唯一说「是哪个状态」的东西，所以状态色贴在它身上（用户点名：打叉红、
        # 打勾绿、有更新与待安装蓝）。浮窗第一行用的是**同一个函数**，所以行上的图标和浮窗的
        # 标题不会各说各话。
        return _status_colour(entry.status)
    if role == ROW_FIELD_BODY and has_version_label(entry):
        # ``[版本]`` 永远绿：它是一个把手（把鼠标移上去就能看到底是什么版本），不是状态。
        return RColor.green
    return _ROW_FIELD_COLOURS[role]


def _reply_lines(source: CommandSource, lines) -> None:
    """One grey line per message — for a screen's closing lines, which are never the point.

    An :class:`RTextBase` line is passed through as it is: the staged plans' closing line
    (``_confirm_ask``) is built from pieces, because the command inside it is a red button, and
    wrapping it in ``RText`` again would flatten the colours and the click away.
    """
    for line in lines:
        if isinstance(line, RTextBase):
            source.reply(line)
        else:
            source.reply(RText(line, RColor.gray))


def _detail_link(command: str, hover: str = "") -> RText:
    """A ``[详细信息]`` label that runs ``command`` when clicked.

    Clicking is by number rather than by mod id: the number is what the reader sees, and
    ``!!modupdate info 3`` is short enough to type if the chat log has since scrolled past the
    row. The command is spellable by hand, so a player is never stuck without the button.

    ``hover`` is the tooltip, and it names the mod rather than the number the click happens to
    carry: the reader hovering is asking "what will this do", and ``info <mod 名>`` is the
    answer a person can read and type, number or no number.
    """
    link = RText(tr("command.list.detail_link"), RColor.aqua).set_click_event(
        RAction.run_command, command
    )
    if hover:
        link.set_hover_text(hover)
    return link


def _status_hover(entry: UpdateEntry) -> RTextList:
    """The ``[状态: ✔]`` tag's tooltip: which state it is, what that means, and how sure we are.

    The tag on the row is the same two words for every mod, so everything the reader wants from
    it is in here. The **first line is the status itself, in the status colour** — red for a
    problem, green for something to fetch, yellow for "you already have the newest" — because
    that colour used to live on the status cell and the cell is now uniform by request. Then the
    sentence that used to sit in parentheses on every row, and the identification caveat, which
    matters: a mod matched loosely by name is a *suggestion*, and the reader hovering the tag is
    asking exactly that.
    """
    tooltip = RTextList(RText(tr("status." + entry.status), _status_colour(entry.status)))
    tooltip.append("\n")
    tooltip.append(RText(tr("explain." + entry.status), RColor.white))
    if entry.matched_by in MATCHED_BY_NOTEWORTHY:
        tooltip.append("\n")
        tooltip.append(RText(tr("matched_by." + entry.matched_by), RColor.gray))
    return tooltip


def _facts_hover(entry: UpdateEntry) -> str:
    """The tooltip behind ``[备份]`` / ``[文件]``: the facts the plain form prints on the row."""
    if entry.status == STATUS_OLD_BACKUP:
        return tr("line.old_backup", size=_format_size(entry.size_bytes), days=entry.age_days)
    return entry.file_name


def _entry_row(number: Optional[int], entry: UpdateEntry, prefix: str,
               verbose: bool = True, chat_form: bool = True) -> RTextList:
    """One mod as a clickable row: the pieces, coloured, then the button that opens its detail.

    Shared by the listing and the summary so a row looks and behaves the same on both, and so
    the button always carries the number the command takes. A row with no number — impossible
    for a real report, but the summary indexes by identity — simply has no button.

    ``chat_form`` picks the row's shape, and it is the caller's answer to "can this reader use
    a mouse?":

    * the **chat** form (:func:`report.chat_row_fields`) pads the name into a column, collapses
      the facts to a green ``[版本]`` / white ``[备份]`` / ``[文件]`` handle and turns the status
      into its own ``[状态]`` tag — with a tooltip on **every** piece, because that is where the
      numbers, sizes, file names and explanations went;
    * the **plain** form keeps name, facts and the parenthetical note on the row, and is what
      the console and the log get — a log line cannot be hovered, so hiding anything behind a
      tooltip there would simply lose it. The caller decides because the source is the only
      thing that knows; when the console was handed the chat form it silently lost every
      version number to a label it has no way to open.
    """
    fields = (chat_row_fields(number, entry, tr, verbose=verbose)
              if chat_form else
              index_row_fields(number, entry, tr, verbose=verbose))
    row = RTextList()
    for role, text in fields:
        piece = RText(text, _row_field_colour(entry, role))
        if chat_form:
            if role == ROW_FIELD_NAME:
                # 全名：名字被截成 `...` 时，这是它唯一的去处。
                piece.set_hover_text(entry.name)
            elif role == ROW_FIELD_BODY:
                piece.set_hover_text(_version_hover(entry) if has_version_label(entry)
                                     else _facts_hover(entry))
            elif role in (ROW_FIELD_NOTE, ROW_FIELD_MARK):
                piece.set_hover_text(_status_hover(entry))
        row.append(piece)
    if number is not None:
        row.append(RText("  "))
        row.append(_detail_link(
            "{} info {}".format(prefix, number),
            tr("command.list.detail_hover",
               command="{} info {}".format(prefix, entry.name)),
        ))
    return row


def _reply_index(
    source: CommandSource, report: Report, entries=None, page: int = 1,
    filter_text: str = "", prefix: str = ROOT_LITERALS[0],
) -> None:
    """The numbered listing with a click on every row, and a pager at the bottom.

    The rows and their selection come from ``report.render_index``; this only decorates them.
    Keeping the two apart is what stops the chat reply from being the place where the page
    budget is computed — which it was, briefly, and it came out two lines too long.

    ``filter_text`` is the status the reader narrowed with, carried so the pager's commands
    page within that filter instead of quietly widening to everything.

    Players close with the rule that names the two handles on the rows; a console gets the
    plain rule, because the same sentence would describe hovering and clicking to a terminal.
    """
    is_player = getattr(source, "is_player", False)
    rows, tail, page_info = render_index(
        report, tr, entries=entries, page=page, budget=CHAT_PAGE_LINES
    )
    _title(source)
    source.reply(_context_field(report))
    for number, entry, _text in rows:
        source.reply(_entry_row(number, entry, prefix, chat_form=is_player))
    _reply_lines(source, tail)
    if page_info is not None and page_info[1] > 1:
        source.reply(_pager_row(page_info[0], page_info[1], filter_text, prefix, source))
    source.reply(_rule_line(_rows_rule_hint() if is_player else ""))


def _list_command(prefix: str, filter_text: str, target: int) -> str:
    """The command that opens page ``target`` of the listing the reader is looking at."""
    parts = [prefix, "list"]
    if filter_text:
        parts.append(filter_text)
    if target > 1:
        parts.append(str(target))
    return " ".join(parts)


def _pager_button(label: str, active: bool, command: str) -> RText:
    """A pager button: aqua and clickable, or dark gray and dead at the end of the range.

    The unavailable side stays on the line — the reader asked for a title-bar-shaped strip, and
    a bar that loses one end reads as broken rather than as "there is no page that way".
    """
    if active:
        return RText(label, RColor.aqua).set_click_event(RAction.run_command, command)
    return RText(label, RColor.dark_gray)


def _pager_row(
    page: int, pages: int, filter_text: str, prefix: str, source: CommandSource
) -> RText:
    """``========  [上一页] 1/2 [下一页]  ========`` — or, for a console, the commands spelled out.

    Players get the buttons, because their client can run a click. The line is shaped like the
    title bar — gold rules either side, aqua for what can be pressed, yellow for the figure —
    so the bottom of a listing looks like it belongs to the same plugin as the top. A button
    with nowhere to go is gray rather than missing, so the bar keeps its shape from page to
    page. The console gets the command text instead: a label nobody can press is decoration,
    and ``[上一页]`` is exactly that in a terminal. The page figure is on the line either way,
    so "which page am I on" is answered for both readers.
    """

    def command_for(target: int) -> str:
        return _list_command(prefix, filter_text, target)

    if not getattr(source, "is_player", False):
        return RText(render_pager(page, pages, tr, command_for), RColor.gray)

    caption = "{} {} {}".format(
        tr("command.list.prev"),
        tr("command.list.page_short", page=page, pages=pages),
        tr("command.list.next"),
    )
    bars = max(_TITLE_BAR_MIN, (_TITLE_WIDTH - display_width(caption) - 4) // 2)
    rule = "=" * bars

    row = RTextList(RText(rule, RColor.gold), "  ")
    row.append(_pager_button(tr("command.list.prev"), page > 1, command_for(page - 1)))
    row.append(" ")
    row.append(RText(tr("command.list.page_short", page=page, pages=pages), RColor.yellow))
    row.append(" ")
    row.append(_pager_button(tr("command.list.next"), page < pages, command_for(page + 1)))
    row.append("  ")
    row.append(RText(rule, RColor.gold))
    return row


def _reply_summary(source: CommandSource, report: Report,
                   prefix: str = ROOT_LITERALS[0]) -> None:
    """The summary as a screen: the same sections the log gets, with clickable rows.

    Sections, headings, truncation and the "and N more" arithmetic all come from
    :func:`report.summarise`, which is also what builds the log form — so the two can disagree
    about colour and about links, which is the point, but not about what happened.

    The project url is not repeated here, and that is deliberate. In the chat it wrapped every
    row onto a second line and could not be clicked; the reading it was meant to serve is one
    button away, which is also where the number the next command needs is. The url is still in
    the log line and in ``last_report.json``, which is where something that fetches files
    automatically would look for it.
    """
    _context, blocks, closing = summarise(report, tr)
    numbers = {id(entry): number for number, entry in report.indexed_entries()}
    is_player = getattr(source, "is_player", False)

    _title(source)
    source.reply(_context_field(report))
    for block in blocks:
        if isinstance(block, SummarySection):
            # 有状态的组：标题按状态上色（这就是这一屏的「状态字段」）。没有状态的组（理论上
            # 不存在，分组都带状态）保持白色。
            source.reply(RText(block.heading,
                               _status_colour(block.status) if block.status else RColor.white))
            for entry in block.entries:
                source.reply(_entry_row(numbers.get(id(entry)), entry, prefix, verbose=False,
                                        chat_form=is_player))
            _reply_lines(source, block.trailing)
        else:
            source.reply(RText(block, RColor.gray))
    _reply_lines(source, closing)
    source.reply(_rule_line(_rows_rule_hint() if is_player else ""))


def _reply_detail(
    source: CommandSource, entry: UpdateEntry, prefix: str = ROOT_LITERALS[0]
) -> None:
    """One mod's detail: its versions, its project page, the one action, and its notes.

    The rows come from ``report.entry_detail_rows``, which also decides where the action goes —
    where the download link used to sit. This only turns a row into a reply: a url opens in the
    browser, a command runs, and everything else is text.
    """
    report = _last_report
    number = _number_of(report, entry) if report is not None else None
    action = action_row(entry, number, prefix, tr, delete_allowed=_deletion_allowed())

    _title(source)
    for row in entry_detail_rows(entry, tr, action=action):
        if row.url:
            value = (RText(row.value, RColor.aqua)
                     .set_click_event(RAction.open_url, row.url)
                     .set_hover_text(row.url))
        elif row.command:
            value = (RText(row.value, RColor.aqua)
                     .set_click_event(RAction.run_command, row.command))
        else:
            value = RText(row.value, RColor.white)
        source.reply(RTextList(RText(row.label, RColor.aqua), value) if row.label else value)
    source.reply(_rule_line())


def _notification_lines(report: Report) -> List[Notice]:
    """The body of an in-game notification, line by line with its role.

    Two sections, because the two situations ask for different things and merging them would
    make the more urgent one invisible: "these need fetching" and "these are fetched, install
    them". A mod appears in exactly one of them, which is what stops an update being announced
    again after it has already been downloaded. Both sections' rows ask somebody to act, so
    both are ``action``; the headings stay white and the footnotes gray, which is what keeps a
    six-line message readable as one block instead of one yellow smear.
    """
    lines: List[Notice] = []

    updates = report.updates
    if updates:
        lines.append(Notice(tr("check.in_game_header", count=len(updates)), "heading"))
        for entry in updates[:NOTIFY_MAX_UPDATES]:
            # ``update`` is green — the same colour the listing gives an available update, so the
            # notification and the listing agree about what green means.
            lines.append(Notice(entry_line_text(entry, tr), "update"))
        if len(updates) > NOTIFY_MAX_UPDATES:
            lines.append(Notice(tr("report.and_more",
                                   count=len(updates) - NOTIFY_MAX_UPDATES), "hint"))
        # The list is the same every start until the admin acts on it, so the one thing worth
        # adding is which part of it just appeared.
        if report.new_since_last:
            lines.append(Notice(tr("report.new_since_last", count=len(report.new_since_last),
                                   names=", ".join(report.new_since_last[:6])), "hint"))

    pending = report.awaiting_install
    if pending:
        lines.append(Notice(tr("check.in_game_awaiting", count=len(pending)), "heading"))
        for entry in pending[:NOTIFY_MAX_UPDATES]:
            lines.append(Notice(entry_line_text(entry, tr), "update"))
        if len(pending) > NOTIFY_MAX_UPDATES:
            lines.append(Notice(tr("report.and_more",
                                   count=len(pending) - NOTIFY_MAX_UPDATES), "hint"))
        lines.append(Notice(tr("check.in_game_awaiting_where"), "hint"))

    if not lines:
        lines.append(Notice(tr("report.no_updates"), "hint"))
    elif updates or pending:
        # A truncated list with no way onward is a dead end, so the notification says which
        # command carries the rest.
        lines.append(Notice(tr("check.in_game_more_hint"), "hint"))
    return lines


def _tell_player(server: PluginServerInterface, player: str, lines: Sequence[Notice]) -> None:
    """Send a few lines to one player, each coloured by what it is for.

    ``server.tell`` rather than a hand-built ``tellraw``: it goes through the active handler's
    own "send message" command (so it is right for whatever handler the server runs, not just
    the vanilla-derived ones), it escapes the payload, and it uses the receiving player's
    preferred language. Delivery is best-effort — the player may have disconnected while a
    check was running.

    The colouring lives here rather than in each builder so the vocabulary stays in one place;
    the *roles* are decided by the builders, which are the only layer that knows whether a
    line is a heading or a row. Until v1.5.0 every one of these messages was painted one
    colour, which made a finished install look exactly as urgent as a broken one.
    """
    if not _server_running(server):
        return
    message = RTextList()
    for index, notice in enumerate(lines):
        if index:
            message.append(RText("\n"))
        message.append(RText(notice.text, _NOTICE_COLOURS.get(notice.role, RColor.yellow)))
    try:
        server.tell(player, message)
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
    told about updates", while ``report.admin_permission`` is "who counts as an admin worth
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
      is ``report.reuse_report_minutes``; set it to ``0`` to always re-check.
    * **A check that could not start still answers.** If another check holds the lock, the
      admin gets the previous report with its age, which beats silence.
    """
    if _stop_event.is_set():
        return

    report = _last_report
    window_minutes = max(0, int(_config.report.reuse_report_minutes))
    age = report.age_seconds() if report is not None else None
    # Age is not the only question: a report carried across a restart, or a mods folder that
    # has been pointed somewhere else since, describes a server this admin is not on.
    reused = (
        report is not None
        and age is not None
        and 0 < age <= window_minutes * 60
        and _report_still_applies(report, _config, server)
    )

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
        _tell_player(server, player, [Notice(tr("check.admin_join_no_report"), "hint")])
        return

    lines = [Notice(tr("check.admin_join_header", version=report.server.describe()), "heading")]
    lines.extend(_notification_lines(report))
    if reused:
        lines.append(
            Notice(tr("check.admin_join_reused", minutes=int((age or 0) // 60)), "hint")
        )
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
    """``summary`` — the last check as a screen, without running anything.

    This used to be what the bare command did. Since the bare command is the help page (the
    project convention), this is how the stored result is read back on demand.
    """
    if _last_report is None:
        source.reply(tr("command.no_report_yet"))
        return
    _reply_summary(source, _last_report)


def _show_list(source: CommandSource, raw: str = "", prefix: str = ROOT_LITERALS[0]) -> None:
    """``list [状态] [页码]`` — the numbered index, optionally filtered and paged.

    Two optional words in one argument, told apart by shape rather than by position: a status
    name narrows the rows, a plain number picks the page (``list 2``), and ``list awaiting_install
    2`` does both — the number last, because that is where the pager buttons put it. A status is
    never a number, so there is nothing to disambiguate.

    Numbers keep their full-list meaning when a filter is on: numbering the filtered set would
    make a click ambiguous, since the same number would mean different mods depending on which
    command produced the row. One numbering means a number always identifies a mod.
    """
    tokens = (raw or "").split()
    page = 1
    if tokens and tokens[-1].isdigit():
        page = int(tokens.pop())
    status_text = " ".join(tokens).strip()
    wanted = status_text.lower()
    # The typo is answered first, report or no report: telling a reader with a misspelled
    # status that no check has run yet would send them to fix the wrong thing.
    if wanted and wanted not in ALL_STATUSES:
        source.reply(
            tr("command.unknown_status", value=status_text, options=", ".join(ALL_STATUSES))
        )
        return
    if _last_report is None:
        source.reply(tr("command.no_report_yet"))
        return

    entries = _last_report.by_status(wanted) if wanted else None
    _reply_index(source, _last_report, entries=entries, page=page,
                 filter_text=wanted, prefix=prefix)


def _resolve_handle(
    source: CommandSource,
    report: Report,
    text: str,
    prefix: str = ROOT_LITERALS[0],
    action: str = "info",
) -> Optional[UpdateEntry]:
    """Look up what the admin typed, and explain it when that fails.

    The three failures get three sentences, because they lead to three different next actions:
    a number past the end of the list means "look at the list again", an ambiguous handle means
    "type more of it" — and the names that matched are offered as clickable completions, which
    is the in-game stand-in for tab completion — and an unknown one means "that mod is not in
    this report". A single "not found" would leave the reader guessing which of the three they
    hit.

    ``prefix`` and ``action`` are used only for those completion buttons: clicking one **fills
    the input box** with ``!!muc download Sodium`` rather than running it, because the reader
    is mid-sentence and may still want to edit before pressing enter.
    """
    entry, reason = report.resolve_handle(text)
    if entry is not None:
        return entry
    if reason == "out-of-range":
        source.reply(tr("command.handle.out_of_range", value=text,
                        count=len(report.entries), command=prefix))
    elif reason == "ambiguous":
        _reply_ambiguous(source, report, text, prefix, action)
    else:
        source.reply(tr("command.info.unknown", value=text))
    return None


def _reply_ambiguous(
    source: CommandSource, report: Report, text: str, prefix: str, action: str
) -> None:
    """Name the mods a handle could have meant, and offer them as clickable completions.

    The clickable row is only sent to a player. In the console the names are already spelled
    out in the sentence above it, and ``[Sodium]`` with nothing to click is one more line to
    read past — the same reason the console never got the listing's buttons.
    """
    candidates = report.ambiguity_candidates(text)
    names = [entry.name for entry in candidates]
    source.reply(
        tr("command.handle.ambiguous", value=text, count=len(names),
           names=", ".join(names[:AMBIGUOUS_NAMES]))
    )
    if not getattr(source, "is_player", False):
        return
    row = RTextList()
    for index, name in enumerate(names[:AMBIGUOUS_NAMES]):
        if index:
            row.append(RText("  "))
        row.append(
            RText("[{}]".format(name), RColor.aqua).set_click_event(
                RAction.suggest_command, "{} {} {}".format(prefix, action, name)
            )
        )
    source.reply(row)


def _show_info(source: CommandSource, target: str, prefix: str = ROOT_LITERALS[0]) -> None:
    """``info <编号|Mod 名|mod id|文件名>`` — one mod's version change, links and notes."""
    if _last_report is None:
        source.reply(tr("command.no_report_yet"))
        return
    text = (target or "").strip()
    if not text:
        source.reply(tr("command.info.usage"))
        return
    entry = _resolve_handle(source, _last_report, text, prefix=prefix, action="info")
    if entry is None:
        return
    _reply_detail(source, entry, prefix=prefix)


# --------------------------------------------------------------------------------------
# 旧版备份（.old）
# --------------------------------------------------------------------------------------
#
# An install never deletes the jar it replaces; it renames it to ``<name>.jar.old``. That is
# the rollback path, and it is worth keeping for a while — but not forever, and a folder that
# has been updated monthly for a year is carrying a year of superseded jars nobody will rename
# back.
#
# These two commands are the only place in the plugin that ever removes something from
# ``mods/``, and the safety model is two rules, in this order:
#
#   1. ``cleanup.allow_delete`` — off by default, and nothing is removed while it is. It gates
#      the commands, the confirmation, and the reminder, from one reader (``_deletion_allowed``)
#      so the three cannot drift apart.
#   2. ``cleanup.is_backup_name`` — a name this plugin did not create is refused, and refused
#      again at the moment of deletion rather than only when the plan was drawn up.
#
# Both forms are staged and confirmed, including the one that names a single file. Naming it is
# unambiguous, but this is the one irreversible thing an admin can ask for here: the file being
# removed is the one that exists to be renamed back. Printing the plan first is also where a
# mistyped number gets caught.


def _deletion_allowed() -> bool:
    """Whether this plugin may remove anything from ``mods/`` at all.

    One reader, three call sites — the two commands, the confirmation that carries out what they
    staged, and the reminder. They have to agree: a gate on the command but not on the
    confirmation would let an admin who staged a plan and then switched the option off still
    delete the files, which is exactly the thing the option exists to prevent.
    """
    return bool(_config.cleanup.allow_delete)


def _refuse_deletion(source: CommandSource) -> None:
    """Say no, and name the exact option and file that would change the answer.

    Not a bare refusal: "you may not" without "here is how you may" reads like a bug, and the
    next thing an admin does is go looking through the source. The path is the same one
    ``!!muc status`` prints, so it can be opened directly.
    """
    server = _server
    source.reply(tr("command.delete.disabled"))
    source.reply(
        tr(
            "command.delete.disabled_hint",
            option="cleanup.allow_delete",
            path=_config_path(server) if server is not None else CONFIG_FILE_NAME,
            command=ROOT_LITERALS[0] + " reload",
        )
    )


def _backups_now(server: Optional[PluginServerInterface]) -> List[Backup]:
    """Every ``.old`` backup in the mods folder, read fresh from the directory.

    From the directory rather than from the last report, and that is the point: a report is a
    snapshot that can be a day old, and every decision about deleting has to be about what is
    actually in the folder at the moment the decision is carried out.
    """
    if server is None:
        return []
    folder = _mods_folder(server, _config)
    return list_backups(folder) if folder is not None else []


def _expired_now(server: Optional[PluginServerInterface]) -> List[Backup]:
    """The backups old enough that the plugin would offer to remove them."""
    return expired(_backups_now(server), _config.cleanup.max_age_days)


def _backups_for(kind: str, folder: Any) -> List[Backup]:
    """The set one bulk deletion is about: everything, or only what is past the threshold.

    The two bulk forms ask different questions — ``cleanup`` means "the expired ones",
    ``delete all`` means "all of them" — and the difference has to survive the trip back to the
    confirmation, which re-derives the set from the folder to notice a change since the plan was
    printed. One shared selector would make ``delete all`` silently narrow to the expired ones
    on the way to ``confirm``: the admin would be confirming a set they were never shown, which
    is exactly what the re-derivation exists to prevent.
    """
    backups = list_backups(folder) if folder is not None else []
    if kind == _KIND_DELETE_ALL:
        return backups
    return expired(backups, _config.cleanup.max_age_days)


#: What lays the same plan out again, per bulk kind — the sentence a stale plan points at.
_BULK_DELETE_RETRY = {
    _KIND_DELETE_EXPIRED: "cleanup",
    _KIND_DELETE_ALL: "delete all",
}


def _forget_backups(gone: Iterable[str]) -> None:
    """Drop the entries for backups that are no longer on disk from the last report.

    The listing, the detail view and the summary all render *the report* — a snapshot from the
    last check — and that snapshot still held the file this plugin had just deleted. Seen in a
    real run: ``delete`` → "已删除 …" → the row is still there, with its number and its size, so
    the admin deletes it again and the second attempt ends in "已经不在了". The plugin was
    offering to act on a file it had removed itself, three times in a row.

    The stored report is rewritten too, quietly: without that, the next start reads the ghost
    back out of ``last_report.json`` and the listing resurrects it. Quietly because this is
    housekeeping after a command, not a check result — the line ``_write_report_files`` normally
    logs belongs to "a check just finished".
    """
    report = _last_report
    server = _server
    names = {name for name in gone}
    if report is None or not names:
        return
    kept = [entry for entry in report.entries if entry.file_name not in names]
    if len(kept) == len(report.entries):
        # Nothing in the report described these files — a backup that was deleted by hand, or a
        # report from before they existed. Not worth a write.
        return
    report.entries = kept
    if server is not None and _config.report.write_file:
        _write_report_files(server, report, announce=False)


def _backup_line(file_name: str, size_bytes: int, age_days: int) -> str:
    return tr("command.cleanup.plan_line", file=file_name,
              size=_format_size(size_bytes), days=age_days)


def _manual_delete(source: CommandSource, handle: str, prefix: str) -> None:
    """``delete <文件名|编号>`` — stage the removal of one backup.

    First thing checked is ``cleanup.allow_delete``, before even the argument: an admin who
    typed the command on a server where deletion is off should learn that, not be walked
    through the syntax of something that will refuse to run either way.
    """
    if not _deletion_allowed():
        _refuse_deletion(source)
        return

    report = _last_report
    if report is None:
        source.reply(tr("command.no_report_yet"))
        return
    text = (handle or "").strip()
    if not text:
        source.reply(tr("command.delete.usage", command=prefix))
        return
    if text.lower() == ALL_TARGET:
        # The bulk form, on the same word the other two verbs use. ``all`` is a reserved word
        # here for the same reason it is there: it means "every backup", even on a server that
        # happens to have a mod called ``all`` — that one is still reachable by its number.
        _manual_delete_all(source, prefix)
        return

    entry = _resolve_handle(source, report, text, prefix=prefix, action="delete")
    if entry is None:
        return
    if entry.status != STATUS_OLD_BACKUP:
        # A real number and a real file, but not one this plugin put there. Explained in full:
        # a bare refusal reads like a bug, and the next question is always "then what may I
        # delete".
        source.reply(tr("command.delete.not_a_backup", name=entry.file_name))
        return

    lines = [
        tr("command.cleanup.plan_header", count=1, size=_format_size(entry.size_bytes)),
        _backup_line(entry.file_name, entry.size_bytes, entry.age_days),
        tr("command.cleanup.plan_undo"),
        _confirm_ask(prefix),
    ]
    _stage_action("delete", source, report, entry, lines)


def _manual_cleanup(source: CommandSource, prefix: str) -> None:
    """``cleanup`` — stage the removal of every expired backup.

    The age threshold is the thing an admin actually reasons about ("keep a month of
    rollbacks"), so it is what picks the set. ``cleanup.enabled`` decides only whether the
    plugin raises the subject on its own; asking for it by name works without it — the same
    split as ``download``, where automatic behaviour needs a switch and a command does not.

    ``cleanup.allow_delete`` is a different kind of switch and is checked first: it is not about
    when to speak up but about whether this plugin may remove anything at all, so it gates the
    command itself.
    """
    if not _deletion_allowed():
        _refuse_deletion(source)
        return

    server = _server
    backups = _backups_now(server)
    if not backups:
        source.reply(tr("command.cleanup.empty"))
        return

    days = max(0, int(_config.cleanup.max_age_days))
    expired_now = expired(backups, days)
    if not expired_now:
        # "Nothing matched the threshold" is the answer an admin gets on their *first* run of
        # this command — backups are young by definition — so the message says how old the
        # oldest one is and what the two ways forward are. A bare "nothing to do" leaves the
        # reader to work out that the threshold is a number they can change.
        #
        # ``backups[0]`` is the oldest: ``list_backups`` sorts by age.
        source.reply(tr("command.cleanup.none", days=days, count=len(backups),
                        oldest=backups[0].age_days))
        source.reply(tr("command.cleanup.none_hint", command=prefix))
        return

    _stage_backup_plan(_KIND_DELETE_EXPIRED, source, expired_now, "command.cleanup.plan_header",
                       prefix)


def _manual_delete_all(source: CommandSource, prefix: str) -> None:
    """``delete all`` — stage the removal of every backup, expired or not.

    The bulk form of ``delete``, and the difference from ``cleanup`` is exactly one thing: this
    one does not look at ``cleanup.max_age_days``. ``cleanup`` is the safe bulk command — it
    keeps a month of rollbacks, which is what the threshold is for; this is the one an admin
    reaches for when the point is to clear the pile out.

    Same two steps as everything else. The plan lists every file with its size and age, which is
    where "that one is from yesterday, I want to keep it" gets caught — and it is the reason
    this can afford to ignore the threshold: the admin sees the whole set, by name, before
    anything is removed.
    """
    if not _deletion_allowed():
        _refuse_deletion(source)
        return

    backups = _backups_now(_server)
    if not backups:
        source.reply(tr("command.cleanup.empty"))
        return
    _stage_backup_plan(_KIND_DELETE_ALL, source, backups, "command.delete.all_header", prefix)


def _stage_backup_plan(kind: str, source: CommandSource, files: Sequence[Backup],
                       header_key: str, prefix: str) -> None:
    """Print the plan for a bulk deletion and stage it, whatever picked the set.

    Shared by ``cleanup`` and ``delete all``: they differ in *which* backups they are about, and
    nothing else. Two copies of the rows, the truncation and the ask-for-confirmation would be
    two chances for one of them to stop asking.
    """
    lines = [tr(header_key, count=len(files), size=_format_size(total_bytes(files)))]
    lines.extend(_backup_line(item.file_name, item.size_bytes, item.age_days)
                 for item in files[:CLEANUP_PLAN_ROWS])
    if len(files) > CLEANUP_PLAN_ROWS:
        lines.append(tr("command.cleanup.plan_more", count=len(files) - CLEANUP_PLAN_ROWS))
    lines.append(tr("command.cleanup.plan_undo"))
    lines.append(_confirm_ask(prefix))
    _stage_batch(kind, source, [item.file_name for item in files], lines)


def _stage_cleanup_notice(files: Sequence[str]) -> bool:
    """Stage the cleanup plan with no command behind it — the reminder's half of the flow.

    ``False`` when something is already staged. A reminder must never clobber a plan an admin
    is halfway through confirming: it is not an instruction from a person, and the one thing
    worse than not staging it is replacing "install these three" with "delete those two".

    ``requester`` is empty, which no command source ever produces (see ``_requester``). It
    means "whoever reads this may confirm it" — the notice is addressed to the server's admins
    rather than to one of them.
    """
    global _pending_action
    with _pending_lock:
        if _pending_action is not None:
            return False
        _pending_action = {
            "kind": _KIND_DELETE_EXPIRED,
            "files": sorted(files),
            "requester": "",
            "deadline": time.monotonic() + CONFIRM_TIMEOUT_SECONDS,
        }
    return True


def _announce_cleanup(server: PluginServerInterface, broadcast: bool = True) -> None:
    """Bring the expired backups up, once per check, when the feature is on.

    Not part of the summary, and not gated on ``report.updates_only``: this is housekeeping,
    not a check result, and folding it into a report that most servers print as a single line
    is the same as not saying it. It is gated on ``cleanup.enabled`` instead — the switch that
    means "I want to hear about this" — and that one sits behind ``cleanup.allow_delete``:
    a reminder whose only next step is a refused command is worse than silence, because it
    teaches the admin that the plugin's instructions do not work.

    The plan is staged when the slot is free, so that the sentence the admin reads ("type
    ``!!muc confirm`` to delete them") is true when they read it, rather than true only in the
    120 seconds after some unrelated event. When the slot is busy the notice says so and names
    the command that stages the plan, which is the honest version of the same sentence.
    """
    if not _deletion_allowed() or not _config.cleanup.enabled:
        return
    expired_now = _expired_now(server)
    if not expired_now:
        return

    days = max(0, int(_config.cleanup.max_age_days))
    size = _format_size(total_bytes(expired_now))
    staged = _stage_cleanup_notice([item.file_name for item in expired_now])

    server.logger.info(tr("cleanup.notice", count=len(expired_now), days=days, size=size))
    server.logger.info(
        tr("cleanup.notice_confirm", command=ROOT_LITERALS[0] + " confirm")
        if staged
        else tr("cleanup.notice_stage", command=ROOT_LITERALS[0] + " cleanup")
    )

    if not broadcast or not (_server_running(server) and _online_players):
        return
    notice = [
        Notice(tr("cleanup.notice", count=len(expired_now), days=days, size=size), "action"),
        Notice(
            tr("cleanup.notice_confirm", command=ROOT_LITERALS[0] + " confirm")
            if staged
            else tr("cleanup.notice_stage", command=ROOT_LITERALS[0] + " cleanup"),
            "hint",
        ),
    ]
    for name in _permitted_players(
        server, sorted(_online_players), _config.report.in_game_permission
    ):
        _tell_player(server, name, notice)


def _confirmed_delete(source: CommandSource, entry: UpdateEntry) -> None:
    """Remove the one backup ``delete`` staged, if it is still there."""
    server = _server
    if server is None:
        return
    folder = _mods_folder(server, _config)
    backups = list_backups(folder) if folder is not None else []
    target = next((item for item in backups if item.file_name == entry.file_name), None)
    if target is None:
        # Deleted by hand between the two commands. Not an error, and not worth inventing a
        # failure for.
        source.reply(tr("command.cleanup.gone", file=entry.file_name))
        return

    results = remove_backups(folder, [target])
    for result in results:
        source.reply(
            tr("command.cleanup.deleted_one", file=result.file_name,
               size=_format_size(target.size_bytes))
            if result.removed
            else tr("command.cleanup.failed_one", file=result.file_name, detail=result.detail)
        )
    _forget_backups([result.file_name for result in results if result.removed])


def _confirm_cleanup(source: CommandSource, pending: Dict[str, Any], prefix: str) -> None:
    """Carry out a staged cleanup, after re-deriving the set from the folder.

    Re-derived rather than reused, and compared as a set rather than counted — the rule the two
    bulk commands follow, for the same reason: a backup added or removed between the notice and
    the confirmation means the admin agreed to a different set than the one about to be acted
    on, and running the intersection would report success over work that was not done.
    """
    server = _server
    if server is None:
        return
    folder = _mods_folder(server, _config)
    doomed = _backups_for(pending["kind"], folder)
    if sorted(item.file_name for item in doomed) != pending["files"]:
        _clear_pending()
        source.reply(tr("command.action.stale_cleanup",
                        command="{} {}".format(prefix,
                                               _BULK_DELETE_RETRY[pending["kind"]])))
        return

    _clear_pending()
    removed_names = set()
    freed = 0
    failures: List[str] = []
    for result in remove_backups(folder, doomed):
        if result.removed:
            removed_names.add(result.file_name)
        else:
            failures.append(result.file_name)
    freed = sum(item.size_bytes for item in doomed if item.file_name in removed_names)
    _forget_backups(removed_names)

    source.reply(tr("command.cleanup.done", count=len(removed_names),
                    size=_format_size(freed)))
    if failures:
        source.reply(tr("command.cleanup.failed", count=len(failures),
                        names=", ".join(sorted(failures)[:AMBIGUOUS_NAMES])))


# --------------------------------------------------------------------------------------
# Fetching and installing one mod, on demand
#
# The same two steps the automatic path takes, but for a single mod the admin names, and
# available whether or not the automatic halves are switched on. Both end in something awkward
# to undo — one spends bandwidth, the other plans a change to ``mods/`` that happens while
# nobody is watching — so neither acts on the first command: ``!!muc download 3`` and
# ``!!muc install 3`` stage a plan, and ``!!muc confirm`` carries it out.
# --------------------------------------------------------------------------------------


def _requester(source: CommandSource) -> str:
    """A stable name for whoever typed a command.

    The console has no player name, and giving it the empty string would collide with a player
    whose name failed to resolve — so it gets a name of its own. Nothing is sent to it; the
    value only ever decides whose ``!!muc confirm`` matches.
    """
    player = getattr(source, "player", "")
    return str(player) if getattr(source, "is_player", False) and player else "<console>"


def _number_of(report: Report, entry: UpdateEntry) -> Optional[int]:
    """The listing's number for an entry, so a reply can name the handle to type next."""
    for number, candidate in report.indexed_entries():
        if candidate is entry:
            return number
    return None


def _clear_pending() -> None:
    global _pending_action
    with _pending_lock:
        _pending_action = None


def _confirm_ask(prefix: str) -> RTextList:
    """The ``type !!muc confirm`` line of every staged plan: the command is a red button.

    Red is the plugin's "needs you" colour, and this command really is the one thing standing
    between the plan and the irreversible part. It is clickable as a **fill**, not a run:
    ``confirm`` executes a deletion or an install, and a single stray click in a chat window
    should not be able to do that — filling the box still asks for the enter key, which is the
    same deliberate step the two-command design exists to force.

    The sentence stays **one template** in the catalogue, command included, and the code splits
    the finished string around the command to draw that word red. Two half-sentences would have
    been the other way, but then each language would hold the deadline in a different half —
    and a translation that drops a placeholder prints the template unformatted, which is exactly
    what the catalogue invariant refuses to allow. Here nothing can be dropped: if the command
    is not in the rendered sentence at all, the line falls back to plain text.

    The console gets the same line (``str()`` drops the colour and the click, not the words).
    """
    command = "{} confirm".format(prefix)
    sentence = tr("command.action.ask", seconds=CONFIRM_TIMEOUT_SECONDS, command=command)
    before, marker, after = sentence.partition(command)
    if not marker:
        return RTextList(RText(sentence, RColor.gray))
    return RTextList(
        RText(before, RColor.gray),
        RText(command, RColor.red).set_click_event(RAction.suggest_command, command),
        RText(after, RColor.gray),
    )


def _stage_action(
    kind: str, source: CommandSource, report: Report, entry: UpdateEntry, lines: Sequence[str]
) -> None:
    """Remember what ``!!muc confirm`` is about to do, and print the plan it will carry out.

    The file name is stored alongside the number on purpose. A check can finish between the two
    commands, and the numbers of the new report are a new mapping — so the confirmation
    re-resolves the handle and refuses if it no longer points at the same mod. A number that
    quietly started meaning something else is precisely the mistake the two steps exist to make
    impossible.
    """
    global _pending_action
    with _pending_lock:
        superseded = _pending_action is not None
        _pending_action = {
            "kind": kind,
            "number": _number_of(report, entry),
            "file_name": entry.file_name,
            "mod_id": entry.mod_id,
            "requester": _requester(source),
            "deadline": time.monotonic() + CONFIRM_TIMEOUT_SECONDS,
        }
    if superseded:
        source.reply(tr("command.action.superseded"))
    _reply_lines(source, lines)


def _stage_batch(
    kind: str, source: CommandSource, files: Sequence[str], lines: Sequence[str]
) -> None:
    """``_stage_action`` for the bulk commands: the payload is a set of file names.

    Names rather than numbers, for the same reason the single form stores one: a check can
    finish between stage and confirm, and the whole batch is re-derived from the current report
    before anything happens. A number that moved is one problem; a batch that silently grew or
    shrank is worse, because it either spends bandwidth the admin never agreed to or leaves out
    part of what they did. Comparing names is what makes "the same mods" mean the same thing
    across two reports.
    """
    global _pending_action
    with _pending_lock:
        superseded = _pending_action is not None
        _pending_action = {
            "kind": kind,
            "files": sorted(files),
            "requester": _requester(source),
            "deadline": time.monotonic() + CONFIRM_TIMEOUT_SECONDS,
        }
    if superseded:
        source.reply(tr("command.action.superseded"))
    _reply_lines(source, lines)


def _pending_action_for(source: CommandSource) -> Optional[Dict[str, Any]]:
    """The staged action, or ``None`` after explaining why there is nothing to confirm.

    The three ways to get here are answered separately on purpose: "you never staged anything",
    "yours expired" and "that was somebody else's" call for three different next actions, and a
    single "nothing to confirm" would leave the reader guessing which one they hit.
    """
    global _pending_action
    with _pending_lock:
        pending = _pending_action
    if pending is None:
        source.reply(tr("command.action.nothing"))
        return None
    if time.monotonic() > pending["deadline"]:
        _clear_pending()
        source.reply(tr("command.action.expired", seconds=CONFIRM_TIMEOUT_SECONDS))
        return None
    # 空字符串是「提醒替所有人准备的」——没有任何命令来源会产生它（见 _requester），
    # 所以它专门表示「读到这条的任一管理员都可以确认」。
    if pending["requester"] and pending["requester"] != _requester(source):
        source.reply(tr("command.action.other_player", player=pending["requester"]))
        return None
    return pending


def _manual_download(source: CommandSource, handle: str, prefix: str) -> None:
    """``download <编号|Mod 名|all>`` — stage a fetch of one mod, or of every mod that needs one.

    Works with ``download.enabled`` off, which is the point: the automatic setting answers
    "fetch everything you find", and an admin who wants one mod now should not have to switch it
    on and wait for a whole run to pick it up.
    """
    report = _last_report
    if report is None:
        source.reply(tr("command.no_report_yet"))
        return
    text = (handle or "").strip()
    if not text:
        source.reply(tr("command.download.usage", command=prefix))
        return
    if text.lower() == ALL_TARGET:
        _manual_download_all(source, prefix)
        return
    entry = _resolve_handle(source, report, text, prefix=prefix, action="download")
    if entry is None:
        return
    number = _number_of(report, entry)

    reason = _download_blocker(entry, prefix, number)
    if reason is not None:
        source.reply(reason)
        return

    _stage_action(
        "download", source, report, entry,
        [
            tr("command.download.plan_header", count=1),
            entry_line_text(entry, tr),
            tr("command.download.plan_file",
               file=safe_jar_name(entry.download_filename, entry.fallback_file_name()),
               size=_format_size(entry.download_size) if entry.download_size
               else tr("command.download.size_unknown")),
            _confirm_ask(prefix),
        ],
    )


def _download_all_candidates(report: Report) -> Tuple[List[UpdateEntry], List[UpdateEntry]]:
    """Split the pending updates into what can be fetched and what cannot.

    The second list is what keeps the plan honest. A project can publish a version whose files
    were all withdrawn, and the platform then hands us a version with nothing to fetch — the
    plan says how many were passed over instead of quietly listing fewer mods than the report
    shows. Both lists are returned rather than only the fetchable one so the caller can say so.
    """
    candidates: List[UpdateEntry] = []
    blocked: List[UpdateEntry] = []
    for entry in report.updates:
        if entry.download_url and entry.download_sha1:
            candidates.append(entry)
        else:
            blocked.append(entry)
    return candidates, blocked


def _manual_download_all(source: CommandSource, prefix: str) -> None:
    """``download all`` — stage one fetch covering every mod that has something to fetch."""
    report = _last_report
    if report is None:
        source.reply(tr("command.no_report_yet"))
        return

    candidates, blocked = _download_all_candidates(report)
    if not candidates:
        source.reply(
            tr("command.download.all_no_files", count=len(blocked))
            if blocked
            else tr("command.download.all_none")
        )
        return

    known = sum(entry.download_size or 0 for entry in candidates)
    unknown = sum(1 for entry in candidates if not entry.download_size)
    lines = [
        tr("command.download.all_header_unknown", count=len(candidates), unknown=unknown)
        if unknown
        else tr("command.download.all_header", count=len(candidates),
                size=_format_size(known))
    ]
    for entry in candidates[:BULK_PLAN_ROWS]:
        lines.append(entry_line_text(entry, tr))
    if len(candidates) > BULK_PLAN_ROWS:
        lines.append(tr("report.and_more", count=len(candidates) - BULK_PLAN_ROWS))
    if blocked:
        lines.append(tr("command.download.all_skipped", count=len(blocked)))
    lines.append(_confirm_ask(prefix))

    _stage_batch("download_all", source, [entry.file_name for entry in candidates], lines)


def _download_blocker(entry: UpdateEntry, prefix: str, number: Optional[int]) -> Optional[str]:
    """Why this entry cannot be fetched, or ``None`` when it can.

    One message per situation rather than a single "cannot download", because the four cases
    ask for four different things from the admin — and the one that matters most is the second:
    a build that is already in the download folder needs ``install``, and telling its owner
    "cannot download" would send them looking for a problem that does not exist.
    """
    if entry.status == STATUS_AWAITING_INSTALL:
        return tr("command.download.already", name=entry.name,
                  command="{} install {}".format(prefix, number))
    if entry.status == STATUS_UPDATE_AVAILABLE:
        if not entry.download_url or not entry.download_sha1:
            return tr("command.download.no_file", name=entry.name)
        return None
    if entry.status == STATUS_NO_COMPATIBLE_BUILD:
        return tr("command.download.no_build", name=entry.name)
    if entry.status in (STATUS_UP_TO_DATE, STATUS_LOCAL_AHEAD):
        return tr("command.download.current", name=entry.name,
                  version=entry.local_version or "?")
    return tr("command.download.unresolvable", name=entry.name,
              status=tr("status." + entry.status))


def _manual_install(source: CommandSource, handle: str, prefix: str) -> None:
    """``install <编号|Mod 名|all>`` — authorise downloaded builds for the next stop.

    The build has to be on disk already: fetching is ``!!muc download``'s job, and letting this
    command imply it would make the two-step form ambiguous about what is being confirmed.
    """
    report = _last_report
    if report is None:
        source.reply(tr("command.no_report_yet"))
        return
    text = (handle or "").strip()
    if not text:
        source.reply(tr("command.install.usage", command=prefix))
        return
    if text.lower() == ALL_TARGET:
        _manual_install_all(source, prefix)
        return
    entry = _resolve_handle(source, report, text, prefix=prefix, action="install")
    if entry is None:
        return
    number = _number_of(report, entry)

    if entry.status != STATUS_AWAITING_INSTALL:
        source.reply(
            tr("command.install.not_downloaded", name=entry.name,
               command="{} download {}".format(prefix, number))
            if entry.status == STATUS_UPDATE_AVAILABLE
            else tr("command.install.not_applicable", name=entry.name,
                    status=tr("status." + entry.status))
        )
        return

    upstream = entry.download_filename or entry.fallback_file_name()
    lines = [
        tr("command.install.plan_header"),
        tr("command.install.plan_swap", old=entry.file_name,
           new=safe_jar_name(upstream, entry.fallback_file_name())),
    ]
    if _config.download.install_on_stop:
        # Said out loud rather than silently accepted: on a server that installs everything
        # anyway this command changes nothing, and an admin who did not know that would think
        # they had just narrowed the next stop to one mod.
        lines.append(tr("command.install.plan_already_automatic"))
    lines.append(_confirm_ask(prefix))
    _stage_action("install", source, report, entry, lines)


def _manual_install_all(source: CommandSource, prefix: str) -> None:
    """``install all`` — stage the authorisation of every downloaded build."""
    report = _last_report
    if report is None:
        source.reply(tr("command.no_report_yet"))
        return

    pending = report.awaiting_install
    if not pending:
        source.reply(tr("command.install.all_none"))
        return

    lines = [tr("command.install.all_header", count=len(pending))]
    for entry in pending[:BULK_PLAN_ROWS]:
        lines.append(tr("command.install.plan_swap", old=entry.file_name,
                        new=safe_jar_name(entry.download_filename,
                                          entry.fallback_file_name())))
    if len(pending) > BULK_PLAN_ROWS:
        lines.append(tr("report.and_more", count=len(pending) - BULK_PLAN_ROWS))
    if _config.download.install_on_stop:
        lines.append(tr("command.install.plan_already_automatic"))
    lines.append(_confirm_ask(prefix))

    _stage_batch("install_all", source, [entry.file_name for entry in pending], lines)


def _manual_confirm(source: CommandSource, prefix: str) -> None:
    """``confirm`` — carry out whatever ``download`` / ``install`` / ``delete`` staged."""
    pending = _pending_action_for(source)
    if pending is None:
        return

    if pending["kind"] in _DELETE_KINDS and not _deletion_allowed():
        # The option was switched off between the two commands (``reload`` does not touch the
        # slot). The plan has lost the thing that authorised it, so it is dropped rather than
        # carried out — a gate on the command but not here would let a staged plan walk past
        # the switch, which is the one thing it exists to stop.
        _clear_pending()
        _refuse_deletion(source)
        return

    if pending["kind"] in _BULK_DELETE_KINDS:
        # 直接从磁盘重新推导集合，而不是从报告里挑：这个命令删的是文件，报告只是快照。
        _confirm_cleanup(source, pending, prefix)
        return
    if pending["kind"] in ("download_all", "install_all"):
        _confirm_batch(source, pending, prefix)
        return

    report = _last_report
    entry = report.entry_by_handle(str(pending["number"])) if report is not None else None
    if entry is None or entry.file_name != pending["file_name"]:
        # The report was replaced between the two commands, so the number means something else
        # now — act on nothing rather than on whatever it happens to point at.
        _clear_pending()
        source.reply(tr("command.action.stale", command=prefix))
        return

    number = int(pending["number"] or 0)
    if pending["kind"] == "download":
        _clear_pending()
        _confirmed_download(source, entry, number, prefix)
    elif pending["kind"] == "delete":
        _clear_pending()
        _confirmed_delete(source, entry)
    else:
        _confirmed_install(source, entry, prefix)


def _confirm_batch(source: CommandSource, pending: Dict[str, Any], prefix: str) -> None:
    """Carry out a staged bulk action, after re-deriving it from the current report.

    The *set* is compared rather than a single handle, because "these mods" is what the admin
    approved. Comparing a count would not do either: two swapped entries keep the count and
    change the work. Anything other than an exact match is dropped whole — running the
    intersection and reporting success would leave part of the batch silently undone, which is
    worse than asking for the command again.
    """
    kind = pending["kind"]
    report = _last_report
    if kind == "download_all":
        entries: List[UpdateEntry] = (
            _download_all_candidates(report)[0] if report is not None else []
        )
    else:
        entries = list(report.awaiting_install) if report is not None else []

    if sorted(entry.file_name for entry in entries) != pending["files"]:
        _clear_pending()
        source.reply(tr("command.action.stale_batch",
                        command="{} {}".format(prefix, kind.split("_", 1)[0])))
        return

    if kind == "download_all":
        _clear_pending()
        _confirmed_download_all(source, entries, prefix)
    else:
        _confirmed_install_all(source, entries, prefix)


def _confirmed_download_all(
    source: CommandSource, entries: Sequence[UpdateEntry], prefix: str
) -> None:
    """Run the batch fetch on its own thread, for the same reason the single form does.

    One transfer at a time inside that thread: the downloader is sequential by design, and a
    batch is exactly the case where twenty parallel connections would be a worse neighbour.
    """
    source.reply(tr("command.download.batch_started", count=len(entries)))

    def run() -> None:
        try:
            outcomes = _perform_manual_downloads(entries)
        except Exception as error:  # noqa: BLE001 - a failure must not take the server down
            server = _server
            if server is not None:
                server.logger.exception("batch download failed")
            _reply_to(source, tr("command.download.batch_failed", reason="{}: {}".format(
                type(error).__name__, error)))
            return
        if outcomes is None:
            _reply_to(source, tr("command.download.batch_failed",
                                 reason=tr("command.download.bad_folder")))
            return

        server = _server
        if server is not None and _last_report is not None:
            folder, _reason = resolve_download_folder(server, _config)
            if folder is not None:
                # The same lines the automatic pass prints, so a batch started by hand leaves
                # the same trace in the console as one the schedule started.
                _log_download_outcomes(server, _last_report, outcomes, folder)

        counts = _download_tally(outcomes)
        _reply_to(source, tr("command.download.batch_done",
                             downloaded=counts[STATUS_DOWNLOADED],
                             existing=counts[STATUS_ALREADY_PRESENT],
                             failed=counts[STATUS_FAILED],
                             skipped=counts[STATUS_SKIPPED]))
        failed = [outcome.name for outcome in outcomes if outcome.status == STATUS_FAILED]
        if failed:
            _reply_to(source, tr("command.download.batch_failed_names",
                                 names=", ".join(failed[:5])))
        if counts[STATUS_DOWNLOADED] or counts[STATUS_ALREADY_PRESENT]:
            _reply_to(source, tr("command.download.batch_hint",
                                 command="{} install all".format(prefix)))

    threading.Thread(
        target=run, name="mod_update_checker_batch_download", daemon=True
    ).start()


def _confirmed_download(
    source: CommandSource, entry: UpdateEntry, number: int, prefix: str
) -> None:
    """Run the fetch the plan was about, on its own thread.

    Off MCDR's command thread because it is a multi-megabyte transfer: replying first and
    fetching behind is what keeps the server's own command handling responsive. Nothing needs a
    lock — two downloads for different mods are independent, and the ledger is saved per file.
    """
    source.reply(tr("command.download.started", name=entry.name,
                    version=entry.latest_version or "?"))

    def run() -> None:
        try:
            outcome = _perform_manual_download(entry)
        except Exception as error:  # noqa: BLE001 - a failure must not take the server down
            server = _server
            if server is not None:
                server.logger.exception("manual download failed")
            _reply_to(source, tr("command.download.failed", name=entry.name,
                                 reason="{}: {}".format(type(error).__name__, error)))
            return
        if outcome is None:
            _reply_to(source, tr("command.download.failed", name=entry.name,
                                 reason=tr("command.download.bad_folder")))
        elif outcome.status in (STATUS_DOWNLOADED, STATUS_ALREADY_PRESENT):
            _reply_to(source, tr("command.download.done", name=entry.name,
                                 version=entry.latest_version or "?",
                                 path=outcome.path,
                                 command="{} install {}".format(prefix, number)))
        else:
            _reply_to(source, tr("command.download.failed", name=entry.name,
                                 reason=_reason_text("download.reason.", outcome.detail)))

    threading.Thread(
        target=run, name="mod_update_checker_manual_download", daemon=True
    ).start()


def _perform_manual_downloads(
    entries: Sequence[UpdateEntry],
) -> Optional[List[DownloadOutcome]]:
    """Fetch these entries, outside the check run. ``None`` when the folder is unusable.

    One function for the single and the bulk form, because the mechanics — the size limit, the
    retry budget, the ledger, the folder — must not be able to disagree between two commands
    that differ only in how many mods they were pointed at.
    """
    server = _server
    assert server is not None
    folder, _reason = resolve_download_folder(server, _config)
    if folder is None:
        return None

    options = DownloadOptions(
        folder=folder,
        max_bytes=max(1, int(_config.download.max_size_mb)) * 1024 * 1024,
        retries=max(0, int(_config.download.retries)),
    )
    ledger = DownloadLedger(
        Path(server.get_data_folder()) / DOWNLOAD_LEDGER_FILE_NAME, logger=server.logger
    )
    http = _make_http_client(_config)
    try:
        wanted = list(entries)
        outcomes = Downloader(http, options, logger=server.logger, ledger=ledger).run(wanted)
        # The builds are on disk now, so they stop being "updates to fetch". Done here rather
        # than left to the next check so the very next ``!!muc list`` shows the new state.
        classify_downloaded(wanted, folder, ledger)
        if _last_report is not None:
            _last_report.download_folder = str(folder)
    finally:
        http.close()
        ledger.save()
    return outcomes


def _perform_manual_download(entry: UpdateEntry) -> Optional[DownloadOutcome]:
    """The single-mod form, kept as its own seam so a test can stand in for one fetch."""
    outcomes = _perform_manual_downloads([entry])
    if outcomes is None:
        return None
    for outcome in outcomes:
        if outcome.file_name == entry.file_name:
            return outcome
    return None


def _confirmed_install(
    source: CommandSource, entry: UpdateEntry, prefix: str
) -> None:
    """Mark one downloaded build as authorised, for the next ``on_server_stop``.

    Nothing is copied here. The install only ever happens with the server down, so this writes
    a mark on the ledger record and says when it will be acted on — which is also why it needs
    a confirmation: the admin is approving an action they will not be present for.
    """
    server = _server
    assert server is not None
    ledger = DownloadLedger(
        Path(server.get_data_folder()) / DOWNLOAD_LEDGER_FILE_NAME, logger=server.logger
    )
    if not ledger.approve(entry_key(entry)):
        # The record is written when the file lands, so this means the download folder was
        # emptied or the ledger edited by hand behind the plugin's back.
        _clear_pending()
        source.reply(tr("command.install.no_record", name=entry.name))
        return
    ledger.save()
    _clear_pending()
    source.reply(tr("command.install.approved", name=entry.name,
                    version=entry.latest_version or "?",
                    command="{} status".format(prefix)))


def _confirmed_install_all(
    source: CommandSource, entries: Sequence[UpdateEntry], prefix: str
) -> None:
    """Authorise a whole batch — one ledger record at a time.

    Records rather than a flag flipped over the ledger, for the same reason the single form
    refuses to widen into the whole set: the ledger is also the work list the *next stop* will
    read, so marking all of it would authorise builds this command was never about — including
    ones fetched on a day the admin has forgotten about. A record that cannot be found is
    reported rather than created, because approving a file that nothing on disk claims would
    authorise an install that may have nothing to install.
    """
    server = _server
    assert server is not None
    ledger = DownloadLedger(
        Path(server.get_data_folder()) / DOWNLOAD_LEDGER_FILE_NAME, logger=server.logger
    )
    approved: List[UpdateEntry] = []
    missing: List[UpdateEntry] = []
    for entry in entries:
        (approved if ledger.approve(entry_key(entry)) else missing).append(entry)
    ledger.save()
    _clear_pending()

    if approved:
        source.reply(tr("command.install.all_approved", count=len(approved),
                        command="{} status".format(prefix)))
    if missing:
        source.reply(tr("command.install.all_missing", count=len(missing),
                        names=", ".join(entry.name for entry in missing[:5])))


def _reply_to(source: CommandSource, message: str) -> None:
    """Reply to a command source from a background thread, tolerating a vanished player."""
    try:
        source.reply(message)
    except Exception as error:  # noqa: BLE001 - the player may have left mid-download
        server = _server
        if server is not None:
            server.logger.debug("could not reply to {}: {}".format(source, error))


def _field(label: str, value: str, value_colour: Any = RColor.white) -> RTextList:
    """``Label: value`` with the label in aqua.

    The trailing ``: `` is part of the translated label rather than added here, so the
    punctuation is translatable and the two languages can disagree about it.

    Shared with the help page's ``command -- description`` rows so the screens look like they
    come from the same plugin: aqua is always the thing you act on (a command, a button, a
    label), white is the content, gray is an aside.
    """
    return RTextList(RText(label, RColor.aqua), RText(value, value_colour))


def _approved_install_count(server: PluginServerInterface) -> int:
    """How many downloads an admin has authorised by hand, or ``0`` if that cannot be told.

    Read for the status page only. ``!!muc status`` reports the automatic setting, and after
    ``!!muc install`` that one line would otherwise still say "off" — technically true, and
    exactly the kind of half-answer that makes an admin go looking for a change that is already
    there.
    """
    try:
        ledger = DownloadLedger(
            Path(server.get_data_folder()) / DOWNLOAD_LEDGER_FILE_NAME, logger=None
        )
    except Exception:  # noqa: BLE001 - a status page is not worth failing over
        return 0
    return len(ledger.approved_keys())


def _manual_map_state(server: PluginServerInterface) -> str:
    """One line for the status page: is the mapping file loaded, and with how much in it?

    Worth a line because the file is silent by design — it changes no setting and prints
    nothing while it works. Without this, "I wrote that file and nothing happened" has no way
    to be answered other than by reading the config and guessing.
    """
    configured = str(_config.sources.manual_map or "").strip()
    if not configured:
        return tr("command.status.manual_map_off")

    try:
        base = Path(server.get_data_folder())
    except Exception as error:  # noqa: BLE001 - a status page is not worth failing over
        return tr(
            "command.status.manual_map_broken",
            detail="{}: {}".format(type(error).__name__, error),
        )

    path, _reason = resolve_map_file(base, configured)
    if path is None:
        return tr("command.status.manual_map_invalid", value=configured)

    mapping = ProjectMap(path)
    if mapping.error:
        return tr("command.status.manual_map_broken", detail=mapping.error)
    if not mapping.loaded:
        return tr("command.status.manual_map_empty", name=path.name)
    return tr(
        "command.status.manual_map",
        name=path.name,
        hashes=mapping.hashes,
        ids=mapping.mod_ids,
    )


def _backup_state(directory: Any) -> str:
    """The ``旧版备份`` line: how many, how big, how many are past the threshold.

    Counted from the directory rather than from the last report, so the line is true on a
    server that has not run a check since the last install — which is exactly the server whose
    backups the admin is asking about.
    """
    backups = list_backups(directory)
    if not backups:
        return tr("command.status.backup_none")
    days = max(0, int(_config.cleanup.max_age_days))
    expired_count = len(expired(backups, days))
    value = tr("command.status.backup", count=len(backups),
               size=_format_size(total_bytes(backups)))
    if expired_count:
        value += tr("command.status.backup_expired", count=expired_count, days=days)
        # Only when there is something to delete: on a server with nothing expired, "deletion is
        # off" is not a fact anyone needs, and printing it on every status screen would turn a
        # safety switch into wallpaper. When there is something, it is the answer to the
        # question the line just raised — why nothing has offered to clean these up.
        if not _deletion_allowed():
            value += tr("command.status.backup_locked")
    return value


def _show_status(source: CommandSource) -> None:
    """What the plugin currently thinks the server is, and what it is configured to do.

    One rich message rather than a line per ``reply`` call: the lines are a single screen, and
    building them as one ``RTextList`` is what lets colours and the title bar survive. (A
    ``server.logger`` call cannot do this — MCDR's log formatter stringifies its argument and
    ``RTextBase.__str__`` drops the colour, which is why the console paths log plain strings.)

    Scanned **without hashing**, deliberately. This page shows a jar count, a directory and the
    detected version; it has no use for a SHA-1, and computing one per jar would mean this
    command reads a whole modpack off the disk — on MCDR's command thread, so the server waits
    for it. A modpack of a few hundred megabytes measures at about a second, which is a second
    of stall for a screen that only needed the file names.
    """
    server = _server
    if server is None:
        return
    scan, context = _scan_current(server, _config, hashes=False)

    modrinth_state = (
        tr("command.status.enabled") if _config.sources.modrinth.enabled else tr("command.status.disabled")
    )
    automatic = _config.download.install_on_stop
    if automatic:
        install_state, install_colour = tr("command.status.install_on"), RColor.yellow
    else:
        approved = _approved_install_count(server)
        install_state = (
            tr("command.status.install_pending", count=approved) if approved
            else tr("command.status.install_off")
        )
        install_colour = RColor.yellow if approved else RColor.gray
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
        # The file the rest of this page was read from. Nothing else in the plugin ever shows
        # it, and "which file is this server actually using" is otherwise unanswerable from
        # the player's side of the server — the question that made a config file somebody had
        # edited and an install the plugin had done look like two different things.
        "\n",
        _field(
            tr("command.status.config_label"),
            tr("command.status.config", path=_config_path(server)),
            RColor.white,
        ),
        "\n",
        _field(tr("command.status.upstream_label"),
               tr("command.status.upstream", modrinth=modrinth_state),
               RColor.white),
        "\n",
        _field(
            tr("command.status.manual_map_label"),
            _manual_map_state(server),
            RColor.white,
        ),
        # The setting that changes files on this server belongs on the page that describes
        # what the plugin is doing, not only in the config file.
        "\n",
        _field(
            tr("command.status.install_label"),
            install_state,
            install_colour,
        ),
    )
    # 备份这一类东西以前完全没有出现在任何界面上：管理员装了半年插件，mods/ 里堆着多少
    # 个 .old 只有他自己 ls 才知道。这一行是它在状态屏上的位置，list 里则是完整的行。
    parts.append("\n")
    parts.append(
        _field(
            tr("command.status.backup_label"),
            _backup_state(scan.directory),
            RColor.white,
        )
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
    # 这一屏没有可点可悬的东西，底栏就是一条闭合线——它把这块输出和后面别的输出分开。
    parts.append("\n")
    parts.append(_rule_line())
    source.reply(parts)


def _reload_config(source: CommandSource) -> None:
    server = _server
    if server is None:
        return
    global _config
    _config = _load_config(server)
    # A plan is shown with the settings that were in force when it was staged — the size limit,
    # the retry count, the folder. Re-reading the config can change all three, so the plan is
    # dropped rather than carried out against numbers the admin never saw.
    _clear_pending()
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
    """``========  Mod Update Checker v1.0.0  ========`` — the name and version read from
    the plugin metadata, so the example above is just a shape, not a version to keep in step.

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


def _rule_line(caption: str = "") -> RTextList:
    """``========  caption  ========`` — the plain rule every screen now closes with.

    Sized to the **title bar's own total width** (computed from the same name and version, not
    from ``_TITLE_WIDTH`` — the two differ by a space when the bar's arithmetic leaves a
    remainder), so the top and bottom edges of a screen line up.

    With no caption it is a plain run of ``=``; with one it carries the reminder that belongs
    at the bottom of that screen — the listing says which of its parts can be hovered and which
    can be clicked. Callers pass a caption only where the reader has a mouse: on the console
    the same sentence would be describing things a terminal cannot do, so the rule stays plain.
    """
    name, version = _plugin_title(_server)
    core = "{} v{}".format(name, version) if version else name
    total = 2 * max(_TITLE_BAR_MIN, (_TITLE_WIDTH - len(core) - 4) // 2) + len(core) + 4
    if not caption:
        return RTextList(RText("=" * total, RColor.gold))
    room = total - display_width(caption) - 4
    left = max(_TITLE_BAR_MIN, room // 2)
    right = max(_TITLE_BAR_MIN, room - left)
    return RTextList(
        RText("=" * left, RColor.gold), "  ",
        RText(caption, RColor.gray), "  ",
        RText("=" * right, RColor.gold),
    )


def _rows_rule_hint() -> str:
    """The closing rule's caption for screens whose rows carry the two red/green handles.

    The labels are read from the same keys the rows themselves use, so the sentence cannot
    claim ``[版本]`` hoverable in a language where the label is spelled something else. The
    status tag is named too: it is the one column that says nothing about itself on the row
    (every row spells it the same), so the hint is where a reader finds out it can be hovered.
    """
    return tr("command.rule.hint_rows", version=tr("line.version_label"),
              status="[{}]".format(tr("line.status_word")),
              details=tr("command.list.detail_link"))


#: How wide each ASCII character is for the player's help page, in **quarters of a letter**.
#:
#: This used to be a table of its own, fitted by ``bench/help_width_model.py`` against the
#: shipped Minecraft font and the font in the reader's screenshot. It is now
#: :data:`report.GLYPH_QUARTERS` — the same table the ``!!muc list`` columns are padded with,
#: measured off that client glyph by glyph (``bench/read_band_runs.py`` prints the advance of
#: every run of pixels). Two tables would have drifted apart the first time either was tuned,
#: and they are answering the same question: how wide does the game draw this?
#:
#: No model can be exact everywhere — a font whose space is a whole letter can only be aligned
#: in whole-letter steps — so the aim is "as straight as the font allows", not for perfection.


def _help_line(description: str, command: str, action: Any, width: int,
               monospaced: bool, hover: str = "") -> RTextList:
    """One ``!!muc <subcommand>  -- description`` row, left column padded to ``width``.

    The separator carries its own leading space, so the column is exactly as wide as the longest
    command on the page, and every description starts at the same place. ``width`` is measured
    from the rows that are actually printed rather than kept as a constant — a hand-kept number
    is one more thing to remember when a command is added, and getting it wrong pushes the
    longest row out of alignment rather than failing visibly.

    ``monospaced`` picks the unit: the console's font is fixed-width, so character count *is*
    the width there and the padding is exact; the game's font is proportional, so the padding
    comes from ``text_quarters``' model — as close as a server can get without seeing the
    client's font.

    ``hover`` is the tooltip: the parts of a command's story that do not belong on a list line
    (which statuses ``list`` accepts, what ``install`` steps do). It is attached to every piece
    of the row rather than to the row as a whole: a list's own style lives on an empty header
    and whether the client passes it down is the client's business — per piece, hovering
    anywhere on the row works.

    The click is two-sided, and that is on purpose. The **command** keeps its own action (run
    for a command that works bare, fill for one that needs an argument); the **description**
    (and the padding between them) always *fills the command in* — the reader asked for that
    after clicking a description and getting nothing, and filling is the harmless half: worst
    case the command is sitting in the input box, ready, and nothing has run.
    """
    if monospaced:
        padding = " " * max(0, width - len(command))
    else:
        padding = " " * max(
            0, int((width - text_quarters(command)) / QUARTERS_PER_LETTER + 0.5)
        )
    click = command + (" " if action is RAction.suggest_command else "")
    fill = command + (" " if action is RAction.suggest_command else "")
    pieces = [
        RText(command, RColor.aqua).set_click_event(action, click),
        RText(padding + " -- ", RColor.gray).set_click_event(RAction.suggest_command, fill),
        RText(description, RColor.white).set_click_event(RAction.suggest_command, fill),
    ]
    if hover:
        for piece in pieces:
            piece.set_hover_text(hover)
    return RTextList(*pieces)


def _list_filter_hover() -> str:
    """``list`` 那一行的浮窗：能填在后面的状态，一行一个。

    从 ``ALL_STATUSES`` 现算而不是抄一份写死的清单：加一个状态时，这份提示跟着长——而它
    正是筛选参数唯一会被读到的地方（筛选只认这些名字）。**一行一个**，不是逗号连成一长串：
    客户端只在空格处断行，十个状态连排会溢出整个屏幕（用户截图里的第一件事）；排版交给
    ``command.help.hover_status_line``（一行格式：名字 + 键）。
    """
    options = "\n".join(
        tr("command.help.hover_status_line", name=tr("status." + name),
           status_key=name)
        for name in ALL_STATUSES
    )
    return tr("command.help.hover_list", options=options)


def _show_help(source: CommandSource, prefix: str = ROOT_LITERALS[0]) -> None:
    """The landing page: what this plugin's commands are, and what each one does.

    Also what the bare command shows — ``!!muc`` with nothing after it. That is the convention
    this project follows for every plugin: a bare invocation teaches the syntax instead of
    guessing what the reader wanted, because a reader who typed no arguments is far more likely
    to be looking for the command list than for a particular one of them.

    ``prefix`` is the spelling actually typed, so ``!!muc help`` lists ``!!muc ...`` and not
    the other alias. The other spelling is mentioned once in the usage line instead.

    Every row is clickable. ``list``, ``check``, ``confirm`` and friends run when clicked —
    they work bare, and running them is what the reader wants. The four whose useful form takes
    an argument (``info``, ``download``, ``install``) suggest instead, filling the input box
    ready for one rather than firing a command that can only answer with its own usage.
    """
    other = next((name for name in ROOT_LITERALS if name != prefix), prefix)
    # Built with explicit ``tr`` calls rather than by looping over a tuple of keys: the
    # catalogue invariant collects key literals from their call sites, so a key reached through
    # a variable would look unused and be reported as a stale entry.
    #
    # Each row is ``(description, command, action, hover)``: the description is the short line
    # the reader scans, and everything that would have been a parenthetical — what the argument
    # accepts, what the step does — moved into the hover. The page is a table of contents; the
    # footnotes belong on the entries.
    entries = (
        (tr("command.help.entry_check"), prefix + " check", RAction.run_command,
         tr("command.help.hover_check")),
        (tr("command.help.entry_list"), prefix + " list", RAction.run_command,
         _list_filter_hover()),
        (tr("command.help.entry_summary"), prefix + " summary", RAction.run_command,
         tr("command.help.hover_summary")),
        (tr("command.help.entry_info"), prefix + " info", RAction.suggest_command,
         tr("command.help.hover_info")),
        (tr("command.help.entry_download"), prefix + " download", RAction.suggest_command,
         tr("command.help.hover_download")),
        (tr("command.help.entry_install"), prefix + " install", RAction.suggest_command,
         tr("command.help.hover_install")),
        (tr("command.help.entry_delete"), prefix + " delete", RAction.suggest_command,
         tr("command.help.hover_delete")),
        (tr("command.help.entry_cleanup"), prefix + " cleanup", RAction.run_command,
         tr("command.help.hover_cleanup")),
        (tr("command.help.entry_confirm"), prefix + " confirm", RAction.run_command,
         tr("command.help.hover_confirm")),
        (tr("command.help.entry_status"), prefix + " status", RAction.run_command,
         tr("command.help.hover_status")),
        (tr("command.help.entry_reload"), prefix + " reload", RAction.run_command,
         tr("command.help.hover_reload")),
        (tr("command.help.entry_help"), prefix + " help", RAction.run_command,
         tr("command.help.hover_help")),
    )
    # The console is a fixed-width font, so character count is the width there; the game's
    # font is proportional, so the width comes from the model above. Same rows either way.
    is_player = getattr(source, "is_player", False)
    monospaced = not is_player
    if monospaced:
        width = max(len(command) for _description, command, _action, _hover in entries)
    else:
        width = max(text_quarters(command)
                    for _description, command, _action, _hover in entries)

    rows = RTextList()
    rows.append(_title_line(_server))
    rows.append("\n")
    rows.append(RText(tr("command.help.usage", command=prefix, alias=other), RColor.gray))
    rows.append("\n")
    rows.append(RText(tr("command.help.permission", level=_config.command_permission_level),
                      RColor.yellow))
    for description, command, action, hover in entries:
        rows.append("\n")
        rows.append(_help_line(description, command, action, width, monospaced, hover))
    rows.append("\n")
    rows.append(_rule_line(tr("command.rule.hint_help") if is_player else ""))
    source.reply(rows)


def _trigger_check(source: CommandSource) -> None:
    source.reply(tr("command.check_started"))
    threading.Thread(
        target=lambda: _run_check(_server, source=source),  # type: ignore[arg-type]
        name="mod_update_checker_manual",
        daemon=True,
    ).start()


def _suggest_handles(pick: Any, include_all: bool = False) -> Any:
    """A completion provider for a command that takes a mod handle.

    Used by the MCDR **console**, which is the one place a ``!!`` command can be tab-completed:
    in game these are chat messages, and vanilla completes only its own ``/`` commands — no
    MCDR plugin can put suggestions into the chat box. So this is where "type half and press
    Tab" actually works; in game the equivalent is typing half and pressing enter, which
    :meth:`Report.resolve_handle` supports by accepting a unique prefix.

    ``pick`` decides which entries are worth offering per command: a download can only fetch
    what is waiting to be fetched, so suggesting the whole report would offer names that can
    only come back with an error. Handles are offered by **display name**, the spelling the
    listing shows, because that is the one a reader would have typed.
    """

    def provider(*_args: Any) -> List[str]:
        report = _last_report
        if report is None:
            return []
        entries = pick(report)
        names = [entry.name for entry in entries if entry.name]
        if include_all:
            names.insert(0, ALL_TARGET)
        return names

    return provider


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
        # Bare command = the help page, by project convention: an invocation with no arguments
        # is a reader asking what the commands are. The summary moved to its own word when this
        # changed — it is still one keystroke away, and it is no longer what an explorer gets.
        .runs(lambda source: _show_help(source, prefix))
        .then(
            Literal("help").runs(
                lambda source: _show_help(source, prefix)
            )
        )
        .then(Literal("summary").runs(_show_summary))
        .then(Literal("check").runs(_trigger_check))
        .then(
            Literal("list")
            .runs(lambda source: _show_list(source, "", prefix))
            .then(
                GreedyText("status").suggests(
                    lambda *_args: list(ALL_STATUSES)
                ).runs(
                    lambda source, context: _show_list(source, context["status"], prefix)
                )
            )
        )
        .then(
            Literal("info")
            .runs(lambda source: source.reply(tr("command.info.usage")))
            .then(
                GreedyText("target").suggests(
                    _suggest_handles(lambda report: report.entries)
                ).runs(
                    lambda source, context: _show_info(source, context["target"], prefix)
                )
            )
        )
        .then(Literal("status").runs(_show_status))
        .then(Literal("reload").runs(_reload_config))
        .then(
            Literal("download")
            .runs(lambda source: source.reply(tr("command.download.usage", command=prefix)))
            .then(
                GreedyText("target").suggests(
                    _suggest_handles(
                        lambda report: _download_all_candidates(report)[0], include_all=True
                    )
                ).runs(
                    lambda source, context: _manual_download(source, context["target"], prefix)
                )
            )
        )
        .then(
            Literal("install")
            .runs(lambda source: source.reply(tr("command.install.usage", command=prefix)))
            .then(
                GreedyText("target").suggests(
                    _suggest_handles(
                        lambda report: list(report.awaiting_install), include_all=True
                    )
                ).runs(
                    lambda source, context: _manual_install(source, context["target"], prefix)
                )
            )
        )
        .then(
            Literal("delete")
            # 裸 delete 给用法，和 info / download / install 一致：只说 "delete" 没说删什么，
            # 唯一能确定的事就是他还需要知道语法。
            .runs(lambda source: source.reply(tr("command.delete.usage", command=prefix)))
            .then(
                GreedyText("target").suggests(
                    _suggest_handles(lambda report: report.backups, include_all=True)
                ).runs(
                    lambda source, context: _manual_delete(source, context["target"], prefix)
                )
            )
        )
        .then(Literal("cleanup").runs(lambda source: _manual_cleanup(source, prefix)))
        .then(Literal("confirm").runs(lambda source: _manual_confirm(source, prefix)))
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

    # Only when nothing was carried over from a reload. A reload keeps the live report, which
    # is by definition at least as fresh as the one on disk.
    if _last_report is None:
        _last_report = _load_previous_report(server, _config)

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
    # ``isinstance`` rather than a bare ``.get``: the file is written by this plugin, but it is
    # also a file in the plugin's folder that anything can edit, and a stray ``[]`` in it used
    # to raise ``AttributeError`` straight out of ``on_load`` — a cache that was hand-edited
    # into nonsense taking the whole plugin down at startup. A log line may never be able to do
    # that, so it checks instead.
    payload = read_json(path)
    records = payload.get("records") if isinstance(payload, dict) else None
    if isinstance(records, dict) and records:
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
    # nonsense; the event is also the natural point to drop stale player state, and a staged
    # ``!!muc confirm`` — whose whole point is that it is acted on now, not at some later stop.
    _online_players.clear()
    _clear_pending()

    if not _config.enabled:
        return
    try:
        _install_on_stop(server)
    except Exception as error:  # noqa: BLE001 - a failed install must not break the shutdown
        server.logger.warning(tr("install.crashed", error="{}: {}".format(
            type(error).__name__, error)))

