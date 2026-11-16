"""Is everything this Hermes profile knows inside its Memory Bank?

Recounts from the sources of truth - the two memory files, the skills folder
and Hermes' session database - with its own small readers (deliberately not
the importer's, so a bug there cannot hide here), then looks each item up in
the bank. Read-only on both sides.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import load_config

DELIMITER = "\n§\n"
SKIP_SOURCES = ("cron", "subagent", "webhook", "msgraph_webhook")
#: Hermes keeps structured (list or dict) message content behind this prefix (hermes_state.py,
#: ``SessionDB._CONTENT_JSON_PREFIX``). The NUL ends a TEXT value early for SQLite's own substr() and length(), so
#: such a row is read as bytes here; every other row is read as before.
JSON_PREFIX = "\x00json:"
_PREFIX_BLOB = "X'" + JSON_PREFIX.encode("utf-8").hex().upper() + "'"      # the same, as an SQL blob literal
#: A row that carries the user's role but that nobody typed (hidden scaffolding, a detached sub-agent's result, a
#: model-switch note ...) has a ``display_kind``; a typed /steer row counts as typed (Hermes: agent/
#: context_compressor.py, ``split_user_originated_turn``). Older state.db files have no such column.
_HUMAN_ROWS = " AND COALESCE(display_kind, '') IN ('', 'steer')"
DEFAULT_HISTORY_LIMIT = 5000    # what the importer uses when capture.history_limit is missing or not a number
_TEXT_PART = re.compile(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)')


def _meta(raw: Any) -> Dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def norm_hash(text: str) -> str:
    """The bank's identity for a piece of text: case and whitespace do not matter."""
    flat = re.sub(r"\s+", " ", text.strip()).lower()
    return hashlib.sha1(flat.encode("utf-8", "replace")).hexdigest()[:16]


def _opening_sql(chars: int) -> str:
    """SQL for the opening of ``content``: ``chars`` characters of an ordinary message, the prefix plus ``chars`` bytes
    of a structured one (read as a blob and handed back as text, still behind its prefix). Not the importer's own
    helper, on purpose: a bug there must not be able to hide here."""
    size = len(JSON_PREFIX)
    return (f"CASE WHEN substr(CAST(content AS BLOB), 1, {size}) = {_PREFIX_BLOB}"
            f" THEN CAST(substr(CAST(content AS BLOB), 1, {chars + size}) AS TEXT)"
            f" ELSE substr(content, 1, {chars}) END")


def _ro(path: Path, *, lenient: bool = False) -> sqlite3.Connection:
    """A read-only connection. ``lenient`` is for Hermes' own file: a stray byte that is not UTF-8 then becomes U+FFFD
    instead of an error that would stop the whole check. The bank is this plugin's own file and stays strict."""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    if lenient:
        conn.text_factory = lambda raw: raw.decode("utf-8", "replace")
    return conn


def memory_entries(home: Path) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for name in ("MEMORY.md", "USER.md"):
        try:
            raw = (home / "memories" / name).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        except OSError:
            raw = ""
        out[name] = [part.strip() for part in raw.split(DELIMITER) if part.strip()]
    return out


def live_skills(home: Path) -> Dict[str, str]:
    """``{key: relative folder}`` for every SKILL.md Hermes can load. The key is
    the skill's name, or its folder when two folders use the same name."""
    root = home / "skills"
    found: Dict[str, str] = {}
    if not root.is_dir():
        return found
    named: List[tuple] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__"))
        if "SKILL.md" not in files:
            continue
        dirs[:] = []
        path = Path(base) / "SKILL.md"
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:6000]
        except OSError:
            continue
        name = path.parent.name
        if head.startswith("---"):
            end = head.find("\n---", 3)
            match = re.search(r"^name:\s*(.+?)\s*$", head[3:end if end > 0 else len(head)], re.M)
            if match:
                name = match.group(1).strip().strip("\"'") or name
        named.append((name, "/".join(path.parent.relative_to(root).parts)))
    uses: Dict[str, int] = {}
    for name, _folder in named:
        uses[name] = uses.get(name, 0) + 1
    for name, folder in named:
        found[name if uses[name] == 1 else folder] = folder
    return found


def _skill_has_text(home: Path, folder: str) -> bool:
    """Does this skill's SKILL.md say anything beyond its front matter?"""
    try:
        text = (home / "skills" / folder / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        return False
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            text = text[end + 4:]
    return len(text.strip()) > 20


def hidden_skill_count(home: Path) -> int:
    root = home / "skills"
    count = 0
    if not root.is_dir():
        return 0
    for base, _dirs, files in os.walk(root):
        rel = Path(base).relative_to(root).parts
        if "SKILL.md" in files and any(part.startswith(".") for part in rel):
            count += 1
    return count


def _has_text(content: Any) -> bool:
    """Does one stored user message carry words (not just an image or a marker)? ``content`` is text, or the bytes
    of a column that was read as a blob."""
    if isinstance(content, (bytes, bytearray, memoryview)):
        content = bytes(content).decode("utf-8", "replace")
    text = content if isinstance(content, str) else str(content or "")
    structured = text.startswith(JSON_PREFIX)
    if structured:
        text = text[len(JSON_PREFIX):]
    text = text.strip()
    if structured or (text[:1] == "[" and '"type"' in text[:300]):
        for part in _TEXT_PART.findall(text):
            try:
                part = json.loads('"' + part + '"')     # escapes are read: a lone line break is no words, a letter is
            except ValueError:
                pass                                    # a text cut inside an escape: what is left still says so
            if part.strip():
                return True
        return False
    return len(text) > 2


def eligible_sessions(home: Path) -> Dict[str, Dict[str, Any]]:
    """Sessions that hold a real conversation, newest first (the order the importer takes them in):
    ``{id: {title, user_prompts}}``. Only what a person typed counts as a user prompt."""
    path = home / "state.db"
    if not path.exists():
        return {}
    conn = _ro(path, lenient=True)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        mcols = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        live = (" AND COALESCE(active, 1) = 1" if "active" in mcols else "") + (
            " AND COALESCE(_compressed_summary, 0) = 0" if "_compressed_summary" in mcols else "") + (
            _HUMAN_ROWS if "display_kind" in mcols else "")
        hidden = "COALESCE(hidden, 0)" if "hidden" in cols else "0"
        source = "COALESCE(source, '')" if "source" in cols else "''"
        rows = conn.execute(
            f"SELECT id, COALESCE(title, '') AS title, COALESCE(message_count, 0) AS n, {hidden} AS hidden,"
            f" {source} AS source FROM sessions" + (" ORDER BY started_at DESC" if "started_at" in cols else "")
        ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if row["n"] < 2 or row["hidden"] or row["source"] in SKIP_SOURCES:
                continue
            prompts = sum(1 for (content,) in conn.execute(
                f"SELECT {_opening_sql(4000)} FROM messages WHERE session_id=? AND role='user'"
                " AND content IS NOT NULL AND (length(content) > 2"
                f" OR substr(CAST(content AS BLOB), 1, {len(JSON_PREFIX)}) = {_PREFIX_BLOB}){live} LIMIT 40",
                (row["id"],)
            ) if _has_text(content))
            out[row["id"]] = {"title": row["title"].strip(), "user_prompts": int(prompts)}
        return out
    finally:
        conn.close()


def _as_limit(value: Any) -> int:
    """A history limit the way the importer reads it: a whole number, or 5000 when it is anything else."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_HISTORY_LIMIT


def _history_limit(home: Path) -> int:
    """How many of the newest conversations the importer indexes for this profile: ``capture.history_limit``
    (0 or less means every one)."""
    try:
        return _as_limit(load_config(home).get("capture", {}).get("history_limit", DEFAULT_HISTORY_LIMIT))
    except AttributeError:      # "capture" in the settings file is not a section
        return DEFAULT_HISTORY_LIMIT


def _failed_ids(bank: sqlite3.Connection) -> List[str]:
    """The conversations the last import could not index; the bank keeps their ids in its ``sessions_failed`` note."""
    try:
        row = bank.execute("SELECT value FROM meta WHERE key='sessions_failed'").fetchone()
        loaded = json.loads(row[0]) if row else []
    except (sqlite3.Error, TypeError, ValueError):
        return []
    return [i for i in loaded if isinstance(i, str)] if isinstance(loaded, list) else []


def _open_gaps(s: Dict[str, Any]) -> bool:
    """Is something missing that an import would add, or is the bank itself damaged? (Conversations the Memory Bank
    cannot read are reported apart: an import only tries them again.)"""
    b = s["bank"]
    return bool(
        s["memory files"]["missing"] or s["memory files"]["not_marked_always_on"] or s["skills"]["missing"]
        or s["skills"]["missing_full_text"] or s["sessions"]["missing"] or b["items_missing_from_search_index"]
        or b["index_rows_without_item"] or b["links_to_nothing"] or b["sqlite_integrity"] != "ok"
    )


def check(hermes_home: Any, bank_path: Any = None, *, history_limit: Optional[int] = None) -> Dict[str, Any]:
    """Compare one profile with its bank (by default the one inside it).
    ``report["ok"]`` is True only when nothing is missing and the bank is
    internally sound.

    Sessions are counted the way the importer takes them: only the newest
    ``history_limit`` conversations (default: the profile's ``capture.history_limit``;
    0 or less means every one) are expected in the bank, and older ones that are not
    there are reported as left out on purpose, not as missing. A conversation the
    last import could not index is reported as ``unreadable`` (it is not in the
    bank, so ``ok`` is False; every import tries it again)."""
    home = Path(hermes_home)
    bank_path = Path(bank_path) if bank_path else home / "plugin-data" / "memorybank" / "bank.db"
    report: Dict[str, Any] = {"profile": home.name, "bank": str(bank_path), "ok": False, "sections": {}}
    if not bank_path.exists():
        report["error"] = "no bank at this location yet"
        return report
    bank = _ro(bank_path)
    try:
        # -- memory files ----------------------------------------------------
        missing: List[str] = []
        not_always_on: List[str] = []
        withheld: List[str] = []
        total = distinct = 0
        for name, entries in memory_entries(home).items():
            hashes = {norm_hash(e): e for e in entries}
            total += len(entries)
            distinct += len(hashes)
            for digest, text in hashes.items():
                row = bank.execute(
                    "SELECT status, in_core, meta FROM nodes WHERE kind='memory' AND content_hash=?"
                    " ORDER BY in_core DESC",
                    (digest,),
                ).fetchone()
                if row is None:
                    missing.append(f"{name}: {text[:60]}")
                elif row["in_core"] and _meta(row["meta"]).get("flagged"):
                    # In the bank, but Hermes' own scan blanks this entry in
                    # the prompt, so the bank does not recall it either.
                    withheld.append(f"{name}: {text[:60]}")
                elif not row["in_core"] or row["status"] != "active":
                    not_always_on.append(f"{name}: {text[:60]}")
        overflow = bank.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind='memory' AND source LIKE 'overflow:%' AND status != 'archived'"
        ).fetchone()[0]
        report["sections"]["memory files"] = {
            "entries_in_files": total, "distinct": distinct, "in_bank": distinct - len(missing),
            "missing": missing, "not_marked_always_on": not_always_on, "saved_beyond_the_files": int(overflow),
            "withheld_by_safety_scan": withheld,
        }

        # -- skills ----------------------------------------------------------
        skills = live_skills(home)
        rows = bank.execute("SELECT uid, status, meta, length(body) AS body FROM nodes WHERE kind='skill'").fetchall()
        in_bank = {row["uid"][2:]: row for row in rows}
        retired = sorted(
            name for name in skills
            if name in in_bank and in_bank[name]["status"] != "active" and _meta(in_bank[name]["meta"]).get("forgotten")
        )
        missing = sorted(
            name for name in skills
            if name not in retired and (name not in in_bank or in_bank[name]["status"] != "active")
        )
        stale = sorted(name for name, row in in_bank.items() if row["status"] == "active" and name not in skills)
        with_text = sum(1 for name in skills if name in in_bank and in_bank[name]["body"])
        # A skill whose SKILL.md has text but whose bank entry has none: a bank built by an older version, or an
        # import that was cut short. (A skill that is only front matter has nothing to be missing.)
        lacking = sorted(
            name for name, folder in skills.items()
            if name in in_bank and in_bank[name]["status"] == "active" and not in_bank[name]["body"]
            and _skill_has_text(home, folder)
        )
        report["sections"]["skills"] = {
            "live_on_disk": len(skills), "in_bank": len(skills) - len(missing), "with_full_text": with_text,
            "missing": missing, "missing_full_text": lacking, "in_bank_but_gone_from_disk": stale,
            "retired_in_the_viewer": retired, "archived_by_hermes_not_indexed": hidden_skill_count(home),
        }

        # -- sessions --------------------------------------------------------
        sessions = eligible_sessions(home)      # newest first, like the importer's own order
        # A conversation the bank retired because Hermes had hidden it, and that Hermes shows again, is not in the bank
        # as far as recall goes: the next import brings it back. (One the person forgot in the viewer is their choice.)
        have, retired = set(), set()
        for row in bank.execute("SELECT uid, status, meta FROM nodes WHERE kind='session'"):
            meta = _meta(row["meta"])
            if row["status"] == "archived" and meta.get("gone") and not meta.get("forgotten"):
                retired.add(row["uid"][2:])
            else:
                have.add(row["uid"][2:])
        limit = _history_limit(home) if history_limit is None else _as_limit(history_limit)
        inside = set(list(sessions)[:limit]) if limit > 0 else set(sessions)
        unreadable_ids = set(_failed_ids(bank))
        missing_ids = [sid for sid in sessions if sid not in have]
        unreadable = [sid for sid in missing_ids if sid in unreadable_ids]
        older = [sid for sid in missing_ids if sid not in unreadable_ids and sid not in inside and sid not in retired]
        due = [sid for sid in missing_ids if sid not in unreadable_ids and (sid in inside or sid in retired)]
        empty = [sid for sid in due if not sessions[sid]["title"] and not sessions[sid]["user_prompts"]]
        report["sections"]["sessions"] = {
            "conversations_in_hermes": len(sessions), "in_bank": len(sessions) - len(missing_ids),
            "missing": [sid for sid in due if sid not in empty],
            "skipped_no_title_and_no_user_text": len(empty),
            "older_than_history_limit": len(older), "history_limit": limit,
            "unreadable": unreadable,
        }

        # -- internal soundness ----------------------------------------------
        unindexed = bank.execute(
            "SELECT COUNT(*) FROM nodes WHERE id NOT IN (SELECT rowid FROM nodes_fts)").fetchone()[0]
        orphan_index = bank.execute(
            "SELECT COUNT(*) FROM nodes_fts WHERE rowid NOT IN (SELECT id FROM nodes)").fetchone()[0]
        dangling = bank.execute(
            "SELECT COUNT(*) FROM edges WHERE src NOT IN (SELECT id FROM nodes) OR dst NOT IN (SELECT id FROM nodes)"
        ).fetchone()[0]
        integrity = bank.execute("PRAGMA integrity_check").fetchone()[0]
        report["sections"]["bank"] = {
            "items": bank.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            "by_kind": dict(bank.execute("SELECT kind, COUNT(*) FROM nodes GROUP BY kind").fetchall()),
            "links": bank.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
            "search_index_rows": bank.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0],
            "items_missing_from_search_index": unindexed, "index_rows_without_item": orphan_index,
            "links_to_nothing": dangling, "sqlite_integrity": integrity,
        }
        s = report["sections"]
        report["ok"] = not _open_gaps(s) and not s["sessions"]["unreadable"]
        return report
    finally:
        bank.close()


def show(report: Dict[str, Any], out: Callable[[str], None] = print) -> None:
    out(f"Coverage of profile '{report['profile']}'")
    if report.get("error"):
        out("  " + report["error"])
        return
    s = report["sections"]
    m, k, c, b = s["memory files"], s["skills"], s["sessions"], s["bank"]
    repeats = m["entries_in_files"] - m["distinct"]
    out(f"  memory entries   {m['in_bank']}/{m['distinct']} in the bank"
        + (f"   ({repeats} repeated line(s) in the files count once)" if repeats else "")
        + (f"   + {m['saved_beyond_the_files']} saved beyond the files" if m["saved_beyond_the_files"] else "")
        + (f"   ({len(m['withheld_by_safety_scan'])} withheld: Hermes' safety scan flags them)"
           if m["withheld_by_safety_scan"] else ""))
    out(f"  skills           {k['in_bank']}/{k['live_on_disk']} in the bank, {k['with_full_text']} with their text (first 12,000 characters)"
        f"   ({k['archived_by_hermes_not_indexed']} archived by Hermes are left out on purpose)")
    unreadable = c.get("unreadable") or []
    notes = []
    if c["skipped_no_title_and_no_user_text"]:
        notes.append(f"{c['skipped_no_title_and_no_user_text']} have no title and no user text")
    if c.get("older_than_history_limit"):
        notes.append(f"{c['older_than_history_limit']} older than history_limit, not indexed on purpose; "
                     f"history_limit is {c.get('history_limit')}; import --limit 0 adds them")
    if unreadable:
        notes.append(f"{len(unreadable)} unreadable, see the log")
    out(f"  sessions         {c['in_bank']}/{c['conversations_in_hermes']} in the bank"
        + (f"   ({'; '.join(notes)})" if notes else ""))
    out(f"  bank             {b['items']} items, {b['links']} links, search index {b['search_index_rows']} rows, "
        f"integrity {b['sqlite_integrity']}")
    for label, items in (
        ("MISSING memory entries", m["missing"]), ("memory entries not marked always-on", m["not_marked_always_on"]),
        ("MISSING skills", k["missing"]), ("skills without their text", k["missing_full_text"]),
        ("MISSING sessions", c["missing"]),
        ("sessions the Memory Bank could not read (unreadable, see the log)", unreadable),
        ("skills in the bank that left the disk", k["in_bank_but_gone_from_disk"]),
    ):
        if items:
            out(f"  ! {label}: {len(items)}")
            for item in items[:10]:
                out(f"      - {item}")
    for label, key in (("items missing from the search index", "items_missing_from_search_index"),
                       ("index rows without an item", "index_rows_without_item"),
                       ("links to nothing", "links_to_nothing")):
        if b[key]:
            out(f"  ! {label}: {b[key]}")
    if report["ok"]:
        verdict = "everything is in the bank"
    elif not _open_gaps(s):
        verdict = ("GAPS FOUND - only conversations the Memory Bank cannot read (see the log); "
                   "every import tries them again")
    else:
        # The profile's own history limit stays in force: only what lies inside it was flagged.
        verdict = "GAPS FOUND - run: import --force" + (
            "   (a session that stays missing after that could not be indexed: see the log)"
            if c["missing"] or unreadable else "")
    out("  RESULT: " + verdict)
