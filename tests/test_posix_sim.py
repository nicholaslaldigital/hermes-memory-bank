"""The macOS/Linux-only code and the SQLite safety gates, exercised on any machine: any Python 3.9+, no Hermes.

    python tests/test_posix_sim.py

The plugin was written and run on Windows. Its macOS/Linux branches (private file modes, the viewer not opening a
browser on Linux, the journal-mode decisions that depend on the SQLite version, the file system and the platform) are
reached here by standing in for the module-level names they look at (``os``, ``sys``, ``sqlite3``) and writing down
what they are asked to do. This shows the decisions are the intended ones; it is not a run on a real Mac or Linux box,
and where a real one can add something (actual file modes) the test says SKIP on Windows and checks them elsewhere.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import gc
import importlib.util
import io
import itertools
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import sys
import tempfile
import traceback
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_core  # noqa: E402  (loads the plugin's core package, as every test file here does)

core = test_core.core
ROOT = test_core.ROOT
cli_mod = test_core.bank_cli
server_mod = core.server
store_mod = core.store

REAL_OS = os
TESTS = []


class Skip(Exception):
    """Raised by a test that needs a real macOS/Linux machine and is running on something else."""


def test(fn):
    TESTS.append(fn)
    return fn


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):
        return None


# ---------------------------------------------------------------------------
# stand-ins
# ---------------------------------------------------------------------------

class PosixOS:
    """``os`` as seen from macOS/Linux (or Windows), writing down every call that sets a mode or opens a file."""

    def __init__(self, log, name="posix"):
        self.log = log
        self.name = name

    def __getattr__(self, attr):
        return getattr(REAL_OS, attr)

    def open(self, path, flags, mode=0o777, **kw):
        self.log.append(("open", Path(path).name, flags & (REAL_OS.O_CREAT | REAL_OS.O_EXCL | REAL_OS.O_TRUNC), mode))
        return REAL_OS.open(path, flags, mode, **kw)

    def fchmod(self, fd, mode):
        self.log.append(("fchmod", mode))
        if hasattr(REAL_OS, "fchmod"):
            REAL_OS.fchmod(fd, mode)

    def chmod(self, path, mode, **kw):
        self.log.append(("chmod", Path(path).name, mode))
        return REAL_OS.chmod(path, mode, **kw)

    def fdopen(self, fd, *a, **kw):
        self.log.append(("fdopen",))      # the file is written to from here on
        return REAL_OS.fdopen(fd, *a, **kw)


@contextlib.contextmanager
def pretend(*modules, name="posix"):
    """Make each module see an ``os`` that says ``name``; Path.chmod (the store's folder) is written down too."""
    log = []
    saved = [(m, m.os) for m in modules]
    real_chmod = pathlib.Path.chmod

    def logging_chmod(self, mode, *a, **k):
        log.append(("Path.chmod", self.name, mode))
        return real_chmod(self, mode, *a, **k)

    pathlib.Path.chmod = logging_chmod
    for module in modules:
        module.os = PosixOS(log, name)
    try:
        yield log
    finally:
        pathlib.Path.chmod = real_chmod
        for module, old in saved:
            module.os = old


@contextlib.contextmanager
def quiet_cli():
    saved = cli_mod._out
    lines = []
    cli_mod._out = lambda text="": lines.append(str(text))
    try:
        yield lines
    finally:
        cli_mod._out = saved


# ---------------------------------------------------------------------------
# private files on macOS and Linux (engine E16/E17, posix F4/F7/F8)
# ---------------------------------------------------------------------------

@test
def a_new_bank_is_created_private_and_an_old_one_is_left_alone(tmp):
    bank = tmp / "h" / "plugin-data" / "memorybank" / "bank.db"
    with pretend(store_mod) as log:
        store_mod.Store(bank).close()
    assert [e for e in log if e[0] == "Path.chmod"] == [("Path.chmod", "memorybank", 0o700)], log
    opened = [e for e in log if e[0] == "open"]
    assert len(opened) == 1 and opened[0][1] == "bank.db" and opened[0][3] == 0o600, log
    assert opened[0][2] & REAL_OS.O_CREAT and opened[0][2] & REAL_OS.O_EXCL, "created exclusively, so nobody's file is reused"
    # a folder somebody else made (the installer): not touched; the new file inside it still starts private
    other = tmp / "h2" / "plugin-data" / "memorybank"
    other.mkdir(parents=True)
    with pretend(store_mod) as log:
        store_mod.Store(other / "bank.db").close()
    assert [e for e in log if e[0] == "Path.chmod"] == [] and [e[3] for e in log if e[0] == "open"] == [0o600], log
    # a bank that exists (made by an older build, say): nothing is changed, whatever its mode is
    with pretend(store_mod) as log:
        store_mod.Store(bank).close()
    assert log == [], log
    # looking never changes anything either
    with pretend(store_mod) as log:
        store_mod.Store(bank, readonly=True).close()
    assert log == [], log
    # Windows has no such modes
    gone = tmp / "w" / "plugin-data" / "memorybank" / "bank.db"
    with pretend(store_mod, name="nt") as log:
        store_mod.Store(gone).close()
    assert log == [], log


@test
def an_export_is_private_before_the_first_byte_is_written(tmp):
    home = tmp / "home"
    store = core.Store(core.db_path(home))
    store.upsert_node("m:1", "memory", content="a fact worth keeping")
    store.close()
    target = tmp / "out" / "export.json"
    with quiet_cli() as lines, pretend(cli_mod) as log:
        assert cli_mod.cmd_export(home, Args(path=str(target))) == 0, lines
    order = [e[0] for e in log]
    assert order[:3] == ["open", "fchmod", "fdopen"], "the mode must be set after opening and before writing: %r" % log
    assert log[0][3] == 0o600 and log[1] == ("fchmod", 0o600), log
    assert json.loads(target.read_text("utf-8"))["format"] == "hermes-memorybank/1"
    # a file that was already there keeps its old mode when it is reopened: it is narrowed before anything is written
    target.write_text("old", "utf-8")
    with quiet_cli() as lines, pretend(cli_mod) as log:
        assert cli_mod.cmd_export(home, Args(path=str(target))) == 0
    assert [e[0] for e in log][:3] == ["open", "fchmod", "fdopen"] and ("fchmod", 0o600) in log, log
    # Windows: no modes
    with quiet_cli() as lines, pretend(cli_mod, name="nt") as log:
        assert cli_mod.cmd_export(home, Args(path=str(target))) == 0
    assert [e[0] for e in log] == ["open", "fdopen"], log


@test
def real_file_modes_on_macos_and_linux(tmp):
    if os.name == "nt":
        raise Skip("file modes need macOS or Linux (the two tests above stand in for them on Windows)")
    home = tmp / "home"
    path = core.db_path(home)
    store = core.Store(path)
    store.upsert_node("m:1", "memory", content="a fact")
    mode = lambda p: stat.S_IMODE(Path(p).stat().st_mode)  # noqa: E731
    assert mode(path.parent) == 0o700 and mode(path) == 0o600, (oct(mode(path.parent)), oct(mode(path)))
    for suffix in ("-wal", "-shm"):
        if Path(str(path) + suffix).exists():
            assert mode(str(path) + suffix) == 0o600, suffix
    store.close()
    target = tmp / "export.json"
    target.write_text("old", "utf-8")
    target.chmod(0o644)
    with quiet_cli():
        assert cli_mod.cmd_export(home, Args(path=str(target))) == 0
    assert mode(target) == 0o600


# ---------------------------------------------------------------------------
# the viewer is not opened automatically on Linux (engine E15, posix F8)
# ---------------------------------------------------------------------------

class _FakeServer:
    server_address = ("127.0.0.1", 4242)
    token = "tok"

    def serve_forever(self, poll_interval=0.5):
        return

    def server_close(self):
        pass


class _FakeStore:
    def close(self):
        pass


class _Immediately:
    """``threading.Timer`` that fires at once: serve() waits 0.4 s before opening the page, and a test need not."""

    def __init__(self, interval, function):
        self.function = function

    def start(self):
        self.function()


@test
def the_viewer_link_is_opened_everywhere_but_linux(tmp):
    opened = []
    real = (server_mod.make_server, server_mod.sys, server_mod.webbrowser, server_mod.threading)
    server_mod.make_server = lambda *a, **k: (_FakeServer(), _FakeStore())
    server_mod.webbrowser = types.SimpleNamespace(open=lambda url: opened.append(url) or True)
    server_mod.threading = types.SimpleNamespace(Timer=_Immediately)
    try:
        # (platform, open_browser, force_open, a browser is started)
        cases = [("linux", True, False, False), ("linux", True, True, True), ("linux", False, True, False),
                 ("linux2", True, False, False), ("darwin", True, False, True), ("win32", True, False, True),
                 ("darwin", False, False, False), ("win32", False, True, False)]
        for platform, want_open, force, expected in cases:
            del opened[:]
            server_mod.sys = types.SimpleNamespace(platform=platform)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert server_mod.serve(tmp, open_browser=want_open, force_open=force) == 0
            assert bool(opened) == expected, (platform, want_open, force, opened)
            said = "not opened automatically on Linux" in out.getvalue()
            assert said == (platform.startswith("linux") and want_open and not force), (platform, out.getvalue())
            assert "http://127.0.0.1:4242/#tok" in out.getvalue()
            assert not opened or opened == ["http://127.0.0.1:4242/#tok"], opened
    finally:
        server_mod.make_server, server_mod.sys, server_mod.webbrowser, server_mod.threading = real


@test
def the_ui_flags_reach_the_viewer(tmp):
    got = {}
    home = tmp / "home"
    core.Store(core.db_path(home)).close()
    saved = server_mod.serve
    server_mod.serve = lambda h, **kw: got.update(kw) or 0
    parser = argparse.ArgumentParser()
    cli_mod.register_cli(parser)
    try:
        for argv, want in ((["ui"], (True, False)), (["ui", "--no-open"], (False, False)), (["ui", "--open"], (True, True))):
            got.clear()
            assert cli_mod.cmd_ui(home, parser.parse_args(argv)) == 0
            assert (got["open_browser"], got["force_open"]) == want, (argv, got)
    finally:
        server_mod.serve = saved


# ---------------------------------------------------------------------------
# WAL only where SQLite is safe to use it, and as durable as the platform allows (posix F1)
# ---------------------------------------------------------------------------

@test
def the_wal_reset_bug_rule_matches_sqlites_own_table(tmp):
    bug = store_mod._wal_reset_bug
    # SQLite's WAL-reset bug: present in 3.7.0 - 3.51.2, fixed in 3.51.3 and back-ported to 3.50.7 and 3.44.6
    for version in [(3, 7, 0), (3, 8, 11), (3, 37, 0), (3, 39, 5), (3, 43, 2), (3, 44, 0), (3, 44, 5), (3, 45, 0), (3, 45, 3),
                    (3, 46, 1), (3, 49, 2), (3, 50, 0), (3, 50, 4), (3, 50, 6), (3, 51, 0), (3, 51, 1), (3, 51, 2)]:
        assert bug(version) is True, version
    for version in [(3, 6, 9), (3, 44, 6), (3, 44, 9), (3, 50, 7), (3, 50, 9), (3, 51, 3), (3, 51, 4), (3, 53, 1), (3, 60, 0), (4, 0, 0)]:
        assert bug(version) is False, version
    assert bug((3, 44)) is True and bug((3, 53)) is False, "a short version is read as 3.44.0 and 3.53.0"
    assert bug() == bug(sqlite3.sqlite_version_info), "without an argument it asks the library that is running"


@test
def durability_follows_the_journal_and_the_platform(tmp):
    durability = store_mod._durability
    # Windows and Linux with a write-ahead log: exactly what the bank always did
    assert durability(True, "win32") == ["PRAGMA synchronous=NORMAL"]
    assert durability(True, "linux") == ["PRAGMA synchronous=NORMAL"]
    # macOS: Apple's fsync() does not order writes - the same two settings Hermes puts on its own databases
    assert durability(True, "darwin") == ["PRAGMA synchronous=FULL", "PRAGMA checkpoint_fullfsync=1"]
    # a rollback journal is only corruption-safe with FULL, whatever the platform
    for platform in ("win32", "linux", "darwin"):
        assert durability(False, platform) == ["PRAGMA synchronous=FULL"], platform


def _counter(prefix=itertools.count()):
    return next(prefix)


class _Disk:
    """How a (simulated) file system answers ``PRAGMA journal_mode=WAL``, and what the store asked it."""

    def __init__(self, behaviour=""):
        self.behaviour = behaviour       # "" real | "silent" answers 'delete' | "raise:<text>" raises OperationalError
        self.log = []                    # every journal_mode statement sent on these connections, ever
        self.asked = []                  # ... and the ones the store sent while it was being opened (set by open_bank)
        disk = self

        class Conn(sqlite3.Connection):
            def execute(self, sql, *args):
                text = sql.strip().lower().replace(" ", "")
                if text.startswith("pragmajournal_mode"):
                    disk.log.append(sql.strip())
                    if text == "pragmajournal_mode=wal":
                        if disk.behaviour.startswith("raise:"):
                            raise sqlite3.OperationalError(disk.behaviour.split(":", 1)[1])
                        if disk.behaviour == "silent":
                            return super().execute("SELECT 'delete'")
                return super().execute(sql, *args)

        self.connection = Conn


class _Library:
    """``sqlite3`` as the store sees it: a chosen version number and connections that answer like ``_Disk``."""

    def __init__(self, disk, version):
        self._disk = disk
        self.sqlite_version_info = version

    def __getattr__(self, name):
        return getattr(sqlite3, name)

    def connect(self, *args, **kwargs):
        kwargs["factory"] = self._disk.connection
        return sqlite3.connect(*args, **kwargs)


def pragma(store, name):
    return store._conn.execute("PRAGMA " + name).fetchone()[0]


def open_bank(tmp, *, version=(3, 53, 1), platform="win32", behaviour="", existing_wal=False):
    """Open a bank as if on ``platform`` with SQLite ``version`` over a file system that answers ``behaviour``."""
    path = tmp / ("bank-%d" % _counter()) / "bank.db"
    path.parent.mkdir(parents=True)
    if existing_wal:
        raw = sqlite3.connect(str(path))
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("CREATE TABLE z(a)")
        raw.commit()
        raw.close()
    disk = _Disk(behaviour)
    real = (store_mod.sqlite3, store_mod.sys)
    store_mod.sqlite3, store_mod.sys = _Library(disk, version), types.SimpleNamespace(platform=platform)
    try:
        store = store_mod.Store(path)
    finally:
        store_mod.sqlite3, store_mod.sys = real
        disk.asked = list(disk.log)      # what opening the bank asked for; reading the settings back adds to ``log``
    return store, disk


def settled(store):
    return (str(pragma(store, "journal_mode")).lower(), int(pragma(store, "synchronous")), int(pragma(store, "checkpoint_fullfsync")))


def works(store):
    node = store.upsert_node("m:1", "memory", content="the bank still takes a write")[0]
    assert store.get(node)["content"] == "the bank still takes a write"


WAL, DELETE = "wal", "delete"
NORMAL, FULL = 1, 2


@test
def a_bank_on_a_safe_library_is_what_it_always_was(tmp):
    for platform in ("win32", "linux"):
        store, disk = open_bank(tmp, version=(3, 53, 1), platform=platform)
        assert settled(store) == (WAL, NORMAL, 0), (platform, settled(store))
        assert disk.asked == ["PRAGMA journal_mode", "PRAGMA journal_mode=WAL"], disk.asked
        works(store)
        store.close()
    # and with the library that is really running here, no stand-ins at all
    plain = core.Store(tmp / "plain" / "bank.db")
    expected = DELETE if store_mod._wal_reset_bug() else WAL
    assert str(pragma(plain, "journal_mode")).lower() == expected, expected
    plain.close()


@test
def on_macos_the_log_is_made_as_durable_as_hermes_makes_its_own(tmp):
    store, disk = open_bank(tmp, version=(3, 53, 1), platform="darwin")
    assert settled(store) == (WAL, FULL, 1), settled(store)
    works(store)
    store.close()


@test
def a_library_with_the_wal_reset_bug_keeps_new_banks_out_of_wal(tmp):
    for platform in ("linux", "darwin"):
        for version in [(3, 39, 5), (3, 43, 2), (3, 45, 3), (3, 50, 4)]:
            store, disk = open_bank(tmp, version=version, platform=platform)
            assert settled(store) == (DELETE, FULL, 0), (platform, version, settled(store))
            assert disk.asked == ["PRAGMA journal_mode"], "WAL was asked for on a library that has the bug: %r" % disk.asked
            works(store)
            store.close()
    # a bank that is already in WAL stays in WAL: switching it back would destroy what other connections have not checkpointed
    store, disk = open_bank(tmp, version=(3, 39, 5), platform="linux", existing_wal=True)
    assert settled(store) == (WAL, NORMAL, 0), settled(store)
    assert not any(word in " ".join(disk.asked).lower() for word in ("=delete", "=truncate", "=off", "=persist")), disk.asked
    works(store)
    store.close()
    store, disk = open_bank(tmp, version=(3, 39, 5), platform="darwin", existing_wal=True)
    assert settled(store) == (WAL, FULL, 1), "an old WAL bank on a Mac is hardened too: %r" % (settled(store),)
    store.close()


@test
def a_file_system_that_refuses_wal_leaves_the_bank_in_a_rollback_journal(tmp):
    # network and FUSE mounts: SQLite raises ...
    for text in ("locking protocol", "not authorized"):
        store, disk = open_bank(tmp, platform="linux", behaviour="raise:" + text)
        assert settled(store) == (DELETE, FULL, 0), (text, settled(store))
        assert not any("=delete" in sql.lower().replace(" ", "") for sql in disk.asked), disk.asked
        works(store)
        store.close()
    # ... or, on a Mac's NFS/SMB, answers 'delete' without raising: the result row is read, not assumed
    store, disk = open_bank(tmp, platform="darwin", behaviour="silent")
    assert settled(store) == (DELETE, FULL, 0), settled(store)
    works(store)
    store.close()
    # an old WAL bank over such a mount is never switched
    store, disk = open_bank(tmp, platform="linux", behaviour="raise:locking protocol", existing_wal=True)
    assert settled(store)[0] == WAL and disk.asked == ["PRAGMA journal_mode"], (settled(store), disk.asked)
    store.close()


@test
def other_errors_from_the_journal_pragma_are_not_swallowed(tmp):
    for text in ("disk I/O error", "database is locked", "attempt to write a readonly database"):
        try:
            open_bank(tmp, platform="linux", behaviour="raise:" + text)
        except sqlite3.OperationalError as exc:
            assert text in str(exc), exc
        else:
            raise AssertionError("an error the store cannot judge was hidden: " + text)
    gc.collect()      # the stores that failed to open were never closed: let go of their files so the folder can go


# ---------------------------------------------------------------------------
# Python 3.9 (posix F6)
# ---------------------------------------------------------------------------

def newline_calls(source: str):
    """Calls of ``write_text`` / ``read_text`` that pass ``newline=``: Path.write_text only has it from Python 3.10."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("write_text", "read_text") \
                and any(keyword.arg == "newline" for keyword in node.keywords):
            found.append(node.lineno)
    return found


@test
def the_plugin_does_not_use_path_text_methods_newer_than_python_3_9(tmp):
    assert newline_calls('p.write_text(t, encoding="utf-8", newline="\\n")') == [1], "the scan itself must catch the call"
    assert newline_calls('os.fdopen(fd, "w", newline="\\n")\nopen(p, "w", newline="")') == [], "other newline= uses are fine"
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for line in newline_calls(path.read_text(encoding="utf-8")):
            offenders.append("%s:%d" % (path.relative_to(ROOT), line))
    assert not offenders, "Path.write_text/read_text(newline=) needs Python 3.10 (3.13 for read_text): " + ", ".join(offenders)


@test
def build_py_writes_the_bundles_on_python_3_9_byte_for_byte(tmp):
    spec = importlib.util.spec_from_file_location("_mb_build_under_test", ROOT / "ui" / "build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    plugin = tmp / "plugin"
    (plugin / "ui").mkdir(parents=True)
    shutil.copy(str(ROOT / "ui" / "engine.js"), str(plugin / "ui" / "engine.js"))
    build.ROOT = plugin
    real_write_text = pathlib.Path.write_text

    def write_text_python_3_9(self, data, encoding=None, errors=None):    # no ``newline`` parameter before Python 3.10
        return real_write_text(self, data, encoding=encoding, errors=errors)

    pathlib.Path.write_text = write_text_python_3_9
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            assert build.build() == 0
    finally:
        pathlib.Path.write_text = real_write_text
    for rel in ("dashboard/dist/index.js", "desktop/plugin.js"):
        made, shipped = (plugin / rel).read_bytes(), (ROOT / rel).read_bytes()
        assert b"\r" not in made, rel + " was written with Windows line ends"
        assert made == shipped, rel + " differs from the bundle that is checked in"
    with contextlib.redirect_stdout(io.StringIO()):
        assert build.build(check=True) == 0


# ---------------------------------------------------------------------------

def main() -> int:
    failed = skipped = 0
    for fn in TESTS:
        tmp = Path(tempfile.mkdtemp(prefix="mbposix-"))
        try:
            fn(tmp)
            print(f"PASS  {fn.__name__}")
        except Skip as why:
            skipped += 1
            print(f"SKIP  {fn.__name__}: {why}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    ran = len(TESTS) - skipped
    print(f"\n{ran - failed}/{ran} passed" + (f"  ({skipped} skipped: not run here)" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
