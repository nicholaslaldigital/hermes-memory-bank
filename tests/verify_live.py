"""Will Hermes use the Memory Bank by itself in this profile?

Runs Hermes' own code against a real profile home, the way a new session does
after a restart: read the profile's config, discover and load the configured
memory provider, initialise it through MemoryManager, and confirm the tool,
the system note, the automatic hooks, the CLI and the app backend all see it.
It talks to no model and starts no server. Starting the provider does what a
real session start does, inside the profile's bank only: creates it if it does
not exist, mirrors the memory files, refreshes the skill index, runs the
first-run history import (once) or the daily refresh when it is due, and keeps
the "standing notes" snapshot of its own throwaway session id. Nothing is
saved, recalled or logged as a memory; MEMORY.md, USER.md, config.yaml and
state.db are only read.

    <hermes venv python> tests/verify_live.py <profile-home> [<hermes-agent dir>]
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(2)

HOME = Path(sys.argv[1]).resolve()
_BASE = (Path(os.environ.get("LOCALAPPDATA", "")) / "hermes") if os.name == "nt" else (Path.home() / ".hermes")
AGENT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(os.environ.get("HERMES_AGENT_DIR") or (_BASE / "hermes-agent"))
if not (AGENT / "hermes_cli").is_dir():
    print("Hermes source not found at %s - pass its folder as the second argument or set HERMES_AGENT_DIR." % AGENT)
    sys.exit(2)
NAME = "memorybank"

sys.argv[0] = str(Path(__file__).resolve())    # Hermes' own start-up code re-reads argv[0]: keep it absolute
os.environ["HERMES_HOME"] = str(HOME)          # before any Hermes import
os.environ.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)
REPO = AGENT
sys.path.insert(0, str(AGENT))
try:
    import hermes_bootstrap  # noqa: F401  (activates Hermes' dependency set, as its own entry points do)
except ImportError:
    pass
os.chdir(str(AGENT))                            # a neutral folder: nothing in the profile can be imported by accident

RESULTS = []
STATE = {}


def check(label, fn):
    try:
        detail = fn()
        RESULTS.append(True)
        print(f"PASS  {label}" + (f"  - {detail}" if detail else ""))
    except Exception as exc:
        RESULTS.append(False)
        print(f"FAIL  {label}  - {exc}")
        if os.environ.get("MB_VERBOSE"):
            traceback.print_exc()


def c_home():
    from hermes_constants import get_hermes_home
    assert Path(get_hermes_home()).resolve() == HOME, get_hermes_home()
    return "Hermes resolves this profile's home"


def c_config():
    from hermes_cli.config import cfg_get, load_config_readonly
    cfg = load_config_readonly()
    provider = cfg_get(cfg, "memory", "provider")
    enabled = cfg_get(cfg, "plugins", "enabled", default=[]) or []
    disabled = cfg_get(cfg, "plugins", "disabled", default=[]) or []
    assert provider == NAME, f"memory.provider is {provider!r}"
    assert NAME in enabled, f"{NAME} is not in plugins.enabled: {enabled}"
    assert NAME not in disabled, "it is listed under plugins.disabled"
    flags = (cfg_get(cfg, "memory", "memory_enabled", default=True), cfg_get(cfg, "memory", "user_profile_enabled", default=True))
    return f"memory.provider = {provider}; plugins.enabled = {enabled}; built-in memory files enabled = {flags}"


def c_discovery():
    from agent.memory_provider import MemoryProvider, is_core_memory_provider
    from plugins.memory import find_provider_dir, list_memory_provider_names, load_memory_provider
    assert not is_core_memory_provider(NAME)
    assert NAME in list_memory_provider_names()
    folder = Path(find_provider_dir(NAME)).resolve()
    assert folder == (HOME / "plugins" / NAME).resolve(), folder
    provider = load_memory_provider(NAME)
    assert isinstance(provider, MemoryProvider) and provider.name == NAME
    assert provider.is_available(), provider.unavailable_reason()
    module_name = type(provider).__module__
    STATE.update(provider=provider, module=sys.modules[module_name], core=sys.modules.get(module_name + ".core"))
    version = getattr(STATE["core"], "__version__", "?")
    return f"found in this profile's plugins folder, loaded by Hermes' own loader, available (version {version})"


def c_session_start():
    """What agent_init._init_memory does for every new session."""
    from agent.memory_manager import MemoryManager
    manager = MemoryManager()
    manager.add_provider(STATE["provider"])
    assert manager.providers, "the manager rejected the provider"
    started = time.perf_counter()
    manager.initialize_all(session_id=f"memorybank-verify-{int(time.time())}", platform="cli")
    init_ms = (time.perf_counter() - started) * 1000
    assert manager.has_tool("memory_bank"), manager.get_all_tool_names()
    assert [s["name"] for s in manager.get_all_tool_schemas()] == ["memory_bank"], "the model would not be offered the tool"
    block = manager.build_system_prompt()
    assert "Memory Bank" in block and "never fills up" in block
    in_view = block.split("Saved beyond", 1)[1].count("\n- #") if "Saved beyond" in block else 0
    STATE["manager"] = manager
    return (f"provider initialised in {init_ms:.0f} ms; tool memory_bank offered to the model; system note "
            f"{len(block)} chars ({in_view} saved-beyond-the-files entries in view)")


def c_tool():
    manager = STATE["manager"]
    status = json.loads(manager.handle_tool_call("memory_bank", {"action": "status"}))
    assert status.get("success"), status
    STATE["status"] = status
    return (f"memories {status['memories']} ({status['always_on']} always-on, {status['saved_beyond_files']} saved beyond "
            f"the files), skills {status['skills']}, sessions {status['sessions']}, entities {status['entities']}, "
            f"links {status['links']}")


def c_everything_is_in_it():
    """The independent recount, through the installed copy of the plugin."""
    core = STATE["core"]
    assert core is not None, "the plugin's engine package is not loaded"
    report = core.coverage.check(HOME)
    sections = report.get("sections", {})
    assert report.get("ok"), {k: v for k, v in sections.items() if k != "bank"} or report.get("error")
    m, k, c = sections["memory files"], sections["skills"], sections["sessions"]
    return (f"memory entries {m['in_bank']}/{m['distinct']}, skills {k['in_bank']}/{k['live_on_disk']} "
            f"({k['with_full_text']} with their text), sessions {c['in_bank']}/{c['conversations_in_hermes']}, "
            f"index and links sound")


def c_recall_path():
    manager = STATE["manager"]
    started = time.perf_counter()
    quiet = manager.prefetch_all("zzqxv kwyzj nothing-in-the-bank-matches-this", session_id="memorybank-verify")
    elapsed = (time.perf_counter() - started) * 1000
    assert quiet == "", quiet
    assert manager.describe_recall() == ""
    started = time.perf_counter()
    manager.on_turn_start(1, "zzqxv kwyzj")
    turn_ms = (time.perf_counter() - started) * 1000
    return (f"recall runs on a turn and stays silent when nothing matches ({elapsed:.0f} ms including thread "
            f"hand-off); turn-start hook {turn_ms:.1f} ms")


def c_hooks():
    from hermes_cli.plugins import has_hook
    wanted = ["transform_tool_result", "on_skill_lifecycle"]
    have = [h for h in wanted if has_hook(h)]
    assert have == wanted, f"registered {have}, expected {wanted}"
    # The hooks only act for a profile whose memory provider is the bank: is this one?
    active = STATE["module"]._active_home()
    assert active is not None and Path(active).resolve() == HOME, f"the hooks would not act for this profile ({active})"
    return "registered and active for this profile: " + ", ".join(have)


def c_never_full():
    """How full the built-in files are - read only; nothing is added."""
    from tools.memory_tool import MemoryStore
    store = MemoryStore()
    store.load_from_disk()
    parts = []
    for target, label in (("memory", "MEMORY.md"), ("user", "USER.md")):
        used, limit = store._char_count(target), store._char_limit(target)
        parts.append(f"{label} {used:,}/{limit:,} chars ({limit - used} free)")
    cfg = STATE["core"].load_config(HOME)
    assert cfg["capture"]["overflow"] is True, "capture.overflow is switched off in memorybank.json"
    gate = "on" if __import__("tools.write_approval", fromlist=["x"]).write_approval_enabled("memory") else "off"
    return "; ".join(parts) + f". A save that does not fit goes to the bank (capture.overflow on; memory.write_approval {gate})"


def c_cli():
    from plugins.memory import discover_plugin_cli_commands
    names = [c["name"] for c in discover_plugin_cli_commands()]
    assert NAME in names, names
    return "`hermes memorybank ...` commands are wired"


def c_backend():
    from hermes_cli.plugins_cmd import _get_disabled_set, _get_enabled_set
    from hermes_cli.web_server_dashboard import _discover_dashboard_plugins, _plugin_api_mount_skip_reason
    mine = [p for p in _discover_dashboard_plugins() if p.get("name") == NAME]
    assert mine, "the app backend does not list the plugin"
    plugin = mine[0]
    reason = _plugin_api_mount_skip_reason(plugin, _get_enabled_set(), _get_disabled_set())
    assert reason is None, f"backend would skip its API: {reason}"
    assert plugin.get("_api_file"), plugin
    return f"backend will mount /api/plugins/{NAME}/ from {plugin['_api_file']} (source: {plugin.get('source')})"


def c_desktop_half():
    path = HOME / "plugins" / NAME / "desktop" / "plugin.js"
    assert path.exists(), path
    return f"desktop page present ({path.stat().st_size // 1024} KB); the app lists it under Capabilities > Plugins at next start"


def c_shutdown():
    manager = STATE["manager"]
    manager.on_session_end([])
    manager.shutdown_all()
    return "clean shutdown"


print(f"Hermes auto-use check for {HOME}")
for label, fn in [
    ("profile home", c_home),
    ("config selects the Memory Bank", c_config),
    ("Hermes discovers and loads it", c_discovery),
    ("a new session starts with it", c_session_start),
    ("the memory_bank tool answers", c_tool),
    ("everything the profile knows is in it", c_everything_is_in_it),
    ("recall is called and gated", c_recall_path),
    ("automatic capture hooks", c_hooks),
    ("memory never fills up", c_never_full),
    ("CLI commands", c_cli),
    ("app backend (desktop + dashboard)", c_backend),
    ("desktop page", c_desktop_half),
    ("shutdown", c_shutdown),
]:
    check(label, fn)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} passed")
sys.exit(0 if passed == len(RESULTS) else 1)
