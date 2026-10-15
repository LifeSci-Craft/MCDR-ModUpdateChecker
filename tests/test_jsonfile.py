"""The plugin's JSON files: how they are written, and what a reader gets back.

Small module, two properties worth pinning separately. The first is *what the bytes look like*
— indented, with mod names and Chinese messages left readable — because these are files an
admin opens by hand when a server is behaving oddly. The second is that a write is **atomic**:
every one of these files is read back at some point (the cache and the ledger at startup, the
report when an admin joins), and a half-written file is worse than a missing one, since it
parses into nonsense rather than into "nothing yet".

The reader's tolerance is the third: ``None`` for anything unusable, because every caller
starts from "there may not be a file yet".
"""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from mod_update_checker.jsonfile import json_text, read_json, write_json


# -- what the files look like ------------------------------------------------------------


def test_the_text_is_readable_by_a_person():
    """缩进 + 不转义非 ASCII。这份文件的读者是人，不是解析器。"""
    text = json_text({"name": "钠", "version": "1.1.0"})

    assert "\n" in text, "一行 JSON 读起来还行，但 diff 起来不行"
    assert "钠" in text, "中文字符被转义成了 \\u 序列"
    assert "\\u" not in text


def test_sorting_is_off_unless_asked_for(tmp_path):
    """排序只给下载账本用：它是一份清单，键顺序每次都可能不同，不排就是每次全变。"""
    payload = {"b": 1, "a": 2}

    assert json_text(payload).index('"b"') < json_text(payload).index('"a"')
    assert json_text(payload, sort_keys=True).index('"a"') < json_text(payload, sort_keys=True).index('"b"')
    write_json(tmp_path / "sorted.json", payload, sort_keys=True)
    assert json.loads((tmp_path / "sorted.json").read_text(encoding="utf-8")) == payload


def test_writing_creates_the_folder_it_needs(tmp_path):
    """插件数据文件夹未必存在——全新安装的服务器上它不是。"""
    target = tmp_path / "config" / "mod_update_checker" / "last_report.json"

    write_json(target, {"ok": True})

    assert read_json(target) == {"ok": True}


# -- atomicity ---------------------------------------------------------------------------


def test_a_write_leaves_no_temporary_file_behind(tmp_path):
    """临时文件只是改名用的跳板，不该留在文件夹里。"""
    target = tmp_path / "resolve-cache.json"

    write_json(target, {"version": 1, "records": {}})

    assert [path.name for path in tmp_path.iterdir()] == ["resolve-cache.json"]


def test_a_failed_write_leaves_the_old_file_untouched(tmp_path, monkeypatch):
    """写坏了就是没写成：旧文件必须原封不动，而不是变成半份新内容。

    这是「先写临时文件再改名」存在的全部理由，所以它值得一条测试——直接构造一次写盘失败
    （磁盘满、权限、被杀掉），断言两种坏结果都没有发生：目标文件既没被改写，也没被截断。
    失败的那次还必须把自己的临时文件带走：失败路径留下垃圾，等于把「换个名字」的优点又还回去。
    """
    target = tmp_path / "download-manifest.json"
    write_json(target, {"version": 1, "mods": {"kept": {"file": "kept.jar"}}})
    before = target.read_text(encoding="utf-8")

    def explode(_source, _destination):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", explode)
    with pytest.raises(OSError):
        write_json(target, {"version": 1, "mods": {}})
    monkeypatch.undo()

    assert target.read_text(encoding="utf-8") == before
    assert read_json(target) == {"version": 1, "mods": {"kept": {"file": "kept.jar"}}}
    assert [path.name for path in tmp_path.iterdir()] == [target.name]


def test_two_threads_writing_one_file_do_not_share_a_temporary_name(tmp_path, monkeypatch):
    """两个写者共用一个 ``.tmp`` 会互相截断——账本正是被两个线程同时写的那个文件。

    这里不靠时序碰运气：``tempfile.mkstemp`` 就是这条性质的实现，所以直接观察**两个线程
    留下的临时名不同**。旧实现下两者都是 ``download-manifest.json.tmp``，断言立刻失败。
    """
    blocker = tmp_path / "download-manifest.json"
    write_json(blocker, {"version": 1, "mods": {}})

    seen = []
    lock = threading.Lock()
    ready = threading.Barrier(2)

    def recording(source, _destination):
        with lock:
            seen.append(Path(source).name)
        raise OSError("stop after the temporary file has been written")

    def writer():
        ready.wait()
        with pytest.raises(OSError):
            write_json(blocker, {"version": 1, "mods": {}})

    monkeypatch.setattr("os.replace", recording)
    threads = [threading.Thread(target=writer) for _index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    monkeypatch.undo()

    try:
        # 3 = how many times a failed rename is retried, so this is the same observation seen
        # through the retry loop: the names are recorded once per attempt.
        assert len(set(seen)) == 2, "two writers shared one temporary name: {}".format(seen)
        assert len(seen) % 3 == 0, seen
    finally:
        for leftover in tmp_path.iterdir():
            if leftover.name != blocker.name:
                leftover.unlink()


def test_eight_threads_writing_one_file_always_succeed(tmp_path):
    """同一目标的并发写必须全部成功，而且一个临时文件都不许剩下。

    这条钉住的是**改名冲突**，不是临时名冲突：8 个写者各自的临时名从来不同（上面那条测试
    已经量过），失败的是两个并发的 ``os.replace`` 指向同一个目标——Windows 会用
    ``WinError 5`` 拒绝第二个。实测 8 线程 × 200 次写同一个文件，只有 write 全部成功、
    652/1600 次 replace 失败。修法是按目标路径串行化写入，所以这里断言的是「零异常」与
    「无 ``.tmp`` 残留」。

    还有一条断言是**这个测试唯一能在锁被删掉时失败的**：``_replace_with_retry`` 会重试 3 次，
    所以「有没有锁」在结果上看不出来——实测把锁去掉、只留重试，这条测试照样一个异常都没有，
    而每次写入都要多等一次 50 ms 的退避。于是这里直接量那次重试**有没有发生**：串行化之后每次
    改名都应该一次成功，1600 次写入不该有任何一次走到重试分支。
    """
    import mod_update_checker.jsonfile as jsonfile_module

    target = tmp_path / "download-manifest.json"
    write_json(target, {"version": 1, "mods": {}})

    errors = []
    retries = []
    real_sleep = time.sleep
    threads = 8
    rounds = 200
    ready = threading.Barrier(threads)

    def watching_sleep(seconds):
        # ``_replace_with_retry`` is the only caller of ``time.sleep`` in this module.
        with lock:
            retries.append(seconds)
        return real_sleep(min(seconds, 0.001))

    def writer(index):
        ready.wait()
        for round_index in range(rounds):
            try:
                write_json(target, {"version": 1, "mods": {}, "writer": index,
                                    "round": round_index})
            except OSError as error:  # noqa: PERF203 - the failure is the observation
                with lock:
                    errors.append("{}: {}".format(type(error).__name__, error))
                return

    lock = threading.Lock()
    workers = [threading.Thread(target=writer, args=(index,)) for index in range(threads)]
    original_sleep = jsonfile_module.time.sleep
    jsonfile_module.time.sleep = watching_sleep
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
    finally:
        jsonfile_module.time.sleep = original_sleep

    assert errors == [], errors
    assert [path.name for path in tmp_path.iterdir()] == [target.name]
    assert isinstance(read_json(target), dict)
    assert retries == [], (
        "{} renames needed a retry, so writes to one file were racing rather than being "
        "serialised".format(len(retries))
    )


def test_writes_to_one_target_never_overlap(tmp_path, monkeypatch):
    """同一目标路径的写入必须真的串行——而且这一点要在任何操作系统上都测得出来。

    上面那条测试是**反推**的：靠「并发的改名会不会失败」与「有没有走重试」判断锁在不在。
    那是 Windows 的替换语义——Linux 上两个并发的 ``os.replace`` 指向同一目标并不会失败，
    于是把锁删掉以后那里既不会有异常、也不会有重试，变异检查会如实报「存活」（CI 就跑在
    Linux 上）。这条测试改成正着量：在写路径里套一层计数包装，只要锁不在，两个写者就会同时
    处在临界区内。
    """
    import mod_update_checker.jsonfile as jsonfile_module

    target = tmp_path / "download-manifest.json"
    write_json(target, {"version": 1, "mods": {}})

    real_replace = jsonfile_module._replace_with_retry
    counter_lock = threading.Lock()
    state = {"inside": 0, "peak": 0}

    def counting_replace(temporary, destination):
        with counter_lock:
            state["inside"] += 1
            state["peak"] = max(state["peak"], state["inside"])
        try:
            # 把重叠窗口放大到调度器一定会撞上的量级；串行化之后这段时间在锁内，
            # 所以整条测试的耗时仍然是可接受的（8 × 20 × 5 ms）。
            time.sleep(0.005)
            return real_replace(temporary, destination)
        finally:
            with counter_lock:
                state["inside"] -= 1

    monkeypatch.setattr(jsonfile_module, "_replace_with_retry", counting_replace)

    threads = 8
    rounds = 20
    ready = threading.Barrier(threads)
    errors = []

    def writer(index):
        ready.wait()
        for round_index in range(rounds):
            try:
                write_json(target, {"writer": index, "round": round_index})
            except OSError as error:  # noqa: PERF203 - the failure is the observation
                errors.append("{}: {}".format(type(error).__name__, error))
                return

    workers = [threading.Thread(target=writer, args=(index,)) for index in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert errors == [], errors
    assert state["peak"] == 1, (
        "{} writers were inside the write path of one target at the same time, so writes to "
        "one file were not serialised".format(state["peak"])
    )


def test_a_rename_that_fails_once_is_retried(tmp_path, monkeypatch):
    """被别的写者挡了一下的改名是可重试错误，重试一次就该过去。"""
    import mod_update_checker.jsonfile as jsonfile_module

    target = tmp_path / "download-manifest.json"
    write_json(target, {"version": 1, "mods": {}})

    attempts = []
    real_replace = os.replace

    def flaky(source, destination):
        attempts.append(Path(source).name)
        if len(attempts) == 1:
            raise OSError("WinError 5: access denied")
        return real_replace(source, destination)

    monkeypatch.setattr(jsonfile_module.os, "replace", flaky)
    write_json(target, {"version": 1, "mods": {"kept": {}}})
    monkeypatch.undo()

    assert len(attempts) == 2, "the rename was not retried"
    assert read_json(target) == {"version": 1, "mods": {"kept": {}}}
    assert [path.name for path in tmp_path.iterdir()] == [target.name]


# -- reading -----------------------------------------------------------------------------


def test_reading_says_none_for_everything_unusable(tmp_path):
    """缺失、读不了、解析不了、顶层形状不对——读的人只需要知道「这里没有能用的东西」。"""
    assert read_json(tmp_path / "absent.json") is None

    broken = tmp_path / "broken.json"
    broken.write_text('{"version": 1,', encoding="utf-8")
    assert read_json(broken) is None

    # 能解析但顶层不是对象：读的人自己用 isinstance 判断，这里不该替它决定。
    array = tmp_path / "array.json"
    array.write_text("[]", encoding="utf-8")
    assert read_json(array) == []

    folder = tmp_path / "folder.json"
    folder.mkdir()
    assert read_json(folder) is None


def test_the_round_trip_survives_what_the_plugin_stores(tmp_path):
    """真实内容过一遍：Mod 名、中文、空列表、布尔、嵌套。"""
    payload = {
        "version": 1,
        "entries": [
            {"name": "Sodium Extra", "notes": [["note.declared_mc", {"range": ">=26.1"}]],
             "size_bytes": 12345, "age_days": 0},
            {"name": "锂-性能优化", "notes": [], "approved": True},
        ],
    }
    target = tmp_path / "last_report.json"

    write_json(target, payload)

    assert read_json(target) == payload
