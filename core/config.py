"""Memory Bank settings: defaults merged with ``<hermes home>/memorybank.json``."""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Set, Tuple

logger = logging.getLogger(__name__)

CONFIG_NAME = "memorybank.json"
DATA_SUBDIR = ("plugin-data", "memorybank")
DB_NAME = "bank.db"

#: Largest number a "how many" setting or option is allowed to be. SQLite binds 64-bit integers: a value beyond that
#: ends in an OverflowError instead of meaning "everything", and nothing here has a billion of anything.
LIMIT_CAP = 10 ** 9

DEFAULTS: Dict[str, Any] = {
    "recall": {
        # Automatic recall before each turn. Everything injected is replayed on
        # every later request of the session, so the budget is deliberately small.
        "enabled": True,
        "max_chars": 1100,
        "max_items": 6,
        "max_sessions": 2,
        "max_skills": 1,
        "min_coverage": 0.34,
        "min_mass": 0.5,
        "anchor_rarity": 0.62,
        "kinds": ["memory", "session", "skill"],
        "cron": False,
        # A conversation held in one messaging chat (Telegram, Slack...) is
        # not surfaced in another chat unless this is on. The desktop app and
        # the CLI always see everything.
        "share_across_chats": False,
        # Always-on room beyond MEMORY.md / USER.md: what overflowed from them
        # (and standing preferences) is shown at the start of every session
        # (profile and preference items first, then the newest), up to this
        # many characters. 0 turns it off.
        "standing_chars": 1200,
    },
    "capture": {
        # What grows the bank without any model call.
        "core_mirror": True,
        # A save refused because MEMORY.md / USER.md is full goes to the bank
        # instead, so saving never fails for lack of room.
        "overflow": True,
        "sessions": True,
        "entities": True,
        "skills": True,
        "import_history_on_first_run": True,
        "history_limit": 5000,  # most recent conversations indexed (0 = all)
        "skills_refresh_hours": 6,
    },
    "ui": {"port": 8765, "graph_limit": 4000},
}


def data_dir(hermes_home: Any) -> Path:
    return Path(hermes_home).joinpath(*DATA_SUBDIR)


def db_path(hermes_home: Any) -> Path:
    return data_dir(hermes_home) / DB_NAME


def config_path(hermes_home: Any) -> Path:
    return Path(hermes_home) / CONFIG_NAME


_IGNORE = object()
_WARNED: Set[Tuple[str, str]] = set()


def _ignored(name: str, value: Any, default: Any) -> None:
    """Say - once per setting and kind of mistake - that a setting of the wrong kind is not used. Ignoring it quietly
    could leave something switched on that was meant to be off: ``"capture": false`` is a natural way to write "no
    capture", and what the section takes is a group of settings such as ``{"sessions": false}``."""
    token = (name, type(value).__name__)
    if token in _WARNED:
        return
    _WARNED.add(token)
    wanted = "group of settings" if isinstance(default, dict) else "list" if isinstance(default, list) else "yes/no setting" if isinstance(default, bool) else "number"
    shown = repr(value) if len(repr(value)) <= 30 else repr(value)[:27] + "..."
    kept = "" if isinstance(default, (dict, list)) else " (%r)" % (default,)
    logger.warning("%s: %r is %s, not a %s; it is ignored and the built-in setting%s is used",
                   CONFIG_NAME, name, shown, wanted, kept)


def _fit(default: Any, value: Any) -> Any:
    """``value`` as the kind of setting ``default`` is, or ``_IGNORE`` when it cannot be one. Settings are read by
    code that does arithmetic and SQL with them: a section that is not a section, a number that is a word or too big
    for SQLite would stop a command with a traceback, so the default stays instead."""
    if isinstance(default, dict):
        if value is False:
            # "capture": false can only mean "none of it": switch every on/off setting of the section off (failing
            # open - leaving capture running - would do the opposite of what was written)
            return {k: False for k, v in default.items() if isinstance(v, bool)}
        return value if isinstance(value, dict) else _IGNORE
    if isinstance(default, list):
        return value if isinstance(value, list) else _IGNORE
    if isinstance(default, bool):
        if isinstance(value, str):        # "false" is truthy to Python: read the words people write for off and on
            word = value.strip().lower()
            if word in ("false", "no", "off", "0"):
                return False
            if word in ("true", "yes", "on", "1"):
                return True
            return _IGNORE
        return value                      # read for its truth, which any value has
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) == float("inf"):
            return _IGNORE
        return max(-LIMIT_CAP, min(int(value) if isinstance(default, int) else value, LIMIT_CAP))
    return value


def _merge(base: Dict[str, Any], override: Dict[str, Any], *, typed: bool = False, where: str = "") -> Dict[str, Any]:
    for key, value in override.items():
        if typed and key in base:
            fitted = _fit(base[key], value)
            if fitted is _IGNORE:
                _ignored(where + str(key), value, base[key])
                continue
            value = fitted
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value, typed=typed, where=where + str(key) + ".")
        else:
            base[key] = value
    return base


def load_config(hermes_home: Any) -> Dict[str, Any]:
    """Defaults overlaid with the user's file. A broken file falls back to defaults, and so does any single setting
    of the wrong kind (a section that is ``null``, a limit that is a word); numbers are cut to ``LIMIT_CAP``."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        raw = config_path(hermes_home).read_text(encoding="utf-8-sig")
        user = json.loads(raw)
        if isinstance(user, dict):
            _merge(cfg, user, typed=True)
    except (OSError, ValueError):
        pass
    return cfg


def save_config(hermes_home: Any, values: Dict[str, Any]) -> Path:
    """Merge ``values`` into the user's file (atomic replace)."""
    path = config_path(hermes_home)
    current: Dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(loaded, dict):
            current = loaded
    except (OSError, ValueError):
        pass
    _merge(current, values)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".memorybank-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(current, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path
