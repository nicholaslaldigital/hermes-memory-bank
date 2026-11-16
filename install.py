"""Install (or remove) the Hermes Memory Bank in one profile, reversibly.

    <python> install.py --profile <name> --dry-run     # show the plan; no setting or plugin file is changed
    <python> install.py --profile <name>               # install, switch on, index, check
    <python> install.py --profile <name> --uninstall   # put everything back
    <python> install.py --root ...                     # the default profile instead of a named one

<python> is `python3` on macOS and Linux. On Windows `python3` is usually only a stub that opens the Microsoft Store:
write `python` (or `py -3`) there, or the full path of any Python 3.8+ (Hermes' own installer does not put one on
PATH). Every command this installer prints that starts a Python names the interpreter that is running it.

Needs only Python 3.8+ (standard library) and a working Hermes. It is written for Windows, macOS and Linux but has
been run on Windows only. The plugin is switched on through Hermes' own commands (`hermes plugins enable`,
`hermes config set memory.provider`), after checking that the `hermes` command really points at the profile being
changed; every call to `hermes` names the profile (`--profile <name>`, or `--profile default` for the root), so a
profile chosen earlier with `hermes profile use` cannot redirect it. The result is read back and verified. The plan
it prints first (also with --dry-run) says which of three ways applies:

  * the `hermes` commands, as above (the normal case);
  * no `hermes` command can be run at all, but Hermes' own Python is found
    (<hermes root>/hermes-agent/venv, or --python): config.yaml is then edited by
    Hermes' own config writer running in that Python, and read back afterwards;
  * otherwise - or when the `hermes` that runs works on a different Hermes home
    than the profile being changed - it still copies and indexes, leaves
    config.yaml alone and prints the two commands to finish.

What an install changes, all inside that profile's own folder:

  plugins/memorybank/            the plugin (the plugin files of this folder, copied into a temporary folder beside it, checked
                                 against the source, then renamed into place: a copy that fails changes nothing)
  config.yaml                    memory.provider -> memorybank, and "memorybank"
                                 enabled as a plugin (nothing else is touched)
  plugin-data/memorybank/        the bank itself (created by the first index),
                                 and install.json, the record of this install
  plugin-data/.memorybank-install.lock
                                 there while an install or uninstall of this profile is running (a second one is
                                 turned away; one left by a run that was killed is taken over after 15 minutes)
  backups/memorybank-<time>/     config.yaml and memorybank.json as they were,
                                 install-state.json, ROLLBACK.md and a copy of this
                                 installer (so the rollback works wherever this
                                 folder has gone)

Nothing is deleted: an uninstall restores the previous provider, takes the plugin off plugins.enabled (on the
`hermes` route it is also listed under plugins.disabled) and moves the plugin folder into a new
backups/memorybank-removed-<time>/ folder; the bank's data stays unless --purge moves it there too (together with
the settings file memorybank.json). On Windows a file that is open (Hermes running, an editor) stops the move - after
a few seconds' patience, in case a virus scanner is holding a folder written a moment ago - and the installer then
stops before changing anything, for the plugin folder and, with --purge, for the bank; macOS and Linux allow moving a
folder with open files, so quit Hermes before installing over a running copy or using --purge.

Exit codes: 0 done and verified, 1 stopped (an install changes nothing when it stops; an uninstall that stops part-way
says which step - running it again finishes the job), 2 bad arguments or nothing found to work on, 3 installed but
something needs attention (not switched on, not verified, or the index is incomplete), or an uninstall that could not
finish (the settings did not go back, or something is still in place): the messages say what to run.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

NAME = "memorybank"
HERE = Path(__file__).resolve().parent
SOURCE = HERE        # the repository's top folder IS the plugin (a plain repository link installs in Hermes); only PLUGIN_ENTRIES are copied
# The top-level files and folders that make up the plugin. Everything else in the repository (this installer, tests/,
# docs/, catalog/, the project's own notes, .git) is never copied into a profile.
PLUGIN_ENTRIES = ("plugin.yaml", "__init__.py", "cli.py", "run.py", "README.md", "core", "dashboard", "desktop", "skills", "ui")
IS_WINDOWS = os.name == "nt"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# What file managers add: never copied into a profile, never counted when the copy is compared with the source.
COPY_IGNORE = ("__pycache__", "*.pyc", "*.pyo", ".DS_Store", "._*", "Thumbs.db", "desktop.ini")
LOCK_NAME = ".memorybank-install.lock"
LOCK_STALE_SECONDS = 15 * 60          # a lock that has not been touched for this long was left by a run that died
LOCK_REFRESH_SECONDS = 30.0           # a running installer touches its lock this often (indexing can take long)
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")      # Hermes' own rule for a profile name (profiles.py)

# Runs inside Hermes' own Python (only when the `hermes` command cannot be used) so the
# edit still goes through its config writer.
_CONFIG_EDIT = r"""
import json, sys
from pathlib import Path
action, name, restore = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, sys.argv[4])
import yaml
from hermes_cli.config import get_config_path, save_config
path = Path(get_config_path())
raw = {}
if path.exists():
    raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
memory = raw.get("memory") if isinstance(raw.get("memory"), dict) else {}
plugins = raw.get("plugins") if isinstance(raw.get("plugins"), dict) else {}
previous = {"provider": memory.get("provider"), "had_memory_section": "memory" in raw,
            "enabled": list(plugins.get("enabled") or []), "disabled": list(plugins.get("disabled") or []),
            "config": str(path)}
if action == "activate":
    enabled = previous["enabled"] + ([name] if name not in previous["enabled"] else [])
    listed = {"enabled": enabled}
    if name in previous["disabled"]:     # `hermes plugins enable` takes it off that list; so does this
        listed["disabled"] = [p for p in previous["disabled"] if p != name]
    save_config({"memory": {"provider": name}, "plugins": listed}, merge_existing=True)
elif action == "deactivate":
    # Only what the install set: another provider chosen since is left alone.
    changes = {}
    if memory.get("provider") == name:
        changes["memory"] = {"provider": restore}
    if name in previous["enabled"]:
        changes["plugins"] = {"enabled": [p for p in previous["enabled"] if p != name]}
    if changes:
        save_config(changes, merge_existing=True)
print("MEMORYBANK_STATE " + json.dumps(previous))
"""


def say(text: str = "") -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        # A console that cannot show every character (cp1252, say) still gets the ones it can show; the rest is
        # written as \uXXXX, which can be told apart and looked up, instead of every one becoming "?".
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            print(text.encode(encoding, "backslashreplace").decode(encoding, "replace"))
        except (LookupError, UnicodeError):
            print(text.encode("ascii", "backslashreplace").decode("ascii"))
    try:
        sys.stdout.flush()
    except OSError:
        pass


def reason_line(text: str, limit: int = 200) -> str:
    """One line out of what an operating system or a program said, shortened: the first non-empty one - or, for a
    Python traceback, whose first line says nothing, the last one (the error itself)."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[-1] if lines[0].startswith("Traceback") else lines[0]
    return line if len(line) <= limit else line[:limit - 3] + "..."


def cmd_quote(value: Any) -> str:
    """Quote for Command Prompt: double quotes. A backslash before the closing quote would escape it, so a trailing
    one is doubled. (cmd.exe expands %NAME% even inside quotes; nothing escapes that.)"""
    text = str(value)
    return '"' + (text + "\\" if text.endswith("\\") else text) + '"'


_PS_QUOTES = "'\u2018\u2019\u201a\u201b"          # PowerShell reads all of these as a single quote


def ps_quote(value: Any) -> str:
    """Quote for PowerShell: single quotes, inside which nothing ($, a backtick, $(...)) is special. A single quote
    in the text is doubled; so is a trailing backslash (it would escape the closing quote of the program's own parser)."""
    text = "".join(char + char if char in _PS_QUOTES else char for char in str(value))
    return "'" + (text + "\\" if text.endswith("\\") else text) + "'"


def quote(value: Any) -> str:
    return cmd_quote(value) if IS_WINDOWS else shlex.quote(str(value))


def read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def plugin_version() -> str:
    try:
        for line in (SOURCE / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return "?"


def parse_version(text: str) -> Optional[Tuple[int, ...]]:
    """The first ``v1.2.3`` (or ``>=1.2.3``) in a line of text, else the first bare ``1.2.3`` or ``1.2``, as numbers;
    None if there is none."""
    for pattern in (r"(?:\bv|>=\s*)(\d+)\.(\d+)(?:\.(\d+))?", r"(\d+)\.(\d+)(?:\.(\d+))?"):
        match = re.search(pattern, text or "")
        if match:
            return tuple(int(part) for part in match.groups() if part is not None)
    return None


def required_hermes() -> Optional[Tuple[int, ...]]:
    """The Hermes version this plugin asks for (``requires_hermes`` in its plugin.yaml)."""
    try:
        for line in (SOURCE / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("requires_hermes:"):
                return parse_version(line.split(":", 1)[1].strip().strip("'\""))
    except OSError:
        pass
    return None


def absolute(value: Any) -> Path:
    """``value`` as an absolute path (``~`` expanded, ``..`` folded away) without following links: a venv's
    interpreter is a link whose own spelling has to be kept, and every path the installer records or prints must
    work from any folder."""
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def private_dir(path: Path) -> None:
    """Make ``path`` (and what is missing above it), readable by this user only on macOS/Linux."""
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS and not existed:
        try:
            os.chmod(str(path), 0o700)
        except OSError:
            pass


def fresh_dir(parent: Path, base: str) -> Path:
    """Create and return parent/base, or parent/base-2, base-3 ... if that name is taken.
    A backup holds copies of config and the bank's data, so on macOS/Linux it is private to this user."""
    number = 1
    while True:
        path = parent / (base if number == 1 else "%s-%d" % (base, number))
        try:
            path.mkdir(parents=True)
        except FileExistsError:
            number += 1
            continue
        if not IS_WINDOWS:
            try:
                os.chmod(str(path), 0o700)
            except OSError:
                pass
        return path


def move(source: Path, target: Path) -> None:
    """``os.rename`` that gives a folder a few seconds to be let go of. On Windows a folder that was written a
    moment ago can be held briefly by a virus scanner or the search indexer; only a refusal that lasts is "in use"
    (Hermes running, an editor). Anything but a permission error is raised at once, and so is everything off Windows."""
    attempts = 8 if IS_WINDOWS else 1
    for attempt in range(attempts):
        try:
            os.rename(str(source), str(target))
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def plain(value: Any) -> str:
    """A name from a config file as one safe word for a command line printed for the user to paste (Hermes'
    own names are letters, digits, dash and underscore; anything else is quoted and loses newlines and backticks -
    and on Windows also ``$``, ``%`` and double-quote characters, which PowerShell or cmd.exe would act on inside the
    double quotes used there)."""
    drop = "[`$%\"\u201c\u201d\u201e\r\n\x00]+" if IS_WINDOWS else "[`\r\n\x00]+"
    text = re.sub(drop, " ", str(value)).strip()
    return text if re.fullmatch(r"[\w.\-]+", text) else quote(text)


def same_path(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(str(a), str(b))
    except OSError:
        return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def owner_conflict(home: Path) -> Optional[str]:
    """A sentence when this is root working on a profile that belongs to somebody else, else None."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    try:
        owner = home.stat().st_uid
    except OSError:
        return None
    return None if owner == 0 else "%s belongs to another user (uid %d) and this is root" % (home, owner)


def clean_profile_name(raw: str) -> Tuple[str, str]:
    """``(name, "")`` with the name as Hermes reads it (stripped, lower-cased), or ``("", a sentence)`` when Hermes
    would refuse it: an empty value (an unset shell variable), a path, or anything that is not lowercase letters,
    digits, ``_`` and ``-`` starting with a letter or digit (at most 64 characters)."""
    hint = " Run `hermes profile list` to see the profile names."
    name = raw.strip().casefold()
    if not name:
        return "", "--profile needs a profile name and this one is empty (an unset shell variable?)." + hint
    if "/" in name or "\\" in name or name in (".", ".."):
        return "", "%r is a path, not a profile name: give the name only (--hermes-root names the Hermes folder)." % raw + hint
    if not PROFILE_NAME.fullmatch(name):
        return "", ("%r is not a profile name Hermes accepts: lowercase letters, digits, '-' and '_', starting with a "
                    "letter or digit, at most 64 characters." % raw) + hint
    return name, ""


# ----------------------------------------------------------------------------- where Hermes is

def default_root() -> Path:
    """Hermes' own rule: HERMES_HOME if set (a profile folder means its parent's parent), else ~/.hermes
    (%LOCALAPPDATA%\\hermes on Windows)."""
    suffix = os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        native = (Path(local) if local else Path.home() / "AppData" / "Local") / ("hermes" + suffix)
    else:
        native = Path.home() / (".hermes" + suffix)
    env = os.environ.get("HERMES_HOME", "").strip()
    if not env:
        return native
    path = Path(os.path.expanduser(os.path.expandvars(env)))
    try:
        path.resolve().relative_to(native.resolve())
        return native
    except ValueError:
        return path.parent.parent if path.parent.name == "profiles" else path


def find_hermes(root: Path, explicit: Optional[str]) -> Optional[str]:
    """The `hermes` command, as an absolute path: the one given, the one on PATH, or one of the usual places."""
    found: Optional[str] = None
    if explicit:
        explicit = os.path.expanduser(explicit)
        found = shutil.which(explicit) or (explicit if Path(explicit).is_file() else None)
    else:
        found = shutil.which("hermes")
        if not found:
            names = ("hermes.exe", "hermes.cmd", "hermes.bat") if IS_WINDOWS else ("hermes",)
            spots = [root / "bin", root / "hermes-agent" / ".hermes" / "bin",
                     root / "hermes-agent" / "venv" / ("Scripts" if IS_WINDOWS else "bin"), Path.home() / ".local" / "bin"]
            if not IS_WINDOWS:
                spots += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
            for spot in spots:
                for name in names:
                    if (spot / name).is_file():
                        found = str(spot / name)
                        break
                if found:
                    break
    return os.path.abspath(found) if found else None


def find_python(root: Path, explicit: Optional[str]) -> Optional[Path]:
    """Hermes' development virtualenv, if there is one (the fallback way to edit config.yaml)."""
    candidates = [Path(os.path.expanduser(explicit))] if explicit else []
    agent = root / "hermes-agent"
    candidates += [agent / "venv" / "Scripts" / "python.exe", agent / "venv" / "bin" / "python",
                   agent / ".venv" / "Scripts" / "python.exe", agent / ".venv" / "bin" / "python"]
    found = next((p for p in candidates if p.exists()), None)
    return absolute(found) if found is not None else None


class Hermes:
    """The `hermes` command aimed at one profile. Never prompts (stdin is closed).

    Every call names its profile with --profile (``default`` is the Hermes root itself): a call without the flag
    follows the choice `hermes profile use <name>` saved in <root>/active_profile, which may be another profile."""

    def __init__(self, exe: str, profile: Optional[str]) -> None:
        self.exe = exe
        self.profile = profile or "default"
        self.reason = ""          # why the last call that did not work did not (first line of what was said)

    def run(self, *args: str, timeout: float = 180.0) -> Tuple[int, str, str]:
        cmd = [self.exe, "--profile", self.profile] + list(args)
        try:
            done = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return -1, "", "timed out after %d s" % int(timeout)
        except OSError as exc:
            return -1, "", "could not run: %s" % exc
        clean = lambda raw: _ANSI.sub("", raw.decode("utf-8", "replace")).strip()  # noqa: E731
        return done.returncode, clean(done.stdout), clean(done.stderr)

    def text(self, *args: str, timeout: float = 180.0) -> Tuple[int, str]:
        code, out, err = self.run(*args, timeout=timeout)
        return code, "\n".join(part for part in (out, err) if part)

    def value(self, key: str) -> Tuple[bool, Any]:
        """`hermes config get <key> --json`; (False, None) when it cannot be read."""
        code, out, _ = self.run("config", "get", key, "--json", timeout=120)
        if code != 0 or not out:
            return False, None
        for candidate in (out, out.splitlines()[-1]):
            try:
                return True, json.loads(candidate)
            except ValueError:
                continue
        return False, None

    def version(self) -> Optional[str]:
        code, out, err = self.run("--version", timeout=120)
        if code == 0 and out:
            return out.splitlines()[0]
        self.reason = reason_line(err or out) or ("it printed no version" if code == 0 else "exit code %d" % code)
        return None

    def config_file(self) -> Optional[Path]:
        code, out, err = self.run("config", "path", timeout=120)
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        if code != 0 or not lines:
            self.reason = reason_line(err or out) or ("it printed nothing" if code == 0 else "exit code %d" % code)
            return None
        if not os.path.isabs(lines[-1]) and len(lines) > 1:
            joined = "".join(lines)               # a long path wrapped over several lines by a console library
            if os.path.isabs(joined):
                return Path(joined)
        return Path(lines[-1])

    def provider(self) -> Optional[str]:
        ok, value = self.value("memory.provider")
        return (str(value) if value else "") if ok else None

    def enabled(self) -> Optional[List[str]]:
        ok, value = self.value("plugins.enabled")
        return [str(v) for v in value] if ok and isinstance(value, list) else None

    def plugin_status(self, name: str) -> Optional[str]:
        code, out, _ = self.run("plugins", "list", "--json", timeout=180)
        if code != 0:
            return None
        try:
            for item in json.loads(out):
                if isinstance(item, dict) and item.get("name") == name:
                    return str(item.get("status"))
        except ValueError:
            return None
        return "missing"


def usable_hermes(exe: Optional[str], profile: Optional[str], config: Path) -> Tuple[Optional[Hermes], str, bool]:
    """The `hermes` command, but only if it runs and works on this very profile.

    Returns ``(command or None, a note, elsewhere)``; ``elsewhere`` is True when the command runs but works on a
    different Hermes home - then the installer does not go round it (that needs an explicit --python)."""
    if not exe:
        return None, "no `hermes` command found (use --hermes <path> if it is somewhere unusual)", False
    cli = Hermes(exe, profile)
    version = cli.version()
    if not version:
        return None, "`%s` did not run (--version failed%s)" % (exe, ": " + cli.reason if cli.reason else ""), False
    version = version.split(" · ")[0].encode("ascii", "replace").decode("ascii")   # keep the line plain text
    where = cli.config_file()
    if where is None:
        return None, "`%s config path` gave no answer (%s%s)" % (exe, version, "; " + cli.reason if cli.reason else ""), False
    if not same_path(where, config):
        return None, ("`%s` works on %s, not on %s - pass the --hermes-root of the Hermes folder that this `hermes` "
                      "uses (the one that contains profiles/), or set HERMES_HOME to that folder before running the "
                      "installer" % (exe, where.parent, config.parent)), True
    return cli, version, False


# ----------------------------------------------------------------------------- reading config without Hermes

def scan_config(path: Path) -> Dict[str, Any]:
    """memory.provider and plugins.enabled / plugins.disabled as written in config.yaml, read as plain text (no
    YAML library)."""
    result: Dict[str, Any] = {"provider": None, "had_memory_section": False, "enabled": [], "disabled": [],
                              "config": str(path)}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return result
    section = ""
    listing = ""              # the plugins list whose "- item" lines are being read, if any
    list_indent = 0
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if indent == 0:
            section = stripped.partition(":")[0].strip()
            listing = ""
            if section == "memory":
                result["had_memory_section"] = True
            continue
        if section == "memory" and stripped.startswith("provider:"):
            value = stripped.split(":", 1)[1].split(" #", 1)[0].strip().strip("'\"")
            result["provider"] = value or None
        elif section == "plugins":
            key = stripped.partition(":")[0].strip() if not stripped.startswith("- ") else ""
            if key in ("enabled", "disabled"):
                rest = stripped.split(":", 1)[1].split(" #", 1)[0].strip()
                listing, list_indent = (key if rest == "" else ""), indent
                if rest.startswith("["):
                    result[key] = [x.strip().strip("'\"") for x in rest.strip("[]").split(",") if x.strip()]
            elif listing and stripped.startswith("- "):
                result[listing].append(stripped[2:].split(" #", 1)[0].strip().strip("'\""))
            elif listing and indent <= list_indent:
                listing = ""
    return result


def edit_config(python: Path, agent_dir: Path, home: Path, action: str, restore: str = "") -> Dict[str, Any]:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    # -I and a neutral working folder: nothing lying around in the profile
    # (a stray yaml.py, say) or set in the environment can be imported.
    done = subprocess.run(
        [str(python), "-I", "-c", _CONFIG_EDIT, action, NAME, restore, str(agent_dir)],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(agent_dir),
    )
    for line in done.stdout.splitlines():
        if line.startswith("MEMORYBANK_STATE "):
            return json.loads(line.split(" ", 1)[1])
    raise RuntimeError("Hermes' config writer failed:\n" + (done.stderr or done.stdout)[-1500:])


def run_tool(python: Path, plugin_dir: Path, home: Path, *args: str, timeout: float = 600.0) -> Tuple[int, str]:
    """Run one of the plugin's own commands without Hermes; returns ``(exit code, output)``.
    A command that does not finish in time returns code -1."""
    try:
        done = subprocess.run(
            [str(python), "-I", "-B", str(plugin_dir / "run.py"), "--home", str(home), *args],
            capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return -1, "timed out after %d s" % int(timeout)
    except OSError as exc:
        return -1, "could not run: %s" % exc
    return done.returncode, (done.stdout + done.stderr).strip()


def current_install_records(home: Path) -> List[Dict[str, Any]]:
    """The ``install-state.json`` of every install made since the last uninstall that moved the plugin away, oldest
    first. (What an earlier installation found says nothing about the provider that this one replaced.)"""
    removed = "%s-removed-" % NAME
    ended = max((folder.name[len(removed):] for folder in (home / "backups").glob(removed + "*")
                 if (folder / "plugin").exists() or (folder / "data").exists()), default="")
    records = []
    for folder in sorted((home / "backups").glob("%s-2*" % NAME)):
        if folder.name[len(NAME) + 1:] > ended:
            record = read_json(folder / "install-state.json")
            if record:
                records.append(record)
    return records


def original_settings(home: Path, current: Dict[str, Any]) -> Dict[str, Any]:
    """What the config looked like before the bank was first installed. A
    re-install sees the bank already switched on, so the record of the very
    first install is carried forward instead of being overwritten."""
    if current.get("provider") != NAME:
        return current
    earlier = read_json(home / "plugin-data" / NAME / "install.json")
    kept = earlier.get("previous") or {}
    if earlier.get("activated") and kept and kept.get("provider") != NAME:
        return kept
    for record in current_install_records(home):
        kept = record.get("previous") or {}
        if kept and kept.get("provider") != NAME:
            return kept
    # No record of it survives: the original provider is not known; built-in it is.
    return {**current, "provider": None, "enabled": [p for p in current.get("enabled", []) if p != NAME]}


def recorded_provider(home: Path) -> str:
    """The provider this profile had before the bank was installed, from the oldest install backup of this
    installation: for an install.json that does not say (empty, or damaged by a failed re-install)."""
    for record in current_install_records(home):
        provider = (record.get("previous") or {}).get("provider")
        if provider and provider != NAME:
            return str(provider)
    return ""


# ----------------------------------------------------------------------------- switching on and off

def switch_on(cli: Hermes, config: Path) -> Tuple[str, Dict[str, Any], List[str]]:
    """Enable the plugin and make it the memory provider through Hermes' own commands.
    Returns (``already`` | ``switched`` | ``failed``, the settings found before, notes)."""
    notes: List[str] = []
    provider, enabled = cli.provider(), cli.enabled()
    seen = scan_config(config)
    previous = {"provider": (provider if provider is not None else seen["provider"]) or None,
                "enabled": enabled if enabled is not None else seen["enabled"],
                "had_memory_section": seen["had_memory_section"], "config": str(config)}
    if provider == NAME and NAME in (enabled or []):
        status = cli.plugin_status(NAME)      # Hermes lets plugins.disabled win over plugins.enabled
        if status == "enabled" or status is None:
            return "already", previous, ["already the memory provider and enabled: config.yaml left as it is"]
        notes.append("the plugin is listed as %r although it is enabled and the provider: enabling it again" % status)
    code, text = cli.text("plugins", "enable", NAME)
    if code != 0:
        return "failed", previous, ["`hermes plugins enable %s` failed (%d): %s" % (NAME, code, text[-400:])]
    notes.append("plugin enabled")
    if provider != NAME:
        code, text = cli.text("config", "set", "memory.provider", NAME)
        if code != 0:
            return "failed", previous, notes + ["`hermes config set memory.provider %s` failed (%d): %s"
                                                % (NAME, code, text[-400:])]
        notes.append("memory.provider set to %s" % NAME)
    return "switched", previous, notes


def verify_active(cli: Hermes) -> Tuple[bool, str]:
    provider = cli.provider()
    status = cli.plugin_status(NAME)
    enabled = cli.enabled()
    on = status == "enabled" or (status is None and NAME in (enabled or []))
    if provider == NAME and on:
        return True, "memory provider is %s and the plugin is enabled (read back from Hermes)" % NAME
    return False, "Hermes reports provider=%r, plugin status=%r" % (provider, status if status is not None else enabled)


def switch_off(cli: Hermes, restore: str) -> List[str]:
    """Undo only what the install set: another provider chosen since is left alone."""
    notes: List[str] = []
    provider = cli.provider()
    if provider == NAME:
        if restore:
            code, text = cli.text("config", "set", "memory.provider", restore)
        else:
            code, text = cli.text("config", "unset", "memory.provider")
            if code != 0 and cli.provider() in ("", None):
                code = 0          # it was not set to begin with
        if code != 0:
            raise RuntimeError("could not restore memory.provider: " + text[-400:])
        notes.append("memory.provider -> %s" % (restore or "(built-in)"))
    elif provider is not None:
        notes.append("memory.provider is %r, not %s: left alone" % (provider, NAME))
    if NAME in (cli.enabled() or []):
        code, text = cli.text("plugins", "disable", NAME)
        if code != 0:
            raise RuntimeError("could not disable the plugin: " + text[-400:])
        notes.append("plugin disabled")
    return notes


def still_switched_on(cli: Optional[Hermes], config: Path) -> str:
    """After an uninstall wrote the settings: "" when the profile no longer has the Memory Bank as its provider and
    enabled (read back from Hermes, or from config.yaml on the Python route), else a sentence saying what is still set.
    Exit codes and Hermes' words are not enough: a Hermes managed by a package manager refuses `config set` yet exits 0."""
    seen = scan_config(config)
    provider = cli.provider() if cli is not None else None
    if provider is None:
        provider = seen["provider"]
    if provider == NAME:
        return "memory.provider is still %s" % NAME
    if cli is not None:
        status = cli.plugin_status(NAME)
        on = status == "enabled" or (status is None and NAME in (cli.enabled() or []))
    else:
        on = NAME in seen["enabled"]
    return "the plugin is still enabled" if on else ""


# ----------------------------------------------------------------------------- the plugin copy

def _ignored(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in COPY_IGNORE)


def _is_source(folder: Any) -> bool:
    return os.path.normcase(os.path.abspath(str(folder))) == os.path.normcase(os.path.abspath(str(SOURCE)))


def copy_ignore(folder: str, names: List[str]) -> List[str]:
    """``shutil.copytree``'s ``ignore``: what file managers add everywhere, and in the repository's top folder
    everything that is not one of PLUGIN_ENTRIES."""
    skip = [name for name in names if _ignored(name)]
    if _is_source(folder):
        skip += [name for name in names if name not in PLUGIN_ENTRIES and name not in skip]
    return skip


def plugin_files(folder: Path) -> Dict[str, int]:
    """Every file an install copies out of ``folder``: {relative path with forward slashes: size in bytes}."""
    found: Dict[str, int] = {}
    for current, dirs, files in os.walk(str(folder), followlinks=True):
        top = _is_source(current)
        dirs[:] = [d for d in dirs if not _ignored(d) and (not top or d in PLUGIN_ENTRIES)]
        for name in files:
            if not _ignored(name) and (not top or name in PLUGIN_ENTRIES):
                full = os.path.join(current, name)
                found[os.path.relpath(full, str(folder)).replace(os.sep, "/")] = os.path.getsize(full)
    return found


def copy_problems(source: Path, installed: Path) -> str:
    """"" when ``installed`` holds the same files (names and sizes) as ``source``, else which ones differ."""
    want, have = plugin_files(source), plugin_files(installed)
    differences = (("missing", sorted(set(want) - set(have))), ("not in the source", sorted(set(have) - set(want))),
                   ("different size", sorted(name for name in want if name in have and want[name] != have[name])))
    return "; ".join("%s: %s%s" % (label, ", ".join(names[:5]), ", ..." if len(names) > 5 else "")
                     for label, names in differences if names)


# ----------------------------------------------------------------------------- one run at a time

class InstallLock:
    """An exclusive lock for one profile, so that two installers (or an install and an uninstall) never work on it
    at once. It is a file made with O_CREAT|O_EXCL in the profile's plugin-data folder and removed when the run ends.
    A running installer touches it every LOCK_REFRESH_SECONDS; one that was killed leaves it behind, and a lock that
    has not been touched for LOCK_STALE_SECONDS is taken over (with a notice)."""

    def __init__(self, home: Path, what: str) -> None:
        self.path = home / "plugin-data" / LOCK_NAME
        self.what = what
        self.notice = ""
        self._token = os.urandom(8).hex()
        self._held = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _age(self) -> Optional[float]:
        try:
            return max(0.0, time.time() - self.path.stat().st_mtime)
        except OSError:
            return None

    def _refusal(self, age: Optional[float]) -> str:
        info = read_json(self.path)
        who = ""
        if age is not None:
            who = " (it was made %d s ago%s)" % (age, " by process %s" % info["pid"] if info.get("pid") else "")
        return ("ERROR: another install or uninstall of this profile seems to be running%s: the lock file\n"
                "    %s\n"
                "  exists. Wait until that run has finished. If none is running (an earlier run was killed), delete\n"
                "  that file and run this again. Nothing was changed." % (who, self.path))

    def acquire(self) -> str:
        """"" when the lock is held now, else a message that says why not and what to do."""
        try:
            private_dir(self.path.parent)
        except OSError as exc:
            return "ERROR: cannot create %s (%s). Nothing was changed." % (self.path.parent, exc)
        taken_over = False
        for attempt in range(8):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                age = self._age()
                if not taken_over and (age is None or age > LOCK_STALE_SECONDS):
                    taken_over = True
                    if age is not None:
                        self.notice = ("NOTICE: the lock file %s was last touched %d minutes ago (a running installer touches "
                                       "it every %d seconds), so the run that made it is taken to be dead and the lock is "
                                       "taken over." % (self.path, age // 60, LOCK_REFRESH_SECONDS))
                        try:
                            os.unlink(str(self.path))
                        except OSError:
                            pass
                    continue
                return self._refusal(age)
            except PermissionError as exc:
                # On Windows a file that another run has deleted a moment ago is "pending delete" and cannot be made
                # again for a short while: give it that moment before taking the refusal for a real one.
                if attempt < 7:
                    time.sleep(0.1)
                    continue
                if self.path.exists():
                    return self._refusal(self._age())
                return "ERROR: cannot create the lock file %s (%s). Nothing was changed." % (self.path, exc)
            except OSError as exc:
                return "ERROR: cannot create the lock file %s (%s). Nothing was changed." % (self.path, exc)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "what": self.what, "token": self._token,
                                         "started": time.strftime("%Y-%m-%d %H:%M:%S")}))
            self._held = True
            self._thread = threading.Thread(target=self._keep_fresh, name="memorybank-lock", daemon=True)
            self._thread.start()
            return ""
        return self._refusal(self._age())

    def _keep_fresh(self) -> None:
        while not self._stop.wait(LOCK_REFRESH_SECONDS):
            try:
                os.utime(str(self.path), None)
            except OSError:
                pass

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._held and read_json(self.path).get("token") == self._token:     # never somebody else's lock
            try:
                os.unlink(str(self.path))
            except OSError:
                pass
        self._held = False


# ----------------------------------------------------------------------------- the rollback note

ROLLBACK = """# Memory Bank - rollback ({stamp})

Profile: `{home}`

## Before you roll back (optional)

A rollback gives something up: MEMORY.md and USER.md are as full as they were, so saves that do not fit are
refused again. Anything that was saved beyond those files while the bank was active stays in the bank, not in the
files. Look at it BEFORE you run the command below - once it has run, `hermes {flag}memorybank` no longer exists
(Hermes only offers the commands of the memory provider that is switched on):

- `hermes {flag}memorybank search <words>` lists what the bank holds about something;
- `hermes {flag}memorybank export` writes a readable JSON copy of what the viewer shows (nothing reads it back in);
- to keep the bank itself, quit Hermes and copy `{home}{sep}plugin-data{sep}memorybank{sep}bank.db` (and `bank.db-wal`
  and `bank.db-shm` if they are there) somewhere safe.

## One command

{command_block}
It restores `memory.provider` to {provider_text} and takes the plugin off `plugins.enabled` (only what the install
set: another provider chosen since is left alone; on the `hermes` route the plugin is also listed under
`plugins.disabled`, on the Hermes-Python route it is only taken off `plugins.enabled`). It moves `plugins/memorybank`
into a NEW folder, `backups/memorybank-removed-<time>/plugin` - the command prints the exact path - not into the
folder this file is in. The bank's data (`plugin-data/memorybank/`) is kept; add `--purge` to move it into `data`
in that new folder as well (that also moves `memorybank.json`, your settings file). Quit Hermes first: on Windows a
file that is open stops the move, but macOS and Linux would not notice and Hermes would keep writing into the moved
bank. Restart Hermes afterwards. The installer used is the copy in this folder, so this works even if the folder it
came from is gone. If the command stops part-way it says which step failed; running it again finishes the job.
To use the Memory Bank again after a rollback run the installer again: `hermes {flag}plugins enable memorybank` only
works while `plugins/memorybank` is still in place.

## After the rollback

`hermes {flag}memorybank ...` is gone, but the removed copy of the plugin can still read the bank without Hermes: use
the `run.py` in the plugin folder that the command printed (`backups{sep}memorybank-removed-<time>{sep}plugin`), this
profile as `--home`, and the same Python as above:

{runner_block}
(`export <file>` works the same way.) That works while the bank is still in `{home}{sep}plugin-data{sep}memorybank`,
which is the case after a rollback WITHOUT `--purge`. After `--purge` the bank is in the `data` folder of the new
folder: put it back as `{home}{sep}plugin-data{sep}memorybank` first.

To go back to an EARLIER BUILD of the plugin instead (after a re-install): every
re-install keeps the copy it replaced as `plugin-previous` in its own backup folder
under `backups/`. With Hermes closed, move the current `plugins/memorybank` away and
put that folder in its place.

## By hand (if the command cannot run)

1. Quit Hermes.
2. Run:

```
{manual_commands}
```

   (or open `{home}{sep}config.yaml` and set `memory.provider` back to {provider_text}; before the
   install `plugins.enabled` was: {enabled_before}. The `config.yaml` in this folder is the file as it
   was when this installer ran. For the first install that is your original - copy it back only if you
   have changed no settings since. After a re-install it already holds the Memory Bank settings, so use
   the commands above instead.)
3. Move or delete `{home}{sep}plugins{sep}memorybank`.
4. Optional: remove `{home}{sep}plugin-data{sep}memorybank` (the bank) and
   `{home}{sep}memorybank.json` (its settings).
5. In the desktop app, switch off "Memory Bank" under Capabilities > Plugins if it was on.
6. Start Hermes.

## What the install changed

- copied the plugin to `plugins/memorybank/`
- `config.yaml`: `memory.provider` {provider_before} -> `memorybank`; `memorybank` enabled as a plugin
  (was: {enabled_before}) - {activation_text}
- created the bank under `plugin-data/memorybank/` by indexing this profile's skills,
  MEMORY.md / USER.md and session history (read-only on those)
- nothing else: MEMORY.md, USER.md, the skills folder and state.db are only ever read

## Keep / roll back rule of thumb

Keep it if, after a few days: `hermes {flag}memorybank status` shows recalls happening and
"saved beyond them" growing instead of saves being refused, the recall lines you see at
the top of turns are relevant, `hermes {flag}memorybank check` still says everything is in the
bank, and `hermes {flag}memorybank usage` shows cost per call has not moved against you. Roll back if
recall is noisy (first try lowering `recall.max_chars` or raising `recall.min_coverage`
in `memorybank.json`; `recall.standing_chars: 0` switches off the always-on notes).
"""


def profile_flag(args: argparse.Namespace) -> str:
    """``--profile <name> `` (quoted for the shell) for the `hermes` commands shown to the user; for the root
    ``--profile default ``: without the flag Hermes follows the profile `hermes profile use` chose, which may be another."""
    return "--profile default " if args.root else "--profile %s " % plain(args.profile)


def target_args(args: argparse.Namespace, root: Path, cli_exe: Optional[str], python: Optional[Path], quoter=None) -> str:
    q = quoter or quote
    parts = ["--root" if args.root else "--profile %s" % q(args.profile), "--hermes-root %s" % q(root)]
    if cli_exe:
        parts.append("--hermes %s" % q(cli_exe))
    elif getattr(args, "no_cli", False):
        parts.append("--no-cli")
    if python is not None:
        parts.append("--python %s" % q(python))
    if getattr(args, "agent_dir", None):
        parts.append("--agent-dir %s" % q(args.agent))
    return " ".join(parts)


def python_command() -> str:
    """The interpreter that is running this installer: what every printed command that starts a Python names."""
    return sys.executable or ("python" if IS_WINDOWS else "python3")


def command_blocks(forms: List[Tuple[str, str]]) -> str:
    """Fenced command blocks, one per shell: ``[("PowerShell", command), ("Command Prompt", command)]``."""
    return "\n".join("%s:\n\n```\n%s\n```\n" % (shell, command) for shell, command in forms)


def runner_forms(python: str, run_py: Any, home: Any) -> List[Tuple[str, str]]:
    """How to read the bank without Hermes, with the plugin's own runner, for each shell of this platform."""
    if IS_WINDOWS:
        return [("PowerShell", "& %s %s --home %s search <words>" % (ps_quote(python), ps_quote(run_py), ps_quote(home))),
                ("Command Prompt", "%s %s --home %s search <words>" % (cmd_quote(python), cmd_quote(run_py), cmd_quote(home)))]
    return [("Terminal", "%s %s --home %s search <words>" % (quote(python), quote(run_py), quote(home)))]


def write_rollback(backup: Path, *, stamp: str, home: Path, args: argparse.Namespace, root: Path,
                   cli_exe: Optional[str], python: Optional[Path], previous: Dict[str, Any], activation: str) -> None:
    py, installer = python_command(), backup / "install.py"
    if IS_WINDOWS:
        ps = "& %s %s %s --uninstall" % (ps_quote(py), ps_quote(installer), target_args(args, root, cli_exe, python, ps_quote))
        cmd = "%s %s %s --uninstall" % (cmd_quote(py), cmd_quote(installer), target_args(args, root, cli_exe, python, cmd_quote))
        block = command_blocks([("PowerShell", ps), ("Command Prompt", cmd)])
        if "%" in cmd:
            block += ("\nNote: Command Prompt expands %NAME% even inside quotes and nothing escapes that; a path above "
                      "contains a percent sign, so use the PowerShell form.\n")
    else:
        block = command_blocks([("Terminal", "%s %s %s --uninstall" % (quote(py), quote(installer),
                                                                      target_args(args, root, cli_exe, python)))])
    flag = profile_flag(args)
    provider = previous.get("provider")
    restore = ("hermes %sconfig set memory.provider %s" % (flag, plain(provider))) if provider else \
              ("hermes %sconfig unset memory.provider" % flag)
    manual = "%s\nhermes %splugins disable %s" % (restore, flag, NAME)
    shown = re.sub(r"[`\r\n]+", " ", str(provider)) if provider else ""
    text = ROLLBACK.format(
        stamp=stamp, home=home, sep=os.sep, flag=flag, command_block=block, manual_commands=manual,
        runner_block=command_blocks(runner_forms(py, home / "backups" / ("%s-removed-<time>" % NAME) / "plugin" / "run.py", home)),
        provider_text="`%s`" % shown if provider else "(built-in: empty)",
        provider_before="`%s`" % shown if provider else "(unset = built-in)",
        enabled_before=re.sub(r"[`\r\n]+", " ", ", ".join(str(p) for p in previous.get("enabled") or [])) or "(empty)",
        activation_text={"cli": "done with Hermes' own commands", "python": "written by Hermes' config writer",
                         "already": "it was already switched on, so nothing was written",
                         "manual": "NOT switched on by the installer: see the end of its output",
                         "none": "left alone (--no-activate)"}.get(activation, activation),
    )
    (backup / "ROLLBACK.md").write_text(text, encoding="utf-8")


# ----------------------------------------------------------------------------- install

def dry_run_note(args: argparse.Namespace) -> None:
    say("\n(dry run: no setting, no plugin file and no backup was written.)")
    if getattr(args, "hermes_started", False):
        say("To read its version and where its config is, the installer started `hermes`; Hermes may write its own "
            "log or cache files whenever it starts.")


def index_bank(cli: Optional[Hermes], target_dir: Path, home: Path, active: bool, flag: str = "") -> bool:
    """Build the bank from this profile's skills, memory files and sessions; True if complete.
    ``flag`` is the ``--profile <name> `` part of the `hermes` commands named in the warnings."""
    complete = True
    say("  indexing skills, MEMORY.md / USER.md and past sessions ...")
    for command, wait in (("import", 1800.0), ("check", 600.0)):
        if command == "check":
            say("  checking that everything this profile knows is in the bank ...")
        code, text = -1, ""
        if cli is not None and active:
            code, text = cli.text("memorybank", command, timeout=wait)
            gone = any(s in text for s in ("invalid choice", "unrecognized arguments", "No such command"))
            if code != 0 and (gone or code == -1):
                code = -2   # the command is not registered (yet) or did not run: use the plugin's own runner below
        if cli is None or not active or code == -2:
            code, text = run_tool(Path(sys.executable), target_dir, home, command, timeout=wait)
            if command == "import":
                text += ("\n(indexed without Hermes' content scanner; inside Hermes every line that recall or "
                         "search shows is screened as it goes out)")
        for line in text.splitlines():
            say("    " + line)
        if code != 0:
            complete = False
            if command == "import":
                say("  WARNING: indexing did not finish. Run: hermes %smemorybank import" % flag)
            else:
                say("  WARNING: the check found gaps. Run: hermes %smemorybank import --force   then   "
                    "hermes %smemorybank check" % (flag, flag))
    return complete


def install(args: argparse.Namespace, root: Path, home: Path, cli: Optional[Hermes], cli_note: str,
            python: Optional[Path]) -> int:
    target_dir = home / "plugins" / NAME
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = home / "backups" / ("%s-%s" % (NAME, stamp))
    method = "none" if args.no_activate else ("cli" if cli else ("python" if python else "manual"))
    say("Memory Bank %s -> %s" % (plugin_version(), home))
    say("  plugin     %s%s" % (target_dir, "  (replaces the existing copy; the old one goes to the backup)"
                               if target_dir.exists() else ""))
    if method == "cli":
        say("  switch on  with Hermes' own commands (%s): plugins enable + memory.provider -> %s" % (cli_note, NAME))
    elif method == "python":
        say("  switch on  through Hermes' config writer (%s): memory.provider -> %s; plugins.enabled += %s"
            % (python, NAME, NAME))
        say("             (no usable `hermes` command: %s)" % cli_note)
    elif method == "manual":
        say("  switch on  by hand - %s; the two commands to run are printed at the end" % cli_note)
    else:
        say("  switch on  not now (--no-activate)")
    say("  bank       %s" % (home / "plugin-data" / NAME / "bank.db"))
    say("  backup     %s" % backup)
    missing = [name for name in ("plugin.yaml", "__init__.py") if not (SOURCE / name).is_file()]
    if missing:          # a lone copy of this file (or the one kept in a backup folder) is not the plugin
        say("ERROR: plugin source not found at %s (no %s there). Nothing was changed." % (SOURCE, " or ".join(missing)))
        return 2
    if args.dry_run:
        dry_run_note(args)
        return 0

    lock = InstallLock(home, "install")
    refusal = lock.acquire()
    if refusal:
        say(refusal)
        return 1
    try:
        if lock.notice:
            say("  " + lock.notice)
        return _install(args, root, home, cli, cli_note, python, target_dir, stamp, backup, method)
    finally:
        lock.release()


def _install(args: argparse.Namespace, root: Path, home: Path, cli: Optional[Hermes], cli_note: str,
             python: Optional[Path], target_dir: Path, stamp: str, backup: Path, method: str) -> int:
    config = home / "config.yaml"
    backup = fresh_dir(backup.parent, backup.name)
    if config.exists():
        shutil.copy2(str(config), str(backup / "config.yaml"))
    sidecar = home / ("%s.json" % NAME)
    if sidecar.exists():
        shutil.copy2(str(sidecar), str(backup / sidecar.name))
    shutil.copy2(str(Path(__file__).resolve()), str(backup / "install.py"))

    # The new copy is made beside the old one and compared with the source before anything is changed; then the
    # old one is set aside by a plain rename and the new one renamed into its place. If a file in the old copy is
    # open (Hermes, an editor) that rename fails - and then nothing has been changed.
    plugins_dir = target_dir.parent
    staging = plugins_dir / (".%s-installing-%d" % (NAME, os.getpid()))
    previous_copy = backup / "plugin-previous"
    try:
        plugins_dir.mkdir(parents=True, exist_ok=True)
        for leftover in plugins_dir.glob(".%s-installing-*" % NAME):      # we hold the lock: a run that died left these
            shutil.rmtree(str(leftover), ignore_errors=True)
        shutil.copytree(str(SOURCE), str(staging), ignore=copy_ignore)
        problem = copy_problems(SOURCE, staging)
        if problem:
            raise RuntimeError("the copy does not match the source (%s)" % problem)
    except Exception as exc:
        say("  ERROR while copying the plugin: %s" % exc)
        say("  Nothing was changed.")
        shutil.rmtree(str(staging), ignore_errors=True)
        shutil.rmtree(str(backup), ignore_errors=True)
        return 1
    if os.path.lexists(str(target_dir)):
        try:
            move(target_dir, previous_copy)
        except OSError as exc:
            say("  ERROR: files in %s are in use (%s)." % (target_dir, exc.__class__.__name__))
            say("  Quit Hermes and close editors or shells opened there, then run this again. Nothing was changed.")
            shutil.rmtree(str(staging), ignore_errors=True)
            shutil.rmtree(str(backup), ignore_errors=True)
            return 1
    try:
        move(staging, target_dir)
        problem = copy_problems(SOURCE, target_dir)
        if problem:
            raise RuntimeError("the installed plugin does not match the source (%s)" % problem)
    except Exception as exc:
        say("  ERROR while putting the plugin in place: %s" % exc)
        shutil.rmtree(str(staging), ignore_errors=True)
        if os.path.lexists(str(target_dir)):
            try:
                move(target_dir, backup / "plugin-failed")
            except OSError:
                shutil.rmtree(str(target_dir), ignore_errors=True)
        if previous_copy.exists():
            try:
                move(previous_copy, target_dir)
                say("  The previous copy was put back.")
            except OSError:
                say("  The previous copy is in %s." % previous_copy)
        return 1
    say("  copied the plugin")

    state: Dict[str, Any] = {"installed_at": stamp, "home": str(home), "backup": str(backup), "activated": False,
                             "activation": method, "version": plugin_version(), "platform": sys.platform,
                             "hermes_command": cli.exe if cli else None}
    attention: List[str] = []
    flag = profile_flag(args)
    seen = scan_config(config)
    was_active = seen["provider"] == NAME and NAME in seen["enabled"] and NAME not in seen["disabled"]
    previous: Dict[str, Any] = original_settings(home, seen)      # every branch below keeps the record carried forward
    try:
        if method == "cli":
            assert cli is not None
            outcome, found, notes = switch_on(cli, config)
            previous = original_settings(home, found)
            for note in notes:
                say("  " + note)
            if outcome == "failed":
                raise RuntimeError(notes[-1])
            state["activation"] = "already" if outcome == "already" else "cli"
            ok, detail = verify_active(cli)
            say("  verified: " + detail if ok else "  NOT verified: " + detail)
            state["activated"] = ok
            if not ok:
                attention.append("Hermes does not report the Memory Bank as active yet: %s. To switch it on run: "
                                 "hermes %splugins enable %s   and   hermes %sconfig set memory.provider %s   "
                                 "then check with: hermes %smemory status" % (detail, flag, NAME, flag, NAME, flag))
        elif method == "python":
            assert python is not None
            found = edit_config(python, args.agent, home, "activate")
            previous = original_settings(home, found)
            say("  config updated by Hermes' config writer; reading config.yaml back ...")
            seen = scan_config(config)                    # nothing else confirms it on this route: look at the file
            ok = seen["provider"] == NAME and NAME in seen["enabled"] and NAME not in seen["disabled"]
            state["activated"] = ok
            if ok:
                say("  verified: config.yaml has memory.provider = %s and the plugin enabled" % NAME)
            else:
                say("  NOT verified: config.yaml shows provider=%r, enabled=%s, disabled=%s"
                    % (seen["provider"], NAME in seen["enabled"], NAME in seen["disabled"]))
                attention.append("Hermes' config writer ran, but config.yaml does not show the Memory Bank as the "
                                 "provider and enabled; check it with `hermes %smemory status`." % flag)
        else:
            if method == "none":
                say("  config left alone (--no-activate)")
            else:
                attention.append("Not switched on: %s." % cli_note)
    except Exception as exc:
        say("  ERROR while switching the plugin on: %s" % exc)
        if was_active:
            # An earlier install switched it on and config.yaml still says so: that is not undone by this failure.
            say("  The plugin is copied. The profile was already switched on by an earlier install and config.yaml "
                "still shows that. Backup: %s" % backup)
            state["activation"], state["activated"] = "already", True
            attention.append("The switch-on could not be confirmed with Hermes (see the error above); check it with "
                             "`hermes %smemory status`." % flag)
        else:
            say("  The plugin is copied but NOT switched on. Backup: %s" % backup)
            attention.append("Switch it on with the two commands below.")
            method = "manual"
            state["activation"] = "manual"
    other = previous.get("provider")
    if other and other not in (NAME, "builtin", "built-in", "default", "none"):
        say("  note: this profile was using the '%s' memory provider; it is recorded for rollback." % other)
    state["previous"] = previous

    (backup / "install-state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    data_dir = home / "plugin-data" / NAME
    private_dir(data_dir)
    (data_dir / "install.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    write_rollback(backup, stamp=stamp, home=home, args=args, root=root, cli_exe=cli.exe if cli else None,
                   python=python if method == "python" else None, previous=previous, activation=state["activation"])

    complete = True
    if not args.no_index:
        complete = index_bank(cli, target_dir, home, bool(state["activated"]), flag)
    if method == "manual" or (method == "none" and not state["activated"]):
        say("\nTo finish, run these two commands (they are what the installer would have run):")
        say("    hermes %splugins enable %s" % (flag, NAME))
        say("    hermes %sconfig set memory.provider %s" % (flag, NAME))
    if not complete:
        attention.append("The index is incomplete (see above).")
    say(("\nInstalled, but: " + " ".join(attention)) if attention else "\nDone.")
    say("Rollback instructions: %s" % (backup / "ROLLBACK.md"))
    say("Next: restart Hermes (quit and reopen the desktop app) so new sessions use it, then switch on")
    say("      'Memory Bank' under Capabilities > Plugins (main sidebar) to get the network page.")
    say("Later:  hermes %smemorybank usage   shows what your chats cost, week by week." % flag)
    return 3 if attention else 0


def uninstall(args: argparse.Namespace, root: Path, home: Path, cli: Optional[Hermes], cli_note: str,
              python: Optional[Path]) -> int:
    target_dir = home / "plugins" / NAME
    data_dir = home / "plugin-data" / NAME
    state = read_json(data_dir / "install.json")
    if not state and not target_dir.exists():
        say("Memory Bank is not installed in %s: nothing to undo." % home)
        return 0
    previous = state.get("previous") or {}
    damaged = (not previous) or previous.get("provider") == NAME     # the signature of a record a failed re-install overwrote
    restore = previous.get("provider") or ""
    if restore == NAME:
        restore = ""
    if not restore and damaged:
        restore = recorded_provider(home)       # an install.json damaged by a failed re-install is repaired from the backups
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = home / "backups" / ("%s-removed-%s" % (NAME, stamp))
    method = "cli" if cli else ("python" if python else "manual")
    flag = profile_flag(args)
    settings_back = False
    if method == "manual":
        seen = scan_config(home / "config.yaml")
        settings_back = seen["provider"] != NAME and NAME not in seen["enabled"]
    say("Removing Memory Bank from %s" % home)
    say("  config   memory.provider -> %s (only if it is still %s); plugin removed from plugins.enabled%s"
        % (restore or "(built-in)", NAME,
           "" if method != "manual" else
           ("  [config.yaml already shows the settings back: nothing to change there]" if settings_back
            else "  [BY HAND: %s]" % cli_note)))
    say("  plugin   %s -> %s" % (target_dir, backup / "plugin"))
    say("  bank     %s" % ("moved to %s (and memorybank.json, if there is one)" % (backup / "data") if args.purge
                           else "kept at %s" % data_dir))
    if args.dry_run:
        dry_run_note(args)
        return 0
    if method == "manual" and not settings_back:
        say("ERROR: no usable `hermes` command and no Hermes Python, and config.yaml still shows the Memory Bank as the "
            "provider or as enabled: the settings cannot be put back automatically.")
        say("  Put them back by hand - run these two commands (or edit config.yaml):")
        say("    hermes %s%s" % (flag, "config set memory.provider %s" % plain(restore) if restore else "config unset memory.provider"))
        say("    hermes %splugins disable %s" % (flag, NAME))
        say("  then run this again: once config.yaml no longer shows the Memory Bank, the installer moves the plugin")
        say("  folder without needing Hermes. Or give it a way to do it all: --hermes <path of the hermes command>,")
        say("  or --python <Hermes' Python>.")
        say("Nothing was changed.")
        return 2

    lock = InstallLock(home, "uninstall")
    refusal = lock.acquire()
    if refusal:
        say(refusal)
        return 1
    try:
        if lock.notice:
            say("  " + lock.notice)
        return _uninstall(args, home, cli, python, target_dir, data_dir, backup, restore, method, flag)
    finally:
        lock.release()


def _uninstall(args: argparse.Namespace, home: Path, cli: Optional[Hermes], python: Optional[Path], target_dir: Path,
               data_dir: Path, backup: Path, restore: str, method: str, flag: str) -> int:
    backup = fresh_dir(backup.parent, backup.name)
    if (home / "config.yaml").exists():
        shutil.copy2(str(home / "config.yaml"), str(backup / "config.yaml"))
    sidecar = home / ("%s.json" % NAME)

    def probe(path: Path, holder: Path, what: str) -> int:
        """Is ``path`` free to move? Find out first, by moving it into ``holder`` and straight back (on Windows an
        open file makes the first move fail; elsewhere nothing does). Only a failure of the FIRST move means that
        nothing has moved: if the way back fails, it is in the backup and must stay there. 0 when all is well."""
        stranded = False
        try:
            move(path, holder)
            stranded = True
            move(holder, path)
        except OSError as exc:
            if stranded:
                say("  ERROR: %s was moved to %s and could not be moved back (%s)." % (what, holder, exc.__class__.__name__))
                say("  Nothing else was changed. Move it back by hand (to %s) or run this again." % path)
                return 1
            say("  ERROR: files in %s are in use (%s)." % (path, exc.__class__.__name__))
            say("  Quit Hermes and close editors or shells opened there, then run this again. Nothing was changed.")
            shutil.rmtree(str(backup), ignore_errors=True)       # only the copy of config.yaml made just now is in it
            return 1
        return 0

    if target_dir.exists():
        if probe(target_dir, backup / "plugin", "the plugin folder"):
            return 1
    if args.purge:
        if data_dir.exists() and probe(data_dir, backup / "data", "the bank folder"):
            return 1
        if sidecar.exists() and probe(sidecar, backup / sidecar.name, "the settings file"):
            return 1
    try:
        if method == "cli":
            assert cli is not None
            for note in switch_off(cli, restore):
                say("  " + note)
        elif method == "python":
            assert python is not None
            edit_config(python, args.agent, home, "deactivate", restore)
            say("  config restored")
        else:
            say("  config.yaml already shows the settings back: nothing to change there")
    except Exception as exc:
        say("  ERROR while restoring the settings: %s" % exc)
        say("  Some settings may already be back (the first step runs first). The plugin folder was left where it is.")
        say("  Run the same command again to finish, or follow %s by hand." % (home / "backups"))
        return 1
    if method != "manual":
        still = still_switched_on(cli, home / "config.yaml")
        if still:                                  # a zero exit is not proof: look at what is there now
            say("  ERROR: %s after the change, so the settings were NOT put back (a Hermes managed by a package "
                "manager, for one, refuses `config set` and `config unset` yet exits 0)." % still)
            say("  The plugin folder was left where it is. Put the settings back yourself, then run this again:")
            say("    hermes %s%s" % (flag, "config set memory.provider %s" % plain(restore) if restore else "config unset memory.provider"))
            say("    hermes %splugins disable %s" % (flag, NAME))
            return 3

    unfinished: List[str] = []
    plugin_moved = False
    if target_dir.exists():
        try:
            move(target_dir, backup / "plugin")
            plugin_moved = True
            say("  plugin moved to the backup")
        except OSError as exc:
            unfinished.append("The settings are restored, but the plugin folder %s is in use (%s) and was left in place: "
                              "quit Hermes and move it yourself, or run the same command again." % (target_dir, exc.__class__.__name__))
    if args.purge and not unfinished and data_dir.exists():        # not while the plugin is still there to use it
        try:
            move(data_dir, backup / "data")
            say("  bank moved to the backup")
        except OSError as exc:
            unfinished.append("The settings are restored and the plugin is moved, but the bank %s is in use (%s) and "
                              "was left in place: quit Hermes, then run the same command again WITH --purge."
                              % (data_dir, exc.__class__.__name__))
    if args.purge and sidecar.exists() and not unfinished:
        try:
            move(sidecar, backup / sidecar.name)
        except OSError as exc:
            unfinished.append("The bank is moved, but the settings file %s is in use (%s) and was left in place: "
                              "move it yourself." % (sidecar, exc.__class__.__name__))
    if unfinished:
        for line in unfinished:
            say("  ERROR: " + line)
        return 3
    say("\nDone. Restart Hermes. (Everything removed is in %s)" % backup)
    if plugin_moved and not args.purge and data_dir.exists():
        say("Read what the bank holds, without Hermes (the removed plugin copy runs on its own; put your words where <words> is):")
        for shell, command in runner_forms(python_command(), backup / "plugin" / "run.py", home):
            say("    %-15s %s" % (shell + ":", command))
    if not IS_WINDOWS:
        say("Hermes keeps files open that this cannot see: if it was running, quit it and restart it before using the profile.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Install or remove the Hermes Memory Bank in one profile.")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--profile", help="Profile name (a folder under <hermes root>/profiles/)")
    where.add_argument("--root", action="store_true", help="The default profile (the Hermes root itself)")
    parser.add_argument("--hermes-root", help="Hermes root folder (default: the standard location)")
    parser.add_argument("--hermes", help="The `hermes` command to use (default: the one on PATH)")
    parser.add_argument("--no-cli", action="store_true", help="Do not use the `hermes` command (rarely needed)")
    parser.add_argument("--python", help="Hermes' Python, for the fallback config edit (default: hermes-agent/venv)")
    parser.add_argument("--agent-dir", help="Hermes source folder (default: <hermes root>/hermes-agent)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change and stop")
    parser.add_argument("--uninstall", action="store_true", help="Restore the previous settings and remove the plugin")
    parser.add_argument("--purge", action="store_true", help="With --uninstall: also move the bank's data and memorybank.json to the backup")
    parser.add_argument("--no-activate", action="store_true", help="Copy the plugin but leave config.yaml alone")
    parser.add_argument("--no-index", action="store_true", help="Skip the initial indexing")
    args = parser.parse_args(argv)

    if sys.version_info < (3, 8):
        say("ERROR: this installer needs Python 3.8 or newer (this is %s)." % sys.version.split()[0])
        return 2
    if args.profile is not None:
        args.profile, problem = clean_profile_name(args.profile)
        if problem:
            say("ERROR: " + problem)
            return 2
    root = absolute(args.hermes_root) if args.hermes_root else absolute(default_root())
    if args.profile in ("default", "root") and not (root / "profiles" / args.profile).is_dir():
        # `hermes profile list` calls the main profile "default"; it is the Hermes root itself, not a folder under profiles/.
        args.root, args.profile = True, None
        say("note: the main profile is the Hermes root itself, so this is the same as --root (%s)" % root)
    home = root if args.root else root / "profiles" / args.profile
    if not home.is_dir():
        say("ERROR: profile folder not found: %s" % home)
        if (root / "profiles").is_dir():
            say("  profiles here: " + ", ".join(sorted(p.name for p in (root / "profiles").iterdir()
                                                       if p.is_dir() and not p.name.startswith("."))))
        else:
            say("  Hermes was not found at %s - pass --hermes-root <folder>." % root)
        return 2
    clash = owner_conflict(home)
    if clash:
        say("ERROR: %s" % clash)
        say("  Run the installer as the user who runs Hermes, not as root: as root it would leave root-owned")
        say("  files in the profile that Hermes (running as you) could then not write.")
        return 2
    args.agent = absolute(args.agent_dir) if args.agent_dir else root / "hermes-agent"
    if args.python:
        args.python = str(absolute(args.python))
    cli: Optional[Hermes] = None
    cli_note, elsewhere = "--no-cli given", False
    exe = None if args.no_cli else find_hermes(root, args.hermes)
    args.hermes_started = bool(exe)         # what a dry run has to admit: asking `hermes` for its version starts it
    if not args.no_cli:
        # The root is always named ("default"): a call without --profile follows `hermes profile use`, which may be another.
        cli, cli_note, elsewhere = usable_hermes(exe, "default" if args.root else args.profile, home / "config.yaml")
    python = find_python(root, args.python) if cli is None else None
    if python is not None and elsewhere and not args.python:
        # The `hermes` that runs works on a different Hermes home: do not go round it unasked.
        python = None
        cli_note += "; pass --python to change config.yaml through Hermes' own Python anyway"
    if cli is not None and not args.uninstall:
        floor, have = required_hermes(), parse_version(cli_note)
        if floor and have is None:
            say("note: could not read the Hermes version; continuing without the version check (%r is what `hermes --version` "
                "said; the Memory Bank needs Hermes %s or newer)." % (cli_note, ".".join(str(part) for part in floor)))
        elif floor and have is not None and have < floor:
            say("ERROR: this Hermes is %s; the Memory Bank needs Hermes %s or newer. Update it (`hermes update`), then "
                "run this again. Nothing was changed." % (cli_note, ".".join(str(part) for part in floor)))
            return 2
    return (uninstall if args.uninstall else install)(args, root, home, cli, cli_note, python)


if __name__ == "__main__":
    sys.exit(main())
