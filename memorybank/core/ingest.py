"""Feeding the bank: MEMORY.md / USER.md, skills, session history, live turns.

Pure stdlib and idempotent: running any importer twice changes nothing the
second time, so they are safe to call from several processes.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .entities import extract_entities
from .extract import (
    clean_prompt, clip, content_hash, content_terms, is_noise_prompt, jaccard, make_title, norm_text,
    parse_frontmatter, split_camel, split_core_entries, strip_refs, term_set, token_bag, trips,
)
from .store import Store

logger = logging.getLogger(__name__)

#: Optional content scanner: returns something truthy when text looks like a
#: prompt-injection or exfiltration attempt (Hermes' own memory scanner).
Scan = Optional[Callable[[str], Any]]

CORE_FILES = (("memory", "MEMORY.md"), ("user", "USER.md"))
MEMORY_KINDS = ("fact", "preference", "decision", "lesson", "procedure", "person", "project", "reference")
MAX_PROMPTS = 6                  # prompts of a session kept for display and recall lines
MAX_MEMORY_CHARS = 8000          # one memory; longer text is refused, never cut
SKILL_BODY_CHARS = 12000         # searchable text kept per skill
SESSION_BODY_PROMPTS = 60        # later prompts of a session kept as searchable text
SESSION_BODY_PROMPT_CHARS = 240
SESSION_BODY_CHARS = 9000
#: Sessions nobody typed into - scheduled jobs, delegated sub-tasks, machine
#: deliveries - are neither captured nor imported.
UNATTENDED_SOURCES = ("cron", "subagent", "webhook", "msgraph_webhook")
#: Why the bank will not edit an entry that lives in the built-in files.
CORE_OWNED = (
    "that entry is in MEMORY.md / USER.md, which the bank only mirrors: change it with the memory tool "
    "(replace or remove) and the bank follows"
)
_IMPORTANCE = {1: 0.3, 2: 0.45, 3: 0.6, 4: 0.8, 5: 0.95}
_SKILL_NAME = re.compile(r"[\w.\-]{1,60}")
# A text part of a part list; the closing quote is optional (group 2 is empty when the message was cut inside it).
_JSON_TEXT_RE = re.compile(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)("?)')
_CUT_ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{0,3}\Z")      # a \uXXXX escape that the cut left unfinished
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")

#: How Hermes stores structured (list or dict) message content: this prefix, then ``json.dumps`` of the parts
#: (hermes_state.py, ``SessionDB._CONTENT_JSON_PREFIX``). SQLite's own ``substr()`` and ``length()`` end a TEXT value at
#: a NUL, so such a message looks empty to them: those rows are read as bytes (a blob has no end but its length) and
#: decoded here; every other row is read as before.
CONTENT_JSON_PREFIX = "\x00json:"
_PREFIX_BLOB = "X'" + CONTENT_JSON_PREFIX.encode("utf-8").hex().upper() + "'"    # the same, as an SQL blob literal
#: Only what a person typed is a prompt. Hermes gives the other rows that carry the user's role - hidden scaffolding
#: such as a seeded setup runbook, a detached sub-agent's result, a model-switch note, a process notice - a
#: ``display_kind``; a /steer row is typed, so it counts (agent/context_compressor.py: ``split_user_originated_turn``).
#: The same test keeps notices that Hermes itself wrote out of the assistant's replies. The column is missing from
#: older state.db files, where every row counts.
_HUMAN_ROWS = " AND COALESCE(display_kind, '') IN ('', 'steer')"
#: Conversations that could not be indexed, remembered so that the next import tries them again and ``check`` can say so.
FAILED_KEEP = 50

_SKILL_TOOLS = {"skill_view", "skills_view", "skill"}


def importance_from_level(level: Any, default: float = 0.55) -> float:
    try:
        return _IMPORTANCE.get(int(level), default)
    except (TypeError, ValueError):
        return default


def _new_memory_uid(store: Store, digest: str) -> str:
    uid = "m:" + digest
    existing = store.get_by_uid(uid)
    if existing is None or existing["content_hash"] == digest:
        return uid
    return f"{uid}-{int(time.time() * 1000) % 0xFFFFFFFF:x}"


def link_entities(
    store: Store,
    node_id: int,
    text: str,
    *,
    explicit: Sequence[str] = (),
    rel: str = "mentions",
    max_entities: int = 12,
    created_at: Optional[float] = None,
) -> List[str]:
    """Connect a node to the things it talks about; returns the names linked."""
    candidates: List[Tuple[str, str]] = []
    given = [n.strip() for n in explicit if isinstance(n, str) and n.strip()]
    candidates.extend((name, "") for name in given[:max_entities])
    candidates.extend(extract_entities(text, store.known_entity_names(), max_entities=max_entities))
    names: List[str] = []
    seen: Set[int] = set()
    for name, subtype in candidates:
        entity_id = store.ensure_entity(name, subtype, created_at=created_at)
        if entity_id is None or entity_id in seen or entity_id == node_id:
            continue
        seen.add(entity_id)
        store.link(node_id, entity_id, rel, 1.0)
        names.append(name)
    if given:
        # Names the caller supplied may not appear in the text: index them too.
        store.add_tags(node_id, given)
    return names


def similar_memories(
    store: Store, text: str, *, limit: int = 5, exclude_id: Optional[int] = None
) -> List[Tuple[float, Dict[str, Any]]]:
    """``[(similarity, node), ...]`` for memories that say much the same thing."""
    terms = content_terms(text, max_terms=16)
    if not terms:
        return []
    ranked = store.fts_search(terms, ["memory"], limit=12, include_core=True)
    nodes = store.get_many([i for i, _ in ranked])
    tokens = term_set(text)
    out: List[Tuple[float, Dict[str, Any]]] = []
    for node_id, _ in ranked:
        node = nodes.get(node_id)
        if node is None or node_id == exclude_id:
            continue
        sim = jaccard(tokens, term_set(node["content"]))
        if sim > 0:
            out.append((sim, node))
    out.sort(key=lambda pair: -pair[0])
    return out[:limit]


def remember(
    store: Store,
    content: str,
    *,
    subtype: str = "fact",
    entities: Sequence[str] = (),
    importance: Optional[float] = None,
    confidence: float = 0.85,
    source: str = "tool",
    session_id: str = "",
    created_at: Optional[float] = None,
    in_core: bool = False,
    meta: Optional[Dict[str, Any]] = None,
    dedupe: bool = True,
    status: Optional[str] = None,
    min_chars: int = 3,
) -> Dict[str, Any]:
    """Store one memory. Saying the same thing twice reinforces the first copy.

    The text is kept as written (line breaks included); only its identity
    ignores case, whitespace, punctuation and word order. Nothing is ever cut
    to fit, and a text that is merely *close* to an existing memory (one
    number, name or version apart - usually a correction) is stored next to
    it, never folded into it: the result then names the older one in
    ``similar`` so the caller can retire it.
    """
    text = (content or "").replace("\r\n", "\n").strip()
    if len(text) < max(1, min_chars):
        return {"error": "content is empty"}
    if len(text) > MAX_MEMORY_CHARS:
        return {"error": f"too long ({len(text)} characters): split it into separate memories of at most "
                         f"{MAX_MEMORY_CHARS} characters each"}
    digest = content_hash(text)
    with store.transaction():
        node = store.find_by_hash("memory", digest)
        similar: Optional[Dict[str, Any]] = None
        if node is None and dedupe and not in_core:
            near = similar_memories(store, text, limit=1)
            if near and near[0][0] >= 0.8 and near[0][1]["status"] in ("active", "evicted"):
                if token_bag(near[0][1]["content"]) == token_bag(text):
                    node = near[0][1]
                else:
                    similar = near[0][1]
        if node is not None:
            fields: Dict[str, Any] = {}
            if node["status"] != "active":
                # Saying a retired fact again brings it back.
                fields["status"] = "active"
                meta = {**(meta or {}), "forgotten": False}
            if in_core and not node["in_core"]:
                fields["in_core"] = True
            boosted = max(float(node["importance"]), float(importance or 0.0))
            if not in_core:
                boosted = min(1.0, boosted + 0.05)
            if boosted > float(node["importance"]) + 1e-9:
                fields["importance"] = boosted
            store.upsert_node(node["uid"], "memory", meta=meta, **fields)
            names = link_entities(store, node["id"], text, explicit=entities) if entities else []
            return {"id": node["id"], "created": False, "merged": True, "text": node["content"], "entities": names}

        node_id, _ = store.upsert_node(
            _new_memory_uid(store, digest), "memory",
            subtype=subtype or "fact", title=make_title(text), content=text,
            importance=0.55 if importance is None else importance, confidence=confidence,
            source=source, session_id=session_id, in_core=in_core, created_at=created_at, meta=meta,
            status=status,
        )
        names = link_entities(store, node_id, text, explicit=entities, created_at=created_at)
        for sim, other in similar_memories(store, text, limit=3, exclude_id=node_id):
            if sim >= 0.25:
                store.link(node_id, other["id"], "related", round(sim, 3))
        if session_id:
            session = store.get_by_uid("s:" + session_id)
            if session is not None:
                store.link(node_id, session["id"], "from", 1.0)
        store.add_event("remember", [node_id], session_id, subtype or "fact")
    saved: Dict[str, Any] = {"id": node_id, "created": True, "merged": False, "entities": names}
    if similar is not None:
        saved["similar"] = {"id": similar["id"], "text": similar["content"]}
    return saved


def update_memory(store: Store, node_id: int, content: str, *, session_id: str = "") -> Dict[str, Any]:
    node = store.get(node_id)
    if node is None or node["kind"] != "memory":
        return {"error": f"no memory with id {node_id}"}
    if node["in_core"]:
        return {"error": CORE_OWNED}
    text = (content or "").replace("\r\n", "\n").strip()
    if len(text) < 3:
        return {"error": "content is empty"}
    if len(text) > MAX_MEMORY_CHARS:
        return {"error": f"too long ({len(text)} characters): at most {MAX_MEMORY_CHARS}"}
    with store.transaction():
        store.upsert_node(node["uid"], "memory", content=text, title=make_title(text), status="active")
        link_entities(store, node_id, text)
        store.add_event("update", [node_id], session_id)
    return {"id": node_id, "updated": True}


def forget(store: Store, node_id: int, *, session_id: str = "") -> Dict[str, Any]:
    """Retire an item: it stays in the bank's history but is never recalled.
    What it was before (active, evicted, superseded) is remembered, so that
    ``restore`` puts it back exactly there."""
    node = store.get(node_id)
    if node is None:
        return {"error": f"no item with id {node_id}"}
    if node["in_core"]:
        return {"error": CORE_OWNED}
    meta: Dict[str, Any] = {"forgotten": True}
    if node["status"] != "archived":
        meta["status_before"] = node["status"]
    with store.transaction():
        store.upsert_node(node["uid"], node["kind"], status="archived", meta=meta)
        store.add_event("forget", [node_id], session_id)
    return {"id": node_id, "forgotten": True, "text": clip(node["content"] or node["title"], 160)}


def restore(store: Store, node_id: int) -> Dict[str, Any]:
    """Undo a ``forget``. Only an item that was retired by forgetting it can be
    restored (not one the scanner withheld, nor a skill whose folder is gone)."""
    node = store.get(node_id)
    if node is None:
        return {"error": f"no item with id {node_id}"}
    if node["meta"].get("flagged"):
        return {"error": "withheld because Hermes' safety scan flags its text; it cannot be made recallable"}
    if node["status"] != "archived" or not node["meta"].get("forgotten"):
        return {"error": "that item was not retired by forgetting it, so there is nothing to restore"}
    before = node["meta"].get("status_before")
    status = before if before in ("active", "evicted", "superseded") else "active"
    with store.transaction():
        store.upsert_node(node["uid"], node["kind"], status=status, meta={"forgotten": False})
        store.add_event("restore", [node_id])
    return {"id": node_id, "restored": True}


# --------------------------------------------------------------------------
# MEMORY.md / USER.md mirror
# --------------------------------------------------------------------------

def core_stamp(memories_dir: Any) -> str:
    """Cheap fingerprint of both built-in memory files (two ``stat`` calls)."""
    directory = Path(memories_dir)
    stamps: List[str] = []
    for _, name in CORE_FILES:
        try:
            st = (directory / name).stat()
            stamps.append(f"{name}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            stamps.append(f"{name}:-")
    return "|".join(stamps)


def reconcile_core(store: Store, memories_dir: Any, *, force: bool = False, scan: Scan = None) -> Dict[str, int]:
    """Mirror Hermes' built-in memory files into the bank.

    Whatever is in the files right now is marked ``in_core`` (it is already in
    every system prompt, so recall skips it). An entry that leaves a file is
    kept: an edit becomes a ``supersedes`` link to the old wording, anything
    else becomes ``evicted`` - no longer always-on, still recallable (marked
    as such). Works from the files alone, so it also catches writes made by
    background reviews, other sessions and manual edits.

    ``scan`` is Hermes' own memory scanner. Hermes blanks a flagged entry in
    its system prompt; the bank tracks such an entry but never recalls it or
    returns it from a search, in the file or after it left.
    """
    directory = Path(memories_dir)
    stamp = core_stamp(directory)
    if not force and store.get_meta("core_stamp") == stamp:
        return {"unchanged": 1}

    result = {"added": 0, "evicted": 0, "superseded": 0, "restored": 0, "withheld": 0}
    complete = True
    touched: List[int] = []
    # Entries found on the very first look were written some time before now;
    # the file's own date is the closest thing to "when" that exists.
    first_look = store.get_meta("core_stamp") is None
    with store.transaction():
        tracked = [n for n in store.list_nodes("memory") if n["in_core"]]
        # Both files are read first: an entry that stands in both is not "gone"
        # while either still has it.
        files: Dict[str, Dict[str, str]] = {}
        written: Dict[str, Optional[float]] = {}
        for target, name in CORE_FILES:
            path = directory / name
            written[target] = None
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
                if first_look:
                    written[target] = path.stat().st_mtime
            except FileNotFoundError:
                raw = ""
            except OSError:
                # Locked or unreadable right now: never mistake that for "emptied".
                complete = False
                continue
            files[target] = {content_hash(entry): entry for entry in split_core_entries(raw)}
        present: Set[str] = set()
        for current in files.values():
            present.update(current)

        for target, current in files.items():
            mine = [n for n in tracked if n["meta"].get("core") == target]
            gone = [n for n in mine if n["content_hash"] not in present]
            gone_tokens = {n["id"]: term_set(n["content"]) for n in gone}
            consumed: Set[int] = set()
            known_hashes = {n["content_hash"] for n in mine}

            for digest, text in current.items():
                if digest in known_hashes:
                    continue
                existing = store.find_by_hash("memory", digest)
                if existing is not None:
                    other = existing["meta"].get("core")
                    if (other and other != target and existing["in_core"] and digest in files.get(other, {})
                            and not existing["meta"].get("flagged")):
                        continue  # the same entry stands in both files: tracked through the other one
                bad = trips(scan, text)
                result["withheld"] += int(bad)
                if existing is not None:
                    store.upsert_node(
                        existing["uid"], "memory", in_core=True, status="archived" if bad else "active",
                        meta={"core": target, "flagged": bad},
                    )
                    result["restored"] += 1
                    touched.append(existing["id"])
                    continue
                saved = remember(
                    store, text, subtype="profile" if target == "user" else "note",
                    importance=0.7, confidence=0.9, source="core:" + target, in_core=True,
                    meta={"core": target, "flagged": True} if bad else {"core": target}, dedupe=False,
                    created_at=written[target], status="archived" if bad else None,
                    min_chars=1,  # the mirror is faithful: even an entry that is just "." is tracked
                )
                node_id = saved.get("id")
                if node_id is None:
                    continue
                result["added"] += 1
                touched.append(node_id)
                # An entry that vanished while a similar one appeared was edited.
                tokens = term_set(text)
                best, best_sim = None, 0.0
                for old in gone:
                    if old["id"] in consumed:
                        continue
                    sim = jaccard(tokens, gone_tokens[old["id"]])
                    if sim > best_sim:
                        best, best_sim = old, sim
                if best is not None and best_sim >= 0.5:
                    consumed.add(best["id"])
                    store.upsert_node(best["uid"], "memory", status="superseded", in_core=False)
                    store.link(node_id, best["id"], "supersedes", 1.0)
                    result["superseded"] += 1

            for old in gone:
                if old["id"] in consumed:
                    continue
                # What Hermes would not show stays out of recall for good.
                hidden = old["status"] == "archived" or trips(scan, old["content"])
                store.upsert_node(old["uid"], "memory", status="archived" if hidden else "evicted", in_core=False)
                result["evicted"] += 1
                touched.append(old["id"])
        if complete:
            store.set_meta("core_stamp", stamp)
        if touched:
            store.add_event("core", touched[:50], "", json.dumps(result, separators=(",", ":")))
    return result


def retire_core_entry(
    store: Store, previous: str, *, replaced_by: str = "", session_id: str = ""
) -> Optional[int]:
    """The built-in memory tool removed or replaced exactly this entry.

    A deliberate removal means "no longer wanted" - it was wrong, private or
    obsolete - so the entry is retired (kept in the bank's history, never
    recalled) rather than treated like one that merely fell out of the file.
    A replacement becomes the old wording of the new entry. Call this after
    ``reconcile_core``; an entry that is still in a file is left alone.
    """
    text = (previous or "").strip()
    if len(text) < 3:
        return None
    node = store.find_by_hash("memory", content_hash(text))
    if node is None or node["in_core"] or node["status"] != "evicted":
        return None
    with store.transaction():
        newer = store.find_by_hash("memory", content_hash(replaced_by)) if replaced_by.strip() else None
        if newer is not None and newer["id"] != node["id"]:
            store.upsert_node(node["uid"], "memory", status="superseded")
            store.link(newer["id"], node["id"], "supersedes", 1.0)
        else:
            store.upsert_node(node["uid"], "memory", status="archived",
                              meta={"forgotten": True, "status_before": "evicted"})
        store.add_event("forget", [node["id"]], session_id, "core")
    return int(node["id"])


# --------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------

def _parse_ts(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def find_skill_files(skills_dir: Any) -> List[Path]:
    """Every live ``SKILL.md`` (hidden folders such as ``.archive`` are skipped)."""
    root = Path(skills_dir)
    found: List[Path] = []
    if not root.is_dir():
        return found
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__"))
        if "SKILL.md" in names:
            found.append(Path(base) / "SKILL.md")
            dirs[:] = []  # a skill's own sub-folders hold assets, not skills
    return found


def _skill_importance(uses: int) -> float:
    return min(0.9, 0.3 + math.log1p(max(0, uses)) / 8.0)


def _usage_meta(stats: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "use_count": int(stats.get("use_count") or 0), "view_count": int(stats.get("view_count") or 0),
        "last_used_at": stats.get("last_used_at"), "created_by": stats.get("created_by"),
        "state": stats.get("state"),
    }


def _skill_instructions(text: str) -> str:
    """A SKILL.md without its front matter."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            rest = text[end + 1:]
            return rest[rest.find("\n") + 1:] if "\n" in rest else ""
    return text


def import_skills(store: Store, skills_dir: Any, *, force: bool = False) -> Dict[str, int]:
    """Index every skill: name, description, category, usage, and its full
    instructions as searchable text.

    Skill files are only re-read when one of them changed; when only the usage
    counters moved (every time a skill is loaded) just the numbers are refreshed.
    """
    root = Path(skills_dir)
    files = find_skill_files(root)
    usage_path = root / ".usage.json"
    newest = 0
    for path in files:
        try:
            newest = max(newest, path.stat().st_mtime_ns)
        except OSError:
            pass
    try:
        usage_stamp = str(usage_path.stat().st_mtime_ns)
    except OSError:
        usage_stamp = "0"
    files_stamp = f"{len(files)}:{newest}"
    files_changed = force or store.get_meta("skills_files_stamp") != files_stamp
    usage_changed = force or store.get_meta("skills_usage_stamp") != usage_stamp
    if not files_changed and not usage_changed:
        return {"unchanged": 1}

    usage: Dict[str, Any] = {}
    try:
        loaded = json.loads(usage_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            usage = {k: v for k, v in loaded.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        pass

    result = {"skills": 0, "new": 0, "archived": 0}
    if not files_changed:
        with store.transaction():
            have = store.uids("skill", statuses=("active",))
            for name, stats in usage.items():
                if "k:" + name in have:
                    store.upsert_node(
                        "k:" + name, "skill", importance=_skill_importance(int(stats.get("use_count") or 0)),
                        meta=_usage_meta(stats),
                    )
            store.set_meta("skills_usage_stamp", usage_stamp)
        result["skills"] = len(have)
        return result

    # Every file is read before the write lock is taken: a first pass over a
    # couple of hundred skills takes seconds on a cold disk, and nothing else
    # can write to the bank while a transaction is open.
    records: List[Tuple[str, str, str, str, float, str]] = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(200_000)
            mtime = path.stat().st_mtime
        except OSError:
            continue
        front = parse_frontmatter(text[:6000])
        name = (front.get("name") or path.parent.name).strip()
        if not name:
            continue
        parts = path.relative_to(root).parts
        records.append((
            name, parts[0] if len(parts) >= 3 else "", clip(front.get("description", ""), 400),
            _skill_instructions(text).strip()[:SKILL_BODY_CHARS], mtime, "/".join(parts[:-1]),
        ))

    # A skill is known by its name; when two folders use the same name (Hermes
    # itself calls that ambiguous) each keeps its own node, keyed by its folder.
    uses: Dict[str, int] = {}
    for record in records:
        uses[record[0]] = uses.get(record[0], 0) + 1
    with store.transaction():
        existing = {n["uid"]: n for n in store.list_nodes("skill")}
        live: Set[str] = set()
        for name, category, description, body, mtime, folder in records:
            stats = usage.get(name, {})
            uid = "k:" + (name if uses[name] == 1 else folder)
            live.add(uid)
            born = _parse_ts(stats.get("created_at")) or mtime
            known = existing.get(uid)
            # A skill someone retired in the viewer stays retired.
            retired = known is not None and bool(known["meta"].get("forgotten"))
            node_id, created = store.upsert_node(
                uid, "skill", subtype=category, title=name, content=description, body=body,
                status=None if retired else "active",
                importance=_skill_importance(int(stats.get("use_count") or 0)), source="skill", created_at=born,
                meta={"path": folder, "category": category, **_usage_meta(stats)},
            )
            result["new"] += int(created)
            if category:
                topic = store.ensure_topic(category, created_at=born)
                if topic is not None:
                    store.link(node_id, topic, "in", 1.0)
        result["skills"] = len(live)
        for uid, node in existing.items():
            if uid not in live and node["status"] == "active":
                store.upsert_node(uid, "skill", status="archived")
                result["archived"] += 1
        store.set_meta("skills_files_stamp", files_stamp)
        store.set_meta("skills_usage_stamp", usage_stamp)
        if result["new"] or result["archived"]:
            store.add_event("skills", [], "", json.dumps(result, separators=(",", ":")))
    return result


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def message_text(content: Any) -> str:
    """Plain text of a stored message (multimodal parts flattened).

    Structured content is stored by Hermes behind ``CONTENT_JSON_PREFIX``; one that was read from a blob arrives as
    bytes. Both are understood, and so is the same list written as plain JSON text."""
    if isinstance(content, (bytes, bytearray, memoryview)):
        content = bytes(content).decode("utf-8", "replace")
    if isinstance(content, str):
        structured = content.startswith(CONTENT_JSON_PREFIX)
        if structured:
            content = content[len(CONTENT_JSON_PREFIX):]
        if structured or (content[:1] == "[" and '"type"' in content[:300]):
            try:
                content = json.loads(content)
            except (ValueError, RecursionError):   # a hostile, absurdly nested message is just text parts, not a crash
                # A part list cut short (the importer reads only the start of
                # very large messages): keep its text parts, drop the rest.
                texts = []
                for match in _JSON_TEXT_RE.finditer(content):
                    raw, closed = match.group(1), match.group(2)
                    if not closed:
                        raw = _CUT_ESCAPE_RE.sub("", raw)   # cut inside this text: keep it as far as it goes
                    try:
                        text = json.loads('"' + raw + '"')
                    except ValueError:
                        continue
                    texts.append(text if closed else _LONE_SURROGATE_RE.sub("", text))
                return "\n".join(texts)
        else:
            return content
    if isinstance(content, dict):
        content = [content]
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""


def skills_from_tool_calls(tool_calls: Any) -> List[str]:
    """Skill names loaded by an assistant message's tool calls."""
    if isinstance(tool_calls, str):
        try:
            tool_calls = json.loads(tool_calls)
        except (ValueError, RecursionError):
            return []
    if not isinstance(tool_calls, list):
        return []
    out: List[str] = []
    for call in tool_calls:
        fn = call.get("function") if isinstance(call, dict) else None
        if not isinstance(fn, dict) or fn.get("name") not in _SKILL_TOOLS:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, RecursionError):
                args = {}
        if not isinstance(args, dict):
            continue
        name = args.get("name") or args.get("skill") or args.get("skill_name")
        if isinstance(name, str) and name.strip():
            short = name.strip().replace("\\", "/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            # The argument is model-written. A skill name is a slug; anything
            # else is not a name and is not kept.
            if _SKILL_NAME.fullmatch(short):
                out.append(short)
    return out


def session_body(prompts: Iterable[str], scan: Scan = None) -> str:
    """The later prompts of a conversation as searchable text. It is indexed
    but never shown in recall or tool results, so a long session can be found
    by anything that was asked in it."""
    parts: List[str] = []
    used = 0
    for prompt in prompts:
        piece = clip(strip_refs(prompt), SESSION_BODY_PROMPT_CHARS)
        if len(piece) < 3 or trips(scan, piece):
            continue
        if len(parts) >= SESSION_BODY_PROMPTS or used + len(piece) + 3 > SESSION_BODY_CHARS:
            break
        parts.append(piece)
        used += len(piece) + 3
    return " ¶ ".join(parts)


def upsert_session(
    store: Store,
    session_id: str,
    *,
    title: str = "",
    fallback_title: str = "",
    prompts: Sequence[str] = (),
    outcome: str = "",
    skills: Iterable[str] = (),
    started_at: Optional[float] = None,
    updated_at: Optional[float] = None,
    message_count: Optional[int] = None,
    source: str = "",
    model: str = "",
    parent_session_id: str = "",
    scan: Scan = None,
    extra_meta: Optional[Dict[str, Any]] = None,
    link_text: Optional[str] = None,
    body: Optional[str] = None,
    tally: Optional[Dict[str, int]] = None,
) -> Optional[int]:
    """Create or refresh the episode node for one conversation.

    ``prompts`` (the first few) are kept for display; ``body`` is the
    searchable text of the later ones (see ``session_body``). ``title`` is a
    title somebody chose (the user, or Hermes' title model); ``fallback_title``
    is only used when there is neither such a title nor a usable prompt.

    Every piece is screened on its own with ``scan``: a prompt, reply or title
    that looks like injected instructions is left out, and nothing else is
    lost because of it.
    """
    if not session_id:
        return None
    given = [clip(p, 280) for p in prompts if p and p.strip()][:MAX_PROMPTS]
    if not given and not title and not fallback_title:
        return None
    kept = [p for p in given if not trips(scan, p)]
    result = clip(outcome, 300) if outcome else ""
    if result and trips(scan, result):
        result = ""
    name = clip(title, 90) if title else ""
    if name and trips(scan, name):
        name = ""
    spare = clip(fallback_title, 90) if fallback_title and not name and not kept else ""
    if spare and trips(scan, spare):
        spare = ""
    if tally is not None:
        dropped = (len(given) - len(kept)) + int(bool(outcome) and not result) + int(bool(title) and not name)
        tally["withheld"] = tally.get("withheld", 0) + dropped
    # Nothing clean is left: keep the episode (when, how long), none of its text.
    flagged = not kept and not name and not spare
    if flagged:
        body, link_text = "", ""
    label = name or (make_title(kept[0], 72) if kept else "") or spare or "Session"
    # Indexed without links and paths; the excerpts shown to people keep them.
    summary = " ¶ ".join(norm_text(strip_refs(p)) for p in kept)
    if result:
        summary += ("\n→ " if summary else "→ ") + result
    skill_names = [] if flagged else sorted({s for s in skills if s})[:12]
    meta: Dict[str, Any] = {"prompts": kept, "outcome": result, "skills": skill_names, "flagged": flagged}
    if message_count is not None:
        meta["messages"] = int(message_count)
    if model:
        meta["model"] = model
    if extra_meta:
        meta.update(extra_meta)
    size = message_count if message_count else len(kept)
    with store.transaction():
        node_id, _ = store.upsert_node(
            "s:" + session_id, "session", title=label, content=summary, body=body, tags=split_camel(label),
            importance=0.35 + min(0.3, math.log1p(size) / 20.0), source=source or "session",
            session_id=session_id, created_at=started_at, updated_at=updated_at, meta=meta,
        )
        text = link_text if link_text is not None else "\n".join([label] + kept)
        if text and not flagged:
            link_entities(store, node_id, text, max_entities=16, created_at=started_at)
        for skill_name in skill_names:
            skill = store.get_by_uid("k:" + skill_name)
            if skill is not None:
                store.link(node_id, skill["id"], "used", 1.0)
        if parent_session_id:
            parent = store.get_by_uid("s:" + parent_session_id)
            if parent is not None:
                store.link(node_id, parent["id"], "continues", 1.0)
    return node_id


def capture_turn(
    store: Store,
    session_id: str,
    user_text: str,
    assistant_text: str,
    *,
    messages: Optional[List[Dict[str, Any]]] = None,
    title: str = "",
    source: str = "",
    model: str = "",
    parent_session_id: str = "",
    scan: Scan = None,
    scope: str = "",
) -> Optional[int]:
    """Fold one completed turn into its session's episode node.

    What earlier turns stored is never taken back because of this one: a
    prompt or reply that the scanner flags is simply not added.
    """
    node = store.get_by_uid("s:" + session_id)
    meta = node["meta"] if node else {}
    prompts = list(meta.get("prompts") or [])
    body = (node["body"] if node else "") or ""
    prompt = clean_prompt(user_text)
    fresh = bool(prompt) and not is_noise_prompt(prompt) and not trips(scan, prompt[:4000])
    if fresh:
        clipped = clip(prompt, 280)
        if clipped not in prompts:
            if len(prompts) < MAX_PROMPTS:
                prompts.append(clipped)
            else:
                # Past the first few: kept as searchable text only.
                piece = clip(strip_refs(prompt), SESSION_BODY_PROMPT_CHARS)
                if len(piece) >= 3 and piece not in body and len(body) + len(piece) + 3 <= SESSION_BODY_CHARS:
                    body = f"{body} ¶ {piece}" if body else piece
    skills = set(meta.get("skills") or [])
    count = None
    if messages:
        count = len(messages)
        for message in list(messages):
            if isinstance(message, dict) and message.get("role") == "assistant" and message.get("tool_calls"):
                skills.update(skills_from_tool_calls(message["tool_calls"]))
    keep_title = title or (node["title"] if node and meta.get("titled") else "")
    extra: Dict[str, Any] = {
        "turns": int(meta.get("turns") or 0) + 1, "titled": bool(title) or bool(meta.get("titled")),
    }
    if scope:
        extra["scope"] = scope
    node_id = upsert_session(
        store, session_id, title=keep_title, prompts=prompts, outcome=message_text(assistant_text),
        skills=skills, updated_at=time.time(), message_count=count, source=source, model=model,
        parent_session_id=parent_session_id, scan=scan, extra_meta=extra,
        link_text=(prompt[:4000] if fresh else ""), body=body,
    )
    if node is None and node_id is not None:
        store.add_event("session", [node_id], session_id)  # lets an open viewer show it appear
    return node_id


def _columns(conn: sqlite3.Connection, table: str) -> Set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _opening_sql(chars: int) -> str:
    """SQL for the opening of ``content``: ``chars`` characters of an ordinary message, and the prefix plus ``chars``
    bytes of a structured one (read as a blob, then handed back as text, still behind its prefix). An ordinary row is
    read exactly as it always was - in characters, so that text outside ASCII is not cut shorter."""
    size = len(CONTENT_JSON_PREFIX)
    return (f"CASE WHEN substr(CAST(content AS BLOB), 1, {size}) = {_PREFIX_BLOB}"
            f" THEN CAST(substr(CAST(content AS BLOB), 1, {chars + size}) AS TEXT)"
            f" ELSE substr(content, 1, {chars}) END")


def _lenient_text(raw: bytes) -> str:
    """``text_factory`` for Hermes' database: a stray byte that is not UTF-8 becomes U+FFFD. By default sqlite3 raises
    "Could not decode to UTF-8" instead, and one such value would stop every import and every check."""
    return raw.decode("utf-8", "replace")


def _is_busy(exc: BaseException) -> bool:
    """A lock that another process holds right now (it passes), as opposed to a database that is damaged or full."""
    code = getattr(exc, "sqlite_errorcode", None)      # Python 3.11+
    if isinstance(code, int):
        return (code & 0xFF) in (5, 6)                 # SQLITE_BUSY, SQLITE_LOCKED (and their extended forms)
    # Older Pythons: SQLite's own wording. (Not a search for "locked" anywhere: some messages quote stored text.)
    return str(exc).lower().startswith((
        "database is locked", "database table is locked", "database schema is locked", "database is busy"))


class _ReadFailed(Exception):
    """Hermes' database could not be read for one conversation. Kept apart from ``sqlite3.Error``, which is what the
    bank's own writes raise: those stop the import so that the caller can retry it."""


def _remembered_failures(store: Store) -> List[str]:
    """Ids of the conversations the last import could not index (the bank's ``sessions_failed`` note; at most
    ``FAILED_KEEP``). Anything else in that note is ignored."""
    try:
        loaded = json.loads(store.get_meta("sessions_failed", "[]") or "[]")
    except (TypeError, ValueError):
        return []
    return [i for i in loaded if isinstance(i, str)][:FAILED_KEEP] if isinstance(loaded, list) else []


def read_session_title(state_db: Any, session_id: str) -> str:
    """The title of a session when somebody chose it - the user, or Hermes'
    title model ("" otherwise; read-only, best effort). A title Hermes merely
    cut from the first words of the opening message is not a title."""
    try:
        conn = sqlite3.connect(Path(state_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
    except (sqlite3.Error, OSError, ValueError):
        return ""
    try:
        if "title_source" in _columns(conn, "sessions"):
            row = conn.execute("SELECT title, title_source FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row and (row[1] or "") == "derived":
                return ""
        else:
            row = conn.execute("SELECT title FROM sessions WHERE id=?", (session_id,)).fetchone()
        return (row[0] or "").strip() if row else ""
    except sqlite3.Error:
        return ""
    finally:
        conn.close()


def session_has_messages(state_db: Any, session_id: str) -> bool:
    """Has this conversation already got messages in Hermes' database? (False
    when unknown: a conversation that cannot be found is treated as new.)"""
    try:
        conn = sqlite3.connect(Path(state_db).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
    except (sqlite3.Error, OSError, ValueError):
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM messages WHERE session_id=? AND role IN ('user', 'assistant') LIMIT 1", (session_id,)
        ).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def import_sessions(
    store: Store,
    state_db: Any,
    *,
    limit: int = 5000,
    scan: Scan = None,
    force: bool = False,
    skip_sources: Sequence[str] = UNATTENDED_SOURCES,
) -> Dict[str, Any]:
    """Index past conversations from Hermes' session database (opened read-only).

    Each becomes one episode node: its title, the first few things the user
    asked, where it ended up, the later prompts as searchable text, and links
    to the skills and entities involved. Incremental: only sessions active
    since the last import are re-read. ``limit`` is how many of the most recent
    conversations to read (0 = all of them); conversations nobody typed into,
    hidden ones and ones with fewer than two messages never count against it.
    A conversation that is hidden in Hermes after it was indexed leaves the
    bank's recall (``retired``). When Hermes shows it again - unhidden, or pinned,
    which changes nothing the watermark could see - the next import reads it
    again and brings it back (``revived``), unless the person forgot it in the
    viewer meanwhile. One that is simply gone from Hermes' database stays
    (Hermes deletes old conversations by itself, and the bank exists to keep
    them).

    A conversation that cannot be indexed (its text cannot be stored, a
    timestamp is not a number, a page of Hermes' file cannot be read) is counted
    in ``failed``, named in ``failed_ids`` (at most ``FAILED_KEEP``, each logged
    at WARNING) and remembered in the bank: ``check`` reports it as unreadable
    and every later import tries it again. It never holds back the conversations
    after it. When not one conversation of a run could be indexed and there are
    more than ``FAILED_KEEP`` of them, that is a bug and not bad data: the first
    error is raised and nothing is recorded as done, so that the next import
    indexes them all. ``skipped`` counts only conversations that were never
    meant to be indexed (machine sessions, hidden ones, ones with fewer than two
    messages).
    """
    path = Path(state_db)
    result: Dict[str, Any] = {
        "sessions": 0, "new": 0, "skipped": 0, "withheld": 0, "retired": 0, "revived": 0, "failed": 0,
        "failed_ids": [],
    }
    if not path.exists():
        return result
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3.0)
    except (sqlite3.Error, OSError, ValueError):
        return result
    conn.row_factory = sqlite3.Row
    conn.text_factory = _lenient_text      # one stray byte that is not UTF-8 must not stop the import
    try:
        try:
            scols, mcols = _columns(conn, "sessions"), _columns(conn, "messages")
        except sqlite3.Error:
            return result
        if "id" not in scols or "session_id" not in mcols:
            return result

        EMPTY_SQL = "''"  # an empty SQL string literal

        def col(name: str, fallback: str = "NULL") -> str:
            return name if name in scols else f"{fallback} AS {name}"

        activity = "COALESCE(last_activity_at, ended_at, started_at)" if {
            "last_activity_at", "ended_at", "started_at"} <= scols else "started_at"
        watermark = 0.0 if force else float(store.get_meta("sessions_watermark", "0") or 0)
        # Which conversations count is decided in SQL, before the cap: machine
        # sessions (cron runs, delegated sub-tasks) must not use up the slots
        # that real conversations need.
        eligible: List[str] = []
        params: List[Any] = []
        skip = tuple(skip_sources or ())
        if "source" in scols and skip:
            eligible.append(f"COALESCE(source, '') NOT IN ({','.join('?' * len(skip))})")
            params.extend(skip)
        if "hidden" in scols:
            eligible.append("COALESCE(hidden, 0) = 0")
        if "message_count" in scols:
            eligible.append("COALESCE(message_count, 0) >= 2")
        keep = " AND ".join(eligible) or "1"
        cap = min(int(limit), 2 ** 62) if int(limit) > 0 else -1   # SQLite binds 64-bit integers: more is "all" anyway
        select = (
            f"SELECT id, {col('title')}, {col('title_source')}, {col('started_at')}, {col('source', EMPTY_SQL)},"
            f" {col('message_count', '0')}, {col('parent_session_id')}, {col('model')}, {col('hidden', '0')},"
            f" {col('session_key')}, {col('user_id')},"
            f" {activity} AS activity FROM sessions"
        )
        rows = conn.execute(
            f"{select} WHERE COALESCE({activity}, 0) > ? AND {keep} ORDER BY started_at DESC LIMIT ?",
            (watermark, *params, cap),
        ).fetchall()
        if eligible:
            result["skipped"] = int(conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE COALESCE({activity}, 0) > ? AND NOT ({keep})",
                (watermark, *params),
            ).fetchone()[0])

        summary_filter = " AND COALESCE(_compressed_summary, 0) = 0" if "_compressed_summary" in mcols else ""
        active_filter = " AND COALESCE(active, 1) = 1" if "active" in mcols else ""
        human_filter = _HUMAN_ROWS if "display_kind" in mcols else ""
        newest = watermark
        tally: Dict[str, int] = {}

        def read_conversation(sid: str) -> Tuple[List[str], List[str], str, Set[str]]:
            """What Hermes' database holds about one conversation: ``(first prompts, later prompts, last reply,
            skills)``. Nothing here touches the bank. Only the opening of each message is read - a pasted log, or the
            inline image parts of a structured message, can make one message megabytes long - and a structured
            message (it starts with a NUL, which ends it early for SQLite's own text functions) as bytes."""
            try:
                prompts: List[str] = []
                later: List[str] = []
                for msg in conn.execute(
                    f"SELECT {_opening_sql(6000)} AS content FROM messages WHERE session_id=?"
                    f" AND role='user'{summary_filter}{active_filter}{human_filter} ORDER BY id LIMIT ?",
                    (sid, MAX_PROMPTS + SESSION_BODY_PROMPTS + 24),
                ):
                    text = clean_prompt(message_text(msg["content"]))
                    if not text or is_noise_prompt(text):
                        continue
                    if len(prompts) < MAX_PROMPTS:
                        prompts.append(text)
                    elif len(later) < SESSION_BODY_PROMPTS:
                        later.append(text)
                    else:
                        break
                # Where it ended up: the newest reply that says something. Plain text is judged in characters, as
                # SQLite does; a structured reply (SQLite sees none of it) by the text that is in it.
                outcome = ""
                for msg in conn.execute(
                    f"SELECT {_opening_sql(2000)} AS content FROM messages"
                    " WHERE session_id=? AND role='assistant' AND content IS NOT NULL AND (length(content) > 20"
                    f" OR substr(CAST(content AS BLOB), 1, {len(CONTENT_JSON_PREFIX)}) = {_PREFIX_BLOB})"
                    f"{summary_filter}{active_filter}{human_filter} ORDER BY id DESC LIMIT 8",
                    (sid,),
                ):
                    text = message_text(msg["content"])
                    if len(text) > 20:
                        outcome = text
                        break
                skills: Set[str] = set()
                if "tool_calls" in mcols:
                    for msg in conn.execute(
                        "SELECT tool_calls FROM messages WHERE session_id=? AND role='assistant'"
                        " AND tool_calls LIKE '%skill_view%' LIMIT 300",
                        (sid,),
                    ):
                        skills.update(skills_from_tool_calls(msg["tool_calls"]))
                return prompts, later, outcome, skills
            except sqlite3.Error as exc:
                if _is_busy(exc):
                    raise   # Hermes holds its file locked right now: stop and try again later, nothing is skipped past
                # Something in Hermes' file that cannot be read for this conversation. The error's own text is left
                # out of the log: sqlite3 puts stored values into some of its messages.
                raise _ReadFailed(
                    (type(exc).__name__ + " " + str(getattr(exc, "sqlite_errorname", ""))).strip()
                    + " while reading Hermes' database"
                ) from None

        def index_one(row: Any, sid: str, activity_ts: float) -> None:
            prompts, later, outcome, skills = read_conversation(sid)
            given_title = (row["title"] or "").strip()
            chosen = "" if (row["title_source"] or "") == "derived" else given_title
            before = store.get_by_uid("s:" + sid)
            # Entities come from the prompts as written (line breaks intact, the
            # opening of each one), not from the flattened excerpts kept for
            # display - and only from prompts the scanner lets through.
            clean = [p for p in prompts if not trips(scan, clip(p, 280))]
            link_text = "\n".join(
                [chosen] + [p[:1500].rsplit(None, 1)[0] if len(p) > 1500 else p for p in clean]
            )
            extra: Dict[str, Any] = {"titled": bool(chosen), "indexed": True}
            scope = str(row["session_key"] or row["user_id"] or "")
            if scope:
                extra["scope"] = scope
            node_id = upsert_session(
                store, sid, title=chosen, fallback_title=given_title, prompts=prompts,
                outcome=outcome, skills=skills,
                started_at=float(row["started_at"]) if row["started_at"] else None,
                updated_at=activity_ts or None, message_count=int(row["message_count"] or 0),
                source=row["source"] or "", model=row["model"] or "",
                parent_session_id=row["parent_session_id"] or "", scan=scan,
                extra_meta=extra, link_text=link_text, body=session_body(later, scan), tally=tally,
            )
            if node_id is not None:
                result["sessions"] += 1
                result["new"] += int(before is None)
                if (before is not None and before["status"] == "archived" and before["meta"].get("gone")
                        and not before["meta"].get("forgotten")):
                    # Hidden or missing last time, back now (unless the person has forgotten it since).
                    store.upsert_node("s:" + sid, "session", status="active", meta={"gone": False})
                    result["revived"] += 1

        failed_ids: List[str] = result["failed_ids"]
        attempts = 0
        first_error: Optional[BaseException] = None

        def attempt(row: Any) -> None:
            """Index one conversation. An error from the bank (busy, full, read-only), or a lock on Hermes' file, stops
            the whole import so that the caller can retry it: nothing is skipped past. Anything else is this
            conversation's own trouble (text that cannot be stored, a timestamp that is not a number, an unreadable
            page): it is counted, logged and remembered, and the import goes on."""
            nonlocal newest, attempts, first_error
            attempts += 1
            sid = row["id"]
            try:
                activity_ts = float(row["activity"] or row["started_at"] or 0)
                newest = max(newest, activity_ts)   # a conversation that always fails must not hold the watermark back
                index_one(row, sid, activity_ts)
            except sqlite3.Error:
                raise
            except Exception as exc:
                result["failed"] += 1
                first_error = first_error or exc
                if len(failed_ids) < FAILED_KEEP:   # a bug that hits every conversation must not flood the log
                    failed_ids.append(str(sid))
                    reason = str(exc) if isinstance(exc, _ReadFailed) else f"{type(exc).__name__}: {exc}"
                    logger.warning("memorybank could not index session %s: %s (it is tried again at the next import)",
                                   sid, clip(reason, 160))

        # Some conversations are read again whatever their activity says, because the watermark would never select
        # them: the ones that failed last time (they index by themselves once the cause is gone), and the ones the bank
        # retired because Hermes hid them - unhiding or pinning a conversation changes nothing but its hidden flag, and
        # reading it again also brings in what was said while it was hidden. What counts is still decided in SQL
        # (keep): one that is hidden again, or gone from Hermes, is not read and stays as it was. One the person has
        # forgotten in the viewer is not looked for.
        remembered = _remembered_failures(store)
        was_hidden = ([n["uid"][2:] for n in store.list_nodes("session", statuses=("archived",))
                       if n["meta"].get("gone") and not n["meta"].get("forgotten")] if "hidden" in scols else [])
        selected = {row["id"] for row in rows}
        again = [sid for sid in dict.fromkeys(remembered + was_hidden) if sid not in selected]
        for start in range(0, len(again), 500):   # SQLite limits how many values one IN list may hold
            chunk = again[start:start + 500]
            for row in conn.execute(
                f"{select} WHERE id IN ({','.join('?' * len(chunk))}) AND {keep}", (*chunk, *params)
            ).fetchall():
                attempt(row)

        # Oldest first, so a follow-up session finds the one it continues.
        for row in reversed(rows):
            attempt(row)
        result["withheld"] = tally.get("withheld", 0)
        if first_error is not None and result["failed"] == attempts and result["failed"] > FAILED_KEEP:
            # Not one conversation could be indexed, and there are more than can be remembered: that is a bug, not bad
            # data. Nothing is recorded as done (no watermark), so that a fixed version indexes them all next time.
            raise first_error
        if result["failed"] > len(failed_ids):
            logger.warning("memorybank could not index %d more conversations (not listed or remembered here; "
                           "import --force tries them again)", result["failed"] - len(failed_ids))
        if sorted(failed_ids) != sorted(remembered):
            store.set_meta("sessions_failed", json.dumps(failed_ids))

        # A conversation hidden in Hermes since it was indexed leaves the bank's
        # recall (and comes back, above, when it is unhidden). One that is gone
        # from the database is left alone: Hermes deletes old conversations on
        # its own (sessions.auto_prune, 90 days by default) and nothing tells
        # that apart from a deletion by hand.
        hidden_ids: Set[str] = set()
        if "hidden" in scols:
            try:
                hidden_ids = {r[0] for r in conn.execute("SELECT id FROM sessions WHERE COALESCE(hidden, 0) = 1")}
            except sqlite3.Error:
                hidden_ids = set()
        if hidden_ids:
            # Any session the database lists as hidden - including one captured live that no importer pass had
            # seen yet. (A node whose conversation is absent from the database is never in this set.)
            for node in store.list_nodes("session", statuses=("active",)):
                if node["uid"][2:] in hidden_ids:
                    store.upsert_node(node["uid"], "session", status="archived", meta={"gone": True})
                    result["retired"] += 1

        if newest > watermark:
            store.set_meta("sessions_watermark", repr(newest))
        store.set_meta("sessions_imported_at", repr(time.time()))
        if result["new"] or result["retired"] or result["revived"]:
            counts = {k: v for k, v in result.items() if k != "failed_ids"}
            store.add_event("sessions", [], "", json.dumps(counts, separators=(",", ":")))
    finally:
        conn.close()
    return result


def seed(
    store: Store,
    hermes_home: Any,
    *,
    sessions: bool = True,
    skills: bool = True,
    core: bool = True,
    limit: int = 5000,
    scan: Scan = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Bring a bank up to date with everything Hermes already knows."""
    home = Path(hermes_home)
    out: Dict[str, Any] = {}
    if skills:
        out["skills"] = import_skills(store, home / "skills", force=force)
    if core:
        out["core"] = reconcile_core(store, home / "memories", force=force, scan=scan)
    if sessions:
        out["sessions"] = import_sessions(store, home / "state.db", limit=limit, scan=scan, force=force)
    return out
