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
import threading
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

    # 目标文件一个字没变。留下的 ``.tmp`` 是这次失败的痕迹，不是会被当成结果读走的东西——
    # 这也正是「换个名字」和「就地改写」的区别：就地改写的那份文件现在是半份。
    #
    # 临时名的**前缀**是目标名，后面两个数字是 pid 与线程号（见
    # ``test_two_threads_writing_one_file_do_not_share_a_temporary_name``）：它们的存在让两个
    # 写者不可能用同一个临时名互相截断，而断言仍要求这份文件「看起来是这次写的」。
    assert target.read_text(encoding="utf-8") == before
    assert read_json(target) == {"version": 1, "mods": {"kept": {"file": "kept.jar"}}}
    leftovers = [path.name for path in tmp_path.iterdir() if path.name != target.name]
    assert len(leftovers) == 1, leftovers
    assert leftovers[0].startswith("download-manifest.json.")
    assert leftovers[0].endswith(".tmp")


def test_two_threads_writing_one_file_do_not_share_a_temporary_name(tmp_path, monkeypatch):
    """两个写者共用一个 ``.tmp`` 会互相截断——账本正是被两个线程同时写的那个文件。

    这里不靠时序碰运气：临时名里的 pid 与线程号就是这条性质的实现，所以直接观察**两个线程
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
        assert len(seen) == 2, seen
        assert seen[0] != seen[1], "two writers shared one temporary name: {}".format(seen)
    finally:
        for leftover in tmp_path.iterdir():
            if leftover.name != blocker.name:
                leftover.unlink()


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
