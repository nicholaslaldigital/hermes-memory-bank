"""Rehearse "memory never fills up" on a COPY of a real profile's memory files.

    <hermes venv python> tests/rehearse_overflow.py <profile-home> [<hermes-agent dir>]

Only MEMORY.md and USER.md are copied (into a throwaway HERMES_HOME that is
deleted afterwards); the profile itself is never written. Hermes' own memory
tool is driven at the real files' size - they are full - through Hermes' own
tool-result hook pipeline, and a brand-new session is started to see what the
model would be shown. Only sizes and the synthetic facts used here are printed,
never the contents of the profile's memory files.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(2)

HERE = Path(__file__).resolve().parent
PLUGIN_SRC = Path(os.environ.get("MEMORYBANK_PLUGIN_DIR") or HERE.parent).resolve()
PROFILE = Path(sys.argv[1]).resolve()
_BASE = (Path(os.environ.get("LOCALAPPDATA", "")) / "hermes") if os.name == "nt" else (Path.home() / ".hermes")
AGENT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(os.environ.get("HERMES_AGENT_DIR") or (_BASE / "hermes-agent"))
if not (AGENT / "hermes_cli").is_dir():
    print("Hermes source not found at %s - pass its folder as the second argument or set HERMES_AGENT_DIR." % AGENT)
    sys.exit(2)
REPO = AGENT

# Hermes' own entry points activate its dependency set (and, from another Python,
# hand the run over to the interpreter Hermes really uses) by importing this first.
sys.path.insert(0, str(REPO))
try:
    import hermes_bootstrap  # noqa: F401
except ImportError:
    pass

TMP = Path(tempfile.mkdtemp(prefix="mb-rehearse-"))
HOME = TMP / "home"
(HOME / "memories").mkdir(parents=True)
for name in ("MEMORY.md", "USER.md"):
    shutil.copy2(PROFILE / "memories" / name, HOME / "memories" / name)
shutil.copytree(PLUGIN_SRC, HOME / "plugins" / "memorybank", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
(HOME / "config.yaml").write_text(
    "memory:\n  provider: memorybank\nplugins:\n  enabled:\n    - memorybank\n  disabled: []\n", encoding="utf-8")
os.environ["HERMES_HOME"] = str(HOME)
os.environ["HERMES_GATEWAY_LOCK_DIR"] = str(TMP / "locks")
os.environ.pop("HERMES_ENABLE_PROJECT_PLUGINS", None)
sys.path.insert(0, str(AGENT))
os.chdir(TMP)

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


def bank():
    import sqlite3
    conn = sqlite3.connect(HOME / "plugin-data" / "memorybank" / "bank.db")
    conn.row_factory = sqlite3.Row
    return conn


def one(sql, params=()):
    conn = bank()
    try:
        return conn.execute(sql, params).fetchone()
    finally:
        conn.close()


def wait_for(probe, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            value = probe()
            if value:
                return value
        except Exception:
            pass
        time.sleep(0.05)
    return probe()


FACT_MEMORY = "The Pelto office moved its ISP to Brightwave on 3 March; the old Zeron circuit ends 30 April."
FACT_USER = "Prefers change windows on Tuesday evenings after 19:00 Berlin time, never on Fridays."


def c_files_are_full():
    from tools.memory_tool import MemoryStore
    store = MemoryStore()
    store.load_from_disk()
    STATE["store"] = store
    STATE["before"] = {n: (HOME / "memories" / n).read_bytes() for n in ("MEMORY.md", "USER.md")}
    parts = []
    for target, label in (("memory", "MEMORY.md"), ("user", "USER.md")):
        used, limit = store._char_count(target), store._char_limit(target)
        parts.append(f"{label} {used:,}/{limit:,} chars ({limit - used} free)")
    return "copy of the profile's files: " + "; ".join(parts)


def c_hermes_refuses_without_the_bank():
    from tools.memory_tool import memory_tool
    raw = memory_tool(action="add", target="memory", content=FACT_MEMORY, store=STATE["store"])
    result = json.loads(raw)
    assert result["success"] is False and "limit" in result["error"], result
    STATE["refusal"] = raw
    return "Hermes' own memory tool says no: " + result["error"][:70].replace("\n", " ") + "..."


def c_the_hook_rescues_it():
    from model_tools import _CallIds, _apply_transform_tool_result_hook
    from agent.memory_manager import MemoryManager
    from plugins.memory import load_memory_provider
    manager = MemoryManager()
    manager.add_provider(load_memory_provider("memorybank"))
    manager.initialize_all(session_id="rehearsal-1", platform="cli")
    STATE["manager"] = manager
    assert wait_for(lambda: one("SELECT value FROM meta WHERE key='core_stamp'"), 15), "mirror did not run"
    ids = _CallIds(session_id="rehearsal-1", tool_call_id="call-1")
    args = {"action": "add", "target": "memory", "content": FACT_MEMORY}
    answer = json.loads(_apply_transform_tool_result_hook("memory", args, STATE["refusal"], 3, ids))
    assert answer["success"] is True and answer["stored_in"] == "memory_bank", answer
    from tools.memory_tool import memory_tool
    # USER.md may have a few free characters: make the second fact long enough not to fit.
    free = STATE["store"]._char_limit("user") - STATE["store"]._char_count("user")
    fact = FACT_USER if len(FACT_USER) > free else FACT_USER + " " + "Keep it that way. " * (free // 18 + 1)
    raw = memory_tool(action="add", target="user", content=fact, store=STATE["store"])
    assert json.loads(raw)["success"] is False, "the second fact fitted; the file is not full"
    answer2 = json.loads(_apply_transform_tool_result_hook(
        "memory", {"action": "add", "target": "user", "content": fact}, raw, 3, ids))
    assert answer2["success"] is True and answer2["target"] == "user", answer2
    STATE["user_fact"] = fact
    return f"both saves answered 'saved' ({len(answer['ids']) + len(answer2['ids'])} kept in the bank)"


def c_files_untouched():
    now = {n: (HOME / "memories" / n).read_bytes() for n in ("MEMORY.md", "USER.md")}
    assert now == STATE["before"], "MEMORY.md / USER.md changed"
    return "MEMORY.md and USER.md are byte-for-byte what they were"


def c_next_session_sees_it():
    from agent.memory_manager import MemoryManager
    from plugins.memory import load_memory_provider
    fresh = MemoryManager()
    fresh.add_provider(load_memory_provider("memorybank"))
    fresh.initialize_all(session_id="rehearsal-2", platform="cli")
    block = fresh.build_system_prompt()
    assert "Saved beyond MEMORY.md / USER.md" in block, block
    assert FACT_MEMORY in block and STATE["user_fact"] in block, block
    status = json.loads(fresh.handle_tool_call("memory_bank", {"action": "status"}))
    assert status["saved_beyond_files"] == 2, status
    found = json.loads(fresh.handle_tool_call("memory_bank", {"action": "search", "query": "Pelto ISP Brightwave"}))
    assert found["count"] >= 1 and "Brightwave" in found["results"][0]["text"], found
    STATE["block_chars"] = len(block)
    fresh.shutdown_all()
    return f"a new session's system prompt carries both (note + entries {len(block)} chars); search finds them"


def c_everything_is_mirrored():
    module = next(m for n, m in sys.modules.items() if n.endswith(".core") and hasattr(m, "coverage") and hasattr(m, "ingest"))
    files = module.coverage.check(HOME)["sections"]["memory files"]
    assert not files["missing"] and not files["not_marked_always_on"], files
    return (f"all {files['distinct']} entries of the two files are in the bank, always-on; "
            f"{files['saved_beyond_the_files']} more saved beyond them")


def c_shutdown():
    STATE["manager"].shutdown_all()
    return "clean"


print(f"Overflow rehearsal on a copy of {PROFILE.name}")
for label, fn in [
    ("the copied files are full", c_files_are_full),
    ("Hermes' memory tool refuses a new entry", c_hermes_refuses_without_the_bank),
    ("with the bank, the same save is accepted", c_the_hook_rescues_it),
    ("the built-in files are not touched", c_files_untouched),
    ("the next session shows it from its first message", c_next_session_sees_it),
    ("every existing entry is in the bank too", c_everything_is_mirrored),
    ("shutdown", c_shutdown),
]:
    check(label, fn)

passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} passed")
os.chdir(HERE)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(0 if passed == len(RESULTS) else 1)
