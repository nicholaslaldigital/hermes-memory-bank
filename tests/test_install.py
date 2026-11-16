"""Installer tests on throwaway Hermes roots: run with any Python 3.8+ (no Hermes needed for most).

    python tests/test_install.py [real-config.yaml-to-copy]

Two ways of switching the plugin on are covered:

* through the `hermes` command - tested against tests/fake_hermes.py, a stand-in that understands the handful
  of commands the installer uses (this runs anywhere, and never touches a real Hermes);
* through Hermes' own Python and config writer - the fallback, tested only when a Hermes source folder with
  PyYAML is available (HERMES_AGENT_DIR, or <hermes root>/hermes-agent); otherwise it is skipped.

When given a real profile's config.yaml, the fallback scenario copies it into its sandbox (never printed) to prove
Hermes' writer leaves every other setting exactly as it was.

Most scenarios start a real installer process with a stub `hermes` program (a .cmd or a shell script); the ones that
are about the installer's own logic (locks, copies, purge, read-back, wording) call install.main() inside this process
and answer its `hermes` calls with the same stand-in, also in-process (fast_hermes), which takes seconds instead of
minutes. Every scenario runs on a throwaway folder; nothing touches a real Hermes profile.
"""

from __future__ import annotations

import ast
import contextlib
import copy
import fnmatch
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import fake_hermes  # noqa: E402
import test_core  # noqa: E402

INSTALLER = ROOT / "install.py"
SYNTH: Dict = {
    "model": {"default": "some/model", "provider": "nous"}, "agent": {"max_turns": 500},
    "skills": {"auto_load": ["skill-a"]},
    "plugins": {"enabled": ["plugin-a", "plugin-b", "plugin-c"], "disabled": []},
    "auxiliary": {"compression": {"provider": "nous"}},
}
PASSED: List[str] = []
SKIPPED: List[str] = []


def hermes_agent_dir() -> Optional[Path]:
    given = os.environ.get("HERMES_AGENT_DIR")
    if given:
        return Path(given)
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes"
    else:
        base = Path.home() / ".hermes"
    return base / "hermes-agent"


AGENT = hermes_agent_dir()


def python_route_available() -> bool:
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return bool(AGENT and (AGENT / "hermes_cli" / "config.py").exists())


def passed(text: str) -> None:
    PASSED.append(text)
    print("PASS  " + text)


def run(*args: str, env: Optional[Dict[str, str]] = None, expect: Optional[int] = 0,
        installer: Optional[Path] = None, cwd: Optional[Path] = None) -> Tuple[int, str]:
    full = dict(os.environ)
    full.update(env or {})
    done = subprocess.run([sys.executable, str(installer or INSTALLER), *args], capture_output=True, text=True,
                          timeout=900, env=full, stdin=subprocess.DEVNULL, cwd=str(cwd) if cwd else None)
    out = done.stdout + done.stderr
    if expect is not None and done.returncode != expect:
        raise AssertionError("install.py %s -> %d (wanted %d):\n%s" % (" ".join(args), done.returncode, expect, out))
    return done.returncode, out


def make_stub(tmp: Path) -> str:
    """A `hermes` command (a .cmd on Windows, a script elsewhere) that runs tests/fake_hermes.py."""
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    script = str(HERE / "fake_hermes.py")
    if os.name == "nt":
        exe = bindir / "hermes.cmd"
        exe.write_text('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, script), encoding="utf-8")
    else:
        exe = bindir / "hermes"
        exe.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script), encoding="utf-8")
        exe.chmod(0o755)
    return str(exe)


def make_world(tmp: Path, name: str = "demo", cfg: Optional[Dict] = None) -> Tuple[Path, Path]:
    """A sandbox Hermes root with one profile that has skills, memory files and past sessions."""
    test_core.make_home(tmp)
    root = tmp / "hermes"
    home = root / "profiles" / name
    home.parent.mkdir(parents=True)
    shutil.move(str(tmp / "home"), str(home))
    fake_hermes.write_state(home, copy.deepcopy(SYNTH if cfg is None else cfg))
    return root, home


def make_root_world(tmp: Path, sticky: Optional[str] = None) -> Tuple[Path, Path]:
    """A sandbox Hermes root that is itself the default profile (skills, memory files, past sessions), with a
    second profile, 'work', beside it. ``sticky`` is what `hermes profile use <name>` would have written."""
    test_core.make_home(tmp)
    root = tmp / "hermes"
    shutil.move(str(tmp / "home"), str(root))
    fake_hermes.write_state(root, copy.deepcopy(SYNTH))
    work = root / "profiles" / "work"
    fake_hermes.write_state(work, copy.deepcopy(SYNTH))
    if sticky:
        (root / "active_profile").write_text(sticky + "\n", encoding="utf-8")
    return root, work


def lock_file(home: Path) -> Path:
    return home / "plugin-data" / ".memorybank-install.lock"


IGNORED = ("__pycache__", "*.pyc", "*.pyo", ".DS_Store", "._*", "Thumbs.db", "desktop.ini")


def tree_files(folder: Path) -> List[str]:
    """Every file under ``folder`` that an install copies, as sorted relative paths with forward slashes."""
    found = []
    for path in folder.rglob("*"):
        rel = path.relative_to(folder)
        if path.is_file() and not any(fnmatch.fnmatch(part, pat) for part in rel.parts for pat in IGNORED):
            found.append(rel.as_posix())
    return sorted(found)


# The plugin, as the repository's top folder holds it. Written out here on purpose, not read from install.py: this is
# the oracle the installer's own list is checked against. Everything else in the repository must stay out of a profile.
PLUGIN_TOP = ("plugin.yaml", "__init__.py", "cli.py", "run.py", "README.md", "core", "dashboard", "desktop", "skills", "ui")
NOT_PLUGIN_TOP = ("install.py", "tests", "docs", "catalog", "CATALOG.md", "CHANGELOG.md", "INSTALL-WITH-CLAUDE.md", "LICENSE",
                  ".gitignore", ".gitattributes")


def plugin_tree_files() -> List[str]:
    """Every file of the plugin that an install copies (PLUGIN_TOP of the repository), sorted, forward slashes."""
    found: List[str] = []
    for name in PLUGIN_TOP:
        path = ROOT / name
        if path.is_file():
            found.append(name)
        else:
            found += [name + "/" + rel for rel in tree_files(path)]
    return sorted(found)


def calls(root: Path) -> List[List]:
    try:
        return [json.loads(line) for line in (root / "fake-calls.log").read_text("utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def backups(home: Path, prefix: str = "memorybank-2") -> List[Path]:
    return sorted(p for p in (home / "backups").iterdir() if p.name.startswith(prefix))


def rollback_command(note: Path) -> str:
    blocks = [b.strip() for b in note.read_text("utf-8").split("```") if "--uninstall" in b and b.strip()]
    return blocks[-1]          # the Command Prompt form on Windows, the only form elsewhere


def install_state(home: Path) -> Dict:
    return json.loads((home / "plugin-data" / "memorybank" / "install.json").read_text("utf-8"))


def untouched(before: Dict, after: Dict, skip=("memory", "plugins")) -> None:
    for key in set(before) | set(after):
        if key not in skip:
            assert before.get(key) == after.get(key), "setting '%s' changed" % key


# ----------------------------------------------------------------------------- through the `hermes` command

def s_cli_fresh(tmp: Path) -> None:
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    env = {"FAKE_HERMES_ROOT": str(root)}
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe]
    config = home / "config.yaml"
    before, before_bytes = fake_hermes.read_state(home), config.read_bytes()

    _, out = run(*common, "--dry-run", env=env)
    assert "dry run" in out and "Hermes' own commands" in out, out
    assert config.read_bytes() == before_bytes and not (home / "plugins" / "memorybank").exists()
    assert not (home / "backups").exists()
    assert not [c for c in calls(root) if c[1:3] in (["config", "set"], ["plugins", "enable"])], "a dry run writes nothing"
    passed("hermes command: dry run shows the plan and changes nothing")

    _, out = run(*common, env=env)
    assert "verified" in out and "everything is in the bank" in out, out
    cfg = fake_hermes.read_state(home)
    assert cfg["memory"]["provider"] == "memorybank", cfg
    assert cfg["plugins"]["enabled"] == before["plugins"]["enabled"] + ["memorybank"], cfg["plugins"]
    untouched(before, cfg)
    state = install_state(home)
    assert state["activation"] == "cli" and state["activated"] and state["previous"]["provider"] is None, state
    assert state["previous"]["enabled"] == before["plugins"]["enabled"], state["previous"]
    plugin = home / "plugins" / "memorybank"
    for rel in ("__init__.py", "plugin.yaml", "core/store.py", "core/usage.py", "dashboard/manifest.json",
                "desktop/plugin.js", "skills/curate/SKILL.md", "ui/engine.js", "run.py", "cli.py"):
        assert (plugin / rel).exists(), rel
    assert not list(plugin.rglob("__pycache__")), "no bytecode is shipped"
    (backup,) = backups(home)
    assert (backup / "config.yaml").read_bytes() == before_bytes and (backup / "install.py").exists()
    if os.name != "nt":      # a backup holds copies of settings and the bank's data: private to this user
        assert stat.S_IMODE(backup.stat().st_mode) == 0o700, oct(backup.stat().st_mode)
        assert stat.S_IMODE((home / "plugin-data" / "memorybank").stat().st_mode) == 0o700
    else:
        SKIPPED.append("POSIX permission checks (0700 on backups and on the bank folder): not run on Windows")
    note = (backup / "ROLLBACK.md").read_text("utf-8")
    assert "--uninstall" in note and "plugins disable memorybank" in note and str(backup / "install.py") in note, note[:500]
    conn = sqlite3.connect(str(home / "plugin-data" / "memorybank" / "bank.db"))
    counts = dict(conn.execute("SELECT kind, COUNT(*) FROM nodes GROUP BY kind").fetchall())
    conn.close()
    assert counts.get("skill") == 4 and counts.get("session") == 3 and counts.get("memory") == 5, counts
    passed("hermes command: install switches on through plugins enable + config set, verifies, indexes (%s)" % counts)

    seen = len(calls(root))
    _, out = run(*common, "--no-index", env=env)
    fresh = [c[1:] for c in calls(root)[seen:]]
    assert not [c for c in fresh if c[:2] in (["config", "set"], ["plugins", "enable"], ["config", "unset"])], fresh
    assert install_state(home)["activation"] == "already" and install_state(home)["previous"]["provider"] is None
    assert len(backups(home)) == 2 and fake_hermes.read_state(home) == cfg, "re-install writes no setting"
    passed("hermes command: re-install of an active bank writes nothing to config and keeps the original record")


def s_cli_provider_and_rollback(tmp: Path) -> None:
    cfg = copy.deepcopy(SYNTH)
    cfg["memory"] = {"provider": "honcho"}
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    env = {"FAKE_HERMES_ROOT": str(root)}
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe]
    run(*common, "--no-index", env=env)
    state = install_state(home)
    assert state["previous"]["provider"] == "honcho", state
    assert fake_hermes.read_state(home)["memory"]["provider"] == "memorybank"
    run(*common, "--no-index", env=env)
    assert install_state(home)["previous"]["provider"] == "honcho", "the ORIGINAL provider survives a re-install"
    newest = backups(home)[-1]
    note = (newest / "ROLLBACK.md").read_text("utf-8")
    assert "`honcho`" in note and "config set memory.provider honcho" in note, note[:900]

    done = subprocess.run(rollback_command(newest / "ROLLBACK.md"), shell=True, capture_output=True, text=True,
                          timeout=300, env=dict(os.environ, **env), stdin=subprocess.DEVNULL)
    assert done.returncode == 0, (done.stdout, done.stderr)
    after = fake_hermes.read_state(home)
    assert after["memory"]["provider"] == "honcho", after.get("memory")
    assert "memorybank" not in after["plugins"]["enabled"] and after["plugins"]["enabled"] == SYNTH["plugins"]["enabled"]
    untouched(cfg, after)
    assert not (home / "plugins" / "memorybank").exists()
    removed = [p for p in (home / "backups").iterdir() if "removed" in p.name]
    assert removed and (removed[0] / "plugin" / "__init__.py").exists()
    passed("hermes command: the ROLLBACK.md command works as written and restores the original provider")

    seen = len(calls(root))
    _, out = run(*common, "--uninstall", env=env)
    mutating = [c[1:] for c in calls(root)[seen:] if c[1:3] in (["config", "set"], ["config", "unset"], ["plugins", "disable"])]
    assert not mutating, mutating
    assert fake_hermes.read_state(home) == after
    passed("hermes command: a second uninstall touches nothing")


def s_cli_provider_unset(tmp: Path) -> None:
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    env = {"FAKE_HERMES_ROOT": str(root)}
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe]
    run(*common, "--no-index", env=env)
    run(*common, "--uninstall", env=env)
    after = fake_hermes.read_state(home)
    assert "provider" not in (after.get("memory") or {}), after.get("memory")
    assert after["plugins"]["enabled"] == SYNTH["plugins"]["enabled"], after["plugins"]
    assert not (home / "plugins" / "memorybank").exists()
    _, out = run("--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--uninstall", "--dry-run", env=env)
    assert "dry run" in out
    passed("hermes command: uninstall returns a profile that had no provider to having none")


def s_cli_failures(tmp: Path) -> None:
    # 1. the command works on another folder: refused, nothing changed, the two commands are printed
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    config = home / "config.yaml"
    before_bytes = config.read_bytes()
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe]
    code, out = run(*common, "--no-index", env={"FAKE_HERMES_ROOT": str(root), "FAKE_HERMES_FAIL": "path-mismatch"}, expect=3)
    assert "not on" in out and "plugins enable memorybank" in out and "config set memory.provider memorybank" in out, out
    assert config.read_bytes() == before_bytes and (home / "plugins" / "memorybank" / "plugin.yaml").exists()
    assert install_state(home)["activation"] == "manual" and not install_state(home)["activated"]
    assert not [c for c in calls(root) if c[1:3] in (["config", "set"], ["plugins", "enable"])], "no write went anywhere"
    passed("hermes command pointing at another folder is refused; plugin copied, config untouched, commands printed")

    # 2. `plugins enable` fails
    tmp2 = tmp / "two"
    tmp2.mkdir()
    root, home = make_world(tmp2)
    exe = make_stub(tmp2)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe]
    before = fake_hermes.read_state(home)
    code, out = run(*common, "--no-index", env={"FAKE_HERMES_ROOT": str(root), "FAKE_HERMES_FAIL": "enable"}, expect=3)
    assert "plugins enable" in out and "NOT switched on" in out, out
    assert fake_hermes.read_state(home) == before and (home / "backups").exists()
    passed("hermes command: a failing `plugins enable` leaves config unchanged and says what to do")

    # 3. `config set` fails after the plugin was enabled: exit 3, and an uninstall puts the enable list back
    tmp3 = tmp / "three"
    tmp3.mkdir()
    root, home = make_world(tmp3)
    exe = make_stub(tmp3)
    env = {"FAKE_HERMES_ROOT": str(root)}
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe]
    before = fake_hermes.read_state(home)
    run(*common, "--no-index", env=dict(env, FAKE_HERMES_FAIL="set"), expect=3)
    half = fake_hermes.read_state(home)
    assert "memorybank" in half["plugins"]["enabled"] and "provider" not in (half.get("memory") or {}), half
    assert not install_state(home)["activated"]
    run(*common, "--uninstall", env=env)
    done = fake_hermes.read_state(home)
    assert done["plugins"]["enabled"] == before["plugins"]["enabled"] and "provider" not in (done.get("memory") or {}), done
    passed("hermes command: a half-done switch-on (enabled, provider not set) is reported and fully undone by uninstall")


def s_cli_missing(tmp: Path) -> None:
    root, home = make_world(tmp)
    config = home / "config.yaml"
    before_bytes = config.read_bytes()
    nothing = str(tmp / "no" / "such" / "hermes")
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", nothing]
    code, out = run(*common, "--no-index", expect=3)
    assert "no `hermes` command found" in out and "plugins enable memorybank" in out, out
    assert config.read_bytes() == before_bytes and (home / "plugins" / "memorybank" / "run.py").exists()
    passed("no hermes command: plugin copied, config untouched, the finishing commands printed")

    state = fake_hermes.read_state(home)                         # the owner runs the two printed commands by hand ...
    switched = copy.deepcopy(state)
    switched["memory"] = {"provider": "memorybank"}
    switched["plugins"]["enabled"] = state["plugins"]["enabled"] + ["memorybank"]
    fake_hermes.write_state(home, switched)
    code, out = run(*common, "--uninstall", expect=2)
    assert "Nothing was changed" in out and (home / "plugins" / "memorybank").exists()
    assert fake_hermes.read_state(home) == switched, "the settings still name the plugin, so it stays"
    passed("no hermes command: an uninstall refuses rather than leave settings pointing at a removed plugin")
    fake_hermes.write_state(home, state)                         # ... and the way they were

    code, out = run(*common, "--no-activate", "--no-index", expect=0)   # asked not to switch on: not a problem
    assert install_state(home)["activation"] == "none" and "plugins enable memorybank" in out
    assert config.read_bytes() == before_bytes
    passed("no hermes command: --no-activate and re-installs are harmless")


def s_cli_index_fallback(tmp: Path) -> None:
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    env = {"FAKE_HERMES_ROOT": str(root), "FAKE_HERMES_FAIL": "import-missing"}
    _, out = run("--hermes-root", str(root), "--profile", "demo", "--hermes", exe, env=env)
    assert "indexed without Hermes' content scanner" in out and "everything is in the bank" in out, out
    assert (home / "plugin-data" / "memorybank" / "bank.db").exists()
    passed("hermes command: when `hermes memorybank` is not available the plugin's own runner builds the bank")


def s_files_in_use(tmp: Path) -> None:
    if os.name != "nt":
        SKIPPED.append("open-file scenarios (Windows only: other systems allow moving a folder with open files)")
        return
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    env = {"FAKE_HERMES_ROOT": str(root)}
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe]
    run(*common, "--no-index", env=env)
    plugin = home / "plugins" / "memorybank"
    cfg = fake_hermes.read_state(home)
    handle = open(str(plugin / "__init__.py"), "rb")
    try:
        code, out = run(*common, "--no-index", env=env, expect=1)
        assert "in use" in out and "Nothing was changed" in out, out
        code, out = run(*common, "--uninstall", env=env, expect=1)
        assert "in use" in out and "Nothing was changed" in out, out
    finally:
        handle.close()
    assert (plugin / "__init__.py").exists() and (plugin / "core" / "store.py").exists(), "plugin must be intact"
    assert fake_hermes.read_state(home) == cfg, "no setting may change while the plugin is locked"
    assert len(backups(home)) == 1, "no leftover backup from the refused runs"
    passed("a locked plugin folder stops install and uninstall before anything changes")


# ----------------------------------------------------------------------------- what the second audit found

def s_nothing_a_file_manager_added_is_installed(tmp: Path) -> None:
    here = tmp / "copy"
    # the whole repository (not just the plugin files): what is not a plugin file must stay behind as well
    shutil.copytree(str(ROOT), str(here), ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
    for junk in (".DS_Store", "._store.py", "Thumbs.db", "desktop.ini"):
        (here / junk).write_text("added by a file manager", "utf-8")
        (here / "core" / junk).write_text("added by a file manager", "utf-8")
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    run("--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index",
        env={"FAKE_HERMES_ROOT": str(root)}, installer=here / "install.py")
    installed = home / "plugins" / "memorybank"
    assert (installed / "plugin.yaml").exists()
    stray = [p.name for p in installed.rglob("*") if p.name in (".DS_Store", "Thumbs.db", "desktop.ini") or p.name.startswith("._")]
    assert not stray, stray
    left_in = [name for name in NOT_PLUGIN_TOP if (installed / name).exists()]
    assert not left_in, "not plugin files, yet copied into the profile: %s" % left_in
    assert tree_files(installed) == plugin_tree_files(), "the installed files are not exactly the plugin's"
    passed("what Finder or Explorer leaves in the folder is not copied into the profile, and neither is anything that is not the plugin")


def s_a_lone_installer_never_replaces_the_plugin(tmp: Path) -> None:
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    env = {"FAKE_HERMES_ROOT": str(root)}
    run("--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index", env=env)
    installed = home / "plugins" / "memorybank"
    before = tree_files(installed)
    assert "plugin.yaml" in before, before
    lone = tmp / "lone"
    lone.mkdir()
    shutil.copy2(str(INSTALLER), str(lone / "install.py"))       # what a backup folder holds: the installer, not the plugin
    for extra in ((), ("--dry-run",)):
        code, out = run("--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index", *extra, env=env,
                        installer=lone / "install.py", expect=None)
        assert code == 2 and "plugin source not found" in out, (code, out)
    assert tree_files(installed) == before, "the installed plugin was changed by an installer that holds no plugin"
    passed("an installer without the plugin's files next to it stops and leaves the installed plugin alone")


def s_rollback_commands_are_quoted(tmp: Path) -> None:
    installer = load_installer()
    cfg = copy.deepcopy(SYNTH)
    cfg["memory"] = {"provider": "x; touch pwned #"}           # a hostile value read from config.yaml
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    run("--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index", env={"FAKE_HERMES_ROOT": str(root)})
    note = (backups(home)[-1] / "ROLLBACK.md").read_text("utf-8")
    by_hand = note.split("## By hand", 1)[1].split("## What the install changed", 1)[0]
    flag = "--profile demo "
    assert installer.plain("x; touch pwned #") != "x; touch pwned #"
    assert "hermes %sconfig set memory.provider %s" % (flag, installer.plain("x; touch pwned #")) in by_hand, by_hand
    assert "hermes %splugins disable memorybank" % flag in by_hand, by_hand
    # a profile name that needs quoting is quoted wherever it is printed (main() refuses such a name up front, but
    # the helpers do not rely on that)
    spaced = types.SimpleNamespace(root=False, profile="demo two")
    assert installer.plain("demo two") != "demo two"
    assert installer.profile_flag(spaced) == "--profile %s " % installer.plain("demo two")
    passed("the 'by hand' commands in ROLLBACK.md quote the profile name and the old provider")


def s_a_hermes_on_another_home_is_not_gone_round(tmp: Path) -> None:
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    scripts = root / "hermes-agent" / "venv" / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    (scripts / ("python.exe" if os.name == "nt" else "python")).write_text("", "utf-8")     # a Hermes Python is there
    config = home / "config.yaml"
    before = config.read_bytes()
    code, out = run("--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index",
                    env={"FAKE_HERMES_ROOT": str(root), "FAKE_HERMES_FAIL": "path-mismatch"}, expect=3)
    assert "pass --python" in out and "plugins enable memorybank" in out, out
    assert config.read_bytes() == before, "the config must not be touched by a route nobody asked for"
    passed("a hermes command that works on another home is not gone round unasked (the two commands are printed)")


def s_too_old_a_hermes_is_refused(tmp: Path) -> None:
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    config = home / "config.yaml"
    before = config.read_bytes()
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    code, out = run(*common, env={"FAKE_HERMES_ROOT": str(root), "FAKE_HERMES_VERSION": "v0.9.1"}, expect=2)
    assert "needs Hermes" in out and "Nothing was changed" in out, out
    assert config.read_bytes() == before and not (home / "plugins" / "memorybank").exists() and not (home / "backups").exists()
    run(*common, env={"FAKE_HERMES_ROOT": str(root)})
    run(*common, "--uninstall", env={"FAKE_HERMES_ROOT": str(root), "FAKE_HERMES_VERSION": "v0.9.1"})   # never refused
    assert not (home / "plugins" / "memorybank").exists()
    passed("a Hermes older than the plugin's floor is refused before anything changes; an uninstall never is")


def s_default_means_the_root(tmp: Path) -> None:
    test_core.make_home(tmp)
    root = tmp / "hermes"
    shutil.move(str(tmp / "home"), str(root))
    fake_hermes.write_state(root, copy.deepcopy(SYNTH))
    exe = make_stub(tmp)
    code, out = run("--hermes-root", str(root), "--profile", "default", "--hermes", exe, "--no-index",
                    env={"FAKE_HERMES_ROOT": str(root)})
    assert "same as --root" in out, out
    assert fake_hermes.read_state(root)["memory"]["provider"] == "memorybank"
    assert (root / "plugins" / "memorybank" / "plugin.yaml").exists()
    passed("--profile default is the Hermes root, as `hermes profile list` calls it")


def s_python_route_is_read_back(tmp: Path) -> None:
    installer = load_installer()
    root, home = make_world(tmp)
    config = home / "config.yaml"

    def arguments():
        return types.SimpleNamespace(profile="demo", root=False, dry_run=False, no_activate=False, no_index=True,
                                     agent=tmp, purge=False, uninstall=False)

    def attempt(writes: bool):
        def edit(python, agent_dir, target, action, restore=""):
            if writes:
                fake_hermes.write_state(target, dict(copy.deepcopy(SYNTH), memory={"provider": "memorybank"},
                                                     plugins={"enabled": SYNTH["plugins"]["enabled"] + ["memorybank"], "disabled": []}))
            return {"provider": None, "had_memory_section": False, "enabled": list(SYNTH["plugins"]["enabled"]),
                    "disabled": [], "config": str(config)}
        installer.edit_config = edit
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = installer.install(arguments(), root, home, None, "no hermes command", Path(sys.executable))
        return code, out.getvalue()

    code, text = attempt(False)                     # the writer says "done" and changes nothing
    assert code == 3 and "NOT verified" in text and "Installed, but" in text, text
    assert not install_state(home)["activated"]
    code, text = attempt(True)
    assert code == 0 and "verified: config.yaml has memory.provider" in text, text
    assert install_state(home)["activated"]
    passed("Hermes' config writer is not taken at its word: config.yaml is read back, and a miss exits 3")


def s_uninstall_never_deletes_the_plugin(tmp: Path) -> None:
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    env = {"FAKE_HERMES_ROOT": str(root)}
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe]
    run(*common, "--no-index", env=env)
    plugin = home / "plugins" / "memorybank"
    (plugin / "LOCAL_EDIT.txt").write_text("my own change", "utf-8")
    before = fake_hermes.read_state(home)
    installer = load_installer()
    real_rename, saved_env = os.rename, os.environ.get("FAKE_HERMES_ROOT")
    os.environ["FAKE_HERMES_ROOT"] = str(root)

    def failing(first):
        """From the ``first``-th rename on, every rename is refused (the installer retries a refusal for a few seconds)."""
        count = {"n": 0}

        def rename(src, dst, *args, **kwargs):
            count["n"] += 1
            if count["n"] >= first:
                raise PermissionError("simulated: something has it open")
            return real_rename(src, dst, *args, **kwargs)
        return rename

    def uninstall():
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = installer.main(list(common) + ["--uninstall"])
        return code, out.getvalue()

    try:
        os.rename = failing(1)                      # the first move fails: nothing has moved
        code, text = uninstall()
        assert code == 1 and "in use" in text and "Nothing was changed" in text, text
        assert (plugin / "LOCAL_EDIT.txt").exists() and fake_hermes.read_state(home) == before
        assert not [p for p in (home / "backups").iterdir() if "removed" in p.name], "no leftover backup"
        os.rename = failing(2)                      # the way back fails: the plugin is in the backup and stays there
        code, text = uninstall()
        assert code == 1 and "could not be moved back" in text, text
        kept = [p for p in (home / "backups").iterdir() if "removed" in p.name]
        assert kept and (kept[0] / "plugin" / "LOCAL_EDIT.txt").exists(), "the user's own edits must survive"
        assert fake_hermes.read_state(home) == before, "no setting changed"
    finally:
        os.rename = real_rename
        if saved_env is None:
            os.environ.pop("FAKE_HERMES_ROOT", None)
        else:
            os.environ["FAKE_HERMES_ROOT"] = saved_env
    passed("an uninstall that cannot move the plugin back keeps it in the backup instead of deleting it")


def s_a_failed_switch_off_is_finished_by_running_it_again(tmp: Path) -> None:
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    env = {"FAKE_HERMES_ROOT": str(root)}
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe]
    before = fake_hermes.read_state(home)
    run(*common, "--no-index", env=env)
    code, out = run(*common, "--uninstall", env=dict(env, FAKE_HERMES_FAIL="disable"), expect=1)
    assert "Run the same command again" in out, out
    half = fake_hermes.read_state(home)
    assert "provider" not in (half.get("memory") or {}) and "memorybank" in half["plugins"]["enabled"], half
    assert (home / "plugins" / "memorybank").exists(), "the plugin folder stays until the settings are back"
    run(*common, "--uninstall", env=env)
    done = fake_hermes.read_state(home)
    assert done["plugins"]["enabled"] == before["plugins"]["enabled"] and "provider" not in (done.get("memory") or {}), done
    assert not (home / "plugins" / "memorybank").exists()
    passed("an uninstall that stopped half-way says so, and running it again finishes the job")


def s_warnings_name_the_profile(tmp: Path) -> None:
    installer = load_installer()
    installer.run_tool = lambda *args, **kwargs: (1, "boom")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        complete = installer.index_bank(None, tmp, tmp, False, "--profile demo ")
    text = out.getvalue()
    assert complete is False
    assert "indexing did not finish. Run: hermes --profile demo memorybank import" in text, text
    assert "Run: hermes --profile demo memorybank import --force   then   hermes --profile demo memorybank check" in text, text
    passed("the indexing warnings name the profile in the commands they suggest")


def s_units_more(tmp: Path) -> None:
    installer = load_installer()
    path = tmp / "disabled.yaml"
    path.write_text("plugins:\n  enabled: [a]\n  disabled:\n    - memorybank\n    - other # note\nmemory:\n  provider: x\n", "utf-8")
    got = installer.scan_config(path)
    assert got["enabled"] == ["a"] and got["disabled"] == ["memorybank", "other"] and got["provider"] == "x", got
    assert installer.parse_version("Hermes Agent v0.21.5+7363 (2026.9.24)") == (0, 21, 5)
    assert installer.parse_version(">=0.21.5") == (0, 21, 5) and installer.parse_version("Hermes 2026.9.24") == (2026, 9, 24)
    assert installer.parse_version("Hermes Agent 0.9.1") == (0, 9, 1), "the v is optional"
    assert installer.parse_version("hermes-3.11 v0.21.5") == (0, 21, 5), "a v-prefixed version wins over a bare one"
    assert installer.parse_version("") is None and installer.parse_version("v1.2") == (1, 2)
    assert installer.parse_version("Hermes Agent (development build)") is None and installer.parse_version("v7") is None
    assert installer.required_hermes() is not None
    assert installer.plain("abc-1.x_y") == "abc-1.x_y" and installer.plain("a b") in ('"a b"', "'a b'")
    assert "`" not in installer.plain("a`b\nc") and "\n" not in installer.plain("a`b\nc")

    class Folder:
        def __init__(self, uid):
            self.uid = uid

        def stat(self):
            return types.SimpleNamespace(st_uid=self.uid)

        def __str__(self):
            return "/profile"

    had, saved = hasattr(os, "geteuid"), getattr(os, "geteuid", None)
    os.geteuid = lambda: 0
    try:
        assert "another user" in installer.owner_conflict(Folder(1000)), "root on somebody else's profile is stopped"
        assert installer.owner_conflict(Folder(0)) is None
        os.geteuid = lambda: 1000
        assert installer.owner_conflict(Folder(1000)) is None
    finally:
        if had:
            os.geteuid = saved
        else:
            del os.geteuid
    wrapped = ["C:\\wrapped\\fol", "der\\config.yaml"] if os.name == "nt" else ["/wrapped/fol", "der/config.yaml"]
    command = installer.Hermes("hermes", None)
    command.run = lambda *args, **kwargs: (0, "\n".join(wrapped), "")
    assert command.config_file() == Path("".join(wrapped)), "a path wrapped over two lines is put back together"

    # a folder a virus scanner holds for a moment is let go of; one that stays held is "in use"
    source, target = tmp / "moving", tmp / "moved"
    source.mkdir()
    real_rename, real_sleep, calls = os.rename, time.sleep, {"n": 0}

    def held_twice(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("held for a moment by a scanner")
        return real_rename(src, dst)

    os.rename, time.sleep = held_twice, (lambda seconds: None)
    try:
        if installer.IS_WINDOWS:
            installer.move(source, target)
            assert target.is_dir() and not source.exists() and calls["n"] == 3, calls
        else:                                                    # off Windows a refusal is real: no waiting
            try:
                installer.move(source, target)
                raise AssertionError("a refusal off Windows must be raised at once")
            except PermissionError:
                assert calls["n"] == 1, calls
        os.rename = lambda src, dst: (_ for _ in ()).throw(PermissionError("held for good"))
        try:
            installer.move(target if installer.IS_WINDOWS else source, tmp / "never")
            raise AssertionError("a refusal that lasts must be raised")
        except PermissionError:
            pass
        os.rename = lambda src, dst: (_ for _ in ()).throw(FileNotFoundError("gone"))
        try:
            installer.move(tmp / "absent", tmp / "never")
            raise AssertionError("anything but a permission error is raised without waiting")
        except FileNotFoundError:
            pass
    finally:
        os.rename, time.sleep = real_rename, real_sleep
    passed("units: disabled list, version floor, quoting, root-on-someone-else's-profile, wrapped paths, patient rename")


# ----------------------------------------------------------------------------- units

def load_installer():
    spec = importlib.util.spec_from_file_location("mb_installer", str(INSTALLER))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def s_units(tmp: Path) -> None:
    installer = load_installer()
    scan = installer.scan_config
    cases = {
        "a": ("memory:\n  provider: honcho   # note\nplugins:\n  enabled:\n    - one\n    - two # x\n  disabled: []\n",
              "honcho", ["one", "two"], True),
        "b": ("plugins:\n  enabled:\n  - one\n  - two\n  disabled:\n  - three\nmemory:\n  provider: \"mem0\"\n",
              "mem0", ["one", "two"], True),
        "c": ("plugins:\n  enabled: [one, 'two']\n", None, ["one", "two"], False),
        "d": ("model:\n  default: x\n", None, [], False),
        "e": ("memory:\n  provider:\n", None, [], True),
    }
    for name, (text, provider, enabled, had) in cases.items():
        path = tmp / ("scan-%s.yaml" % name)
        path.write_text(text, "utf-8")
        got = scan(path)
        assert (got["provider"], got["enabled"], got["had_memory_section"]) == (provider, enabled, had), (name, got)
    assert scan(tmp / "missing.yaml")["enabled"] == []

    saved = {k: os.environ.get(k) for k in ("HERMES_HOME", "HERMES_DATA_DIR_SUFFIX")}
    try:
        os.environ.pop("HERMES_DATA_DIR_SUFFIX", None)
        os.environ.pop("HERMES_HOME", None)
        native = installer.default_root()
        os.environ["HERMES_HOME"] = str(native / "profiles" / "p")
        assert installer.default_root() == native, "a profile under the standard home resolves to the standard root"
        os.environ["HERMES_HOME"] = str(tmp / "custom" / "profiles" / "p")
        assert installer.default_root() == tmp / "custom", "a profile of a custom root resolves to that root"
        os.environ["HERMES_HOME"] = str(tmp / "plainhome")
        assert installer.default_root() == tmp / "plainhome"
        os.environ["HERMES_HOME"] = ""
        assert installer.default_root() == native
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    fake_root = tmp / "frog"
    (fake_root / "bin").mkdir(parents=True)
    exe = fake_root / "bin" / ("hermes.exe" if os.name == "nt" else "hermes")
    exe.write_text("x", "utf-8")
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = str(tmp / "emptybin")
    try:
        assert installer.find_hermes(fake_root, None) == str(exe), "the root's own bin folder is searched"
        assert installer.find_hermes(fake_root, str(tmp / "nope")) is None
    finally:
        os.environ["PATH"] = old
    assert installer.quote("a b") in ('"a b"', "'a b'")
    passed("units: config scanning (inline/block lists, comments), Hermes root rules, hermes command lookup")

    source = INSTALLER.read_text("utf-8")
    ast.parse(source, feature_version=(3, 8))
    for marker in (".removeprefix(", ".removesuffix(", " | None", "list[str]", "dict[str", "tuple[", "is_relative_to("):
        assert marker not in source, "3.9+ construct in install.py: " + marker
    passed("units: install.py parses as Python 3.8 and avoids 3.9+ library calls")


# ----------------------------------------------------------------------------- through Hermes' own Python (fallback)

def load_yaml(path: Path) -> Dict:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}


YAML_SYNTH = (
    "model:\n  default: some/model\n  provider: nous\nagent:\n  max_turns: 500\n"
    "skills:\n  auto_load:\n    - skill-a\n"
    "plugins:\n  enabled:\n    - plugin-a\n    - plugin-b\n    - plugin-c\n  disabled: []\n"
    "auxiliary:\n  compression:\n    provider: nous\n"
)


def py_common(root: Path, name: str) -> List[str]:
    return ["--hermes-root", str(root), "--profile", name, "--no-cli", "--python", sys.executable, "--agent-dir", str(AGENT)]


def s_python_route(tmp: Path, real: Optional[Path]) -> None:
    if not python_route_available():
        SKIPPED.append("Hermes-Python fallback (needs PyYAML and a Hermes source folder: set HERMES_AGENT_DIR)")
        return
    test_core.make_home(tmp)
    hermes_root = tmp / "hermes"
    home = hermes_root / "profiles" / "demo"
    home.parent.mkdir(parents=True)
    shutil.move(str(tmp / "home"), str(home))
    config = home / "config.yaml"
    if real and real.exists():
        shutil.copy2(str(real), str(config))
        print("      (using a copy of a real profile config; contents not shown)")
    else:
        config.write_text(YAML_SYNTH, encoding="utf-8")
    original = config.read_bytes()
    before = load_yaml(config)
    common = py_common(hermes_root, "demo")

    _, out = run(*common, "--dry-run")
    assert "dry run" in out and config.read_bytes() == original and not (home / "backups").exists()
    _, out = run(*common)
    assert "everything is in the bank" in out, out
    after = load_yaml(config)
    assert after["memory"]["provider"] == "memorybank", after.get("memory")
    already = list((before.get("plugins") or {}).get("enabled") or [])
    expected = already + ([] if "memorybank" in already else ["memorybank"])     # a config may be installed already
    assert after["plugins"]["enabled"] == expected, after["plugins"]
    untouched(before, after)
    (backup,) = [p for p in backups(home)]
    assert (backup / "config.yaml").read_bytes() == original, "backup must be byte-identical"
    assert install_state(home)["activation"] == "python"
    run(*common, "--no-index")
    assert load_yaml(config)["plugins"]["enabled"] == expected, "no duplicate entry on re-install"
    run(*common, "--uninstall")
    restored = load_yaml(config)
    assert (restored.get("plugins") or {}).get("enabled") == [p for p in expected if p != "memorybank"], restored.get("plugins")
    assert (restored.get("memory") or {}).get("provider") in (None, "", (before.get("memory") or {}).get("provider"))
    untouched(before, restored)
    code, out = run("--hermes-root", str(hermes_root), "--profile", "nope", "--no-cli", expect=2)
    assert "not found" in out
    passed("Hermes-Python fallback: install, re-install, uninstall restore the settings (%d other sections identical)" % len(before))

    tmp2 = tmp / "second"
    tmp2.mkdir()
    test_core.make_home(tmp2)
    root = tmp2 / "hermes two"                               # a space in the folder name: every path is quoted
    home = root / "profiles" / "demo"
    home.parent.mkdir(parents=True)
    shutil.move(str(tmp2 / "home"), str(home))
    config = home / "config.yaml"
    config.write_text(YAML_SYNTH + "memory:\n  provider: honcho\n", encoding="utf-8")
    before = load_yaml(config)
    common = py_common(root, "demo")
    run(*common, "--no-index")
    run(*common, "--no-index")
    assert install_state(home)["previous"]["provider"] == "honcho"
    note = backups(home)[-1] / "ROLLBACK.md"
    text = note.read_text("utf-8")
    assert "`honcho`" in text and str(root) in text and "hermes two" in text, text[:600]
    done = subprocess.run(rollback_command(note), shell=True, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, (done.stdout, done.stderr)
    restored = load_yaml(config)
    assert restored["memory"]["provider"] == "honcho" and restored["plugins"]["enabled"] == before["plugins"]["enabled"]
    passed("Hermes-Python fallback: a profile with another provider is restored by the ROLLBACK.md command")


def s_run_tool(tmp: Path) -> None:
    installer = load_installer()
    fake = tmp / "fakeplugin"
    fake.mkdir()
    (fake / "run.py").write_text("import sys, time\nif 'import' in sys.argv:\n    print('boom')\n    sys.exit(3)\ntime.sleep(5)\n", "utf-8")
    code, text = installer.run_tool(Path(sys.executable), fake, tmp, "import")
    assert code == 3 and "boom" in text, (code, text)
    code, text = installer.run_tool(Path(sys.executable), fake, tmp, "check", timeout=0.5)
    assert code == -1 and "timed out" in text, (code, text)
    passed("indexing failures and timeouts come back as failures")


# ----------------------------------------------------------------------------- the review fixes of 1.3.0

def args_for(tmp: Path, **extra) -> types.SimpleNamespace:
    """The parsed arguments of a run, for calling install() / uninstall() inside this process."""
    base = dict(profile="demo", root=False, dry_run=False, no_activate=False, no_index=True, agent=tmp, purge=False,
                uninstall=False, no_cli=False, agent_dir=None)
    base.update(extra)
    return types.SimpleNamespace(**base)


def run_fake(root: Path, args, fail: str = "") -> Tuple[int, str, str]:
    """tests/fake_hermes.py run inside this process (no program is started): (exit code, stdout, stderr)."""
    keep = {key: os.environ.get(key) for key in ("FAKE_HERMES_ROOT", "FAKE_HERMES_FAIL")}
    os.environ["FAKE_HERMES_ROOT"], os.environ["FAKE_HERMES_FAIL"] = str(root), fail
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fake_hermes.main(list(args))
    finally:
        for key, value in keep.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return code, out.getvalue().strip(), err.getvalue().strip()


@contextlib.contextmanager
def fast_hermes(installer, root: Path, fail: str = ""):
    """Answer the installer's `hermes` calls with the stand-in running inside this process instead of starting a
    program per call (for the scenarios that are about the installer's own logic, not about starting programs)."""
    real_run = installer.Hermes.run

    def run_here(self, *args, timeout=180.0):
        return run_fake(root, (["--profile", self.profile] if self.profile else []) + list(args), fail)

    installer.Hermes.run = run_here
    try:
        yield
    finally:
        installer.Hermes.run = real_run


@contextlib.contextmanager
def in_env(**values: str):
    """Set environment variables for the duration of the block (for runs that happen inside this process)."""
    keep = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in keep.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def call_main(installer, argv) -> Tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = installer.main(list(argv))
    return code, out.getvalue()


def same(a, b) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def s_the_default_profile_is_always_named_to_hermes(tmp: Path) -> None:
    """After `hermes profile use work` a call without --profile acts on 'work': the root has to be named."""
    installer = load_installer()
    root, work = make_root_world(tmp, sticky="work")
    exe = make_stub(tmp)
    env = {"FAKE_HERMES_ROOT": str(root)}
    common = ["--hermes-root", str(root), "--root", "--hermes", exe]
    before_work = fake_hermes.read_state(work)
    with fast_hermes(installer, root):
        code, out = call_main(installer, common + ["--dry-run"])
        assert code == 0 and "Hermes' own commands" in out, out   # the hermes route is usable although 'work' is sticky
        code, out = call_main(installer, common + ["--no-index"])
        assert code == 0, out
    cfg = fake_hermes.read_state(root)
    assert cfg["memory"]["provider"] == "memorybank" and "memorybank" in cfg["plugins"]["enabled"], cfg
    assert fake_hermes.read_state(work) == before_work, "the sticky profile is not the one that was installed"
    assert {c[0] for c in calls(root)} == {"default"}, "every call to hermes names the profile it means"
    note = backups(root)[-1] / "ROLLBACK.md"
    text = note.read_text("utf-8")
    assert "hermes --profile default config unset memory.provider" in text, text
    assert "hermes --profile default plugins disable memorybank" in text, text
    done = subprocess.run(rollback_command(note), shell=True, capture_output=True, text=True, timeout=300,
                          env=dict(os.environ, **env), stdin=subprocess.DEVNULL)
    assert done.returncode == 0, (done.stdout, done.stderr)
    after = fake_hermes.read_state(root)
    assert "provider" not in (after.get("memory") or {}) and "memorybank" not in after["plugins"]["enabled"], after
    assert not (root / "plugins" / "memorybank").exists() and fake_hermes.read_state(work) == before_work
    assert {c[0] for c in calls(root)} == {"default"}
    seen = len(calls(root))                                    # a named profile never depended on the sticky choice
    with fast_hermes(installer, root):
        code, out = call_main(installer, ["--hermes-root", str(root), "--profile", "work", "--hermes", exe, "--no-index"])
        assert code == 0, out
    assert {c[0] for c in calls(root)[seen:]} == {"work"}
    assert fake_hermes.read_state(work)["memory"]["provider"] == "memorybank" and fake_hermes.read_state(root) == after
    passed("the default profile is always named to hermes (--profile default): a sticky `profile use` cannot redirect it")


def s_the_stand_in_follows_hermes_rules(tmp: Path) -> None:
    root, work = make_root_world(tmp)
    (root / "plugins" / "memorybank").mkdir(parents=True)
    (root / "plugins" / "memorybank" / "plugin.yaml").write_text("name: memorybank\n", "utf-8")

    def fake(*args: str, fail: str = "") -> Tuple[int, str, str]:
        return run_fake(root, args, fail)

    (root / "active_profile").write_text("work\n", encoding="utf-8")       # `hermes profile use work`
    assert fake("config", "path")[1] == str(work / "config.yaml"), "no flag: the sticky choice decides"
    assert fake("--profile", "default", "config", "path")[1] == str(root / "config.yaml")
    assert fake("--profile", "WORK", "config", "path")[1] == str(work / "config.yaml"), "names are lowercased"
    (root / "active_profile").write_text("default\n", encoding="utf-8")
    assert fake("config", "path")[1] == str(root / "config.yaml")
    assert not (root / "profiles" / "default").exists(), "`default` is the root, not a folder under profiles/"
    code, _, err = fake("--profile", "two words", "--version")
    assert code == 2 and "profile list" in err, (code, err)

    def status(cfg) -> str:
        fake_hermes.write_state(root, cfg)
        return json.loads(fake("--profile", "default", "plugins", "list", "--json")[1])[0]["status"]

    assert status({}) == "not enabled"
    assert status({"plugins": {"enabled": ["memorybank"]}}) == "enabled"
    assert status({"plugins": {"enabled": ["memorybank"], "disabled": ["memorybank"]}}) == "disabled"
    assert status({"memory": {"provider": "memorybank"}}) == "enabled"
    assert status({"memory": {"provider": "memorybank"}, "plugins": {"disabled": ["memorybank"]}}) == "disabled"
    cfg = {"memory": {"provider": "honcho"}}                    # a managed installation: said on stderr, exit 0, nothing written
    fake_hermes.write_state(root, cfg)
    code, _, err = fake("--profile", "default", "config", "set", "memory.provider", "x", fail="managed")
    assert code == 0 and "managed" in err and fake_hermes.read_state(root) == cfg, (code, err)
    code, _, err = fake("--profile", "default", "config", "unset", "memory.provider", fail="managed")
    assert code == 0 and "managed" in err and fake_hermes.read_state(root) == cfg, (code, err)
    passed("the stand-in follows Hermes' rules: sticky profile, --profile default, disabled wins, managed mode")


def s_a_second_installer_is_turned_away(tmp: Path) -> None:
    installer = load_installer()
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    lock, config = lock_file(home), home / "config.yaml"
    before = config.read_bytes()
    with fast_hermes(installer, root):
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({"pid": 4242, "what": "install"}), "utf-8")          # somebody else's, fresh
        code, out = call_main(installer, common)
        assert code == 1 and "another install or uninstall" in out and str(lock) in out, out
        assert "delete" in out and "Nothing was changed" in out, out
        assert lock.exists() and json.loads(lock.read_text("utf-8"))["pid"] == 4242, "somebody else's lock stays"
        assert config.read_bytes() == before and not (home / "plugins" / "memorybank").exists()
        assert not (home / "backups").exists()
        code, out = call_main(installer, common + ["--dry-run"])
        assert code == 0 and "dry run" in out, "a dry run changes nothing and needs no lock"
        lock.unlink()
        assert call_main(installer, common)[0] == 0 and not lock.exists(), "the lock is gone when the run ends"
        lock.write_text(json.dumps({"pid": 4242, "what": "install"}), "utf-8")
        code, out = call_main(installer, common + ["--uninstall"])
        assert code == 1 and "another install or uninstall" in out and (home / "plugins" / "memorybank").exists(), out
        lock.unlink()
        assert call_main(installer, common + ["--uninstall"])[0] == 0 and not lock.exists()
    passed("a second installer on the same profile is turned away and told which lock file to delete")


def s_a_stale_lock_is_taken_over(tmp: Path) -> None:
    installer = load_installer()
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    lock = lock_file(home)
    lock.parent.mkdir(parents=True)

    def leave_lock(minutes: int) -> None:
        lock.write_text(json.dumps({"pid": 4242, "what": "install"}), "utf-8")
        old = time.time() - minutes * 60
        os.utime(str(lock), (old, old))

    with fast_hermes(installer, root):
        leave_lock(10)                                          # ten minutes: still somebody's
        code, out = call_main(installer, common)
        assert code == 1 and "another install or uninstall" in out and lock.exists(), out
        leave_lock(20)                                          # twenty: a run that was killed
        code, out = call_main(installer, common)
        assert code == 0 and "taken over" in out and str(lock) in out, out
        assert not lock.exists() and (home / "plugins" / "memorybank" / "plugin.yaml").exists()
    passed("a lock left by a run that died (older than 15 minutes) is taken over with a notice")


def s_the_lock_waits_out_a_file_that_is_being_deleted(tmp: Path) -> None:
    """Windows refuses to make a file again for a moment after another run deleted it ("delete pending"): that is not
    a second installer, and is not worth an error either."""
    installer = load_installer()
    real_open, real_sleep, seen = os.open, time.sleep, {"n": 0, "always": False}

    def flaky(path, flags, mode=0o777, *a, **k):
        if str(path).endswith(".memorybank-install.lock"):
            seen["n"] += 1
            if seen["always"] or seen["n"] <= 2:
                raise PermissionError(13, "simulated: the file is being deleted")
        return real_open(path, flags, mode, *a, **k)

    os.open, time.sleep = flaky, (lambda seconds: None)
    try:
        lock = installer.InstallLock(tmp / "one", "install")
        assert lock.acquire() == "" and seen["n"] == 3, seen
        lock.release()
        seen["always"] = True                                    # a refusal that does not go away is an error, said as such
        text = installer.InstallLock(tmp / "two", "install").acquire()
        assert "cannot create the lock file" in text and "another install" not in text, text
    finally:
        os.open, time.sleep = real_open, real_sleep
    passed("a lock file that cannot be made for a moment (delete pending) is retried, then reported as what it is")


def s_the_lock_never_outlives_a_run(tmp: Path) -> None:
    installer = load_installer()
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    lock = lock_file(home)
    held: List[bool] = []
    with fast_hermes(installer, root, fail="enable"):
        inner = installer.Hermes.run

        def watching(self, *args, **kwargs):                    # is the lock there while the run is working?
            held.append(lock.exists())
            return inner(self, *args, **kwargs)

        installer.Hermes.run = watching
        code, out = call_main(installer, common)
        assert code == 3 and not lock.exists(), out
        assert any(held), "the lock is held while the run works"
    with fast_hermes(installer, root):
        assert call_main(installer, common)[0] == 0
    with fast_hermes(installer, root, fail="disable"):
        code, out = call_main(installer, common + ["--uninstall"])
        assert code == 1 and not lock.exists(), out
    real = installer.write_rollback
    installer.write_rollback = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
    try:
        with fast_hermes(installer, root):
            try:
                call_main(installer, common)
                raise AssertionError("the simulated failure must reach the caller")
            except RuntimeError:
                pass
    finally:
        installer.write_rollback = real
    assert not lock.exists(), "an error in the middle of a run still removes the lock"
    passed("the lock is removed whatever way a run ends (done, stopped, or an unexpected error)")


def s_two_installers_at_once_never_leave_a_broken_plugin(tmp: Path) -> None:
    expected = plugin_tree_files()
    for round_ in ("fresh", "again"):
        sub = tmp / round_
        sub.mkdir()
        root, home = make_world(sub)
        exe = make_stub(sub)
        env = dict(os.environ, FAKE_HERMES_ROOT=str(root))
        common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
        if round_ == "again":
            installer = load_installer()
            with fast_hermes(installer, root):
                assert call_main(installer, common)[0] == 0
        procs = [subprocess.Popen([sys.executable, str(INSTALLER), *common], stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, env=env, stdin=subprocess.DEVNULL)
                 for _ in range(2)]
        outs = [proc.communicate(timeout=600)[0] for proc in procs]
        codes = [proc.returncode for proc in procs]
        have = tree_files(home / "plugins" / "memorybank") if (home / "plugins" / "memorybank").exists() else []
        assert have == expected, (round_, codes, sorted(set(expected) - set(have)), outs)
        assert 0 in codes and set(codes) <= {0, 1}, (round_, codes, outs)
        for code, out in zip(codes, outs):
            if code == 1:
                assert "another install or uninstall" in out, out
        cfg = fake_hermes.read_state(home)
        assert cfg["memory"]["provider"] == "memorybank" and "memorybank" in cfg["plugins"]["enabled"], cfg
        assert not lock_file(home).exists() and not list((home / "plugins").glob(".memorybank*")), "nothing left behind"
    passed("two installers started at the same moment: one wins, the other is turned away, the plugin is complete")


def s_the_new_copy_is_staged_beside_the_old_and_checked(tmp: Path) -> None:
    installer = load_installer()
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    plugin = home / "plugins" / "memorybank"
    real_copytree, real_move = shutil.copytree, installer.move
    seen: Dict = {}
    with fast_hermes(installer, root):
        assert call_main(installer, common)[0] == 0
        (plugin / "LOCAL_EDIT.txt").write_text("my own change", "utf-8")

        def watching(src, dst, *a, **k):                        # (copytree calls itself for every sub-folder)
            if "dst" not in seen:
                seen["dst"], seen["old_in_place"] = Path(dst), (plugin / "LOCAL_EDIT.txt").exists()
            return real_copytree(src, dst, *a, **k)

        def losing(src, dst, *a, **k):                          # a copy that quietly loses a file
            result = real_copytree(src, dst, *a, **k)
            if Path(dst).name.startswith(".memorybank"):
                (Path(dst) / "core" / "store.py").unlink()
            return result

        def damaging(src, dst):                                 # the folder changes while it is being put in place
            real_move(src, dst)
            if Path(src).name.startswith(".memorybank") and same(dst, plugin):
                (plugin / "run.py").unlink()

        try:
            shutil.copytree = watching
            code, text = call_main(installer, common)
            assert code == 0, text
            assert seen["old_in_place"], "the old copy stays in place while the new one is being copied"
            assert seen["dst"].parent == plugin.parent and not same(seen["dst"], plugin), seen
            assert not list(plugin.parent.glob(".memorybank*")), "no staging folder is left behind"
            assert (plugin / "plugin.yaml").exists() and not (plugin / "LOCAL_EDIT.txt").exists()

            (plugin / "LOCAL_EDIT.txt").write_text("my own change", "utf-8")
            before = fake_hermes.read_state(home)
            shutil.copytree = losing
            made = len(backups(home))
            code, text = call_main(installer, common)
            assert code == 1 and "Done." not in text and "does not match" in text and "store.py" in text, text
            assert "while copying the plugin" in text and "Nothing was changed" in text, "caught before the swap: " + text
            assert (plugin / "LOCAL_EDIT.txt").exists() and (plugin / "core" / "store.py").exists(), "old copy untouched"
            assert len(backups(home)) == made, "the old copy was never moved aside, so no backup folder is left"
            assert not list(plugin.parent.glob(".memorybank*")) and fake_hermes.read_state(home) == before
            assert not lock_file(home).exists()

            shutil.copytree, installer.move = real_copytree, damaging
            code, text = call_main(installer, common)
            assert code == 1 and "Done." not in text and "run.py" in text, text
            assert (plugin / "LOCAL_EDIT.txt").exists() and (plugin / "run.py").exists(), "the previous copy is back"
            assert fake_hermes.read_state(home) == before
        finally:
            shutil.copytree, installer.move = real_copytree, real_move
    passed("the new copy is staged beside the old one, compared with the source and swapped in; a bad copy is undone")


def s_purge_probes_the_bank_before_anything_changes(tmp: Path) -> None:
    installer = load_installer()
    cfg = copy.deepcopy(SYNTH)
    cfg["memory"] = {"provider": "honcho"}
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    data, plugin = home / "plugin-data" / "memorybank", home / "plugins" / "memorybank"
    real_rename, real_sleep = os.rename, time.sleep

    def removed() -> List[Path]:
        return sorted(p for p in (home / "backups").iterdir() if "removed" in p.name)

    def refusing(after_plugin_moved: bool):
        def rename(src, dst, *a, **k):
            moved = bool(list((home / "backups").glob("memorybank-removed-*/plugin")))
            if same(src, data) and (moved or not after_plugin_moved):
                raise PermissionError("simulated: something has the bank open")
            return real_rename(src, dst, *a, **k)
        return rename

    with fast_hermes(installer, root):
        assert call_main(installer, common)[0] == 0
        (data / "bank.db").write_bytes(b"not really a bank")
        (home / "memorybank.json").write_text('{"recall": {}}', "utf-8")
        installed = fake_hermes.read_state(home)
        time.sleep = lambda seconds: None
        try:
            os.rename = refusing(False)                        # open from the start: nothing may change
            code, out = call_main(installer, common + ["--uninstall", "--purge"])
            assert code == 1 and "in use" in out and "Nothing was changed" in out, out
            assert fake_hermes.read_state(home) == installed and plugin.exists() and (data / "bank.db").exists()
            assert (home / "memorybank.json").exists() and not removed(), "no leftover backup, no copy of the bank"
            os.rename = refusing(True)                         # busy only after the settings are back: not "Done"
            code, out = call_main(installer, common + ["--uninstall", "--purge"])
            assert code == 3 and "Done." not in out and "WITH --purge" in out, out
            (gone,) = removed()
            assert (gone / "plugin" / "plugin.yaml").exists() and not (gone / "data").exists(), "no duplicate bank"
            assert (data / "bank.db").exists() and (data / "install.json").exists(), "the bank was left whole"
            now = fake_hermes.read_state(home)
            assert now["memory"]["provider"] == "honcho" and "memorybank" not in now["plugins"]["enabled"], now
        finally:
            os.rename, time.sleep = real_rename, real_sleep
        code, out = call_main(installer, common + ["--uninstall", "--purge"])      # what had it open is gone
        assert code == 0 and "Done." in out, out
        (kept,) = [p for p in removed() if (p / "data").exists()]
        assert (kept / "data" / "bank.db").read_bytes() == b"not really a bank" and (kept / "memorybank.json").exists()
        assert not data.exists() and not (home / "memorybank.json").exists()
    passed("--purge probes the bank like the plugin folder; a bank that becomes busy later is not 'Done' (exit 3)")


def s_purge_does_not_take_the_bank_from_a_plugin_that_stays(tmp: Path) -> None:
    installer = load_installer()
    cfg = copy.deepcopy(SYNTH)
    cfg["memory"] = {"provider": "honcho"}
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    plugin, data = home / "plugins" / "memorybank", home / "plugin-data" / "memorybank"
    real_rename, real_sleep, seen = os.rename, time.sleep, {"n": 0}

    def rename(src, dst, *a, **k):                              # the plugin folder can be probed, but not moved for good
        if same(src, plugin):
            seen["n"] += 1
            if seen["n"] >= 2:
                raise PermissionError("simulated: something has the plugin folder open")
        return real_rename(src, dst, *a, **k)

    with fast_hermes(installer, root):
        assert call_main(installer, common)[0] == 0
        (data / "bank.db").write_bytes(b"not really a bank")
        os.rename, time.sleep = rename, lambda seconds: None
        try:
            code, out = call_main(installer, common + ["--uninstall", "--purge"])
        finally:
            os.rename, time.sleep = real_rename, real_sleep
    assert code == 3 and "Done." not in out and "plugin folder" in out and "in use" in out, out
    assert plugin.exists() and (data / "bank.db").exists(), "the bank stays with the plugin that is still installed"
    (gone,) = backups(home, "memorybank-removed")
    assert not (gone / "data").exists() and not (gone / "plugin").exists()
    assert fake_hermes.read_state(home)["memory"]["provider"] == "honcho", "the settings were put back"
    passed("a plugin folder that cannot be moved is not left without its bank: --purge waits, exit 3, no 'Done'")


def s_a_bank_that_is_open_stops_a_purge(tmp: Path) -> None:
    if os.name != "nt":
        SKIPPED.append("open-bank purge scenario (Windows only: other systems allow moving a folder with open files)")
        return
    installer = load_installer()
    cfg = copy.deepcopy(SYNTH)
    cfg["memory"] = {"provider": "honcho"}
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    data, real_sleep = home / "plugin-data" / "memorybank", time.sleep
    with fast_hermes(installer, root):
        assert call_main(installer, common)[0] == 0
        (data / "bank.db").write_bytes(b"not really a bank")
        installed = fake_hermes.read_state(home)
        handle = open(str(data / "bank.db"), "rb")             # what Hermes would be doing while it runs
        time.sleep = lambda seconds: None                       # the installer's patience is not what is tested
        try:
            code, out = call_main(installer, common + ["--uninstall", "--purge"])
            assert code == 1 and "in use" in out and "Nothing was changed" in out, out
        finally:
            handle.close()
            time.sleep = real_sleep
    assert fake_hermes.read_state(home) == installed and (home / "plugins" / "memorybank").exists()
    assert (data / "bank.db").exists() and not backups(home, "memorybank-removed"), "nothing moved, nothing duplicated"
    passed("a bank held open stops --uninstall --purge before anything changes")


def s_a_failed_reinstall_keeps_the_original_provider(tmp: Path) -> None:
    installer = load_installer()
    cfg = copy.deepcopy(SYNTH)
    cfg["memory"] = {"provider": "honcho"}
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    with fast_hermes(installer, root):
        assert call_main(installer, common)[0] == 0
    assert install_state(home)["previous"]["provider"] == "honcho" and install_state(home)["activated"]

    def blocked(*args, **kwargs):
        raise RuntimeError("Hermes' config writer is blocked")

    real_edit, out = installer.edit_config, io.StringIO()
    installer.edit_config = blocked                              # a re-install through Hermes' Python that fails
    try:
        with contextlib.redirect_stdout(out):
            code = installer.install(args_for(tmp), root, home, None, "no hermes command", Path(sys.executable))
    finally:
        installer.edit_config = real_edit
    text = out.getvalue()
    state = install_state(home)
    assert code == 3 and "blocked" in text, (code, text)
    assert state["previous"]["provider"] == "honcho", state["previous"]
    assert state["activated"] is True and "NOT switched on" not in text, (state, text)
    assert json.loads((backups(home)[-1] / "install-state.json").read_text("utf-8"))["previous"]["provider"] == "honcho"
    with fast_hermes(installer, root):
        assert call_main(installer, common + ["--uninstall"])[0] == 0
    assert fake_hermes.read_state(home)["memory"]["provider"] == "honcho", "the rollback restores the ORIGINAL provider"

    # an install.json already damaged that way (provider 'memorybank') is repaired from the oldest backup
    tmp2 = tmp / "two"
    tmp2.mkdir()
    root, home = make_world(tmp2, "demo", cfg)
    exe = make_stub(tmp2)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    with fast_hermes(installer, root):
        assert call_main(installer, common)[0] == 0
        path = home / "plugin-data" / "memorybank" / "install.json"
        damaged = json.loads(path.read_text("utf-8"))
        damaged["previous"]["provider"], damaged["activated"] = "memorybank", False
        path.write_text(json.dumps(damaged), "utf-8")
        assert call_main(installer, common + ["--uninstall"])[0] == 0
    assert fake_hermes.read_state(home)["memory"]["provider"] == "honcho", fake_hermes.read_state(home)
    passed("a re-install that fails keeps the original provider on record; a damaged record is repaired from the backups")


def s_an_earlier_installation_does_not_decide_the_provider(tmp: Path) -> None:
    installer = load_installer()
    cfg = copy.deepcopy(SYNTH)
    cfg["memory"] = {"provider": "honcho"}
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    with fast_hermes(installer, root):
        assert call_main(installer, common)[0] == 0
        assert call_main(installer, common + ["--uninstall"])[0] == 0
        assert fake_hermes.read_state(home)["memory"]["provider"] == "honcho"
        state = fake_hermes.read_state(home)                     # the owner then goes back to the built-in memory
        state["memory"].pop("provider")
        fake_hermes.write_state(home, state)
        assert call_main(installer, common)[0] == 0
        assert install_state(home)["previous"]["provider"] is None
        assert call_main(installer, common + ["--uninstall"])[0] == 0
    assert "provider" not in (fake_hermes.read_state(home).get("memory") or {}), "an earlier honcho is not brought back"
    passed("the oldest backup only decides for the installation it belongs to: an earlier provider is not brought back")


def s_a_provider_given_up_without_uninstalling_is_not_brought_back(tmp: Path) -> None:
    installer = load_installer()
    cfg = copy.deepcopy(SYNTH)
    cfg["memory"] = {"provider": "honcho"}
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    with fast_hermes(installer, root):
        assert call_main(installer, common)[0] == 0
        state = fake_hermes.read_state(home)                     # built-in memory again, memorybank switched off by hand
        state["memory"].pop("provider")
        state["plugins"]["enabled"] = [n for n in state["plugins"]["enabled"] if n != "memorybank"]
        fake_hermes.write_state(home, state)
        assert call_main(installer, common)[0] == 0
        assert install_state(home)["previous"]["provider"] is None, "the second install records what it found: no provider"
        assert call_main(installer, common + ["--uninstall"])[0] == 0
    assert "provider" not in (fake_hermes.read_state(home).get("memory") or {}), "the abandoned honcho was brought back"
    passed("an uninstall trusts an intact record that says 'no provider': an abandoned provider is not brought back")


def s_printed_python_commands_name_the_running_interpreter(tmp: Path) -> None:
    installer = load_installer()
    head = installer.__doc__.split("Needs only Python", 1)[0]
    assert "On Windows" in head and "python3" in head, "the usage lines say what to type on Windows"
    cfg = copy.deepcopy(SYNTH)
    cfg["memory"] = {"provider": "honcho"}
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    with fast_hermes(installer, root):
        assert call_main(installer, common)[0] == 0
        text = (backups(home)[-1] / "ROLLBACK.md").read_text("utf-8")
        code, out = call_main(installer, common + ["--uninstall"])
        assert code == 0, out
    runner = [line for line in (text + "\n" + out).splitlines() if "run.py" in line and "--home" in line]
    assert runner, "the way to read the bank without Hermes is printed"
    for line in runner:
        assert any(quoter(sys.executable) in line for quoter in (installer.quote, installer.cmd_quote, installer.ps_quote)), line
    assert "python3" not in text and "python3" not in out, "a bare python3 does not exist on Windows"
    passed("every printed command that starts a Python names the interpreter that is running")


def s_relative_paths_become_absolute(tmp: Path) -> None:
    installer = load_installer()
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    env = {"FAKE_HERMES_ROOT": str(root)}
    rel_root, rel_exe = os.path.relpath(str(root), str(tmp)), os.path.relpath(exe, str(tmp))
    assert not os.path.isabs(rel_root) and not os.path.isabs(rel_exe)
    here = os.getcwd()
    os.chdir(str(tmp))
    try:
        with fast_hermes(installer, root):
            code, out = call_main(installer, ["--hermes-root", rel_root, "--profile", "demo", "--hermes", rel_exe,
                                              "--no-index"])
        assert code == 0, out
        try:                                                       # the other two paths a run can be given
            rel_python, rel_agent = os.path.relpath(sys.executable, str(tmp)), os.path.relpath(str(tmp), str(tmp))
        except ValueError:                                         # another drive: there is no relative spelling
            rel_python = rel_agent = ""
        if rel_python:
            with fast_hermes(installer, root):
                code, out = call_main(installer, ["--hermes-root", rel_root, "--profile", "demo", "--no-cli", "--python",
                                                  rel_python, "--agent-dir", rel_agent, "--dry-run"])
            assert code == 0 and "(%s)" % sys.executable in out and not os.path.isabs(rel_python), out
    finally:
        os.chdir(here)
    state = install_state(home)
    for key in ("home", "backup", "hermes_command"):
        assert os.path.isabs(state[key]), (key, state[key])
    note = backups(home)[-1] / "ROLLBACK.md"
    text = note.read_text("utf-8")
    assert str(root) in text and exe in text, text[:900]
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    done = subprocess.run(rollback_command(note), shell=True, capture_output=True, text=True, timeout=300,
                          cwd=str(elsewhere), env=dict(os.environ, **env), stdin=subprocess.DEVNULL)
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert not (home / "plugins" / "memorybank").exists()
    passed("--hermes-root and --hermes given as relative paths are recorded absolute: the rollback works from anywhere")


def s_the_rollback_note_names_the_profile_in_every_hermes_command(tmp: Path) -> None:
    installer = load_installer()
    for named in (True, False):
        sub = tmp / ("named" if named else "root")
        sub.mkdir()
        if named:
            root, home = make_world(sub)
            who, flag = ["--profile", "demo"], "--profile demo "
        else:
            root, _ = make_root_world(sub)
            home, who, flag = root, ["--root"], "--profile default "
        exe = make_stub(sub)
        common = ["--hermes-root", str(root), *who, "--hermes", exe, "--no-index"]
        with fast_hermes(installer, root):
            assert call_main(installer, common)[0] == 0
        text = (backups(home)[-1] / "ROLLBACK.md").read_text("utf-8")
        bare = re.findall(r"hermes (?!--profile )(?:config|plugins|memorybank|memory)\b[^\n`]*", text)
        assert not bare, "a `hermes` command without the profile flag would act on whichever profile is sticky: %r" % bare
        for command in ("plugins enable memorybank", "memorybank search", "memorybank export", "memorybank status",
                        "memorybank check", "memorybank usage"):
            assert "hermes %s%s" % (flag, command) in text, (command, text)
    passed("every `hermes` command in ROLLBACK.md carries the profile flag (--profile <name>, or default for the root)")


def s_the_bank_can_be_read_after_the_rollback(tmp: Path) -> None:
    installer = load_installer()
    cfg = copy.deepcopy(SYNTH)
    cfg["memory"] = {"provider": "honcho"}
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe]
    with fast_hermes(installer, root):
        assert call_main(installer, common)[0] == 0              # with the index
        note = (backups(home)[-1] / "ROLLBACK.md").read_text("utf-8")
        assert note.index("Before you roll back") < note.index("## One command"), "search first, then roll back"
        code, out = call_main(installer, common + ["--uninstall"])
        assert code == 0, out
    (gone,) = backups(home, "memorybank-removed")
    shown = [re.match(r"\s*(Terminal|Command Prompt|PowerShell):\s+(.*)$", line) for line in out.splitlines()]
    forms = {m.group(1): m.group(2) for m in shown if m and "run.py" in m.group(2)}
    command = forms["Command Prompt" if os.name == "nt" else "Terminal"].replace("<words>", "WireGuard")
    assert str(gone / "plugin" / "run.py") in command and str(home) in command, command
    done = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    assert done.returncode == 0 and "WireGuard" in done.stdout, (done.returncode, done.stdout, done.stderr)
    assert "after the rollback" in note.lower() and "run.py" in note and str(home) in note, note
    passed("after the rollback the printed run.py command still reads the bank; ROLLBACK.md says to look BEFORE")


def s_uninstall_without_hermes_finishes_once_the_settings_are_back(tmp: Path) -> None:
    installer = load_installer()
    cfg = copy.deepcopy(SYNTH)
    cfg["memory"] = {"provider": "honcho"}
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    with fast_hermes(installer, root):
        assert call_main(installer, ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"])[0] == 0
    nothing = ["--hermes-root", str(root), "--profile", "demo", "--hermes", str(tmp / "no" / "hermes"), "--uninstall"]
    code, out = call_main(installer, nothing)
    assert code == 2 and "Nothing was changed" in out and "--hermes" in out and "config.yaml" in out, out
    assert "hermes --profile demo config set memory.provider honcho" in out, out
    assert (home / "plugins" / "memorybank").exists()
    state = fake_hermes.read_state(home)                         # the owner runs the two printed commands by hand
    state["memory"]["provider"] = "honcho"
    state["plugins"]["enabled"] = [p for p in state["plugins"]["enabled"] if p != "memorybank"]
    state["plugins"]["disabled"] = ["memorybank"]
    fake_hermes.write_state(home, state)
    code, out = call_main(installer, nothing)
    assert code == 0 and "Done." in out and not (home / "plugins" / "memorybank").exists(), out
    assert fake_hermes.read_state(home) == state, "nothing in config.yaml was touched"
    assert backups(home, "memorybank-removed")[0].joinpath("plugin", "plugin.yaml").exists()
    passed("no hermes command: once config.yaml no longer names the plugin, running the uninstall again finishes it")


def s_a_reinstall_heals_a_plugin_that_is_also_disabled(tmp: Path) -> None:
    installer = load_installer()
    cfg = copy.deepcopy(SYNTH)                                   # what 1.2.0's python route could leave behind
    cfg["memory"] = {"provider": "memorybank"}
    cfg["plugins"] = {"enabled": SYNTH["plugins"]["enabled"] + ["memorybank"], "disabled": ["memorybank"]}
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    with fast_hermes(installer, root):
        code, out = call_main(installer, common)
    after = fake_hermes.read_state(home)
    assert code == 0 and "verified" in out, out
    assert "memorybank" in after["plugins"]["enabled"] and "memorybank" not in after["plugins"]["disabled"], after
    assert [c for c in calls(root) if c[1:3] == ["plugins", "enable"]], "`plugins enable` was run"

    tmp2 = tmp / "two"                                           # settings that do not take: the message says what to run
    tmp2.mkdir()
    root, home = make_world(tmp2)
    exe = make_stub(tmp2)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    with fast_hermes(installer, root, fail="managed"):
        code, out = call_main(installer, common)
    assert code == 3 and "NOT verified" in out, out
    assert "hermes --profile demo plugins enable memorybank" in out, out
    assert "hermes --profile demo config set memory.provider memorybank" in out, out
    passed("a re-install enables a plugin that is also disabled; a switch-on that does not take names the commands to run")


def s_windows_rollback_commands_are_quoted_for_their_shell(tmp: Path) -> None:
    installer = load_installer()
    ps, cmd = installer.ps_quote, installer.cmd_quote
    assert ps("plain") == "'plain'" and ps("a b") == "'a b'"
    assert ps("pay$day hermes") == "'pay$day hermes'" and ps("a$(1+1)b") == "'a$(1+1)b'" and ps("a`b") == "'a`b'"
    assert ps("it's") == "'it''s'" and ps("curly \u2019 and \u2018") == "'curly \u2019\u2019 and \u2018\u2018'"
    assert ps("C:\\") == "'C:\\\\'" and ps("C:\\a b\\") == "'C:\\a b\\\\'" and ps("C:\\a b") == "'C:\\a b'"
    assert cmd("a b") == '"a b"' and cmd("a%TEMP%b") == '"a%TEMP%b"' and cmd("C:\\") == '"C:\\\\"'
    saved = installer.IS_WINDOWS
    installer.IS_WINDOWS = True
    try:
        backup = tmp / "b"
        backup.mkdir()
        args = types.SimpleNamespace(profile="demo", root=False, no_cli=False, agent_dir=None)

        def note(root_name: str) -> str:
            installer.write_rollback(backup, stamp="20260101-000000", home=tmp / "home", args=args, root=tmp / root_name,
                                     cli_exe=None, python=None, previous={}, activation="cli")
            return (backup / "ROLLBACK.md").read_text("utf-8")

        text = note("pay$day (x) it's")
        shell, prompt = text.split("PowerShell:", 1)[1].split("Command Prompt:", 1)
        root = str(tmp / "pay$day (x) it's")
        assert "& " + ps(sys.executable) in shell and ps(root) in shell and "''" in shell and ps(backup / "install.py") in shell
        assert cmd(sys.executable) in prompt and cmd(root) in prompt and cmd(backup / "install.py") in prompt
        assert "'" not in prompt.split("```")[1].replace("it's", ""), "the Command Prompt form uses double quotes"
        assert "percent sign" not in text
        assert "percent sign" in note("50%off hermes"), "cmd.exe expands %...% even inside quotes: say so"
        for hostile in ("x$(calc)y", "a%TEMP%b", 'q"r', "s\u201cet\u201d"):    # a name read from config.yaml, in "..."
            safe = installer.plain(hostile)
            assert not any(ch in safe[1:-1] for ch in '$%"\u201c\u201d\u201e'), (hostile, safe)
        assert installer.plain("honcho") == "honcho" and installer.plain("my-plugin_2.x") == "my-plugin_2.x"
        installer.IS_WINDOWS = False
        assert installer.plain("a$b") == "'a$b'", "shlex quoting is enough off Windows: the name is kept as it is"
    finally:
        installer.IS_WINDOWS = saved
    passed("PowerShell gets single quotes and the call operator; Command Prompt keeps double quotes and a % warning")


def s_bad_profile_names_are_refused(tmp: Path) -> None:
    installer = load_installer()
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    before = sorted(p.name for p in (root / "profiles").iterdir())
    with fast_hermes(installer, root):
        for bad in ("", " ", ".", "..", "a/b", "a\\b", "demo two", "d\u00e9mo", "x" * 65, "_demo"):
            code, out = call_main(installer, ["--hermes-root", str(root), "--profile", bad, "--hermes", exe, "--dry-run"])
            assert code == 2 and "hermes profile list" in out and "Memory Bank" not in out.split("ERROR", 1)[0], (bad, out)
        assert sorted(p.name for p in (root / "profiles").iterdir()) == before, "nothing was created under profiles/"
        assert not (root / "plugins").exists() and not (root / "backups").exists()
        code, out = call_main(installer, ["--hermes-root", str(root), "--profile", " DEMO ", "--hermes", exe, "--dry-run"])
        assert code == 0 and str(home) in out, "Hermes lowercases and strips the name, so the installer does too"
        code, out = call_main(installer, ["--hermes-root", str(root), "--profile", "x" * 64, "--hermes", exe, "--dry-run"])
        assert code == 2 and "not found" in out, "a valid name of the longest length gets as far as looking for its folder"
    passed("empty, path-like and un-Hermes-like profile names are refused with a pointer to `hermes profile list`")


def s_the_reason_a_hermes_did_not_run_is_shown(tmp: Path) -> None:
    installer = load_installer()
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--dry-run"]
    with fast_hermes(installer, root, fail="version"):          # the stand-in prints "boom" on stderr
        code, out = call_main(installer, common + ["--hermes", exe])
    assert code == 0 and "did not run" in out and "boom" in out, out
    broken = tmp / "broken" / "hermes"                          # a file the operating system cannot start
    broken.parent.mkdir()
    broken.write_text("this is not a program\n", "utf-8")
    code, out = call_main(installer, common + ["--hermes", str(broken)])
    assert code == 0 and "did not run" in out and "could not run" in out, out
    trace = "Traceback (most recent call last):\n  File \"x.py\", line 1, in <module>\nModuleNotFoundError: No module named 'x'"
    assert installer.reason_line(trace) == "ModuleNotFoundError: No module named 'x'", "a traceback: its last line"
    assert installer.reason_line("\n  first thing said\nsecond") == "first thing said" and installer.reason_line("") == ""
    assert len(installer.reason_line("x" * 500)) <= 200
    passed("when `hermes` cannot be started the first line of the reason is shown")


def s_a_console_that_cannot_show_every_character_still_gets_the_rest(tmp: Path) -> None:
    installer = load_installer()
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", newline="\n")
    saved, sys.stdout = sys.stdout, stream
    try:
        installer.say("Rollback instructions: C:\\caf\u00e9 \u65e5\u672c\\ROLLBACK.md")
        stream.flush()
    finally:
        sys.stdout = saved
    assert stream.buffer.getvalue().decode("cp1252") == "Rollback instructions: C:\\caf\u00e9 \\u65e5\\u672c\\ROLLBACK.md\n"
    sub = tmp / "caf\u00e9 \u65e5\u672c"
    sub.mkdir()
    root, home = make_world(sub)
    done = subprocess.run([sys.executable, str(INSTALLER), "--hermes-root", str(root), "--profile", "demo", "--no-cli",
                           "--no-activate", "--no-index"], capture_output=True, timeout=300, stdin=subprocess.DEVNULL,
                          env=dict(os.environ, PYTHONIOENCODING="cp1252:strict"))
    text = done.stdout.decode("cp1252") + done.stderr.decode("cp1252")
    assert done.returncode == 0 and "caf\u00e9 \\u65e5\\u672c" in text, (done.returncode, text)
    passed("a console that cannot show every character gets the characters it can show, the rest as \\uXXXX")


def s_the_version_gate_reads_any_version_line(tmp: Path) -> None:
    installer = load_installer()
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]

    def with_version(text: str, *extra: str) -> Tuple[int, str]:
        with fast_hermes(installer, root), in_env(FAKE_HERMES_VERSION=text):
            return call_main(installer, common + list(extra))

    for old in ("0.9.1", "v0.9.1", "0.21"):
        code, out = with_version(old)
        assert code == 2 and "needs Hermes" in out and "Nothing was changed" in out and "hermes update" in out, (old, out)
    assert not (home / "plugins" / "memorybank").exists() and not (home / "backups").exists()
    for fine in ("0.21.5", "v0.21.5+7363 (2026.9.24)", "2026.9.24"):
        code, out = with_version(fine, "--dry-run")
        assert code == 0 and "could not read the Hermes version" not in out and "dry run" in out, (fine, out)
    code, out = with_version("(development build)", "--dry-run")
    assert code == 0 and "could not read the Hermes version; continuing" in out, out
    passed("the version floor is checked on any version in the line; an unreadable one is said, not skipped")


def s_a_dry_run_says_truthfully_what_it_did(tmp: Path) -> None:
    installer = load_installer()
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo"]
    with fast_hermes(installer, root):
        code, out = call_main(installer, common + ["--hermes", exe, "--dry-run"])
        assert code == 0 and "dry run" in out and "started `hermes`" in out, out
        assert "Hermes" in out.split("started `hermes`", 1)[1], "it says what Hermes may write"
        assert "nothing was changed" not in out.lower(), out
        code, out = call_main(installer, common + ["--no-cli", "--dry-run"])
        assert code == 0 and "dry run" in out and "started" not in out, out
        assert call_main(installer, common + ["--hermes", exe, "--no-index"])[0] == 0
        code, out = call_main(installer, common + ["--hermes", exe, "--uninstall", "--dry-run"])
        assert code == 0 and "dry run" in out and "started `hermes`" in out and "nothing was changed" not in out.lower(), out
    assert (home / "plugins" / "memorybank").exists(), "a dry run uninstall moves nothing"
    passed("a dry run says it started `hermes` (which may write its own logs) and changed nothing of the installer's")


def s_uninstall_wording_is_true_for_both_routes(tmp: Path) -> None:
    installer = load_installer()
    doc = " ".join(installer.__doc__.split())
    assert "works on Windows, macOS and Linux" not in doc and "run on Windows only" in doc, "claims it cannot back"
    assert "lists it under plugins.disabled" not in doc and "plugins.enabled" in doc, doc
    assert "memorybank.json" in doc, "--purge moves the settings file too"
    root, home = make_world(tmp)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe]
    with fast_hermes(installer, root):
        assert call_main(installer, common + ["--no-index"])[0] == 0
        code, out = call_main(installer, common + ["--uninstall", "--dry-run"])
    assert code == 0 and "switched off" not in out and "plugins.enabled" in out, out
    text = " ".join((backups(home)[-1] / "ROLLBACK.md").read_text("utf-8").split())
    for sentence in re.split(r"(?<=[.;]) ", text):
        if "plugins.disabled" in sentence:
            assert "hermes plugins disable" in sentence or "`hermes` route" in sentence, sentence
    passed("every sentence about what an uninstall does is true for the hermes route and Hermes' Python alike")


def s_the_posix_branches_of_the_installer_run_here_too(tmp: Path) -> None:
    """What only runs on macOS and Linux (private folders, a private lock file, a rename that is not retried, shell
    quoting) is exercised by switching the platform flag and watching os.chmod / os.open: modes cannot be read on NTFS."""
    installer = load_installer()
    chmods: List[Tuple[str, int]] = []
    opens: List[Tuple[str, int]] = []
    real_chmod, real_open, real_rename, real_sleep = os.chmod, os.open, os.rename, time.sleep
    renames = {"n": 0}

    def spy_open(path, flags, mode=0o777, *a, **k):
        opens.append((str(path), mode))
        return real_open(path, flags, mode, *a, **k)

    def refuse(src, dst, *a, **k):
        renames["n"] += 1
        raise PermissionError("simulated")

    installer.IS_WINDOWS = False
    os.chmod = lambda path, mode, *a, **k: chmods.append((str(path), mode))
    os.open = spy_open
    try:
        new = tmp / "a" / "b"
        installer.private_dir(new)
        assert [c for c in chmods if c[0] == str(new)] == [(str(new), 0o700)], chmods
        count = len(chmods)
        installer.private_dir(new)
        assert len(chmods) == count, "a folder that was already there is left as it was"
        backup = installer.fresh_dir(tmp, "backup")
        assert (str(backup), 0o700) in chmods, "a backup holds copies of settings and data: private"
        lock = installer.InstallLock(tmp / "profile", "install")
        assert lock.acquire() == ""
        assert [m for p, m in opens if p.endswith(".memorybank-install.lock")] == [0o600], opens
        assert (tmp / "profile" / "plugin-data").is_dir() and (str(tmp / "profile" / "plugin-data"), 0o700) in chmods
        lock.release()
        os.rename, time.sleep = refuse, (lambda seconds: (_ for _ in ()).throw(AssertionError("must not wait")))
        try:
            installer.move(tmp / "x", tmp / "y")
            raise AssertionError("a refusal off Windows is real and is raised")
        except PermissionError:
            assert renames["n"] == 1, "off Windows there is no waiting for a virus scanner"
        assert installer.quote("a b") == "'a b'" and installer.quote("plain") == "plain", "shell quoting, not double quotes"
    finally:
        os.chmod, os.open, os.rename, time.sleep = real_chmod, real_open, real_rename, real_sleep
    passed("the installer's macOS/Linux branches run here too: private folders and lock file, no rename retry, shell quoting")


def s_an_uninstall_that_changed_nothing_is_not_reported_done(tmp: Path) -> None:
    installer = load_installer()
    cfg = copy.deepcopy(SYNTH)
    cfg["memory"] = {"provider": "honcho"}
    root, home = make_world(tmp, "demo", cfg)
    exe = make_stub(tmp)
    common = ["--hermes-root", str(root), "--profile", "demo", "--hermes", exe, "--no-index"]
    plugin = home / "plugins" / "memorybank"
    with fast_hermes(installer, root):
        assert call_main(installer, common)[0] == 0
    with fast_hermes(installer, root, fail="managed"):             # `config set` says "managed" and writes nothing
        code, out = call_main(installer, common + ["--uninstall"])
    assert code == 3 and "Done." not in out, out
    assert "hermes --profile demo config set memory.provider honcho" in out, out
    assert plugin.exists() and fake_hermes.read_state(home)["memory"]["provider"] == "memorybank"
    assert not lock_file(home).exists()
    real_edit, out2 = installer.edit_config, io.StringIO()         # the python route is read back as well
    installer.edit_config = lambda *a, **k: {"provider": "memorybank", "enabled": [], "disabled": [], "config": ""}
    try:
        with contextlib.redirect_stdout(out2):
            code = installer.uninstall(args_for(tmp, uninstall=True), root, home, None, "no hermes command",
                                       Path(sys.executable))
    finally:
        installer.edit_config = real_edit
    assert code == 3 and "Done." not in out2.getvalue() and plugin.exists(), out2.getvalue()
    with fast_hermes(installer, root):                              # once the setting can be written, it finishes
        code, out = call_main(installer, common + ["--uninstall"])
    assert code == 0 and not plugin.exists() and fake_hermes.read_state(home)["memory"]["provider"] == "honcho", out
    passed("an uninstall reads the provider back: if it still names the plugin, the plugin stays and the exit is 3")


def main() -> int:
    real = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    scenarios = [
        ("s_cli_fresh", lambda t: s_cli_fresh(t)), ("s_cli_provider_and_rollback", lambda t: s_cli_provider_and_rollback(t)),
        ("s_cli_provider_unset", lambda t: s_cli_provider_unset(t)), ("s_cli_failures", lambda t: s_cli_failures(t)),
        ("s_cli_missing", lambda t: s_cli_missing(t)), ("s_cli_index_fallback", lambda t: s_cli_index_fallback(t)),
        ("s_files_in_use", lambda t: s_files_in_use(t)), ("s_units", lambda t: s_units(t)),
        ("s_python_route", lambda t: s_python_route(t, real)), ("s_run_tool", lambda t: s_run_tool(t)),
        ("s_nothing_a_file_manager_added_is_installed", lambda t: s_nothing_a_file_manager_added_is_installed(t)),
        ("s_a_lone_installer_never_replaces_the_plugin", lambda t: s_a_lone_installer_never_replaces_the_plugin(t)),
        ("s_rollback_commands_are_quoted", lambda t: s_rollback_commands_are_quoted(t)),
        ("s_a_hermes_on_another_home_is_not_gone_round", lambda t: s_a_hermes_on_another_home_is_not_gone_round(t)),
        ("s_too_old_a_hermes_is_refused", lambda t: s_too_old_a_hermes_is_refused(t)),
        ("s_default_means_the_root", lambda t: s_default_means_the_root(t)),
        ("s_python_route_is_read_back", lambda t: s_python_route_is_read_back(t)),
        ("s_uninstall_never_deletes_the_plugin", lambda t: s_uninstall_never_deletes_the_plugin(t)),
        ("s_a_failed_switch_off_is_finished_by_running_it_again",
         lambda t: s_a_failed_switch_off_is_finished_by_running_it_again(t)),
        ("s_warnings_name_the_profile", lambda t: s_warnings_name_the_profile(t)),
        ("s_units_more", lambda t: s_units_more(t)),
    ]
    # the review fixes of 1.3.0: each of these fails on the 1.3.0 installer
    for fn in (s_the_default_profile_is_always_named_to_hermes, s_the_stand_in_follows_hermes_rules,
               s_a_second_installer_is_turned_away, s_a_stale_lock_is_taken_over,
               s_the_lock_waits_out_a_file_that_is_being_deleted, s_the_lock_never_outlives_a_run,
               s_two_installers_at_once_never_leave_a_broken_plugin, s_the_new_copy_is_staged_beside_the_old_and_checked,
               s_purge_probes_the_bank_before_anything_changes, s_a_bank_that_is_open_stops_a_purge,
               s_purge_does_not_take_the_bank_from_a_plugin_that_stays,
               s_a_failed_reinstall_keeps_the_original_provider, s_an_earlier_installation_does_not_decide_the_provider,
               s_a_provider_given_up_without_uninstalling_is_not_brought_back,
               s_printed_python_commands_name_the_running_interpreter, s_relative_paths_become_absolute,
               s_the_rollback_note_names_the_profile_in_every_hermes_command, s_the_bank_can_be_read_after_the_rollback,
               s_uninstall_without_hermes_finishes_once_the_settings_are_back,
               s_a_reinstall_heals_a_plugin_that_is_also_disabled, s_windows_rollback_commands_are_quoted_for_their_shell,
               s_bad_profile_names_are_refused, s_the_reason_a_hermes_did_not_run_is_shown,
               s_a_console_that_cannot_show_every_character_still_gets_the_rest, s_the_version_gate_reads_any_version_line,
               s_a_dry_run_says_truthfully_what_it_did, s_uninstall_wording_is_true_for_both_routes,
               s_the_posix_branches_of_the_installer_run_here_too, s_an_uninstall_that_changed_nothing_is_not_reported_done):
        scenarios.append((fn.__name__, fn))
    failed = 0
    base = Path(tempfile.mkdtemp(prefix="mb-install-"))
    try:
        for index, (name, fn) in enumerate(scenarios):
            box = base / ("%02d-%s" % (index, name))
            box.mkdir()
            try:
                fn(box)
            except Exception:
                failed += 1
                print("FAIL  " + name)
                traceback.print_exc()
    finally:
        shutil.rmtree(str(base), ignore_errors=True)
    for note in SKIPPED:
        print("SKIP  " + note)
    print("\n%d checks passed, %d scenario(s) failed, %d skipped" % (len(PASSED), failed, len(SKIPPED)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
