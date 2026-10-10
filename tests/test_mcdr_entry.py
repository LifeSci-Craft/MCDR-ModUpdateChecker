"""The MCDR entry module: how it hooks the lifecycle, and what it must not do twice.

Two of the defects this file guards against are invisible at runtime — no exception, no
warning, just wrong behaviour that only shows up as an extra request or a check that stops
firing — so they are pinned here rather than left to a review.
"""

import ast
import hashlib
import json
import sys
import time
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "mod_update_checker"
REPO = PACKAGE.parent
ENTRY = PACKAGE / "__init__.py"

#: MCDR discovers entry-module handlers by these names (see MCDRPluginEvents).
EVENT_HANDLERS = (
    "on_load",
    "on_unload",
    "on_server_startup",
    "on_server_stop",
    "on_player_joined",
    "on_player_left",
)


class _FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, message, *_args, **_kwargs):
        self.messages.append(str(message))

    def debug(self, *_args, **_kwargs):
        pass

    def warning(self, message, *_args, **_kwargs):
        self.messages.append("WARN " + str(message))

    def error(self, message, *_args, **_kwargs):
        self.messages.append("ERROR " + str(message))

    def exception(self, *_args, **_kwargs):
        pass


class _FakeServer:
    """Just enough of PluginServerInterface to run the entry module head-less.

    Covers ``on_load`` (data folder, MCDR config, language, registration) and the player
    notification paths (``is_server_running``, ``get_permission_level``, ``tell``), which is
    what the on-join feature needs in order to be exercised without booting MCDR.
    """

    def __init__(self, tmp_path, levels=None, running=True):
        self.logger = _FakeLogger()
        self._folder = Path(tmp_path) / "config" / "mod_update_checker"
        self._folder.mkdir(parents=True, exist_ok=True)
        self._mcdr_config = {"working_directory": str(tmp_path), "language": "zh_cn"}
        self.help_messages = []
        self.commands = []
        #: name -> MCDR permission level. An absent name raises, like an unknown player.
        self._levels = dict(levels or {})
        self._running = running
        #: (player, text) for every message this server delivered.
        self.delivered = []

    def get_data_folder(self):
        return str(self._folder)

    def load_config_simple(self, file_name=None, *, target_class=None, **_kwargs):
        """Behave like MCDR for a ``Serializable`` target — file and all.

        What the plugin says *about* its config file is a reaction to what MCDR does to the
        file on disk: it creates it when it is missing, and it fills in options the file does
        not have and writes the whole thing back. A stub that just returned
        ``target_class.get_default()`` would leave the file untouched, so every such message
        would be describing a state a real server cannot produce. This mirrors MCDR's
        ``load_config_simple``: load, deserialize with the missing-field callback, and save
        exactly when the file was unreadable or something was missing.
        """
        from mcdreforged.plugin.si._simple_config_handler import SimpleConfigHandler

        handler = SimpleConfigHandler(file_name, None, self.get_data_folder())
        incomplete = False

        def note_missing(*_args):
            nonlocal incomplete
            incomplete = True

        try:
            raw = handler.load(encoding="utf8")
        except OSError:
            config = target_class.get_default()
            handler.save(config.serialize(), encoding="utf8")
            return config

        config = target_class.deserialize(
            raw, missing_callback=note_missing, redundancy_callback=note_missing
        )
        if incomplete:
            handler.save(config.serialize(), encoding="utf8")
        return config

    def get_mcdr_config(self):
        return dict(self._mcdr_config)

    def get_mcdr_language(self):
        return self._mcdr_config["language"]

    def is_server_running(self):
        return self._running

    def register_help_message(self, prefix, message, permission=0):
        self.help_messages.append((prefix, message, permission))

    def register_command(self, node, **_kwargs):
        self.commands.append(node)

    def get_permission_level(self, name):
        if name not in self._levels:
            raise KeyError("no permission level for {!r}".format(name))
        return self._levels[name]

    def tell(self, player, text, **_kwargs):
        self.delivered.append((player, str(text)))

    def told(self, player=None):
        return [text for name, text in self.delivered if player is None or name == player]


def _entry_source() -> str:
    return ENTRY.read_text(encoding="utf-8")


def test_the_lifecycle_handlers_exist_with_mcdr_names():
    """MCDR finds these by name on the entry module. A rename silently unhooks the event."""
    import mod_update_checker as plugin

    for name in EVENT_HANDLERS:
        assert callable(getattr(plugin, name, None)), "{} is missing or not callable".format(name)


def test_event_handlers_are_not_registered_explicitly_as_well():
    """Registering an event handler explicitly *duplicates* it.

    MCDR's ``register_event_listener`` during plugin loading only *stages* the listener, and
    ``_register_default_listeners`` then registers the staged ones **and** every module-level
    function named after an event. So an explicit registration alongside a correctly named
    function installs the handler twice, and the event fires twice — a second post-startup
    check thread on every server start, with no error to show for it.

    Checked on the syntax tree rather than the raw text so that a mention in a comment or a
    docstring cannot trip it.
    """
    tree = ast.parse(_entry_source())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "register_event_listener":
            offenders.append(node.lineno)
    assert offenders == [], (
        "on_load must not call register_event_listener (lines {}); name the handler after "
        "the event instead and let MCDR register it".format(offenders)
    )


def test_load_stops_the_previous_modules_scheduler():
    """A reload hands the old module to ``on_load`` and never fires ``on_unload``.

    Verified in MCDR's own ``plugin_manager``: ``__reload_plugin`` calls ``plugin.reload()``
    directly, with no ``PLUGIN_UNLOADED`` dispatch and no ``__unload_plugin``. So the outgoing
    module's scheduler thread is still alive, and if ``on_load`` does not stop it, every
    reload leaves one more thread running checks against this server forever.
    """
    tree = ast.parse(_entry_source())
    loader = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "on_load"
    )
    body = ast.dump(loader)
    assert "_stop_scheduler" in body, "on_load does not call into the previous module"
    assert "prev_module" in body, "on_load ignores the previous module handed to it"


def test_the_stop_event_is_cleared_before_a_new_scheduler_starts(tmp_path, monkeypatch):
    """``!!modupdate reload`` stops the old thread and then starts a new one.

    If the stop event is not cleared in between, the new loop's very first ``wait`` returns
    immediately and the thread exits — silently disabling interval checking until the next
    MCDR restart. Reproduced here by setting the event, starting the scheduler, and checking
    that a live thread actually came up.
    """
    import mod_update_checker as plugin

    monkeypatch.setattr(plugin, "_config", _config_with({"check.interval_hours": 1}),
                        raising=False)
    plugin._stop_scheduler()            # leaves _stop_event set, exactly as a reload does
    assert plugin._stop_event.is_set()

    server = _FakeServer(tmp_path)
    try:
        plugin._start_interval_scheduler(server)
        assert not plugin._stop_event.is_set(), "a stale stop event survived"
        thread = plugin._scheduler_thread
        assert thread is not None and thread.is_alive()
        assert thread.daemon, "a scheduler thread must not keep the process alive"
    finally:
        plugin._stop_scheduler()

    assert not plugin._scheduler_thread


def test_no_scheduler_thread_when_the_interval_is_zero(tmp_path, monkeypatch):
    import mod_update_checker as plugin

    monkeypatch.setattr(plugin, "_config", _config_with(), raising=False)
    plugin._scheduler_thread = None
    plugin._start_interval_scheduler(_FakeServer(tmp_path))
    assert plugin._scheduler_thread is None


def test_stopping_twice_is_harmless(monkeypatch):
    """Both ``on_unload`` and the next ``on_load`` may ask for a stop; that must be safe."""
    import mod_update_checker as plugin

    plugin._stop_scheduler()
    plugin._stop_scheduler()
    assert plugin._scheduler_thread is None


def test_on_load_counts_jars_without_hashing_them(tmp_path, monkeypatch):
    """``on_load`` must count, not read.

    It runs on MCDR's plugin-loading thread, so walking and hashing every jar there would
    block MCDR's startup and every ``!!MCDR reload plugin`` for as long as it takes to read
    the whole ``mods/`` folder — seconds on a real modpack. A fixture of five tiny jars makes
    that invisible, which is exactly why it is pinned: the first version of this plugin did
    hash the whole folder during load.
    """
    import mod_update_checker as plugin

    from support import fabric_metadata, write_jar

    mods = Path(tmp_path) / "mods"
    mods.mkdir(parents=True)
    for index in range(3):
        write_jar(
            mods / "mod{}.jar".format(index),
            fabric=fabric_metadata(id="mod{}".format(index)),
        )
    (mods / "stale.jar.disabled").write_bytes(b"not a jar")

    def explode(*_args, **_kwargs):
        raise AssertionError("on_load hashed the mods folder; that belongs in the check")

    monkeypatch.setattr(plugin, "scan_mods", explode, raising=False)
    plugin._stop_event.clear()

    server = _FakeServer(tmp_path)
    plugin.on_load(server, None)

    joined = "\n".join(server.logger.messages)
    assert "3" in joined, joined                    # the three jars were counted
    assert "mod0.jar" not in joined                 # and not one of them was opened
    assert len(server.commands) == 2                # both root aliases registered
    assert {prefix for prefix, _m, _p in server.help_messages} == {"!!modupdate", "!!muc"}

    plugin._stop_scheduler()


def test_on_load_warns_when_the_mods_folder_is_missing(tmp_path):
    import mod_update_checker as plugin

    plugin._stop_event.clear()
    server = _FakeServer(tmp_path)
    plugin.on_load(server, None)

    joined = "\n".join(server.logger.messages)
    assert "WARN" in joined and "mods" in joined, joined

    plugin._stop_scheduler()


def test_config_defaults_are_the_documented_ones():
    """The README's "works out of the box" claim depends on these exact values."""
    import mod_update_checker as plugin

    config = plugin.Config.get_default()

    # The three at the root, deliberately not buried in a section.
    assert config.enabled is True
    assert config.language == "auto"
    assert config.command_permission_level == 3

    assert config.server.mods_directory == ""
    assert config.server.loader == "fabric"
    assert config.server.mc_version == "auto"

    assert config.check.on_server_start is True
    assert config.check.interval_hours == 0        # no surprise periodic load
    assert config.check.include_beta is False      # release builds only, by default
    assert config.check.include_alpha is False
    assert config.check.ignored_mods == []

    assert config.report.updates_only is True
    assert config.report.in_game is False          # never broadcast to players by default
    assert config.report.write_file is True

    assert config.sources.modrinth.enabled is True
    assert config.sources.modrinth.api_base == ""   # official endpoint

    # The auto-download feature writes files, so "off unless asked for" is part of the
    # contract rather than a preference.
    assert config.download.enabled is False
    assert config.download.folder_name == "downloads"
    assert config.download.max_size_mb == 128
    # Extra attempts after the first, matching the ``network.retries`` convention.
    assert config.download.retries == 3

    assert config.network.timeout_seconds == 20
    assert config.network.cache.enabled is True      # and therefore must work for a user
    assert config.network.requests_per_minute == 240  # under Modrinth's documented 300/min

    # A day, not half an hour: an admin logging in wants the answer, and the answer from
    # yesterday is still the answer unless something has been installed since.
    assert config.report.reuse_report_minutes == 1440

    # The two permission thresholds are deliberately separate settings rather than one shared
    # value: one decides who counts as an admin, the other who may receive a broadcast. A refactor
    # that merges them would still pass every other assertion here.
    assert config.report.admin_permission == 3
    assert config.report.in_game_permission == 3


def _write_config(server, payload):
    """Put ``payload`` where the plugin will look for its config file."""
    import mod_update_checker as plugin

    path = Path(server.get_data_folder()) / plugin.CONFIG_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_a_config_file_still_using_the_flat_option_names_is_reported(tmp_path):
    """MCDR drops keys it does not recognise without saying so, so this has to be said here.

    An admin who already has a config file keeps it; every option in it is then read as absent,
    defaults are substituted, and the file is rewritten in the new shape. Without this warning
    the only symptom is settings that appear to have been forgotten for no reason — which looks
    like a bug in the plugin rather than a renamed option.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    _write_config(server, {"ignored_mods": ["pinned"], "download_updates": True})

    config = plugin._load_config(server)

    joined = "\n".join(server.logger.messages)
    assert "WARN" in joined, joined
    # The message has to name the move, or the admin cannot act on it.
    assert "ignored_mods -> check.ignored_mods" in joined, joined
    assert "download_updates -> download.enabled" in joined, joined
    # A warning, not a failure: the plugin still comes up.
    assert isinstance(config, plugin.Config)
    # And nothing was archived as if the file had been corrupt.
    assert "config_invalid" not in joined and ".broken." not in joined, joined


def test_a_config_file_in_the_new_shape_never_gets_the_legacy_warning(tmp_path):
    """The flat-name warning must not fire on a file that is already grouped.

    An incomplete file in the new shape is a different matter — it is filled in and reported
    as such, which the tests below cover. What must not happen here is the *legacy* warning,
    which would send the admin looking for options that never moved.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    _write_config(server, {"check": {"ignored_mods": ["pinned"]}, "download": {"enabled": True}})

    plugin._load_config(server)

    joined = "\n".join(server.logger.messages)
    assert "WARN" not in joined, joined


def test_a_config_file_that_does_not_exist_yet_is_reported_where_it_was_created(tmp_path):
    """A first install — or one where the file was deleted — says where the file went.

    "Where is the config file" is otherwise a question about conventions: this plugin's, or
    MCDR's, or a wiki's. One line, once, answers it.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin._load_config(server)

    written = Path(server.get_data_folder()) / plugin.CONFIG_FILE_NAME
    assert written.is_file(), "loading must have created the file"
    assert isinstance(config, plugin.Config)

    joined = "\n".join(server.logger.messages)
    assert "已按默认值创建" in joined, joined
    assert str(written) in joined, joined


def test_an_incomplete_config_file_is_healed_and_the_added_options_are_named(tmp_path):
    """The report a user needed and did not get.

    The missing option was never the whole story: what could not be answered from the outside
    was whether the plugin had *noticed*, and whether the file had been brought up to date.
    Both halves are asserted — the file gains the options, and the console names them with the
    path — because either one alone can regress without the other.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    stale = plugin.Config.get_default().serialize()
    del stale["download"]["install_on_stop"]
    del stale["sources"]["manual_map"]
    path = _write_config(server, stale)

    plugin._load_config(server)

    rewritten = path.read_text(encoding="utf-8")
    assert "install_on_stop" in rewritten and "manual_map" in rewritten

    joined = "\n".join(server.logger.messages)
    assert "已按默认值补上" in joined, joined
    assert "download.install_on_stop" in joined, joined
    assert "sources.manual_map" in joined, joined
    assert str(path) in joined, joined


def test_a_config_file_that_was_not_updated_is_warned_about_with_its_path(tmp_path):
    """The message for the state nobody has reproduced yet: the file just will not change.

    It is reached when a load leaves the file missing options it should have gained — whatever
    the cause, from another program holding the file to the plugin that was updated not being
    the plugin that is running. When it appears, the path and the option names are the two
    facts needed to find out which.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    stale = plugin.Config.get_default().serialize()
    del stale["download"]["install_on_stop"]
    path = _write_config(server, stale)

    plugin._report_config_file_state(
        server,
        plugin.Config.get_default(),
        str(path),
        existed_before=True,
        leaves_before=plugin._leaf_paths(stale),
        legacy_rebuilt=False,
    )

    joined = "\n".join(server.logger.messages)
    assert "WARN" in joined, joined
    assert "download.install_on_stop" in joined, joined
    assert str(path) in joined, joined


def test_a_complete_config_file_is_not_talked_about(tmp_path):
    """The report is for the three states worth knowing about — not for every single load."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    _write_config(server, plugin.Config.get_default().serialize())

    plugin._load_config(server)

    assert server.logger.messages == [], server.logger.messages


def test_two_default_configs_do_not_share_their_nested_state():
    """Two ``Config`` objects must not share the contents of a section.

    MCDR builds a nested default with ``copy.copy``, which is shallow, so two instances would
    share the same list object — and appending to one would change the other, the class
    attribute, and every config built afterwards. The plugin rebuilds each section per
    instance to prevent that; this is the assertion that notices if a new section is added
    without going through the same path.
    """
    import mod_update_checker as plugin

    first = plugin.Config.get_default()
    second = plugin.Config.get_default()

    first.check.ignored_mods.append("sodium")
    assert second.check.ignored_mods == [], "the two configs share one ignored_mods list"
    assert plugin.CheckConfig.ignored_mods == [], "the class attribute itself was mutated"

    # Same question one level deeper, where the section-of-a-section lives.
    assert first.sources.modrinth is not second.sources.modrinth
    assert first.network.cache is not second.network.cache
    first.network.cache.ttl_hours = 1
    assert second.network.cache.ttl_hours == 24


def test_the_end_to_end_run_uses_the_shipped_defaults():
    """The real-MCDR run must execute the config a user actually gets.

    This test exists because of a specific escape. The matrix tool used to write a config with
    ``use_resolve_cache: False`` for convenience; that disabled the cache code path, so it was
    never executed against a real MCDR, and it turned out to crash on the *default* config. A
    green end-to-end run therefore proved nothing about the shipped behaviour.

    Every override now has to be declared in ``CONFIG_OVERRIDES`` with a reason, and this
    test fails if the dict drifts from that list — so switching a code path off cannot happen
    silently again.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "mcdr_matrix", REPO / "tools" / "mcdr_matrix.py"
    )
    assert spec is not None and spec.loader is not None
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    class _Upstream:
        modrinth_base = "http://127.0.0.1:1/v2"

    config = tool.plugin_config(_Upstream())

    # Every option the tool sets, as a dotted path — the config file groups its options into
    # sections, so comparing sets of paths is the only way to ask this question of both shapes.
    #
    # Both runs are asked. One option (``download.install_on_stop``) is set by ``--with-install``
    # alone, because installing moves the fetched files out of the folder the default run's
    # download assertions inspect; without asking about both, the declared list and the set it
    # describes would never agree.
    from support import flatten_options, option_paths

    with_install = tool.plugin_config(_Upstream(), install=True)
    overridden = flatten_options(config)

    assert set(tool.CONFIG_OVERRIDES) == set(overridden) | set(flatten_options(with_install)), (
        "the matrix config drifted from its declared overrides://n"
        "  extra: {}\n  missing: {}".format(
            sorted((set(overridden) | set(flatten_options(with_install)))
                   - set(tool.CONFIG_OVERRIDES)),
            sorted(set(tool.CONFIG_OVERRIDES)
                   - set(overridden) - set(flatten_options(with_install))),
        )
    )

    # And the install run differs by exactly that one option, so the second run stays a second
    # run of the same thing rather than a differently configured one.
    assert set(flatten_options(with_install)) - set(overridden) == {"download.install_on_stop"}

    # The options whose defaults must be in force, i.e. absent from the override dict. These
    # are the ones that decide whether a code path runs at all.
    for key in (
        "network.cache.enabled",   # the crash that hid here
        "enabled",
        "report.write_file",
        "report.updates_only",
        "server.loader",
        "language",
        "server.mc_version",
        "server.mods_directory",
        "check.on_server_start",
        # Where files are written must stay at the shipped value: pointing the run at some
        # other folder would leave the download assertions looking at an empty directory and
        # quietly passing.
        "download.folder_name",
        "download.max_size_mb",
        # And so must the retry budget, so the matrix keeps exercising the number a user gets.
        "download.retries",
        # 清理的阈值同理，而且这一次不只是「别关掉代码路径」：矩阵种下的两个备份正好跨在这条线
        # 两侧（400 天 / 3 天），``cleanup`` 只拿过期的那个、``delete all`` 两个都拿——两条命令
        # 的区别**只**在于这条线。把它改成 0，整个区别就消失了。
        "cleanup.max_age_days",
    ):
        assert key not in overridden, (
            "{} must stay at its shipped default in the end-to-end run, otherwise the "
            "behaviour a user gets is never exercised".format(key)
        )

    # ``report.in_game`` is the one exception, and a deliberate one: its default is off, and
    # off means the in-game notification path never executes. That path builds a message out of
    # mod names, so it is worth running. The override is declared above, and the run asserts
    # the payload is valid JSON addressed to the right player.
    assert overridden["report.in_game"] is True

    # ``download.enabled`` is off by default for the same reason, and switched on here because
    # it is the only feature that writes files — the last one to leave to unit tests alone.
    assert overridden["download.enabled"] is True

    # ``cleanup.allow_delete`` 则**故意**留在出厂值（关）：这一次运行的前半段就是「默认配置
    # 下什么都不许删」，后半段由场景把配置文件改开再 reload。把它写进上面那份配置，就等于
    # 永远走不到拒绝那条路。矩阵工具里有一段注释专门说明这件事。
    assert "cleanup.allow_delete" not in overridden
    assert "cleanup.allow_delete" not in flatten_options(with_install)

    # And every override has to name an option the plugin actually has, so a typo cannot
    # silently become a no-op. Walked off the class structure rather than a hand-kept list, so a
    # renamed section fails here instead of passing by accident.
    import mod_update_checker as plugin

    known = option_paths(plugin.Config)
    unknown = set(tool.CONFIG_OVERRIDES) - known
    assert not unknown, "overrides for options that do not exist: {}".format(sorted(unknown))


# --------------------------------------------------------------------------------------
# Admin-on-join checks and notification
# --------------------------------------------------------------------------------------


def _report_with(entries):
    """A report holding exactly ``entries``."""
    from mod_update_checker.report import Report
    from mod_update_checker.serverinfo import ServerContext

    return Report(
        generated_at="2026-01-01T00:00:00+00:00",
        server=ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        mods_directory="server/mods",
        entries=list(entries),
    )


def _report(updates=2, age_seconds=0.0, mods_directory=None):
    """A report with ``updates`` pending updates, produced ``age_seconds`` ago."""
    from datetime import datetime, timedelta, timezone

    from mod_update_checker.report import Report, UpdateEntry
    from mod_update_checker.serverinfo import ServerContext

    produced = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    report = Report(
        generated_at=produced.isoformat(timespec="seconds"),
        server=ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        mods_directory=_MODS_DIRECTORY if mods_directory is None else mods_directory,
    )
    for index in range(updates):
        report.entries.append(
            UpdateEntry(
                mod_id="mod{}".format(index),
                name="Mod {}".format(index),
                file_name="mod{}.jar".format(index),
                local_version="1.0.0",
                latest_version="1.1.0",
                status="update_available",
            )
        )
    return report


#: The mods directory the plugin resolves for the fake server. ``entry_env`` sets it to what
#: the fixture's server actually implies, because a stored report is only reused when it
#: describes the server in front of us — so a synthetic report naming some other directory is
#: correctly refused, and every test of the reuse window would quietly turn into a test of the
#: fresh-check path instead.
_MODS_DIRECTORY = "mods"


@pytest.fixture
def entry_env(tmp_path, monkeypatch):
    """The plugin module wired to a fake server, with a recording check function."""
    import mod_update_checker as plugin
    from mod_update_checker.scanner import resolve_mods_directory

    # Only two departures from the shipped defaults here; everything else this fixture used to
    # spell out was already the default, and saying so twice is how a stub drifts. The language
    # is pinned for the same reason ``_manual_env`` pins it: the translator is module state, so
    # a fixture that leaves it alone makes every assertion below depend on which test ran first
    # — a green suite one day and a red one the next, depending on the collection order.
    monkeypatch.setattr(
        plugin,
        "_config",
        _config_with({"report.in_game": True, "report.reuse_report_minutes": 30,
                      "language": "zh_cn"}),
        raising=False,
    )
    plugin._apply_language(None, plugin._config)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_MODS_DIRECTORY",
        str(resolve_mods_directory(str(tmp_path), "")),
        raising=False,
    )
    plugin._stop_event.clear()
    plugin._online_players.clear()
    monkeypatch.setattr(plugin, "_last_report", None, raising=False)

    server = _FakeServer(tmp_path, levels={"Admin": 4, "Helper": 2, "Guest": 0})
    calls = []

    def fake_run(server, source=None, announce_clean=True, broadcast=True):
        calls.append({"announce_clean": announce_clean, "broadcast": broadcast})
        return _report()

    monkeypatch.setattr(plugin, "_run_check", fake_run, raising=False)
    return plugin, server, calls


def test_an_admin_joining_with_no_report_runs_a_check_and_is_told(entry_env, tmp_path):
    plugin, server, calls = entry_env

    plugin._admin_join_worker(server, "Admin")

    assert len(calls) == 1, "a check should have run"
    # No separate broadcast: this admin is already being messaged with the same figures.
    assert calls[0]["broadcast"] is False
    told = server.told("Admin")
    assert len(told) == 1, told
    assert "服务端 26.3" in told[0]
    assert "Mod 0" in told[0] and "1.0.0 -> 1.1.0" in told[0]


def test_a_recent_report_is_reused_instead_of_rechecking(entry_env):
    """Newest first: an admin logging in should not wait for a scan, and should not cause
    one either when the answer is minutes old."""
    plugin, server, calls = entry_env
    plugin._last_report = _report(updates=1, age_seconds=120)

    plugin._admin_join_worker(server, "Admin")

    assert calls == [], "no check should have run for a 2-minute-old report"
    told = server.told("Admin")
    assert len(told) == 1
    assert "2 分钟前" in told[0], told[0]
    # 120 seconds is well inside the 24-hour default, so nothing was re-fetched.


def test_a_stale_report_triggers_a_fresh_check(entry_env):
    plugin, server, calls = entry_env
    plugin._last_report = _report(updates=1, age_seconds=25 * 3600)

    plugin._admin_join_worker(server, "Admin")

    assert len(calls) == 1, "a 25-hour-old report is past the 24-hour window"
    assert "分钟前" not in server.told("Admin")[0]


def test_the_window_can_be_switched_off(entry_env, monkeypatch):
    """``0`` means "always re-check", which is the literal reading of the feature."""
    plugin, server, calls = entry_env
    monkeypatch.setattr(plugin._config.report, "reuse_report_minutes", 0)
    plugin._last_report = _report(updates=1, age_seconds=1)

    plugin._admin_join_worker(server, "Admin")

    assert len(calls) == 1


def test_a_non_admin_gets_nothing(entry_env):
    plugin, server, calls = entry_env

    plugin.on_player_joined(server, "Helper", None)
    plugin.on_player_joined(server, "Guest", None)
    assert calls == []
    assert server.delivered == []
    # Both are still tracked as online, which the broadcast notification relies on.
    assert {"Helper", "Guest"} <= plugin._online_players


def test_an_admin_joining_is_checked_against_the_permission_level(entry_env):
    plugin, server, calls = entry_env

    plugin.on_player_joined(server, "Admin", None)

    # The worker runs on its own thread; wait briefly for it rather than assume.
    for _ in range(100):
        if server.told("Admin"):
            break
        time.sleep(0.02)
    assert server.told("Admin"), "the admin was never told"
    assert len(calls) == 1


def test_an_unknown_player_does_not_raise(entry_env):
    """A permission lookup can fail for a name MCDR does not know."""
    plugin, server, calls = entry_env

    plugin.on_player_joined(server, "SomeoneMCDRDoesNotKnow", None)

    assert calls == []
    assert "SomeoneMCDRDoesNotKnow" in plugin._online_players


def test_the_feature_can_be_switched_off(entry_env, monkeypatch):
    plugin, server, calls = entry_env
    monkeypatch.setattr(plugin._config.report, "on_admin_join", False)

    plugin.on_player_joined(server, "Admin", None)

    assert calls == []


def test_a_disabled_plugin_does_nothing_on_join(entry_env, monkeypatch):
    plugin, server, calls = entry_env
    monkeypatch.setattr(plugin._config, "enabled", False)

    plugin.on_player_joined(server, "Admin", None)

    assert calls == []


def test_a_check_that_cannot_start_still_answers(entry_env, monkeypatch):
    """If another check holds the lock, the admin gets the previous report, not silence."""
    plugin, server, calls = entry_env
    # Past the reuse window, so a check is genuinely attempted — and refused the lock.
    previous = _report(updates=1, age_seconds=25 * 3600)

    monkeypatch.setattr(plugin, "_run_check", lambda *a, **k: None, raising=False)
    plugin._last_report = previous

    plugin._admin_join_worker(server, "Admin")

    told = server.told("Admin")
    assert len(told) == 1, told
    assert "Mod 0" in told[0]


def test_no_report_at_all_is_stated_plainly(entry_env, monkeypatch):
    plugin, server, calls = entry_env
    monkeypatch.setattr(plugin, "_run_check", lambda *a, **k: None, raising=False)
    plugin._last_report = None

    plugin._admin_join_worker(server, "Admin")

    told = server.told("Admin")
    assert len(told) == 1
    assert "还没有任何检查结果" in told[0]


def test_nothing_is_sent_while_the_server_is_stopped(entry_env):
    """A message that cannot be delivered must not be attempted, nor crash the thread."""
    plugin, _, _ = entry_env
    stopped = _FakeServer(Path("."), levels={"Admin": 4}, running=False)
    plugin._last_report = _report(updates=1)

    plugin._admin_join_worker(stopped, "Admin")

    assert stopped.delivered == []


def test_an_unloaded_plugin_does_not_send(entry_env):
    plugin, server, calls = entry_env
    plugin._last_report = _report(updates=1)
    plugin._stop_event.set()
    try:
        plugin._admin_join_worker(server, "Admin")
    finally:
        plugin._stop_event.clear()

    assert server.delivered == []
    assert calls == []


def test_a_clean_report_says_so_rather_than_nothing(entry_env):
    plugin, server, _calls = entry_env
    plugin._last_report = _report(updates=0)

    plugin._admin_join_worker(server, "Admin")

    told = server.told("Admin")
    assert len(told) == 1
    assert "没有发现更新" in told[0]


def test_the_join_message_respects_the_update_cap(entry_env):
    """A server with 200 outdated mods must not put 200 lines in someone's chat box."""
    from mod_update_checker import NOTIFY_MAX_UPDATES

    plugin, server, _calls = entry_env
    plugin._last_report = _report(updates=NOTIFY_MAX_UPDATES + 5)

    plugin._admin_join_worker(server, "Admin")

    told = server.told("Admin")[0]
    listed = [line for line in told.splitlines() if "1.0.0 -> 1.1.0" in line]
    assert len(listed) == NOTIFY_MAX_UPDATES, len(listed)
    assert "5 个" in told, "the remainder should be summarised, not silently dropped"


def test_broadcast_and_join_use_their_own_permission_settings(entry_env, monkeypatch):
    """The two thresholds mean different things and must not be folded into one."""
    plugin, server, _calls = entry_env
    plugin._online_players.update({"Admin", "Helper"})
    monkeypatch.setattr(plugin._config.report, "in_game_permission", 4)

    plugin._notify_in_game(server, _report(updates=1))

    assert server.told("Admin"), "the level-4 admin should be told"
    assert not server.told("Helper"), "the level-2 helper should not be"


# --------------------------------------------------------------------------------------
# Auto-download wiring
# --------------------------------------------------------------------------------------


def test_downloads_land_in_a_folder_of_the_plugins_own(tmp_path):
    """Inside the plugin's data folder, and named exactly as configured."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    folder, reason = plugin.resolve_download_folder(server, config)

    assert reason == ""
    assert folder is not None
    assert folder == Path(server.get_data_folder()) / "downloads"


@pytest.mark.parametrize(
    "name",
    ["../server/mods", "../..", "server/mods", "/absolute", "C:\\Windows", "a/b", "..", ""],
)
def test_the_download_folder_cannot_be_aimed_outside_the_plugin(tmp_path, name):
    """The setting is a folder *name*, and that is what makes this safe.

    If it took a path, an admin could silently point it at the live server's ``mods``
    directory — which would load jars straight into a running server, the exact thing this
    plugin exists to avoid. Rejected with a reason, so the log explains itself.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    config.download.folder_name = name

    folder, reason = plugin.resolve_download_folder(server, config)

    assert folder is None
    assert reason, "a refusal has to say why"


def _downloaded_entry(tmp_path, folder_name="downloads", blob=b"PK\x03\x04payload"):
    """An entry with an update whose build is sitting in the download folder already."""
    from mod_update_checker.report import UpdateEntry

    folder = Path(tmp_path) / "config" / "mod_update_checker" / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    name = "mod0-1.1.0.jar"
    (folder / name).write_bytes(blob)
    return UpdateEntry(
        mod_id="mod0",
        name="Mod 0",
        file_name="mod0.jar",
        local_version="1.0.0",
        latest_version="1.1.0",
        status="update_available",
        platform="modrinth",
        download_url="https://cdn.example/" + name,
        download_filename=name,
        download_sha1=hashlib.sha1(blob).hexdigest(),
        download_size=len(blob),
    )


def test_a_downloaded_build_is_no_longer_announced_as_an_update(tmp_path):
    """The behaviour the whole ledger exists for.

    Once the newer build is on disk, announcing it as an update again on every start is noise:
    the admin already did that step. It becomes "downloaded, waiting to be installed" instead,
    which is a different statement and the one they still have to act on.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    entry = _downloaded_entry(tmp_path)

    plugin._sync_download_state(server, _report_with([entry]), config)

    assert entry.status == "awaiting_install"


def test_download_state_is_read_even_with_downloading_switched_off(tmp_path):
    """Off means "do not fetch", not "forget what was fetched".

    An admin who turns the option off after using it should still be told the file is waiting,
    rather than being told once more that an update exists.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    assert config.download.enabled is False
    entry = _downloaded_entry(tmp_path)

    plugin._sync_download_state(server, _report_with([entry]), config)

    assert entry.status == "awaiting_install"


def test_a_file_that_does_not_match_its_hash_stays_an_update(tmp_path):
    """Presence is not enough: a truncated or hand-replaced jar must not be called ready.

    Left as ``update_available`` so the download stage deals with it, which is the correct
    outcome — telling someone to install a file that is not what it claims to be is worse than
    telling them to fetch it again.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    entry = _downloaded_entry(tmp_path)
    folder = Path(server.get_data_folder()) / "downloads"
    # Present, right name, wrong bytes.
    (folder / entry.download_filename).write_bytes(b"not the jar you are looking for")

    plugin._sync_download_state(server, _report_with([entry]), config)

    assert entry.status == "update_available"


def test_a_record_for_a_file_that_is_gone_is_dropped(tmp_path):
    """The admin installed it (or deleted it); the bookkeeping should follow, not go stale."""
    import mod_update_checker as plugin
    from mod_update_checker.downloads import DownloadLedger

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    entry = _downloaded_entry(tmp_path)
    folder = Path(server.get_data_folder()) / "downloads"

    ledger = DownloadLedger(Path(server.get_data_folder()) / plugin.DOWNLOAD_LEDGER_FILE_NAME)
    ledger.record("mod0", entry.download_filename, entry.download_sha1, "1.1.0", "now")
    ledger.save()
    (folder / entry.download_filename).unlink()

    plugin._sync_download_state(server, _report_with([entry]), config)

    assert entry.status == "update_available"
    reloaded = DownloadLedger(Path(server.get_data_folder()) / plugin.DOWNLOAD_LEDGER_FILE_NAME)
    assert reloaded.get("mod0") is None, "a record outlived its file"


def test_nothing_is_fetched_when_the_feature_is_off(tmp_path, monkeypatch):
    """Off is the shipped default, so this is the path almost every server takes."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    assert config.download.enabled is False
    entry = _entry_with_update()

    def explode(*_args, **_kwargs):
        raise AssertionError("a download was attempted while the feature is off")

    monkeypatch.setattr(plugin, "Downloader", explode, raising=False)

    plugin._reconcile_downloads(server, _report_with([entry]), config)

    assert entry.status == "update_available"
    assert not (Path(server.get_data_folder()) / "downloads").exists()


def _entry_with_update():
    from mod_update_checker.report import UpdateEntry

    return UpdateEntry(
        mod_id="mod0", name="Mod 0", file_name="mod0.jar",
        local_version="1.0.0", latest_version="1.1.0",
        status="update_available", platform="modrinth",
        download_url="https://cdn.invalid/mod0-1.1.0.jar",
        download_filename="mod0-1.1.0.jar",
        download_sha1="a" * 40, download_size=100,
    )


def test_a_broken_download_stage_does_not_fail_the_check(tmp_path):
    """A successful check must not be reported as failed because a fetch went wrong.

    The report has already been written and announced at this point, so an exception escaping
    here would leave the admin with a "check failed" line for a check that actually worked.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    config = plugin.Config.get_default()
    config.download.enabled = True
    # No data folder available is the cheapest way to make the stage throw from the inside.
    server.get_data_folder = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

    plugin._reconcile_downloads(server, _report_with([_entry_with_update()]), config)

    joined = "\n".join(server.logger.messages)
    assert "WARN" in joined, joined
    assert "boom" in joined or "no-data-folder" in joined, joined


def test_the_report_records_what_was_downloaded(tmp_path):
    """The note is what makes the report file the place to look afterwards."""
    import mod_update_checker as plugin
    from mod_update_checker.downloads import DownloadOutcome

    server = _FakeServer(tmp_path)
    report = _report(updates=2)
    outcomes = [
        DownloadOutcome("mod0.jar", "mod0", "Mod 0", "downloaded",
                        path="/x/downloads/mod0-1.1.0.jar", bytes_written=2048),
        DownloadOutcome("mod1.jar", "mod1", "Mod 1", "already_present",
                        path="/x/downloads/mod1-1.1.0.jar"),
    ]

    plugin._log_download_outcomes(server, report, outcomes, Path("/x/downloads"))

    first = dict(report.entries[0].notes)
    assert "note.downloaded" in first
    assert first["note.downloaded"]["path"].endswith("mod0-1.1.0.jar")
    second = dict(report.entries[1].notes)
    assert "note.download_already_present" in second

    # And the summary line is logged, so the console shows the outcome without opening a file.
    assert any("下载结果" in message for message in server.logger.messages)


def test_config_round_trips_through_json():
    import mod_update_checker as plugin

    config = plugin.Config.get_default()
    payload = config.serialize()
    assert json.loads(json.dumps(payload)) == payload
    restored = plugin.Config.deserialize(payload)
    assert restored.command_permission_level == config.command_permission_level


def test_colour_survives_the_reply_path_and_only_there():
    """The colour paths differ, and mixing them up loses the colour silently.

    ``source.reply`` renders an RText (MCDR's ``StdoutReplier`` calls ``to_colored_text``, and
    a player receives a chat component), while ``server.logger.info`` runs the message through
    ``str()`` — and ``RTextBase.__str__`` returns plain text. So a screen is built from RText
    and the log is built from strings, and this is what keeps the two apart: the same screen
    text stringifies back to exactly itself, and is coloured only when it is rendered.

    Empirically checked against the installed MCDR: ``str(RText('hello', RColor.yellow))`` is
    ``'hello'`` with no ANSI escape in it.
    """
    import mod_update_checker as plugin
    from mod_update_checker.report import UpdateEntry

    title = plugin._title_line(None)
    assert "\x1b[" not in str(title)                 # nothing depends on ANSI surviving
    assert "\x1b[" in title.to_colored_text()        # but the reply path does colour it
    assert str(title).startswith("=") and "Mod Update Checker" in str(title)

    # 一行的每一块按**它是什么**上色，而颜色表是唯一的：号码永远黄、名称永远白、状态查状态表。
    # 这条替换掉了「整行一个颜色」的旧规则——它让**号码**的颜色随行而变（1 号黄、其余灰），
    # 读者就是这么报上来的。
    fresh = UpdateEntry(mod_id="a", name="Alpha", file_name="a.jar",
                        local_version="1.0.0", latest_version="1.1.0",
                        status=plugin.STATUS_UPDATE_AVAILABLE)
    current = UpdateEntry(mod_id="b", name="Beta", file_name="b.jar",
                          local_version="1.0.0", status=plugin.STATUS_UP_TO_DATE)
    older = UpdateEntry(mod_id="c", name="Gamma", file_name="c.jar",
                        local_version="1.0.0", latest_version="2.0.0",
                        status=plugin.STATUS_NO_COMPATIBLE_BUILD)

    assert plugin._row_field_colour(fresh, plugin.ROW_FIELD_NUMBER) == plugin.RColor.yellow
    assert plugin._row_field_colour(current, plugin.ROW_FIELD_NUMBER) == plugin.RColor.yellow
    assert plugin._row_field_colour(fresh, plugin.ROW_FIELD_NAME) == plugin.RColor.white
    assert plugin._row_field_colour(current, plugin.ROW_FIELD_NAME) == plugin.RColor.white
    # 状态那一格现在是**每一行都一样的** ``[状态: ✔]``（用户点名），所以它永远是黄的；
    # 状态本身由图标说、由浮窗的第一行说（下一条测试钉住那里才是状态色）。
    assert plugin._row_field_colour(fresh, plugin.ROW_FIELD_NOTE) == plugin.RColor.yellow
    assert plugin._row_field_colour(older, plugin.ROW_FIELD_NOTE) == plugin.RColor.yellow
    assert plugin._row_field_colour(current, plugin.ROW_FIELD_NOTE) == plugin.RColor.yellow

    # 一行拆出来的四段：号码、名称、版本/事实、状态——顺序与补白都钉住。
    # 语言自己钉住（``make_translator``），不依赖前面某个测试留下的语言——这个坑记在
    # tests/README 里，单个测试被挑出来跑时会踩到。
    from mod_update_checker.i18n import make_translator
    from mod_update_checker.report import (
        CHAT_PREFIX_QUARTERS,
        QUARTERS_PER_LETTER,
        chat_cell_widths,
        chat_row_fields,
        text_quarters,
    )

    chinese = make_translator("zh_cn")
    body_width, status_width = chat_cell_widths(chinese)

    def pieces_of(entry):
        # 聊天那一份：名称补成固定列，版本/事实收进 ``[版本]`` 之类的把手，状态是 ``[状态: ✔]``。
        # 控制台那一份（``index_row_fields``）把版本数字和整句状态印在行上，见 ``_entry_row``。
        fields = chat_row_fields(1, entry, chinese)
        return ([text for _role, text in fields],
                [plugin._row_field_colour(entry, role) for role, _text in fields])

    texts, colours = pieces_of(fresh)
    assert texts[0] == "[1] "
    # 号码 + 名称补到 ``CHAT_PREFIX_QUARTERS``：后面的列因此在所有行上对齐。补的是**整格空格**，
    # 而文字宽度不总是四分之一字母的整数倍，所以落点最多差半个空格——这就是这个字体的上限。
    assert abs(text_quarters(texts[0] + texts[1]) - CHAT_PREFIX_QUARTERS) \
        <= QUARTERS_PER_LETTER // 2, texts[1]
    assert texts[1].startswith("Alpha"), texts[1]
    assert texts[2] == "  [版本]"
    # 状态格是三块：文字、图标、收尾的括号 + 补白——图标单独一块，好让它带自己的颜色。
    assert texts[3] == "  [状态: ", texts[3]
    assert texts[4] == "↑", texts[4]
    assert (text_quarters("".join(texts[3:]))
            - 2 * QUARTERS_PER_LETTER - status_width) <= QUARTERS_PER_LETTER // 2, texts[3:]
    assert colours == [plugin.RColor.yellow, plugin.RColor.white, plugin.RColor.green,
                       plugin.RColor.yellow, plugin.RColor.blue, plugin.RColor.yellow], colours

    # 同一句话画在每一行上，文字永远黄、**图标跟状态走**（用户点名）。
    for entry, icon in ((current, "✔"), (older, "❌")):
        texts, colours = pieces_of(entry)
        assert texts[2] == "  [版本]"
        assert texts[3] == "  [状态: ", texts[3]
        assert texts[4] == icon, texts[4]
        assert colours[3] == plugin.RColor.yellow, colours
        assert colours[4] == plugin._status_colour(entry.status), colours
        assert colours[5] == plugin.RColor.yellow, colours


def test_the_console_path_logs_plain_strings():
    """Guards the asymmetry: passing RText to the logger is a silent no-op for colour.

    Also for the buttons: a click event logged to the console is a click event nobody can use,
    and the text it decorates arrives as its bare label.
    """
    import ast

    rtext_builders = {"_title_line", "_entry_row", "_field", "RText", "RTextList"}
    tree = ast.parse(_entry_source())
    notify = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_notify"
    )
    logger_calls = [
        node for node in ast.walk(notify)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "info"
    ]
    assert logger_calls, "_notify no longer logs anything"
    for call in logger_calls:
        assert call.args, "logger.info called with no message"
        argument = call.args[0]
        assert not isinstance(argument, ast.Call) or not (
            isinstance(argument.func, ast.Name) and argument.func.id in rtext_builders
        ), "an RText line is being logged, where the colour and the buttons would be dropped"


# --------------------------------------------------------------------------------------
# The help and status screens
# --------------------------------------------------------------------------------------


def _config_with(overrides=None):
    """The shipped config with specific options replaced. Keys are dotted paths.

    Built from the real ``Config`` rather than hand-written. A stub that mirrors four fields of
    the config has to be updated whenever the config changes, and the time it is not updated is
    the time the test quietly stops exercising the real object — which is the failure mode this
    whole suite exists to catch. ``check.interval_hours`` rather than ``check_interval_hours``,
    because the config file groups its options into sections and the tests should speak the
    same language as the file.
    """
    import mod_update_checker as plugin

    config = plugin.Config.get_default()
    for path, value in (overrides or {}).items():
        parts = path.split(".")
        target = config
        for name in parts[:-1]:
            target = getattr(target, name)
        setattr(target, parts[-1], value)
    return config


class _ReplyRecorder:
    """A command source that keeps whatever the command replied with."""

    def __init__(self):
        self.replies = []

    def reply(self, text, **_kwargs):
        self.replies.append(text)


def _render_help(prefix, language="zh_cn"):
    import mod_update_checker as plugin

    previous = plugin._config
    plugin._apply_language(None, _config_with({"language": language}))
    try:
        source = _ReplyRecorder()
        plugin._show_help(source, prefix)
        return source.replies
    finally:
        plugin._config = previous


def _render_help_for_player(prefix, language="zh_cn"):
    """The same screen as a player sees it — which is the version the padding model changes."""
    import mod_update_checker as plugin

    previous = plugin._config
    plugin._apply_language(None, _config_with({"language": language}))
    try:
        source = _PlayerSource("Admin")
        plugin._show_help(source, prefix)
        return source
    finally:
        plugin._config = previous


def test_help_is_one_rich_message_rather_than_a_line_per_reply():
    """One ``RTextList``, not a stack of separate replies.

    The screen is a single block: the title bar, the blank lines and the aligned columns only
    make sense if they are composed together. Replying line by line also loses the colours on
    any client that renders each reply separately.
    """
    from mcdreforged.api.rtext import RTextList

    replies = _render_help("!!muc")

    assert len(replies) == 1, replies
    assert isinstance(replies[0], RTextList)


def test_help_names_the_spelling_that_was_typed():
    """``!!muc help`` must not list ``!!modupdate ...``.

    Both aliases are registered and either may be the one an admin remembers; showing the
    other one is how someone concludes the command they typed does not exist.
    """
    for prefix, other in (("!!muc", "!!modupdate"), ("!!modupdate", "!!muc")):
        body = str(_render_help(prefix)[0])

        assert prefix + " <" in body or prefix + " check" in body
        assert "{} check".format(prefix) in body
        assert "{} check".format(other) not in body
        # The other spelling is still mentioned once, in the usage line.
        assert other in body


def test_help_lists_every_registered_subcommand():
    """A subcommand missing from the help is a subcommand nobody will use.

    Read off the command tree rather than a hand-kept list, so adding a subcommand without
    adding a help row fails here instead of shipping silently. Uses the public accessors
    (``literals`` / ``get_children``) so a reshuffle inside MCDR does not make this lie.
    """
    import mod_update_checker as plugin

    node = plugin._command_tree("!!muc")
    registered = set()
    for child in node.get_children():
        registered.update(child.literals)

    assert registered, "the command tree reported no subcommands"
    assert "help" in registered, "the help subcommand itself is missing"

    body = str(_render_help("!!muc")[0])
    for name in sorted(registered):
        assert "!!muc {}".format(name) in body, "{} is not in the help".format(name)


def test_help_columns_line_up_for_both_aliases():
    """The description column starts at the same place on every row — the console's form.

    The padding is computed from the longest command, so the row that *is* the longest is the
    one this fails on when the separator forgets its leading space. A terminal is a fixed-width
    font, so here character count is the width — the player's page is a different question and
    has its own test below.
    """
    for prefix in ("!!muc", "!!modupdate"):
        body = str(_render_help(prefix)[0])
        columns = [line.index("-- ") for line in body.splitlines() if "-- " in line]

        assert columns, body
        assert len(set(columns)) == 1, (prefix, columns)


def test_the_player_help_page_pads_by_the_width_model():
    """游戏里的帮助页按字宽模型补齐，不是按字符数——比例字体里后者是歪的。

    这是用户带着截图报上来的：``--`` 参差不齐，最宽差到一个字母。玩家的字体是比例字体，
    服务器看不见它，所以按一个模型补（``_HELP_GLYPH_QUARTERS``，在两种字体上拟合出来，
    见 ``bench/help_width_model.py``）。这里钉两件事：

    * 每一行「命令 + 补白」按模型量出来的宽度落在同一列上（误差不超过半个空格）；
    * 模型确实在用——``list`` 与 ``install`` 各比字符数补齐多一格（模型给这两行修正的地方）。
      退回成按字符数补齐时，这两条会立刻失败。
    """
    import mod_update_checker as plugin

    for prefix in ("!!muc", "!!modupdate"):
        source = _render_help_for_player(prefix)
        leaves = list(_segments(source.raw[-1]))

        pads = {}
        columns = []
        for index, item in enumerate(leaves):
            text = item.get("text", "")
            if item.get("color") != "aqua" or not text.startswith(prefix + " "):
                continue
            separator = leaves[index + 1].get("text", "")
            assert separator.endswith(" -- "), separator
            pad = len(separator) - len(" -- ")
            pads[text.split()[-1]] = pad
            columns.append(plugin.text_quarters(text) + 4 * pad)

        assert len(columns) >= 10, columns
        assert max(columns) - min(columns) <= 4, (prefix, columns)
        assert pads["list"] == 5, pads
        assert pads["install"] == 2, pads


def test_help_rows_are_short_and_the_details_live_on_hover():
    """帮助页：行上是短句，细节全在浮窗里——用户拿着截图点名的第一条。

    上一版的每行末尾都挂着一对长括号（``list`` 那行光括号就有半个屏宽），页面读起来像
    说明书。规则换成「行上说它做什么，浮窗里说怎么用」。两条断言：

    * 可见文案里没有括号——半角全角都不许（这一版刚把全角换成半角，行上索性一个不留）；
    * 每一行都挂着浮窗，``list`` 的浮窗**列出全部能筛的状态**。那个清单是从
      ``ALL_STATUSES`` 现算的：将来加了状态却忘了改文案，第一类断言看不见，这条看得见。
    """
    import json as _json

    from mod_update_checker.i18n import make_translator
    from mod_update_checker.report import ALL_STATUSES

    for language in ("zh_cn", "en_us"):
        source = _render_help_for_player("!!muc", language=language)
        leaves = list(_segments(source.raw[-1]))
        translator = make_translator(language)

        descriptions = []
        hovers = {}
        for index, item in enumerate(leaves):
            text = item.get("text", "")
            if item.get("color") != "aqua" or not text.startswith("!!muc "):
                continue
            separator = leaves[index + 1]
            description = leaves[index + 2]
            assert description.get("color") == "white", description
            descriptions.append(description["text"])
            # 三块都挂了浮窗：悬停在行的哪儿都出得来（列表自身的样式只落在空 header 上，
            # 会不会下发到子节点是客户端的事——按块挂就不赌这件事）。
            for piece in (item, separator, description):
                assert "hoverEvent" in piece, (language, text, piece)
            hovers[text] = _json.dumps(description["hoverEvent"], ensure_ascii=False)

        assert len(descriptions) == 12, descriptions
        for text in descriptions:
            assert "(" not in text and "（" not in text, text

        listing = hovers["!!muc list"]
        for name in ALL_STATUSES:
            assert name in listing, (language, name)
            assert translator("status." + name) in listing, (language, name)


#: One line of a tooltip may not exceed this many display columns (CJK counts as two). The
#: client only breaks lines **at spaces**, so a Chinese sentence with no spaces in it is drawn
#: as one overflowing line — which is exactly what the user's screenshot showed, the second
#: line running off the screen edge. Fixing it means breaking every tooltip by hand, and this
#: is the number that keeps those hand-made lines safe: vanilla fits 20 CJK glyphs (200px) or
#: about 33 ASCII glyphs per line, and the reader's client — CJK ≈ 2.25 letters, space ≈ one
#: letter — lands in the same range. 32 columns sits under both.
TOOLTIP_LINE_LIMIT = 32


def test_every_tooltip_line_fits_within_a_tooltip():
    """浮窗的每一行都短到放得下——**手工断行**是这条规则的一半，另一半是这个上限。

    客户端只在空格处断行：中文句子没有空格，写成一长条就会被画成一行、直接跑出屏幕（用户
    截图里 ``check`` 的浮窗第二行就是这样）。所以浮窗文案里每一行都是我们自己断的，而这条
    测试保证断得够短——两种语言的每一条都查，加长任何一条都会在这里失败。
    """
    import json as _json
    import pathlib

    import mod_update_checker as plugin
    from mod_update_checker.report import display_width

    previous = plugin._config
    try:
        for language in ("zh_cn", "en_us"):
            plugin._apply_language(None, _config_with({"language": language}))
            lang_file = (pathlib.Path(plugin.__file__).resolve().parent
                         / "lang" / (language + ".json"))
            catalogue = _json.loads(lang_file.read_text(encoding="utf-8"))
            values = [(key, text) for key, text in catalogue.items()
                      if "hover" in key or key.startswith("explain.")]
            # ``list`` 的浮窗是现算的（一行一个状态），要按生成结果查，不能只看模板。
            values.append(("command.help.hover_list (generated)", plugin._list_filter_hover()))
            assert any(key == "command.help.hover_list" for key, _ in values)

            for key, template in values:
                for line in template.replace("{command}", "!!muc info Example").split("\n"):
                    width = display_width(line)
                    assert width <= TOOLTIP_LINE_LIMIT, (
                        "{} [{}]: {:d} columns: {!r}".format(key, language, width, line))
    finally:
        plugin._config = previous


def test_clicking_a_help_description_fills_the_command_in():
    """帮助页的行上，**说明文字**点一下也能把命令填进输入框（用户点名）。

    「也能」正是关键：命令那一段保持它自己的动作（能裸跑的跑、要参数的填），说明与中间那段
    空白则一律 ``suggest_command``——读者点描述时想要的是「这条命令长什么样」，最坏的结果也
    只是输入框里躺着一行还没按回车的命令。
    """
    source = _render_help_for_player("!!muc")
    leaves = list(_segments(source.raw[-1]))
    rows = {}
    for index, item in enumerate(leaves):
        text = item.get("text", "")
        if item.get("color") == "aqua" and text.startswith("!!muc "):
            rows[text] = (item, leaves[index + 1], leaves[index + 2])

    # ``list``：命令是 run_command，说明段是「填进输入框」，不带尾随空格（它不需要参数）。
    command, separator, description = rows["!!muc list"]
    assert command["clickEvent"]["action"] == "run_command"
    for piece in (separator, description):
        assert piece["clickEvent"]["action"] == "suggest_command", piece
        assert piece["clickEvent"]["value"] == "!!muc list", piece

    # ``info``：命令本身就要参数（suggest + 尾随空格），说明段给的是同一份拼写。
    command, separator, description = rows["!!muc info"]
    assert command["clickEvent"]["action"] == "suggest_command"
    for piece in (separator, description):
        assert piece["clickEvent"]["action"] == "suggest_command", piece
        assert piece["clickEvent"]["value"] == "!!muc info ", piece


def test_every_help_row_is_clickable_and_describes_its_command():
    """Colour and click targets are the whole point of the rich help page.

    Asserted on ``to_json_object()`` rather than on private attributes: that is the form the
    client actually receives, so it proves the colour and the click survive the trip, and it
    is a documented interface instead of an internal one.
    """
    segments = list(_segments(_render_help("!!muc")[0]))

    commands = [
        item for item in segments
        if item.get("text", "").startswith("!!muc") and item.get("color") == "aqua"
    ]
    assert commands, "no clickable command segments found"

    for item in commands:
        click = item.get("clickEvent")
        assert click is not None, item
        # Clicking runs or types the command itself, never its description.
        assert click.get("value", "").startswith("!!muc"), item
        assert click.get("action") in ("run_command", "suggest_command"), item

    # The descriptions are present and set apart, so the screen reads as two columns.
    descriptions = [item for item in segments if item.get("color") == "white"]
    assert descriptions, "the help rows have no descriptions"


def test_only_the_row_that_needs_an_argument_suggests_instead_of_running():
    """Help rows run, except where the command is useless without an argument.

    ``list`` runs: it is a read-only listing and the useful thing to see. ``info`` does not —
    it needs a mod, so clicking it fills the input box rather than firing an error, which is
    what makes the number in the listing worth copying. ``download``, ``install`` and ``delete``
    follow the same rule for the same reason; ``confirm`` and ``cleanup`` take nothing, so they
    run.
    """
    segments = list(_segments(_render_help("!!muc")[0]))
    rows = {
        item["text"]: item
        for item in segments
        if item.get("text", "").startswith("!!muc ") and "clickEvent" in item
    }

    for command in ("!!muc list", "!!muc check", "!!muc status", "!!muc reload",
                    "!!muc confirm", "!!muc cleanup"):
        assert rows[command]["clickEvent"]["action"] == "run_command", command

    for command in ("!!muc info", "!!muc download", "!!muc install", "!!muc delete"):
        assert rows[command]["clickEvent"]["action"] == "suggest_command", command
        # A trailing space, so the number is typed straight after the command.
        assert rows[command]["clickEvent"]["value"].endswith(" "), command

    # Every command the tree registers is on the page, so a new one cannot be added and left
    # undiscoverable — the failure this list would otherwise not notice at all. The bare
    # ``!!muc`` row is excluded by the ``"!!muc "`` filter above, which is also what keeps this
    # list about *subcommands*: the bare form is this very page, so a row for it would be a row
    # for the screen the reader is already looking at.
    assert set(rows) == {
        "!!muc check", "!!muc list", "!!muc summary", "!!muc info", "!!muc download",
        "!!muc install", "!!muc delete", "!!muc cleanup", "!!muc confirm", "!!muc status",
        "!!muc reload", "!!muc help",
    }


def _segments(node):
    """Every leaf of an ``RTextList``, as the JSON objects the client is sent."""
    children = getattr(node, "children", None)
    if children is None:
        yield node.to_json_object()
        return
    for child in children:
        yield from _segments(child)


def test_the_status_screen_uses_the_same_title_bar_as_the_help(tmp_path):
    """The two screens are meant to look like one plugin's.

    Checked by shape rather than by exact text: the title bar is gold ``=`` bars around the
    plugin name, and it opens both screens.
    """
    import mod_update_checker as plugin

    class _Scan:
        directory = "server/mods"
        mods = []
        disabled = []

    config = _config_with({"language": "zh_cn"})
    previous_config, previous_scan, previous_server = plugin._config, plugin._scan_current, plugin._server
    try:
        plugin._config = config
        plugin._apply_language(None, config)
        plugin._scan_current = lambda *_a, **_k: (
            _Scan(),
            __import__(
                "mod_update_checker.serverinfo", fromlist=["ServerContext"]
            ).ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        )
        plugin._server = _FakeServer(tmp_path)

        source = _ReplyRecorder()
        plugin._show_status(source)
    finally:
        plugin._config, plugin._scan_current, plugin._server = (
            previous_config, previous_scan, previous_server,
        )

    assert len(source.replies) == 1
    bars = [
        item for item in _segments(source.replies[0])
        if item.get("text", "").startswith("=") and item.get("color") == "gold"
    ]
    assert bars, "the status screen has no title bar"
    assert bars[0]["text"].strip("=") == "", bars[0]


def _one_screen_setup(tmp_path, monkeypatch, entries):
    """The plugin wired to a fake server with a report, for driving one screen at a time."""
    import mod_update_checker as plugin
    from mod_update_checker.serverinfo import ServerContext

    class _Scan:
        directory = "server/mods"
        mods = []
        disabled = []

    server = _FakeServer(tmp_path)
    monkeypatch.setattr(plugin, "_server", server, raising=False)
    monkeypatch.setattr(plugin, "_last_report", _report_with(entries), raising=False)
    monkeypatch.setattr(plugin, "_config", _config_with({"language": "zh_cn"}), raising=False)
    monkeypatch.setattr(
        plugin, "_scan_current",
        lambda *_a, **_k: (
            _Scan(),
            ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        ),
        raising=False,
    )
    plugin._apply_language(server, plugin._config)
    return plugin, server


def _entry_for_screens():
    """One mod with everything a screen can show: a version change, a page and an action."""
    from mod_update_checker.report import UpdateEntry

    entry = UpdateEntry(
        mod_id="alpha", name="Alpha", file_name="alpha.jar",
        local_version="1.0.0", latest_version="1.1.0", status="update_available",
    )
    entry.project_url = "https://modrinth.com/mod/alpha"
    entry.download_url = "https://cdn.example/alpha-1.1.0.jar"
    entry.download_sha1 = "a" * 40
    return entry


def test_every_screen_opens_with_the_same_title_bar(tmp_path, monkeypatch):
    """One bar, five screens.

    They used to disagree: ``help`` and ``status`` drew a gold bar, the listing and the summary
    opened with the plugin's badge line, and a mod's detail had no header at all. A reader who
    has to recognise the layout afresh on every command is reading the layout instead of the
    answer — and this is asserted on the segments that actually go to the client, so a screen
    that quietly drops the bar fails rather than looking merely inconsistent.
    """
    entry = _entry_for_screens()
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, [entry])
    report = plugin._last_report

    screens = {
        "summary": lambda source: plugin._reply_summary(source, report),
        "list": lambda source: plugin._reply_index(source, report),
        "info": lambda source: plugin._reply_detail(source, entry),
        "status": plugin._show_status,
        "help": plugin._show_help,
    }

    bars = {}
    for name, call in screens.items():
        source = _ReplyRecorder()
        call(source)
        assert source.replies, "{} replied with nothing".format(name)
        # 只取第一条回复里**第一个换行之前**的金色段：整块屏幕作为一条消息的屏（help、
        # status）末尾还有一条金色的闭合线，它不是标题栏。
        gold = []
        for item in _segments(source.replies[0]):
            if item.get("text") == "\n":
                break
            if item.get("color") == "gold":
                gold.append(item["text"])
        assert gold, "{} does not open with the title bar".format(name)
        bars[name] = gold

    assert len({tuple(value) for value in bars.values()}) == 1, bars


def test_every_screen_closes_with_a_rule_as_wide_as_its_title(tmp_path, monkeypatch):
    """每屏底部一条 ``====`` 分割线，宽度与顶部标题栏一致——用户点名的第二条。

    对齐查的是**显示宽度**而不是字符数（标题栏本身也是量出来的宽度），而且比对的是插件
    真画出来的那两条，不是写死的 53——那是插件恰好叫这个名字、装这个版本时才成立的数字。

    玩家版的 ``help`` / ``list`` / ``summary`` 顺带用它捎一句提醒（哪块能悬停、哪块能点）；
    控制台没有鼠标，同一屏给的是一条素线；``info`` / ``status`` 没有可点可悬的东西，也是素线。
    最后一条断言在钉「提示说的是行里那两个标签」：文案里的 ``{version}`` / ``{details}``
    就是从行自己那两个键里取的。
    """
    from mod_update_checker.report import display_width

    entry = _entry_for_screens()
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, [entry])
    report = plugin._last_report

    screens = {
        "summary": lambda source: plugin._reply_summary(source, report),
        "list": lambda source: plugin._reply_index(source, report),
        "info": lambda source: plugin._reply_detail(source, entry),
        "status": plugin._show_status,
        "help": plugin._show_help,
    }

    hint_rows = plugin.tr(
        "command.rule.hint_rows",
        version=plugin.tr("line.version_label"),
        status="[{}]".format(plugin.tr("line.status_word")),
        details=plugin.tr("command.list.detail_link"),
    )
    hint_help = plugin.tr("command.rule.hint_help")

    for name, call in screens.items():
        source = _PlayerSource("Admin")
        call(source)
        title = str(source.raw[0]).split("\n")[0]
        closing = str(source.raw[-1]).split("\n")[-1]

        assert closing.startswith("=") and closing.endswith("="), (name, closing)
        assert display_width(closing) == display_width(title), (name, closing, title)

        if name in ("list", "summary"):
            assert hint_rows in closing, (name, closing)
        elif name == "help":
            assert hint_help in closing, (name, closing)
        else:
            assert closing == "=" * display_width(closing), (name, closing)

    # 控制台：同一条闭合线，但没有那句描述悬停/点击的话——终端两样都做不到。
    for name in ("list", "summary", "help"):
        console = _ReplyRecorder()
        screens[name](console)
        closing = str(console.replies[-1]).split("\n")[-1]
        assert closing == "=" * len(closing), (name, closing)


def test_the_chat_summary_offers_a_button_where_the_log_offers_a_url(tmp_path, monkeypatch):
    """The url belongs in the log, the button in the chat — and both read the same sections.

    A raw url in a chat line wraps onto a second row on any real mod, and cannot be clicked
    from inside the game at all. The log keeps it: a log line cannot be clicked either, so
    there the url is the only way through, and it can at least be copied.
    """
    entry = _entry_for_screens()
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, [entry])
    report = plugin._last_report

    source = _ReplyRecorder()
    plugin._reply_summary(source, report)
    segments = [item for reply in source.replies for item in _segments(reply)]

    body = "\n".join(str(reply) for reply in source.replies)
    assert entry.project_url not in body
    assert "cdn.example" not in body

    clicks = [item.get("clickEvent", {}).get("value") for item in segments]
    assert "!!modupdate info 1" in clicks, clicks

    # And the log form really does still carry it, so the two are not the same screen twice.
    from mod_update_checker.i18n import make_translator
    from mod_update_checker.report import render_summary

    assert entry.project_url in "\n".join(render_summary(report, make_translator("zh_cn")))


# --------------------------------------------------------------------------------------
# 翻页、候选补全、裸命令、通知配色（v1.5.0）
#
# 四件事都是「让界面更会用」：列表翻页而不是截断、歧义时给可点的候选、不带参数的命令显示
# 帮助、通知按语义上色。这里的断言都盯着发给客户端的东西（segment、clickEvent、color），
# 因为它们正是 ``str()`` 会丢掉的那部分。
# --------------------------------------------------------------------------------------


def _listing_entries(count, status="up_to_date"):
    return [
        _bulk_entry("mod{:02d}".format(index), "Mod {:02d}".format(index),
                    "mod{:02d}.jar".format(index), status=status)
        for index in range(count)
    ]


def _pager_clicks(source):
    """玩家看到的翻页按钮（run_command 的点击目标），按出现顺序。

    翻页条**不是**最后一条回复了：每屏末尾还有一条底部闭合线（v1.6.0 起）。所以这里按内容
    找——含 ``[上一页]`` / ``[下一页]`` 的那条，而不是按位置取最后一条。
    """
    strip = next(
        reply for reply in reversed(source.raw)
        if any(item.get("text") in ("[上一页]", "[下一页]") for item in _segments(reply))
    )
    return [item["clickEvent"] for item in _segments(strip) if "clickEvent" in item]


def _last_reply_with(source, marker):
    """最后一条含 ``marker`` 的回复——同上，位置不再可依赖。

    ``_PlayerSource`` 存原始对象（``raw``），``_ReplyRecorder`` 只存字符串；两种都认。
    """
    replies = getattr(source, "raw", None) or source.replies
    return next(reply for reply in reversed(replies) if marker in str(reply))


def test_the_listing_pages_with_clickable_buttons(tmp_path, monkeypatch):
    """一页装不下的列表翻页看，而不是被截断；翻页条做成标题栏的形状。

    14 个 Mod、每页 10 行（底部闭合线占掉一行之后），所以有两页：两页的翻页条都是完整的
    （金 ``=``、aqua 按钮、黄页码），走到头的那一侧变成灰的、点不动——条的形状不随页码变。
    而回到第一页的命令**不带页码**——``!!muc list`` 就是第一页，命令短一点更值得。
    """
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, _listing_entries(14))
    source = _PlayerSource("Admin")

    plugin._show_list(source, "", "!!muc")

    assert "Mod 00" in source.body and "Mod 13" not in source.body
    segments = list(_segments(_last_reply_with(source, "[上一页]")))
    text = "".join(item.get("text", "") for item in segments)
    assert text.startswith("=") and text.endswith("="), text
    assert "1/2" in text
    clicks = _pager_clicks(source)
    assert [click["value"] for click in clicks] == ["!!muc list 2"]
    assert clicks[0]["action"] == "run_command"
    # 上一页在条上，但走到头了：灰的、没有点击事件。
    previous = next(item for item in segments if item.get("text") == "[上一页]")
    assert previous["color"] == "dark_gray" and "clickEvent" not in previous

    second = _PlayerSource("Admin")
    plugin._show_list(second, "2", "!!muc")

    assert "Mod 13" in second.body and "Mod 00" not in second.body
    assert [click["value"] for click in _pager_clicks(second)] == ["!!muc list"]
    following = next(
        item for item in _segments(_last_reply_with(second, "[下一页]"))
        if item.get("text") == "[下一页]"
    )
    assert following["color"] == "dark_gray" and "clickEvent" not in following


def test_turning_the_page_does_not_widen_a_filtered_listing(tmp_path, monkeypatch):
    """筛选后翻页仍在筛选里翻——翻页不能把「待安装的」悄悄变成「全部」。

    这是把 filter_text 一路带到按钮里的原因：命令是拼出来的，拼接的地方错了，第二页就会
    变成另一份列表。
    """
    plugin, _server = _one_screen_setup(
        tmp_path, monkeypatch, _listing_entries(13, status="awaiting_install"))
    source = _PlayerSource("Admin")

    plugin._show_list(source, "awaiting_install", "!!muc")

    assert [click["value"] for click in _pager_clicks(source)] == [
        "!!muc list awaiting_install 2"
    ]


def test_a_page_number_does_not_hide_a_misspelled_status(tmp_path, monkeypatch):
    """页码先被摘掉，剩下的词才是状态——报错时显示的是那个词，不是整串。"""
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, _listing_entries(14))
    source = _ReplyRecorder()

    plugin._show_list(source, "updateable 2", "!!muc")

    message = str(source.replies[0])
    assert "未知的状态" in message and "updateable" in message
    assert "updateable 2" not in message, "页码不该出现在报错里"


def test_the_console_gets_the_pager_as_a_command_not_a_button(tmp_path, monkeypatch):
    """控制台点不了，所以给它能敲的命令——带按钮的那一行在终端里只是装饰。"""
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, _listing_entries(14))
    source = _ReplyRecorder()

    plugin._show_list(source, "", "!!muc")

    pager = str(_last_reply_with(source, "第 1/2 页"))
    assert "第 1/2 页" in pager
    assert "下一页：!!muc list 2" in pager
    assert "上一页" not in pager
    # 底栏对控制台是**素的一条线**：那句「悬停/点击」在终端里描述的事情做不到。
    # 宽度对的是标题栏那一条（不是写死的 53——那是插件真正叫这个名字时才成立）。
    closing = str(source.replies[-1])
    title = str(source.replies[0])
    assert closing == "=" * len(title), (closing, title)


def test_the_console_listing_keeps_the_version_numbers_on_the_row(tmp_path, monkeypatch):
    """控制台的列表行印**版本数字**，不是 ``[版本]`` 标签——终端里没有悬停。

    这是把上一轮的管道接上：``_entry_row`` 当初把行形态写死成聊天形态（那个旋钮现在叫
    ``chat_form``），于是聊天**和**控制台都换成了标签——可控制台的读者悬停不了，那些版本号
    就这么从他们眼前消失了（模块自己的 docstring 却写着「the console keeps the numbers」）。
    现在由调用方按 source 决定：玩家给标签（悬停看），控制台给数字。
    """
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, _listing_entries(2))

    console = _ReplyRecorder()
    plugin._show_list(console, "", "!!muc")
    body = "\n".join(str(reply) for reply in console.replies)
    assert "[版本]" not in body, body
    assert "1.0.0" in body, body

    player = _PlayerSource("Admin")
    plugin._show_list(player, "", "!!muc")
    assert "[版本]" in player.body
    # 玩家看到的行上没有版本数字——数字在标签的浮窗里（另一条测试钉着浮窗的内容）。
    assert "1.0.0" not in player.body, player.body


def test_the_versions_and_the_detail_link_explain_themselves_on_hover():
    """``[版本]`` 与 ``[详细信息]`` 的浮窗：用户点名要的两处。

    - ``[版本]``（绿）悬停：更新的行是「旧(红) >>> 新(绿)」，已是最新就是一个黄版本；
    - ``[详细信息]``（aqua）悬停：说明它会执行 ``!!muc info <Mod 名>``。
      点击仍然按编号跑（编号是最稳的手柄），但浮窗里给的是人能读、能照着敲的那条命令。

    控制台保留印在行上的数字：日志行悬停不了——和 URL 只留在控制台是同一个理由。
    """
    import json as _json

    import mod_update_checker as plugin
    from mod_update_checker.report import (
        VERSION_ARROW,
        VERSION_CURRENT,
        VERSION_NEW,
        VERSION_OLD,
        UpdateEntry,
        render_index_row,
        version_summary,
    )

    previous = plugin._config
    plugin._apply_language(None, _config_with({"language": "zh_cn"}))
    try:
        fresh = UpdateEntry(mod_id="a", name="Alpha", file_name="a.jar",
                            local_version="1.0.0", latest_version="1.1.0",
                            status=plugin.STATUS_UPDATE_AVAILABLE)
        current = UpdateEntry(mod_id="b", name="Beta", file_name="b.jar",
                              local_version="1.0.0", status=plugin.STATUS_UP_TO_DATE)

        assert version_summary(fresh, plugin.tr) == [
            (VERSION_OLD, "1.0.0"), (VERSION_ARROW, " >>> "), (VERSION_NEW, "1.1.0"),
        ]
        assert version_summary(current, plugin.tr) == [(VERSION_CURRENT, "1.0.0")]

        segments = list(_segments(plugin._entry_row(1, fresh, "!!muc")))
        label = next(item for item in segments if item.get("text", "").endswith("[版本]"))
        assert label["color"] == "green"
        tooltip = _json.dumps(label["hoverEvent"], ensure_ascii=False)
        assert '"1.0.0", "color": "red"' in tooltip, tooltip
        assert '"1.1.0", "color": "green"' in tooltip, tooltip

        link = next(item for item in segments if item.get("text") == "[详细信息]")
        assert link["clickEvent"]["value"] == "!!muc info 1"
        assert "!!muc info Alpha" in _json.dumps(link["hoverEvent"], ensure_ascii=False)

        console = str(render_index_row(1, fresh, plugin.tr))
        assert "1.0.0 -> 1.1.0" in console and "[版本]" not in console
    finally:
        plugin._config = previous


def test_the_font_model_is_the_one_measured_off_the_screenshots():
    """字宽表钉在**截图里量出来的数字**上——它是量出来的，不是数出来的。

    这条不能靠 ``test_the_chat_listing_lines_up_in_columns``：那条用的是同一张表去量，
    表整体错了它也不知道（自证）。这里的期望值全部来自玩家客户端的截图，
    ``bench/read_band_runs.py`` 逐字形量出步进、``bench/measure_list_columns.py`` 量出每格的
    起始列——单位是 1/4 个普通字母（那份字体里一个字母 24 像素）。

    改这张表就必须来改这里的数字，而改之前得先有一张新截图。
    """
    from mod_update_checker.report import (
        CJK_QUARTERS,
        QUARTERS_PER_LETTER,
        char_quarters,
        text_quarters,
    )

    assert QUARTERS_PER_LETTER == 4
    # 普通字母、数字、空格：一个字母
    assert char_quarters("A") == 4 and char_quarters(" ") == 4 and char_quarters("7") == 4
    # 窄的：四分之三
    for narrow in "ijltfI1":
        assert char_quarters(narrow) == 3, narrow
    # 点与方括号：一半
    for half in ".[]":
        assert char_quarters(half) == 2, half
    # 汉字：2¼（``display_width`` 说的是 2）
    assert CJK_QUARTERS == 9 and char_quarters("版") == 9

    # 整行：``[1] QuickShulker`` 在截图里从 x=9 走到 x=351，342 像素 ÷ 6 = 57
    assert text_quarters("[1] QuickShulker") == 57
    assert text_quarters("[2] Ledger") == 36
    assert text_quarters("[10] Carpet TIS Addition") == 86
    # 两格固定标签：``[版本]`` 22、``[状态: ✔]`` 34
    assert text_quarters("[版本]") == 22
    assert text_quarters("[状态: ✔]") == 34


def test_the_cell_widths_are_cached_but_never_across_languages():
    """格宽按语言缓存，而**不**是全局缓存一个值。

    那两个宽度是每行都要的（原来每行现算 13 次取词），所以缓存是对的；但中英两套标签长度
    差得多（`[status: ✔]` 对 `[状态: ✔]`），缓存串了语言就是整列歪掉，而且不会有任何报错。
    这条同时钉住「缓存命中」与「换语言会算新的」。
    """
    from mod_update_checker.i18n import make_translator
    from mod_update_checker.report import chat_cell_widths

    chinese = make_translator("zh_cn")
    english = make_translator("en_us")

    first = chat_cell_widths(chinese)
    assert chat_cell_widths(chinese) == first, "第二次调用应当命中缓存"
    assert first != chat_cell_widths(english), "两种语言的格宽不可能一样"
    assert chat_cell_widths(chinese) == first, "英文那次不能污染中文的缓存"

    # 不认识语言属性的翻译器（测试里的替身、lambda）：照旧现算，不做缓存。
    assert chat_cell_widths(lambda key, **kwargs: "x") == (4, 4)


def test_the_chat_listing_lines_up_in_columns(tmp_path, monkeypatch):
    """聊天的列表行是**分列**的：名称一格、``[版本]`` 一格、``[状态: ✔]`` 一格、按钮一格（用户点名）。

    名字长短不齐的时候，列就参差——现在名称（含编号）补到 ``CHAT_PREFIX_QUARTERS``，
    ``[版本]`` 与 ``[状态: ✔]`` 也各自补到固定宽度，于是三列都落在同一个落点上。

    对齐量的**不是** ``display_width``：游戏里那套字宽是量出来的（``report.GLYPH_QUARTERS``），
    汉字 2¼ 个字母、``i``/``l`` 之类只有四分之三——按「东亚字符算两个」去补，正是列歪掉的
    原因。量的是 ``text_quarters``。

    而这个字体给不出「一模一样」：补白只能是**整格空格**，文字宽度却不总是四分之一字母的
    整数倍，所以每一格的落点与目标最多差半个空格。测试断言的是这个上界——写死成「相等」
    会是一条永远没人能通过的断言。
    """
    from mod_update_checker.report import (
        QUARTERS_PER_LETTER,
        ROW_FIELD_BODY,
        ROW_FIELD_NOTE,
        chat_row_fields,
        text_quarters,
    )

    names = ["Ledger", "Just Enough Items", "A Very Long Mod Name Indeed",
             "Carpet TIS Addition", "Sodium", "MCDRCommand", "Simple Voice Chat",
             "AppleSkin", "Lithium", "Better Hanging Signs", "Iris"]
    entries = [_bulk_entry("m{:02d}".format(i), name, "m{:02d}.jar".format(i),
                           status="up_to_date")
               for i, name in enumerate(names)]
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, entries)

    starts = {ROW_FIELD_BODY: set(), ROW_FIELD_NOTE: set(), "link": set()}
    for number, entry in enumerate(entries, start=1):
        column = 0
        seen = set()
        for role, text in chat_row_fields(number, entry, plugin.tr):
            # 状态格是三块（文字 / 图标 / 括号+补白），一列只算它**开始**的那一块。
            if role in starts and role not in seen:
                starts[role].add(column)
                seen.add(role)
            column += text_quarters(text)

        row = plugin._entry_row(number, entry, "!!muc")
        text = "".join(str(piece) for piece in row.children)
        starts["link"].add(text_quarters(text[:text.index("[详细信息]")]))

    for name, columns in starts.items():
        assert max(columns) - min(columns) <= QUARTERS_PER_LETTER - 1, (name, sorted(columns))
    # 一列都没对齐的话上面也会「通过」（只有一个值）——顺带钉住它确实在往右走。
    assert starts["body"] != starts["note"] != starts["link"], starts


def test_a_long_mod_name_is_cut_and_its_full_form_is_on_hover(tmp_path, monkeypatch):
    """名字超宽就截成 ``...``，全名在浮窗里——每一行的名称都有这个浮窗，不只是被截的那些。

    编号也是这一格的一部分：``[10] `` 比 ``[1] `` 宽，截断按**当前行**真正剩下的宽度算，
    所以两行的名称格都落在同一个落点上（补白是整格空格，最多差半个空格）。
    """
    import json as _json

    from mod_update_checker.report import (
        CHAT_PREFIX_QUARTERS,
        QUARTERS_PER_LETTER,
        text_quarters,
    )

    long_name = "A Very Long Mod Name Indeed"
    entry = _bulk_entry("long", long_name, "long.jar", status="up_to_date")
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, [entry])

    for number in (1, 10):
        row = plugin._entry_row(number, entry, "!!muc")
        pieces = [piece for piece in row.children if str(piece)]
        prefix, name_piece = pieces[0], pieces[1]
        assert str(prefix) == "[{}] ".format(number)
        assert str(name_piece).rstrip().endswith("..."), str(name_piece)
        assert abs(text_quarters(str(prefix) + str(name_piece))
                   - CHAT_PREFIX_QUARTERS) <= QUARTERS_PER_LETTER // 2, str(name_piece)
        assert _json.dumps(name_piece.to_json_object(), ensure_ascii=False).count(long_name), (
            "全名没有挂在这块上")
        # 名字那一段的浮窗就是全名本身。
        hover = name_piece.to_json_object()["hoverEvent"]
        assert _json.dumps(hover, ensure_ascii=False).count(long_name) == 1

    short = _bulk_entry("short", "Iris", "iris.jar", status="up_to_date")
    row = plugin._entry_row(1, short, "!!muc")
    name_piece = [piece for piece in row.children if str(piece)][1]
    assert str(name_piece).startswith("Iris")
    assert "..." not in str(name_piece)
    # 不截断的名字也带全名浮窗：规则是「任何名称都解释自己」，不是「只有截断的才解释」。
    assert "hoverEvent" in name_piece.to_json_object()


def test_the_status_tag_explains_itself_on_hover(tmp_path, monkeypatch):
    """``[状态: ❌]``：每一行都是同一句话，图标说状态，**浮窗第一行用状态色说出这个状态**。

    读者看到的是 ``[状态: ❌]``；「上游没有适配当前加载器或游戏版本的构建」、识别方式
    （``按名称近似匹配``），以及顶在最前面的那个**亮红色的状态名**都在浮窗里——状态色原来
    贴在行上，现在那一格每一行都长一样，颜色就搬到浮窗的第一行（用户点的名）。
    控制台那一份没有浮窗，所以整句照旧印在行上。
    """
    import json as _json

    import mod_update_checker as plugin
    from mod_update_checker.report import UpdateEntry

    previous = plugin._config
    plugin._apply_language(None, _config_with({"language": "zh_cn"}))
    try:
        blocked = UpdateEntry(mod_id="q", name="QuickShulker", file_name="q.jar",
                              local_version="1.0.0", latest_version="1.1.0",
                              status=plugin.STATUS_NO_COMPATIBLE_BUILD,
                              matched_by="name")

        segments = list(_segments(plugin._entry_row(1, blocked, "!!muc")))
        # 那一格的文字永远黄（每一行都是同一句话），**图标带状态自己的颜色**（用户点名：
        # 打叉红、打勾绿、有更新与待安装蓝），浮窗挂在两块上——鼠标落在哪一块都能展开。
        words = next(item for item in segments
                     if item.get("text", "").startswith("  [状态: "))
        assert words["color"] == "yellow", words
        mark = next(item for item in segments if item.get("text") == "❌")
        assert mark["color"] == "red", mark
        for piece in (words, mark):
            assert "hoverEvent" in piece, piece

        tooltip = plugin._status_hover(blocked)
        first = tooltip.children[0]
        assert str(first) == "无适配构建", str(first)
        assert first.to_json_object()["color"] == "red", first.to_json_object()
        flat = _json.dumps(tooltip.to_json_object(), ensure_ascii=False)
        assert "上游没有适配当前" in flat, flat
        assert "按名称近似匹配" in flat, flat

        # 同一个状态，控制台那一份：整句 + 识别方式都印在行上（那里没有浮窗）。
        console = str(plugin._entry_row(1, blocked, "!!muc", chat_form=False))
        assert "(无适配构建，按名称近似匹配)" in console, console
    finally:
        plugin._config = previous


def test_the_facts_of_a_backup_or_a_jar_row_live_in_their_tooltips(tmp_path, monkeypatch):
    """没有版本可藏的两类行：中格是 ``[备份]`` / ``[文件]``，事实（大小、天数、文件名）在浮窗里。

    控制台那一份没有任何悬停可言，所以它照旧把事实印在行上——同一个 ``_facts_hover`` 里的
    文本，正是从这里（或从 ``line.old_backup``）取的。
    """
    import json as _json

    from mod_update_checker.cleanup import Backup
    from mod_update_checker.report import STATUS_UNRESOLVED, UpdateEntry, entry_from_backup

    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, [])

    previous = plugin._config
    plugin._apply_language(None, _config_with({"language": "zh_cn"}))
    try:
        backup = entry_from_backup(Backup("retired.jar.old", 1258291, 412))
        segments = list(_segments(plugin._entry_row(2, backup, "!!muc")))
        cell = next(item for item in segments if item.get("text", "").strip() == "[备份]")
        tooltip = _json.dumps(cell["hoverEvent"], ensure_ascii=False)
        assert "1.2 MB" in tooltip and "412" in tooltip, tooltip
        mark = next(item for item in segments if item.get("text") == "⏪")
        assert "插件留下的旧版备份" in _json.dumps(mark["hoverEvent"], ensure_ascii=False)

        unresolved = UpdateEntry(mod_id="x", name="MCDRCommand",
                                 file_name="MCDRcommandFabric-26.3-v1.3.0.jar",
                                 status=STATUS_UNRESOLVED)
        segments = list(_segments(plugin._entry_row(6, unresolved, "!!muc")))
        cell = next(item for item in segments if item.get("text", "").strip() == "[文件]")
        assert "MCDRcommandFabric-26.3-v1.3.0.jar" in _json.dumps(
            cell["hoverEvent"], ensure_ascii=False)

        console = str(plugin._entry_row(6, unresolved, "!!muc", chat_form=False))
        assert "MCDRcommandFabric-26.3-v1.3.0.jar" in console, console
    finally:
        plugin._config = previous


def test_the_staged_plans_confirm_line_is_a_red_fillable_button(tmp_path, monkeypatch):
    """计划末尾那句「输入 ``!!muc confirm``」：命令是**亮红色**的，点一下填进输入框。

    填而不是跑是有意的：``confirm`` 一按下去就真的删/装了，聊天框里手滑点一下不该能造成那种
    事——填进输入框仍然要再按一次回车，和「两步确认」要的正是同一个动作。控制台拿到的是同一
    句话（``str()`` 只掉颜色和点击，不掉字）。
    """
    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("ancient.jar.old", 512, 400)],
    )
    source = _PlayerSource("Admin")

    plugin._manual_delete(source, "ancient.jar.old", "!!muc")

    ask = str(source.raw[-1])
    assert "!!muc confirm" in ask and "120" in ask
    pieces = [piece for piece in source.raw[-1].children]
    button = next(piece for piece in pieces if str(piece) == "!!muc confirm")
    assert button.to_json_object()["color"] == "red", button.to_json_object()
    assert button.to_json_object()["clickEvent"] == {
        "action": "suggest_command", "value": "!!muc confirm",
    }, button.to_json_object()

    # 控制台形态：同一句话，一个字的差别都没有。
    console = _ReplyRecorder()
    plugin._manual_delete(console, "ancient.jar.old", "!!muc")
    expected = plugin.tr("command.action.ask", seconds=120, command="!!muc confirm")
    assert str(console.replies[-1]) == expected, console.replies[-1]
    assert (mods / "ancient.jar.old").is_file(), "这条命令不该真的做什么"
    # 计划还摆在槽位里——收掉它，别让下一条测试撞见（槽位是模块级的）。
    plugin._clear_pending()


def _run_bare(tree, source, command):
    """Run a command through MCDR's own parser, without a server.

    ``_entry_execute`` is marked private, but it is what MCDR's CommandManager calls, and
    ``DirectCallbackInvoker`` is MCDR's own helper for invoking the scheduled callbacks — so
    this asserts "what happens when that command is typed" rather than "the handler can
    print", which is the difference between testing the wiring and testing the string.
    """
    from mcdreforged.command.builder.callback import DirectCallbackInvoker

    executions = tree._entry_execute(source, command)
    assert executions, "{} matched nothing".format(command)
    for execution in executions:
        execution.scheduled_callback.invoke(DirectCallbackInvoker())


def test_the_bare_command_shows_the_help_page(tmp_path, monkeypatch):
    """裸命令 = 帮助页，这是本项目的约定（v1.5.0 起）。"""
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, [])
    source = _PlayerSource("Admin")

    _run_bare(plugin._command_tree("!!muc"), source, "!!muc")

    assert "用法：!!muc <子命令>" in source.body
    assert "不带子命令就是本页" in source.body
    assert "!!muc check" in source.body and "!!muc summary" in source.body


def test_the_summary_is_still_reachable_by_its_own_word(tmp_path, monkeypatch):
    """汇总从裸命令搬到了 ``!!muc summary``——少一个词，不少一条路。"""
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, [_entry_for_screens()])
    source = _PlayerSource("Admin")

    _run_bare(plugin._command_tree("!!muc"), source, "!!muc summary")

    assert "Alpha" in source.body, source.body


def _ambiguous_entry(name):
    from mod_update_checker.report import UpdateEntry

    item = UpdateEntry(
        mod_id=name.lower().replace(" ", "_"), name=name,
        file_name=name.lower().replace(" ", "-") + ".jar",
        status="update_available",
    )
    item.download_url = "https://cdn.example/x.jar"
    item.download_sha1 = "a" * 40
    return item


def test_an_ambiguous_handle_offers_clickable_completions(tmp_path, monkeypatch):
    """歧义时列出候选，每个候选都能点——点一下把**完整命令填进输入框**。

    这是游戏内的候选列表：用 suggest_command 而不是 run_command，因为读者正在打字，填进去
    后还可以改；而且这样他们看得到完整的拼写，下次不用再点。
    """
    plugin, _server = _one_screen_setup(
        tmp_path, monkeypatch, [_ambiguous_entry("Sodium"), _ambiguous_entry("Sodium Extra")]
    )
    source = _PlayerSource("Admin")

    plugin._manual_download(source, "sod", "!!muc")

    assert plugin._pending_action is None, "歧义的时候不该暂存任何操作"
    assert "同时匹配 2 个 Mod" in source.body
    clicks = [item["clickEvent"] for item in _segments(source.raw[-1]) if "clickEvent" in item]
    assert [click["value"] for click in clicks] == [
        "!!muc download Sodium", "!!muc download Sodium Extra",
    ]
    assert {click["action"] for click in clicks} == {"suggest_command"}


def test_the_console_gets_the_candidates_without_a_button_row(tmp_path, monkeypatch):
    """控制台里候选已经在句子里了，不再多打一行点不了的东西。"""
    plugin, _server = _one_screen_setup(
        tmp_path, monkeypatch, [_ambiguous_entry("Sodium"), _ambiguous_entry("Sodium Extra")]
    )
    source = _ReplyRecorder()

    plugin._manual_download(source, "sod", "!!muc")

    assert len(source.replies) == 1, [str(item) for item in source.replies]
    assert "Sodium Extra" in str(source.replies[0])


def test_the_mcdr_console_can_tab_complete_mod_names(tmp_path, monkeypatch):
    """控制台里的 Tab 补全，走 MCDR 自己的建议管线。

    游戏内做不到（``!!`` 是聊天消息，原版只补全 ``/`` 命令），控制台可以——``suggests()``
    是 MCDR 的建议接口，这两条断言的就是「敲 ``!!muc download `` 再按 Tab 会看到什么」。
    """
    plugin, _server = _one_screen_setup(
        tmp_path, monkeypatch,
        [
            _bulk_entry("sodium", "Sodium", "sodium.jar"),
            _bulk_entry("flaky", "Flaky Mod", "flaky.jar", status="awaiting_install"),
        ],
    )
    tree = plugin._command_tree("!!muc")
    source = _PlayerSource("Admin")

    def complete(command):
        return [item.command for item in tree._entry_generate_suggestions(source, command)]

    assert complete("!!muc download ") == ["!!muc download all", "!!muc download Sodium"]
    assert complete("!!muc install ") == ["!!muc install all", "!!muc install Flaky Mod"]
    assert "!!muc list up_to_date" in complete("!!muc list ")
    assert "!!muc info Flaky Mod" in complete("!!muc info ")

    # 没有报告时不给候选——比给一份过期名单好。
    plugin._last_report = None
    assert complete("!!muc download ") == []


class _TextRecorder:
    """A server that keeps the raw text object of every delivery, for colour assertions."""

    def __init__(self):
        self.deliveries = []

    def is_server_running(self):
        return True

    def tell(self, player, text, **_kwargs):
        self.deliveries.append((player, text))


def test_an_install_notice_is_coloured_by_role_not_painted_one_colour(tmp_path, monkeypatch):
    """「已替换 N 个 Mod」不再整段同色：头部白、替换行灰、装不上的行红。

    颜色由行的**角色**决定，而角色在构造句子的地方就定了——不是投递层读文本猜出来的
    （v1.3.0 的教训）。所以这里断言的是 segment 的 color 字段。
    """
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, [])
    server = _TextRecorder()
    data = {
        "at": "2026-10-08T18:55:17+08:00",
        "installed": [
            {"name": "Fabric API", "version": "0.162.0", "backup_file": "fabric.old"},
            {"name": "Just Enough Items", "version": "31.9.0", "backup_file": "jei.old"},
        ],
        "skipped": [{"name": "Broken Mod", "detail": "hash-mismatch"}],
    }

    plugin._tell_player(server, "Admin", plugin._install_summary_lines(data))

    _player, text = server.deliveries[0]
    coloured = {
        item.get("text", "").strip(): item.get("color")
        for item in _segments(text)
        if item.get("text", "").strip()
    }
    header = [color for body, color in coloured.items() if "已替换" in body]
    rows = [color for body, color in coloured.items() if "Fabric API" in body]
    skipped = [color for body, color in coloured.items() if "没有被安装" in body]

    assert header == ["white"], coloured
    assert rows == ["gray"], coloured
    assert skipped == ["red"], coloured
    assert len(set(coloured.values())) > 1, "还是一整片同色"


def test_the_update_notice_gives_each_kind_of_line_its_role(tmp_path, monkeypatch):
    """更新通知的每一行也带角色：小节标题、要处理的行、脚注。

    角色断言在这里、颜色断言在上面那条：映射只有一份（``_NOTICE_COLOURS``），所以分开钉
    才不会让两套东西各自演化。
    """
    from mod_update_checker.report import UpdateEntry

    waiting = UpdateEntry(mod_id="a", name="Alpha", file_name="a.jar",
                          local_version="1.0.0", latest_version="1.1.0",
                          status="update_available")
    plugin, _server = _one_screen_setup(tmp_path, monkeypatch, [waiting])

    lines = plugin._notification_lines(plugin._last_report)

    assert lines[0].role == "heading"
    # 有更新的行是 ``update``（蓝）——和列表里同一个状态用同一个颜色，而不是「要处理就黄」。
    assert any(notice.role == "update" for notice in lines)
    assert lines[-1].role == "hint"
    # 行文本不带状态：小节的标题已经说了「更新尚未下载」，行里再写一遍就是重复。
    assert not any("（" in notice.text or "(" in notice.text for notice in lines)


# --------------------------------------------------------------------------------------
# !!muc download / install / confirm
#
# The three commands exist so a single mod can be fetched and installed without switching the
# automatic halves on. Both end in something awkward to undo, so both are staged and carried
# out by ``confirm`` — which means the interesting invariants are about what must NOT happen:
# nothing is fetched on the first command, nothing is installed that the admin did not name,
# and a plan cannot be carried out after the numbers it referred to have moved.
# --------------------------------------------------------------------------------------


class _PlayerSource:
    """A named player command source that records the replies.

    Keeps the raw objects alongside their string form: colour and click events live in the
    segments, and ``str()`` is exactly what drops them. ``has_permission`` answers like an
    admin's, so a command tree can be executed against this source without MCDR itself.
    """

    def __init__(self, player="Admin"):
        self.player = player
        self.is_player = True
        self.replies = []
        self.raw = []

    def reply(self, text, **_kwargs):
        self.raw.append(text)
        self.replies.append(str(text))

    def has_permission(self, _level):
        return True

    @property
    def body(self):
        return "\n".join(self.replies)


def _manual_env(tmp_path, monkeypatch, status="update_available", player="Admin"):
    """The plugin wired to a fake server, with one entry and a matching ledger record.

    The ledger record is written because both ``download`` and ``install`` act on records, not
    on the report: a build with no record is a build the install stage will refuse, and a test
    that skipped this step would pass while the real flow refused.

    The language is set explicitly rather than inherited. ``_translator`` is module state that
    survives between tests, so a fixture that leaves it alone makes every assertion below depend
    on which test ran first — which is a green suite one day and a red one the next.
    """
    import mod_update_checker as plugin
    from mod_update_checker.downloads import DownloadLedger
    from mod_update_checker.report import UpdateEntry

    monkeypatch.setattr(plugin, "_config", _config_with({"language": "zh_cn"}), raising=False)
    plugin._apply_language(None, plugin._config)
    plugin._stop_event.clear()
    plugin._clear_pending()

    server = _FakeServer(tmp_path, levels={"Admin": 4, "Other": 4})
    entry = UpdateEntry(
        mod_id="sodium",
        name="Sodium",
        file_name="sodium.jar",
        local_version="1.0.0",
        latest_version="1.1.0",
        status=status,
        platform="modrinth",
        project_url="https://modrinth.com/mod/sodium",
        download_url="https://cdn.example/sodium-1.1.0.jar",
        download_filename="sodium-fabric-1.1.0.jar",
        download_sha1="a" * 40,
        download_size=2048,
    )
    monkeypatch.setattr(plugin, "_last_report", _report_with([entry]), raising=False)
    monkeypatch.setattr(plugin, "_server", server, raising=False)

    ledger = DownloadLedger(
        Path(server.get_data_folder()) / plugin.DOWNLOAD_LEDGER_FILE_NAME, logger=server.logger
    )
    ledger.record("sodium", "sodium-fabric-1.1.0.jar", "a" * 40, "1.1.0", "2026-01-01T00:00:00+00:00",
                  installed_file="sodium.jar", name="Sodium")
    ledger.save()

    return plugin, server, _PlayerSource(player), entry, ledger


def test_download_stages_a_plan_and_fetches_nothing(tmp_path, monkeypatch):
    """The first command must not spend bandwidth: that is what ``confirm`` is for."""
    plugin, _server, source, entry, _ledger = _manual_env(tmp_path, monkeypatch)
    fetched = []
    monkeypatch.setattr(
        plugin, "_perform_manual_download", lambda item: fetched.append(item), raising=False
    )

    plugin._manual_download(source, "1", "!!muc")

    assert fetched == [], "the download ran before it was confirmed"
    pending = plugin._pending_action
    assert pending is not None and pending["kind"] == "download"
    assert "!!muc confirm" in source.body
    assert "sodium-fabric-1.1.0.jar" in source.body, source.body
    assert "2 KB" in source.body, "the plan should say how big the file is"


def test_confirm_is_what_runs_the_download(tmp_path, monkeypatch):
    """And the outcome is reported back with the next command to type."""
    from mod_update_checker.downloads import STATUS_DOWNLOADED

    plugin_mod, server, source, entry, _ledger = _manual_env(tmp_path, monkeypatch)

    class _Outcome:
        status = STATUS_DOWNLOADED
        path = "config/mod_update_checker/downloads/sodium-fabric-1.1.0.jar"
        file_name = "sodium.jar"
        detail = ""

    plugin_mod._manual_download(source, "1", "!!muc")
    monkeypatch.setattr(plugin_mod, "_perform_manual_download",
                        lambda item: _Outcome(), raising=False)
    plugin_mod._manual_confirm(source, "!!muc")

    # The fetch runs on its own thread; wait for it rather than sleeping a fixed time.
    for _ in range(200):
        if "已下载到" in source.body:
            break
        time.sleep(0.02)

    assert "!!muc install 1" in source.body, source.body
    assert plugin_mod._pending_action is None, "the plan was carried out, so it is spent"


def test_a_failed_download_says_why_and_stays_an_update(tmp_path, monkeypatch):
    """A failure is reported in words, not as the internal code it travels as."""
    from mod_update_checker.downloads import STATUS_FAILED

    plugin_mod, _server, source, entry, _ledger = _manual_env(tmp_path, monkeypatch)

    class _Outcome:
        status = STATUS_FAILED
        path = ""
        file_name = "sodium.jar"
        detail = "no-hash-to-verify"

    plugin_mod._manual_download(source, "1", "!!muc")
    monkeypatch.setattr(plugin_mod, "_perform_manual_download",
                        lambda item: _Outcome(), raising=False)
    plugin_mod._manual_confirm(source, "!!muc")

    for _ in range(200):
        if "失败" in source.body:
            break
        time.sleep(0.02)

    assert "上游没有提供哈希" in source.body, source.body
    assert "no-hash-to-verify" not in source.body, "a raw code reached the player"


def test_an_already_downloaded_mod_points_at_install(tmp_path, monkeypatch):
    """The one case that must not say "cannot download" — there is nothing left to fetch."""
    plugin, _server, source, _entry, _ledger = _manual_env(
        tmp_path, monkeypatch, status="awaiting_install"
    )

    plugin._manual_download(source, "1", "!!muc")

    assert plugin._pending_action is None
    assert "!!muc install 1" in source.body, source.body


def test_a_mod_with_nothing_to_fetch_says_which_case_it_is(tmp_path, monkeypatch):
    """Four situations, four sentences — an admin needs to know which one they are in."""
    for status, expected in (
        ("up_to_date", "已是最新"),
        ("no_compatible_build", "没有适配本服务端的构建"),
        ("unresolved", "无法定位到 Modrinth"),
    ):
        plugin, _server, source, _entry, _ledger = _manual_env(
            tmp_path, monkeypatch, status=status
        )
        plugin._manual_download(source, "1", "!!muc")

        assert plugin._pending_action is None, status
        assert expected in source.body, (status, source.body)


def test_install_refuses_a_mod_that_has_not_been_downloaded(tmp_path, monkeypatch):
    """It names the command that fixes it, with the number already filled in."""
    plugin, _server, source, _entry, _ledger = _manual_env(tmp_path, monkeypatch)

    plugin._manual_install(source, "1", "!!muc")

    assert plugin._pending_action is None
    assert "!!muc download 1" in source.body, source.body


def test_the_number_the_install_hint_suggests_is_the_listing_number(tmp_path, monkeypatch):
    """``!!muc install <name>`` may answer "type !!muc download 3" — the ``3`` has to be right.

    Asserted with the mod **not** first in the listing, because a one-entry report would be
    satisfied by any numbering at all. The handle is a name on purpose: that is the form which
    has to go through the lookup and then still produce the listing's number.
    """
    import mod_update_checker as plugin
    from mod_update_checker.report import UpdateEntry

    monkeypatch.setattr(plugin, "_config", _config_with({"language": "zh_cn"}), raising=False)
    plugin._apply_language(None, plugin._config)
    plugin._stop_event.clear()
    plugin._clear_pending()

    server = _FakeServer(tmp_path, levels={"Admin": 4})
    fetched = UpdateEntry(mod_id="alpha", name="Alpha", file_name="alpha.jar",
                          local_version="1.0.0", latest_version="1.1.0",
                          status="awaiting_install")
    waiting = UpdateEntry(mod_id="zeta", name="Zeta", file_name="zeta.jar",
                          local_version="1.0.0", latest_version="1.1.0",
                          status="update_available")
    # Both are actionable, so they share a rank and are ordered by name: Alpha is 1, Zeta is 2.
    monkeypatch.setattr(plugin, "_last_report", _report_with([fetched, waiting]), raising=False)
    monkeypatch.setattr(plugin, "_server", server, raising=False)

    source = _PlayerSource("Admin")
    plugin._manual_install(source, "Zeta", "!!muc")

    assert "!!muc download 2" in source.body, source.body


def test_install_stages_the_swap_and_confirm_authorises_only_that_mod(tmp_path, monkeypatch):
    """The whole point of the per-record flag: five downloads, one named, one installed."""
    from mod_update_checker.downloads import DownloadLedger

    plugin_mod, server, source, _entry, ledger = _manual_env(
        tmp_path, monkeypatch, status="awaiting_install"
    )
    # A second mod, downloaded and NOT authorised.
    ledger.record("other", "other-2.0.jar", "b" * 40, "2.0", "2026-01-01T00:00:00+00:00",
                  installed_file="other.jar", name="Other")
    ledger.save()

    plugin_mod._manual_install(source, "1", "!!muc")
    assert plugin_mod._pending_action["kind"] == "install"
    assert "sodium.jar" in source.body and "sodium-fabric-1.1.0.jar" in source.body, source.body
    assert "!!muc confirm" in source.body

    plugin_mod._manual_confirm(source, "!!muc")

    assert "已授权" in source.body, source.body
    # Read back from disk: the command writes its own ledger, because by the time an admin
    # confirms, the plugin may have been reloaded and the record is what is on disk.
    written = DownloadLedger(
        Path(server.get_data_folder()) / plugin_mod.DOWNLOAD_LEDGER_FILE_NAME
    )
    assert written.approved_keys() == ["sodium"], "an unauthorised record was swept up"


def test_confirm_with_nothing_staged_says_so(tmp_path, monkeypatch):

    plugin_mod, _server, source, _entry, _ledger = _manual_env(tmp_path, monkeypatch)
    plugin_mod._clear_pending()
    plugin_mod._manual_confirm(source, "!!muc")

    assert "没有待确认的操作" in source.body, source.body


def test_only_the_one_who_staged_it_can_confirm(tmp_path, monkeypatch):
    """A confirmation is a decision, and only its author can make it."""
    plugin, _server, source, _entry, _ledger = _manual_env(tmp_path, monkeypatch, player="Admin")
    other = _PlayerSource("Other")

    plugin._manual_download(source, "1", "!!muc")
    plugin._manual_confirm(other, "!!muc")

    assert "只有本人可以确认" in other.body, other.body
    assert plugin._pending_action is not None, "somebody else's confirm consumed the plan"


def test_a_lapsed_confirmation_is_refused(tmp_path, monkeypatch):
    """A ``!!muc confirm`` typed much later must not act on a plan nobody remembers."""
    plugin, _server, source, _entry, _ledger = _manual_env(tmp_path, monkeypatch)

    plugin._manual_download(source, "1", "!!muc")
    plugin._pending_action["deadline"] = time.monotonic() - 1

    plugin._manual_confirm(source, "!!muc")

    assert "作废" in source.body, source.body
    assert plugin._pending_action is None


def test_a_confirmation_is_dropped_when_the_report_moved_underneath_it(tmp_path, monkeypatch):
    """Numbers are a mapping, and a new report is a new mapping.

    Re-resolving ``1`` against a report that has been replaced is how the wrong mod gets
    installed — the numbers still exist, they just mean something else now.
    """
    from mod_update_checker.report import UpdateEntry

    plugin_mod, server, source, _entry, _ledger = _manual_env(
        tmp_path, monkeypatch, status="awaiting_install"
    )
    plugin_mod._manual_install(source, "1", "!!muc")

    # Same number, different mod: exactly what a re-run produces.
    replacement = UpdateEntry(
        mod_id="lithium", name="Lithium", file_name="lithium.jar",
        local_version="0.1", latest_version="0.2", status="awaiting_install",
    )
    plugin_mod._last_report = _report_with([replacement])

    plugin_mod._manual_confirm(source, "!!muc")

    assert "作废" in source.body, source.body
    assert plugin_mod._pending_action is None


def test_the_console_counts_as_its_own_requester(tmp_path, monkeypatch):
    """A player cannot confirm a plan the console staged, and vice versa."""

    plugin_mod, server, _source, _entry, _ledger = _manual_env(tmp_path, monkeypatch)
    console = _ReplyRecorder()
    console.player = ""
    console.is_player = False

    plugin_mod._manual_install(console, "1", "!!muc")
    # The entry is not downloaded, so nothing was staged — stage it directly to test ``confirm``.
    plugin_mod._pending_action = {
        "kind": "install", "number": 1, "file_name": "sodium.jar", "mod_id": "sodium",
        "requester": plugin_mod._requester(console),
        "deadline": time.monotonic() + 60,
    }

    player = _PlayerSource("Admin")
    plugin_mod._manual_confirm(player, "!!muc")

    assert "只有本人可以确认" in player.body, player.body


# --------------------------------------------------------------------------------------
# The bulk forms: ``download all`` / ``install all``
#
# A batch is a different promise from a single mod: instead of "this one", the admin approves
# a *set*. So the tests below are about the set surviving whatever happens between the two
# commands, about a batch never widening into the ledger, and about ``all`` being a word with
# one meaning rather than sometimes-a-mod.
# --------------------------------------------------------------------------------------


def _bulk_entry(mod_id, name, file_name, status="update_available", latest="1.1.0",
                downloadable=True):
    """One report entry, with all three spellings deliberately different.

    ``id``, ``file name`` and ``display name`` are unrelated on purpose: a test whose entry is
    named after its own id can pass through the id path whatever happened to the name path —
    this suite has made exactly that mistake once already (v1.2.0).
    """
    from mod_update_checker.report import UpdateEntry

    entry = UpdateEntry(
        mod_id=mod_id, name=name, file_name=file_name,
        local_version="1.0.0", latest_version=latest, status=status,
    )
    if downloadable:
        entry.download_url = "https://cdn.example/" + file_name
        entry.download_filename = "{}-{}.jar".format(mod_id, latest)
        entry.download_sha1 = "b" * 40
        entry.download_size = 2048
    return entry


def _bulk_env(tmp_path, monkeypatch, entries):
    """The plugin wired to a fake server whose report holds exactly ``entries``."""
    import mod_update_checker as plugin

    monkeypatch.setattr(plugin, "_config", _config_with({"language": "zh_cn"}), raising=False)
    plugin._apply_language(None, plugin._config)
    plugin._stop_event.clear()
    plugin._clear_pending()

    server = _FakeServer(tmp_path, levels={"Admin": 4})
    monkeypatch.setattr(plugin, "_last_report", _report_with(entries), raising=False)
    monkeypatch.setattr(plugin, "_server", server, raising=False)
    return plugin, server, _PlayerSource("Admin")


def test_download_all_stages_one_plan_for_every_fetchable_mod(tmp_path, monkeypatch):
    """One confirmation for the lot, and the plan names what it is about.

    The un-fetchable update is counted out loud rather than dropped: a plan that quietly covers
    fewer mods than the report lists is how an admin comes to believe a mod was fetched.
    """
    fetchable = _bulk_entry("sodium", "Sodium", "sodium.jar")
    second = _bulk_entry("lithium", "Lithium", "lithium.jar")
    withdrawn = _bulk_entry("withdrawn", "Withdrawn", "withdrawn.jar", downloadable=False)
    settled = _bulk_entry("iris", "Iris", "iris.jar", status="up_to_date")

    plugin, _server, source = _bulk_env(
        tmp_path, monkeypatch, [fetchable, second, withdrawn, settled]
    )
    monkeypatch.setattr(
        plugin, "_perform_manual_downloads",
        lambda items: pytest.fail("the batch ran before it was confirmed"), raising=False,
    )

    plugin._manual_download(source, "all", "!!muc")

    assert plugin._pending_action["kind"] == "download_all"
    assert plugin._pending_action["files"] == ["lithium.jar", "sodium.jar"]
    assert "Sodium" in source.body and "Lithium" in source.body
    assert "下载 2 个" in source.body, source.body
    assert "1 个更新没有可下载的文件" in source.body, source.body
    assert "!!muc confirm" in source.body


def test_download_all_with_nothing_to_fetch_says_which_case_it_is(tmp_path, monkeypatch):
    """Two empty answers, because they are two different situations.

    "Nothing is pending" is a server that is up to date. "Nothing *fetchable*" is a platform
    that published a version whose files were withdrawn — and an admin told the first while
    the second is true stops looking for a problem that does not exist.
    """
    settled = _bulk_entry("iris", "Iris", "iris.jar", status="up_to_date")
    plugin, _server, source = _bulk_env(tmp_path, monkeypatch, [settled])

    plugin._manual_download(source, "all", "!!muc")

    assert "没有待下载的更新" in source.body, source.body
    assert plugin._pending_action is None

    plugin._last_report = _report_with(
        [_bulk_entry("withdrawn", "Withdrawn", "withdrawn.jar", downloadable=False)]
    )
    source.replies.clear()

    plugin._manual_download(source, "all", "!!muc")

    assert "都没有提供可下载的文件" in source.body, source.body
    assert plugin._pending_action is None


def test_confirm_runs_the_whole_batch_and_reports_it(tmp_path, monkeypatch):
    """The outcome is a summary rather than a line per mod, and it names the next command."""
    from mod_update_checker.downloads import STATUS_DOWNLOADED, STATUS_FAILED

    first = _bulk_entry("sodium", "Sodium", "sodium.jar")
    second = _bulk_entry("lithium", "Lithium", "lithium.jar")
    plugin, _server, source = _bulk_env(tmp_path, monkeypatch, [first, second])

    class _Outcome:
        def __init__(self, file_name, name, status):
            self.file_name, self.name, self.status = file_name, name, status
            self.path = "config/mod_update_checker/downloads/" + file_name
            self.detail = ""
            self.bytes_written = 0

    def stand_in(entries):
        # Every candidate arrives at the downloader — a batch that quietly dropped one is the
        # failure this test exists to catch, and it would be invisible from the summary alone.
        assert {entry.file_name for entry in entries} == {"lithium.jar", "sodium.jar"}
        return [
            _Outcome("sodium.jar", "Sodium", STATUS_DOWNLOADED),
            _Outcome("lithium.jar", "Lithium", STATUS_FAILED),
        ]

    monkeypatch.setattr(plugin, "_perform_manual_downloads", stand_in, raising=False)

    plugin._manual_download(source, "all", "!!muc")
    plugin._manual_confirm(source, "!!muc")

    # The fetch runs on its own thread; wait for it rather than sleeping a fixed time.
    for _ in range(200):
        if "批量下载结束" in source.body:
            break
        time.sleep(0.02)

    assert "1 个已下载" in source.body, source.body
    assert "失败的是：Lithium" in source.body, source.body
    assert "!!muc install all" in source.body, source.body
    assert plugin._pending_action is None, "the plan was carried out, so it is spent"


def test_all_is_the_bulk_word_even_when_a_mod_answers_to_it(tmp_path, monkeypatch):
    """``all`` is reserved, and being reserved is the point.

    It has to mean the list even on a server that ships a mod whose name, id and file stem are
    all spelled that way — a bulk command that silently narrowed to one mod would be the worst
    kind of surprise. Such a mod stays reachable by the number the listing shows it under.
    """
    tricky = _bulk_entry("allmod", "All", "all.jar")
    other = _bulk_entry("sodium", "Sodium", "sodium.jar")
    plugin, _server, source = _bulk_env(tmp_path, monkeypatch, [tricky, other])

    plugin._manual_download(source, "all", "!!muc")

    assert plugin._pending_action["kind"] == "download_all"
    assert plugin._pending_action["files"] == ["all.jar", "sodium.jar"]

    # And the same word on ``install``, on a report where it is the only candidate — which is
    # the case where a name lookup would also have "worked" and meant something else.
    plugin._clear_pending()
    plugin._last_report = _report_with(
        [_bulk_entry("allmod", "All", "all.jar", status="awaiting_install")]
    )
    plugin._manual_install(source, "all", "!!muc")

    assert plugin._pending_action["kind"] == "install_all"


def test_a_batch_confirmation_is_dropped_when_the_set_of_mods_changed(tmp_path, monkeypatch):
    """Same rule as the single form, one level up — because "these mods" is what was approved.

    Running whatever overlap is left would report success over a batch that partly did not
    happen, and the admin would have no way to know which half.
    """
    first = _bulk_entry("sodium", "Sodium", "sodium.jar")
    second = _bulk_entry("lithium", "Lithium", "lithium.jar")
    plugin, _server, source = _bulk_env(tmp_path, monkeypatch, [first, second])
    monkeypatch.setattr(
        plugin, "_perform_manual_downloads",
        lambda items: pytest.fail("a stale batch was carried out"), raising=False,
    )

    plugin._manual_download(source, "all", "!!muc")
    # A re-run finished underneath: one of the two is no longer waiting to be fetched.
    plugin._last_report = _report_with([first])

    plugin._manual_confirm(source, "!!muc")

    assert "作废" in source.body, source.body
    assert "!!muc download all" in source.body, source.body
    assert plugin._pending_action is None


def test_install_all_authorises_every_downloaded_build(tmp_path, monkeypatch):
    """One command for the lot, and one ledger record at a time underneath.

    The records are the point: the ledger is also the work list the next stop reads, so the
    batch must approve exactly the builds it named — and nothing else that happens to be in it.
    """
    from mod_update_checker.downloads import DownloadLedger

    waiting = [
        _bulk_entry("sodium", "Sodium", "sodium.jar", status="awaiting_install"),
        _bulk_entry("lithium", "Lithium", "lithium.jar", status="awaiting_install"),
        _bulk_entry("iris", "Iris", "iris.jar", status="awaiting_install"),
    ]
    plugin, server, source = _bulk_env(tmp_path, monkeypatch, waiting)

    ledger = DownloadLedger(
        Path(server.get_data_folder()) / plugin.DOWNLOAD_LEDGER_FILE_NAME
    )
    for entry in waiting:
        ledger.record(entry.mod_id, entry.download_filename, "b" * 40, "1.1.0",
                      "2026-01-01T00:00:00+00:00", installed_file=entry.file_name,
                      name=entry.name)
    ledger.save()

    plugin._manual_install(source, "all", "!!muc")

    assert plugin._pending_action["kind"] == "install_all"
    assert plugin._pending_action["files"] == ["iris.jar", "lithium.jar", "sodium.jar"]
    assert "即将安排安装 3 个" in source.body, source.body

    plugin._manual_confirm(source, "!!muc")

    assert "已授权 3 个 Mod" in source.body, source.body
    # Read back from disk: the command writes its own ledger, because by the time an admin
    # confirms, the plugin may have been reloaded and the record is what is on disk.
    written = DownloadLedger(
        Path(server.get_data_folder()) / plugin.DOWNLOAD_LEDGER_FILE_NAME
    )
    assert written.approved_keys() == ["iris", "lithium", "sodium"]


def test_install_all_with_nothing_waiting_says_so(tmp_path, monkeypatch):
    """Not an error, and not silence either: there is simply nothing at the install step yet."""
    plugin, _server, source = _bulk_env(
        tmp_path, monkeypatch, [_bulk_entry("sodium", "Sodium", "sodium.jar")]
    )

    plugin._manual_install(source, "all", "!!muc")

    assert "没有已下载、等待安装的 Mod" in source.body, source.body
    assert plugin._pending_action is None


# --------------------------------------------------------------------------------------
# The completion notice
#
# Two readers, one event: the console is where an admin who stepped away during the transfer
# catches up, and the game is where an admin who is *there* finds out the files arrived. The
# in-game gate is deliberately its own and these tests pin it in both directions — see
# ``_announce_download_complete`` for why it is not ``report.in_game``.
# --------------------------------------------------------------------------------------


def _check_with_fetched_files(tmp_path, monkeypatch, count, size, broadcast=True):
    """Drive one whole check whose download pass reports ``count`` files of ``size`` bytes.

    Only the reconcile step is stubbed; the announcement, the summary and the in-game delivery
    are the real code paths. The reconcile step is replaced rather than exercised because what
    is under test here is the notice, and staging real transfers to produce it would make the
    test a worse copy of ``test_e2e``.
    """
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path, levels={"Admin": 4, "Player": 0})
    plugin._apply_language(server, _config_with({"language": "zh_cn"}))
    monkeypatch.setattr(plugin, "_online_players", {"Admin", "Player"}, raising=False)
    monkeypatch.setattr(
        plugin, "_reconcile_downloads",
        lambda server, report, config: (count, size), raising=False,
    )

    _run_with_stub(
        plugin, server, tmp_path, monkeypatch,
        _report(updates=0, mods_directory=str(Path(tmp_path) / "mods")),
        broadcast=broadcast, language="zh_cn",
    )
    return server


def test_a_finished_download_is_announced_to_the_console_and_to_admins(tmp_path, monkeypatch):
    """One line in the console, and the same news in game — addressed to who can act on it.

    Not gated on ``report.in_game``, which is off here as shipped: that switch is about "an
    update exists" announcements, which stay true until acted on and are repeated at the next
    login. This is this server's own files having changed a moment ago. The permission gate
    still applies: a player who cannot run the command is not told.
    """
    server = _check_with_fetched_files(tmp_path, monkeypatch, count=2, size=5 * 1024 * 1024)

    console = "\n".join(server.logger.messages)
    assert "下载完成" in console, console
    assert "2 个" in console and "5.0 MB" in console, console
    assert "!!modupdate install all" in console, "the console line must name the next command"

    told = "\n".join(server.told("Admin"))
    assert "下载完成" in told, told
    assert server.told("Player") == [], "a player who cannot act on the notice was told anyway"


def test_a_check_that_fetched_nothing_says_nothing(tmp_path, monkeypatch):
    """A completion line for a non-event is how a notice stops being read.

    The summary already describes where things stand; "download finished" is only true when
    something actually finished.
    """
    server = _check_with_fetched_files(tmp_path, monkeypatch, count=0, size=0)

    assert "下载完成" not in "\n".join(server.logger.messages)
    assert server.told() == []


def test_a_join_check_keeps_the_completion_on_the_console_only(tmp_path, monkeypatch):
    """The join path runs with ``broadcast=False``.

    Its admin is being answered personally a moment later, and re-broadcasting at that instant
    is how one event becomes two messages for everyone else. The console still records it,
    because the console is where the history is.
    """
    server = _check_with_fetched_files(tmp_path, monkeypatch, count=1, size=2048,
                                       broadcast=False)

    assert "下载完成" in "\n".join(server.logger.messages)
    assert server.told() == []


def test_install_on_stop_installs_only_what_was_authorised(tmp_path, monkeypatch):
    """With the automatic half off, the ledger is not a work list — the approvals are.

    This is the invariant the per-record flag exists for. Installing the whole ledger here
    would turn "install this one" into "install everything that happens to be downloaded".
    """
    from mod_update_checker.downloads import DownloadLedger

    plugin_mod, server, _source, entry, ledger = _manual_env(
        tmp_path, monkeypatch, status="awaiting_install"
    )
    monkeypatch.setattr(plugin_mod, "_config", _config_with(), raising=False)

    # Two real jars: the installed one, and the fetched replacement.
    mods = Path(tmp_path) / "mods"
    downloads = Path(server.get_data_folder()) / "downloads"
    mods.mkdir(parents=True, exist_ok=True)
    downloads.mkdir(parents=True, exist_ok=True)
    (mods / "sodium.jar").write_bytes(b"old")
    blob = b"new-build"
    (downloads / "sodium-fabric-1.1.0.jar").write_bytes(blob)
    (mods / "other.jar").write_bytes(b"other-old")
    (downloads / "other-2.0.jar").write_bytes(b"other-new")

    fresh = DownloadLedger(
        Path(server.get_data_folder()) / plugin_mod.DOWNLOAD_LEDGER_FILE_NAME
    )
    fresh.record("sodium", "sodium-fabric-1.1.0.jar", hashlib.sha1(blob).hexdigest(), "1.1.0",
                 "2026-01-01T00:00:00+00:00", installed_file="sodium.jar", name="Sodium")
    fresh.record("other", "other-2.0.jar", hashlib.sha1(b"other-new").hexdigest(), "2.0",
                 "2026-01-01T00:00:00+00:00", installed_file="other.jar", name="Other")
    fresh.approve("sodium")
    fresh.save()

    plugin_mod._install_on_stop(server)

    assert (mods / "sodium-fabric-1.1.0.jar").is_file(), "the authorised mod was not installed"
    assert (mods / "sodium.jar.old").is_file(), "the old jar was not kept"
    assert (mods / "other.jar").is_file() and not (mods / "other.jar.old").exists(), (
        "an unauthorised mod was installed"
    )
    assert (mods / "other-2.0.jar").exists() is False


def test_install_on_stop_does_nothing_at_all_without_an_approval(tmp_path, monkeypatch):
    """A server that never used the command must not grow an install report."""

    plugin_mod, server, _source, _entry, _ledger = _manual_env(tmp_path, monkeypatch)
    monkeypatch.setattr(plugin_mod, "_config", _config_with(), raising=False)

    mods = Path(tmp_path) / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    (mods / "sodium.jar").write_bytes(b"old")

    plugin_mod._install_on_stop(server)

    assert not (Path(server.get_data_folder()) / plugin_mod.INSTALL_REPORT_FILE_NAME).exists()
    assert (mods / "sodium.jar.old").exists() is False


def test_the_automatic_setting_installs_the_whole_ledger(tmp_path, monkeypatch):
    """Switching install-on-stop on is itself the instruction, so the flag is not consulted."""
    from mod_update_checker.installer import pending_records

    plugin_mod, _server, _source, _entry, ledger = _manual_env(tmp_path, monkeypatch)
    ledger.record("other", "other-2.0.jar", "b" * 40, "2.0", "2026-01-01T00:00:00+00:00",
                  installed_file="other.jar", name="Other")
    ledger.save()

    assert pending_records(ledger, approved_only=False) == ["other", "sodium"]
    assert pending_records(ledger, approved_only=True) == []


# --------------------------------------------------------------------------------------
# Whether a report may be reused at all
#
# The age window used to be the only question, which was enough while the report only ever
# lived in memory. Now that ``last_report.json`` is read back after a restart, "produced
# recently" and "still describes this server" became different questions — and answering only
# the first one means an admin who just swapped their mods folder, changed loader, or upgraded
# Minecraft is shown the previous server's answer as though it were current.
# --------------------------------------------------------------------------------------


def _scan_of(tmp_path, *jar_names):
    from mod_update_checker.scanner import scan_mods

    mods = Path(tmp_path) / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    for name in jar_names:
        (mods / name).write_bytes(b"not read; only listed")
    return scan_mods(mods)


def _run_with_stub(plugin, server, tmp_path, monkeypatch, report, broadcast=True, **config):
    """Drive ``_run_check`` with ``_scan_current`` and ``Checker`` stubbed out."""
    from mod_update_checker.serverinfo import ServerContext

    recorded = {}

    class _RecordingChecker:
        def __init__(self, options, logger=None):
            recorded["options"] = options

        def run(self, scan, context, cache_path=None, map_path=None):
            recorded["map_path"] = map_path
            report.server = context
            report.mods_directory = scan.directory
            return report

        def close(self):
            pass

    monkeypatch.setattr(plugin, "Checker", _RecordingChecker, raising=False)
    monkeypatch.setattr(
        plugin,
        "_scan_current",
        lambda *_a, **_k: (
            _scan_of(tmp_path),
            ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        ),
        raising=False,
    )
    settings = {"report.write_file": False}
    settings.update(config)
    monkeypatch.setattr(plugin, "_config", _config_with(settings), raising=False)
    plugin._stop_event.clear()
    plugin._last_report = None

    plugin._run_check(server, broadcast=broadcast)
    plugin._last_report = None
    return recorded


def test_a_report_for_a_different_mods_folder_is_not_reused(entry_env, tmp_path):
    """Pointing ``server.mods_directory`` somewhere else makes the stored answer wrong."""
    plugin, server, calls = entry_env
    plugin._last_report = _report(updates=1, age_seconds=60, mods_directory="D:/somewhere/else")

    plugin._admin_join_worker(server, "Admin")

    assert calls, "the stored report described another folder and should not have been reused"


def test_a_report_for_a_different_pinned_game_version_is_not_reused(entry_env, tmp_path,
                                                                  monkeypatch):
    """Upgrading Minecraft is exactly when a stale answer is most dangerous.

    With ``mc_version: auto`` there is nothing cheap to compare against, so this asserts the
    case the admin can control — and the README already tells them to pin it after a major
    upgrade.
    """
    plugin, server, calls = entry_env
    monkeypatch.setattr(
        plugin, "_config",
        _config_with({"report.reuse_report_minutes": 30, "server.mc_version": "1.22"}),
        raising=False,
    )
    plugin._last_report = _report(updates=1, age_seconds=60)

    plugin._admin_join_worker(server, "Admin")

    assert calls, "the stored report was for another game version"


def test_a_report_from_another_loader_is_not_reused(entry_env, monkeypatch):
    from mod_update_checker.serverinfo import ServerContext

    plugin, server, calls = entry_env
    monkeypatch.setattr(
        plugin, "_config",
        _config_with({"report.reuse_report_minutes": 30, "server.loader": "neoforge"}),
        raising=False,
    )
    stale = _report(updates=1, age_seconds=60)
    stale.server = ServerContext(mc_version="26.3", loader="fabric")
    plugin._last_report = stale

    plugin._admin_join_worker(server, "Admin")

    assert calls, "the stored report was produced for a different loader"


def test_a_report_that_still_applies_is_reused(entry_env):
    """The other direction, so the gate cannot pass by refusing everything."""
    plugin, server, calls = entry_env
    plugin._last_report = _report(updates=1, age_seconds=60)

    plugin._admin_join_worker(server, "Admin")

    assert calls == []


# --------------------------------------------------------------------------------------
# last_report.json being read back
# --------------------------------------------------------------------------------------


def _store_report(server, **overrides):
    from mod_update_checker.report import Report

    values = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "mods_directory": _MODS_DIRECTORY,
    }
    values.update(overrides)
    report = overrides.get("report")
    if report is None:
        from mod_update_checker.serverinfo import ServerContext

        report = Report(
            generated_at=values["generated_at"],
            server=ServerContext(mc_version="26.3", mc_version_source="config",
                                 loader="fabric"),
            mods_directory=values["mods_directory"],
        )
    Path(server.get_data_folder(), "last_report.json").write_text(
        report.to_json(), encoding="utf-8"
    )
    return report


def test_a_stored_report_is_read_back_at_load(tmp_path, monkeypatch):
    """Otherwise every restart throws the reuse window away and re-asks Modrinth."""
    import mod_update_checker as plugin
    from mod_update_checker.report import Report

    server = _FakeServer(tmp_path)
    _store_report(server)
    monkeypatch.setattr(plugin, "_last_report", None, raising=False)
    plugin._stop_event.clear()

    plugin.on_load(server, None)

    assert isinstance(plugin._last_report, Report)
    assert plugin._last_report.mods_directory == _MODS_DIRECTORY
    plugin._last_report = None
    plugin._stop_scheduler()


def test_a_stored_report_is_not_read_when_report_files_are_off(tmp_path, monkeypatch):
    """``write_file: false`` means "do not persist results", so nothing is read back either."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    _store_report(server)
    plugin._stop_event.clear()
    monkeypatch.setattr(plugin, "_last_report", None, raising=False)
    monkeypatch.setattr(
        plugin, "_config",
        _config_with({"report.write_file": False}),
        raising=False,
    )

    assert plugin._load_previous_report(server, plugin._config) is None

    plugin._stop_scheduler()


def test_an_unreadable_stored_report_is_ignored_rather_than_raising(tmp_path):
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    Path(server.get_data_folder(), "last_report.json").write_text("{not json", encoding="utf-8")

    assert plugin._load_previous_report(server, _config_with()) is None


def test_a_live_report_from_a_reload_is_kept_over_the_one_on_disk(tmp_path, monkeypatch):
    """A reload carries the in-memory report, which is by definition at least as fresh."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    _store_report(server, generated_at="2020-01-01T00:00:00+00:00")
    live = _report(updates=1)
    plugin._stop_event.clear()
    monkeypatch.setattr(plugin, "_last_report", live, raising=False)

    plugin.on_load(server, None)

    assert plugin._last_report is live
    plugin._last_report = None
    plugin._stop_scheduler()


def test_the_configured_map_file_reaches_the_checker(tmp_path, monkeypatch):
    """The map is only useful if the path assembled in ``_run_check`` arrives intact."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    recorded = _run_with_stub(
        plugin, server, tmp_path, monkeypatch,
        _report(updates=0, mods_directory=str(Path(tmp_path) / "mods")),
    )

    assert recorded["map_path"] == Path(server.get_data_folder()) / "project-map.json"


def test_a_map_setting_that_is_a_path_is_rejected_and_warned_about(tmp_path, monkeypatch):
    """It has to be a file name, and a silent refusal would look like a broken feature."""
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    recorded = _run_with_stub(
        plugin, server, tmp_path, monkeypatch,
        _report(updates=0, mods_directory=str(Path(tmp_path) / "mods")),
        **{"sources.manual_map": "../outside.json"},
    )

    assert recorded["map_path"] is None
    assert any("manual_map" in message for message in server.logger.messages), (
        server.logger.messages
    )


def test_an_empty_map_setting_switches_the_feature_off_without_a_warning(tmp_path, monkeypatch):
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    recorded = _run_with_stub(
        plugin, server, tmp_path, monkeypatch,
        _report(updates=0, mods_directory=str(Path(tmp_path) / "mods")),
        **{"sources.manual_map": ""},
    )

    assert recorded["map_path"] is None
    assert not any("manual_map" in message for message in server.logger.messages)


# --------------------------------------------------------------------------------------
# The status page's one line about the map
#
# The file is silent by design, so this line is the only way to answer "I wrote that file and
# nothing happened" without reading the config and guessing.
# --------------------------------------------------------------------------------------


def _status_with_map(tmp_path, monkeypatch, map_payload=None, setting=None):
    import mod_update_checker as plugin

    server = _FakeServer(tmp_path)
    if map_payload is not None:
        Path(server.get_data_folder(), "project-map.json").write_text(
            map_payload, encoding="utf-8"
        )

    overrides = {"language": "zh_cn"}
    if setting is not None:
        overrides["sources.manual_map"] = setting
    config = _config_with(overrides)

    class _Scan:
        directory = "server/mods"
        mods = []
        disabled = []

    previous = (plugin._config, plugin._scan_current, plugin._server)
    try:
        plugin._config = config
        plugin._apply_language(server, config)
        plugin._scan_current = lambda *_a, **_k: (
            _Scan(),
            __import__(
                "mod_update_checker.serverinfo", fromlist=["ServerContext"]
            ).ServerContext(mc_version="26.3", mc_version_source="config", loader="fabric"),
        )
        plugin._server = server
        source = _ReplyRecorder()
        plugin._show_status(source)
    finally:
        plugin._config, plugin._scan_current, plugin._server = previous
    return "\n".join(str(item) for item in source.replies)


def test_the_status_page_says_the_map_is_off(tmp_path, monkeypatch):
    rendered = _status_with_map(tmp_path, monkeypatch, setting="")

    assert "本地映射表" in rendered and "关" in rendered


def test_the_status_page_counts_what_is_in_the_map(tmp_path, monkeypatch):
    import json

    payload = json.dumps(
        {"version": 1, "by_sha1": {"a" * 40: "sodium"}, "by_mod_id": {"mycustommod": "lithium"}}
    )
    rendered = _status_with_map(tmp_path, monkeypatch, map_payload=payload)

    assert "project-map.json" in rendered
    assert "1 条按哈希" in rendered and "1 条按 mod id" in rendered


def test_the_status_page_says_when_the_map_file_is_missing_pieces(tmp_path, monkeypatch):
    """The reason is carried through, because "cannot read it" alone is not actionable."""
    rendered = _status_with_map(tmp_path, monkeypatch, map_payload='{"by_sha1": []}')

    assert "无法读取" in rendered
    assert "by_sha1" in rendered


def test_the_status_page_says_when_the_map_name_is_not_a_file_name(tmp_path, monkeypatch):
    rendered = _status_with_map(tmp_path, monkeypatch, setting="sub/dir.json")

    assert "已忽略" in rendered and "sub/dir.json" in rendered


def test_the_status_page_says_which_config_file_it_read(tmp_path, monkeypatch):
    """The page is where "which file is this server actually using" gets answered.

    The question reads as trivial until a file has been edited, upgraded and reinstalled
    without ever changing — at which point the first useful fact is the path the plugin
    itself resolved, on the server that is running.
    """
    rendered = _status_with_map(tmp_path, monkeypatch, setting="")

    expected = str(Path(tmp_path) / "config" / "mod_update_checker" / "config.json")
    assert "配置文件" in rendered, rendered
    assert expected in rendered, rendered


def test_the_status_screen_does_not_hash_the_mods_folder(tmp_path, monkeypatch):
    """``!!muc status`` shows a count, a directory and a version — none of which is a SHA-1.

    And it runs on MCDR's command thread, so hashing every jar means the server waits. On a
    modpack of a few hundred megabytes that is about a second of stall for a screen that only
    needed the file names.

    Asserted by counting calls to the digest function rather than by timing anything, and
    rather than by making it raise: ``scan_mods`` deliberately swallows a per-jar exception and
    records that jar as unreadable, so a poisoning function would leave the jar count intact
    and this test passing for the wrong reason.
    """
    import mod_update_checker as plugin
    import mod_update_checker.scanner as scanner

    from support import fabric_metadata, write_jar

    mods = Path(tmp_path) / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    write_jar(mods / "example.jar", fabric=fabric_metadata(id="example", version="1.0.0"))

    read_calls = []

    def spy(handle):
        read_calls.append(handle)
        return "", 0

    monkeypatch.setattr(scanner, "digests_of_file", spy, raising=False)

    server = _FakeServer(tmp_path)
    previous = (plugin._config, plugin._server)
    try:
        plugin._config = _config_with({"language": "zh_cn"})
        plugin._apply_language(server, plugin._config)
        plugin._server = server
        source = _ReplyRecorder()
        plugin._show_status(source)
    finally:
        plugin._config, plugin._server = previous

    rendered = "\n".join(str(item) for item in source.replies)
    assert read_calls == [], "!!muc status read jar bytes"
    assert "1 个 jar" in rendered, rendered


def test_the_status_screen_still_reads_the_metadata(tmp_path, monkeypatch):
    """The other half: skipping the hashing must not turn into skipping the folder.

    Without this, "does not hash" could be satisfied by a status page that reports zero jars.
    """
    import mod_update_checker as plugin

    from support import fabric_metadata, write_jar

    mods = Path(tmp_path) / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    for index in range(3):
        write_jar(mods / "mod{}.jar".format(index),
                  fabric=fabric_metadata(id="mod{}".format(index)))

    server = _FakeServer(tmp_path)
    previous = (plugin._config, plugin._server)
    try:
        plugin._config = _config_with({"language": "zh_cn"})
        plugin._apply_language(server, plugin._config)
        plugin._server = server
        source = _ReplyRecorder()
        plugin._show_status(source)
    finally:
        plugin._config, plugin._server = previous

    assert "3 个 jar" in "\n".join(str(item) for item in source.replies)


# --------------------------------------------------------------------------------------
# Is the config file complete?
#
# The question came from a user report that ``download.install_on_stop`` was missing from
# their file. It turned out not to be reproducible — MCDR fills a missing field in from the
# class default and rewrites the file, on every version this plugin supports, which is
# asserted below — but asking it turned up no test that the shipped defaults *can* produce a
# complete file at all. A field MCDR could not write would be invisible: the plugin would use
# its class default and the admin would simply never see the option.
# --------------------------------------------------------------------------------------


def _serialised_leaf_paths(node, prefix=""):
    """Every scalar in a serialised config, as a dotted path."""
    leaves = []
    for key, value in node.items():
        path = "{}.{}".format(prefix, key) if prefix else key
        if isinstance(value, dict):
            leaves.extend(_serialised_leaf_paths(value, path))
        else:
            leaves.append(path)
    return leaves


def test_every_option_reaches_the_config_file():
    """``Config``'s fields and the file MCDR writes must be the same set, both ways.

    One direction catches an option that exists but never gets written — an admin would only
    ever see the documented default and could not change it. The other catches a written key
    with no field behind it, which ``deserialize`` reports as redundant and discards.
    """
    import mod_update_checker as plugin

    from support import option_paths

    fields = set(option_paths(plugin.Config))
    written = set(_serialised_leaf_paths(plugin.Config.get_default().serialize()))

    assert written - fields == set(), "written but not an option"
    assert fields - written == set(), "an option that never reaches the file"


def test_a_config_file_missing_an_option_is_filled_in_and_rewritten(tmp_path):
    """Why "my file does not have that option" self-heals, and the reason it is worth knowing.

    MCDR hands ``deserialize`` a callback for missing fields; any miss marks the result
    imperfect and the file is saved again straight away. So an option added in a later release
    appears in an upgraded server's file on the next plugin load, keeping whatever the admin
    had already set.

    Simulated with MCDR's own ``SimpleConfigHandler`` and ``deserialize`` rather than by booting
    a server, because the behaviour under test is MCDR's and this is exactly the code path
    ``load_config_simple`` takes.
    """
    from mcdreforged.plugin.si._simple_config_handler import SimpleConfigHandler

    import mod_update_checker as plugin

    handler = SimpleConfigHandler("config.json", None, str(tmp_path))
    path = tmp_path / "config.json"

    stale = plugin.Config.get_default().serialize()
    del stale["download"]["install_on_stop"]      # as if written before that option existed
    del stale["sources"]["manual_map"]
    stale["download"]["folder_name"] = "jars"     # something the admin changed by hand
    handler.save(stale, encoding="utf8")

    state = {"imperfect": False}

    def note_missing(*_args):
        state["imperfect"] = True

    config = plugin.Config.deserialize(
        handler.load(encoding="utf8"),
        missing_callback=note_missing,
        redundancy_callback=note_missing,
    )
    assert state["imperfect"], "MCDR would not have noticed the file was incomplete"
    handler.save(config.serialize(), encoding="utf8")

    rewritten = json.loads(path.read_text(encoding="utf-8"))
    assert rewritten["download"]["install_on_stop"] is False
    assert rewritten["sources"]["manual_map"] == "project-map.json"
    assert rewritten["download"]["folder_name"] == "jars", "the admin's own value was lost"


# --------------------------------------------------------------------------------------
# !!muc delete / !!muc cleanup —— 旧版备份
#
# 这是插件里唯一会从 mods/ 里删东西的两条路径，所以断言的重点全是「什么不许发生」：
# 不是备份的东西不许删，集合变了不许按旧计划删，开关关着不许主动开口。
# --------------------------------------------------------------------------------------


def _cleanup_setup(tmp_path, monkeypatch, *, allow_delete=True, enabled=True, max_age_days=30,
                   backups=(), entries=()):
    """The plugin wired to a mods folder holding the given backups.

    ``allow_delete`` defaults to *on* here, unlike the shipped default: these tests are about
    what the commands do, and the switch that lets them run is either the thing under test (the
    locked tests below pass ``allow_delete=False``) or an uninteresting precondition.
    """
    import os
    import time

    import mod_update_checker as plugin
    from mod_update_checker.report import UpdateEntry

    mods = tmp_path / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for name, size, age_days in backups:
        path = mods / name
        path.write_bytes(b"x" * size)
        stamp = now - age_days * 86400
        os.utime(str(path), (stamp, stamp))

    def entry(file_name, status, **kwargs):
        return UpdateEntry(mod_id=file_name, name=file_name, file_name=file_name,
                           status=status, **kwargs)

    holders = list(entries) + [entry(name, "old_backup", size_bytes=size, age_days=age_days)
                               for name, size, age_days in backups]
    server = _FakeServer(tmp_path)
    monkeypatch.setattr(plugin, "_server", server, raising=False)
    monkeypatch.setattr(plugin, "_last_report", _report_with(holders), raising=False)
    monkeypatch.setattr(
        plugin, "_config",
        _config_with({"language": "zh_cn", "cleanup.allow_delete": allow_delete,
                      "cleanup.enabled": enabled, "cleanup.max_age_days": max_age_days}),
        raising=False,
    )
    monkeypatch.setattr(plugin, "_mods_folder", lambda *_a, **_k: mods, raising=False)
    plugin._clear_pending()
    plugin._apply_language(server, plugin._config)
    return plugin, server, mods


def test_the_default_is_off_and_nothing_is_said(tmp_path, monkeypatch):
    """默认关闭：备份照样在报告里，但插件不会自己开口。"""
    plugin, server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, enabled=False, backups=[("ancient.jar.old", 10, 400)],
    )

    plugin._announce_cleanup(server)

    assert "旧版备份" not in "\n".join(server.logger.messages)
    assert plugin._pending_action is None, "关着开关却摆好了计划"


def test_an_expired_backup_is_announced_and_the_plan_is_ready(tmp_path, monkeypatch):
    """打开开关后，提醒里那句「输入 confirm 删除」必须是真的。

    这是用户描述的那条路：提醒 → 同意 → ``!!muc confirm``。所以提醒必须**顺手把计划摆好**，
    否则那句话只有在前 120 秒里成立，而管理员可能是五分钟后才读到控制台的。
    """
    plugin, server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("ancient.jar.old", 2048, 400)],
    )

    plugin._announce_cleanup(server)

    logged = "\n".join(server.logger.messages)
    assert "已超过 30 天" in logged
    assert "confirm" in logged
    assert plugin._pending_action is not None
    assert plugin._pending_action["files"] == ["ancient.jar.old"]
    # 空 requester = 读到这条的任一管理员都能确认，因为这条提醒是对全服管理员说的。
    assert plugin._pending_action["requester"] == ""


def test_a_fresh_backup_is_left_alone(tmp_path, monkeypatch):
    plugin, server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("fresh.jar.old", 10, 3)],
    )

    plugin._announce_cleanup(server)

    assert "旧版备份" not in "\n".join(server.logger.messages)
    assert plugin._pending_action is None


def test_a_reminder_never_clobbers_a_plan_that_is_already_staged(tmp_path, monkeypatch):
    """提醒不是人的指令，它不能顶掉管理员正在确认的那一条。

    把「install 这三个」换成「删掉那两个」，比不提醒糟得多。
    """
    plugin, server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("ancient.jar.old", 10, 400)],
    )
    plugin._stage_batch("install_all", _PlayerSource("Admin"), ["a.jar"],
                        ["计划已列好"])

    plugin._announce_cleanup(server)

    assert plugin._pending_action["kind"] == "install_all"
    assert plugin._pending_action["files"] == ["a.jar"]
    logged = "\n".join(server.logger.messages)
    assert "cleanup" in logged, "顶不掉却又不说怎么办"


def test_deleting_one_backup_takes_two_steps_and_really_deletes_it(tmp_path, monkeypatch):
    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("ancient.jar.old", 512, 400)],
    )
    source = _PlayerSource("Admin")

    plugin._manual_delete(source, "ancient.jar.old", "!!muc")
    assert (mods / "ancient.jar.old").is_file(), "还没确认就删了"
    assert "即将删除" in source.body
    assert plugin._pending_action["kind"] == "delete"

    plugin._manual_confirm(source, "!!muc")

    assert not (mods / "ancient.jar.old").exists()
    assert "已删除" in source.body


def test_a_number_that_points_at_a_mod_is_refused(tmp_path, monkeypatch):
    """编号是真的、文件是真的，但它不是插件留下的备份——那就一个字节都不许动。"""
    from mod_update_checker.report import UpdateEntry

    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("ancient.jar.old", 512, 400)],
        entries=[UpdateEntry(mod_id="sodium", name="Sodium", file_name="sodium.jar",
                             status="up_to_date")],
    )
    (mods / "sodium.jar").write_bytes(b"a live mod")
    source = _PlayerSource("Admin")

    plugin._manual_delete(source, "sodium.jar", "!!muc")

    assert "不会删除" in source.body
    assert plugin._pending_action is None, "拒绝之后不该留下待确认的东西"
    assert (mods / "sodium.jar").is_file()


def test_cleanup_only_takes_the_expired_ones(tmp_path, monkeypatch):
    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, max_age_days=30,
        backups=[("ancient.jar.old", 128, 400), ("fresh.jar.old", 64, 2)],
    )
    source = _PlayerSource("Admin")

    plugin._manual_cleanup(source, "!!muc")
    assert "即将删除 1 个" in source.body
    assert plugin._pending_action["files"] == ["ancient.jar.old"]

    plugin._manual_confirm(source, "!!muc")

    assert not (mods / "ancient.jar.old").exists()
    assert (mods / "fresh.jar.old").is_file(), "没过期的也被删了"


def test_a_cleanup_is_dropped_whole_when_the_set_changed(tmp_path, monkeypatch):
    """集合变了就整条作废，而不是只删还对得上的那几个。"""
    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("ancient.jar.old", 128, 400)],
    )
    source = _PlayerSource("Admin")
    plugin._manual_cleanup(source, "!!muc")

    # 两条命令之间，mods/ 里多了一个**已过期**的备份。刚创建的文件不算——它没到期限，
    # 集合其实没变，而这条测试要证的正是「集合变了就整条作废」。
    import os
    import time

    added = mods / "another.jar.old"
    added.write_bytes(b"y" * 16)
    stamp = time.time() - 400 * 86400
    os.utime(str(added), (stamp, stamp))

    plugin._manual_confirm(source, "!!muc")

    assert (mods / "ancient.jar.old").is_file()
    assert "作废" in source.body


def test_the_status_screen_names_the_backups(tmp_path, monkeypatch):
    plugin, _server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("ancient.jar.old", 1048576, 400)],
    )
    source = _PlayerSource("Admin")

    plugin._show_status(source)

    body = source.body
    assert "旧版备份" in body
    assert "1.0 MB" in body
    assert "已超过 30 天" in body


def test_the_listing_shows_a_backup_and_can_be_filtered_to_it(tmp_path, monkeypatch):
    plugin, _server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("ancient.jar.old", 2048, 400)],
    )
    source = _PlayerSource("Admin")

    plugin._show_list(source, "", "!!muc")
    assert "ancient.jar.old" in source.body
    # 行上的状态格现在只说 ``[状态: ⏪]``（每一行都一样）；「旧版备份」那句解释搬进了浮窗，
    # 所以正文里不再出现它（浮窗的内容由事实那条测试钉住）。
    assert "[状态: ⏪]" in source.body
    assert "旧版备份" not in source.body

    filtered = _PlayerSource("Admin")
    plugin._show_list(filtered, "old_backup", "!!muc")
    assert "ancient.jar.old" in filtered.body


def test_the_detail_view_offers_the_delete_button(tmp_path, monkeypatch):
    plugin, _server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("ancient.jar.old", 2048, 400)],
    )
    source = _PlayerSource("Admin")
    entry = plugin._last_report.backups[0]

    plugin._reply_detail(source, entry, prefix="!!muc")

    assert "删除此备份" in source.body
    assert "ancient.jar.old" in source.body
    assert "12.4" not in source.body  # 2048 B -> KB，不是乱写的数字


def test_installing_stamps_the_backup_with_the_moment_it_was_made(tmp_path):
    """改名会保留原 jar 的时间戳，所以安装完必须把它刷成「现在」。

    不刷的话，``sodium.jar.old`` 带的是那个 jar 当初放进 mods/ 的日期——可能是一年前——
    而清理功能正是按这个时间戳算年龄的：升级之后第一次运行就会把服务器上**每一个**备份
    都当成早就过期。
    """
    import os
    import time

    from mod_update_checker import installer

    mods = tmp_path / "mods"
    downloads = tmp_path / "downloads"
    mods.mkdir()
    downloads.mkdir()
    old = mods / "sodium.jar"
    old.write_bytes(b"old")
    stamp = time.time() - 500 * 86400
    os.utime(str(old), (stamp, stamp))

    fresh = downloads / "sodium-1.1.0.jar"
    fresh.write_bytes(b"new")
    import hashlib

    record = {
        "file": "sodium-1.1.0.jar",
        "name": "Sodium",
        "local": "sodium.jar",
        "version": "1.1.0",
        "sha1": hashlib.sha1(b"new").hexdigest(),
    }

    result = installer._install_one(record, installer.InstallOptions(mods, downloads))

    assert result.status == installer.STATUS_INSTALLED
    backup = mods / result.backup_file
    assert backup.is_file()
    age_days = (time.time() - backup.stat().st_mtime) / 86400
    assert age_days < 1, "备份的时间戳还是那个旧 jar 的"


# --------------------------------------------------------------------------------------
# cleanup.allow_delete —— 「什么都不许删」那个开关
#
# 这一组测的全是「不许发生什么」：默认配置下两条命令都拒绝、提醒也不出现（提醒的下一步
# 必然被拒，说了就是废话）、连**已经在等确认的计划**在开关被关掉之后也不执行。最后一条
# 是这一组里最重要的：门禁只拦命令、不拦确认的话，那枚计划就绕过了开关。
# --------------------------------------------------------------------------------------


def _clicked_commands(source):
    """Every click target in everything this source was sent, in order.

    ``suggest_command`` and ``run_command`` alike: the question these tests ask is "did the
    reader get something to click that would delete a file", and both spellings answer yes.
    """
    return [
        item["clickEvent"]["value"]
        for reply in source.raw
        for item in _segments(reply)
        if "clickEvent" in item
    ]


def test_the_shipped_default_forbids_deletion():
    """出厂设置里这一项是关的——整个插件的默认姿态是「一个字节都不删」。"""
    import mod_update_checker as plugin

    assert plugin.Config.get_default().cleanup.allow_delete is False


def test_delete_is_refused_while_the_switch_is_off(tmp_path, monkeypatch):
    """关着的时候 ``delete`` 不删东西，而且要说清在哪打开。"""
    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, allow_delete=False, backups=[("ancient.jar.old", 512, 400)],
    )
    source = _PlayerSource("Admin")

    plugin._manual_delete(source, "ancient.jar.old", "!!muc")

    assert (mods / "ancient.jar.old").is_file(), "开关关着却删了文件"
    assert plugin._pending_action is None, "开关关着却摆好了计划"
    body = source.body
    assert "删除功能未开启" in body
    assert "cleanup.allow_delete" in body, "没说清去改哪个选项"
    assert plugin._config_path(plugin._server) in body, "没给出配置文件的路径"


def test_cleanup_is_refused_while_the_switch_is_off(tmp_path, monkeypatch):
    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, allow_delete=False, backups=[("ancient.jar.old", 512, 400)],
    )
    source = _PlayerSource("Admin")

    plugin._manual_cleanup(source, "!!muc")

    assert (mods / "ancient.jar.old").is_file()
    assert plugin._pending_action is None
    assert "cleanup.allow_delete" in source.body


def test_the_reminder_needs_the_delete_switch_as_well(tmp_path, monkeypatch):
    """``enabled`` 开着也没用：``allow_delete`` 关着时提醒不出现。

    这正是用户要求的「前置开关」。提醒的全部意义是「输入 confirm 删除它们」，而那种状态下
    confirm 必然被拒——一句做不到的话会让管理员以为插件坏了。
    """
    plugin, server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, allow_delete=False, enabled=True,
        backups=[("ancient.jar.old", 2048, 400)],
    )

    plugin._announce_cleanup(server)

    assert "旧版备份" not in "\n".join(server.logger.messages)
    assert plugin._pending_action is None


def test_a_plan_staged_before_the_switch_went_off_is_not_carried_out(tmp_path, monkeypatch):
    """开关关掉之后，连**已经在等确认**的计划也不执行。

    这一条问的是一份不变量：无论配置是怎么变的，删除计划的执行都要自己再看一次开关。现实里
    ``!!muc reload`` 会顺手清掉待确认的计划（见 ``_reload_config``），所以这条路径今天多半
    走不到——但那正是它值得有的理由：安全性不该建立在「另一处会顺手清掉」上面。门禁只装在
    命令上、没装在确认上的话，任何一处忘了清、或者将来加了不清的路径，这枚计划就带着一份
    早已作废的授权把文件删了。

    所以这里直接改配置对象（模拟「配置已经不是原来那份了」），而不是走 reload 那条会自己
    清空槽位的路。
    """
    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("ancient.jar.old", 512, 400)],
    )
    source = _PlayerSource("Admin")
    plugin._manual_delete(source, "ancient.jar.old", "!!muc")
    assert plugin._pending_action is not None, "（前提：计划已经摆好）"

    # 配置变了。刻意不调 ``_reload_config``：它自己会清空槽位，那样就测不到这条门禁了。
    plugin._config.cleanup.allow_delete = False

    plugin._manual_confirm(source, "!!muc")

    assert (mods / "ancient.jar.old").is_file(), "开关关掉之后，计划的删除还是执行了"
    assert plugin._pending_action is None, "被拒之后计划还留着"
    assert "cleanup.allow_delete" in source.body


def test_the_detail_view_explains_itself_instead_of_offering_a_locked_button(
    tmp_path, monkeypatch,
):
    """关着的时候详情页不给按钮，给一行说明——点了必然报错的按钮比没有按钮更差。"""
    plugin, _server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, allow_delete=False, backups=[("ancient.jar.old", 2048, 400)],
    )
    source = _PlayerSource("Admin")
    entry = plugin._last_report.backups[0]

    plugin._reply_detail(source, entry, prefix="!!muc")

    assert "cleanup.allow_delete" in source.body
    assert "删除此备份" not in source.body, "开关关着却还给了删除按钮"
    # 这一行说的是「说明文字」，不是「灰掉的按钮」：整条回复里不能有任何点击目标。
    assert not _clicked_commands(source), _clicked_commands(source)


def test_the_status_line_says_why_nothing_is_cleaning_up(tmp_path, monkeypatch):
    """状态屏那一行在「有过期备份 + 删除关着」时要给出原因。"""
    plugin, _server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, allow_delete=False, backups=[("ancient.jar.old", 1048576, 400)],
    )
    source = _PlayerSource("Admin")

    plugin._show_status(source)

    assert "旧版备份" in source.body
    assert "删除功能未开启" in source.body


def test_the_status_line_is_quiet_about_the_switch_when_there_is_nothing_to_delete(
    tmp_path, monkeypatch,
):
    """没有过期备份时不提这个开关——否则它会变成每一屏都在的背景噪音。"""
    plugin, _server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, allow_delete=False, backups=[("fresh.jar.old", 1024, 1)],
    )
    source = _PlayerSource("Admin")

    plugin._show_status(source)

    assert "旧版备份" in source.body
    assert "删除功能未开启" not in source.body


def test_a_cache_file_that_is_not_an_object_cannot_break_the_load(tmp_path, monkeypatch):
    """``resolve-cache.json`` 里装着 ``[]`` 时，插件必须照常加载。

    这是这一轮代码审查揪出来的：``_log_cache_summary`` 直接对解析结果调 ``.get``，而它被
    ``on_load`` 不设防地调用——一份被手工改坏的缓存文件（或者任何把 ``null`` / ``[]`` 写进去
    的东西）会让**插件加载失败**。一行日志没有资格造成这种后果，on_load 里其它每一步都包了
    try/except，只有它没有，因为它假设「这文件是我们自己写的」。它确实是——但插件文件夹里
    的文件不归我们独占。
    """
    import json
    import pathlib

    import mod_update_checker as plugin

    for body in ("[]", "null", '"text"', "{}", '{"version": 1, "records": {}}'):
        root = tmp_path / body.replace('"', "").replace(" ", "")[:8]
        server = _FakeServer(root)
        folder = pathlib.Path(server.get_data_folder())
        folder.mkdir(parents=True, exist_ok=True)
        (folder / plugin.CACHE_FILE_NAME).write_text(body, encoding="utf-8")
        monkeypatch.setattr(plugin, "_server", server, raising=False)
        # 语言在这里自己钉住：单个测试被挑出来跑时，前面没有任何测试设过中文，断言会因语言
        # 残留而失败（这个坑 tests/README 里记着）。
        monkeypatch.setattr(
            plugin, "_config",
            _config_with({"language": "zh_cn", "network.cache.enabled": True}),
            raising=False,
        )
        plugin._apply_language(server, plugin._config)

        plugin._log_cache_summary(server, plugin._config)   # 不许抛

    # 而且好的那份仍然会被念出来——把崩溃挡掉不等于把功能关掉。
    server = _FakeServer(tmp_path / "good")
    folder = pathlib.Path(server.get_data_folder())
    folder.mkdir(parents=True, exist_ok=True)
    (folder / plugin.CACHE_FILE_NAME).write_text(
        json.dumps({"version": 1, "records": {"a" * 40: {"mod_id": "sodium"}}}),
        encoding="utf-8",
    )
    plugin._log_cache_summary(server, plugin._config)

    logged = "\n".join(server.logger.messages)
    assert "识别缓存：1 条记录" in logged, logged


def test_cleanup_says_how_to_proceed_when_nothing_is_expired_yet(tmp_path, monkeypatch):
    """默认阈值下第一次敲 ``cleanup`` 必然是这个结果（备份都是新的），所以它必须给出下一步。

    这条来自用户的真实一问：「我输入 !!muc cleanup 之后为何没有删除旧的文件？」——他的六个备份
    21–22 天，阈值 30 天。回答「没有超过 30 天的」是对的，但停在那里等于把「阈值是个可以改的
    数字」和「点名删除不看天数」两件事留给读者自己发现。
    """
    plugin, _server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, max_age_days=30, backups=[("fresh.jar.old", 2048, 21)],
    )
    source = _PlayerSource("Admin")

    plugin._manual_cleanup(source, "!!muc")

    body = source.body
    assert "没有超过 30 天的旧版备份(当前共 1 个，最老的 21 天)" in body, body
    assert "delete" in body and "max_age_days" in body, body
    assert plugin._pending_action is None, "没东西可删却摆了计划"


def test_cleanup_on_a_folder_with_no_backups_at_all_says_so(tmp_path, monkeypatch):
    """一个备份都没有时，说「共 0 个，最老的 ? 天」是废话——那句话只为「有备份但都没到期」写。"""
    plugin, _server, _mods = _cleanup_setup(tmp_path, monkeypatch, backups=[])
    source = _PlayerSource("Admin")

    plugin._manual_cleanup(source, "!!muc")

    body = source.body
    assert "没有旧版备份" in body, body
    assert "最老的" not in body and "max_age_days" not in body, body


def test_delete_all_covers_the_backups_that_are_not_expired_yet(tmp_path, monkeypatch):
    """``delete all`` 与 ``cleanup`` 只差一件事：不看天数。

    这是用户点名要的（`download all` / `install all` 的对称形式）。动机很实际：堆在 mods/ 里的
    备份**大多数都还没到期**，而「清理」这件事的出发点通常就是「把这一堆清掉」——``cleanup``
    在这种时候只会回答「没有超过 N 天的」，一句都不删。
    """
    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, max_age_days=30,
        backups=[("fresh.jar.old", 2048, 21), ("ancient.jar.old", 1024, 400)],
    )

    # 前提：同样的两个文件，cleanup 的计划里**只有**过期的那一个。
    first = _PlayerSource("Admin")
    plugin._manual_cleanup(first, "!!muc")
    assert plugin._pending_action["files"] == ["ancient.jar.old"], "（前提：cleanup 只认过期的）"
    assert "fresh.jar.old" not in first.body
    plugin._clear_pending()

    source = _PlayerSource("Admin")
    plugin._manual_delete(source, "all", "!!muc")

    body = source.body
    assert "即将删除全部 2 个旧版备份" in body, body
    assert "fresh.jar.old" in body and "ancient.jar.old" in body, body
    assert plugin._pending_action["files"] == ["ancient.jar.old", "fresh.jar.old"], "集合不对"

    plugin._manual_confirm(source, "!!muc")

    assert not (mods / "fresh.jar.old").exists(), "没到期的那个被漏掉了"
    assert not (mods / "ancient.jar.old").exists()


def test_delete_all_is_dropped_when_the_set_changed(tmp_path, monkeypatch):
    """集合变了整条作废——而且提示语要指回 ``delete all``，不是 ``cleanup``。

    指错了命令，管理员照着敲就会得到一个**完全不同**的计划（只删过期的），而他以为自己是在
    重列刚才那条。
    """
    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, max_age_days=30, backups=[("fresh.jar.old", 512, 21)],
    )
    source = _PlayerSource("Admin")
    plugin._manual_delete(source, "all", "!!muc")
    assert plugin._pending_action is not None

    (mods / "another.jar.old").write_bytes(b"y" * 8)

    plugin._manual_confirm(source, "!!muc")

    assert (mods / "fresh.jar.old").is_file()
    assert "作废" in source.body
    assert "!!muc delete all" in source.body, source.body


def test_delete_all_needs_the_switch_like_everything_else(tmp_path, monkeypatch):
    """总开关关着时，``delete all`` 与单条那条一样被拒绝。"""
    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, allow_delete=False, backups=[("ancient.jar.old", 512, 400)],
    )
    source = _PlayerSource("Admin")

    plugin._manual_delete(source, "all", "!!muc")

    assert "删除功能未开启" in source.body
    assert plugin._pending_action is None
    assert (mods / "ancient.jar.old").is_file()


def test_delete_all_with_nothing_to_delete_says_so(tmp_path, monkeypatch):
    plugin, _server, _mods = _cleanup_setup(tmp_path, monkeypatch, backups=[])
    source = _PlayerSource("Admin")

    plugin._manual_delete(source, "all", "!!muc")

    assert "没有旧版备份" in source.body
    assert plugin._pending_action is None


def test_a_deleted_backup_leaves_the_listing_at_once(tmp_path, monkeypatch):
    """删掉之后立刻从列表里消失——这条来自用户实测。

    列表、详情、汇总渲染的都是「上次检查的报告」，而它是一份快照：删完文件之后报告里还留着那条，
    于是列表照旧显示它（编号、大小、天数一个不少），再敲一次 delete 还能列出一个「即将删除」的
    计划，直到 confirm 才说「已经不在了」。用户的日志里连着出现了三次——他的结论是「删除要等到
    关服才生效」，而真相是删除当场就完成了，只是**列表没有跟着变**。
    """
    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("ancient.jar.old", 512, 400)],
    )
    source = _PlayerSource("Admin")

    plugin._manual_delete(source, "ancient.jar.old", "!!muc")
    plugin._manual_confirm(source, "!!muc")

    assert not (mods / "ancient.jar.old").exists()
    assert [entry.file_name for entry in plugin._last_report.backups] == [], "报告里还留着幽灵"
    assert "已删除" in source.body

    listing = _PlayerSource("Admin")
    plugin._show_list(listing, "", "!!muc")
    assert "ancient.jar.old" not in listing.body, listing.body

    # 再删一次：它已经不在手柄里了，所以是「找不到」而不是一个删空气的计划。
    again = _PlayerSource("Admin")
    plugin._manual_delete(again, "ancient.jar.old", "!!muc")
    assert plugin._pending_action is None
    assert "已经不在了" not in again.body, "又列出了一个删空气的计划"


def test_a_batch_deletion_takes_every_row_out_of_the_listing(tmp_path, monkeypatch):
    """批量那条同样要跟着更新——否则一次 cleanup 之后留下六行幽灵。"""
    plugin, _server, mods = _cleanup_setup(
        tmp_path, monkeypatch, max_age_days=0,
        backups=[("a.jar.old", 128, 400), ("b.jar.old", 256, 300)],
    )
    source = _PlayerSource("Admin")

    plugin._manual_cleanup(source, "!!muc")
    plugin._manual_confirm(source, "!!muc")

    assert not (mods / "a.jar.old").exists() and not (mods / "b.jar.old").exists()
    assert plugin._last_report.backups == []


def test_the_stored_report_is_rewritten_so_the_ghost_cannot_come_back(tmp_path, monkeypatch):
    """``last_report.json`` 也要改：不然重启一次，那份快照又把删掉的文件读回来。"""
    import json
    from pathlib import Path

    plugin, server, _mods = _cleanup_setup(
        tmp_path, monkeypatch, backups=[("ancient.jar.old", 512, 400)],
    )
    stored = Path(server.get_data_folder()) / "last_report.json"
    plugin._write_report_files(server, plugin._last_report)   # 快照先落盘，才有「幽灵回来」可言
    assert "ancient.jar.old" in stored.read_text(encoding="utf-8"), "（前提：快照里有它）"

    source = _PlayerSource("Admin")
    plugin._manual_delete(source, "ancient.jar.old", "!!muc")
    plugin._manual_confirm(source, "!!muc")

    saved = json.loads(stored.read_text(encoding="utf-8"))
    names = [entry.get("file_name") for entry in saved.get("entries", [])]
    assert "ancient.jar.old" not in names, names
    assert saved.get("counts", {}).get("old_backup") == 0, saved.get("counts")


def test_every_status_gets_the_colour_the_rule_says(tmp_path, monkeypatch):
    """配色的规则逐个状态钉住——加一个新状态而不来这张表登记，这条就失败。

    规则（用户定的）：绿 = 已是最新，蓝 = 有得更新（含待安装），红 = 有问题，其余灰；
    编号与状态格永远黄、名称永远白。颜色只在 ``_STATUS_COLOURS`` / ``_ROW_FIELD_COLOURS``
    两份表里，界面各处查它们。

    状态格现在是每一行都一样的 ``[状态: ✔]``：**文字永远黄、图标查状态表**（打叉红、打勾绿、
    有更新与待安装蓝——用户点名要图标分色），浮窗第一行用的是同一个 ``_status_colour``，所以行上
    的图标与浮窗的标题不会各说各话。
    """
    import mod_update_checker as plugin
    from mod_update_checker.i18n import make_translator
    from mod_update_checker.report import (
        ALL_STATUSES,
        ROW_FIELD_MARK,
        ROW_FIELD_NOTE,
        UpdateEntry,
        has_version_label,
        index_row_fields,
    )

    expected = {
        "update_available": plugin.RColor.blue,
        "awaiting_install": plugin.RColor.blue,
        "no_compatible_build": plugin.RColor.red,
        "error": plugin.RColor.red,
        "up_to_date": plugin.RColor.green,
        # 信息类：不是「要做什么」
        "local_ahead": plugin.RColor.gray,
        "unresolved": plugin.RColor.gray,
        "not_a_mod": plugin.RColor.gray,
        "ignored": plugin.RColor.gray,
        "old_backup": plugin.RColor.gray,
    }
    assert set(expected) == set(ALL_STATUSES), "有新状态没在这张表里登记"

    chinese = make_translator("zh_cn")
    for status, colour in expected.items():
        entry = UpdateEntry(mod_id="x", name="Alpha", file_name="alpha.jar",
                            local_version="1.0.0", latest_version="2.0.0", status=status)
        assert plugin._status_colour(status) == colour, status
        fields = index_row_fields(1, entry, chinese)
        assert plugin._row_field_colour(entry, plugin.ROW_FIELD_NUMBER) == plugin.RColor.yellow
        assert plugin._row_field_colour(entry, plugin.ROW_FIELD_NAME) == plugin.RColor.white
        # 状态格：文字一句话、永远黄；**图标查状态表**（打叉红、打勾绿、有更新与待安装蓝）。
        assert plugin._row_field_colour(entry, ROW_FIELD_NOTE) == plugin.RColor.yellow, status
        assert plugin._row_field_colour(entry, ROW_FIELD_MARK) == colour, status
        # ``[版本]`` 那一块：有版本可藏的状态是绿的（把手色），其余保持白。
        expected_body = (plugin.RColor.green if has_version_label(entry)
                         else plugin.RColor.white)
        assert plugin._row_field_colour(entry, plugin.ROW_FIELD_BODY) == expected_body, status

        # 而且状态**只说一次**：模板里不再夹带一份（那条重复是用户报上来的）
        words = chinese("status." + status)
        body = "".join(text for role, text in fields if role != ROW_FIELD_NOTE)
        assert words not in body, (status, body)
