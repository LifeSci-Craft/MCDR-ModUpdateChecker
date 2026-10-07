"""The MCDR entry module: how it hooks the lifecycle, and what it must not do twice.

Two of the defects this file guards against are invisible at runtime — no exception, no
warning, just wrong behaviour that only shows up as an extra request or a check that stops
firing — so they are pinned here rather than left to a review.
"""

import ast
import json
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "mod_update_checker"
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

    def warning(self, *_args, **_kwargs):
        pass


class _FakeServer:
    """Just enough of PluginServerInterface for the scheduling helpers."""

    def __init__(self):
        self.logger = _FakeLogger()


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


def test_the_stop_event_is_cleared_before_a_new_scheduler_starts(monkeypatch):
    """``!!modupdate reload`` stops the old thread and then starts a new one.

    If the stop event is not cleared in between, the new loop's very first ``wait`` returns
    immediately and the thread exits — silently disabling interval checking until the next
    MCDR restart. Reproduced here by setting the event, starting the scheduler, and checking
    that a live thread actually came up.
    """
    import mod_update_checker as plugin

    class _Config:
        enabled = True
        check_interval_hours = 1
        notify_on_updates_only = True

    monkeypatch.setattr(plugin, "_config", _Config(), raising=False)
    plugin._stop_scheduler()            # leaves _stop_event set, exactly as a reload does
    assert plugin._stop_event.is_set()

    server = _FakeServer()
    try:
        plugin._start_interval_scheduler(server)
        assert not plugin._stop_event.is_set(), "a stale stop event survived"
        thread = plugin._scheduler_thread
        assert thread is not None and thread.is_alive()
        assert thread.daemon, "a scheduler thread must not keep the process alive"
    finally:
        plugin._stop_scheduler()

    assert not plugin._scheduler_thread


def test_no_scheduler_thread_when_the_interval_is_zero(monkeypatch):
    import mod_update_checker as plugin

    class _Config:
        enabled = True
        check_interval_hours = 0
        notify_on_updates_only = True

    monkeypatch.setattr(plugin, "_config", _Config(), raising=False)
    plugin._scheduler_thread = None
    plugin._start_interval_scheduler(_FakeServer())
    assert plugin._scheduler_thread is None


def test_stopping_twice_is_harmless(monkeypatch):
    """Both ``on_unload`` and the next ``on_load`` may ask for a stop; that must be safe."""
    import mod_update_checker as plugin

    plugin._stop_scheduler()
    plugin._stop_scheduler()
    assert plugin._scheduler_thread is None


def test_config_defaults_are_the_documented_ones():
    """The README's "works out of the box" claim depends on these exact values."""
    import mod_update_checker as plugin

    config = plugin.Config.get_default()
    assert config.modrinth_api_base == ""          # official endpoint
    assert config.curseforge_api_base == ""
    assert config.curseforge_api_key == ""         # so CurseForge is skipped, not fatal
    assert config.use_modrinth is True
    assert config.use_curseforge is True
    assert config.mc_version == "auto"
    assert config.loader == "fabric"
    assert config.language == "auto"
    assert config.mods_directory == ""
    assert config.check_on_server_start is True
    assert config.check_interval_hours == 0        # no surprise periodic load
    assert config.notify_in_game is False          # never broadcast to players by default
    assert config.notify_on_updates_only is True
    assert config.command_permission_level == 3
    assert config.use_resolve_cache is True
    assert config.requests_per_minute == 240       # under Modrinth's documented 300/min


def test_config_round_trips_through_json():
    """``load_config_simple`` serialises and deserialises; unknown keys must not explode."""
    import mod_update_checker as plugin

    config = plugin.Config.get_default()
    payload = config.serialize()
    assert json.loads(json.dumps(payload)) == payload
    restored = plugin.Config.deserialize(payload)
    assert restored.command_permission_level == config.command_permission_level


def test_coloured_lines_are_only_used_where_colour_survives():
    """The colour paths differ, and mixing them up loses the colour silently.

    ``source.reply`` renders an RText (MCDR's ``StdoutReplier`` calls ``to_colored_text``, a
    player gets a chat component), while ``server.logger.info`` runs the message through
    ``str()`` — and ``RTextBase.__str__`` returns plain text. So a line built for a reply must
    still stringify back to exactly the original text, or logging it would be lossy.

    Empirically checked against the installed MCDR: ``str(RText('hello', RColor.yellow))`` is
    ``'hello'`` with no ANSI escape in it.
    """
    import mod_update_checker as plugin

    update_line = "Alpha  1.0.0 -> 1.1.0"
    coloured = plugin._coloured_line(update_line)

    assert str(coloured) == update_line
    assert "\x1b[" not in str(coloured)              # nothing depends on ANSI surviving
    assert "\x1b[" in coloured.to_colored_text()     # but the reply path does colour it

    header = plugin._coloured_line("Mod update check — server 26.3")
    assert str(header) == "Mod update check — server 26.3"
    # A header and an update line must not end up the same colour.
    assert coloured.to_colored_text() != header.to_colored_text()


def test_the_console_path_logs_plain_strings():
    """Guards the asymmetry: passing RText to the logger is a silent no-op for colour."""
    import ast

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
            isinstance(argument.func, ast.Name) and argument.func.id == "_coloured_line"
        ), "an RText line is being logged, where the colour would be dropped"
