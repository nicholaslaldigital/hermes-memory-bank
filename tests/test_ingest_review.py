"""Tests for the review fixes in the session importer (core/ingest.py) and the coverage check (core/coverage.py):
structured messages as Hermes really stores them, rows nobody typed, un-hidden conversations, the history limit in
`check`, conversations that cannot be indexed, and state.db text that is not UTF-8.

Run with any Python 3.9+ (no Hermes needed):

    python tests/test_ingest_review.py [word ...]        (words: only the tests whose name contains one of them)

Everything is built in throwaway folders; nothing outside them is read or written. Names are invented. The file is
pure ASCII on purpose: text outside ASCII is written as escapes, so that no editor or tool can change what a test means.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_core as tc  # noqa: E402  (loads the plugin from MEMORYBANK_PLUGIN_DIR, else the repository top folder)

core, ingest, coverage = tc.core, tc.ingest, tc.coverage
NOW, DAY = tc.NOW, tc.DAY

#: What Hermes puts in front of a structured (list or dict) message before it is stored: hermes_state.py,
#: SessionDB._CONTENT_JSON_PREFIX. The NUL makes SQLite's substr() and length() see an empty string.
HERMES_JSON_PREFIX = "\x00json:"

SESSIONS_SQL = """CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL, ended_at REAL,
    last_activity_at REAL, message_count INTEGER, parent_session_id TEXT, model TEXT, hidden INTEGER DEFAULT 0,
    title_source TEXT, session_key TEXT, user_id TEXT)"""
MESSAGES_SQL = """CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
    tool_calls TEXT, _compressed_summary INTEGER DEFAULT 0, active INTEGER DEFAULT 1, display_kind TEXT)"""
OLD_MESSAGES_SQL = MESSAGES_SQL.replace(", display_kind TEXT", "")   # a state.db from before Hermes had display_kind

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ---------------------------------------------------------------------------------------------- fixtures

def enc(content):
    """What Hermes' SessionDB._encode_content stores: text as it is, anything structured behind the sentinel."""
    return content if isinstance(content, str) else HERMES_JSON_PREFIX + json.dumps(content)


def make_home(tmp: Path, *, display_kind: bool = True) -> Path:
    home = tmp / "home"
    home.mkdir(parents=True)
    conn = sqlite3.connect(home / "state.db")
    conn.execute(SESSIONS_SQL)
    conn.execute(MESSAGES_SQL if display_kind else OLD_MESSAGES_SQL)
    conn.commit()
    conn.close()
    return home


def add_session(home: Path, sid: str, messages, *, title: str = "", title_source=None, started=None, activity=None,
                hidden: int = 0, source: str = "desktop", count=None) -> None:
    """``messages``: ``[(role, content)]`` or ``[(role, content, display_kind)]``."""
    started = NOW - DAY if started is None else started
    activity = started + 600 if activity is None else activity
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (sid, source, title, started, started + 600, activity,
                      len(messages) if count is None else count, None, "glm", hidden, title_source, None, None))
        has_kind = "display_kind" in {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        for item in messages:
            role, content = item[0], item[1]
            if has_kind:
                conn.execute("INSERT INTO messages(session_id, role, content, display_kind) VALUES(?,?,?,?)",
                             (sid, role, content, item[2] if len(item) > 2 else None))
            else:
                conn.execute("INSERT INTO messages(session_id, role, content) VALUES(?,?,?)", (sid, role, content))
        conn.commit()
    finally:
        conn.close()


def add_messages(home: Path, sid: str, messages) -> None:
    """More messages in a conversation that exists already: ``[(role, content)]``."""
    conn = sqlite3.connect(home / "state.db")
    try:
        for role, content in messages:
            conn.execute("INSERT INTO messages(session_id, role, content) VALUES(?,?,?)", (sid, role, content))
        conn.commit()
    finally:
        conn.close()


def update(home: Path, sql: str, params=()) -> None:
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def typed(n: int = 0):
    """An ordinary exchange: one typed question and a reply long enough to count as the outcome."""
    return [("user", f"Typed question {n} about the Acme backup window"),
            ("assistant", f"The backup window {n} stays at 02:00 every night, as before.")]


def shown(report) -> str:
    lines = []
    coverage.show(report, lines.append)
    return "\n".join(lines)


def count_events(store) -> int:
    return int(store._q("SELECT COUNT(*) AS n FROM events WHERE type='sessions'")[0]["n"])


class Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def watching(logger):
    """The records a logger gets meanwhile (and nothing is printed to the console)."""
    handler, old = Collect(), logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)


# ---------------------------------------------------------------------------------------------- E2

@test
def message_text_understands_the_hermes_sentinel(tmp):
    parts = [{"type": "text", "text": "Janet Quillan's caption"},
             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    stored = HERMES_JSON_PREFIX + json.dumps(parts)
    assert ingest.message_text(stored) == "Janet Quillan's caption", repr(ingest.message_text(stored))
    assert ingest.message_text(stored.encode("utf-8")) == "Janet Quillan's caption"      # read as a blob
    # Hermes dumps with ensure_ascii: text outside ASCII is stored as \uXXXX escapes and must come back
    escaped = HERMES_JSON_PREFIX + json.dumps([{"type": "text", "text": "Caf\u00e9 \u65e5\u672c"}])
    assert "\\u65e5" in escaped
    assert ingest.message_text(escaped) == "Caf\u00e9 \u65e5\u672c"
    # several text parts are joined, a part that is a bare string counts, an image part adds nothing
    many = HERMES_JSON_PREFIX + json.dumps([{"type": "text", "text": "one"}, "two", shot_part(), {"type": "text", "text": "three"}])
    assert ingest.message_text(many) == "one\ntwo\nthree", repr(ingest.message_text(many))
    # the opening of a very large message is all that is read: a part list cut inside the image still has its caption
    cut = stored[: stored.index("base64") + 3]
    assert ingest.message_text(cut) == "Janet Quillan's caption", repr(ingest.message_text(cut))
    # ... and so does one cut inside the caption itself (as far as it goes, without a half-written escape)
    inside = HERMES_JSON_PREFIX + json.dumps([{"type": "text", "text": "Sandy Vexor wrote \u00e9\u00e9 and more"}])
    half = inside[: inside.index(" and more") - 2]     # ends in the middle of the second \u00e9 escape
    assert ingest.message_text(half) == "Sandy Vexor wrote \u00e9", repr(ingest.message_text(half))
    # a single part stored as a dict
    assert ingest.message_text(HERMES_JSON_PREFIX + json.dumps({"type": "text", "text": "solo"})) == "solo"
    # a payload that is not JSON at all gives nothing and does not raise
    assert ingest.message_text(HERMES_JSON_PREFIX + '[{"type": "text"} EXTRA garbage') == ""
    assert ingest.message_text(HERMES_JSON_PREFIX) == ""
    # ordinary text is returned as it was, even when it starts with a bracket
    assert ingest.message_text("[ERROR] build failed in the Acme pipeline") == "[ERROR] build failed in the Acme pipeline"
    assert ingest.message_text("plain words") == "plain words"
    assert ingest.message_text(None) == ""


def shot_part():
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


@test
def structured_messages_are_read_as_hermes_stores_them(tmp):
    home = make_home(tmp)
    caption = "Please ask Janet Quillan why the Brightwave printer is offline."
    add_session(home, "s-photo", [("user", enc([{"type": "text", "text": caption}, shot_part()])),
                                  ("assistant", "The printer shows a paper jam light on the tray.")],
                started=NOW - 8 * DAY)
    add_session(home, "s-mix", [
        ("user", "Typed question about the Contoso licence renewal for the Acme head office"),
        ("assistant", "The renewal is due on the first of next month."),
        ("user", enc([{"type": "text", "text": "Now the invoice from Tom Berrio"}, shot_part()])),
        ("assistant", "I have matched it to the renewal quote."),
        ("user", "One more typed note about the Acme backup window"),
        ("assistant", "Noted, the window stays at 02:00."),
    ], started=NOW - 7 * DAY)
    add_session(home, "s-plain", typed(1), started=NOW - 6 * DAY)
    big = enc([{"type": "text", "text": "Sandy Vexor sent this rack photo for the Brightwave audit"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 200000}}])
    add_session(home, "s-big", [("user", big), ("assistant", "The rack photo shows a loose patch cable.")],
                started=NOW - 5 * DAY)
    add_session(home, "s-imageonly", [("user", enc([shot_part()])),
                                      ("assistant", "That is a picture of a rack in a closet.")], started=NOW - 4 * DAY)
    add_session(home, "s-reply", [
        ("user", "What is wrong with the Brightwave printer?"),
        ("assistant", "An earlier plain reply that is long enough to count."),
        ("assistant", enc([{"type": "text", "text": "Final answer: the printer needs a new fuser kit from Brightwave."}])),
    ], started=NOW - 3 * DAY)
    add_session(home, "s-toolonly", [
        ("user", "Run the diagnostics for the Acme firewall"),
        ("assistant", "Running the diagnostics now, this takes a minute or two."),
        ("assistant", enc([{"type": "tool_use", "id": "t1", "name": "run_diag", "input": {"target": "fw1"}}])),
    ], started=NOW - 2 * DAY)
    store = tc.open_store(home)
    result = ingest.import_sessions(store, home / "state.db")

    photo = store.get_by_uid("s:s-photo")
    assert photo is not None, "a conversation whose only user message is structured was not indexed: " + repr(result)
    assert photo["meta"]["prompts"] == [caption], photo["meta"]
    assert photo["title"].startswith("Please ask Janet Quillan"), photo["title"]
    for name in ("janet quillan", "brightwave"):
        assert store.get_by_uid("e:" + name) is not None, f"the caption did not yield the entity {name!r}"

    mix = store.get_by_uid("s:s-mix")
    assert mix is not None and mix["meta"]["prompts"] == [
        "Typed question about the Contoso licence renewal for the Acme head office",
        "Now the invoice from Tom Berrio",
        "One more typed note about the Acme backup window",
    ], mix and mix["meta"]
    assert store.get_by_uid("e:tom berrio") is not None

    plain = store.get_by_uid("s:s-plain")
    assert plain is not None and plain["meta"]["prompts"] == ["Typed question 1 about the Acme backup window"]

    big_node = store.get_by_uid("s:s-big")     # the read stops long before the image: the caption is in the part that was read
    assert big_node is not None, "a message cut inside its image lost its caption"
    assert big_node["meta"]["prompts"] == ["Sandy Vexor sent this rack photo for the Brightwave audit"], big_node["meta"]

    assert store.get_by_uid("s:s-imageonly") is None, "nothing was typed: no title, no prompt, nothing to index"

    reply = store.get_by_uid("s:s-reply")
    assert reply is not None and reply["meta"]["outcome"] == \
        "Final answer: the printer needs a new fuser kit from Brightwave.", reply and reply["meta"]
    tool = store.get_by_uid("s:s-toolonly")      # a reply without text is not an outcome: the one before it is
    assert tool is not None and tool["meta"]["outcome"] == "Running the diagnostics now, this takes a minute or two.", \
        tool and tool["meta"]

    for node in store.list_nodes("session"):     # the sentinel itself never reaches what is stored or shown
        blob = json.dumps(node, default=str)
        assert "json:[" not in blob and "\\u0000" not in blob, node["uid"]
    store.close()


@test
def the_opening_of_a_message_is_read_in_characters_unless_it_is_structured(tmp):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE m(id INTEGER PRIMARY KEY, content TEXT)")
    cjk = "\u65e5\u672c\u8a9e" * 1700                                    # 5100 characters, three bytes each
    structured = HERMES_JSON_PREFIX + json.dumps([{"type": "text", "text": "x" * 9000}])
    conn.execute("INSERT INTO m(content) VALUES(?)", (cjk,))
    conn.execute("INSERT INTO m(content) VALUES(?)", (structured,))
    conn.execute("INSERT INTO m(content) VALUES(NULL)")
    for sql in (ingest._opening_sql(6000), coverage._opening_sql(6000)):
        rows = [r[0] for r in conn.execute(f"SELECT {sql} FROM m ORDER BY id")]
        assert rows[0] == cjk, "text outside ASCII was cut shorter than the characters asked for: %d" % len(rows[0])
        assert rows[1] == structured[:len(HERMES_JSON_PREFIX) + 6000], len(rows[1])
        assert rows[2] is None
    assert [r[0] for r in conn.execute(f"SELECT {ingest._opening_sql(10)} FROM m ORDER BY id")][0] == cjk[:10]


@test
def a_photo_message_as_hermes_flattens_it_and_the_live_capture_survive_the_import(tmp):
    home = make_home(tmp)
    caption = "Please ask Sandy Vexor why the Acme switch keeps rebooting."
    # the ordinary shape: Hermes' flush stores a text projection ("[screenshot]" for each image part)
    flat = caption + "\n\n[Image attached at: image-1.png]\n[screenshot]"
    add_session(home, "s-flat", [("user", flat), ("assistant", "The switch logs a power supply fault.")],
                started=NOW - 5 * DAY)
    # the shape that compaction, retries and forks insert: the parts behind the sentinel
    add_session(home, "s-parts", [
        ("user", enc([{"type": "text", "text": caption}, shot_part()])),
        ("assistant", "The switch logs a power supply fault."),
        ("user", "Typed follow-up about the Acme switch firmware"),
        ("assistant", "Firmware 4.2 fixes the reboot loop."),
    ], started=NOW - 4 * DAY)
    store = tc.open_store(home)
    # what live capture stored while the conversation was going on: the caption, as text
    ingest.capture_turn(store, "s-parts", caption, "The switch logs a power supply fault.")
    ingest.import_sessions(store, home / "state.db")
    flat_prompts = store.get_by_uid("s:s-flat")["meta"]["prompts"]
    assert len(flat_prompts) == 1 and flat_prompts[0].startswith("Please ask Sandy Vexor"), flat_prompts
    kept = store.get_by_uid("s:s-parts")["meta"]["prompts"]
    assert kept == [caption, "Typed follow-up about the Acme switch firmware"], \
        f"the import erased the caption that live capture had stored: {kept}"
    assert coverage.check(home)["ok"]
    store.close()


@test
def coverage_sees_structured_messages_too(tmp):
    home = make_home(tmp)
    add_session(home, "s-typed", typed(2), started=NOW - 3 * DAY)
    store = tc.open_store(home)
    ingest.import_sessions(store, home / "state.db")
    assert coverage.check(home)["ok"]
    add_session(home, "s-photo", [
        ("user", enc([{"type": "text", "text": "Why does the Brightwave printer keep jamming?"}, shot_part()])),
        ("assistant", "The rollers need cleaning."),
    ], started=NOW - DAY)
    add_session(home, "s-imageonly", [("user", enc([shot_part()])), ("assistant", "That is a rack in a closet.")],
                started=NOW - DAY + 60)
    seen = coverage.eligible_sessions(home)
    assert seen["s-photo"]["user_prompts"] == 1, seen
    assert seen["s-imageonly"]["user_prompts"] == 0, seen
    report = coverage.check(home)           # the photo conversation has words in it and is not in the bank yet
    assert not report["ok"] and report["sections"]["sessions"]["missing"] == ["s-photo"], report["sections"]["sessions"]
    assert report["sections"]["sessions"]["skipped_no_title_and_no_user_text"] == 1, report["sections"]["sessions"]
    ingest.import_sessions(store, home / "state.db")
    assert coverage.check(home)["ok"]
    store.close()


# ---------------------------------------------------------------------------------------------- E6

DELEGATED = "Findings from the delegated agent: Please ask Janet Quillan about the Contoso Orchard rollout."


@test
def rows_nobody_typed_are_not_prompts_titles_or_entities(tmp):
    home = make_home(tmp)
    runbook = ("Setup runbook for the assistant: the operator is Zephyrine Quillbrook, keep the vault hint "
               "for Contoso Tower.")
    add_session(home, "s-seed", [
        ("user", runbook, "hidden"),
        ("assistant", "Understood, I will follow the setup runbook."),
        ("user", "Please check why the Brightwave printer is offline"),
        ("assistant", "The printer tray has a paper jam, clear it and retry."),
        ("user", "Also remind Sandy Vexor about the Acme invoice", "steer"),      # typed by the person
        ("assistant", "Internal note about the delegated work that nobody saw.", "hidden"),
        ("assistant", "This turn did not complete. Some actions may already have run; verify their effects.", "failed_turn"),
        ("user", DELEGATED, "async_delegation_complete"),
        ("user", "Model switched to glm for this conversation", "model_switch"),
    ])
    add_session(home, "s-onlyhidden", [
        ("user", runbook, "hidden"), ("assistant", "Understood, I will follow the setup runbook."),
        ("user", DELEGATED, "async_delegation_complete"),
        ("assistant", "I have noted the delegated findings for later."),
    ], started=NOW - 2 * DAY)
    store = tc.open_store(home)
    ingest.import_sessions(store, home / "state.db")
    node = store.get_by_uid("s:s-seed")
    assert node["meta"]["prompts"] == ["Please check why the Brightwave printer is offline",
                                       "Also remind Sandy Vexor about the Acme invoice"], node["meta"]["prompts"]
    assert node["title"].startswith("Please check why the Brightwave"), node["title"]
    assert node["meta"]["outcome"] == "The printer tray has a paper jam, clear it and retry.", node["meta"]["outcome"]
    assert "runbook" not in node["content"] and "Orchard" not in node["content"], node["content"]
    for name in ("zephyrine quillbrook", "contoso tower", "janet quillan", "contoso orchard"):
        assert store.get_by_uid("e:" + name) is None, f"an entity came from a row nobody typed: {name}"
    for name in ("sandy vexor", "brightwave"):
        assert store.get_by_uid("e:" + name) is not None, name
    engine = core.RecallEngine(store, {})
    assert all(h.uid != "s:s-seed" for h in engine.search("Zephyrine Quillbrook Contoso Tower vault", gate=False))
    assert store.get_by_uid("s:s-onlyhidden") is None, "a conversation whose only user text was scaffolding was indexed"
    store.close()
    seen = coverage.eligible_sessions(home)
    assert seen["s-seed"]["user_prompts"] == 2 and seen["s-onlyhidden"]["user_prompts"] == 0, seen
    report = coverage.check(home)
    assert report["ok"] and report["sections"]["sessions"]["skipped_no_title_and_no_user_text"] == 1, \
        report["sections"]["sessions"]


@test
def an_old_state_db_without_display_kind_still_imports(tmp):
    home = make_home(tmp, display_kind=False)
    add_session(home, "s-old", typed(3), started=NOW - 2 * DAY)
    add_session(home, "s-photo", [("user", enc([{"type": "text", "text": "A caption for Janet Quillan"}, shot_part()])),
                                  ("assistant", "That shows a patch panel.")], started=NOW - DAY)
    store = tc.open_store(home)
    result = ingest.import_sessions(store, home / "state.db")
    assert result["sessions"] == 2, result
    assert store.get_by_uid("s:s-old")["meta"]["prompts"] == ["Typed question 3 about the Acme backup window"]
    assert store.get_by_uid("s:s-photo")["meta"]["prompts"] == ["A caption for Janet Quillan"]
    assert coverage.check(home)["ok"]
    store.close()


# ---------------------------------------------------------------------------------------------- E8

@test
def a_conversation_that_is_unhidden_comes_back_without_new_activity(tmp):
    home = make_home(tmp)
    add_session(home, "s-a", typed(4), started=NOW - 4 * DAY)
    add_session(home, "s-b", [("user", "Planning the Brightwave printer replacement for the Contoso office"),
                              ("assistant", "I suggest ordering two units and keeping one as a spare.")],
                started=NOW - 3 * DAY)
    add_session(home, "s-c", typed(8), started=NOW - DAY)       # newer: the watermark ends up above s-b's activity
    store = tc.open_store(home)
    db = home / "state.db"
    assert ingest.import_sessions(store, db)["sessions"] == 3
    update(home, "UPDATE sessions SET hidden=1 WHERE id='s-b'")
    assert ingest.import_sessions(store, db)["retired"] == 1
    node = store.get_by_uid("s:s-b")
    assert node["status"] == "archived" and node["meta"]["gone"] is True, node
    engine = core.RecallEngine(store, {})
    assert all(h.uid != "s:s-b" for h in engine.search("Brightwave printer replacement Contoso", gate=False))
    still = ingest.import_sessions(store, db)                              # still hidden: nothing is read, nothing returns
    assert store.get_by_uid("s:s-b")["status"] == "archived" and still["revived"] == 0 and still["sessions"] == 0, still

    # a turn is typed while it is hidden; its activity stays below the watermark (s-c is newer)
    add_messages(home, "s-b", [("user", "Also ask Tom Berrio about the Brightwave fuser kit"),
                               ("assistant", "I will ask Tom Berrio about the fuser kit tomorrow.")])
    update(home, "UPDATE sessions SET last_activity_at=?, message_count=4 WHERE id='s-b'", (NOW - 3 * DAY + 900,))
    update(home, "UPDATE sessions SET hidden=0 WHERE id='s-b'")          # Hermes changes nothing else
    report = coverage.check(home)           # Hermes shows it, the bank does not: that is a gap, and `check` says so
    assert not report["ok"] and report["sections"]["sessions"]["missing"] == ["s-b"], report["sections"]["sessions"]
    events_before_revival = count_events(store)
    back = ingest.import_sessions(store, db)
    assert count_events(store) == events_before_revival + 1, "an open viewer is not told that a conversation is back"
    node = store.get_by_uid("s:s-b")
    assert node["status"] == "active" and node["meta"]["gone"] is False, f"still retired after being unhidden: {node}"
    assert back["revived"] == 1 and back["retired"] == 0 and back["sessions"] == 1, back
    assert node["meta"]["prompts"] == ["Planning the Brightwave printer replacement for the Contoso office",
                                       "Also ask Tom Berrio about the Brightwave fuser kit"], \
        f"what was said while it was hidden is missing: {node['meta']['prompts']}"
    assert any(h.uid == "s:s-b" for h in engine.search("Brightwave printer replacement Contoso", gate=False))
    assert coverage.check(home)["ok"], shown(coverage.check(home))

    before_events, before_time = count_events(store), node["updated_at"]
    quiet = ingest.import_sessions(store, db)                              # nothing changed: nothing is done
    assert quiet["revived"] == 0 and quiet["retired"] == 0 and quiet["sessions"] == 0, quiet
    assert count_events(store) == before_events, "an import that changed nothing left an event"
    assert store.get_by_uid("s:s-b")["updated_at"] == before_time
    store.close()


@test
def a_conversation_forgotten_in_the_viewer_stays_retired_when_unhidden(tmp):
    home = make_home(tmp)
    add_session(home, "s-c", typed(5), started=NOW - 3 * DAY)
    add_session(home, "s-d", typed(6), started=NOW - 2 * DAY)
    add_session(home, "s-e", typed(7), started=NOW - DAY)
    store = tc.open_store(home)
    db = home / "state.db"
    ingest.import_sessions(store, db)
    update(home, "UPDATE sessions SET hidden=1 WHERE id IN ('s-c', 's-d', 's-e')")
    assert ingest.import_sessions(store, db)["retired"] == 3
    for sid in ("s-c", "s-e"):
        assert "forgotten" in ingest.forget(store, store.get_by_uid("s:" + sid)["id"])   # the person retired it for good
    update(home, "UPDATE sessions SET hidden=0 WHERE id IN ('s-c', 's-d')")
    back = ingest.import_sessions(store, db)
    assert store.get_by_uid("s:s-c")["status"] == "archived", "a conversation the person forgot came back"
    assert store.get_by_uid("s:s-d")["status"] == "active" and back["revived"] == 1, back
    assert back["sessions"] == 1, "a conversation the person forgot was read again for nothing"
    report = coverage.check(home)           # forgetting is the person's choice, not a gap
    assert report["ok"], report["sections"]["sessions"]
    # the same when the conversation has new activity, so that it is read again on the way
    update(home, "UPDATE sessions SET hidden=0, last_activity_at=? WHERE id='s-e'", (time.time() + 5,))
    ingest.import_sessions(store, db)
    assert store.get_by_uid("s:s-e")["status"] == "archived", "a forgotten conversation came back when it was read again"
    store.close()


@test
def unhidden_conversations_are_found_in_chunks(tmp):
    home = make_home(tmp)
    many = 700
    conn = sqlite3.connect(home / "state.db")
    conn.executemany("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     [(f"c{i}", "desktop", f"Conversation {i}", NOW - DAY, NOW - DAY + 60, NOW - DAY + 60, 2, None,
                       "glm", 0, "llm", None, None) for i in range(many)])
    conn.commit()
    conn.close()
    store = tc.open_store(home)
    with store.transaction():
        for i in range(many):
            store.upsert_node(f"s:c{i}", "session", title=f"Conversation {i}", status="archived", meta={"gone": True})
    store.set_meta("sessions_watermark", repr(NOW))      # nothing is newer: only a look at the retired ones finds them
    real_connect = sqlite3.connect
    can_limit = hasattr(sqlite3.Connection, "setlimit")  # Python 3.11+: this SQLite then takes 600 values in a list

    def limited(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if can_limit:
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 600)
        return conn

    sqlite3.connect = limited
    try:
        result = ingest.import_sessions(store, home / "state.db")
    finally:
        sqlite3.connect = real_connect
    assert result["revived"] == many, result
    assert len(store.list_nodes("session", statuses=("active",))) == many
    store.close()


# ---------------------------------------------------------------------------------------------- E9

@test
def check_applies_the_history_limit_the_importer_applies(tmp):
    home = make_home(tmp)
    for i in reversed(range(12)):          # oldest first, ids not in date order: only the date can pick the newest
        add_session(home, f"h{(i * 5) % 12:02d}", typed(i), started=NOW - (i + 1) * DAY)
    add_session(home, "h-hidden", typed(99), started=NOW - 1800, hidden=1)       # newer than all, but never counts
    core.save_config(home, {"capture": {"history_limit": 3}})
    newest = ["h00", "h05", "h10"]
    store = tc.open_store(home)
    ingest.import_sessions(store, home / "state.db", limit=3)            # what `import` does with this profile's setting
    assert sorted(n["uid"][2:] for n in store.list_nodes("session")) == newest
    report = coverage.check(home)
    section = report["sections"]["sessions"]
    assert report["ok"], section
    assert section["missing"] == [] and section["older_than_history_limit"] == 9, section
    assert section["conversations_in_hermes"] == 12 and section["in_bank"] == 3, section
    text = shown(report)
    assert "older than history_limit, not indexed on purpose" in text, text
    assert "MISSING sessions" not in text and "GAPS FOUND" not in text, text
    assert "everything is in the bank" in text, text

    add_session(home, "h-new", typed(50), started=NOW - 600)            # a conversation inside the limit that was never indexed
    report = coverage.check(home)
    section = report["sections"]["sessions"]
    assert not report["ok"] and section["missing"] == ["h-new"], section
    assert section["older_than_history_limit"] == 9, section       # h10 is older now, but it is in the bank
    text = shown(report)
    assert "MISSING sessions" in text and "import --limit 0 adds them" in text, text    # how to get the older ones too
    result_line = text.splitlines()[-1]
    assert result_line.startswith("  RESULT: GAPS FOUND - run: import --force") and "limit 0" not in result_line, \
        result_line                                                                      # the remedy keeps the limit

    # the same number the importer reads (load_config: a number counts; a string, even "3", is ignored with a warning
    # and the default of 5000 applies)
    for value, older, limit in (("3", 0, 5000), (0, 0, 0), ("abc", 0, 5000), (None, 0, 5000), (-1, 0, -1), (7.9, 6, 7)):
        core.save_config(home, {"capture": {"history_limit": value}})
        section = coverage.check(home)["sections"]["sessions"]
        assert section["older_than_history_limit"] == older and section["history_limit"] == limit, (value, section)
    assert coverage.check(home, history_limit=3)["sections"]["sessions"]["older_than_history_limit"] == 9
    assert coverage.check(home, history_limit=0)["sections"]["sessions"]["older_than_history_limit"] == 0
    store.close()


@test
def the_remedy_that_check_prints_really_closes_the_gap_it_found(tmp):
    home = make_home(tmp)
    for i in range(12):
        add_session(home, f"g{i:02d}", typed(i), started=NOW - (12 - i) * DAY)
    core.save_config(home, {"capture": {"history_limit": 3}})
    store = tc.open_store(home)
    ingest.import_sessions(store, home / "state.db", limit=3)
    assert coverage.check(home)["ok"]
    core.save_config(home, {"capture": {"history_limit": 10}})       # the person raises the limit: seven more are due
    section = coverage.check(home)["sections"]["sessions"]
    assert len(section["missing"]) == 7 and section["older_than_history_limit"] == 2, section
    ingest.import_sessions(store, home / "state.db", limit=10)       # a plain import only looks at what is new ...
    assert len(coverage.check(home)["sections"]["sessions"]["missing"]) == 7
    ingest.import_sessions(store, home / "state.db", limit=10, force=True)      # ... which is why check says --force
    report = coverage.check(home)
    assert report["ok"] and report["sections"]["sessions"]["older_than_history_limit"] == 2, shown(report)
    store.close()


# ---------------------------------------------------------------------------------------------- E11

def poison():
    """A part list with a lone surrogate: it parses, but sqlite3 cannot bind it, so the conversation cannot be stored."""
    return json.dumps([{"type": "text", "text": "Broken emoji \ud800 in the caption"}])


@test
def a_conversation_that_cannot_be_indexed_is_failed_not_skipped(tmp):
    home = make_home(tmp)
    add_session(home, "sess-A", typed(7), started=NOW - 3 * DAY)
    add_session(home, "sess-B", [("user", poison()), ("assistant", "I read that, thank you very much.")],
                started=NOW - 2 * DAY)
    add_session(home, "sess-C", typed(8), started=NOW - DAY)
    store = tc.open_store(home)
    db = home / "state.db"
    with watching(ingest.logger) as records:
        result = ingest.import_sessions(store, db)
    assert result.get("failed") == 1 and result.get("failed_ids") == ["sess-B"], result
    assert result["skipped"] == 0 and result["sessions"] == 2, result
    assert store.get_by_uid("s:sess-C") is not None, "a conversation after the broken one was not indexed"
    activity = NOW - DAY + 600
    assert abs(float(store.get_meta("sessions_watermark")) - activity) < 1e-6, "the broken one must not hold the watermark back"
    warned = [r for r in records if r.levelno == logging.WARNING and "sess-B" in r.getMessage()]
    assert len(warned) == 1, [r.getMessage() for r in records]
    assert "UnicodeEncodeError" in warned[0].getMessage(), warned[0].getMessage()      # the reason is in the log

    with watching(ingest.logger):               # it is tried again, and reported again, never skipped in silence
        again = ingest.import_sessions(store, db)
    assert again["failed"] == 1 and again["failed_ids"] == ["sess-B"] and again["sessions"] == 0, again

    report = coverage.check(home)
    section = report["sections"]["sessions"]
    assert section["unreadable"] == ["sess-B"] and section["missing"] == [], section
    assert not report["ok"], "a conversation that is not in the bank is still a gap"
    text = shown(report)
    assert "unreadable, see the log" in text and "MISSING sessions" not in text, text
    assert "import --force" not in text, "no remedy that cannot help: " + text
    assert "GAPS FOUND" in text.splitlines()[-1], text

    update(home, "UPDATE messages SET content=? WHERE session_id='sess-B' AND role='user'",
           ("Why does the Brightwave printer keep jamming?",))              # repaired: no new activity is needed
    fixed = ingest.import_sessions(store, db)
    assert fixed["failed"] == 0 and fixed["failed_ids"] == [] and fixed["sessions"] == 1, fixed
    assert store.get_by_uid("s:sess-B") is not None
    assert coverage.check(home)["ok"], shown(coverage.check(home))
    assert ingest.import_sessions(store, db)["failed"] == 0
    store.close()


@test
def failed_ids_are_capped_and_counted_in_full(tmp):
    home = make_home(tmp)
    for i in range(55):
        add_session(home, f"bad{i:02d}", [("user", poison()), ("assistant", "Read, thank you very much.")],
                    started=NOW - (60 - i) * 3600)
    add_session(home, "fine", typed(20), started=NOW - 600)          # not every conversation fails: this is bad data
    store = tc.open_store(home)
    with watching(ingest.logger) as records:
        result = ingest.import_sessions(store, home / "state.db")
    assert result["failed"] == 55 and len(result["failed_ids"]) == 50 and result["skipped"] == 0, result
    assert result["sessions"] == 1 and store.get_by_uid("s:fine") is not None, result
    assert len(json.loads(store.get_meta("sessions_failed"))) == 50
    warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 51, f"one line per remembered conversation and one for the rest: {len(warnings)}"
    assert "5 more conversations" in warnings[-1] and "import --force" in warnings[-1], warnings[-1]
    store.close()


@test
def a_bug_that_hits_every_conversation_is_not_recorded_as_done(tmp):
    home = make_home(tmp)
    for i in range(55):
        add_session(home, f"bad{i:02d}", [("user", poison()), ("assistant", "Read, thank you very much.")],
                    started=NOW - (60 - i) * 3600)
    store = tc.open_store(home)
    db = home / "state.db"
    try:
        with watching(ingest.logger) as records:
            ingest.import_sessions(store, db)
    except UnicodeEncodeError:
        pass
    else:
        raise AssertionError("an import in which every conversation failed was taken for a good one")
    assert len([r for r in records if r.levelno == logging.WARNING]) == 50, "the log was flooded or silent"
    for key in ("sessions_watermark", "sessions_imported_at", "sessions_failed"):
        assert store.get_meta(key) is None, f"{key} was saved by an import that did nothing"
    update(home, "UPDATE messages SET content='Why does the Brightwave printer keep jamming?' WHERE role='user'")
    fixed = ingest.import_sessions(store, db)             # the cause is gone: the next import indexes all of them
    assert fixed["sessions"] == 55 and fixed["failed"] == 0, fixed
    store.close()


@test
def a_bug_in_the_importer_is_isolated_and_remembered(tmp):
    home = make_home(tmp)
    for i, sid in enumerate(("sess-A", "sess-B", "sess-C")):
        add_session(home, sid, typed(30 + i), started=NOW - (3 - i) * DAY)
    db = home / "state.db"
    real = ingest.upsert_session
    broken = {"sess-B"}

    def flaky(store, session_id, *args, **kwargs):
        if session_id in broken:
            raise TypeError("a bug that only these conversations hit")
        return real(store, session_id, *args, **kwargs)

    one, every = tc.open_store(home), core.Store(tmp / "second.db")
    ingest.upsert_session = flaky
    try:
        with watching(ingest.logger):
            single = ingest.import_sessions(one, db)
            broken.update({"sess-A", "sess-C"})         # now every conversation hits it, but there are only three
            everything = ingest.import_sessions(every, db)
    finally:
        ingest.upsert_session = real
    assert single["failed_ids"] == ["sess-B"] and single["sessions"] == 2, single
    assert one.get_by_uid("s:sess-C") is not None and float(one.get_meta("sessions_watermark")) > NOW - DAY
    assert everything["failed"] == 3 and everything["sessions"] == 0, everything     # a few: bad data, not a bug
    assert sorted(json.loads(every.get_meta("sessions_failed"))) == ["sess-A", "sess-B", "sess-C"]
    fixed = ingest.import_sessions(every, db)             # no --force: what failed is tried again by itself
    assert fixed["sessions"] == 3 and fixed["failed"] == 0, fixed
    assert coverage.check(home, bank_path=tmp / "second.db")["ok"]
    one.close()
    every.close()


@test
def an_unreadable_timestamp_fails_one_conversation_not_the_import(tmp):
    home = make_home(tmp)
    add_session(home, "sess-A", typed(9), started=NOW - 3 * DAY)
    add_session(home, "sess-B", typed(10), started=NOW - 2 * DAY)
    add_session(home, "sess-C", typed(11), started=NOW - DAY)
    update(home, "UPDATE sessions SET last_activity_at='n/a' WHERE id='sess-B'")
    store = tc.open_store(home)
    with watching(ingest.logger):
        result = ingest.import_sessions(store, home / "state.db")
    assert result["failed_ids"] == ["sess-B"] and result["failed"] == 1, result
    assert store.get_by_uid("s:sess-A") is not None and store.get_by_uid("s:sess-C") is not None, result
    store.close()


@test
def a_read_error_in_hermes_file_is_one_conversations_but_a_bank_error_stops_the_import(tmp):
    home = make_home(tmp)
    add_session(home, "sess-A", typed(12), started=NOW - 3 * DAY)
    add_session(home, "sess-B", typed(13), started=NOW - 2 * DAY)
    add_session(home, "sess-C", typed(14), started=NOW - DAY)
    db = home / "state.db"

    def with_flaky_state(error):
        class Flaky(sqlite3.Connection):
            def execute(self, sql, *args):
                if "FROM messages" in sql and args and args[0] and args[0][0] == "sess-B":
                    raise error
                return super().execute(sql, *args)
        return Flaky

    real_connect = sqlite3.connect

    def run(store, error):
        flaky = with_flaky_state(error)
        sqlite3.connect = lambda *a, **kw: real_connect(*a, factory=flaky, **kw)
        try:
            with watching(ingest.logger) as records:
                return ingest.import_sessions(store, db), records
        finally:
            sqlite3.connect = real_connect

    # Hermes' file cannot be read for one conversation (damaged page): that conversation fails, the others go on
    store = tc.open_store(home)
    result, records = run(store, sqlite3.DatabaseError("database disk image is malformed: " + "SECRET words " * 3))
    assert result["failed_ids"] == ["sess-B"] and result["sessions"] == 2, result
    assert store.get_by_uid("s:sess-C") is not None
    reason = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    assert len(reason) == 1 and "sess-B" in reason[0] and "DatabaseError" in reason[0], reason
    assert "SECRET" not in reason[0], "the log carries no text from Hermes' database: " + reason[0]
    assert "tried again at the next import" in reason[0], reason[0]
    store.close()

    # an error that merely quotes the word "locked" from a stored text is not a lock
    quoting = core.Store(tmp / "quoting.db")
    result, records = run(quoting, sqlite3.OperationalError("Could not decode to UTF-8 column 'content' with text "
                                                            "'the door is locked and SECRET'"))
    assert result["failed_ids"] == ["sess-B"] and result["sessions"] == 2, result
    assert all("SECRET" not in r.getMessage() for r in records)
    quoting.close()

    # a lock on Hermes' file passes: the import stops so that nothing is skipped past, and nothing is remembered as failed
    other = core.Store(tmp / "second.db")
    try:
        run(other, sqlite3.OperationalError("database is locked"))
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("a lock on Hermes' database was taken for a broken conversation")
    assert other.get_meta("sessions_watermark") is None and other.get_meta("sessions_failed") is None

    # the bank is full or locked: the import stops and nothing is skipped past
    third = core.Store(tmp / "third.db")
    real_upsert = third.upsert_node

    def flaky_upsert(uid, *args, **kwargs):
        if uid == "s:sess-B":
            raise sqlite3.OperationalError("database or disk is full")
        return real_upsert(uid, *args, **kwargs)

    third.upsert_node = flaky_upsert
    try:
        ingest.import_sessions(third, db)
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("an error from the bank was taken for a broken conversation")
    assert third.get_meta("sessions_watermark") is None and third.get_meta("sessions_failed") is None
    other.close()
    third.close()


# ---------------------------------------------------------------------------------------------- E12

@test
def text_that_is_not_utf8_does_not_stop_the_import_or_the_check(tmp):
    home = make_home(tmp)
    add_session(home, "sess-A", typed(15), started=NOW - 3 * DAY)
    add_session(home, "sess-B", [("user", "placeholder"), ("assistant", "A reply that is long enough to count.")],
                started=NOW - 2 * DAY, title="Printer audit", title_source="user")
    add_session(home, "sess-C", typed(16), started=NOW - DAY)
    conn = sqlite3.connect(home / "state.db")      # bytes that are not UTF-8, stored as TEXT (CAST keeps them as they are)
    conn.execute("UPDATE messages SET content=CAST(? AS TEXT) WHERE session_id='sess-B' AND role='user'",
                 (b"Why is the Brightwave printer \xff\xfe offline?",))
    conn.execute("UPDATE sessions SET title=CAST(? AS TEXT) WHERE id='sess-B'", (b"Printer \xff audit",))
    conn.commit()
    conn.close()
    store = tc.open_store(home)
    result = ingest.import_sessions(store, home / "state.db")
    assert result["sessions"] == 3 and result["failed"] == 0, result
    assert store.get_by_uid("s:sess-C") is not None, "a newer conversation was never indexed"
    assert store.get_meta("sessions_watermark") is not None
    node = store.get_by_uid("s:sess-B")
    assert node["meta"]["prompts"] == ["Why is the Brightwave printer \ufffd\ufffd offline?"], node["meta"]
    assert node["title"].startswith("Printer "), node["title"]
    report = coverage.check(home)           # used to raise "Could not decode to UTF-8"
    assert report["ok"], report["sections"]["sessions"]
    store.close()


# ---------------------------------------------------------------------------------------------- runner

def main() -> int:
    only = sys.argv[1:]                    # optional: run just the tests whose name contains one of these words
    chosen = [fn for fn in TESTS if not only or any(word in fn.__name__ for word in only)]
    failed = 0
    for fn in chosen:
        tmp = Path(tempfile.mkdtemp(prefix="mbingest-"))
        try:
            fn(tmp)
            print(f"PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(chosen) - failed}/{len(chosen)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
