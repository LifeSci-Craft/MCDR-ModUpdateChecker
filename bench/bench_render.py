"""渲染路径有多贵：200 个 Mod 的列表 + 汇总 + 详情，用 cProfile 找热点。

问题是「每个行都会重算一遍那些本来只跟语言有关的宽度」这类重复——按猜测去改是不行的，
所以先把开销量出来。用法（仓库根目录）：

    python bench/bench_render.py
"""

import cProfile
import io
import pstats
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".testlibs"))

import mod_update_checker as plugin  # noqa: E402
from mod_update_checker.report import (  # noqa: E402
    Report,
    UpdateEntry,
    render_full,
    summarise,
)
from mod_update_checker.serverinfo import ServerContext  # noqa: E402


class Metadata:
    name = "Mod Update Checker"
    version = "1.6.0"


class FakeServer:
    def __init__(self, folder):
        self._folder = folder

    def get_data_folder(self):
        return self._folder

    def get_self_metadata(self):
        return Metadata()

    def get_mcdr_language(self):
        return "zh_cn"


folder = tempfile.mkdtemp(prefix="muc_bench_")
plugin._server = FakeServer(folder)
plugin._config = plugin.Config.get_default()
plugin._config.language = "zh_cn"
plugin._apply_language(plugin._server, plugin._config)


class Player:
    is_player = True
    player = "Admin"

    def __init__(self):
        self.replies = []

    def reply(self, text, **_kwargs):
        self.replies.append(text)


STATUSES = ["update_available", "awaiting_install", "up_to_date", "local_ahead",
            "no_compatible_build", "unresolved", "error", "not_a_mod", "ignored",
            "old_backup"]
NAMES = ["Sodium", "Lithium", "Iris", "Carpet TIS Addition", "Simple Voice Chat",
         "[合成配方查询] Just Enough Items", "A Very Long Mod Name Indeed",
         "Better Hanging Signs", "MCDRCommand", "AppleSkin"]


def build(count):
    entries = []
    for index in range(count):
        entries.append(UpdateEntry(
            mod_id="mod{:03d}".format(index),
            name="{} {}".format(NAMES[index % len(NAMES)], index),
            file_name="mod{:03d}.jar".format(index),
            local_version="1.0.0",
            latest_version="2.0.0",
            status=STATUSES[index % len(STATUSES)],
        ))
    return Report(
        generated_at="2026-10-10T00:00:00+08:00",
        server=ServerContext(mc_version="26.3", mc_version_source="server_info",
                             loader="Fabric"),
        mods_directory="server/mods",
        total_jars=count,
        entries=entries,
    )


def timed(label, call, repeats=20):
    call()  # warm
    started = time.perf_counter()
    for _ in range(repeats):
        call()
    elapsed = (time.perf_counter() - started) / repeats
    print("  {:<34} {:>8.3f} ms".format(label, elapsed * 1000))
    return elapsed


CASES = []
for size in (20, 200):
    report = build(size)
    source = Player()
    CASES.append(("列表 {:<3} 行".format(size),
                  lambda report=report: plugin._reply_index(Player(), report)))
    CASES.append(("汇总 {:<3} 个 Mod".format(size),
                  lambda report=report: summarise(report, plugin.tr)))
    CASES.append(("整份报告 {:<3} 行（日志形态）".format(size),
                  lambda report=report: render_full(report, plugin.tr)))
    CASES.append(("详情页（1 行）", lambda report=report:
                  plugin._reply_detail(Player(), report.entries[0])))

print("每项 20 次，取平均：")
for label, call in CASES:
    timed(label, call)


def profile_report():
    for _label, call in CASES:
        call()


print()
print("cProfile（全部用例跑一遍）:")
stream = io.StringIO()
profiler = cProfile.Profile()
profiler.enable()
profile_report()
profiler.disable()
stats = pstats.Stats(profiler, stream=stream).sort_stats("tottime")
stats.print_stats(14)
print(stream.getvalue())
