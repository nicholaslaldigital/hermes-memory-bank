"""Tests for what the 1.3.0 review found in the store, purge, command line and settings: any Python 3.9+, no Hermes.

    python tests/test_store_review.py

Each test below failed on the tree the review was made against. Nothing here depends on how fast the machine is: where
the review measured seconds, these count statements, query plans and lock hand-overs instead.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_core  # noqa: E402  (loads the plugin's core package, as every test file here does)

core = test_core.core
ingest = test_core.ingest
bank_cli = test_core.bank_cli
make_home = test_core.make_home
open_store = test_core.open_store
ROOT = test_core.ROOT
store_mod = core.store
core.config.logger.addHandler(logging.NullHandler())   # the tests below feed load_config bad files on purpose: no stderr noise

TESTS = []


class Skip(Exception):
    """Raised by a test whose scenario cannot be built on this machine."""


def test(fn):
    TESTS.append(fn)
    return fn


def need_wal(path: Path) -> None:
    """A reader in the middle of a query holds up emptying the write-ahead log, and a bank with no such log (a SQLite
    with the WAL-reset bug keeps new banks in a rollback journal) has no log to empty and no such reader."""
    probe = core.Store(path, timeout=0.05)
    mode = str(probe._conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    probe.close()
    if mode != "wal":
        raise Skip("this SQLite keeps new banks out of WAL (journal mode %r): there is no log file to hold up" % mode)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def everything(path: Path) -> bytes:
    """What is on disk for a bank: the database and its write-ahead log."""
    return b"".join(Path(str(path) + suffix).read_bytes() for suffix in ("", "-wal") if Path(str(path) + suffix).exists())


def wal_size(path: Path) -> int:
    wal = Path(str(path) + "-wal")
    return wal.stat().st_size if wal.exists() else 0


def capture(fn, *args, **kwargs):
    """Run a command-line function with the real ``_out``; returns (result, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    saved = bank_cli._out
    bank_cli._out = test_core._REAL_OUT
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = fn(*args, **kwargs)
    finally:
        bank_cli._out = saved
    return result, out.getvalue(), err.getvalue()


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __getattr__(self, name):   # an option nobody gave
        return None


@contextlib.contextmanager
def no_pause():
    """Retries and hand-overs inside the store wait a moment on purpose; a test has no one to wait for."""
    saved = {name: getattr(store_mod, name) for name in ("_PURGE_PAUSE",) if hasattr(store_mod, name)}
    for name in saved:
        setattr(store_mod, name, 0.0)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(store_mod, name, value)


@contextlib.contextmanager
def quick_store():
    """The command line opens its own Store; give it a short busy timeout so a blocked bank is found out quickly."""
    class Quick(core.Store):
        def __init__(self, path, **kw):
            kw.setdefault("timeout", 0.05)
            super().__init__(path, **kw)

    saved = bank_cli.Store
    bank_cli.Store = Quick
    try:
        with no_pause():
            yield
    finally:
        bank_cli.Store = saved


@contextlib.contextmanager
def trace(store, sink):
    """Hand every statement the store's connection starts to ``sink(sql)``."""
    store._conn.set_trace_callback(sink)
    try:
        yield
    finally:
        store._conn.set_trace_callback(None)


# ---------------------------------------------------------------------------
# purge: the copies of node text, bounded transactions, the index, an honest report (engine E1, E3, E7, E10)
# ---------------------------------------------------------------------------

@test
def purge_removes_the_per_session_copies_of_pinned_text(tmp):
    home = make_home(tmp)
    path = core.db_path(home)
    store = open_store(home)
    secret = "Rotate the Marrowgate vault key at dawn, code word quokkaxyz"
    pinned = ingest.remember(store, secret, subtype="preference", meta={"pinned": True}, dedupe=False)["id"]
    kept = ingest.remember(store, "Standing preference about the badge printer ribbon", subtype="preference",
                           meta={"pinned": True}, dedupe=False)["id"]
    text, ids = core.standing_notes(store, 1200)
    assert pinned in ids and kept in ids and "quokkaxyz" in text, text
    earlier = json.dumps({"text": "- #%d Standing preference about the badge printer ribbon" % kept, "ids": [kept]},
                         separators=(",", ":"))       # a conversation that began before the secret was saved
    for session in ("session-B", "session-C"):         # ... and two that began after: what the provider saves at a start
        store.put_snapshot("standing:" + session, json.dumps({"text": text, "ids": ids}, separators=(",", ":")))
    store.put_snapshot("standing:session-A", earlier)
    store.put_snapshot("standing:session-broken", "{ not json")
    store.put_snapshot("other:x", "not a copy of anything")
    store.set_meta("sessions_watermark", "123.5")
    assert b"quokkaxyz" in everything(path)

    assert store.purge([pinned]) == 1
    assert store.get_meta("standing:session-B") is None and store.get_meta("standing:session-C") is None, \
        "a resumed conversation would be handed the purged text again"
    assert store.get_meta("standing:session-broken") is None, "a copy nobody can read says nothing worth keeping"
    assert store.get_meta("standing:session-A") == earlier, "a conversation that never held the item keeps what it was told"
    assert store.get_meta("other:x") == "not a copy of anything" and store.get_meta("sessions_watermark") == "123.5"
    blob = everything(path).lower()
    assert b"quokkaxyz" not in blob and b"marrowgate" not in blob, "the text is still in the database or its log"
    assert b"badge printer ribbon" in blob, "what was not purged stays"
    store.close()
    blob = everything(path).lower()
    assert b"quokkaxyz" not in blob and b"marrowgate" not in blob


@test
def deleting_an_item_finds_its_recall_rows_by_index(tmp):
    store = core.Store(tmp / "e3.db")
    node = store.upsert_node("m:1", "memory", content="a note")[0]
    store.record_recall("s1", [(node, 1.0)])
    seen = []
    with trace(store, seen.append):
        store.delete_node(node)
    stmt = [sql for sql in seen if sql.startswith("DELETE FROM recalls")]
    assert stmt, seen
    plan = " | ".join(str(row[3]) for row in store._conn.execute("EXPLAIN QUERY PLAN " + stmt[0]))
    assert "recalls_node" in plan and "SCAN" not in plan.upper().replace("SEARCH", ""), \
        "the recall log was read from end to end for every deleted item: " + plan
    store.close()


@test
def purge_holds_the_lock_for_a_few_hundred_items_at_a_time(tmp):
    path = tmp / "e3.db"
    store = core.Store(path)
    ids = []
    with store.transaction():
        for i in range(700):
            node = store.upsert_node(f"m:{i}", "memory", content=f"Bulk note {i} about rack {i}")[0]
            ids.append(node)
            entity = store.ensure_entity(f"Zephyrine Rack {i}")
            store.link(node, entity, "mentions")
    store.record_recall("s1", [(i, 0.5) for i in ids])
    other = sqlite3.connect(str(path), timeout=0, isolation_level=None)   # another process that will not wait
    began, writes, per_transaction, current = [0], [], [], [0]

    def watch(sql):
        if sql.startswith("BEGIN IMMEDIATE"):
            began[0] += 1
            current[0] = 0
            if began[0] > 1:   # the previous transaction has committed: somebody else tries to write right now
                try:
                    other.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, 'x')", ("between-%d" % began[0],))
                    writes.append(True)
                except sqlite3.OperationalError:
                    writes.append(False)
        elif sql.startswith("DELETE FROM nodes WHERE id="):
            current[0] += 1
            per_transaction.append(current[0])

    with no_pause(), trace(store, watch):
        assert store.purge(ids) == 700
    other.close()
    assert max(per_transaction) <= 400, "one transaction deleted %d items: nobody else can write meanwhile" % max(per_transaction)
    assert len(writes) >= 3 and all(writes), "a second writer was shut out between the batches: %r" % (writes,)
    assert store._conn.execute("SELECT COUNT(*) FROM nodes WHERE kind IN ('memory', 'entity')").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM recalls").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'between-%'").fetchone()[0] == len(writes), \
        "what the other writer saved between the batches is kept"
    store.close()


@test
def purging_thousands_of_items_does_not_rescan_the_recall_log(tmp):
    store = core.Store(tmp / "e3.db")
    ids = []
    with store.transaction():
        for i in range(1500):
            ids.append(store.upsert_node(f"m:{i}", "memory", content=f"note {i}")[0])
        store._conn.executemany("INSERT INTO recalls(ts, session_id, node_id, score) VALUES(1.0, ?, ?, 0.5)",
                                [("s%d" % (n % 50), ids[n % len(ids)]) for n in range(30000)])
    steps = [0]
    store._conn.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 1000)   # one tick per 1000 VM steps
    try:
        assert store.purge(ids) == 1500
    finally:
        store._conn.set_progress_handler(None, 0)
    # a scan of 30,000 rows for each of 1,500 items is tens of millions of steps (about 68,000 ticks); looking each one up
    # costs about 530 ticks
    assert steps[0] < 6000, "purge did %d thousand VM steps: it is scanning the recall log again" % steps[0]
    assert store._conn.execute("SELECT COUNT(*) FROM recalls").fetchone()[0] == 0
    store.close()


@test
def a_purge_compacts_text_that_older_versions_left_in_free_space(tmp):
    path = tmp / "old.db"
    store = core.Store(path)
    old_secret = "Quillbrook-escrow-passphrase-zxcvbn77"
    old = store.upsert_node("m:old", "memory", content=f"The vault passphrase is {old_secret} and nobody may tell Janet Quillan")[0]
    for i in range(40):
        store.upsert_node(f"m:f{i}", "memory", content=f"Filler note {i} about the badge printer ribbon on floor {i}")
    keep = store.upsert_node("m:keep", "memory", content="Keep this note about the Contoso router")[0]
    victim = store.upsert_node("m:victim", "memory", content="Throw away this note about the loaner laptop")[0]
    store.close()
    # What a 1.2.0 purge did: delete without overwriting (secure_delete was not switched on).
    raw = sqlite3.connect(str(path), isolation_level=None)
    raw.execute("PRAGMA secure_delete=OFF")
    raw.execute("DELETE FROM nodes_fts WHERE rowid=?", (old,))
    raw.execute("DELETE FROM nodes WHERE id=?", (old,))
    raw.close()
    assert old_secret.encode() in everything(path), "the scenario did not leave the old text in the file"

    store = core.Store(path)
    assert store.get_meta("compacted_at") is None
    report = {}
    assert store.purge([victim], report=report) == 1
    assert report["clean"] and report["compacted"], report
    assert store.get_meta("compacted_at"), "the bank is not marked as compacted"
    assert old_secret.encode() not in everything(path), "the first purge left an older version's deleted text in the file"
    assert [h.id for h in core.RecallEngine(store, {}).search("Contoso router", gate=False)] == [keep], "search works after it"
    seen = []
    with trace(store, seen.append):
        store.purge([keep])
    assert any(sql.startswith("VACUUM") for sql in seen), "every purge rewrites the file: index pages keep text secure delete misses"
    store.close()


@test
def purge_runs_its_clean_up_under_the_store_lock(tmp):
    store = core.Store(tmp / "lock.db")
    node = store.upsert_node("m:1", "memory", content="note one")[0]
    owned = {}

    def watch(sql):
        for name in ("INSERT INTO nodes_fts(nodes_fts)", "VACUUM", "PRAGMA wal_checkpoint"):
            if sql.startswith(name):
                owned.setdefault(name, []).append(store._lock._is_owned())

    with trace(store, watch):
        store.purge([node])
    assert set(owned) == {"INSERT INTO nodes_fts(nodes_fts)", "VACUUM", "PRAGMA wal_checkpoint"}, owned
    assert all(all(flags) for flags in owned.values()), "another thread of this process could run in between: %r" % owned
    store.close()


def hold_a_read(path: Path):
    """A second connection in the middle of a read: while it lasts, the write-ahead log cannot be emptied."""
    reader = sqlite3.connect(str(path), isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM nodes").fetchall()
    return reader


@test
def purge_says_when_the_log_could_not_be_emptied(tmp):
    path = tmp / "busy.db"
    need_wal(path)
    store = core.Store(path, timeout=0.05)
    secret = "Tr0ub4dor-hunter2-quokkaxyz"
    node = store.upsert_node("m:1", "memory", content=f"The staging password is {secret}")[0]
    store.upsert_node("m:2", "memory", content="An unrelated note about the badge printer")
    reader = hold_a_read(path)
    try:
        report = {}
        attempts = []
        with no_pause(), trace(store, lambda sql: attempts.append(sql) if sql.startswith("PRAGMA wal_checkpoint") else None):
            assert store.purge([node], report=report) == 1, "the purge itself still returns how many items it deleted"
        assert len(attempts) == 1, "a second try straight away would only wait for the same reader: %r" % (attempts,)
        assert report["clean"] is False and report["log_emptied"] is False, report
        assert any("log file" in line for line in report["left"]), report
        assert store.cleanup_pending(), "nothing remembers that the clean-up is unfinished"
        assert store.get(node) is None, "the item is deleted all the same"
        assert secret.encode() in everything(path), "the log was not emptied while a reader held it, so the text is still in it"

        # the reader lets go: the same call finishes the job, and the log is empty
        reader.execute("COMMIT")
        assert store.finish_cleanup(report) is True
        assert report["clean"] and report["log_emptied"] and report["left"] == [], report
        assert not store.cleanup_pending()
        assert wal_size(path) == 0 and secret.encode() not in everything(path), "the text is still in the log file"
    finally:
        with contextlib.suppress(sqlite3.Error):
            reader.close()
        store.close()


class _Failing:
    """A connection that answers like the real one except that statements starting with ``prefix`` fail."""

    def __init__(self, real, prefix, error):
        self._real, self._prefix, self._error = real, prefix, error

    def execute(self, sql, *args):
        if sql.strip().startswith(self._prefix):
            raise sqlite3.OperationalError(self._error)
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


@test
def every_clean_up_step_that_fails_is_reported(tmp):
    cases = [
        ("INSERT INTO nodes_fts(nodes_fts)", "database or disk is full", "the search index could not be merged",
         {"index_merged": False, "compacted": True, "log_emptied": True}),
        ("VACUUM", "database or disk is full", "the free space in the file could not be compacted",
         {"index_merged": True, "compacted": False, "log_emptied": True}),
        ("PRAGMA wal_checkpoint", "database table is locked", "the log file (clean.db-wal) could not be emptied: database table is locked",
         {"index_merged": True, "compacted": True, "log_emptied": False}),
    ]
    for prefix, error, said, flags in cases:
        store = core.Store(tmp / "clean.db")
        node = store.upsert_node("m:%s" % prefix[:4], "memory", content="a note to purge")[0]
        real = store._conn
        store._conn = _Failing(real, prefix, error)
        report = {}
        try:
            assert store.purge([node], report=report) == 1
        finally:
            store._conn = real
        assert report["clean"] is False and any(said in line for line in report["left"]), (prefix, report)
        assert error in " ".join(report["left"]), report
        assert {k: report[k] for k in flags} == flags, (prefix, report)
        assert store.cleanup_pending(), prefix + ": a clean-up that failed must stay on the list"
        assert store.get(node) is None
        assert store.finish_cleanup(report) is True and report["clean"] and not store.cleanup_pending(), (prefix, report)
        store.close()
        (tmp / "clean.db").unlink()
        for suffix in ("-wal", "-shm"):
            if Path(str(tmp / "clean.db") + suffix).exists():
                Path(str(tmp / "clean.db") + suffix).unlink()


@test
def purge_with_nobody_in_the_way_reports_clean(tmp):
    path = tmp / "idle.db"
    store = core.Store(path)
    node = store.upsert_node("m:1", "memory", content="a note nobody else is reading")[0]
    report = {}
    assert store.purge([node], report=report) == 1
    assert report["clean"] is True and report["left"] == [] and report["log_emptied"] and report["index_merged"], report
    assert report["deleted"] == 1 and not store.cleanup_pending()
    assert wal_size(path) == 0
    assert store.purge([], report=report) == 0 and report["deleted"] == 0 and report["clean"], report
    assert store.purge([node]) == 1, "purging what is not there is not an error, and report is optional"
    store.close()


@test
def a_purge_that_was_cut_short_is_remembered_and_can_be_finished(tmp):
    path = tmp / "cut.db"
    store = core.Store(path)
    secret = "Rotate the Marrowgate vault key, code word quokkaxyz"
    node = store.upsert_node("m:1", "memory", content=secret)[0]
    other = store.upsert_node("m:2", "memory", content="An unrelated note about the badge printer ribbon")[0]
    saved = store.finish_cleanup

    def killed(report=None):
        raise RuntimeError("the process was stopped before the clean-up")

    store.finish_cleanup = killed
    try:
        store.purge([node])
        raise AssertionError("the stand-in did not stop the purge")
    except RuntimeError:
        pass
    store.finish_cleanup = saved
    assert store.get(node) is None and store.get(other) is not None, "the batches that were done stay done"
    assert store.cleanup_pending(), "nothing remembers that the clean-up never happened"
    report = {}
    assert store.finish_cleanup(report) is True and report["clean"] and not store.cleanup_pending()
    assert b"quokkaxyz" not in everything(path)
    store.close()


@test
def a_purge_that_stops_half_way_says_so_and_can_be_run_again(tmp):
    home = make_home(tmp)
    store = open_store(home)
    ids = [ingest.remember(store, f"Scratch note number {i} about the Contoso printer", dedupe=False)["id"] for i in range(3)]
    store.close()
    real = core.Store.purge

    def stops(self, node_ids, *, report=None):
        raise sqlite3.OperationalError("database is locked")

    core.Store.purge = stops
    try:
        code, out, err = capture(bank_cli.cmd_purge, home, Args(ids=ids, yes=True))
    finally:
        core.Store.purge = real
    assert code == 1 and "stopped before the end: database is locked" in out and "run the same command again" in out.lower(), out
    code, out, err = capture(bank_cli.cmd_purge, home, Args(ids=ids, yes=True))      # the same command, now with a free bank
    assert code == 0 and "deleted 3 item(s) from the bank for good" in out, out


@test
def the_command_line_tells_the_truth_about_a_purge_it_could_not_finish(tmp):
    home = make_home(tmp)
    path = core.db_path(home)
    need_wal(path)
    store = open_store(home)
    ingest.seed(store, home, sessions=False, skills=False)
    note = ingest.remember(store, "Scratch note about the Contoso conference badge printer", dedupe=False)["id"]
    ingest.forget(store, note)
    store.close()
    reader = hold_a_read(path)
    try:
        with quick_store():
            code, out, err = capture(bank_cli.cmd_purge, home, Args(ids=[note], yes=True))
        assert code == 1, (code, out)
        assert "deleted 1 item" in out and "for good" not in out, "it must not claim more than it did:\n" + out
        assert "log file" in out and "purge --yes" in out and "close Hermes" in out, out
        assert "rewrites" in out, "a purge says it also rewrites the file:\n" + out
        reader.execute("COMMIT")
        # nothing is left to delete, but an earlier purge did not finish: --yes finishes it, and says so
        with quick_store():
            code, out, err = capture(bank_cli.cmd_purge, home, Args(ids=[note], yes=True))
        assert code == 0 and "finished" in out and "log file is empty" in out, (code, out)
        check = core.Store(path)
        assert not check.cleanup_pending()
        check.close()
        # and when there is nothing to finish, asking for nothing is still refused
        with quick_store():
            code, out, err = capture(bank_cli.cmd_purge, home, Args(yes=True))
        assert code == 2 and "nothing chosen" in out, (code, out)
        # a clean purge keeps its old wording
        store = open_store(home)
        second = ingest.remember(store, "Another scratch note about the Contoso stapler", dedupe=False)["id"]
        store.close()
        with quick_store():
            code, out, err = capture(bank_cli.cmd_purge, home, Args(ids=[second], yes=True))
        assert code == 0 and "deleted 1 item(s) from the bank for good" in out and "rewrites" in out, out
        assert "Hermes keeps its own copies" in out and "state.db" in out, "what purge cannot reach is said:\n" + out
    finally:
        with contextlib.suppress(sqlite3.Error):
            reader.close()


# ---------------------------------------------------------------------------
# the entity index (engine E4)
# ---------------------------------------------------------------------------

ENTITY_SELECT = "SELECT id, uid, title, meta FROM nodes WHERE kind='entity'"


@test
def unrelated_writes_do_not_rebuild_the_entity_index(tmp):
    store = core.Store(tmp / "e4.db")
    for name in ("Janet Quillan", "Brightwave", "Sandy Vexor"):
        store.ensure_entity(name)
    rebuilds = []
    with trace(store, lambda sql: rebuilds.append(sql) if sql.startswith(ENTITY_SELECT) else None):
        names = store.known_entity_names()
        assert set(names) == {"janet quillan", "brightwave", "sandy vexor"}, names
        for i in range(30):                     # what an import does for every conversation
            store.set_meta("k%d" % i, i)
            store.upsert_node("m:%d" % i, "memory", content="an unrelated note %d" % i)
            store.record_recall("s1", [(1, 0.5)])
            store.add_event("note", [])
            store.known_entity_names()
        assert len(rebuilds) == 1, "the whole entity table was read %d times for 30 writes that touch no entity" % len(rebuilds)

        # ...but a change to an entity shows at once: a new one without reading the table again, a changed or deleted
        # one by reading it once more
        store.ensure_entity("Contoso")
        assert "contoso" in store.known_entity_names() and len(rebuilds) == 1, len(rebuilds)
        store.ensure_entity("Contoso")             # nothing changed
        store.known_entity_names()
        assert len(rebuilds) == 1
        store.upsert_node("e:contoso", "entity", title="Contoso Ltd", meta={"aliases": ["Contoso Limited"]})
        index = store.entity_index()
        assert index["contoso"][1] == "Contoso Ltd" and "contoso limited" in index and len(rebuilds) == 2, (index, len(rebuilds))
        gone = index["brightwave"][0]
        store.delete_node(gone)
        assert "brightwave" not in store.known_entity_names() and len(rebuilds) == 3
    store.close()


@test
def an_import_that_meets_new_names_in_every_conversation_reads_the_entity_table_once(tmp):
    path = tmp / "e4.db"
    store = core.Store(path)
    store.ensure_entity("Janet Quillan")
    rebuilds = []
    with trace(store, lambda sql: rebuilds.append(sql) if sql.startswith(ENTITY_SELECT) else None):
        store.known_entity_names()
        for i in range(60):                          # one conversation after the other, each with names nobody has met
            node = store.upsert_node("s:%d" % i, "session", title="conversation %d" % i)[0]
            with store.transaction():
                known = store.known_entity_names()
                for name in ("Zephyrine Quillbrook %d" % i, "Brightwave Rack %d" % i):
                    entity = store.ensure_entity(name)
                    assert entity is not None
                    store.link(node, entity, "mentions")
                assert "brightwave rack %d" % i in store.known_entity_names() and "brightwave rack %d" % i not in known
        assert len(rebuilds) == 1, "the entity table was read %d times for 60 conversations" % len(rebuilds)
    # what was kept in step is exactly what a fresh read of the table gives
    other = core.Store(path)
    assert store.entity_index() == other.entity_index() and store.known_entity_names() == other.known_entity_names()
    other.close()
    store.close()


@test
def the_entity_index_handed_out_is_a_copy(tmp):
    store = core.Store(tmp / "e4.db")
    store.ensure_entity("Janet Quillan")
    handed = store.entity_index()
    handed["intruder"] = (999, "Intruder")
    names = store.known_entity_names()
    names["another"] = "Another"
    assert "intruder" not in store.entity_index() and "another" not in store.known_entity_names()
    store.ensure_entity("Brightwave")
    assert "brightwave" not in handed, "an index somebody holds does not change under them"
    store.close()


@test
def a_rolled_back_entity_does_not_stay_in_the_index(tmp):
    store = core.Store(tmp / "e4.db")
    store.ensure_entity("Janet Quillan")
    try:
        with store.transaction():
            store.ensure_entity("Rolled Back")
            assert "rolled back" in store.known_entity_names()
            raise RuntimeError("the import stopped half-way")
    except RuntimeError:
        pass
    assert "rolled back" not in store.known_entity_names(), "an entity that was never saved is still offered"
    store.close()


@test
def another_connections_entities_show_up(tmp):
    path = tmp / "e4.db"
    mine, theirs = core.Store(path), core.Store(path)
    assert "contoso" not in mine.known_entity_names()
    theirs.ensure_entity("Contoso")
    assert "contoso" in mine.known_entity_names(), "a second process added an entity and this one does not see it"
    theirs.upsert_node("e:contoso", "entity", title="Contoso Ltd")
    assert mine.entity_index()["contoso"][1] == "Contoso Ltd"
    mine.close()
    theirs.close()


# ---------------------------------------------------------------------------
# settings and limits (engine E13)
# ---------------------------------------------------------------------------

@test
def a_setting_of_the_wrong_type_is_ignored(tmp):
    home = tmp / "home"
    home.mkdir()
    path = core.config_path(home)

    def load(text):
        path.write_text(text, "utf-8")
        return core.load_config(home)

    defaults = core.DEFAULTS
    for section in ("capture", "recall", "ui"):
        for wrong in ("null", '"yes"', "[]", "5", "true"):
            cfg = load('{"%s": %s}' % (section, wrong))
            assert cfg[section] == defaults[section], (section, wrong, cfg[section])
    cfg = load('{"recall": {"max_chars": "abc", "max_items": null, "kinds": "memory", "enabled": false, "min_coverage": 1}}')
    assert cfg["recall"]["max_chars"] == 1100 and cfg["recall"]["max_items"] == 6, cfg["recall"]
    assert cfg["recall"]["kinds"] == defaults["recall"]["kinds"], "a list setting that is not a list"
    assert cfg["recall"]["enabled"] is False and cfg["recall"]["min_coverage"] == 1, "right types still apply"
    cfg = load('{"recall": {"max_chars": 700.0}, "capture": {"history_limit": 1e30, "skills_refresh_hours": Infinity}}')
    assert cfg["recall"]["max_chars"] == 700 and isinstance(cfg["recall"]["max_chars"], int), cfg["recall"]
    assert cfg["capture"]["history_limit"] == core.config.LIMIT_CAP, "a limit SQLite cannot hold is cut down"
    assert cfg["capture"]["skills_refresh_hours"] == defaults["capture"]["skills_refresh_hours"], "not a number at all"
    cfg = load('{"capture": {"history_limit": true}, "my_own": {"x": 1}}')
    assert cfg["capture"]["history_limit"] == 5000 and cfg["my_own"] == {"x": 1}, cfg
    assert core.DEFAULTS["recall"]["max_chars"] == 1100 and core.DEFAULTS["capture"]["history_limit"] == 5000
    # saving still takes whatever it is given: only reading is strict
    core.save_config(home, {"capture": {"history_limit": 3}})
    assert core.load_config(home)["capture"]["history_limit"] == 3


@test
def a_setting_that_is_ignored_is_said_once(tmp):
    home = tmp / "home"
    home.mkdir()
    path = core.config_path(home)
    said = []

    class Collect(logging.Handler):
        def emit(self, record):
            said.append(record.getMessage())

    handler, logger = Collect(), core.config.logger
    saved = (logger.level, logger.propagate)
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    core.config._WARNED.clear()
    try:
        path.write_text('{"capture": "off", "recall": {"max_chars": "abc"}, "ui": {"port": 9090}}', "utf-8")
        for _ in range(3):                                   # the provider reads the file many times: one line each
            cfg = core.load_config(home)
        assert len(said) == 2, said
        assert "'capture'" in said[0] and "'off'" in said[0] and "ignored" in said[0] and "group of settings" in said[0], said[0]
        assert "'recall.max_chars'" in said[1] and "'abc'" in said[1] and "(1100)" in said[1], said[1]
        assert cfg["capture"]["sessions"] is True and cfg["ui"]["port"] == 9090, "what is right still applies"
        path.write_text('{"capture": {"sessions": false}}', "utf-8")
        assert core.load_config(home)["capture"]["sessions"] is False and len(said) == 2, "a good setting says nothing"
        path.write_text('{"capture": false, "recall": false}', "utf-8")     # "none of it" must not leave capture running
        off = core.load_config(home)
        assert not any(v for v in off["capture"].values() if v is True) and off["recall"]["enabled"] is False, off
        assert off["capture"]["history_limit"] == 5000, "numbers keep their value"
        path.write_text('{"capture": 5}', "utf-8")
        core.load_config(home)
        assert len(said) == 3, "a different mistake for the same setting is a new line"
        path.write_text('{"capture": 7}', "utf-8")
        core.load_config(home)
        assert len(said) == 3, "the same kind of mistake again: still one line"
        path.write_text('{"recall": "' + "x" * 500 + '\\u001b[2J"}', "utf-8")
        core.load_config(home)
        assert len(said) == 4 and len(said[3]) < 200 and "\x1b" not in said[3], "what the file held is shortened and shown safely"
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved[0])
        logger.propagate = saved[1]


@test
def commands_survive_broken_settings_and_huge_limits(tmp):
    home = make_home(tmp)
    store = open_store(home)
    ingest.seed(store, home)
    ingest.remember(store, "Janet Quillan keeps the WireGuard keepalive at 25 seconds", dedupe=False)
    store.close()
    path = core.config_path(home)
    for text in ('{"capture": null, "recall": null, "ui": null}', '{"capture": "yes", "recall": []}',
                 '{"capture": {"history_limit": Infinity}}', '{"capture": {"history_limit": 1e30}}',
                 '{"capture": {"history_limit": 99999999999999999999}}', '{"recall": {"max_chars": "abc"}}'):
        path.write_text(text, "utf-8")
        assert bank_cli._history_limit(Args(limit=None), home) in (5000, core.config.LIMIT_CAP), text
        code, out, err = capture(bank_cli.main, ["--home", str(home), "import"])
        assert code == 0, (text, out, err)
        code, out, err = capture(bank_cli.main, ["--home", str(home), "recall", "wireguard", "keepalive"])
        assert code == 0, (text, out, err)
    path.write_text("{}", "utf-8")
    for command in (["import", "--limit", str(10 ** 20)], ["search", "wireguard", "--limit", str(10 ** 20)]):
        code, out, err = capture(bank_cli.main, ["--home", str(home)] + command)
        assert code == 0 and "Traceback" not in err, (command, out, err)
    assert bank_cli._history_limit(Args(limit=10 ** 20), home) == core.config.LIMIT_CAP
    assert bank_cli._history_limit(Args(limit=0), home) == 0 and bank_cli._history_limit(Args(limit=7), home) == 7
    # a store clamps what it is asked for, whoever asks (the recall engine passes its settings straight through)
    store = open_store(home)
    assert store.fts_search(["wireguard"], ["memory"], limit=10 ** 20)
    assert store.list_nodes("memory", limit=10 ** 20)
    assert isinstance(store.pinned_memories(10 ** 20), list) and isinstance(store.events_since(0, limit=10 ** 20), list)
    assert isinstance(store.events_since(1, limit=10 ** 20), list) and isinstance(store.neighbors(1, limit=10 ** 20), list)
    cfg = core.load_config(home)
    cfg["recall"]["max_items"] = 10 ** 20
    assert core.RecallEngine(store, cfg).search("wireguard keepalive", limit=10 ** 20, gate=False)
    store.close()


@test
def a_bad_history_limit_does_not_crash_the_importer(tmp):
    home = tmp / "home"
    home.mkdir()
    path = core.config_path(home)
    cases = {'{"capture": null}': 5000, '{"capture": 7}': 5000, '{"capture": {"history_limit": "many"}}': 5000,
             '{"capture": {"history_limit": 12}}': 12}
    for text, expected in cases.items():
        path.write_text(text, "utf-8")
        assert bank_cli._history_limit(Args(limit=None), home) == expected, text
    # even a configuration that bypasses load_config's checks cannot crash it
    saved = bank_cli.load_config
    try:
        for bad in ({"capture": None}, {"capture": "yes"}, {"capture": {"history_limit": float("inf")}},
                    {"capture": {"history_limit": float("nan")}}):
            bank_cli.load_config = lambda home, bad=bad: bad
            assert bank_cli._history_limit(Args(limit=None), home) == 5000, bad
    finally:
        bank_cli.load_config = saved


# ---------------------------------------------------------------------------
# the command line (engine E5, E17, docs:scanner-load-failure, the importer's failed conversations)
# ---------------------------------------------------------------------------

def installed_copy(tmp: Path, name: str):
    """The plugin installed in a profile (<root>/profiles/work/plugins/memorybank) and its cli module loaded from there."""
    home = tmp / "hermes" / "profiles" / "work"
    plugin = home / "plugins" / "memorybank"
    shutil.copytree(ROOT, plugin, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (home / "config.yaml").write_text("model:\n  default: x\n", "utf-8")
    state = sqlite3.connect(str(home / "state.db"))
    state.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, api_call_count INTEGER)")
    state.execute("INSERT INTO sessions VALUES ('a', 'cli', 'm', 1700000000.0, 5)")
    state.commit()
    state.close()
    spec = importlib.util.spec_from_file_location(name, plugin / "core" / "__init__.py",
                                                  submodule_search_locations=[str(plugin / "core")])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return home, importlib.import_module(name + ".cli")


@contextlib.contextmanager
def unload(prefix: str):
    try:
        yield
    finally:
        for key in [k for k in sys.modules if k == prefix or k.startswith(prefix + ".")]:
            del sys.modules[key]


@test
def the_installed_profile_notice_goes_to_stderr_so_json_stays_clean(tmp):
    with unload("_mb_e5_core"):
        home, installed_cli = installed_copy(tmp, "_mb_e5_core")
        other = tmp / "some-other-home"
        other.mkdir()
        saved_env = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(other)
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = installed_cli.main(["usage", "--json"])
            assert code == 0, (out.getvalue(), err.getvalue())
            json.loads(out.getvalue())                       # the whole of stdout is the JSON document
            assert "the profile this plugin is installed in" in err.getvalue() and str(home.resolve()) in err.getvalue(), err.getvalue()
            assert "installed in" not in out.getvalue()
            # a HERMES_HOME that points elsewhere is said not to be used; one that points here, or none, is not mentioned
            assert "HERMES_HOME is set to %s and is not used here" % other in err.getvalue(), err.getvalue()
            for value in (str(home), None, "  "):
                if value is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = value
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    assert installed_cli.main(["status"]) == 0
                assert "installed in" in err.getvalue() and "HERMES_HOME" not in err.getvalue(), (value, err.getvalue())
            os.environ["HERMES_HOME"] = str(other)
            # the notice is there for destructive commands too, and HERMES_HOME does not win over the installed copy
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = installed_cli.main(["purge", "--retired", "--yes"])
            assert "installed in" in err.getvalue() and "installed in" not in out.getvalue(), (out.getvalue(), err.getvalue())
            assert str(home.resolve()) in err.getvalue() and not (other / "plugin-data").exists()
            # --home or --profile say what is meant: no notice
            for argv in (["--home", str(other), "status"], ["--profile", "work", "status"]):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        installed_cli.main(argv)
                    except SystemExit:
                        pass                                  # --profile work: no such profile under that other home
                assert "installed in" not in err.getvalue() + out.getvalue(), argv
        finally:
            if saved_env is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = saved_env


@test
def export_looks_without_creating_a_bank(tmp):
    home = tmp / "home that is not there"
    for argv in ([], [str(tmp / "out" / "x.json")]):
        code, out, err = capture(bank_cli.cmd_export, home, Args(path=argv[0] if argv else None))
        assert code == 1 and "no bank yet" in out, (code, out)
    assert not home.exists() and not (tmp / "out").exists(), "export made a folder"
    empty = tmp / "empty-home"
    empty.mkdir()
    code, out, err = capture(bank_cli.main, ["--home", str(empty), "export"])
    assert code == 1 and "no bank yet" in out and list(empty.iterdir()) == [], list(empty.iterdir())
    # with a bank it still writes the file
    full = make_home(tmp / "full")
    store = open_store(full)
    store.upsert_node("m:1", "memory", content="a fact")
    store.close()
    code, out, err = capture(bank_cli.cmd_export, full, Args(path=None))
    assert code == 0 and len(list((full / "backups").glob("memorybank-export-*.json"))) == 1, out


@test
def the_export_help_says_what_the_file_holds(tmp):
    import argparse
    parser = argparse.ArgumentParser(prog="memorybank")
    bank_cli.register_cli(parser)
    help_text = " ".join(parser.format_help().split())
    line = help_text[help_text.index("export "):]
    assert "whole bank" not in help_text, "the file leaves out the long searchable text of sessions and skills"
    assert "left out" in line[:400] and "skills" in line[:400], line[:400]


@test
def a_scanner_that_cannot_be_loaded_is_said_once(tmp):
    names = ("tools", "tools.threat_patterns")
    saved = {n: sys.modules.get(n, "absent") for n in names}
    warned = getattr(bank_cli, "_SCANNER_WARNED", None)
    try:
        sys.modules["tools"] = None                    # `import tools...` fails, as it does without Hermes on the path
        sys.modules.pop("tools.threat_patterns", None)
        bank_cli._SCANNER_WARNED = False
        results, out, err = capture(lambda: [bank_cli._scanner(), bank_cli._scanner()])
        assert results == [None, None], "what is allowed does not change"
        assert out == "" and err.count("not being screened by Hermes' scanner") == 1 and err.count("\n") == 1, (out, err)
        # when it loads, it is used and nothing is said
        fake = types.ModuleType("tools")
        fake.__path__ = []
        patterns = types.ModuleType("tools.threat_patterns")
        calls = []
        patterns.first_threat_message = lambda text, scope=None: calls.append((text, scope)) or ("bad" if "bad" in text else None)
        sys.modules["tools"], sys.modules["tools.threat_patterns"] = fake, patterns
        bank_cli._SCANNER_WARNED = False
        scan, out, err = capture(bank_cli._scanner)
        assert scan("bad words") == "bad" and scan("fine") is None and calls[0][1] == "strict" and out == err == "", (out, err)
        # when it loads but fails as it is called, that is said once too, and the text is let through as before
        def broken(text, scope=None):
            raise ValueError("signature drift: " + text)

        patterns.first_threat_message = broken
        bank_cli._SCANNER_WARNED = False
        scan, out, err = capture(bank_cli._scanner)
        assert out == err == "", "nothing to say before it is used"
        results, out, err = capture(lambda: [scan("one"), scan("two")])
        assert results == [None, None] and out == "", (results, out)
        assert err.count("failed (ValueError)") == 1 and err.count("\n") == 1 and "not being screened" in err, err
        assert "one" not in err, "what it was asked about is not repeated"
    finally:
        for name, module in saved.items():
            if module == "absent":
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if warned is not None:
            bank_cli._SCANNER_WARNED = warned


@test
def import_says_how_many_conversations_could_not_be_read(tmp):
    home = make_home(tmp)
    saved = ingest.seed
    try:
        for sessions, expect_line in (({"sessions": 4, "failed": 2, "failed_ids": ["s-one", "s-two"]}, True),
                                      ({"sessions": 4, "failed": 0, "failed_ids": []}, False),
                                      ({"sessions": 4}, False)):
            ingest.seed = lambda store, home, sessions=sessions, **kw: {"sessions": dict(sessions)}
            code, out, err = capture(bank_cli.cmd_import, home, Args(limit=None, force=False))
            assert code == 0, out
            line = "2 conversation(s) could not be read and were left out (see the log)"
            assert (line in out) == expect_line, out
            assert "s-one" not in out and "failed_ids" not in out, "the ids belong in the log, not in the summary:\n" + out
            if expect_line:
                assert "failed=2" in out, out
    finally:
        ingest.seed = saved


@test
def a_failed_commit_does_not_leave_the_store_stuck(tmp):
    store = core.Store(tmp / "commit.db")

    class Proxy:
        """The real connection, except that the first COMMIT fails as it does when a reader holds the file."""
        def __init__(self, conn):
            self.conn, self.fail = conn, True

        def execute(self, sql, *args):
            if sql == "COMMIT" and self.fail:
                self.fail = False
                raise sqlite3.OperationalError("database is locked")
            return self.conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    real = store._conn
    store._conn = Proxy(real)
    try:
        store.set_meta("first", 1)
        raise AssertionError("the failing COMMIT did not raise")
    except sqlite3.OperationalError:
        pass
    assert not real.in_transaction, "the failed transaction was left open"
    store.set_meta("second", 2)                      # before: "cannot start a transaction within a transaction", for ever
    assert store.get_meta("second") == "2" and store.get_meta("first") is None
    store.close()


@test
def a_later_purge_leaves_no_text_from_items_that_were_updated(tmp):
    path = tmp / "again.db"
    store = core.Store(path)
    ids = []
    for i in range(30):
        ids.append(store.upsert_node(f"m:{i}", "memory", content=f"Scratch note {i} " + "padding " * (i % 7))[0])
    for i in range(0, 30, 2):                        # re-written items leave old wording in index pages
        store.upsert_node(f"m:{i}", "memory", content=f"Scratch note {i} reworded " + "more padding " * (i % 5))
    secret = "Quillbrook-second-purge-secret-4471"
    victim = store.upsert_node("m:secret", "memory", content=f"The cage code is {secret} for Sandy Vexor")[0]
    store.purge([ids[1]])
    store.purge([victim])
    assert secret.encode() not in everything(path), "the second purge of a bank left the deleted text in the file"
    store.close()


@test
def a_resumed_purge_also_sweeps_names_nothing_links_to(tmp):
    store = core.Store(tmp / "resume.db")
    orphan = store.upsert_node("e:orphaned-name", "entity", content="Zephyrine Quillbrook")[0]
    store.set_meta("purge_cleanup_pending", "1")
    report = {}
    assert store.finish_cleanup(report), report
    assert store.get_by_uid("e:orphaned-name") is None, "a name that nothing links to was left behind"
    assert not store.cleanup_pending()
    store.close()


@test
def words_for_off_and_on_in_the_settings_file_are_understood(tmp):
    home = tmp / "home"
    home.mkdir()
    path = home / "memorybank.json"
    for text, expect in (("false", False), ("off", False), ("No", False), ("0", False), ("true", True), ("ON", True)):
        path.write_text(json.dumps({"capture": {"sessions": text}}), "utf-8")
        assert core.load_config(home)["capture"]["sessions"] is expect, (text, core.load_config(home)["capture"])
    core.config._WARNED.clear()
    path.write_text(json.dumps({"capture": {"sessions": "maybe"}}), "utf-8")
    assert core.load_config(home)["capture"]["sessions"] is True, "an unreadable word keeps the built-in setting"


# ---------------------------------------------------------------------------

def main() -> int:
    failed = skipped = 0
    for fn in TESTS:
        tmp = Path(tempfile.mkdtemp(prefix="mbreview-"))
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
