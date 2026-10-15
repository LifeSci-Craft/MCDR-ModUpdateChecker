"""v1.6.0 的负担画像：加载 / 扫描 / 一次完整检查 / 空闲 CPU。

报告在 ``bench/perf-v1.6.0.md``，这里的数字就是它的来源。在仓库目录下运行：

    PYTHONPATH=".testlibs;tests" python bench/perf_v1.6.0.py [--mods <你的 mods 目录>]

五件事各自独立测量，互不借用成见：

1. **导入**：新起进程 import 一次，取 5 次的中位数——这是每次启动都要付的钱；
2. **加载的可测部分**：命令树构建与语言装载（``on_load`` 里除日志与注册之外的主要工作）；
3. **扫描**：一个真实服务端的 ``mods/`` 与 100 个合成 jar 各扫一遍（顺序读 + 单遍 SHA-1，
   正是检查里最重的本地工作）。真实目录由 ``--mods`` 或 ``MUC_BENCH_MODS`` 给出——**不再写死
   作者本机路径**：那既把私人目录结构带进公开仓库，又让第 3 节在别人机器上只打印一句「跳过」
   然后以 0 退出，而文档把那一节的数字当来源引用；
4. **一次完整检查**：对本地假上游（真的 HTTP，环回）跑 100 个 Mod 的全流程，量墙钟、进程 CPU、
   请求数；
5. **空闲**：一个像调度线程那样反复 ``Event.wait`` 的线程，在 3 秒里消耗多少 CPU（应为零）。

渲染路径的数字由 ``bench/bench_render.py`` 单独产出；两者一起构成「它有多重」的全部答案。
"""

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / ".testlibs"))
sys.path.insert(0, str(REPO / "tests"))

#: 要扫描的真实 mods 目录，由调用者给出：``--mods <目录>`` 或 ``MUC_BENCH_MODS`` 环境变量。
#:
#: 这里原来写死的是作者本机路径（``C:/Users/.../Desktop/...``）。两个后果：公开仓库里带着一个
#: 私人目录结构，而别人跑同一个脚本时第 3 节只打印一句「跳过」然后以 0 退出——README-dev 却把
#: 那一节量出来的数字当作来源引用。所以它现在是显式输入：没给就跳过，并说清楚怎么给。
REAL_MODS = None

COUNT = 100
PAYLOAD_MB = 1


def parse_args(argv=None):
    """``--mods`` 是唯一参数：其余章节用合成 Mod，不需要任何输入。"""
    parser = argparse.ArgumentParser(
        prog="perf_v1.6.0.py",
        description="量这个插件的开销：导入、扫描、一次完整检查、空闲 CPU。",
        epilog=(
            "第 3 节扫描的是你自己的 mods 目录，必须显式给出，否则跳过。"
            " 例如：--mods /path/to/server/mods"
        ),
    )
    parser.add_argument(
        "--mods",
        metavar="DIR",
        default=os.environ.get("MUC_BENCH_MODS") or None,
        help="真实 mods 目录（只读）。默认取环境变量 MUC_BENCH_MODS；两者都没有就跳过第 3 节",
    )
    return parser.parse_args(argv)


def head(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def ms(seconds):
    if seconds < 1:
        return "{:.1f} ms".format(seconds * 1000)
    return "{:.2f} s".format(seconds)


def main(argv=None):
    """跑完全部五节。参数只有一个 ``--mods``，见 :func:`parse_args`。"""
    args = parse_args(argv)
    global REAL_MODS
    REAL_MODS = args.mods

    print("Python :", sys.version.split()[0], "|", sys.platform)
    print("仓库   :", REPO)

    # --------------------------------------------------------------------------------------
    # 1. 导入
    # --------------------------------------------------------------------------------------
    head("1. 导入（新进程 ×5，取中位数；同一件事三种口径）")
    env = dict(os.environ, PYTHONPATH=str(REPO / ".testlibs"))
    TIMED = ("import time; t=time.perf_counter(); import mod_update_checker; "
             "print(time.perf_counter()-t)")
    VARIANTS = (
        ("冷启动整体（连 mcdreforged 与依赖一起）", TIMED),
        ("mcdreforged 已加载", "import mcdreforged; " + TIMED),
        ("mcdreforged + requests 已加载（宿主常态）", "import mcdreforged, requests; " + TIMED),
    )
    for label, code in VARIANTS:
        times = []
        for _ in range(5):
            proc = subprocess.run(
                [sys.executable, "-c", code],
                cwd=str(REPO), env=env, capture_output=True, text=True,
            )
            times.append(float(proc.stdout.strip()))
        print("  {:<46} 中位 {}".format(label, ms(statistics.median(times))))
        print("      每次: " + ", ".join(ms(item) for item in times))

    # --------------------------------------------------------------------------------------
    # 2. 加载的可测部分
    # --------------------------------------------------------------------------------------
    head("2. 加载的可测部分：命令树构建 + 语言装载")
    import timeit  # noqa: E402

    import mod_update_checker as plugin  # noqa: E402


    class _Cfg:
        language = "zh_cn"


    tree_time = timeit.timeit(lambda: plugin._command_tree("!!muc"), number=50) / 50
    lang_time = timeit.timeit(lambda: plugin._apply_language(None, _Cfg()), number=50) / 50
    print("  命令树构建 :", ms(tree_time))
    print("  语言装载   :", ms(lang_time))

    # --------------------------------------------------------------------------------------
    # 3. 扫描真实文件夹
    # --------------------------------------------------------------------------------------
    head("3. 扫描真实服务端的 mods/（只读）")
    from mod_update_checker.scanner import scan_mods  # noqa: E402

    if REAL_MODS is None:
        print("  跳过：没有给出真实 mods 目录。")
        print("  用 --mods <目录> 或环境变量 MUC_BENCH_MODS=<目录> 指一个再跑一次；")
        print("  第 4 节用合成 Mod，不受影响。")
    else:
        REAL_MODS = Path(REAL_MODS)
        if not REAL_MODS.is_dir():
            print("  跳过：{} 不是一个目录".format(REAL_MODS))
            REAL_MODS = None

    if REAL_MODS is not None:
        jars = sorted(p for p in REAL_MODS.iterdir() if p.suffix == ".jar")
        total = sum(p.stat().st_size for p in jars)
        print("  目录: {} 个 jar，{:.0f} MiB".format(len(jars), total / 1048576))
        for round_no in (1, 2):
            started = time.perf_counter()
            scan_mods(REAL_MODS)
            print("  第 {} 遍墙钟: {}".format(round_no, ms(time.perf_counter() - started)))
        tracemalloc.start()
        scan_mods(REAL_MODS)
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print("  峰值分配（tracemalloc 口径）: {:.1f} MiB".format(peak / 1048576))

    # --------------------------------------------------------------------------------------
    # 4. 100 个合成 Mod：扫描 + 一次完整检查
    # --------------------------------------------------------------------------------------
    head("4. {} 个合成 Mod：扫描 + 一次完整检查（本地假上游，真 HTTP 环回）".format(COUNT))
    from fake_upstream import FakeFile, FakeProject, FakeUpstream, FakeVersion  # noqa: E402
    from support import fabric_metadata  # noqa: E402

    from mod_update_checker.checker import Checker, CheckOptions  # noqa: E402
    from mod_update_checker.serverinfo import ServerContext  # noqa: E402

    work = Path(tempfile.mkdtemp(prefix="muc_perf_"))
    upstream = FakeUpstream().start()
    try:
        mods = work / "mods"
        mods.mkdir()
        payload = os.urandom(PAYLOAD_MB * 1024 * 1024)
        for index in range(COUNT):
            mod_id = "perfmod{:03d}".format(index)
            path = mods / (mod_id + ".jar")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
                archive.writestr("fabric.mod.json", json.dumps(fabric_metadata(
                    id=mod_id, name="Perf Mod {:03d}".format(index), version="1.0.0")))
                archive.writestr("payload.bin", payload)
            sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
            upstream.add_project(FakeProject(
                id="proj-" + mod_id,
                slug=mod_id,
                title="Perf Mod {:03d}".format(index),
                versions=[
                    FakeVersion(id=mod_id + "-v1", project_id="proj-" + mod_id,
                                version_number="1.0.0",
                                files=[FakeFile(sha1=sha1, filename=mod_id + ".jar")]),
                    FakeVersion(id=mod_id + "-v2", project_id="proj-" + mod_id,
                                version_number="1.1.0",
                                date_published="2026-02-01T00:00:00Z",
                                files=[FakeFile(sha1="b" * 40,
                                                filename=mod_id + "-1.1.0.jar",
                                                url=upstream.file_url(mod_id + "-1.1.0.jar"))]),
                ],
            ))
        on_disk = sum(p.stat().st_size for p in mods.iterdir())
        print("  合成目录: {} 个 jar，{:.0f} MiB".format(COUNT, on_disk / 1048576))

        started = time.perf_counter()
        scan = scan_mods(mods)
        print("  扫描（顺序读 + 单遍 SHA-1）:", ms(time.perf_counter() - started))

        options = CheckOptions(
            loader="fabric", mc_version="26.3", modrinth_base=upstream.modrinth_base,
            workers=4, requests_per_minute=0, timeout=10, retries=1, use_cache=False,
        )
        context = ServerContext(mc_version="26.3", mc_version_source="server_info", loader="Fabric")

        checker = Checker(options)
        try:
            started = time.perf_counter()
            cpu0 = time.process_time()
            report = checker.run(scan, context, cache_path=None)
            wall = time.perf_counter() - started
            cpu = time.process_time() - cpu0
        finally:
            checker.close()
        print("  完整检查墙钟（含假上游的 HTTP 往返）:", ms(wall))
        print("  完整检查进程 CPU:", ms(cpu))
        print("  报告自记 duration_seconds: {:.2f} s".format(report.duration_seconds))
        updates = sum(1 for item in report.entries if item.status == "update_available")
        print("  结果: {} 个条目，{} 个判为有更新".format(len(report.entries), updates))
        print("  HTTP 请求数: {}".format(len(upstream.requests)))
        for method, path, _body in upstream.requests:
            print("    {} {}".format(method, path))

        tracemalloc.start()
        started = time.perf_counter()
        checker = Checker(options)
        try:
            checker.run(scan_mods(mods), context, cache_path=None)
        finally:
            checker.close()
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print("  完整检查峰值分配（tracemalloc 口径）: {:.1f} MiB".format(peak / 1048576))
    finally:
        upstream.stop()
        shutil.rmtree(work, ignore_errors=True)

    # --------------------------------------------------------------------------------------
    # 5. 空闲 CPU
    # --------------------------------------------------------------------------------------
    head("5. 空闲 CPU：像调度线程那样反复等待的线程")
    stop = threading.Event()


    def _waiter():
        while not stop.wait(0.2):
            pass


    worker = threading.Thread(target=_waiter, daemon=True)
    cpu0 = time.process_time()
    worker.start()
    time.sleep(3.0)
    stop.set()
    worker.join(timeout=5)
    print("  一个等待线程跑 3 秒的进程 CPU 增量:", ms(time.process_time() - cpu0))
    print("  （调度线程平时就活在这样的等待里；默认 check.interval_hours=0 时它不启动，")
    print("    开服后那次检查也只是 delay 线程 sleep 60 秒再干活一次。）")


if __name__ == "__main__":
    main()
