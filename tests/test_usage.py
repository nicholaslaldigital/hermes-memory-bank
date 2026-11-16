"""Usage meter tests on synthetic state.db files: run with any Python 3.8+ (no Hermes needed).

    python tests/test_usage.py
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import math
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(os.environ.get("MEMORYBANK_PLUGIN_DIR") or Path(__file__).resolve().parent.parent).resolve()
_spec = importlib.util.spec_from_file_location(
    "_memorybank_core", ROOT / "core" / "__init__.py", submodule_search_locations=[str(ROOT / "core")]
)
core = importlib.util.module_from_spec(_spec)
sys.modules["_memorybank_core"] = core
_spec.loader.exec_module(core)

from _memorybank_core import cli as bank_cli  # noqa: E402
from _memorybank_core import usage  # noqa: E402

DAY = 86400.0


def at(y: int, m: int, d: int, hh: int = 12, mm: int = 0) -> float:
    """A local wall-clock time, so the tests do not depend on the machine's time zone."""
    return time.mktime((y, m, d, hh, mm, 0, 0, 0, -1))


NOW = at(2026, 11, 12)  # a Thursday; its week starts Monday 2026-11-09

FULL_SESSIONS = """CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, api_call_count INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT)"""
FULL_MESSAGES = """CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT,
    tool_name TEXT, timestamp REAL)"""


def make_state(home: Path, sessions=(), messages=(), sessions_sql=FULL_SESSIONS, messages_sql=FULL_MESSAGES) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(sessions_sql)
    if messages_sql:
        conn.execute(messages_sql)
    for s in sessions:
        row = {"source": "desktop", "model": "model-a", "api_call_count": 10, "input_tokens": 1000, "output_tokens": 100,
               "cache_read_tokens": 9000, "cache_write_tokens": 1000, "reasoning_tokens": 0, "estimated_cost_usd": 1.0,
               "actual_cost_usd": None}
        row.update(s)
        cols = ", ".join(row)
        conn.execute("INSERT INTO sessions (%s) VALUES (%s)" % (cols, ",".join("?" * len(row))), tuple(row.values()))
    for sid, role, tool, content, ts in messages:
        conn.execute("INSERT INTO messages (session_id, role, tool_name, content, timestamp) VALUES (?,?,?,?,?)",
                     (sid, role, tool, content, ts))
    conn.commit()
    conn.close()
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(fn, *args, **kwargs):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(*args, **kwargs)
    return code, buf.getvalue()


def t_totals(tmp: Path) -> None:
    home = tmp / "totals"
    make_state(home, sessions=[
        {"id": "s1", "started_at": NOW - 2 * DAY, "api_call_count": 10, "input_tokens": 1000, "cache_read_tokens": 8000,
         "cache_write_tokens": 1000, "output_tokens": 500, "reasoning_tokens": 100, "estimated_cost_usd": 1.00},
        {"id": "s2", "started_at": NOW - 1 * DAY, "api_call_count": 20, "input_tokens": 2000, "cache_read_tokens": 16000,
         "cache_write_tokens": 2000, "output_tokens": 1000, "estimated_cost_usd": 3.00, "actual_cost_usd": 2.50},
        {"id": "s3", "started_at": NOW - 1 * DAY, "api_call_count": 0},                  # never called a model
        {"id": "s4", "started_at": NOW - 100 * DAY},                                      # outside the window
        {"id": "s5", "started_at": "2026-11-10"},                                         # a date string, not a time
    ])
    data = usage.load(home, NOW - 7 * DAY, NOW + 1)
    s = usage.whole(data)
    assert s["sessions"] == 2 and s["calls"] == 30, s
    assert s["input_tokens"] == 30000 and abs(s["avg_context"] - 1000) < 1e-9, s
    assert abs(s["read_share"] - 0.8) < 1e-9 and abs(s["write_share"] - 0.1) < 1e-9, s
    assert abs(s["cost"] - 3.50) < 1e-9, "a recorded actual cost wins over the estimate"
    assert abs(s["cost_per_call"] - 3.5 / 30) < 1e-9 and abs(s["writes_per_call"] - 100) < 1e-9, s
    assert s["output_tokens"] == 1600, "output and reasoning tokens are added"
    print("PASS  totals: calls, token shares, cost (actual beats estimate), exclusions")


def t_pauses_and_tools(tmp: Path) -> None:
    home = tmp / "pauses"
    msgs = []
    base = NOW - 3 * DAY
    for ts in (0, 100, 401, 4001, 7602, 7602):                       # gaps 100, 301, 3600, 3601, 0
        msgs.append(("s1", "assistant", None, "x", base + ts))
    msgs.append(("s1", "assistant", None, "x", base + 7000))          # goes back in time: ignored
    msgs.append(("s2", "assistant", None, "x", base + 10 * DAY / 100))  # a different session: no gap to s1
    msgs += [("s1", "tool", "read_file", "a" * 400, base + 10), ("s1", "tool", "read_file", "b" * 800, base + 11),
             ("s1", "tool", "terminal", "c" * 40, base + 12), ("s1", "user", None, "hi", base + 13)]
    make_state(home, sessions=[{"id": "s1", "started_at": base}, {"id": "s2", "started_at": base + 5}], messages=msgs)
    data = usage.load(home, NOW - 7 * DAY, NOW + 1)
    g = data.gaps["s1"]
    assert g[1] == 2 and g[2] == 1, g          # 301 s and exactly 3600 s are 5-60 min; 3601 s is over an hour
    assert data.pauses_known
    s = usage.whole(data)
    assert s["pauses_5_60"] == 2 and s["pauses_over_60"] == 1, s
    rf = s["tools"]["read_file"]
    assert rf["calls"] == 2 and abs(rf["avg_tokens"] - 150) < 1e-9 and abs(rf["calls_per_session"] - 1.0) < 1e-9, rf
    assert "terminal" not in s["tools"], "only the tracked tools are listed"
    only = usage.whole(usage.load(home, NOW - 7 * DAY, NOW + 1, ("terminal",)))
    assert list(only["tools"]) == ["terminal"]
    # One row stamped earlier than its predecessor must not make the next gap look longer than it was.
    other = tmp / "pauses-order"
    steps = [("o1", "assistant", None, "x", base + ts) for ts in (0, 10000, 50, 10100)]    # 10000 s pause, then 100 s
    steps += [("o2", "assistant", None, "x", base + ts) for ts in (0, 60, 30, 340)]         # 280 s is no pause at all
    make_state(other, sessions=[{"id": "o1", "started_at": base}, {"id": "o2", "started_at": base}], messages=steps)
    g = usage.load(other, NOW - 7 * DAY, NOW + 1).gaps
    assert g["o1"][1:] == [0, 1] and g["o2"][1:] == [0, 0], g
    print("PASS  pauses (301 s, 3600 s, 3601 s, zero and backwards gaps) and tool statistics")


def t_weeks(tmp: Path) -> None:
    home = tmp / "weeks"
    make_state(home, sessions=[
        {"id": "sun", "started_at": at(2026, 11, 1, 23, 30)},     # Sunday night: the week of 10-26
        {"id": "mon", "started_at": at(2026, 11, 2, 0, 30)},      # Monday just after midnight: the week of 11-02
        {"id": "tue", "started_at": at(2026, 11, 10, 9, 0)},
        {"id": "old", "started_at": at(2026, 10, 18, 9, 0)},      # before the 3-week window
    ])
    result = usage.build(home, now=NOW, weeks=3)
    rows = result["weeks"]
    assert [r["week"] for r in rows] == ["2026-10-26", "2026-11-02", "2026-11-09"], [r["week"] for r in rows]
    assert [r["sessions"] for r in rows] == [1, 1, 1], rows
    four = usage.build(home, now=NOW, weeks=4)["weeks"]
    assert [r["week"] for r in four] == ["2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09"]
    assert [r["sessions"] for r in four] == [0, 1, 1, 1], "Sunday 10-18 belongs to the week of 10-12, outside four weeks"
    five = usage.build(home, now=NOW, weeks=5)["weeks"]
    assert [r["sessions"] for r in five] == [1, 0, 1, 1, 1]
    quiet = usage.build(home, now=NOW + 30 * DAY, weeks=2)["weeks"]
    assert [r["sessions"] for r in quiet] == [0, 0] and quiet[0]["cost_per_call"] is None
    print("PASS  weeks start on Monday, empty weeks are kept")


def t_other_versions(tmp: Path) -> None:
    # Fewer columns, no messages table: still works, says what is missing.
    home = tmp / "lean"
    home.mkdir()
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, api_call_count INTEGER, input_tokens INTEGER)")
    conn.execute("INSERT INTO sessions VALUES ('a', ?, 5, 500)", (NOW - DAY,))
    conn.commit()
    conn.close()
    result = usage.build(home, now=NOW, weeks=2)
    s = result["summary"]
    assert s["calls"] == 5 and s["cost"] is None and s["cost_per_call"] is None and not result["pauses_known"], s
    text = "\n".join(usage.render(result))
    assert "no message timestamps" in text, text
    # a sessions table without the call counter cannot be measured
    other = tmp / "nocalls"
    other.mkdir()
    conn = sqlite3.connect(other / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL)")
    conn.commit()
    conn.close()
    try:
        usage.build(other, now=NOW)
        raise AssertionError("expected UsageError")
    except usage.UsageError as exc:
        assert "api_call_count" in str(exc), exc
    # not a database at all / no sessions table
    junk = tmp / "junk"
    junk.mkdir()
    (junk / "state.db").write_bytes(b"this is not a database" * 50)
    code, out = capture(usage.run, junk, out=lambda t: print(t))
    assert code == 1 and out.startswith("Usage: "), out
    empty = tmp / "emptydb"
    empty.mkdir()
    sqlite3.connect(empty / "state.db").close()
    code, out = capture(usage.run, empty, out=lambda t: print(t))
    assert code == 1 and "sessions" in out, out
    print("PASS  older/leaner databases are measured as far as possible; unusable ones are explained")


def t_readonly_and_paths(tmp: Path) -> None:
    home = tmp / "odd dir#1%20 x"
    path = make_state(home, sessions=[{"id": "a", "started_at": NOW - DAY}], messages=[("a", "assistant", None, "x", NOW - DAY)])
    before = sha(path)
    stamp = path.stat().st_mtime_ns
    code, out = capture(usage.run, home, out=lambda t: print(t), now=NOW)
    assert code == 0 and "Usage  " in out, out
    code, out = capture(usage.run, home, as_json=True, out=lambda t: print(t), now=NOW)
    json.loads(out)
    assert sha(path) == before and path.stat().st_mtime_ns == stamp, "state.db must never change"
    assert sorted(p.name for p in home.iterdir()) == ["state.db"], "no side files for a read-only command"
    missing = tmp / "none"
    missing.mkdir()
    code, out = capture(usage.run, missing, out=lambda t: print(t))
    assert code == 1 and "no state.db" in out and not (missing / "plugin-data").exists(), out
    print("PASS  state.db is never modified; odd folder names work; nothing is created by looking")


def t_busy_and_wal(tmp: Path) -> None:
    # A WAL database (Hermes' own mode) is read while a writer has an open transaction: only committed rows count,
    # and the only files that may appear next to state.db are SQLite's own side files.
    home = tmp / "wal"
    path = make_state(home, sessions=[{"id": "w1", "started_at": NOW - DAY}], messages=[("w1", "assistant", None, "x", NOW - DAY)])
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    insert = "INSERT INTO sessions (id, source, model, started_at, api_call_count) VALUES (?, 'desktop', 'm', ?, ?)"
    writer.execute(insert, ("w2", NOW - DAY, 5))
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(insert, ("w3", NOW - DAY, 7))                                  # never committed
    code, out = capture(usage.run, home, as_json=True, out=lambda t: print(t), now=NOW)
    assert code == 0, out
    seen = json.loads(out)["summary"]
    assert seen["sessions"] == 2 and seen["calls"] == 15, seen
    names = sorted(p.name for p in home.iterdir())
    assert set(names) <= {"state.db", "state.db-wal", "state.db-shm"}, names
    writer.rollback()
    writer.close()

    # A database another program holds locked is reported, not read half-written.
    busy = tmp / "busy"
    path = make_state(busy, sessions=[{"id": "b1", "started_at": NOW - DAY}])
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    saved, usage.OPEN_TIMEOUT = usage.OPEN_TIMEOUT, 0.2
    try:
        code, out = capture(usage.run, busy, out=lambda t: print(t), now=NOW)
    finally:
        usage.OPEN_TIMEOUT = saved
        holder.execute("ROLLBACK")
        holder.close()
    assert code == 1 and "locked" in out and "try again" in out, out
    code, out = capture(usage.run, busy, out=lambda t: print(t), now=NOW)
    assert code == 0 and "Usage  busy" in out, out
    print("PASS  a WAL database is read while Hermes writes; a locked one is reported")


def t_unpriced(tmp: Path) -> None:
    home = tmp / "unpriced"
    sessions = []
    for sid, cost, status in (("p1", 1.0, "estimated"), ("p2", 0.0, "included"), ("p3", 0.0, "unknown"),
                              ("p4", 2.0, "unknown"), ("p5", 5.0, "included"), ("p6", 3.0, None)):
        sessions.append({"id": sid, "started_at": NOW - DAY, "model": "m-a", "api_call_count": 10,
                         "estimated_cost_usd": cost, "cost_status": status})
    make_state(home, sessions=sessions)
    s = usage.whole(usage.load(home, NOW - 7 * DAY, NOW + 1))
    # priced: p1 (estimated), p4 (unknown but with an estimate), p6 (no status, a number); not priced: included and
    # unknown-without-estimate sessions, which would otherwise count as free and drag the cost per call down
    assert s["calls"] == 60 and s["priced_calls"] == 30 and s["unpriced_calls"] == 30, s
    assert abs(s["cost"] - 6.0) < 1e-9 and abs(s["cost_per_call"] - 0.2) < 1e-9, s
    model = s["models"]["m-a"]
    assert model["priced_calls"] == 30 and abs(model["cost_per_call"] - 0.2) < 1e-9, model
    text = "\n".join(usage.render(usage.build(home, now=NOW, weeks=2)))
    assert "30 of 60 calls have no recorded price" in text, text
    # nothing priced at all: no cost, and the report still says why
    only = tmp / "included-only"
    make_state(only, sessions=[{"id": "i1", "started_at": NOW - DAY, "estimated_cost_usd": 0.0, "cost_status": "included"}])
    s = usage.whole(usage.load(only, NOW - 7 * DAY, NOW + 1))
    assert s["cost"] is None and s["cost_per_call"] is None and s["unpriced_calls"] == 10, s
    print("PASS  plan-included and unpriced chats are left out of the cost figures, and counted")


def baseline_world(home: Path, after_cpc: float, after_sessions: int = 6) -> float:
    """Four weeks of history, a marker on Monday 11-09, and a week after it. Returns the marker time."""
    marker_at = at(2026, 11, 9, 9, 0)
    sessions, k = [], 0
    for week_start, cpc in ((at(2026, 10, 12), 0.10), (at(2026, 10, 19), 0.12), (at(2026, 10, 26), 0.08), (at(2026, 11, 2), 0.11)):
        for i in range(2):
            k += 1
            sessions.append({"id": "b%d" % k, "model": "model-a", "started_at": week_start + (i + 1) * DAY, "api_call_count": 75,
                             "estimated_cost_usd": 75 * cpc})
    sessions.append({"id": "bB", "model": "model-b", "started_at": at(2026, 11, 3), "api_call_count": 60, "estimated_cost_usd": 60 * 0.02})
    for i in range(after_sessions):
        sessions.append({"id": "a%d" % i, "model": "model-a", "started_at": marker_at + (i + 1) * 3600 * 5, "api_call_count": 100,
                         "estimated_cost_usd": 100 * after_cpc})
    sessions.append({"id": "aC", "model": "model-c", "started_at": marker_at + DAY, "api_call_count": 50, "estimated_cost_usd": 0.5})
    make_state(home, sessions=sessions)
    return marker_at


def t_markers(tmp: Path) -> None:
    home = tmp / "mark"
    marker_at = baseline_world(home, after_cpc=0.07)
    (home / "config.yaml").write_text("compression:\n  threshold: 0.35   # a setting that is not about caching\n", "utf-8")
    code, out = capture(usage.run, home, mark_label="cache-ttl", now=marker_at, out=lambda t: print(t))
    assert code == 0 and "Marked" in out and "Baseline kept" in out, out
    stored = json.loads(usage.markers_path(home).read_text("utf-8"))["markers"][0]
    assert stored["before"]["sessions"] == 9 and stored["before"]["calls"] == 75 * 8 + 60, stored["before"]
    assert str(stored["settings"]["compression.threshold"]) == "0.35", stored["settings"]
    assert usage.build(home, now=marker_at + DAY)["comparison"]["scope"] is None, "not a cache setting: all models count"
    kept = stored["before_week_models"]
    assert len(kept) == 4 and all("model-a" in week["models"] for week in kept), kept
    assert kept[0]["models"]["model-a"] == [150.0, 0.10] and kept[1]["models"]["model-a"][1] == 0.12, kept
    # the same label twice, and a bad label, are refused without touching the file
    code, out = capture(usage.run, home, mark_label="cache-ttl", now=marker_at, out=lambda t: print(t))
    assert code == 1 and "already exists" in out, out
    code, out = capture(usage.run, home, mark_label="bad label!", now=marker_at, out=lambda t: print(t))
    assert code == 1 and "label" in out, out
    for odd in ("x\n", "a" * 41, "", "two words"):          # a newline at the end is not allowed either
        code, out = capture(usage.run, home, mark_label=odd, now=marker_at, out=lambda t: print(t))
        assert code == 1 or not odd, (odd, out)
    assert len(usage.read_markers(home)) == 1

    now = marker_at + 7 * DAY
    result = usage.build(home, now=now, weeks=4)
    c = result["comparison"]
    assert c["label"] == "cache-ttl" and abs(c["days_after"] - 7.0) < 1e-6, c
    assert c["after"]["sessions"] == 7 and c["after"]["calls"] == 650 and c["enough"], c["after"]
    lfl = c["like_for_like"]
    assert lfl["models"] == ["model-a"], "only the model used on both sides counts"
    before_a = (75 * (0.10 + 0.12 + 0.08 + 0.11) * 2) / (75 * 8)
    assert abs(lfl["before_per_call"] - before_a) < 1e-9 and abs(lfl["after_per_call"] - 0.07) < 1e-9, lfl
    assert abs(lfl["change"] - (0.07 / before_a - 1.0)) < 1e-9
    lo, hi = c["weekly_swing"]
    assert abs(lo - 0.08) < 1e-9 and abs(hi - 0.12) < 1e-9, c["weekly_swing"]
    text = "\n".join(usage.render(result))
    # 0.07 is below every one of the four weeks (0.08-0.12) but not outside what four weeks allow for chance, so it is no
    # "real drop" (out of the min..max of four weeks, a figure from the same distribution lands 2 times in 5)
    assert "Before / after" in text and "same models only (1)" in text and "inside that range" in text, text
    assert "real drop" not in text and "points to" not in text, text
    assert "An idea worth measuring" not in text, "no suggestion once a change is being measured"
    assert all(len(line) <= 120 for line in text.splitlines()), max(text.splitlines(), key=len)

    # a result far below that is called a lead, in words that do not say "real"
    home1 = tmp / "mark1"
    marker1 = baseline_world(home1, after_cpc=0.03)
    capture(usage.run, home1, mark_label="x", now=marker1, out=lambda t: print(t))
    text = "\n".join(usage.render(usage.build(home1, now=marker1 + 7 * DAY)))
    assert "below that range" in text and "points to a lower cost" in text and "real drop" not in text, text

    # a result inside the usual swing is said to be inside it
    home2 = tmp / "mark2"
    marker2 = baseline_world(home2, after_cpc=0.10)
    capture(usage.run, home2, mark_label="x", now=marker2, out=lambda t: print(t))
    text = "\n".join(usage.render(usage.build(home2, now=marker2 + 7 * DAY)))
    assert "inside that range" in text, text

    # too few sessions after the marker: no verdict
    home3 = tmp / "mark3"
    marker3 = baseline_world(home3, after_cpc=0.07, after_sessions=2)
    capture(usage.run, home3, mark_label="x", now=marker3, out=lambda t: print(t))
    text = "\n".join(usage.render(usage.build(home3, now=marker3 + 2 * DAY)))
    assert "Too early to judge" in text and "Cost per call" not in text, text

    # right after the marker nothing has happened yet: say "too early", and do not blame missing timestamps
    home4 = tmp / "mark4"
    marker4 = baseline_world(home4, after_cpc=0.07, after_sessions=0)
    make_state_messages = sqlite3.connect(str(home4 / "state.db"))
    make_state_messages.execute("INSERT INTO messages (session_id, role, timestamp) VALUES ('b1', 'assistant', ?)", (marker4 - DAY,))
    make_state_messages.commit()
    make_state_messages.close()
    capture(usage.run, home4, mark_label="x", now=marker4 + 2 * DAY, out=lambda t: print(t))   # marked after the last session
    text = "\n".join(usage.render(usage.build(home4, now=marker4 + 2 * DAY + 3600)))
    assert "Too early to judge: 0 sessions" in text and "timestamps are missing" not in text, text

    # an unknown marker is explained; a second marker is chosen with --vs, the newest otherwise
    try:
        usage.build(home, now=now, vs="nope")
        raise AssertionError("expected UsageError")
    except usage.UsageError as exc:
        assert "cache-ttl" in str(exc), exc
    usage.mark(home, "later", now=marker_at + 3 * DAY)
    assert usage.build(home, now=now)["comparison"]["label"] == "later"
    assert usage.build(home, now=now, vs="cache-ttl")["comparison"]["label"] == "cache-ttl"
    print("PASS  markers: baseline kept, like-for-like by model, verdict wording, too-early, --vs, refusals")


def scope_world(home: Path, claude_after: int, other_after: int) -> float:
    """Four weeks of Claude and non-Claude chats, then a marker, then chats on one or both kinds of model."""
    marker_at = at(2026, 11, 9, 9, 0)
    sessions, k = [], 0
    for week_start, cpc in ((at(2026, 10, 12), 0.10), (at(2026, 10, 19), 0.12), (at(2026, 10, 26), 0.08), (at(2026, 11, 2), 0.11)):
        for i in range(2):
            k += 1
            sessions.append({"id": "c%d" % k, "model": "anthropic/claude-x", "started_at": week_start + (i + 1) * DAY,
                             "api_call_count": 75, "estimated_cost_usd": 75 * cpc})
            sessions.append({"id": "h%d" % k, "model": "tencent/hy", "started_at": week_start + (i + 1) * DAY + 3600,
                             "api_call_count": 75, "estimated_cost_usd": 75 * 0.01})
    for i in range(other_after):
        sessions.append({"id": "ho%d" % i, "model": "tencent/hy", "started_at": marker_at + (i + 1) * 5 * 3600,
                         "api_call_count": 100, "estimated_cost_usd": 100 * 0.01})
    for i in range(claude_after):
        sessions.append({"id": "ca%d" % i, "model": "anthropic/claude-x", "started_at": marker_at + (i + 1) * 5 * 3600 + 600,
                         "api_call_count": 100, "estimated_cost_usd": 100 * 0.06})
    make_state(home, sessions=sessions)
    (home / "config.yaml").write_text("prompt_caching:\n  cache_ttl: auto\n", "utf-8")
    return marker_at


def t_cache_scope(tmp: Path) -> None:
    # Chats since the change were all on a non-Claude model: plenty of sessions, but nothing for a cache setting to act on.
    home = tmp / "scope1"
    marker_at = scope_world(home, claude_after=0, other_after=7)
    marker = usage.mark(home, "ttl", now=marker_at)
    assert len(marker["before_week_models"]) == 4 and all("anthropic/claude-x" in w["models"] for w in marker["before_week_models"])
    result = usage.build(home, now=marker_at + 7 * DAY)
    c = result["comparison"]
    assert c["scope"] == "claude" and c["scope_sessions"] == 0 and c["scope_calls"] == 0, c
    assert c["after"]["sessions"] == 7 and c["after"]["calls"] == 700 and not c["enough"], "non-Claude chats cannot judge a cache setting"
    text = "\n".join(usage.render(result))
    assert "mainly changes how Claude models are cached" in text and "0 of 700 calls (0%)" in text, text
    assert "Too early to judge: 0 Claude sessions, 0 Claude calls" in text, text
    assert "nothing to act on yet" in text, text

    # Claude chats arrive: the verdict uses Claude models only, against the usual swing of Claude weeks.
    home = tmp / "scope2"
    marker_at = scope_world(home, claude_after=6, other_after=7)
    usage.mark(home, "ttl", now=marker_at)
    result = usage.build(home, now=marker_at + 7 * DAY)
    c = result["comparison"]
    assert c["scope_sessions"] == 6 and c["scope_calls"] == 600 and c["enough"], c
    lfl = c["like_for_like"]
    assert lfl["models"] == ["anthropic/claude-x"] and abs(lfl["after_per_call"] - 0.06) < 1e-9, lfl
    lo, hi = c["weekly_swing"]
    assert abs(lo - 0.08) < 1e-9 and abs(hi - 0.12) < 1e-9, c["weekly_swing"]
    text = "\n".join(usage.render(result))
    assert "same Claude models only (1)" in text and "inside that range" in text and "Too early" not in text, text
    assert not any(phrase in text for phrase in CLAIMS), text
    assert all(len(line) <= 120 for line in text.splitlines()), max(text.splitlines(), key=len)

    # A marker saved by 1.2.0 (no per-model weeks, no priced-call split) gets the same yardstick from state.db.
    path = usage.markers_path(home)
    stored = json.loads(path.read_text("utf-8"))
    old = stored["markers"][0]
    del old["before_week_models"]
    for m in old["before"]["models"].values():
        del m["priced_calls"]
    path.write_text(json.dumps(stored), "utf-8")
    again = usage.build(home, now=marker_at + 7 * DAY)["comparison"]["weekly_swing"]
    assert again is not None and abs(again[0] - 0.08) < 1e-9 and abs(again[1] - 0.12) < 1e-9, again
    # ... and when Hermes has pruned that history before a marker was upgraded, the marker's own summary stays and there
    # is no usual range to quote. (The first report above saved the rebuilt baseline, so the older form is put back.)
    path.write_text(json.dumps(stored), "utf-8")
    keep = sqlite3.connect(str(home / "state.db"))
    keep.execute("DELETE FROM sessions WHERE started_at < ?", (marker_at - DAY,))
    keep.commit()
    keep.close()
    pruned = usage.build(home, now=marker_at + 7 * DAY)["comparison"]
    assert pruned["weekly_swing"] is None and pruned["before"]["calls"] == old["before"]["calls"], pruned
    assert "cannot be rebuilt for this older marker" in "\n".join(usage.render(usage.build(home, now=marker_at + 7 * DAY)))
    assert usage.is_claude("anthropic/claude-opus-5") and usage.is_claude("claude-3") and not usage.is_claude("tencent/hy3")
    assert not usage.is_claude(None) and not usage.is_claude("gpt-6")
    print("PASS  a cache-lifetime marker counts Claude models only, says so, and waits for Claude chats")


def mix_world(home: Path, base, after) -> float:
    """base: [(model, [cost per call in each of the four baseline weeks], calls per week)];
    after: [(model, cost per call, sessions, calls per session)] in the week after a marker on Monday 11-09."""
    marker_at = at(2026, 11, 9, 9, 0)
    sessions, k = [], 0
    for week, week_start in enumerate((at(2026, 10, 12), at(2026, 10, 19), at(2026, 10, 26), at(2026, 11, 2))):
        for model, costs, calls in base:
            k += 1
            sessions.append({"id": "b%d" % k, "model": model, "started_at": week_start + DAY + k * 60, "api_call_count": calls,
                             "estimated_cost_usd": calls * costs[week]})
    for model, cpc, n, calls in after:
        for i in range(n):
            k += 1
            sessions.append({"id": "a%d" % k, "model": model, "started_at": marker_at + (i + 1) * 5 * 3600 + k,
                             "api_call_count": calls, "estimated_cost_usd": calls * cpc})
    make_state(home, sessions=sessions)
    return marker_at


def verdict(tmp: Path, name: str, base, after):
    home = tmp / name
    marker_at = mix_world(home, base, after)
    usage.mark(home, "x", now=marker_at)
    result = usage.build(home, now=marker_at + 7 * DAY)
    text = "\n".join(usage.render(result))
    assert all(len(line) <= 120 for line in text.splitlines()), max(text.splitlines(), key=len)
    return result["comparison"], text


def t_verdicts(tmp: Path) -> None:
    a_weeks, b_weeks = [0.10, 0.12, 0.08, 0.11], [0.018, 0.022, 0.020, 0.021]
    flat_b = [0.02] * 4
    # A mixed baseline (a dear and a cheap model) and afterwards only the dear one, 5% lower than its usual swing:
    # the usual range is that of the dear model alone, not of the mix (0.05-0.07), so this is inside it, not a "rise".
    c, text = verdict(tmp, "v1", [("model-a", a_weeks, 150), ("model-b", flat_b, 150)], [("model-a", 0.09, 6, 100)])
    assert c["like_for_like"]["models"] == ["model-a"] and c["enough"], c
    lo, hi = c["weekly_swing"]
    assert abs(lo - 0.08) < 1e-9 and abs(hi - 0.12) < 1e-9, c["weekly_swing"]
    assert "inside that range" in text and not any(phrase in text for phrase in CLAIMS), text
    # ... and a drop on the dear model far below what its four weeks allow is said to point to a lower cost (the old
    # all-model range hid it), as a lead: four weeks of history is thin
    c, text = verdict(tmp, "v1b", [("model-a", a_weeks, 150), ("model-b", flat_b, 150)], [("model-a", 0.03, 6, 100)])
    assert "below that range" in text and "points to a lower cost" in text and "4 weeks of history is thin" in text, text
    assert "real drop" not in text, text
    # Only the cheap model after the marker, at its usual price: no change, not "a drop" because the mix got cheaper.
    c, text = verdict(tmp, "v1c", [("model-a", a_weeks, 150), ("model-b", b_weeks, 150)], [("model-b", 0.02, 6, 100)])
    assert c["like_for_like"]["models"] == ["model-b"] and abs(c["like_for_like"]["change"]) < 0.06, c["like_for_like"]
    assert "inside that range" in text and not any(phrase in text for phrase in CLAIMS), text
    # A pure model switch has no like-for-like figure, and so no verdict at all.
    c, text = verdict(tmp, "v2", [("model-a", a_weeks, 150)], [("model-b", 0.02, 6, 100)])
    assert c["like_for_like"] is None and "no model" in c["like_for_like_gap"], c
    assert "No verdict: no model with a recorded price was used both before and after." in text, text
    assert not any(phrase in text for phrase in CLAIMS) and "19 of 20" not in text, text
    # The same single model but with hardly any baseline calls: the reason is the thin baseline, not "models differ".
    c, text = verdict(tmp, "v3", [("model-a", [0.10] * 4, 5)], [("model-a", 0.09, 6, 100)])
    assert c["like_for_like"] is None and "fewer than 30 priced calls" in c["like_for_like_gap"], c
    assert "No verdict: the models used on both sides have fewer than 30 priced calls" in text, text
    assert "models used differ" not in text and "real drop" not in text, text
    # Plenty of calls in total, but the like-for-like figure rests on one small session: no verdict yet.
    c, text = verdict(tmp, "v4", [("model-a", a_weeks, 150)], [("model-c", 0.5, 6, 100), ("model-a", 0.06, 1, 30)])
    assert c["enough"] and c["like_for_like"]["calls"] == 30 and c["like_for_like"]["sessions"] == 1, c
    assert "No verdict yet: that figure rests on 30 priced calls in 1 priced chat." in text, text
    assert not any(phrase in text for phrase in CLAIMS), text
    # A marker whose weeks are unknown can still show the numbers, but quotes no usual swing.
    home = tmp / "v1"
    path = usage.markers_path(home)
    stored = json.loads(path.read_text("utf-8"))
    stored["markers"][0]["before_week_models"] = []
    path.write_text(json.dumps(stored), "utf-8")
    text = "\n".join(usage.render(usage.build(home, now=at(2026, 11, 9, 9, 0) + 7 * DAY)))
    assert "No verdict: too few earlier weeks" in text and "a real drop" not in text and "a real rise" not in text, text
    print("PASS  verdicts: same models and same mix only; no verdict for a model switch or a thin figure")


def t_marker_file(tmp: Path) -> None:
    home = tmp / "mfile"
    marker_at = baseline_world(home, after_cpc=0.07)
    usage.mark(home, "one", now=marker_at)
    path = usage.markers_path(home)
    good = path.read_bytes()
    # a byte-order mark (Notepad adds one) is tolerated
    path.write_bytes(b"\xef\xbb\xbf" + good)
    assert [m["label"] for m in usage.read_markers(home)] == ["one"]
    usage.mark(home, "two", now=marker_at + 1)
    assert [m["label"] for m in usage.read_markers(home)] == ["one", "two"]
    path.write_bytes(good)
    # a file that cannot be read is reported and left alone: the report still works, --mark refuses
    path.write_text("{ this is not json", "utf-8")
    damaged = path.read_bytes()
    result = usage.build(home, now=marker_at + 7 * DAY)
    assert "comparison" not in result and "not valid JSON" in result["markers_note"], result.get("markers_note")
    assert "note: usage-markers.json is not valid JSON" in "\n".join(usage.render(result))
    code, out = capture(usage.run, home, mark_label="three", now=marker_at + 2, out=lambda t: print(t))
    assert code == 1 and "usage-markers.json" in out and "not valid JSON" in out and "move that file aside" in out, out
    assert path.read_bytes() == damaged and not Path(str(path) + ".lock").exists()
    # entries with the wrong types are skipped instead of crashing every report; the good one keeps working
    entries = json.loads(good.decode("utf-8"))["markers"]
    bad = [{"label": "text-at", "at": "yesterday"}, {"label": "null-at", "at": None}, {"label": "list-before", "at": marker_at, "before": [1, 2]},
           {"label": 5, "at": marker_at}, "junk", {"label": "no-history", "at": marker_at - DAY, "before": None, "before_week_models": "x"},
           {"label": "bad-model", "at": marker_at - DAY, "before": {"sessions": 1, "calls": 2, "models": {"m": {"calls": "many"}}}},
           {"label": "bad-tools", "at": marker_at - DAY, "before": {"sessions": 1, "calls": 2, "tools": {"read_file": {"avg_tokens": "big"}}}}]
    path.write_text(json.dumps({"markers": bad + entries}), "utf-8")
    markers, note = usage.load_markers(home)
    assert [m["label"] for m in markers] == ["no-history", "one"] and note and note.startswith("7 entries"), (markers, note)
    result = usage.build(home, now=marker_at + 7 * DAY, vs="one")
    assert result["comparison"]["label"] == "one" and "7 entries" in result["markers_note"]
    assert "Before / after" in "\n".join(usage.render(result))
    lone = "\n".join(usage.render(usage.build(home, now=marker_at + 7 * DAY, vs="no-history")))
    assert "nothing to compare against" in lone, lone
    code, out = capture(usage.run, home, mark_label="again", now=marker_at + 3, out=lambda t: print(t))
    assert code == 1 and "could not be used" in out, "entries that cannot be read are not silently dropped on the next write"
    # several markers set at the same moment all survive
    path.unlink()
    import threading
    errors = []

    def mark_it(i):
        try:
            usage.mark(home, "t%d" % i, now=marker_at + 10 + i)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=mark_it, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert sorted(m["label"] for m in usage.read_markers(home)) == sorted("t%d" % i for i in range(8))
    assert not Path(str(path) + ".lock").exists(), "the lock file is removed"
    # a lock left behind by a run that died does not block for ever
    lock = Path(str(path) + ".lock")
    lock.write_text("", "utf-8")
    old = time.time() - 600
    os.utime(str(lock), (old, old))
    usage.mark(home, "after-stale-lock", now=marker_at + 99)
    assert not lock.exists()
    print("PASS  marker file: BOM, damage, wrong types, simultaneous --mark, stale lock")


def t_arguments(tmp: Path) -> None:
    home = tmp / "args"
    baseline_world(home, after_cpc=0.07)

    def say(t):
        print(t)

    for kwargs, expect in (({"weeks": 0}, "--weeks"), ({"weeks": -3}, "--weeks"), ({"weeks": 5000}, "--weeks"),
                           ({"weeks": 99999999999}, "--weeks"), ({"weeks": "many"}, "--weeks"),
                           ({"mark_label": "x", "baseline_days": 0}, "--baseline-days"),
                           ({"mark_label": "x", "baseline_days": -5}, "--baseline-days"),
                           ({"mark_label": "x", "baseline_days": 100000}, "--baseline-days"),
                           ({"since": "next tuesday"}, "--since"), ({"since": "2026-13-45"}, "--since")):
        code, out = capture(usage.run, home, out=say, now=NOW, **kwargs)
        assert code == 1 and expect in out and "Traceback" not in out, (kwargs, out)
    for date in ("0001-01-01", "1900-01-01", "9999-12-31"):          # what a computer cannot represent is a message
        code, out = capture(usage.run, home, since=date, out=say, now=NOW)
        assert code in (0, 1) and "Traceback" not in out, (date, out)
    assert not (home / "plugin-data").exists(), "refused options leave nothing behind"
    # a clock outside the years a marker can be read back from: refused, instead of saving an entry the reader would skip
    for odd_now in (0.0, 5e9, float("nan"), float("inf")):
        code, out = capture(usage.run, home, mark_label="x", out=say, now=odd_now)
        assert code == 1 and "clock" in out and not (home / "plugin-data").exists(), (odd_now, out)
    # --mark: a folder that does not exist, a state.db that cannot be read, a data folder that is a file
    nowhere = tmp / "nowhere"
    code, out = capture(usage.run, nowhere, mark_label="x", out=say, now=NOW)
    assert code == 1 and "no such Hermes folder" in out and not nowhere.exists(), out
    drift = tmp / "drift"
    drift.mkdir()
    conn = sqlite3.connect(drift / "state.db")
    conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, calls INTEGER)")
    conn.execute("INSERT INTO sessions VALUES ('a', ?, 5)", (NOW - DAY,))
    conn.commit()
    conn.close()
    code, out = capture(usage.run, drift, mark_label="x", out=say, now=NOW)
    assert code == 1 and "api_call_count" in out and not (drift / "plugin-data").exists(), out
    junk = tmp / "junkmark"
    junk.mkdir()
    (junk / "state.db").write_bytes(b"this is not a database" * 50)
    code, out = capture(usage.run, junk, mark_label="x", out=say, now=NOW)
    assert code == 1 and not (junk / "plugin-data").exists(), out
    blocked = tmp / "blocked"
    make_state(blocked, sessions=[{"id": "a", "started_at": NOW - DAY}])
    (blocked / "plugin-data").write_text("a file where a folder should be", "utf-8")
    code, out = capture(usage.run, blocked, mark_label="x", out=say, now=NOW)
    assert code == 1 and "could not save the marker" in out and "Traceback" not in out, out
    # a config.yaml that is not UTF-8 only costs the settings line
    ansi = tmp / "ansi"
    make_state(ansi, sessions=[{"id": "a", "started_at": NOW - DAY}])
    (ansi / "config.yaml").write_bytes("prompt_caching:\n  cache_ttl: caf\xe9\n".encode("latin-1"))
    code, out = capture(usage.run, ansi, now=NOW, out=say)
    assert code == 0 and "Usage  ansi" in out, out
    assert usage.read_settings(ansi) == {}
    print("PASS  out-of-range options, unreadable state.db or config, and an unwritable data folder end in messages")


def t_formatting(tmp: Path) -> None:
    assert usage._k(999950) == "1.0M" and usage._k(1000) == "1.0k" and usage._k(999) == "999" and usage._k(None) == "-"
    assert usage._k(1234567) == "1.2M" and usage._k(0) == "0"
    assert usage._change(5, 4.998) == "0%" and usage._change(5, 4) == "-20%" and usage._change(0, 3) == "" and usage._change(-1, 3) == ""
    assert usage._signed(0.003) == "0%" and usage._signed(-0.1) == "-10%" and usage._signed(0.1) == "+10%"
    home = tmp / ("a-profile-folder-with-a-rather-long-name-" + "x" * 40)
    make_state(home, sessions=[{"id": "a", "started_at": NOW - DAY}])
    text = "\n".join(usage.render(usage.build(home, now=NOW)))
    assert "the last row is the week so far" in text and "By week the chat started" in text, text
    assert all(len(line) <= 120 for line in text.splitlines()[2:]), max(text.splitlines()[2:], key=len)
    for value in (float("nan"), float("inf")):                         # corrupt numbers do not crash the meter
        lone = tmp / ("nan%d" % int(value != value))
        make_state(lone, sessions=[{"id": "a", "started_at": NOW - DAY, "estimated_cost_usd": value, "input_tokens": value}])
        s = usage.whole(usage.load(lone, NOW - 7 * DAY, NOW + 1))
        assert s["cost"] is None and s["input_tokens"] == 9000 + 1000, s
        json.dumps(s, allow_nan=False)
    print("PASS  number formatting, header and week labels, corrupt numbers")


def t_marker_without_history(tmp: Path) -> None:
    home = tmp / "fresh"
    make_state(home, sessions=[{"id": "n1", "started_at": NOW + 3600, "api_call_count": 40}])
    marker = usage.mark(home, "first", now=NOW)
    assert marker["before"] is None
    text = "\n".join(usage.render(usage.build(home, now=NOW + DAY)))
    assert "nothing to compare against" in text, text
    # a profile with no state.db yet can still be marked
    bare = tmp / "bare"
    bare.mkdir()
    usage.mark(bare, "first", now=NOW)
    assert usage.read_markers(bare)[0]["before"] is None
    print("PASS  a marker can be set before there is any history")


def t_idea(tmp: Path) -> None:
    home = tmp / "idea"
    sessions = [{"id": "s%d" % i, "started_at": NOW - (i + 1) * DAY, "api_call_count": 100, "cache_write_tokens": 2000,
                 "input_tokens": 500, "cache_read_tokens": 20000, "model": "anthropic/claude-x"} for i in range(6)]
    msgs = []
    for sess in sessions:
        for j in range(8):                                          # 7 pauses of 10 minutes per session
            msgs.append((sess["id"], "assistant", None, "x", sess["started_at"] + j * 600))
    make_state(home, sessions=sessions, messages=msgs)
    text = "\n".join(usage.render(usage.build(home, now=NOW)))
    assert "An idea worth measuring" in text and "hermes config set prompt_caching.cache_ttl auto" in text, text
    assert all(len(line) <= 120 for line in text.splitlines()), max(text.splitlines(), key=len)
    (home / "config.yaml").write_text("prompt_caching:\n  cache_ttl: auto\n", "utf-8")
    assert "An idea worth measuring" not in "\n".join(usage.render(usage.build(home, now=NOW)))
    (home / "config.yaml").write_text("prompt_caching:\n  cache_ttl: 5m\n", "utf-8")
    assert "An idea worth measuring" in "\n".join(usage.render(usage.build(home, now=NOW)))
    quiet = tmp / "quiet"
    make_state(quiet, sessions=sessions, messages=[(s["id"], "assistant", None, "x", s["started_at"] + 5) for s in sessions])
    assert "An idea worth measuring" not in "\n".join(usage.render(usage.build(quiet, now=NOW)))
    # The same pauses do not count when no cache setting could change anything for them: cron/subagent runs keep the
    # short cache under Hermes' own "auto", and a cache-lifetime setting does nothing for non-Claude models.
    for name, change in (("cron", {"source": "cron"}), ("subagent", {"source": "subagent"}), ("qwen", {"model": "qwen3-coder-plus"}),
                         ("gpt", {"model": "gpt-6-astra"})):
        other = tmp / ("idea-" + name)
        make_state(other, sessions=[dict(s, **change) for s in sessions], messages=msgs)
        text = "\n".join(usage.render(usage.build(other, now=NOW)))
        assert "An idea worth measuring" not in text, (name, text)
    print("PASS  the cache idea appears only for human-paced Claude chats whose pauses and writes back it")


def t_settings(tmp: Path) -> None:
    home = tmp / "settings"
    home.mkdir()
    text = ("model:\n  default: x   # c\nprompt_caching:  # note\n  cache_ttl: \"auto\"  # trial\ncompression:\n"
            "  threshold: 0.35\n  target_ratio: 0.15\nmemory:\n  provider: memorybank\nother:\n  cache_ttl: wrong\n")
    (home / "config.yaml").write_text(text, "utf-8")
    plain = usage._scan_setting
    assert plain(text, "prompt_caching", "cache_ttl") == "auto", "plain scan: quotes and comments are dropped"
    assert plain(text, "compression", "threshold") == "0.35" and plain(text, "memory", "provider") == "memorybank"
    assert plain(text, "compression", "idle_compact_after_seconds") is None
    saved = sys.modules.get("yaml", "absent")
    sys.modules["yaml"] = None                                      # `import yaml` now fails: the plain scan is used
    try:
        s = usage.read_settings(home)
    finally:
        if saved == "absent":
            del sys.modules["yaml"]
        else:
            sys.modules["yaml"] = saved
    assert s["prompt_caching.cache_ttl"] == "auto" and s["compression.threshold"] == "0.35", s
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("      (PyYAML not installed here: only the plain-scan path was exercised)")
    else:
        s2 = usage.read_settings(home)
        assert s2["prompt_caching.cache_ttl"] == "auto" and s2["compression.threshold"] == 0.35, s2
    assert usage.read_settings(tmp / "nonexistent") == {}
    print("PASS  settings are read with or without a YAML library")


def t_cli(tmp: Path) -> None:
    home = tmp / "cli"
    baseline_world(home, after_cpc=0.07)
    code, out = capture(bank_cli.main, ["--home", str(home), "usage", "--json", "--weeks", "2"])
    assert code == 0 and "weeks" in json.loads(out), out[:300]
    code, out = capture(bank_cli.main, ["--home", str(home), "usage", "--mark", "from-cli"])
    assert code == 0 and "Marked" in out, out
    code, out = capture(bank_cli.main, ["--home", str(home), "usage", "--since", "not-a-date"])
    assert code == 1 and "--since" in out, out
    code, out = capture(bank_cli.main, ["--home", str(home), "usage", "--since", "2026-10-01", "--tools", "read_file,terminal"])
    assert code == 0 and "Since 2026-10-01" in out, out
    # the tool habits are part of the plain views too (without a marker), not only of a before/after report
    plain = tmp / "cli-plain"
    baseline_world(plain, after_cpc=0.07)
    conn = sqlite3.connect(str(plain / "state.db"))
    conn.execute("INSERT INTO messages (session_id, role, tool_name, content, timestamp) VALUES ('b1', 'tool', 'read_file', ?, ?)",
                 ("x" * 4000, at(2026, 10, 14)))
    conn.commit()
    conn.close()
    for argv in (["usage", "--since", "2026-10-01", "--tools", "read_file"], ["usage", "--weeks", "52", "--tools", "read_file,terminal"]):
        code, out = capture(bank_cli.main, ["--home", str(plain)] + argv)
        assert code == 0 and "Tools the model leaned on" in out and "read_file" in out and "1.0k" in out, (argv, out)
    code, out = capture(bank_cli.main, ["--home", str(plain), "usage", "--weeks", "0"])
    assert code == 1 and "--weeks" in out, out
    # looking at usage never creates a bank
    assert not (home / "plugin-data" / "memorybank" / "bank.db").exists()
    print("PASS  command line: --json, --mark, --since, --tools, bad dates")


# ----------------------------------------------------------------------------- 1.3.0 post-review fixes

LINEAGE_SESSIONS = FULL_SESSIONS.replace("cost_status TEXT)", "cost_status TEXT, parent_session_id TEXT)")
MODEL_USAGE = """CREATE TABLE session_model_usage (session_id TEXT NOT NULL, model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '', billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode TEXT NOT NULL DEFAULT '', task TEXT NOT NULL DEFAULT '', api_call_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task))"""
CLAIMS = ("real drop", "real rise", "points to a lower", "points to a higher")   # what a report says when it claims an effect
A_WEEKS = (0.10, 0.12, 0.08, 0.11)
BASELINE_MONDAYS = (at(2026, 10, 12), at(2026, 10, 19), at(2026, 10, 26), at(2026, 11, 2))


def old_marker_form(home: Path) -> dict:
    """Rewrite the first marker the way 1.2.0 saved it (no per-model weeks, no priced-call split, none of the newer keys)."""
    path = usage.markers_path(home)
    stored = json.loads(path.read_text("utf-8"))
    old = stored["markers"][0]
    for key in ("before_week_models", "tools_tracked", "upgrade_note"):
        old.pop(key, None)
    for key in ("mixed_sessions", "mixed_calls"):
        (old.get("before") or {}).pop(key, None)
    for m in ((old.get("before") or {}).get("models") or {}).values():
        for key in ("priced_calls", "cache_write_tokens", "writes_per_call"):
            m.pop(key, None)
    path.write_text(json.dumps(stored), "utf-8")
    return old


def t_prediction_interval(tmp: Path) -> None:
    pi = usage.prediction_interval
    assert pi([], 1) is None and pi([0.1, 0.2], 1) is None and pi([0.1, None, 0.2], 1) is None, "three weeks are the least"
    weeks = list(A_WEEKS)
    got = pi(weeks, 1)
    mean = sum(weeks) / 4
    sd = math.sqrt(sum((w - mean) ** 2 for w in weeks) / 3)
    half = 3.182 * sd * math.sqrt(1.0 + 1.0 / 4)
    assert got["n"] == 4 and abs(got["mean"] - mean) < 1e-12 and abs(got["sd"] - sd) < 1e-12, got
    assert abs(got["low"] - (mean - half)) < 1e-9 and abs(got["high"] - (mean + half)) < 1e-9, got
    assert abs(pi(weeks, 0.2)["high"] - got["high"]) < 1e-12, "an after-period shorter than a week counts as one week"
    two = pi(weeks, 2)
    assert abs(two["high"] - (mean + 3.182 * sd * math.sqrt(0.5 + 0.25))) < 1e-9 and two["high"] < got["high"], two
    # the t table: exact two-sided 95% values up to 30 degrees of freedom, never meaningfully too small beyond
    for df, truth in ((2, 4.3027), (3, 3.1824), (4, 2.7764), (9, 2.2622), (19, 2.0930), (29, 2.0452), (30, 2.0423)):
        assert abs(usage._t975(df) - truth) < 0.001, (df, usage._t975(df))
    for df, truth in ((31, 2.0395), (40, 2.0211), (41, 2.0195), (60, 2.0003), (61, 1.9996), (120, 1.9799), (121, 1.9798),
                      (500, 1.9647), (5000, 1.9604)):
        assert -0.002 <= usage._t975(df) - truth <= 0.03, (df, usage._t975(df))
    # coverage: a figure drawn from the same distribution lands inside about 95% of the time, even on a thin history
    rng = random.Random(7)
    for n, k in ((3, 1), (4, 1), (5, 1), (4, 2), (8, 1), (6, 3)):
        misses, trials = 0, 4000
        for _ in range(trials):
            history = [rng.gauss(0.10, 0.02) for _ in range(n)]
            new = sum(rng.gauss(0.10, 0.02) for _ in range(k)) / k
            interval = pi(history, k)
            if not interval["low"] <= new <= interval["high"]:
                misses += 1
        rate = misses / float(trials)
        assert 0.035 <= rate <= 0.065, ("false alarm rate", n, k, rate)
    print("PASS  prediction interval: t quantiles, sqrt(1/k + 1/n) width, 95% coverage on thin histories")


def t_verdict_wording(tmp: Path) -> None:
    def say(new, low, high, n=4, k=1.0, ttl=None, chats=6, calls=600.0, days=7.0):
        lfl = {"models": ["m"], "calls": calls, "sessions": chats, "priced_chats": chats, "weights": {"m": calls},
               "before_per_call": 0.10, "after_per_call": new, "change": new / 0.10 - 1.0}
        c = {"weekly_swing": (low, high), "weekly_n": n, "cache_ttl": ttl, "days_after": days,
             "weekly_interval": {"n": n, "mean": (low + high) / 2, "sd": 0.01, "low": low, "high": high, "weeks_after": k}}
        text = "\n".join(usage._verdict_lines(c, lfl, {"cost_per_call": 0.10}, {"cost_per_call": new}))
        assert "real drop" not in text and "real rise" not in text, text
        assert all(len(line) <= 120 for line in text.splitlines()), max(text.splitlines(), key=len)
        return text
    # compared after rounding to the four decimals that are printed: $0.079996 shows as $0.0800, the same as the edge
    text = say(0.079996, 0.0800004, 0.12)
    assert "$0.0800" in text and "inside that range" in text and "points to" not in text, text
    text = say(0.12004, 0.08, 0.1199996)
    assert "inside that range" in text and "points to" not in text, text
    # "inside" says how much this history could show: a move of more than ~20% from the usual $0.1000 would stand out
    text = say(0.10, 0.08, 0.12)
    assert "With 4 weeks of history only a move of more than about 20% from the usual $0.1000 would stand out." in text, text
    # a range that reaches zero cannot show a drop at all
    text = say(0.10, -0.05, 0.25)
    assert "only a rise of more than about 150% from the usual $0.1000 would stand out; no drop could." in text, text
    # outside after rounding: a lead, with the number of weeks it rests on
    text = say(0.07994, 0.0800004, 0.12)
    assert "below that range" in text and "points to a lower cost" in text and "4 weeks" in text, text
    assert "lead, not a proof" in text and "thin" in text and "19 of 20" in text, text
    text = say(0.13, 0.08, 0.12, n=20)
    assert "above that range" in text and "points to a higher cost" in text and "20 weeks" in text and "thin" not in text, text
    assert "2.5-week" in say(0.13, 0.08, 0.12, k=2.5), "an after-period longer than a week is said to be what the range is for"
    # a 1-hour or auto cache lifetime is booked at the 5-minute write price: the sentence says so
    assert "5-minute" in say(0.05, 0.08, 0.12, ttl="auto") and "5-minute" in say(0.05, 0.08, 0.12, ttl="1h")
    assert "5-minute" not in say(0.05, 0.08, 0.12, ttl=None)
    # fewer than seven days since the change: the range is made for a week, and the report says it is a little narrow
    text = say(0.10, 0.08, 0.12, days=3.0)
    assert "Only 3.0 days have passed since the change" in text and "narrow for so short a time" in text, text
    assert "Only" not in say(0.10, 0.08, 0.12, days=7.0) and "Only" not in say(0.10, 0.08, 0.12, days=12.5)
    print("PASS  verdict wording: compared as printed, N weeks named, never a 'real' drop or rise")


def t_no_change_false_alarms(tmp: Path) -> None:
    # The same cost distribution before and after, over and over: a verdict that claims an effect should be rare.
    # (Out of the min..max of four earlier weeks, a figure from the same distribution lands 2 times in 5.)
    runs, claims, reached = 60, 0, 0
    marker_at = at(2026, 11, 9, 9, 0)
    for run in range(runs):
        rng = random.Random(26111200 + run)
        home = tmp / ("null%d" % run)
        sessions = []
        for w, start in enumerate(BASELINE_MONDAYS):
            cpc = max(0.02, rng.gauss(0.10, 0.012))
            for i in range(2):
                sessions.append({"id": "b%d_%d" % (w, i), "model": "model-a", "started_at": start + (i + 1) * DAY,
                                 "api_call_count": 75, "estimated_cost_usd": 75 * cpc})
        cpc = max(0.02, rng.gauss(0.10, 0.012))
        for i in range(6):
            sessions.append({"id": "a%d" % i, "model": "model-a", "started_at": marker_at + (i + 1) * 5 * 3600,
                             "api_call_count": 100, "estimated_cost_usd": 100 * cpc})
        make_state(home, sessions=sessions)
        usage.mark(home, "x", now=marker_at)
        result = usage.build(home, now=marker_at + 7 * DAY)
        c = result["comparison"]
        if c["enough"] and c["like_for_like"] and c["weekly_swing"]:
            reached += 1
        text = "\n".join(usage.render(result))
        if any(phrase in text for phrase in CLAIMS):
            claims += 1
        shutil.rmtree(home, ignore_errors=True)
    assert reached >= 57, reached
    assert claims <= 5, "a report with no change at all claimed an effect %d times in %d (target: about 8%% or less)" % (claims, runs)
    print("PASS  no-change worlds: an effect is claimed in %d of %d verdicts (the min..max of 4 weeks is left 2 times in 5)" % (claims, reached))


def t_cache_ttl_note(tmp: Path) -> None:
    for ttl in ("auto", "1h", "5m", None):
        home = tmp / ("ttlnote-%s" % ttl)
        marker_at = scope_world(home, claude_after=6, other_after=0)
        config = home / "config.yaml"
        if ttl is None:
            config.unlink()
        else:
            config.write_text("prompt_caching:\n  cache_ttl: %s\n" % ttl, "utf-8")
        usage.mark(home, "ttl", now=marker_at)
        result = usage.build(home, now=marker_at + 7 * DAY)
        text = "\n".join(usage.render(result))
        c = result["comparison"]
        assert all(len(line) <= 120 for line in text.splitlines()), max(text.splitlines(), key=len)
        if ttl in ("auto", "1h"):
            assert "list-price estimates book cache writes at the 5-minute price" in text and "upper bound on the saving" in text, text
            assert "are in scope: on Claude models" in text, text
            rows = [line for line in text.splitlines() if line.startswith("  cache-write tokens/call, in scope")]
            assert len(rows) == 1 and "-25%" in rows[0] and "13" in rows[0] and "10" in rows[0], (rows, text)
            writes = c["scope_writes_per_call"]
            assert abs(writes["before"] - 1000.0 / 75) < 1e-9 and abs(writes["after"] - 10.0) < 1e-9, writes
            assert "real drop" not in text and "real rise" not in text, text
        else:
            assert "book cache writes" not in text and "in scope" not in text and "scope_writes_per_call" not in c, text
    print("PASS  a 1h/auto marker says writes are booked at the 5-minute price and shows cache-write tokens per call")


def t_weeks_from_mondays(tmp: Path) -> None:
    home = tmp / "tuesday"
    marker_at = at(2026, 11, 10, 10, 0)                           # a Tuesday; --baseline-days 30 is not a multiple of 7
    sessions = [{"id": "w%d" % k, "model": "model-a", "started_at": start + DAY, "api_call_count": 150,
                 "estimated_cost_usd": 150 * 0.10} for k, start in enumerate(BASELINE_MONDAYS)]
    sessions.append({"id": "mon", "model": "model-a", "started_at": at(2026, 11, 9, 12), "api_call_count": 150,
                     "estimated_cost_usd": 150 * 0.25})           # Monday, before the marker: in the 'before' summary
    make_state(home, sessions=sessions)
    marker = usage.mark(home, "x", now=marker_at, baseline_days=30)
    weeks = [w["week"] for w in marker["before_week_models"]]
    assert weeks == ["2026-10-12", "2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09"], weeks
    assert marker["before"]["calls"] == 750 and marker["before_week_models"][-1]["models"]["model-a"] == [150.0, 0.25], marker
    # a marker saved by 1.2.0 is rebuilt over the same weeks
    old = {k: v for k, v in marker.items() if k != "before_week_models"}
    rebuilt = usage._upgrade(home, old, usage.DEFAULT_TOOLS)
    assert [w["week"] for w in rebuilt["before_week_models"]] == weeks, rebuilt["before_week_models"]
    print("PASS  a baseline that is not a whole number of weeks still covers the week the marker is in")


def t_upgrade_keeps_baseline(tmp: Path) -> None:
    home = tmp / "upgrade"
    marker_at = scope_world(home, claude_after=6, other_after=7)
    usage.mark(home, "ttl", now=marker_at)
    old = old_marker_form(home)
    stored_calls = old["before"]["calls"]
    assert stored_calls == 1200, stored_calls
    # a chat that began before the marker keeps running afterwards (resumed, long-lived): the baseline must not move
    conn = sqlite3.connect(str(home / "state.db"))
    conn.execute("UPDATE sessions SET api_call_count = api_call_count + 400, estimated_cost_usd = estimated_cost_usd + 120 WHERE id = 'c1'")
    conn.commit()
    conn.close()
    result = usage.build(home, now=marker_at + 7 * DAY)
    c = result["comparison"]
    assert c["before"]["calls"] == stored_calls and c["before"]["cost_per_call"] == old["before"]["cost_per_call"], c["before"]
    assert c["weekly_swing"] is None and c.get("upgrade_note"), c
    text = "\n".join(usage.render(result))
    assert "cannot be rebuilt for this older marker" in text and not any(p in text for p in CLAIMS), text
    saved = json.loads(usage.markers_path(home).read_text("utf-8"))["markers"][0]      # persisted once: it stops moving
    assert saved["before_week_models"] == [] and saved["before"]["calls"] == stored_calls, saved
    again = usage.build(home, now=marker_at + 8 * DAY)["comparison"]
    assert again["before"]["calls"] == stored_calls and again["weekly_swing"] is None, again
    # untouched history: rebuilt once, saved, and kept when Hermes prunes the old sessions later
    home2 = tmp / "upgrade2"
    marker_at = scope_world(home2, claude_after=6, other_after=7)
    usage.mark(home2, "ttl", now=marker_at)
    old_marker_form(home2)
    first = usage.build(home2, now=marker_at + 7 * DAY)["comparison"]
    assert first["weekly_swing"] is not None and first["before"]["calls"] == 1200 and not first.get("upgrade_note"), first
    saved = json.loads(usage.markers_path(home2).read_text("utf-8"))["markers"][0]
    assert saved["before_week_models"] and "priced_calls" in next(iter(saved["before"]["models"].values())), saved
    conn = sqlite3.connect(str(home2 / "state.db"))
    conn.execute("DELETE FROM sessions WHERE started_at < ?", (marker_at - DAY,))
    conn.commit()
    conn.close()
    second = usage.build(home2, now=marker_at + 7 * DAY)["comparison"]
    assert second["weekly_swing"] == first["weekly_swing"] and second["before"]["calls"] == 1200, second
    # the rebuilt baseline is accepted only when it has the call count the marker recorded, within 2% either way
    for delta, accepted in ((10, True), (-10, True), (30, False), (-30, False)):
        drift = tmp / ("upgrade-%+d" % delta)
        marker_at = scope_world(drift, claude_after=6, other_after=7)
        usage.mark(drift, "ttl", now=marker_at)
        old_marker_form(drift)
        conn = sqlite3.connect(str(drift / "state.db"))
        conn.execute("UPDATE sessions SET api_call_count = api_call_count + ? WHERE id = 'c1'", (delta,))
        conn.commit()
        conn.close()
        c = usage.build(drift, now=marker_at + 7 * DAY)["comparison"]
        assert (c["before"]["calls"] == 1200 + delta) == accepted and (c["weekly_swing"] is not None) == accepted, (delta, c["before"]["calls"])
    print("PASS  a marker saved by 1.2.0 is upgraded once, kept, and never lets the baseline move")


def t_mixed_model_chats(tmp: Path) -> None:
    def world(name: str, table: bool) -> float:
        home = tmp / name
        marker_at = at(2026, 11, 9, 9, 0)
        sessions, k = [], 0
        for w, start in enumerate(BASELINE_MONDAYS):
            for i in range(2):
                k += 1
                sessions.append({"id": "b%d" % k, "model": "model-a", "started_at": start + (i + 1) * DAY, "api_call_count": 75,
                                 "estimated_cost_usd": 75 * (0.008, 0.012, 0.009, 0.011)[w]})
        for i in range(6):       # opened on the cheap model, switched to a dear one: Hermes books the whole chat to the first
            sessions.append({"id": "m%d" % i, "model": "model-a", "started_at": marker_at + (i + 1) * 5 * 3600,
                             "api_call_count": 100, "estimated_cost_usd": 100 * 0.094})
        make_state(home, sessions=sessions)
        if table:
            conn = sqlite3.connect(str(home / "state.db"))
            conn.execute(MODEL_USAGE)
            ins = "INSERT INTO session_model_usage (session_id, model, task, api_call_count) VALUES (?, ?, ?, ?)"
            for i in range(6):
                conn.execute(ins, ("m%d" % i, "model-a", "", 20))
                conn.execute(ins, ("m%d" % i, "model-b", "", 80))
            for k in range(1, 8):
                conn.execute(ins, ("b%d" % k, "model-a", "", 75))
            conn.execute(ins, ("b1", "model-b", "compression", 3))        # an auxiliary call is not a second route
            for provider, calls in (("anthropic", 40), ("openrouter", 35)):      # the same model on two providers is two routes
                conn.execute("INSERT INTO session_model_usage (session_id, model, billing_provider, task, api_call_count) "
                             "VALUES ('b8', 'model-a', ?, '', ?)", (provider, calls))
            conn.commit()
            conn.close()
        usage.mark(home, "x", now=marker_at)
        return marker_at

    marker_at = world("mixed", table=True)
    result = usage.build(tmp / "mixed", now=marker_at + 7 * DAY)
    c = result["comparison"]
    assert c["before"]["mixed_sessions"] == 1 and c["before"]["models"]["model-a"]["sessions"] == 7, c["before"]
    assert c["before"]["sessions"] == 8 and c["before"]["calls"] == 600, "the chat that moved between providers stays in the totals"
    assert c["after"]["mixed_sessions"] == 6 and c["after"]["sessions"] == 6 and c["after"]["calls"] == 600, c["after"]
    assert "model-a" not in c["after"]["models"], "chats that switched models are left out of the per-model figures"
    assert c["like_for_like"] is None, c["like_for_like"]
    text = "\n".join(usage.render(result))
    assert "left out of the same-model figures: 1 before, 6 after" in text, text
    assert not c["enough"] and "Too early to judge: 0 sessions, 0 calls" in text and not any(p in text for p in CLAIMS), text
    assert abs(c["after"]["cost"] - 56.4) < 1e-9, "they stay in the totals"
    # no session_model_usage table (an older Hermes): nothing changes
    marker_at = world("plain", table=False)
    c = usage.build(tmp / "plain", now=marker_at + 7 * DAY)["comparison"]
    assert c["like_for_like"] is not None and not c["after"].get("mixed_sessions"), c
    # a table from before auxiliary calls had their own column: every row is a main-loop route
    old = tmp / "oldtable"
    make_state(old, sessions=[{"id": "s1", "started_at": NOW - DAY}, {"id": "s2", "started_at": NOW - DAY}])
    conn = sqlite3.connect(str(old / "state.db"))
    conn.execute("CREATE TABLE session_model_usage (session_id TEXT, model TEXT, api_call_count INTEGER)")
    conn.executemany("INSERT INTO session_model_usage VALUES (?, ?, ?)", [("s1", "m1", 5), ("s1", "m2", 5), ("s2", "m1", 10)])
    conn.commit()
    conn.close()
    s = usage.whole(usage.load(old, NOW - 7 * DAY, NOW + 1))
    assert s["mixed_sessions"] == 1 and s["sessions"] == 2 and s["models"]["model-a"]["sessions"] == 1, s
    print("PASS  chats that switched models are left out of the per-model figures, counted, and kept in the totals")


def pace_world(home: Path, ttl, after) -> float:
    """Four baseline weeks of desktop chats on one Claude model, plus one dear cron run a week; then chats after a marker."""
    marker_at = at(2026, 11, 9, 9, 0)
    sessions = []
    for w, start in enumerate(BASELINE_MONDAYS):
        for i in range(2):
            sessions.append({"id": "d%d_%d" % (w, i), "model": "claude-x", "source": "desktop", "started_at": start + (i + 1) * DAY,
                             "api_call_count": 75, "estimated_cost_usd": 75 * A_WEEKS[w]})
        sessions.append({"id": "c%d" % w, "model": "claude-x", "source": "cron", "started_at": start + 3 * DAY,
                         "api_call_count": 75, "estimated_cost_usd": 75 * 0.40})
    for i, (source, cpc) in enumerate(after):
        sessions.append({"id": "a%d" % i, "model": "claude-x", "source": source, "started_at": marker_at + (i + 1) * 4 * 3600,
                         "api_call_count": 100, "estimated_cost_usd": 100 * cpc})
    make_state(home, sessions=sessions)
    if ttl is not None:
        (home / "config.yaml").write_text("prompt_caching:\n  cache_ttl: %s\n" % ttl, "utf-8")
    return marker_at


def t_pacing_scope(tmp: Path) -> None:
    assert set(usage.MACHINE_PACED_SOURCES) == {"subagent", "cron", "oneshot", "webhook", "kanban", "api", "tool", "batch"}, \
        "the list is Hermes' own (agent/prompt_caching.py)"

    def report(name, ttl, after):
        home = tmp / name
        marker_at = pace_world(home, ttl, after)
        usage.mark(home, "x", now=marker_at)
        result = usage.build(home, now=marker_at + 7 * DAY)
        return result["comparison"], "\n".join(usage.render(result))

    # auto: only cron runs since the change - nothing a person paces, so nothing for the setting to act on
    c, text = report("auto-cron", "auto", [("cron", 0.40)] * 6)
    assert c["scope_sessions"] == 0 and c["scope_calls"] == 0 and not c["enough"], c
    assert "Too early to judge: 0 Claude sessions" in text and not any(p in text for p in CLAIMS), text
    assert "machine-paced" in text and "not counted" in text, text
    # ... the same for sources Hermes spells with other case or spaces (it lowercases and strips them)
    c, text = report("auto-case", "auto", [("Cron", 0.40), (" CRON ", 0.40), ("Subagent", 0.4), ("webhook", 0.4), ("batch", 0.4), ("api", 0.4)])
    assert c["scope_sessions"] == 0, c
    # auto: chats a person paces, next to cron runs that cost far more per call: both sides are compared without the cron runs
    c, text = report("auto-mix", "auto", [("desktop", 0.10)] * 6 + [("cron", 0.40)] * 3)
    assert c["scope_sessions"] == 6 and c["scope_calls"] == 600 and c["enough"], c
    lfl = c["like_for_like"]
    assert abs(lfl["before_per_call"] - sum(A_WEEKS) / 4) < 1e-9 and abs(lfl["after_per_call"] - 0.10) < 1e-9, lfl
    assert tuple(round(v, 9) for v in c["weekly_swing"]) == (0.08, 0.12), c["weekly_swing"]
    assert c["before"]["models"]["claude-x"]["sessions"] == 8 and c["after"]["models"]["claude-x"]["sessions"] == 6, c
    assert c["after"]["sessions"] == 9 and c["before"]["sessions"] == 12, "the totals still count every chat"
    # explicit 1h: cron runs count, delegated subagent runs do not (Hermes keeps those on 5 minutes)
    c, text = report("one-hour-cron", "1h", [("cron", 0.40)] * 6)
    assert c["scope_sessions"] == 6 and c["enough"], c
    c, text = report("one-hour-subagent", "1h", [("subagent", 0.40)] * 6)
    assert c["scope_sessions"] == 0 and not c["enough"] and "Too early to judge" in text, c
    # any other value: every chat counts, nothing is filtered
    c, text = report("five-minutes", "5m", [("cron", 0.40)] * 6)
    assert c["scope"] is None and c["scope_sessions"] == 6 and c["before"]["models"]["claude-x"]["sessions"] == 12, c
    print("PASS  auto counts chats a person paces only, 1h leaves out subagent runs; the list of sources is Hermes' own")


def t_verdict_needs_priced_chats(tmp: Path) -> None:
    def report(name: str, after):
        home = tmp / ("chats-" + name)
        marker_at = at(2026, 11, 9, 9, 0)
        sessions, k = [], 0
        for w, start in enumerate(BASELINE_MONDAYS):
            for i in range(2):
                k += 1
                sessions.append({"id": "b%d" % k, "model": "model-a", "started_at": start + (i + 1) * DAY, "api_call_count": 75,
                                 "estimated_cost_usd": 75 * A_WEEKS[w]})
        for j, row in enumerate(after):
            base = {"id": "a%d" % j, "model": "model-a", "started_at": marker_at + (j + 1) * 5 * 3600, "api_call_count": 100,
                    "estimated_cost_usd": 100 * 0.03}
            base.update(row)
            sessions.append(base)
        make_state(home, sessions=sessions, sessions_sql=LINEAGE_SESSIONS)
        usage.mark(home, "x", now=marker_at)
        result = usage.build(home, now=marker_at + 7 * DAY)
        return result["comparison"], "\n".join(usage.render(result))

    # one long chat that Hermes compressed four times is five rows, but one chat: no verdict from it
    chain = [{"id": "l%d" % j, "parent_session_id": ("l%d" % (j - 1)) if j else None, "api_call_count": 120} for j in range(5)]
    c, text = report("lineage", chain)
    assert c["enough"] and c["like_for_like"]["calls"] == 600 and c["like_for_like"]["priced_chats"] == 1, c
    assert "No verdict yet: that figure rests on 600 priced calls in 1 priced chat" in text and not any(p in text for p in CLAIMS), text
    # five separate chats are enough
    c, text = report("separate", [{} for _ in range(5)])
    assert c["like_for_like"]["priced_chats"] == 5 and "points to a lower cost" in text, text
    # chats with no recorded price do not count towards it
    unpriced = [{"api_call_count": 200}] * 3 + [{"api_call_count": 50, "estimated_cost_usd": 0.0, "cost_status": "included"}] * 4
    c, text = report("unpriced", unpriced)
    assert c["enough"] and c["like_for_like"]["priced_chats"] == 3 and c["like_for_like"]["calls"] == 600, c
    assert "No verdict yet: that figure rests on 600 priced calls in 3 priced chats" in text, text
    # a chain whose parent started before the window still counts once
    c, text = report("orphans", [{"id": "o%d" % j, "parent_session_id": "gone-%d" % (j // 3), "api_call_count": 100} for j in range(6)])
    assert c["like_for_like"]["priced_chats"] == 2, c["like_for_like"]
    print("PASS  a verdict needs enough priced chats in the like-for-like set; a compressed chat counts once")


def t_presentation(tmp: Path) -> None:
    # the report says what it does not cover
    home = tmp / "present"
    marker_at = baseline_world(home, after_cpc=0.03)
    text = "\n".join(usage.render(usage.build(home, now=marker_at + 7 * DAY)))
    assert "(main-loop chat calls only: compression, vision, title and background-review calls are not included)" in text, text
    # tool habits are shown for a marker with no earlier chats as well
    fresh = tmp / "fresh-tools"
    make_state(fresh, sessions=[{"id": "n1", "started_at": NOW + 3600, "api_call_count": 40}],
               messages=[("n1", "tool", "read_file", "x" * 400, NOW + 3700)])
    usage.mark(fresh, "first", now=NOW)
    text = "\n".join(usage.render(usage.build(fresh, now=NOW + DAY)))
    assert "nothing to compare against" in text and "Tools the model leaned on" in text and "read_file" in text, text
    # a tool that was not tracked when the marker was set is not shown as "-" (which reads as "not used before")
    tools_home = tmp / "tools"
    marker_at = baseline_world(tools_home, after_cpc=0.03)
    conn = sqlite3.connect(str(tools_home / "state.db"))
    rows = [("b1", "tool", "read_file", "x" * 400, marker_at - DAY), ("b1", "tool", "terminal", "y" * 200, marker_at - DAY),
            ("a0", "tool", "read_file", "x" * 400, marker_at + DAY), ("a0", "tool", "terminal", "y" * 200, marker_at + DAY)]
    conn.executemany("INSERT INTO messages (session_id, role, tool_name, content, timestamp) VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    marker = usage.mark(tools_home, "x", now=marker_at, tools=("read_file",))
    assert marker["tools_tracked"] == ["read_file"] and "terminal" not in marker["before"]["tools"], marker
    text = "\n".join(usage.render(usage.build(tools_home, now=marker_at + 7 * DAY, tools=("read_file", "terminal"))))
    lines = {line.split()[0]: line for line in text.splitlines() if line.startswith("  read_file") or line.startswith("  terminal")}
    assert "(not tracked then)" in lines["terminal"] and "(not tracked then)" not in lines["read_file"], lines
    assert "0.1 x" in lines["read_file"], lines       # one call spread over the 9 chats before the marker
    # a pure model switch: the cost-per-call row has no percentage that looks comparable, and the report says no verdict
    c, text = verdict(tmp, "switch", [("model-a", list(A_WEEKS), 150)], [("model-b", 0.02, 6, 100)])
    row = [line for line in text.splitlines() if line.startswith("estimated cost per call")]
    assert len(row) == 1 and "%" not in row[0], row
    assert "No verdict: no model with a recorded price was used both before and after." in text, text
    print("PASS  the report states its scope, shows tools without a baseline, and does not dress a model switch as a % change")


def t_marker_validation(tmp: Path) -> None:
    home = tmp / "validation"
    marker_at = baseline_world(home, after_cpc=0.03)
    conn = sqlite3.connect(str(home / "state.db"))
    conn.executemany("INSERT INTO messages (session_id, role, tool_name, content, timestamp) VALUES (?,?,?,?,?)",
                     [("b1", "tool", "read_file", "x" * 400, marker_at - DAY), ("a0", "tool", "read_file", "x" * 400, marker_at + DAY)])
    conn.commit()
    conn.close()
    usage.mark(home, "good", now=marker_at)
    path = usage.markers_path(home)
    good = json.loads(path.read_text("utf-8"))
    now = marker_at + 7 * DAY

    def second(mutate, old_form=False):
        data = json.loads(json.dumps(good))
        entry = data["markers"][0]
        entry["label"] = "bad"
        entry["at"] = marker_at + 5.0
        mutate(entry)
        if old_form:
            entry.pop("before_week_models", None)
        data["markers"] = [good["markers"][0], entry]
        path.write_text(json.dumps(data), "utf-8")
        return capture(usage.run, home, now=now, vs="bad", out=lambda t: print(t))

    # (mutation, what the report does with the entry): a number that is not a finite number cannot be used - the entry is
    # refused when the file is read; a value that only has to be sensible (baseline_days, a tool entry that lacks a number,
    # settings) is made sensible, and the entry works
    usable, refused = "usable", "refused"
    cases = {
        "tool entry without avg_tokens": (lambda m: m["before"].update(tools={"read_file": {"calls": 3, "calls_per_session": 1.0}}), usable),
        "tool entry without calls_per_session": (lambda m: m["before"].update(tools={"read_file": {"avg_tokens": 100.0}}), usable),
        "tool entry empty": (lambda m: m["before"].update(tools={"read_file": {}}), usable),
        "unpriced_calls text": (lambda m: m["before"].update(unpriced_calls="many"), refused),
        "unpriced_calls list": (lambda m: m["before"].update(unpriced_calls=[1]), refused),
        "unpriced_calls dict": (lambda m: m["before"].update(unpriced_calls={"a": 1}), refused),
        "unpriced_calls inf": (lambda m: m["before"].update(unpriced_calls=float("inf")), refused),
        "unpriced_calls huge": (lambda m: m["before"].update(unpriced_calls=1e999), refused),
        "priced_calls text": (lambda m: m["before"].update(priced_calls="x"), refused),
        "pauses text": (lambda m: m["before"].update(pauses_5_60="x", pauses_over_60=[]), refused),
        "baseline_days huge": (lambda m: m.update(baseline_days=1e30), usable),
        "baseline_days negative": (lambda m: m.update(baseline_days=-5), usable),
        "baseline_days text": (lambda m: m.update(baseline_days="x"), usable),
        "at huge": (lambda m: m.update(at=1e30), refused),
        "at negative": (lambda m: m.update(at=-1e30), refused),
        "at tiny": (lambda m: m.update(at=5.0), refused),
        "settings list": (lambda m: m.update(settings=["x"]), usable),
        "weeks wrong type": (lambda m: m.update(before_week_models=[{"week": 5, "models": {"model-a": "x"}}]), usable),
    }
    for name, (mutate, expected) in cases.items():
        for old_form in (False, True):
            code, out = second(mutate, old_form)
            assert "Traceback" not in out, (name, old_form, out)
            if expected == usable:
                assert code == 0 and "Before / after" in out and "could not be used" not in out, (name, old_form, out)
            else:
                assert code == 1 and "no marker called 'bad'" in out and "1 entry in usage-markers.json could not be used" in out, \
                    (name, old_form, out)
    # a tool entry that lacks a number is still shown, without crashing
    code, out = second(cases["tool entry without avg_tokens"][0])
    assert code == 0 and "read_file" in out, out
    # an entry that cannot be used is said to be unusable, and the good one next to it keeps working
    path.write_text(json.dumps({"markers": [good["markers"][0], dict(good["markers"][0], label="bad", at=1e30)]}), "utf-8")
    code, out = capture(usage.run, home, now=now, vs="good", out=lambda t: print(t))
    assert code == 0 and "Before / after" in out and "1 entry in usage-markers.json could not be used" in out, out

    # a marker nobody could predict still ends in the "could not be used" note when the report is worked out, not a
    # traceback - but a problem with state.db is still said as it is
    real_compare = usage._compare

    def cannot_compare(*args, **kwargs):
        raise OverflowError("cannot convert float infinity to integer")

    usage._compare = cannot_compare
    try:
        path.write_text(json.dumps(good), "utf-8")
        result = usage.build(home, now=now)
        assert "comparison" not in result and "OverflowError" in result["markers_note"], result
        assert "1 entry in usage-markers.json could not be used (\"good\"" in result["markers_note"], result["markers_note"]
        assert "(note: 1 entry in usage-markers.json could not be used" in "\n".join(usage.render(result))

        def unreadable(*args, **kwargs):
            raise usage.UsageError("could not read state.db: disk I/O error")

        usage._compare = unreadable
        try:
            usage.build(home, now=now)
            raise AssertionError("expected UsageError")
        except usage.UsageError as exc:
            assert "disk I/O error" in str(exc), exc
    finally:
        usage._compare = real_compare

    # a marker nobody could predict still ends in the "could not be used" note, not a traceback: here a before block
    # that passes every check but whose numbers cannot be formatted by a future layout
    real = usage._comparison_lines

    def cannot_format(c):
        raise OverflowError("cannot convert float infinity to integer")

    usage._comparison_lines = cannot_format
    try:
        path.write_text(json.dumps(good), "utf-8")
        result = usage.build(home, now=now)
        text = "\n".join(usage.render(result))
    finally:
        usage._comparison_lines = real
    assert "1 entry in usage-markers.json could not be used" in text and '"good"' in text and "OverflowError" in text, text

    # fuzz: random damage to a valid file never gives a traceback (deterministic seeds)
    base = {"new": json.loads(json.dumps(good))}
    old_style = json.loads(json.dumps(good))
    for key in ("before_week_models", "tools_tracked"):
        old_style["markers"][0].pop(key, None)
    base["old"] = old_style
    junk = [None, True, False, 0, -1, 1, 2 ** 70, 1e30, -1e30, 1e-30, float("inf"), float("-inf"), float("nan"), "", "x", "1e999",
            [], {}, [1, 2], {"a": 1}, [[]], [None], {"models": 5}, 0.5, -0.0]

    def nodes(node, prefix=()):
        yield prefix
        if isinstance(node, dict):
            for key, value in list(node.items()):
                for found in nodes(value, prefix + (key,)):
                    yield found
        elif isinstance(node, list):
            for index, value in enumerate(node):
                for found in nodes(value, prefix + (index,)):
                    yield found

    def walk(root, steps):
        for step in steps:
            root = root[step]
        return root

    failures = []
    for seed in range(150):
        rng = random.Random(8000 + seed)
        data = json.loads(json.dumps(base["old" if seed % 2 else "new"]))
        spots = [p for p in nodes(data) if p]
        for _ in range(rng.randint(1, 3)):
            steps = rng.choice(spots)
            try:
                parent, key = walk(data, steps[:-1]), steps[-1]
                action = rng.random()
                if action < 0.7:
                    parent[key] = rng.choice(junk)
                elif action < 0.85:
                    del parent[key]
                else:                               # a copy of another part (a copy: never a node inside itself)
                    parent[key] = json.loads(json.dumps(walk(data, rng.choice(spots))))
            except (KeyError, IndexError, TypeError):
                continue                            # an earlier change in this round already removed that spot
        path.write_text(json.dumps(data), "utf-8")
        try:
            code, out = capture(usage.run, home, now=now, out=lambda t: print(t), as_json=bool(seed % 3 == 0))
            assert code in (0, 1) and "Traceback" not in out, out
        except Exception as exc:  # noqa: BLE001
            failures.append((seed, "%s: %s" % (type(exc).__name__, exc)))
    assert not failures, "a damaged marker file crashed the report: %s" % failures[:5]
    print("PASS  damaged or hand-edited markers (targeted cases and 150 random mutations) never end in a traceback")


def t_marker_file_concurrency(tmp: Path) -> None:
    home = tmp / "concurrency"
    marker_at = baseline_world(home, after_cpc=0.03)
    usage.mark(home, "one", now=marker_at)
    path = usage.markers_path(home)
    # (a) Windows refuses to open a file that is being replaced: that is a moment, not damage
    real_read = Path.read_bytes
    refusals = {"left": 2}

    def flaky(self):
        if self.name == usage.MARKERS_NAME and refusals["left"] > 0:
            refusals["left"] -= 1
            raise PermissionError(13, "Permission denied")
        return real_read(self)

    Path.read_bytes = flaky
    try:
        markers, note = usage.load_markers(home)
        assert [m["label"] for m in markers] == ["one"] and note is None and refusals["left"] == 0, (markers, note)
        refusals["left"] = 10 ** 6                    # a file that stays unreadable is still reported
        markers, note = usage.load_markers(home)
        assert markers == [] and "could not be read" in note, (markers, note)
    finally:
        Path.read_bytes = real_read
    # (b) a stale lock is taken over by one run only: if a live lock appears between "it is old" and "take it away",
    # the live lock must not be destroyed
    lock = Path(str(path) + ".lock")
    old = time.time() - 600
    lock.write_text("", "utf-8")
    os.utime(str(lock), (old, old))
    real_replace, real_unlink = os.replace, os.unlink
    state = {"raced": False}

    def other_run_takes_the_lock():
        if state["raced"]:
            return
        state["raced"] = True
        real_unlink(str(lock))                        # the other run found the same stale lock and took it over first ...
        os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY))       # ... and now holds a fresh one

    def hooked_replace(src, dst, *args, **kwargs):
        if str(src) == str(lock):
            other_run_takes_the_lock()
        return real_replace(src, dst, *args, **kwargs)

    def hooked_unlink(target, *args, **kwargs):
        if str(target) == str(lock):
            other_run_takes_the_lock()
        return real_unlink(target, *args, **kwargs)

    saved_wait = getattr(usage, "LOCK_WAIT_SECONDS", None)
    usage.LOCK_WAIT_SECONDS = 0.4
    os.replace, os.unlink = hooked_replace, hooked_unlink
    entered = []
    try:
        try:
            with usage._locked(path):
                entered.append(True)
        except usage.UsageError as exc:
            assert "seems to be running" in str(exc), exc
    finally:
        os.replace, os.unlink = real_replace, real_unlink
        if saved_wait is None:
            del usage.LOCK_WAIT_SECONDS
        else:
            usage.LOCK_WAIT_SECONDS = saved_wait
    assert state["raced"] and not entered, "a second run entered the critical section while the first still held the lock"
    assert lock.exists(), "the live lock was destroyed"
    assert sorted(p.name for p in path.parent.iterdir() if ".stale" in p.name) == [], "no leftovers from the takeover"
    lock.unlink()
    print("PASS  a marker read that meets a replace in progress is retried; a stale lock is taken over by one run only")


def t_mark_keeps_foreign_entries(tmp: Path) -> None:
    home = tmp / "foreign"
    marker_at = baseline_world(home, after_cpc=0.03)
    path = usage.markers_path(home)
    path.parent.mkdir(parents=True)
    future = {"label": "from-the-future", "at": marker_at - 5 * DAY, "schema": 7, "notes": ["keep me"],
              "settings": ["future", "layout"], "before": None,
              "before_week_models": [{"week": "2026-10-26", "models": {"model-a": {"calls": 5, "cost": 1.0}}}]}
    path.write_text(json.dumps({"version": 9, "markers": [future]}), "utf-8")
    assert [m["label"] for m in usage.read_markers(home)] == ["from-the-future"], "readable, with the settings read as empty"
    usage.mark(home, "second", now=marker_at)
    saved = json.loads(path.read_text("utf-8"))
    assert saved["version"] == 9, "other top-level keys stay"
    assert saved["markers"][0] == future, saved["markers"][0]
    assert [m["label"] for m in saved["markers"]] == ["from-the-future", "second"]
    # if the file changes under a --mark so that what was read first can no longer be found, nothing is written
    real_raw = usage._raw_markers
    usage._raw_markers = lambda home: (None, [])
    try:
        usage.mark(home, "third", now=marker_at + 1)
        raise AssertionError("expected UsageError")
    except usage.UsageError as exc:
        assert "changed while the marker was being saved" in str(exc), exc
    finally:
        usage._raw_markers = real_raw
    assert json.loads(path.read_text("utf-8")) == saved, "the file is untouched"
    assert not Path(str(path) + ".lock").exists()
    print("PASS  --mark leaves earlier entries (and keys it does not know) exactly as they were")


def t_data_dir_private(tmp: Path) -> None:
    calls = []
    real_chmod = Path.chmod
    had_posix = hasattr(usage, "_posix")
    real_posix = getattr(usage, "_posix", None)
    Path.chmod = lambda self, mode, *args, **kwargs: calls.append((str(self), mode))
    usage._posix = lambda: True                       # act as on macOS/Linux, whatever this machine is
    try:
        home = tmp / "private"
        home.mkdir()
        usage.mark(home, "first", now=NOW)
        folder = usage.markers_path(home).parent
        assert (str(folder), 0o700) in calls, calls
        calls.clear()
        usage.mark(home, "second", now=NOW + 1)       # it exists now: left as it is
        assert not calls, calls
        theirs = tmp / "theirs"
        (theirs / "plugin-data" / "memorybank").mkdir(parents=True)
        usage.mark(theirs, "x", now=NOW)              # a folder somebody else made is not touched
        assert not calls, calls
        usage._posix = lambda: False
        other = tmp / "windows"
        other.mkdir()
        usage.mark(other, "x", now=NOW)
        assert not calls, "no chmod on Windows"
    finally:
        Path.chmod = real_chmod
        if had_posix:
            usage._posix = real_posix
        elif hasattr(usage, "_posix"):
            del usage._posix
    print("PASS  a data folder this code creates starts private (owner only) on macOS/Linux, one that exists is left alone")


def wal_snapshot(tmp: Path, name: str, tail: int, blocker: bool = True) -> Path:
    """A copy of a WAL database: 2 sessions in the main file and ``tail`` newer ones only in the -wal file; the -shm
    cannot be created (a directory has its name), as on a read-only place."""
    src = tmp / (name + "-src")
    src.mkdir()
    conn = sqlite3.connect(src / "state.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, api_call_count INTEGER, "
                 "estimated_cost_usd REAL)")
    insert = "INSERT INTO sessions VALUES (?, 'desktop', 'm', ?, 10, 1.0)"
    for i in range(2):
        conn.execute(insert, ("k%d" % i, NOW - DAY))
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(FULL)")
    for i in range(tail):
        conn.execute(insert, ("t%d" % i, NOW - 3600))
        conn.commit()
    snap = tmp / name
    snap.mkdir()
    shutil.copy2(str(src / "state.db"), str(snap / "state.db"))
    if tail:
        shutil.copy2(str(src / "state.db-wal"), str(snap / "state.db-wal"))
    conn.close()
    if blocker:
        (snap / "state.db-shm").mkdir()
    return snap


def normal_open_fails(home: Path) -> bool:
    """True when SQLite cannot open this database read-only the normal way (which is what sends the meter to the
    main-file-only fallback). It does here for a directory standing in for the -shm file; other systems may differ."""
    conn = None
    try:
        conn = sqlite3.connect((home / "state.db").resolve().as_uri() + "?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        if conn is not None:
            conn.close()


def t_immutable_fallback(tmp: Path) -> None:
    # A snapshot with changes only in its -wal file, on a place where SQLite cannot read it the normal way: the main
    # file alone would silently leave out the newest chats. The report says so (and stops), and --mark refuses.
    snap = wal_snapshot(tmp, "tail", tail=5)
    if normal_open_fails(snap):
        code, out = capture(usage.run, snap, now=NOW, as_json=True, out=lambda t: print(t))
        assert code == 1 and out.startswith("Usage: could not open") and "state.db-wal" in out, out
        assert "may hold chats" in out and "since Hermes' last checkpoint" in out, out
        code, out = capture(usage.run, snap, now=NOW, out=lambda t: print(t))
        assert code == 1 and "state.db-wal" in out, out
        code, out = capture(usage.run, snap, now=NOW, mark_label="after-change", out=lambda t: print(t))
        assert code == 1 and "state.db-wal" in out and not (snap / "plugin-data").exists(), out
    else:
        print("      (this SQLite reads a WAL snapshot with an unusable -shm the normal way: only the forced case below was run)")
    # the same snapshot with no unmerged changes (a cleanly closed database): still read, through immutable
    clean = wal_snapshot(tmp, "clean", tail=0)
    code, out = capture(usage.run, clean, now=NOW, as_json=True, out=lambda t: print(t))
    assert code == 0 and json.loads(out)["summary"]["sessions"] == 2, out
    # a live writer with unmerged changes in an ordinary folder is read the normal way: all of it, no refusal
    live = tmp / "live"
    live.mkdir()
    writer = sqlite3.connect(live / "state.db")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, api_call_count INTEGER, "
                   "estimated_cost_usd REAL)")
    for i in range(7):
        writer.execute("INSERT INTO sessions VALUES (?, 'desktop', 'm', ?, 10, 1.0)", ("s%d" % i, NOW - 3600))
        writer.commit()
    try:
        code, out = capture(usage.run, live, now=NOW, as_json=True, out=lambda t: print(t))
        assert code == 0 and json.loads(out)["summary"]["sessions"] == 7, out
    finally:
        writer.close()

    # a hot rollback journal takes the same branch and would show rows that were never committed
    home = tmp / "journal"
    home.mkdir()
    crash = ("import os, sqlite3, sys\n"
             "c = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
             "c.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, api_call_count INTEGER, "
             "estimated_cost_usd REAL, pad TEXT)')\n"
             "c.execute(\"INSERT INTO sessions VALUES ('a', 'desktop', 'm', %r, 10, 1.0, '')\")\n"
             "c.execute('PRAGMA cache_size=10')\n"
             "c.execute('BEGIN')\n"
             "for i in range(3000):\n"
             "    c.execute('INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)', ('z%%d' %% i, 'desktop', 'm', %r, 10, 1.0, 'x' * 200))\n"
             "os._exit(0)\n") % (NOW - DAY, NOW - 3600)
    subprocess.run([sys.executable, "-c", crash, str(home / "state.db")], check=True)
    journal = home / "state.db-journal"
    if journal.exists() and journal.stat().st_size > 0 and normal_open_fails(home):
        code, out = capture(usage.run, home, now=NOW, out=lambda t: print(t))
        assert code == 1 and "state.db-journal" in out and "cannot be opened read-only" in out, out
        code, out = capture(usage.run, home, now=NOW, mark_label="x", out=lambda t: print(t))
        assert code == 1 and not (home / "plugin-data").exists(), out
    else:
        print("      (this SQLite left no hot journal behind a crashed writer: only the forced case below was run)")

    # the guard itself, with the normal open forced to fail the way SQLite fails on a read-only place
    plain = tmp / "forced"
    make_state(plain, sessions=[{"id": "f1", "started_at": NOW - DAY}])
    real_connect = sqlite3.connect

    def refuse_normal_open(target, *args, **kwargs):
        if "mode=ro" in str(target) and "immutable" not in str(target):
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real_connect(target, *args, **kwargs)

    sqlite3.connect = refuse_normal_open
    try:
        for suffix, size, refused in (("-wal", 0, False), ("-journal", 0, False), ("-wal", 64, True), ("-journal", 64, True)):
            side = Path(str(plain / "state.db") + suffix)
            side.write_bytes(b"\0" * size)
            code, out = capture(usage.run, plain, now=NOW, as_json=True, out=lambda t: print(t))
            assert (code == 1) == refused, (suffix, size, code, out[:200])
            if refused:
                assert "state.db" + suffix in out, out
            side.unlink()
        code, out = capture(usage.run, plain, now=NOW, as_json=True, out=lambda t: print(t))
        assert code == 0 and json.loads(out)["summary"]["sessions"] == 1, "no side file: the main file is all there is"
    finally:
        sqlite3.connect = real_connect
    print("PASS  a database that can only be read as it is on disk is refused when a -wal or -journal may hold more")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mb-usage-"))
    tests = [t_totals, t_pauses_and_tools, t_weeks, t_other_versions, t_readonly_and_paths, t_busy_and_wal, t_unpriced,
             t_markers, t_cache_scope, t_verdicts, t_marker_file, t_arguments, t_formatting, t_marker_without_history,
             t_idea, t_settings, t_cli,
             t_prediction_interval, t_verdict_wording, t_no_change_false_alarms, t_cache_ttl_note, t_weeks_from_mondays,
             t_upgrade_keeps_baseline, t_mixed_model_chats, t_pacing_scope, t_verdict_needs_priced_chats, t_presentation,
             t_marker_validation, t_marker_file_concurrency, t_mark_keeps_foreign_entries, t_data_dir_private,
             t_immutable_fallback]
    wanted = set(sys.argv[1:])                    # optional: run only the tests named on the command line
    if wanted:
        unknown = wanted - set(fn.__name__ for fn in tests)
        if unknown:
            print("FAIL  no such test: %s" % ", ".join(sorted(unknown)))
            shutil.rmtree(tmp, ignore_errors=True)
            return 2
        tests = [fn for fn in tests if fn.__name__ in wanted]
    failed = 0
    try:
        for fn in tests:
            try:
                fn(tmp)
            except Exception:
                failed += 1
                print("FAIL  " + fn.__name__)
                traceback.print_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
