"""SQLite graph store for the Memory Bank.

One database per Hermes home (profile). Nodes are memories, entities, skills,
sessions and topics; edges connect them; an FTS5 index makes them searchable.
Every write is a short transaction so the agent, the gateway, the desktop
backend and the graph viewer can all share the file (WAL mode, wherever this
SQLite and the file system make that safe; see ``_wal_reset_bug``).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from .config import LIMIT_CAP
from .extract import content_hash, join_tags, make_title, norm_entity

SCHEMA_VERSION = 2

#: Items deleted per transaction by ``purge`` (the bank stays writable for other processes between two of them) and the
#: pause between two, so that a process waiting for the write lock gets a turn.
_PURGE_BATCH = 250
_PURGE_PAUSE = 0.01
#: Prefixes of ``meta`` rows (written with ``put_snapshot``) that hold a copy of node text. A purge removes them.
_TEXT_SNAPSHOTS = ("standing:",)

#: Statuses that take part in recall. ``evicted`` = dropped from MEMORY.md /
#: USER.md to make room but still true; ``superseded`` and ``archived`` do not.
RECALLABLE = ("active", "evicted")

_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5("
    "title, content, tags, body, tokenize='porter unicode61 remove_diacritics 2')"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uid TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  subtype TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  in_core INTEGER NOT NULL DEFAULT 0,
  importance REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 0.8,
  source TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_recalled REAL NOT NULL DEFAULT 0,
  recall_count INTEGER NOT NULL DEFAULT 0,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS nodes_kind ON nodes(kind, status);
CREATE INDEX IF NOT EXISTS nodes_hash ON nodes(kind, content_hash);
CREATE TABLE IF NOT EXISTS edges(
  src INTEGER NOT NULL,
  dst INTEGER NOT NULL,
  rel TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY(src, dst, rel)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS recalls(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  session_id TEXT NOT NULL,
  node_id INTEGER NOT NULL,
  score REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS recalls_session ON recalls(session_id, node_id);
CREATE INDEX IF NOT EXISTS recalls_node ON recalls(node_id);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  type TEXT NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  nodes TEXT NOT NULL DEFAULT '[]',
  info TEXT NOT NULL DEFAULT ''
);
"""

_NODE_FIELDS = (
    "subtype", "title", "content", "body", "tags", "status", "in_core", "importance",
    "confidence", "source", "session_id",
)
#: Every column except ``body``: listings and ranking never need the long text.
_LIGHT = (
    "id, uid, kind, subtype, title, content, tags, status, in_core, importance, confidence, source,"
    " session_id, content_hash, created_at, updated_at, last_recalled, recall_count, meta"
)
#: Relative weight of a match in title / content / tags / body.
_BM25 = "bm25(nodes_fts, 3.0, 1.0, 1.5, 0.5)"
_EVENT_KEEP = 5000


def fts5_available() -> bool:
    """True when this Python's SQLite can build the index the bank needs."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='porter unicode61')")
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


def _limit(value: Any) -> int:
    """A LIMIT SQLite can bind: a whole number, never beyond ``LIMIT_CAP`` (it binds 64-bit integers; more is an
    OverflowError, not "everything")."""
    return max(-LIMIT_CAP, min(int(value), LIMIT_CAP))


def _wal_reset_bug(info: Optional[Sequence[int]] = None) -> bool:
    """True when this SQLite has the WAL-reset bug (https://sqlite.org/wal.html#walresetbug): present in 3.7.0 - 3.51.2,
    fixed in 3.51.3 and back-ported to 3.50.7 and 3.44.6. Hermes applies the same rule to its own databases."""
    v = tuple((tuple(info or sqlite3.sqlite_version_info) + (0, 0, 0))[:3])
    return not (v < (3, 7, 0) or v >= (3, 51, 3) or (3, 50, 7) <= v < (3, 51, 0) or (3, 44, 6) <= v < (3, 45, 0))


def _durability(wal: bool, platform: str) -> List[str]:
    """The PRAGMAs that make a commit as durable as the journal allows. NORMAL is only corruption-safe in WAL mode (a
    rollback journal needs FULL); on macOS even a WAL bank needs FULL and a full fsync at checkpoints, because Apple's
    fsync() does not order writes (Hermes sets the same two on its own databases)."""
    if not wal:
        return ["PRAGMA synchronous=FULL"]
    if platform == "darwin":
        return ["PRAGMA synchronous=FULL", "PRAGMA checkpoint_fullfsync=1"]
    return ["PRAGMA synchronous=NORMAL"]


class Store:
    """Thread-safe handle on one bank database."""

    def __init__(self, path: Any, *, timeout: float = 2.5, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self._timeout = timeout
        self._lock = threading.RLock()
        self._depth = 0
        #: ``((data_version, generation), index, names)``: the entity index stays valid until another connection writes
        #: (``data_version`` moves) or this one changes an entity (``_entity_gen`` moves) - not on every write.
        self._entity_cache: Optional[Tuple[Tuple[int, int], Dict[str, Tuple[int, str]], Dict[str, str]]] = None
        self._entity_gen = 0
        self.in_memory = str(path) == ":memory:"
        if self.in_memory:
            target, uri = ":memory:", False
        elif readonly:
            target, uri = self.path.resolve().as_uri() + "?mode=ro", True
        else:
            made = not self.path.parent.exists()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                # The bank holds what was said in conversations: on a machine with other users it is for its owner
                # only. A folder this bank just made, and a database it creates, start private (SQLite gives the
                # -wal and -shm files the database's own mode); anything that already existed is left as it was.
                if made:
                    try:
                        self.path.parent.chmod(0o700)
                    except OSError:
                        pass
                if not self.path.exists():
                    try:
                        os.close(os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                    except OSError:
                        pass
            target, uri = str(self.path), False
        self._conn = sqlite3.connect(
            target, uri=uri, timeout=timeout, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        if not readonly:
            wal = False
            if not self.in_memory:
                wal = self._journal_mode() == "wal"
            for pragma in _durability(wal, sys.platform):
                self._conn.execute(pragma)
            self._conn.execute("PRAGMA temp_store=MEMORY")
            # What is deleted (or rewritten) is overwritten with zeros, not left in free pages: "purge" means it.
            self._conn.execute("PRAGMA secure_delete=ON")
            self._init_schema()

    # -- plumbing -----------------------------------------------------------

    def _journal_mode(self) -> str:
        """The journal mode in effect, asking for WAL only where that is safe. A bank that is already in WAL keeps it
        (switching it back would destroy what other connections have not checkpointed yet), a library with the WAL-reset
        bug leaves a new bank in its rollback journal, and a file system that refuses WAL - by raising, or by answering
        "delete" - leaves it there too. Nothing here ever asks for another mode."""
        mode = str(self._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if mode == "wal" or _wal_reset_bug():
            return mode
        try:
            return str(self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        except sqlite3.OperationalError as exc:
            if any(text in str(exc).lower() for text in ("locking protocol", "not authorized")):
                return mode
            raise

    def _init_schema(self) -> None:
        with self._lock:
            # executescript commits implicitly, so it runs outside _tx().
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            version = int(row["value"]) if row and str(row["value"]).isdigit() else 0
            if version >= SCHEMA_VERSION:
                return
        # First open, or a bank written by an older version: one writer at a
        # time brings it up to date (a second process waits, then sees it done).
        with self._tx():
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            version = int(row["value"]) if row and str(row["value"]).isdigit() else 0
            if version < 1:
                self._conn.execute(_FTS_DDL)
                self._conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', ?)", (str(time.time()),)
                )
            elif version < 2:
                self._migrate_1_to_2()
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    def _migrate_1_to_2(self) -> None:
        """v2 adds ``body``: long searchable text (a skill's instructions, the
        later prompts of a session) kept apart from the short ``content``."""
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(nodes)")}
        if "body" not in columns:
            self._conn.execute("ALTER TABLE nodes ADD COLUMN body TEXT NOT NULL DEFAULT ''")
        self._conn.execute("DROP TABLE IF EXISTS nodes_fts")
        self._conn.execute(_FTS_DDL)
        self._conn.execute(
            "INSERT INTO nodes_fts(rowid, title, content, tags, body) SELECT id, title, content, tags, body FROM nodes"
        )
        # The new column is empty, and "nothing changed since last time" would keep it so: forget the stamps that
        # make the skill and session importers skip what they have already seen, so the next import fills it in.
        self._conn.execute(
            "DELETE FROM meta WHERE key IN ('skills_files_stamp', 'skills_usage_stamp', 'sessions_watermark')"
        )

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """Serialised write transaction; nested use joins the outer one."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                self._entity_gen += 1   # an entity created in this transaction may be in the cached index and is gone now
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if outer:
                    try:
                        self._conn.execute("COMMIT")
                    except BaseException:
                        # A COMMIT can fail (a reader holds the file in a rollback-journal bank): the transaction is
                        # still open then, and every later write would fail with "cannot start a transaction within a
                        # transaction". Close it so the store stays usable.
                        self._entity_gen += 1
                        try:
                            self._conn.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                        raise

    def transaction(self):
        """Group several writes into one atomic, single-fsync unit."""
        return self._tx()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def _q(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _q1(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    @staticmethod
    def _node(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        data = dict(row)
        try:
            data["meta"] = json.loads(data.get("meta") or "{}")
        except (TypeError, ValueError):
            data["meta"] = {}
        return data

    # -- meta ---------------------------------------------------------------

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._q1("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._tx():
            self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))

    def probe_write(self, wait: float = 0.1) -> None:
        """Raise ``sqlite3.OperationalError`` when this bank cannot take a write right now. A write lock that another
        process holds raises "database is locked" after ``wait`` seconds, not after the store's full timeout."""
        with self._lock:
            self._conn.execute(f"PRAGMA busy_timeout={int(wait * 1000)}")
            try:
                self.set_meta("opened_at", int(time.time()))
            finally:
                self._conn.execute(f"PRAGMA busy_timeout={int(self._timeout * 1000)}")

    def put_snapshot(self, key: str, value: str, *, keep: int = 200) -> None:
        """Remember a per-session value in the meta table (key ``kind:session``).
        Only the newest ``keep`` of each kind are kept, so it cannot grow."""
        prefix = key.split(":", 1)[0] + ":"
        with self._tx():
            self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, str(value)))
            self._conn.execute(
                "DELETE FROM meta WHERE key LIKE ? AND rowid NOT IN"
                " (SELECT rowid FROM meta WHERE key LIKE ? ORDER BY rowid DESC LIMIT ?)",
                (prefix + "%", prefix + "%", int(keep)),
            )

    # -- nodes --------------------------------------------------------------

    def _index(self, node_id: int, title: str, content: str, tags: str, body: str = "") -> None:
        self._conn.execute("DELETE FROM nodes_fts WHERE rowid=?", (node_id,))
        self._conn.execute(
            "INSERT INTO nodes_fts(rowid, title, content, tags, body) VALUES(?, ?, ?, ?, ?)",
            (node_id, title or "", content or "", tags or "", body or ""),
        )

    def upsert_node(
        self,
        uid: str,
        kind: str,
        *,
        subtype: Optional[str] = None,
        title: Optional[str] = None,
        content: Optional[str] = None,
        body: Optional[str] = None,
        tags: Optional[str] = None,
        status: Optional[str] = None,
        in_core: Optional[bool] = None,
        importance: Optional[float] = None,
        confidence: Optional[float] = None,
        source: Optional[str] = None,
        session_id: Optional[str] = None,
        created_at: Optional[float] = None,
        updated_at: Optional[float] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, bool]:
        """Insert or update the node ``uid``; returns ``(id, created)``.

        Only the fields that are passed change, and ``meta`` is merged key by
        key, so callers can refresh one aspect without clobbering the rest.
        ``content`` is the short text people and the model read; ``body`` is
        longer text that is only searched.
        """
        now = time.time()
        given = {
            "subtype": subtype, "title": title, "content": content, "body": body, "tags": tags, "status": status,
            "in_core": None if in_core is None else int(bool(in_core)),
            "importance": None if importance is None else float(max(0.0, min(1.0, importance))),
            "confidence": None if confidence is None else float(max(0.0, min(1.0, confidence))),
            "source": source, "session_id": session_id,
        }
        with self._tx():
            row = self._conn.execute("SELECT * FROM nodes WHERE uid=?", (uid,)).fetchone()
            if row is None:
                values = {
                    "subtype": "", "title": "", "content": "", "body": "", "tags": "", "status": "active",
                    "in_core": 0, "importance": 0.5, "confidence": 0.8, "source": "", "session_id": "",
                }
                values.update({k: v for k, v in given.items() if v is not None})
                if not values["title"] and values["content"]:
                    values["title"] = make_title(values["content"])
                created = float(created_at if created_at is not None else now)
                cur = self._conn.execute(
                    "INSERT INTO nodes(uid, kind, subtype, title, content, body, tags, status, in_core, importance,"
                    " confidence, source, session_id, content_hash, created_at, updated_at, meta)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        uid, kind, values["subtype"], values["title"], values["content"], values["body"],
                        values["tags"], values["status"], values["in_core"], values["importance"],
                        values["confidence"], values["source"], values["session_id"],
                        content_hash(values["content"]), created,
                        float(updated_at if updated_at is not None else created),
                        json.dumps(meta or {}, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                node_id = int(cur.lastrowid)
                self._index(node_id, values["title"], values["content"], values["tags"], values["body"])
                if kind == "entity":
                    self._entity_added(node_id, uid, values["title"], meta)
                return node_id, True

            node_id = int(row["id"])
            changes: Dict[str, Any] = {}
            for field in _NODE_FIELDS:
                value = given[field]
                if value is not None and value != row[field]:
                    changes[field] = value
            if "content" in changes:
                changes["content_hash"] = content_hash(changes["content"])
            if created_at is not None and float(created_at) < float(row["created_at"]) - 0.5:
                changes["created_at"] = float(created_at)
            if meta:
                try:
                    merged = json.loads(row["meta"] or "{}")
                except (TypeError, ValueError):
                    merged = {}
                before = json.dumps(merged, sort_keys=True)
                merged.update(meta)
                if json.dumps(merged, sort_keys=True) != before:
                    changes["meta"] = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
            if changes:
                changes["updated_at"] = float(updated_at if updated_at is not None else now)
                sets = ", ".join(f"{k}=?" for k in changes)
                self._conn.execute(f"UPDATE nodes SET {sets} WHERE id=?", (*changes.values(), node_id))
                if row["kind"] == "entity":   # its name, spelling or aliases may have changed: what the index holds
                    self._entity_gen += 1
                if {"title", "content", "tags", "body"} & set(changes):
                    self._index(
                        node_id,
                        changes.get("title", row["title"]),
                        changes.get("content", row["content"]),
                        changes.get("tags", row["tags"]),
                        changes.get("body", row["body"]),
                    )
            return node_id, False

    def get(self, node_id: int) -> Optional[Dict[str, Any]]:
        return self._node(self._q1("SELECT * FROM nodes WHERE id=?", (int(node_id),)))

    def get_by_uid(self, uid: str) -> Optional[Dict[str, Any]]:
        return self._node(self._q1("SELECT * FROM nodes WHERE uid=?", (uid,)))

    def get_many(self, ids: Iterable[int]) -> Dict[int, Dict[str, Any]]:
        ids = [int(i) for i in ids]
        found: Dict[int, Dict[str, Any]] = {}
        for start in range(0, len(ids), 500):   # SQLite refuses an IN list of tens of thousands of variables
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for r in self._q(f"SELECT {_LIGHT} FROM nodes WHERE id IN ({marks})", chunk):
                found[int(r["id"])] = self._node(r)  # type: ignore[assignment]
        return found

    def find_by_hash(self, kind: str, digest: str) -> Optional[Dict[str, Any]]:
        return self._node(
            self._q1("SELECT * FROM nodes WHERE kind=? AND content_hash=? ORDER BY id LIMIT 1", (kind, digest))
        )

    def list_nodes(
        self,
        kind: str,
        *,
        statuses: Optional[Sequence[str]] = None,
        source_prefix: Optional[str] = None,
        limit: int = 100000,
    ) -> List[Dict[str, Any]]:
        sql, params = f"SELECT {_LIGHT} FROM nodes WHERE kind=?", [kind]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        if source_prefix:
            sql += " AND source LIKE ?"
            params.append(source_prefix + "%")
        sql += " ORDER BY id LIMIT ?"
        params.append(_limit(limit))
        return [self._node(r) for r in self._q(sql, params)]  # type: ignore[misc]

    def pinned_memories(self, limit: int = 60) -> List[Dict[str, Any]]:
        """Memories that should stay in view although they are not in
        MEMORY.md / USER.md: what overflowed from those files, and what was
        stored as a standing preference. User-profile items first, newest first."""
        rows = self._q(
            f"SELECT {_LIGHT} FROM nodes WHERE kind='memory' AND status='active' AND in_core=0"
            " AND json_extract(meta, '$.pinned') = 1"
            " ORDER BY (subtype IN ('profile', 'preference')) DESC, updated_at DESC, id DESC LIMIT ?",
            (_limit(limit),),
        )
        return [self._node(r) for r in rows]  # type: ignore[misc]

    def uids(self, kind: str, *, statuses: Optional[Sequence[str]] = None) -> Dict[str, int]:
        """``{uid: id}`` for one kind - the cheap way to ask "what do we have?"."""
        sql, params = "SELECT uid, id FROM nodes WHERE kind=?", [kind]
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params.extend(statuses)
        return {row["uid"]: int(row["id"]) for row in self._q(sql, params)}

    def set_fields(self, node_id: int, **fields: Any) -> bool:
        """Update plain columns of one node by id (re-indexing when text changes)."""
        node = self.get(node_id)
        if node is None:
            return False
        self.upsert_node(node["uid"], node["kind"], **fields)
        return True

    def add_tags(self, node_id: int, names: Sequence[str]) -> None:
        node = self.get(node_id)
        if node is None or not names:
            return
        merged = join_tags(node["tags"], names)
        if merged != node["tags"]:
            self.upsert_node(node["uid"], node["kind"], tags=merged)

    def delete_node(self, node_id: int) -> None:
        node_id = int(node_id)
        with self._tx():
            self._conn.execute("DELETE FROM edges WHERE src=? OR dst=?", (node_id, node_id))
            self._conn.execute("DELETE FROM nodes_fts WHERE rowid=?", (node_id,))
            self._conn.execute("DELETE FROM recalls WHERE node_id=?", (node_id,))
            self._conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))
            self._entity_gen += 1

    def retired_ids(self) -> List[int]:
        """Everything that was forgotten or retired (status ``archived``)."""
        return [int(r[0]) for r in self._q("SELECT id FROM nodes WHERE status='archived' ORDER BY id")]

    def purge(self, node_ids: Iterable[int], *, report: Optional[Dict[str, Any]] = None) -> int:
        """Delete items for good - text, links, index entries, recall log, and the per-session copies of their text
        that the provider keeps in ``meta``. This is the only code path that deletes items; bookkeeping rows (snapshots, events) are trimmed elsewhere.

        Returns how many items were asked for. The work is done a batch of items per transaction, so that other
        processes can write in between, and is followed by ``finish_cleanup``: the first purge of a bank also
        compacts it (older versions left deleted text in free space). When a ``report`` dict is given it is filled
        with ``deleted``, ``clean`` (nothing is left to do), ``left`` (plain-language reasons when something is) and,
        when the clean-up ran, ``index_merged``, ``compacted`` and ``log_emptied``."""
        ids = sorted({int(i) for i in node_ids})
        if not ids:
            if report is not None:
                report.update(deleted=0, clean=True, left=[])
            return 0
        for start in range(0, len(ids), _PURGE_BATCH):
            if start:
                time.sleep(_PURGE_PAUSE)
            batch = ids[start:start + _PURGE_BATCH]
            with self._tx():
                for node_id in batch:
                    self.delete_node(node_id)
                self._forget_text_copies(set(batch))
        self._sweep_orphans()
        with self._tx():
            self._event("purge", [], "", str(len(ids)))
        self.finish_cleanup(report)
        if report is not None:
            report["deleted"] = len(ids)
        return len(ids)

    def _forget_text_copies(self, doomed: Set[int]) -> None:
        """Inside a purge's transaction: drop the per-session copies of node text that the provider saved (``meta``
        rows ``standing:<session>`` = ``{"text": ..., "ids": [...]}``) when they hold an item that is being purged. A
        resumed conversation that lost its copy has none, like one that began before the notes existed; the copies of
        other conversations keep their text and the ids they were already told. Dropping the whole row is safer than
        cutting lines out of rendered text. Also notes that the clean-up is not yet confirmed."""
        for prefix in _TEXT_SNAPSHOTS:
            for row in self._conn.execute("SELECT key, value FROM meta WHERE key LIKE ?", (prefix + "%",)).fetchall():
                try:
                    held = {int(i) for i in json.loads(row["value"]).get("ids", [])}
                except (TypeError, ValueError, AttributeError):
                    held = doomed   # unreadable, so of no use to anyone: it goes
                if held & doomed:
                    self._conn.execute("DELETE FROM meta WHERE key=?", (row["key"],))
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('purge_cleanup_pending', ?)", (repr(time.time()),)
        )

    def _sweep_orphans(self) -> None:
        """A name or topic that was only there because of what was purged is purged with it: a person or
        password-shaped word that nothing links to any more says what was in the deleted text."""
        while True:
            with self._tx():   # asked again inside each transaction: a link made meanwhile keeps its name
                orphans = [int(r[0]) for r in self._conn.execute(
                    "SELECT id FROM nodes WHERE kind IN ('entity', 'topic') "
                    "AND id NOT IN (SELECT src FROM edges) AND id NOT IN (SELECT dst FROM edges) LIMIT ?",
                    (_PURGE_BATCH,))]
                for node_id in orphans:
                    self.delete_node(node_id)
            if len(orphans) < _PURGE_BATCH:
                return
            time.sleep(_PURGE_PAUSE)

    def cleanup_pending(self) -> bool:
        """True when a purge was made here whose clean-up has not been confirmed (see ``finish_cleanup``)."""
        return self.get_meta("purge_cleanup_pending") is not None

    def finish_cleanup(self, report: Optional[Dict[str, Any]] = None) -> bool:
        """Make sure deleted text does not stay in the file: merge the search index (it keeps a deleted item's words
        until its segments are merged), rewrite the file (secure delete alone leaves old index-page text and what older
        versions freed), and fold the write-ahead log into the file and empty it (it holds the old pages). Another process that
        has the bank busy can hold any of these up: nothing is lost then, and ``report`` says what is left - the
        same call finishes it later. Returns True when everything is done; see ``purge`` for the ``report`` keys."""
        left: List[str] = []
        merged = compacted = emptied = True
        if not self._depth:   # an earlier purge may have stopped before its last sweep of names nothing links to any more
            try:
                self._sweep_orphans()
            except sqlite3.Error as exc:
                left.append("the names and topics that only purged text linked to could not be swept (%s)" % exc)
        with self._lock:
            if self._depth:
                merged = compacted = emptied = False
                left.append("the clean-up cannot run inside a transaction")
            else:
                try:   # the words of what was deleted leave every search at once; they leave the file at the next merge
                    self._conn.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('optimize')")
                except sqlite3.Error as exc:
                    merged = False
                    left.append("the search index could not be merged (%s)" % exc)
                if not self.in_memory:
                    compacted = self._compact_once(left)
                if merged and compacted:   # before the last step, so that nothing is written after the log is emptied
                    try:
                        with self._tx():
                            self._conn.execute("DELETE FROM meta WHERE key='purge_cleanup_pending'")
                    except sqlite3.Error:
                        pass   # a note that is left behind only means one more clean-up later
                if not self.in_memory:
                    emptied = self._empty_log(left)
                if left:
                    try:
                        self.set_meta("purge_cleanup_pending", repr(time.time()))
                    except sqlite3.Error:
                        pass
        if report is not None:
            report.update(clean=not left, left=list(left), index_merged=merged, compacted=compacted, log_emptied=emptied)
        return not left

    def _compact_once(self, left: List[str]) -> bool:
        """VACUUM the file on every purge: secure delete overwrites what is freed, but text that earlier updates left
        in the unused gap of an index page, and everything an older version freed, survives it - only rewriting the
        file removes those. (``meta.compacted_at`` records the last time, for information.)"""
        try:   # the copy VACUUM makes stays in memory (temp_store): a plain-text copy of the bank on disk would defeat it
            self._conn.execute("VACUUM")
        except sqlite3.Error as exc:
            left.append("the free space in the file could not be compacted (%s)" % exc)
            return False
        try:
            self.set_meta("compacted_at", repr(time.time()))
        except sqlite3.Error:
            pass   # compacted all the same; the next purge simply does it once more
        return True

    def _empty_log(self, left: List[str]) -> bool:
        """Fold the write-ahead log into the file and empty it. The pragma does not raise when a reader holds it
        up: it waits for the busy timeout and then answers ``(busy, log, checkpointed)`` with ``busy`` not 0 - so that
        is what is read. (A second try straight away would only wait for the same reader again.)"""
        try:
            row = self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error as exc:
            why = str(exc)
        else:
            if row is not None and int(row[0]) == 0:
                return True
            why = "the bank is in use by another program"
        left.append("the log file (%s-wal) could not be emptied: %s" % (self.path.name, why))
        return False

    # -- entities and topics --------------------------------------------------

    def ensure_entity(self, name: str, subtype: str = "", *, created_at: Optional[float] = None) -> Optional[int]:
        """Entity node for ``name`` (created on first sight); None for junk names."""
        key = norm_entity(name)
        if len(key) < 2 or len(key) > 80:
            return None
        node_id, _ = self.upsert_node(
            "e:" + key, "entity", title=name.strip(), created_at=created_at,
            subtype=subtype if subtype and subtype != "known" else None,
        )
        return node_id

    def ensure_topic(self, name: str, *, created_at: Optional[float] = None) -> Optional[int]:
        key = norm_entity(name)
        if not key:
            return None
        node_id, _ = self.upsert_node("t:" + key, "topic", title=name.strip(), created_at=created_at)
        return node_id

    def _entity_key(self) -> Tuple[int, int]:
        """What the cached entity index is valid for: ``data_version`` moves when another connection commits, the
        generation when this one changes or removes an entity (or rolls back) - not on every write."""
        return (int(self._conn.execute("PRAGMA data_version").fetchone()[0]), self._entity_gen)

    def _entity_added(self, node_id: int, uid: str, title: str, meta: Optional[Dict[str, Any]]) -> None:
        """A new entity (inside a transaction): put it in the cached index as it stands, or - when there is none to
        keep right, or the entity brings aliases - let the next ask read the table again. An import that meets new
        names in every conversation would otherwise read every entity once per conversation."""
        cache = self._entity_cache
        if cache is not None and not (meta and meta.get("aliases")) and cache[0] == self._entity_key():
            cache[1][uid[2:]] = (node_id, title)
            cache[2][uid[2:]] = title
        else:
            self._entity_gen += 1

    def _entities(self) -> Tuple[Dict[str, Tuple[int, str]], Dict[str, str]]:
        """The cached ``(index, names)`` themselves - for use under ``self._lock`` and never handed out, because they
        grow in place."""
        version = self._entity_key()
        if self._entity_cache is not None and self._entity_cache[0] == version:
            return self._entity_cache[1], self._entity_cache[2]
        index: Dict[str, Tuple[int, str]] = {}
        for row in self._conn.execute("SELECT id, uid, title, meta FROM nodes WHERE kind='entity'"):
            index[row["uid"][2:]] = (int(row["id"]), row["title"])
            if row["meta"] and row["meta"] != "{}":
                try:
                    for alias in json.loads(row["meta"]).get("aliases", []):
                        index.setdefault(norm_entity(alias), (int(row["id"]), row["title"]))
                except (TypeError, ValueError):
                    pass
        names = {key: value[1] for key, value in index.items()}
        self._entity_cache = (version, index, names)
        return index, names

    def entity_index(self) -> Dict[str, Tuple[int, str]]:
        """``{normalised name: (id, display name)}`` for every entity: a copy of an index that is kept until an entity
        changes (here, or in another connection), not rebuilt after every write."""
        with self._lock:
            return dict(self._entities()[0])

    def known_entity_names(self) -> Dict[str, str]:
        """``{normalised name: display name}``, a copy of the same kept index (a copy is a fraction of building one)."""
        with self._lock:
            return dict(self._entities()[1])

    # -- edges --------------------------------------------------------------

    def link(self, src: int, dst: int, rel: str, weight: float = 1.0, *, mode: str = "max") -> None:
        """Create or strengthen an edge. ``mode``: ``max`` | ``add`` | ``set``."""
        if src == dst:
            return
        now = time.time()
        update = {
            "max": "weight = MAX(weight, excluded.weight)",
            "add": "weight = weight + excluded.weight",
            "set": "weight = excluded.weight",
        }[mode]
        with self._tx():
            self._conn.execute(
                "INSERT INTO edges(src, dst, rel, weight, created_at, updated_at) VALUES(?,?,?,?,?,?)"
                f" ON CONFLICT(src, dst, rel) DO UPDATE SET {update}, updated_at = excluded.updated_at",
                (int(src), int(dst), rel, float(weight), now, now),
            )

    def unlink(self, src: int, dst: int, rel: Optional[str] = None) -> None:
        with self._tx():
            if rel is None:
                self._conn.execute("DELETE FROM edges WHERE src=? AND dst=?", (int(src), int(dst)))
            else:
                self._conn.execute("DELETE FROM edges WHERE src=? AND dst=? AND rel=?", (int(src), int(dst), rel))

    def neighbors(self, node_id: int, *, limit: int = 60) -> List[Dict[str, Any]]:
        """Nodes one edge away, strongest first."""
        rows = self._q(
            "SELECT n.id, n.kind, n.subtype, n.title, n.status, e.rel, e.weight, 'out' AS dir"
            " FROM edges e JOIN nodes n ON n.id = e.dst WHERE e.src=?"
            " UNION ALL "
            "SELECT n.id, n.kind, n.subtype, n.title, n.status, e.rel, e.weight, 'in' AS dir"
            " FROM edges e JOIN nodes n ON n.id = e.src WHERE e.dst=?"
            " ORDER BY weight DESC LIMIT ?",
            (int(node_id), int(node_id), _limit(limit)),
        )
        return [dict(r) for r in rows]

    def degree(self, node_id: int) -> int:
        row = self._q1("SELECT COUNT(*) AS c FROM edges WHERE src=? OR dst=?", (int(node_id), int(node_id)))
        return int(row["c"]) if row else 0

    # -- full-text ----------------------------------------------------------

    @staticmethod
    def _match(terms: Sequence[str]) -> str:
        return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)

    def fts_total(self) -> int:
        row = self._q1("SELECT COUNT(*) AS c FROM nodes")
        return int(row["c"]) if row else 0

    def fts_df(self, term: str) -> int:
        row = self._q1("SELECT COUNT(*) AS c FROM nodes_fts WHERE nodes_fts MATCH ?", (self._match([term]),))
        return int(row["c"]) if row else 0

    def fts_search(
        self,
        terms: Sequence[str],
        kinds: Sequence[str],
        *,
        limit: int = 40,
        statuses: Sequence[str] = RECALLABLE,
        include_core: bool = False,
    ) -> List[Tuple[int, float]]:
        """``[(node_id, bm25), ...]`` best first (bm25 is negative; lower is better)."""
        if not terms or not kinds:
            return []
        sql = (
            f"SELECT n.id AS id, {_BM25} AS rank"
            " FROM nodes_fts JOIN nodes n ON n.id = nodes_fts.rowid"
            f" WHERE nodes_fts MATCH ? AND n.kind IN ({','.join('?' * len(kinds))})"
            f" AND n.status IN ({','.join('?' * len(statuses))})"
        )
        if not include_core:
            sql += " AND n.in_core = 0"
        sql += " ORDER BY rank LIMIT ?"
        rows = self._q(sql, (self._match(terms), *kinds, *statuses, _limit(limit)))
        return [(int(r["id"]), float(r["rank"])) for r in rows]

    def fts_hits(self, term: str, ids: Iterable[int], *, columns: Optional[Sequence[str]] = None) -> Set[int]:
        """Which of ``ids`` contain ``term`` (using the index's own stemming),
        optionally only in the given columns (``title``, ``content``, ``tags``)."""
        ids = [int(i) for i in ids]
        if not ids:
            return set()
        match = self._match([term])
        if columns:
            match = "{" + " ".join(columns) + "} : " + match
        marks = ",".join("?" * len(ids))
        rows = self._q(
            f"SELECT rowid FROM nodes_fts WHERE nodes_fts MATCH ? AND rowid IN ({marks})", (match, *ids)
        )
        return {int(r[0]) for r in rows}

    # -- recall log and events ------------------------------------------------

    def record_recall(self, session_id: str, scored: Sequence[Tuple[int, float]]) -> None:
        if not scored:
            return
        now = time.time()
        with self._tx():
            self._conn.executemany(
                "INSERT INTO recalls(ts, session_id, node_id, score) VALUES(?,?,?,?)",
                [(now, session_id or "", int(i), float(s)) for i, s in scored],
            )
            self._conn.executemany(
                "UPDATE nodes SET recall_count = recall_count + 1, last_recalled = ? WHERE id = ?",
                [(now, int(i)) for i, _ in scored],
            )
            self._event("recall", [int(i) for i, _ in scored], session_id)

    def recalled_ids(self, session_id: str) -> Set[int]:
        if not session_id:
            return set()
        rows = self._q("SELECT DISTINCT node_id FROM recalls WHERE session_id=?", (session_id,))
        return {int(r[0]) for r in rows}

    def forget_session_recalls(self, session_id: str) -> None:
        """Let a session be re-told what it was told before (after compression)."""
        if not session_id:
            return
        with self._tx():
            self._conn.execute(
                "UPDATE recalls SET session_id = session_id || '#old' WHERE session_id=?", (session_id,)
            )

    def keep_session_recalls(self, session_id: str, keep: Iterable[int]) -> int:
        """Bring a session's told-list in line with what its transcript still
        shows: recalls of items not in ``keep`` stop counting as told. Returns
        how many items were released (no write at all when there are none)."""
        if not session_id:
            return 0
        wanted = {int(i) for i in keep}
        drop = sorted(i for i in self.recalled_ids(session_id) if i not in wanted)
        if not drop:
            return 0
        with self._tx():
            for start in range(0, len(drop), 400):
                chunk = drop[start:start + 400]
                self._conn.execute(
                    "UPDATE recalls SET session_id = session_id || '#old' WHERE session_id=?"
                    f" AND node_id IN ({','.join('?' * len(chunk))})",
                    (session_id, *chunk),
                )
        return len(drop)

    def _event(self, kind: str, nodes: Sequence[int], session_id: str = "", info: str = "") -> None:
        self._conn.execute(
            "INSERT INTO events(ts, type, session_id, nodes, info) VALUES(?,?,?,?,?)",
            (time.time(), kind, session_id or "", json.dumps(list(nodes)), info or ""),
        )

    def add_event(self, kind: str, nodes: Sequence[int], session_id: str = "", info: str = "") -> None:
        with self._tx():
            self._event(kind, nodes, session_id, info)
            row = self._conn.execute("SELECT MAX(id) AS m, MIN(id) AS n FROM events").fetchone()
            if row["m"] and row["m"] - row["n"] > _EVENT_KEEP * 1.2:
                self._conn.execute("DELETE FROM events WHERE id < ?", (row["m"] - _EVENT_KEEP,))

    def events_since(self, after_id: int = 0, *, limit: int = 100) -> List[Dict[str, Any]]:
        if after_id <= 0:
            rows = self._q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (_limit(limit),))
            rows = list(reversed(rows))
        else:
            rows = self._q("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (int(after_id), _limit(limit)))
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["nodes"] = json.loads(item["nodes"])
            except (TypeError, ValueError):
                item["nodes"] = []
            out.append(item)
        return out

    def last_event_id(self) -> int:
        row = self._q1("SELECT MAX(id) AS m FROM events")
        return int(row["m"] or 0) if row else 0

    # -- summaries ----------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        kinds: Dict[str, int] = {}
        for row in self._q("SELECT kind, COUNT(*) AS c FROM nodes WHERE status != 'archived' GROUP BY kind"):
            kinds[row["kind"]] = int(row["c"])
        status: Dict[str, int] = {}
        for row in self._q("SELECT status, COUNT(*) AS c FROM nodes WHERE kind='memory' GROUP BY status"):
            status[row["status"]] = int(row["c"])
        edges = self._q1("SELECT COUNT(*) AS c FROM edges")
        recalls = self._q1("SELECT COUNT(*) AS c, MAX(ts) AS last FROM recalls")
        core = self._q1("SELECT COUNT(*) AS c FROM nodes WHERE kind='memory' AND in_core=1 AND status='active'")
        overflow = self._q1(
            "SELECT COUNT(*) AS c FROM nodes WHERE kind='memory' AND source LIKE 'overflow:%' AND status != 'archived'"
        )
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {
            "nodes": kinds,
            "memory_status": status,
            "in_core": int(core["c"]) if core else 0,
            "overflow": int(overflow["c"]) if overflow else 0,
            "edges": int(edges["c"]) if edges else 0,
            "recalls": int(recalls["c"]) if recalls else 0,
            "last_recall": float(recalls["last"] or 0) if recalls else 0.0,
            "db_bytes": size,
            "path": str(self.path),
        }

    def export_graph(
        self,
        *,
        kinds: Optional[Sequence[str]] = None,
        limit: int = 4000,
        min_entity_degree: int = 2,
        include_archived: bool = False,
    ) -> Tuple[List[Dict[str, Any]], List[Tuple[int, int, str, float]]]:
        """Nodes and edges for the network view.

        Entities that connect fewer than ``min_entity_degree`` things are left
        out (they add clutter, not structure). When the bank outgrows ``limit``
        the most connected, most important and most recent nodes are kept.
        """
        sql = (
            "SELECT id, uid, kind, subtype, title, status, in_core, importance, created_at, updated_at,"
            " recall_count, last_recalled, source FROM nodes"
        )
        where, params = [], []
        if not include_archived:
            where.append("status != 'archived'")
        if kinds:
            where.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if where:
            sql += " WHERE " + " AND ".join(where)
        nodes = {int(r["id"]): dict(r) for r in self._q(sql, params)}
        edges = [
            (int(r["src"]), int(r["dst"]), r["rel"], float(r["weight"]))
            for r in self._q("SELECT src, dst, rel, weight FROM edges")
            if int(r["src"]) in nodes and int(r["dst"]) in nodes
        ]
        degree: Dict[int, int] = {}
        for src, dst, _, _ in edges:
            degree[src] = degree.get(src, 0) + 1
            degree[dst] = degree.get(dst, 0) + 1
        drop = {
            node_id for node_id, node in nodes.items()
            if node["kind"] in ("entity", "topic") and degree.get(node_id, 0) < min_entity_degree
        }
        if drop:
            nodes = {k: v for k, v in nodes.items() if k not in drop}
            edges = [e for e in edges if e[0] not in drop and e[1] not in drop]
            degree = {}
            for src, dst, _, _ in edges:
                degree[src] = degree.get(src, 0) + 1
                degree[dst] = degree.get(dst, 0) + 1
        if len(nodes) > limit:
            now = time.time()

            def rank(node: Dict[str, Any]) -> float:
                age_days = max(0.0, (now - float(node["created_at"])) / 86400.0)
                kind_bonus = {"memory": 3.0, "skill": 1.0, "session": 0.5}.get(node["kind"], 0.0)
                return kind_bonus + degree.get(int(node["id"]), 0) * 0.25 + float(node["importance"]) * 2.0 \
                    + 1.0 / (1.0 + age_days / 30.0)

            keep = {int(n["id"]) for n in sorted(nodes.values(), key=rank, reverse=True)[:limit]}
            nodes = {k: v for k, v in nodes.items() if k in keep}
            edges = [e for e in edges if e[0] in keep and e[1] in keep]
            degree = {}
            for src, dst, _, _ in edges:
                degree[src] = degree.get(src, 0) + 1
                degree[dst] = degree.get(dst, 0) + 1
        out = []
        for node_id, node in nodes.items():
            node["degree"] = degree.get(node_id, 0)
            out.append(node)
        out.sort(key=lambda n: n["id"])
        return out, edges
