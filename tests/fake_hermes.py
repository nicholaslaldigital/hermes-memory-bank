"""A stand-in for the `hermes` command, just enough for the installer tests (no Hermes needed).

It understands:  --profile NAME, --version, config path, config get KEY --json, config set KEY VALUE,
config unset KEY, plugins enable|disable NAME, plugins list --json, memory status, memorybank <command>.

State lives in <home>/fake-config.json and config.yaml is rewritten from it after every change, so no YAML
library is needed. Every call is appended to <root>/fake-calls.log as [the --profile value as given, or null,
then the arguments].

It follows the rules of the real Hermes that the installer's riskiest assumptions rest on (hermes_cli/main.py
_apply_profile_override, hermes_cli/profiles.py, hermes_cli/plugins_cmd.py _plugin_status, hermes_cli/config.py):

* a call WITHOUT --profile follows the sticky choice in <root>/active_profile (what `hermes profile use NAME`
  writes); `--profile default` always means the root itself; a profile name must be lowercase letters, digits,
  `_` and `-`, starting with a letter or digit, at most 64 characters (it is stripped and lowercased first);
* `plugins list --json` lists the plugin folders that have a plugin.yaml and reports "disabled" when the name
  is in plugins.disabled (an explicit disable wins), else "enabled" when it is in plugins.enabled or is the
  memory.provider, else "not enabled";
* in managed mode (FAKE_HERMES_FAIL=managed) `config set` and `config unset` print the refusal real Hermes
  prints and exit 0 WITHOUT writing anything.

Environment: FAKE_HERMES_ROOT (the sandbox root); FAKE_HERMES_VERSION (what --version reports, default v9.0.0-stub);
and FAKE_HERMES_FAIL, a comma list of: version, enable, disable, set, path-mismatch, import-missing, managed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULTS = {"memory.provider": "", "plugins.enabled": [], "prompt_caching.cache_ttl": "5m"}
PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def yaml_lines(value, indent=0):
    pad = "  " * indent
    lines = []
    for key, val in value.items():
        if isinstance(val, dict):
            if val:
                lines.append("%s%s:" % (pad, key))
                lines.extend(yaml_lines(val, indent + 1))
            else:
                lines.append("%s%s: {}" % (pad, key))
        elif isinstance(val, list):
            if val:
                lines.append("%s%s:" % (pad, key))
                lines.extend("%s  - %s" % (pad, item) for item in val)
            else:
                lines.append("%s%s: []" % (pad, key))
        else:
            lines.append("%s%s: %s" % (pad, key, val))
    return lines


def write_state(home: Path, cfg: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "fake-config.json").write_text(json.dumps(cfg), encoding="utf-8")
    (home / "config.yaml").write_text("\n".join(yaml_lines(cfg)) + "\n", encoding="utf-8")


def read_state(home: Path) -> dict:
    try:
        return json.loads((home / "fake-config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def nested_get(cfg, dotted):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def nested_set(cfg, dotted, value):
    parts = dotted.split(".")
    cur = cfg
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def nested_unset(cfg, dotted):
    parts = dotted.split(".")
    cur = cfg
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            return False
        cur = cur[part]
    return cur.pop(parts[-1], None) is not None


def plugin_status(cfg, name: str) -> str:
    """Hermes' own rule (plugins_cmd._plugin_status): an explicit disable wins, then the allow-list or the
    memory provider that is switched on through config, else "not enabled"."""
    plugins = cfg.get("plugins") or {}
    if name in (plugins.get("disabled") or []):
        return "disabled"
    if name in (plugins.get("enabled") or []) or name == (cfg.get("memory") or {}).get("provider"):
        return "enabled"
    return "not enabled"


def main(argv) -> int:
    root = Path(os.environ["FAKE_HERMES_ROOT"])
    fail = set(filter(None, os.environ.get("FAKE_HERMES_FAIL", "").split(",")))
    args = list(argv)
    flag = None                                   # the --profile value as given (None: no flag)
    if args[:1] == ["--profile"]:
        flag, args = args[1], args[2:]
    with (root / "fake-calls.log").open("a", encoding="utf-8") as log:
        log.write(json.dumps([flag] + args) + "\n")
    profile = None if flag is None else flag.strip().lower()
    if profile is not None and profile != "default" and not PROFILE_NAME.fullmatch(profile):
        print("Error: %r is not a valid profile name. Use lowercase letters, numbers, '-' or '_', starting with a "
              "letter or number, up to 64 characters" % flag, file=sys.stderr)
        print("Run `hermes profile list` to see your profiles.", file=sys.stderr)
        return 2
    if profile is None:                           # no flag: the sticky choice decides
        sticky = root / "active_profile"
        if sticky.exists():
            chosen = sticky.read_text(encoding="utf-8-sig").strip()
            if chosen and chosen != "default":
                profile = chosen
    if profile == "default":                      # `--profile default` is the root itself
        profile = None
    home = root / "profiles" / profile if profile else root
    cfg = read_state(home)

    if args == ["--version"]:
        if "version" in fail:
            print("boom", file=sys.stderr)
            return 1
        print("Hermes Agent " + os.environ.get("FAKE_HERMES_VERSION", "v9.0.0-stub"))
        return 0
    if args[:2] == ["config", "path"]:
        print(str(root / "elsewhere" / "config.yaml") if "path-mismatch" in fail else str(home / "config.yaml"))
        return 0
    if args[:2] == ["config", "get"] and len(args) >= 3:
        found, value = nested_get(cfg, args[2])
        if not found:
            if args[2] not in DEFAULTS:
                print("Config key not set: " + args[2], file=sys.stderr)
                return 1
            value = DEFAULTS[args[2]]
        print(json.dumps(value))
        return 0
    if args[:2] == ["config", "set"] and len(args) >= 4:
        if "managed" in fail:                     # real Hermes: managed_error(), then returns - exit 0, nothing written
            print("Cannot set configuration values: this Hermes installation is managed by NixOS.\n"
                  "Use your package manager to upgrade or reinstall Hermes.", file=sys.stderr)
            return 0
        if "set" in fail:
            print("refused: " + args[2], file=sys.stderr)
            return 1
        nested_set(cfg, args[2], args[3])
        write_state(home, cfg)
        print("Set %s = %s" % (args[2], args[3]))
        return 0
    if args[:2] == ["config", "unset"] and len(args) >= 3:
        if "managed" in fail:
            print("Cannot unset configuration values: this Hermes installation is managed by NixOS.\n"
                  "Use your package manager to upgrade or reinstall Hermes.", file=sys.stderr)
            return 0
        if not nested_unset(cfg, args[2]):
            print("Config key not set: " + args[2], file=sys.stderr)
            return 1
        write_state(home, cfg)
        print("Unset " + args[2])
        return 0
    if args[:2] in (["plugins", "enable"], ["plugins", "disable"]) and len(args) >= 3:
        name = args[2]
        if args[1] == "disable" and "disable" in fail:
            print("cannot disable", file=sys.stderr)
            return 1
        if args[1] == "enable":
            if "enable" in fail:
                print("cannot enable", file=sys.stderr)
                return 1
            if not (home / "plugins" / name / "plugin.yaml").exists():
                print("Unknown plugin: " + name, file=sys.stderr)
                return 1
        plugins = cfg.setdefault("plugins", {})
        enabled, disabled = list(plugins.get("enabled") or []), list(plugins.get("disabled") or [])
        mine, other = (enabled, disabled) if args[1] == "enable" else (disabled, enabled)
        if name not in mine:
            mine.append(name)
        if name in other:
            other.remove(name)
        plugins["enabled"], plugins["disabled"] = enabled, disabled
        write_state(home, cfg)
        print("Plugin %s %sd." % (name, args[1]))
        return 0
    if args[:2] == ["plugins", "list"]:
        names = sorted(p.name for p in (home / "plugins").glob("*") if (p / "plugin.yaml").exists())
        print(json.dumps([{"name": n, "status": plugin_status(cfg, n), "version": "1.0.0", "source": "user"}
                          for n in names]))
        return 0
    if args[:2] == ["memory", "status"]:
        print("Provider:  %s" % ((cfg.get("memory") or {}).get("provider") or "(built-in)"))
        return 0
    if args[:1] == ["memorybank"]:
        provider = (cfg.get("memory") or {}).get("provider")
        if provider != "memorybank" or "import-missing" in fail:
            print("hermes: error: argument command: invalid choice: 'memorybank'", file=sys.stderr)
            return 2
        runner = home / "plugins" / "memorybank" / "run.py"
        done = subprocess.run([sys.executable, "-I", "-B", str(runner), "--home", str(home)] + args[1:],
                              capture_output=True, text=True)
        sys.stdout.write(done.stdout)
        sys.stderr.write(done.stderr)
        return done.returncode
    print("fake hermes: unsupported: %r" % (args,), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
