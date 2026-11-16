"""Usage meter: what a profile's chats cost, read from Hermes' own ``state.db``.

Read-only and offline. It never writes to ``state.db`` and never calls a model.
The one file it writes is its own ``usage-markers.json`` (``--mark``, and once
when a report upgrades a marker saved by 1.2.0). Costs are the figures Hermes
recorded itself (list-price estimates unless the provider reported a real
cost), so they are good for comparing one week with another, not for
reconciling an invoice. Chats Hermes recorded no price for (included in a plan,
or on a model it could not price) are left out of every cost figure and counted
separately. Only the main loop of each chat is read: calls Hermes books
elsewhere (compression, vision, titles, background review) are not included.

Two ideas make it useful for judging a settings change:

* a *marker* (``--mark``) records "something changed now" together with the
  numbers of the weeks before it, so a later report can put before and after
  side by side even when Hermes has since pruned old sessions;
* costs are compared *like for like*: only models that appear on both sides of
  the marker count, weighted by how much each was used afterwards, and the
  weekly figures of the weeks before the marker say how far a new figure may
  stray by chance (a 95% prediction interval) - so switching to a cheaper model
  cannot pass for a saving, and no verdict is given until enough calls, chats
  and earlier weeks stand behind the like-for-like figure.

Plain standard library, Python 3.8+.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .config import data_dir

SHORT_PAUSE = 300.0      # seconds: Hermes' default prompt cache lives 5 minutes
LONG_PAUSE = 3600.0      # ... and its longer option one hour
OPEN_TIMEOUT = 10.0      # seconds to wait for a busy state.db before saying so
LOCK_WAIT_SECONDS = 5.0  # how long a --mark waits for another --mark to finish
LOCK_STALE_SECONDS = 60.0    # a lock file older than this was left behind by a run that died
MARKER_READ_TRIES = 5    # Windows refuses to open the marker file for a moment while a --mark replaces it
MIN_MARKER_AT = 315532800.0      # 1980-01-01 and 2100-01-01: a marker time outside them is not one this code wrote
MAX_MARKER_AT = 4102444800.0
DEFAULT_TOOLS = ("read_file", "skill_view", "session_search", "web_extract", "memory_bank")
MARKERS_NAME = "usage-markers.json"
BASELINE_DAYS = 28
MAX_BASELINE_DAYS = 3650
MAX_WEEKS = 520
MIN_CALLS_FOR_VERDICT = 500      # priced calls behind a like-for-like figure before it may be called lower or higher
MIN_SESSIONS_FOR_VERDICT = 5     # ... and the priced chats (a chat Hermes compressed is one chat) they come from
MIN_LIKE_CALLS = 30              # priced calls a model needs on each side of a marker to count as like for like
MIN_WEEK_MODEL_CALLS = 20        # ... and in a baseline week for that week to count towards the usual swing
MIN_WEEK_CALLS = 100
THIN_HISTORY_WEEKS = 8           # fewer earlier weeks than this is called thin history in the verdict
# A cache-lifetime setting (prompt_caching.cache_ttl = 1h or auto) mainly changes how Claude models are cached (Hermes
# also sends the marker on a few other routes, which this meter does not count), so a before/after report for such a
# change counts Claude models only.
CACHE_SCOPE_TTLS = ("1h", "auto")
# Sources Hermes treats as machine-paced: with cache_ttl = auto they stay on the 5-minute cache (MACHINE_PACED_SOURCES in
# Hermes' agent/prompt_caching.py - keep this list the same as that one); the same list is "background" in the report.
MACHINE_PACED_SOURCES = ("subagent", "cron", "oneshot", "webhook", "kanban", "api", "tool", "batch")
UNATTENDED_SOURCES = MACHINE_PACED_SOURCES
SETTINGS = (
    ("prompt_caching", "cache_ttl"), ("compression", "threshold"), ("compression", "target_ratio"),
    ("compression", "idle_compact_after_seconds"), ("memory", "provider"),
)
_LABEL = re.compile(r"[\w.\-]{1,40}")
_SESSION_FIELDS = (
    "id", "model", "source", "started_at", "api_call_count", "input_tokens", "cache_read_tokens",
    "cache_write_tokens", "output_tokens", "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd", "cost_status",
    "parent_session_id",
)
# The sessions of one time window (reused as a sub-query on the messages table).
_WINDOW = ("SELECT id FROM sessions WHERE COALESCE(api_call_count, 0) > 0 "
           "AND typeof(started_at) IN ('real', 'integer') AND started_at >= ? AND started_at < ?")


class UsageError(Exception):
    """Something the user can act on; the message is shown as it is."""


# ----------------------------------------------------------------------------- reading state.db

def state_db(home: Any) -> Path:
    return Path(home) / "state.db"


def _pending_side_files(path: Path) -> List[str]:
    """Side files that may hold changes the main file does not: an unmerged ``-wal`` or a hot ``-journal``.

    A WAL file keeps its size after a checkpoint, so a non-empty one only says "may" - which is all the caller needs.
    """
    pending = []
    for suffix in ("-wal", "-journal"):
        try:
            if os.path.getsize(str(path) + suffix) > 0:
                pending.append(path.name + suffix)
        except OSError:
            pass                     # not there, or gone meanwhile: nothing to replay
    return pending


def _connect(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri()
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(uri + "?mode=ro", uri=True, timeout=OPEN_TIMEOUT)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.OperationalError as exc:
        if conn is not None:
            conn.close()
        text = str(exc).lower()
        if "unable to open" not in text and "readonly" not in text and "read-only" not in text:
            raise      # "database is locked" and the like are reported: a file being written is not read as it is
        # A database on a read-only place (or with a hot journal): the main file can be read as it is - but "as it is"
        # ignores an unmerged -wal and a hot -journal, so when either may hold more, say so instead of showing numbers
        # (and, with --mark, freezing them into a baseline) that leave out the newest chats.
        pending = _pending_side_files(path)
        if pending:
            raise sqlite3.OperationalError(
                "%s cannot be opened read-only here, and %s may hold chats it does not contain (those since Hermes' last "
                "checkpoint could be missing); copy the folder to a writable place, or let Hermes close it, and run "
                "this again" % (path.name, " / ".join(pending)))
        conn = sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True, timeout=OPEN_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


class Data:
    """The sessions of a window, plus what is known about their pauses and tool calls."""

    def __init__(self) -> None:
        self.sessions: List[Dict[str, Any]] = []
        self.gaps: Dict[str, List[int]] = {}                  # session -> [gaps, pauses 5-60 min, pauses > 1 h]
        self.tools: Dict[str, Dict[str, Tuple[int, int]]] = {}  # session -> tool -> (calls, characters)
        self.mixed: Set[Any] = set()                          # chats that ran the main loop on more than one model
        self.pauses_known = False
        self.tools_known = False


def load(home: Any, since: float, until: float, tools: Sequence[str] = DEFAULT_TOOLS) -> Data:
    path = state_db(home)
    if not path.exists():
        raise UsageError("no state.db in %s - Hermes has not recorded any chats there yet" % path.parent)
    try:
        conn = _connect(path)
    except sqlite3.Error as exc:
        hint = " (Hermes may be writing to it - try again in a moment)" if "locked" in str(exc).lower() else ""
        raise UsageError("could not open %s: %s%s" % (path, exc, hint))
    try:
        return _load(conn, float(since), float(until), tools)
    except sqlite3.Error as exc:
        raise UsageError("could not read %s: %s" % (path.name, exc))
    finally:
        conn.close()


def _load(conn: sqlite3.Connection, since: float, until: float, tools: Sequence[str]) -> Data:
    have = _columns(conn, "sessions")
    if not have:
        raise UsageError("state.db has no sessions table - a different Hermes version?")
    missing = [c for c in ("id", "started_at", "api_call_count") if c not in have]
    if missing:
        raise UsageError("this state.db has no %s column in its sessions table - a different Hermes version?"
                         % "/".join(missing))
    data = Data()
    select = ", ".join(c if c in have else "NULL AS %s" % c for c in _SESSION_FIELDS)
    sql = ("SELECT %s FROM sessions WHERE COALESCE(api_call_count, 0) > 0 "
           "AND typeof(started_at) IN ('real', 'integer') AND started_at >= ? AND started_at < ?" % select)
    data.sessions = [dict(row) for row in conn.execute(sql, (since, until))]
    if not data.sessions:
        return data
    # Hermes books a whole chat to the model it opened on, even after a /model switch or a fallback, and keeps the real
    # split per route (model, provider, base url, billing mode) in session_model_usage (task '' = the main loop;
    # auxiliary calls have their own task and are not routes). A chat with more than one main-loop route cannot be put
    # in any one model's figures: find those chats.
    ucols = _columns(conn, "session_model_usage")
    if {"session_id", "model"} <= ucols:
        main_loop = " AND COALESCE(task, '') = ''" if "task" in ucols else ""      # older tables have main-loop rows only
        route = " || '|' || ".join("COALESCE(%s, '')" % c for c in ("model", "billing_provider", "billing_base_url", "billing_mode")
                                   if c in ucols)
        sql = ("SELECT session_id FROM session_model_usage WHERE session_id IN (%s)%s GROUP BY session_id "
               "HAVING COUNT(DISTINCT %s) > 1" % (_WINDOW, main_loop, route))
        data.mixed = {row[0] for row in conn.execute(sql, (since, until))}
    mcols = _columns(conn, "messages")
    if {"session_id", "timestamp", "id"} <= mcols:
        data.pauses_known = True
        prev_sid: Any = None
        prev_ts = 0.0
        sql = ("SELECT session_id, timestamp FROM messages WHERE timestamp IS NOT NULL AND session_id IN (%s) "
               "ORDER BY session_id, id" % _WINDOW)
        for sid, ts in conn.execute(sql, (since, until)):
            if not isinstance(ts, (int, float)) or not math.isfinite(ts):
                continue
            counts = data.gaps.setdefault(sid, [0, 0, 0])
            if sid == prev_sid:
                gap = ts - prev_ts
                if gap > 0:
                    counts[0] += 1
                    if gap > LONG_PAUSE:
                        counts[2] += 1
                    elif gap > SHORT_PAUSE:
                        counts[1] += 1
                prev_ts = max(prev_ts, float(ts))         # a row stamped earlier than the one before it is not a new start
            else:
                prev_sid, prev_ts = sid, float(ts)
    if tools and {"session_id", "role", "tool_name", "content"} <= mcols:
        data.tools_known = True
        marks = ",".join("?" * len(tools))
        sql = ("SELECT session_id, tool_name, COUNT(*), COALESCE(SUM(LENGTH(content)), 0) FROM messages "
               "WHERE role = 'tool' AND tool_name IN (%s) AND session_id IN (%s) GROUP BY session_id, tool_name"
               % (marks, _WINDOW))
        for sid, name, calls, chars in conn.execute(sql, tuple(tools) + (since, until)):
            data.tools.setdefault(sid, {})[name] = (int(calls), int(chars or 0))
    return data


# ----------------------------------------------------------------------------- arithmetic

def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _num(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _is_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _priced(session: Dict[str, Any]) -> Optional[float]:
    """The cost Hermes recorded for a session, or None when there is no price to count.

    A recorded real cost wins over the estimate. A session Hermes marks as included (a plan, not a per-call price)
    has no price, and neither has an "unknown" one without an estimate: counting them as free would make the cost
    per call of a model drift with how many of its chats happened to be included.
    """
    cost = session.get("actual_cost_usd")
    if not _is_number(cost):
        cost = session.get("estimated_cost_usd")
    if not _is_number(cost):
        return None
    value = float(cost)
    status = str(session.get("cost_status") or "").lower()
    if status == "included" or (status == "unknown" and not value > 0):
        return None
    return value


def _new_bucket() -> Dict[str, Any]:
    return {"sessions": 0, "calls": 0, "fresh": 0.0, "reads": 0.0, "writes": 0.0, "out": 0.0, "cost": 0.0,
            "cost_calls": 0, "gaps": 0, "p5": 0, "p60": 0, "models": {}, "tools": {}, "sources": {},
            "mixed": 0, "mixed_calls": 0}


def _add(bucket: Dict[str, Any], session: Dict[str, Any], data: Data,
         include: Optional[Callable[[Dict[str, Any]], bool]] = None) -> None:
    """Count one session. The totals take every session; the per-model figures only those ``include`` accepts (the chats
    a setting under test reaches) and that stayed on one model - chats that switched models are counted aside."""
    calls = int(_num(session.get("api_call_count")))
    fresh = _num(session.get("input_tokens"))
    reads = _num(session.get("cache_read_tokens"))
    writes = _num(session.get("cache_write_tokens"))
    cost = _priced(session)
    known = cost is not None
    bucket["sessions"] += 1
    bucket["calls"] += calls
    bucket["fresh"] += fresh
    bucket["reads"] += reads
    bucket["writes"] += writes
    bucket["out"] += _num(session.get("output_tokens")) + _num(session.get("reasoning_tokens"))
    if known:
        bucket["cost"] += cost
        bucket["cost_calls"] += calls
    gaps = data.gaps.get(session["id"])
    if gaps:
        bucket["gaps"] += gaps[0]
        bucket["p5"] += gaps[1]
        bucket["p60"] += gaps[2]
    source = bucket["sources"].setdefault(str(session.get("source") or "unknown"), {"calls": 0, "cost": 0.0})
    source["calls"] += calls
    if known:
        source["cost"] += cost
    for name, (n, chars) in (data.tools.get(session["id"]) or {}).items():
        entry = bucket["tools"].setdefault(name, [0, 0])
        entry[0] += n
        entry[1] += chars
    if include is not None and not include(session):
        return
    if session["id"] in data.mixed:
        bucket["mixed"] += 1
        bucket["mixed_calls"] += calls
        return
    model = bucket["models"].setdefault(str(session.get("model") or "unknown"), {
        "sessions": 0, "calls": 0, "input": 0.0, "writes": 0.0, "cost": 0.0, "cost_calls": 0})
    model["sessions"] += 1
    model["calls"] += calls
    model["input"] += fresh + reads + writes
    model["writes"] += writes
    if known:
        model["cost"] += cost
        model["cost_calls"] += calls


def _ratio(num: float, den: float) -> Optional[float]:
    return num / den if den else None


def summarize(bucket: Dict[str, Any]) -> Dict[str, Any]:
    """The numbers of one bucket, in plain JSON-able form."""
    calls = bucket["calls"]
    inp = bucket["fresh"] + bucket["reads"] + bucket["writes"]
    sessions = bucket["sessions"]
    models = {}
    for name, m in bucket["models"].items():
        models[name] = {"sessions": m["sessions"], "calls": m["calls"], "priced_calls": m["cost_calls"],
                        "cost_per_call": _ratio(m["cost"], m["cost_calls"]),
                        "write_share": _ratio(m["writes"], m["input"]),
                        "cache_write_tokens": m["writes"], "writes_per_call": _ratio(m["writes"], m["calls"])}
    tools = {}
    for name, (n, chars) in bucket["tools"].items():
        tools[name] = {"calls": n, "calls_per_session": _ratio(n, sessions), "avg_tokens": _ratio(chars / 4.0, n)}
    background = sum(s["calls"] for name, s in bucket["sources"].items() if name in MACHINE_PACED_SOURCES)
    return {
        "sessions": sessions, "calls": calls, "input_tokens": inp, "cache_read_tokens": bucket["reads"],
        "cache_write_tokens": bucket["writes"], "fresh_tokens": bucket["fresh"], "output_tokens": bucket["out"],
        "avg_context": _ratio(inp, calls), "read_share": _ratio(bucket["reads"], inp),
        "write_share": _ratio(bucket["writes"], inp), "writes_per_call": _ratio(bucket["writes"], calls),
        "cost": bucket["cost"] if bucket["cost_calls"] else None,
        "cost_per_call": _ratio(bucket["cost"], bucket["cost_calls"]),
        "priced_calls": bucket["cost_calls"], "unpriced_calls": calls - bucket["cost_calls"],
        "pauses_5_60": bucket["p5"], "pauses_over_60": bucket["p60"],
        "p5_per_100_calls": _ratio(100.0 * bucket["p5"], calls), "p60_per_100_calls": _ratio(100.0 * bucket["p60"], calls),
        "background_share": _ratio(background, calls),
        "mixed_sessions": bucket.get("mixed", 0), "mixed_calls": bucket.get("mixed_calls", 0),
        "models": models, "tools": tools,
    }


def monday_of(ts: float) -> datetime:
    day = datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def weekly(data: Data, first_monday: datetime, weeks: int,
           include: Optional[Callable[[Dict[str, Any]], bool]] = None) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for session in data.sessions:
        label = monday_of(float(session["started_at"])).strftime("%Y-%m-%d")
        _add(buckets.setdefault(label, _new_bucket()), session, data, include)
    rows = []
    for k in range(weeks):
        label = (first_monday + timedelta(days=7 * k)).strftime("%Y-%m-%d")
        row = summarize(buckets.get(label) or _new_bucket())
        row["week"] = label
        rows.append(row)
    return rows


def whole(data: Data, include: Optional[Callable[[Dict[str, Any]], bool]] = None) -> Dict[str, Any]:
    bucket = _new_bucket()
    for session in data.sessions:
        _add(bucket, session, data, include)
    return summarize(bucket)


def is_claude(model: str) -> bool:
    name = (model or "").lower()
    return "claude" in name or name.startswith("anthropic/")


def _priced_calls(model: Dict[str, Any]) -> float:
    """Calls that have a price (markers saved by 1.2.0 did not keep the split: all of their calls count)."""
    value = model.get("priced_calls")
    return float(value if value is not None else model.get("calls") or 0)


def _like_models(before: Dict[str, Any], after: Dict[str, Any], only: Optional[Callable[[str], bool]]) -> List[str]:
    """Models used on both sides with a cost per call on both (and accepted by ``only``)."""
    names = []
    for name, a in (after.get("models") or {}).items():
        if only is not None and not only(name):
            continue
        b = (before.get("models") or {}).get(name)
        if b and a.get("cost_per_call") is not None and b.get("cost_per_call") is not None:
            names.append(name)
    return names


def like_for_like(before: Dict[str, Any], after: Dict[str, Any], min_calls: int = MIN_LIKE_CALLS,
                  only: Optional[Callable[[str], bool]] = None) -> Optional[Dict[str, Any]]:
    """Cost per call before and after, for the models used on both sides only (and, with ``only``, only the
    models it accepts), weighted by how many priced calls each had after. ``weights`` keeps those weights so the
    usual week-to-week swing can be rebuilt for exactly the same models in the same mix."""
    names, calls, sessions, before_sum, after_sum = [], 0.0, 0, 0.0, 0.0
    weights: Dict[str, float] = {}
    for name in _like_models(before, after, only):
        a, b = after["models"][name], before["models"][name]
        weight = _priced_calls(a)
        if weight < min_calls or _priced_calls(b) < min_calls:
            continue
        names.append(name)
        weights[name] = weight
        calls += weight
        sessions += int(a.get("sessions") or 0)
        before_sum += b["cost_per_call"] * weight
        after_sum += a["cost_per_call"] * weight
    if not names or not calls or not before_sum:
        return None
    return {"models": sorted(names), "calls": calls, "sessions": sessions, "weights": weights,
            "before_per_call": before_sum / calls, "after_per_call": after_sum / calls,
            "change": after_sum / before_sum - 1.0}


def like_for_like_gap(before: Dict[str, Any], after: Dict[str, Any], min_calls: int = MIN_LIKE_CALLS,
                      only: Optional[Callable[[str], bool]] = None) -> str:
    """Why there is no like-for-like figure, in words."""
    shared = _like_models(before, after, only)
    if not shared:
        return "no model with a recorded price was used both before and after"
    if all(_priced_calls(after["models"][n]) < min_calls or _priced_calls(before["models"][n]) < min_calls
           for n in shared):
        return "the models used on both sides have fewer than %d priced calls on one side" % min_calls
    return "the shared models have no recorded cost before the change"


def week_models(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per week and model: priced calls and cost per call - the baseline a marker keeps."""
    out = []
    for row in rows:
        models = {}
        for name, m in (row.get("models") or {}).items():
            if m.get("cost_per_call") is not None and _priced_calls(m) > 0:
                models[name] = [_priced_calls(m), m["cost_per_call"]]
        if models:
            out.append({"week": row.get("week", ""), "models": models})
    return out


def like_for_like_weeks(history: Optional[Sequence[Dict[str, Any]]], weights: Dict[str, float]) -> List[float]:
    """Cost per call of each baseline week for the models of a like-for-like figure, in the same mix.

    A week counts only when every one of those models was used enough in it, so that the weekly figures and the
    "after" figure are made of the same ingredients.
    """
    total = float(sum(weights.values()))
    out: List[float] = []
    if not total:
        return out
    for week in history or []:
        models = week.get("models") or {}
        cost = calls = 0.0
        for name, weight in weights.items():
            entry = models.get(name)
            if not entry or entry[0] < MIN_WEEK_MODEL_CALLS:
                break
            cost += entry[1] * weight
            calls += entry[0]
        else:
            if calls >= MIN_WEEK_CALLS:
                out.append(cost / total)
    return out


def spread(values: Sequence[float]) -> Optional[Tuple[float, float]]:
    """The weeks' lowest and highest figure, if there are three or more. Only a description of the weeks: how far a new
    figure may stray by chance is ``prediction_interval`` (out of the min..max of a few weeks, a figure from the same
    distribution lands 2 times in n + 1)."""
    usable = [v for v in values if v is not None]
    return (min(usable), max(usable)) if len(usable) >= 3 else None


# Two-sided 95% Student t quantiles by degrees of freedom (index = df).
_T975 = (None, 12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
         2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042)


def _t975(df: int) -> float:
    """The 97.5% point of Student's t for ``df`` degrees of freedom: the table up to 30, then steps that are never
    meaningfully smaller than the true value (so an interval is never too narrow)."""
    if df < 1:
        df = 1
    if df <= 30:
        return float(_T975[df])
    for limit, value in ((40, 2.04), (60, 2.02), (120, 2.0), (500, 1.98)):
        if df <= limit:
            return value
    return 1.96


def prediction_interval(values: Sequence[Optional[float]], weeks_after: float = 1.0) -> Optional[Dict[str, float]]:
    """Where a new figure from the same distribution lands 19 times in 20, worked out from the earlier weekly figures:
    ``mean +/- t * sd * sqrt(1/k + 1/n)`` for ``n`` earlier weeks and a new figure that covers ``k`` weeks (at least
    one). None with fewer than three weeks: that is too little to say how much a week varies."""
    usable = [float(v) for v in values if _finite(v)]
    n = len(usable)
    if n < 3:
        return None
    mean = sum(usable) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in usable) / (n - 1))
    k = max(1.0, float(weeks_after)) if _finite(weeks_after) else 1.0
    half = _t975(n - 1) * sd * math.sqrt(1.0 / k + 1.0 / n)
    return {"n": n, "mean": mean, "sd": sd, "low": mean - half, "high": mean + half, "weeks_after": k}


def _norm_source(value: Any) -> str:
    """A session source the way Hermes reads it when it decides a cache lifetime (stripped, lower case)."""
    return str(value or "").strip().lower()


def _machine_paced(session: Dict[str, Any]) -> bool:
    return _norm_source(session.get("source")) in MACHINE_PACED_SOURCES


def _ttl_of(marker: Dict[str, Any]) -> str:
    return str((marker.get("settings") or {}).get("prompt_caching.cache_ttl") or "")


def _paced_by(ttl: str) -> Optional[Callable[[Dict[str, Any]], bool]]:
    """Which chats a cache-lifetime setting reaches, as Hermes decides it from a chat's source: with ``auto`` only chats
    a person paces (machine-paced sources stay on 5 minutes); with an explicit ``1h`` every chat but a delegated
    subagent run. None for any other value: every chat counts."""
    if ttl == "auto":
        return lambda session: not _machine_paced(session)
    if ttl == "1h":
        return lambda session: _norm_source(session.get("source")) != "subagent"
    return None


def _chat_keys(sessions: Sequence[Dict[str, Any]]) -> Dict[Any, Any]:
    """The chat each session belongs to: a session with a parent (a compression continuation, a subagent run, a branch)
    is counted with its parent, so one long chat that Hermes compressed several times is one chat. The parent may be
    outside the window; its id then stands for the chat."""
    by_id = {s["id"]: s for s in sessions}
    keys: Dict[Any, Any] = {}
    for session in sessions:
        key, seen, current = session["id"], {session["id"]}, session
        while True:
            parent = current.get("parent_session_id")
            if not parent or parent in seen:
                break
            seen.add(parent)
            key = parent
            current = by_id.get(parent)
            if current is None:
                break
        keys[session["id"]] = key
    return keys


def _priced_chats(data: Data, models: Sequence[str], include: Optional[Callable[[Dict[str, Any]], bool]]) -> int:
    """How many different chats, with a recorded price, stand behind a like-for-like figure."""
    names = set(models)
    keys = _chat_keys(data.sessions)
    chats = set()
    for session in data.sessions:
        if (str(session.get("model") or "unknown") in names and _priced(session) is not None
                and session["id"] not in data.mixed and (include is None or include(session))):
            chats.add(keys[session["id"]])
    return len(chats)


def _scope_writes(summary: Dict[str, Any], only: Optional[Callable[[str], bool]]) -> Optional[float]:
    """Cache-write tokens per call of the models ``only`` accepts; None when a summary did not keep the numbers."""
    calls = writes = 0.0
    for name, m in (summary.get("models") or {}).items():
        if only is not None and not only(name):
            continue
        if m.get("cache_write_tokens") is None:
            return None
        calls += float(m.get("calls") or 0)
        writes += float(m["cache_write_tokens"])
    return writes / calls if calls else None


def _human_claude(data: Data) -> Dict[str, Any]:
    """Pauses and cache writes of the chats a longer cache lifetime could help: Claude models, human-paced sources."""
    bucket = _new_bucket()
    for session in data.sessions:
        if is_claude(str(session.get("model") or "")) and not _machine_paced(session):
            _add(bucket, session, data)
    s = summarize(bucket)
    return {"sessions": s["sessions"], "calls": s["calls"], "pauses_5_60": s["pauses_5_60"],
            "write_share": s["write_share"]}


# ----------------------------------------------------------------------------- settings and markers

def _scan_setting(text: str, section: str, key: str) -> Optional[str]:
    """The value of ``section: key:`` in a config file without a YAML library."""
    inside = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            head, _, rest = line.partition(":")
            inside = head.strip() == section and rest.split("#", 1)[0].strip() == ""
            continue
        if inside:
            name, _, rest = line.strip().partition(":")
            if name == key:
                value = rest.split(" #", 1)[0].strip().strip("'\"")
                return value or None
    return None


def read_settings(home: Any) -> Dict[str, Any]:
    """The handful of Hermes settings that move cost, as they are in config.yaml (None = not set)."""
    try:
        text = (Path(home) / "config.yaml").read_text(encoding="utf-8-sig")
    except (OSError, ValueError):           # missing, unreadable, or not UTF-8: the settings only decorate the report
        return {}
    parsed: Any = None
    try:
        import yaml  # present inside Hermes; the standalone tool falls back to a plain scan
        parsed = yaml.safe_load(text)
    except Exception:
        parsed = None
    settings: Dict[str, Any] = {}
    for section, key in SETTINGS:
        value = None
        if isinstance(parsed, dict):
            block = parsed.get(section)
            value = block.get(key) if isinstance(block, dict) else None
        elif parsed is None:
            value = _scan_setting(text, section, key)
        settings["%s.%s" % (section, key)] = value
    return settings


def markers_path(home: Any) -> Path:
    return data_dir(home) / MARKERS_NAME


_BEFORE_NUMBERS = ("unpriced_calls", "priced_calls", "pauses_5_60", "pauses_over_60", "input_tokens", "cache_read_tokens",
                   "cache_write_tokens", "fresh_tokens", "output_tokens", "background_share", "mixed_sessions", "mixed_calls")


def _valid_before(before: Any) -> bool:
    """A saved baseline whose numbers can all be formatted (a hand-edited file must not crash the report)."""
    if not isinstance(before, dict):
        return False
    if not (_finite(before.get("sessions")) and _finite(before.get("calls"))):
        return False
    for key in [row[1] for row in _ROWS] + list(_BEFORE_NUMBERS):
        if before.get(key) is not None and not _finite(before[key]):
            return False
    for table, numbers in (("models", ("calls", "priced_calls", "cost_per_call", "sessions", "write_share",
                                       "cache_write_tokens", "writes_per_call")),
                           ("tools", ("calls", "calls_per_session", "avg_tokens"))):
        entries = before.get(table, {})
        if not isinstance(entries, dict):
            return False
        for entry in entries.values():
            if not isinstance(entry, dict):
                return False
            if any(entry.get(k) is not None and not _finite(entry[k]) for k in numbers):
                return False
        if table == "models" and any(not _finite(e.get("calls")) for e in entries.values()):
            return False
    return True


def _clean_history(history: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(history, list):
        return None
    out = []
    for week in history:
        if not isinstance(week, dict) or not isinstance(week.get("models"), dict):
            continue
        models = {}
        for name, entry in week["models"].items():
            if (isinstance(name, str) and isinstance(entry, (list, tuple)) and len(entry) == 2
                    and _finite(entry[0]) and _finite(entry[1])):
                models[name] = [float(entry[0]), float(entry[1])]
        out.append({"week": str(week.get("week") or ""), "models": models})
    return out


def _clean_marker(item: Any) -> Optional[Dict[str, Any]]:
    """A copy of a saved marker that the rest of the code can rely on, or None. Reading only: the file keeps the entry
    as it was written (``--mark`` appends to the raw list)."""
    if not isinstance(item, dict):
        return None
    label, at = item.get("label"), item.get("at")
    if not isinstance(label, str) or not label or not _finite(at) or not MIN_MARKER_AT <= at <= MAX_MARKER_AT:
        return None
    marker = dict(item)
    marker["at"] = float(at)
    if marker.get("before") is not None and not _valid_before(marker["before"]):
        return None
    if not isinstance(marker.get("settings"), dict):
        marker["settings"] = {}
    if "before_week_models" in marker:
        marker["before_week_models"] = _clean_history(marker["before_week_models"])
    days = marker.get("baseline_days")
    if not (_finite(days) and 1 <= days <= MAX_BASELINE_DAYS):
        marker["baseline_days"] = None
    tracked = marker.get("tools_tracked")
    if tracked is not None and not (isinstance(tracked, list) and all(isinstance(t, str) for t in tracked)):
        marker["tools_tracked"] = None
    if not isinstance(marker.get("upgrade_note"), str):
        marker["upgrade_note"] = None
    return marker


def _read_marker_file(path: Path) -> Tuple[Optional[bytes], Optional[str]]:
    """The bytes of the marker file (None when there is no file) and a sentence when it could not be read. Windows
    refuses to open a file for a moment while a ``--mark`` replaces it: that is retried, it is not damage."""
    for attempt in range(MARKER_READ_TRIES):
        try:
            return path.read_bytes(), None
        except (FileNotFoundError, NotADirectoryError):
            return None, None
        except PermissionError as exc:
            if attempt + 1 < MARKER_READ_TRIES:
                time.sleep(0.05)
                continue
            return None, "%s could not be read (%s)" % (MARKERS_NAME, exc)
        except OSError as exc:
            return None, "%s could not be read (%s)" % (MARKERS_NAME, exc)
    return None, "%s could not be read" % MARKERS_NAME


def load_markers(home: Any) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """The markers that can be used, and a sentence about what is wrong with the file (None = nothing)."""
    raw, problem = _read_marker_file(markers_path(home))
    if raw is None:
        return [], problem
    try:
        loaded = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        return [], "%s is not valid JSON (%s)" % (MARKERS_NAME, exc)
    items = loaded.get("markers") if isinstance(loaded, dict) else None
    if not isinstance(items, list):
        return [], "%s has no list of markers" % MARKERS_NAME
    good: List[Dict[str, Any]] = []
    skipped, seen = 0, set()
    for item in items:
        marker = _clean_marker(item)
        if marker is None or marker["label"] in seen:
            skipped += 1
            continue
        seen.add(marker["label"])
        good.append(marker)
    note = None
    if skipped:
        note = "%d entr%s in %s could not be used" % (skipped, "y" if skipped == 1 else "ies", MARKERS_NAME)
    return good, note


def read_markers(home: Any) -> List[Dict[str, Any]]:
    return load_markers(home)[0]


def _posix() -> bool:
    return os.name != "nt"


def _make_data_dir(directory: Path) -> None:
    """Create the plugin's data folder. One this code creates itself starts private (owner only) on macOS and Linux, as
    the bank's own does; a folder that already exists is left as it is."""
    made = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    if made and _posix():
        try:
            directory.chmod(0o700)
        except OSError:
            pass


def _break_stale_lock(lock: str) -> bool:
    """Take away a lock that a run which died left behind. Of several runs doing this at once only one wins (the move is
    atomic); and if what was moved is not the stale file that was looked at - a live run took its place in between - it
    is put back and nothing is taken."""
    try:
        seen = os.stat(lock)
    except OSError:
        return False
    if time.time() - seen.st_mtime <= LOCK_STALE_SECONDS:
        return False
    mine = "%s.%d.%s.stale" % (lock, os.getpid(), os.urandom(4).hex())
    try:
        os.replace(lock, mine)
    except OSError:
        return False                        # another run got there first
    try:
        moved = os.stat(mine)
        if (moved.st_ino, moved.st_mtime_ns) == (seen.st_ino, seen.st_mtime_ns):
            return True
        try:
            os.link(mine, lock)             # a live lock: give it back (fails if somebody has made a new one: leave it)
        except OSError:
            pass
        return False
    except OSError:
        return False
    finally:
        try:
            os.unlink(mine)
        except OSError:
            pass


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """One ``--mark`` at a time: the read-modify-write of the marker file must not interleave."""
    try:
        _make_data_dir(path.parent)
    except OSError as exc:
        raise UsageError("could not save the marker in %s: %s" % (path.parent, exc))
    lock = str(path) + ".lock"
    deadline = time.time() + LOCK_WAIT_SECONDS
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except (FileExistsError, PermissionError) as exc:
            if not os.path.exists(lock) and isinstance(exc, PermissionError) and not os.access(str(path.parent), os.W_OK):
                raise UsageError("could not save the marker in %s: %s" % (path.parent, exc))
            if _break_stale_lock(lock):         # left behind by a run that died
                continue
            if time.time() > deadline:
                raise UsageError("another 'usage --mark' seems to be running (%s exists); try again in a moment"
                                 % Path(lock).name)
            time.sleep(0.05)
        except OSError as exc:
            raise UsageError("could not save the marker in %s: %s" % (path.parent, exc))
    try:
        yield
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def _raw_markers(home: Any) -> Tuple[Optional[Dict[str, Any]], List[Any]]:
    """The marker file as it is - its top-level object and its list of entries, untouched - so that what is written back
    keeps every entry (and every key) this version does not understand exactly as it was."""
    raw, _problem = _read_marker_file(markers_path(home))
    if raw is None:
        return None, []
    try:
        loaded = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None, []
    items = loaded.get("markers") if isinstance(loaded, dict) else None
    return (loaded if isinstance(loaded, dict) else None), (list(items) if isinstance(items, list) else [])


def _write_markers(home: Any, markers: List[Any], top: Optional[Dict[str, Any]] = None) -> Path:
    path = markers_path(home)
    payload: Dict[str, Any] = dict(top) if isinstance(top, dict) else {}
    payload["markers"] = markers
    try:
        _make_data_dir(path.parent)
        fd, tmp = tempfile.mkstemp(prefix=".usage-", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=1, default=str)
                handle.write("\n")
            for attempt in range(20):               # Windows refuses a replace while a reader has the file open
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError as exc:
        raise UsageError("could not save the marker in %s: %s" % (path, exc))
    return path


def _weeks_between(first: datetime, last: datetime) -> int:
    """How many Mondays there are from ``first`` to ``last``, both included."""
    return max(1, (last - first).days // 7 + 1)


def _upgrade(home: Any, marker: Dict[str, Any], tools: Sequence[str]) -> Dict[str, Any]:
    """A marker saved before per-model weeks were kept (1.2.0): rebuild its baseline from state.db - but only when the
    chats there still add up to what the marker recorded (the same number of calls, give or take 2%). Chats Hermes has
    pruned, or a chat that began before the marker and has grown since, would move the baseline after the fact: then the
    marker keeps the summary it saved itself and no usual range is claimed for it."""
    if marker.get("before_week_models") is not None or not marker.get("before"):
        return marker                    # already in the new form, or nothing was recorded before it

    def kept(why: str) -> Dict[str, Any]:
        return dict(marker, before_week_models=[],
                    upgrade_note="the usual range cannot be rebuilt for this older marker: %s" % why)

    days = int(marker.get("baseline_days") or BASELINE_DAYS)
    at = float(marker["at"])
    try:
        data = load(home, at - days * 86400.0, at, tools)
    except UsageError:
        return kept("state.db could not be read")
    include = _paced_by(_ttl_of(marker))
    fresh = whole(data, include)
    stored = float(marker["before"].get("calls") or 0)
    if not data.sessions or abs(fresh["calls"] - stored) > 0.02 * stored:
        return kept("its earlier chats no longer match what it recorded")
    first = monday_of(at - days * 86400.0)
    rows = weekly(data, first, _weeks_between(first, monday_of(at)), include)
    return dict(marker, before=fresh, before_week_models=week_models(rows), tools_tracked=list(tools))


def _save_upgrade(home: Any, upgraded: Dict[str, Any]) -> None:
    """Keep an upgraded marker in the file, once, so that its baseline stops being rebuilt (and moving) on every report.
    Best effort: a report must still work from a folder it cannot write to."""
    path = markers_path(home)
    try:
        with _locked(path):
            top, raw = _raw_markers(home)
            for item in raw:
                if (isinstance(item, dict) and item.get("label") == upgraded["label"]
                        and item.get("before_week_models") is None):          # nobody else upgraded it meanwhile
                    for key in ("before", "before_week_models", "tools_tracked", "upgrade_note"):
                        if upgraded.get(key) is not None:
                            item[key] = upgraded[key]
                    _write_markers(home, raw, top)
                    return
    except UsageError:
        return


# ----------------------------------------------------------------------------- the report

def _day(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return "(date out of range)"


def build(home: Any, *, now: float, weeks: int = 8, since: Optional[float] = None, vs: Optional[str] = None,
          tools: Sequence[str] = DEFAULT_TOOLS) -> Dict[str, Any]:
    """Everything the report shows, as plain data."""
    first = monday_of(now) - timedelta(days=7 * (max(1, weeks) - 1))
    start = float(since) if since is not None else first.timestamp()
    data = load(home, start, now + 1.0, tools)
    result: Dict[str, Any] = {
        "home": str(home), "generated_at": now, "settings": read_settings(home),
        "pauses_known": data.pauses_known, "tools_known": data.tools_known,
        "window": {"from": start, "to": now}, "summary": whole(data), "idea": _human_claude(data),
    }
    if since is None:
        result["weeks"] = weekly(data, first, max(1, weeks))
    markers, problem = load_markers(home)
    if problem:
        result["markers_note"] = problem
    chosen = None
    if vs:
        chosen = next((m for m in markers if m["label"] == vs), None)
        if chosen is None:
            raise UsageError("no marker called %r (markers: %s)%s" % (
                vs, ", ".join(m["label"] for m in markers) or "none yet", "; " + problem if problem else ""))
    elif markers:
        chosen = max(markers, key=lambda m: m["at"])
    result["markers"] = [{"label": m["label"], "at": m["at"]} for m in markers]
    if chosen is not None:
        try:
            result["comparison"] = _compare(home, chosen, now, tools)
        except UsageError:
            raise
        except Exception as exc:  # noqa: BLE001 - whatever a hand-edited or newer marker makes of the arithmetic
            note = _unusable_entry(chosen, exc)
            result["markers_note"] = "%s; %s" % (result["markers_note"], note) if result.get("markers_note") else note
    return result


def _unusable_entry(marker: Dict[str, Any], exc: Exception) -> str:
    return "1 entry in %s could not be used (\"%s\": %s: %s)" % (MARKERS_NAME, marker.get("label"), type(exc).__name__, exc)


def _compare(home: Any, chosen: Dict[str, Any], now: float, tools: Sequence[str]) -> Dict[str, Any]:
    """The before/after comparison for one marker."""
    upgraded = _upgrade(home, chosen, tools)
    if upgraded is not chosen:
        _save_upgrade(home, upgraded)               # once: from now on the baseline is the marker's own
    chosen = upgraded
    ttl = _ttl_of(chosen)
    include = _paced_by(ttl)                        # the chats a cache-lifetime setting reaches (None: every chat)
    after_data = load(home, float(chosen["at"]), now + 1.0, tools)
    after = whole(after_data, include)
    before = chosen.get("before")
    scope = "claude" if ttl in CACHE_SCOPE_TTLS else None      # a cache setting mainly affects Claude models
    in_scope = is_claude if scope else None
    scoped = [m for name, m in (after.get("models") or {}).items() if in_scope is None or in_scope(name)]
    days_after = max(0.0, (now - float(chosen["at"])) / 86400.0)
    comparison: Dict[str, Any] = {
        "label": chosen["label"], "at": chosen["at"], "days_after": days_after,
        "settings_then": chosen.get("settings") or {}, "baseline_days": chosen.get("baseline_days"),
        "before": before, "after": after, "pauses_known": after_data.pauses_known, "scope": scope,
        "cache_ttl": ttl or None, "tools_tracked": chosen.get("tools_tracked"), "upgrade_note": chosen.get("upgrade_note"),
        "scope_sessions": sum(m["sessions"] for m in scoped), "scope_calls": sum(m["calls"] for m in scoped),
    }
    if before:
        lfl = like_for_like(before, after, only=in_scope)
        comparison["like_for_like"] = lfl
        if lfl is None:
            comparison["like_for_like_gap"] = like_for_like_gap(before, after, only=in_scope)
            comparison["weekly_swing"] = comparison["weekly_interval"] = None
            comparison["weekly_n"] = 0
        else:
            lfl["priced_chats"] = _priced_chats(after_data, lfl["models"], include)
            weeks = like_for_like_weeks(chosen.get("before_week_models"), lfl["weights"])
            comparison["weekly_swing"] = spread(weeks)                    # what the weeks were: not what decides a verdict
            comparison["weekly_interval"] = prediction_interval(weeks, days_after / 7.0)
            comparison["weekly_n"] = len(weeks)
        comparison["enough"] = (comparison["scope_sessions"] >= MIN_SESSIONS_FOR_VERDICT
                                and comparison["scope_calls"] >= MIN_CALLS_FOR_VERDICT)
        if scope:
            comparison["scope_writes_per_call"] = {"before": _scope_writes(before, in_scope),
                                                   "after": _scope_writes(after, in_scope)}
    return comparison


def mark(home: Any, label: str, *, now: float, baseline_days: int = BASELINE_DAYS,
         tools: Sequence[str] = DEFAULT_TOOLS) -> Dict[str, Any]:
    """Record that a setting was changed now, keeping the weeks before it as the baseline."""
    if not _LABEL.fullmatch(label or ""):
        raise UsageError("a marker label is 1-40 letters, digits, dots, dashes or underscores")
    if not 1 <= int(baseline_days) <= MAX_BASELINE_DAYS:
        raise UsageError("--baseline-days wants a number from 1 to %d" % MAX_BASELINE_DAYS)
    if not Path(home).is_dir():
        raise UsageError("no such Hermes folder: %s" % home)
    if not (_finite(now) and MIN_MARKER_AT <= now <= MAX_MARKER_AT):
        raise UsageError("this computer's clock is outside the years 1980-2100, so a marker would not be usable; nothing was saved")

    def check(markers: List[Dict[str, Any]], problem: Optional[str]) -> None:
        if problem:
            raise UsageError("%s - fix or move that file aside first, it holds your earlier baselines" % problem)
        if any(m["label"] == label for m in markers):
            raise UsageError("a marker called %r already exists; pick another label" % label)

    check(*load_markers(home))                      # fail early, before reading state.db
    settings = read_settings(home)
    include = _paced_by(str(settings.get("prompt_caching.cache_ttl") or ""))      # the per-model baseline: chats the setting reaches
    try:
        data = load(home, now - baseline_days * 86400.0, now, tools)
    except UsageError:
        if state_db(home).exists():
            raise                                   # unreadable: no marker without its baseline
        data = Data()                               # no state.db yet: marking before the first chat is fine
    before = whole(data, include) if data.sessions else None
    first = monday_of(now - baseline_days * 86400.0)
    week_rows = weekly(data, first, _weeks_between(first, monday_of(now)), include) if data.sessions else []
    marker = {"label": label, "at": now, "at_text": _day(now), "baseline_days": baseline_days, "settings": settings,
              "before": before, "before_week_models": week_models(week_rows), "tools_tracked": list(tools)}
    with _locked(markers_path(home)):
        markers, problem = load_markers(home)       # again, now that nobody else can write
        check(markers, problem)
        top, raw = _raw_markers(home)               # entries are kept exactly as they are in the file, whatever they hold
        if len(raw) < len(markers):
            raise UsageError("%s changed while the marker was being saved; nothing was written - run the command again" % MARKERS_NAME)
        raw.append(marker)
        _write_markers(home, raw, top)
    return marker


# ----------------------------------------------------------------------------- text output

def _k(value: Optional[float]) -> str:
    if value is None:
        return "-"
    value = float(value)
    magnitude = abs(value)
    if magnitude >= 1e6 or round(magnitude / 1e3, 1) >= 1000.0:
        return "%.1fM" % (value / 1e6)
    if magnitude >= 1e3:
        return "%.1fk" % (value / 1e3)
    return "%d" % round(value)


def _n(value: Optional[float]) -> str:
    return "-" if value is None else "{:,}".format(int(round(value)))


def _pct(value: Optional[float], digits: int = 0) -> str:
    return "-" if value is None else "%.*f%%" % (digits, 100.0 * value)


def _usd(value: Optional[float], small: bool = False) -> str:
    if value is None:
        return "-"
    return "$%.4f" % value if small else "${:,.2f}".format(value)


def _signed(delta: float) -> str:
    """A change as a signed whole percent; a move too small to show is plain 0%."""
    return "0%" if abs(100.0 * delta) < 0.5 else "%+.0f%%" % (100.0 * delta)


def _change(before: Optional[float], after: Optional[float]) -> str:
    if before in (None, 0) or after is None or before < 0:
        return ""
    return _signed(after / before - 1.0)


def _rate(value: Optional[float]) -> str:
    return "-" if value is None else "%.1f" % value


# (label, key, formatter, comparable): totals over windows of different length are not comparable, rates are.
_ROWS: Tuple[Tuple[str, str, Callable[[Optional[float]], str], bool], ...] = (
    ("sessions", "sessions", _n, False),
    ("API calls", "calls", _n, False),
    ("average context per call (tokens)", "avg_context", _k, True),
    ("cache reads, share of input", "read_share", _pct, True),
    ("cache writes, share of input", "write_share", lambda v: _pct(v, 1), True),
    ("cache-write tokens per call", "writes_per_call", _k, True),
    ("pauses of 5-60 min, per 100 calls", "p5_per_100_calls", _rate, True),
    ("pauses over 1 h, per 100 calls", "p60_per_100_calls", _rate, True),
    ("estimated cost per call", "cost_per_call", lambda v: _usd(v, True), True),
    ("estimated cost, total", "cost", _usd, False),
)


def _weekly_lines(rows: List[Dict[str, Any]]) -> List[str]:
    lines = ["Week of     sessions   calls  avg ctx  reads  writes     est. $   $/call  pauses>5m /100 calls",
             "----------  --------  ------  -------  -----  ------  ---------  -------  --------------------"]
    for r in rows:
        if not r["calls"]:
            lines.append("%-10s  %8s" % (r["week"], "-"))
            continue
        pauses = None if r["p5_per_100_calls"] is None else r["p5_per_100_calls"] + (r["p60_per_100_calls"] or 0.0)
        lines.append("%-10s  %8d  %6s  %7s  %5s  %6s  %9s  %7s  %20s" % (
            r["week"], r["sessions"], _n(r["calls"]), _k(r["avg_context"]), _pct(r["read_share"]),
            _pct(r["write_share"], 1), _usd(r["cost"]), _usd(r["cost_per_call"], True), _rate(pauses)))
    return lines


def _table_lines(header: List[str], columns: List[Dict[str, Any]], with_change: bool,
                 blank: Sequence[str] = ()) -> List[str]:
    """The before/after table. ``blank`` names rows whose change would look comparable but is not."""
    width = 36
    out = [" " * width + "".join("%18s" % h for h in header) + ("%10s" % "change" if with_change else "")]
    for label, key, fmt, comparable in _ROWS:
        cells = [fmt(c.get(key)) for c in columns]
        extra = ""
        if with_change and len(columns) == 2:
            extra = "%10s" % (_change(columns[0].get(key), columns[1].get(key)) if comparable and key not in blank else "")
        out.append("%-*s" % (width, label) + "".join("%18s" % c for c in cells) + extra)
    return out


def _tools_lines(before: Optional[Dict[str, Any]], after: Dict[str, Any], tracked: Optional[Sequence[str]] = None) -> List[str]:
    """Tool habits, before -> after. ``tracked`` is the tool list the marker was set with: a tool outside it was not
    watched then, which is not the same as not used."""
    names = sorted(set((before or {}).get("tools") or {}) | set(after.get("tools") or {}))
    if not names:
        return []
    lines = ["", "Tools the model leaned on (calls per session x average result size in tokens)"]

    def cell(t: Optional[Dict[str, Any]]) -> str:
        return "-" if not t else "%.1f x %s" % (_num(t.get("calls_per_session")), _k(t.get("avg_tokens")))

    for name in names:
        b = ((before or {}).get("tools") or {}).get(name)
        a = (after.get("tools") or {}).get(name)
        was = cell(b) if (b or tracked is None or name in tracked) else "(not tracked then)"
        lines.append("  %-16s %s%s" % (name, ("%16s  ->  " % was) if before else "", cell(a)))
    return lines


def _unpriced_lines(*periods: Tuple[str, Optional[Dict[str, Any]]]) -> List[str]:
    """Calls that have no price are left out of the cost rows - say how many, so a low figure is not a surprise."""
    out = []
    for label, s in periods:
        n = (s or {}).get("unpriced_calls") or 0
        if n > 0 and s and s.get("calls"):
            out.append("%s%s of %s calls have no recorded price (in a plan, or unpriced); cost rows leave them out."
                       % (label + ": " if label else "", _n(n), _n(s["calls"])))
    return out


def _left_out_lines(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> List[str]:
    """Chats that ran on more than one model route cannot be put under any one model: they are in the totals only."""
    b, a = (before or {}).get("mixed_sessions"), after.get("mixed_sessions")
    if not (b or a):
        return []
    return ["Chats that switched models or providers are in the totals, but left out of the same-model figures: %s before, %s after."
            % (_n(b), _n(a))]


def render(result: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    home = Path(result["home"])
    out.append("Usage  %s" % home.name)
    out.append("(costs are Hermes' own list-price estimates, not a bill; state.db is only read)")
    out.append("(main-loop chat calls only: compression, vision, title and background-review calls are not included)")
    s = result.get("summary") or {}
    comparison = result.get("comparison")
    if not s.get("sessions"):
        out.append("")
        out.append("No chats with model calls in this period (%s to %s)." % (_day(result["window"]["from"]), _day(result["window"]["to"])))
    elif "weeks" in result:
        out.append("")
        out.append("By week the chat started (local time, weeks start on Monday)")
        out.extend("  " + line for line in _weekly_lines(result["weeks"]))
        out.append("  (the last row is the week so far)")
    else:
        out.append("")
        out.append("Since %s" % _day(result["window"]["from"]))
        out.extend(_table_lines(["this period"], [s], False))
    if s.get("sessions"):
        unpriced = _unpriced_lines(("", s))
        if unpriced:
            out.append("")
            out.extend(unpriced)
    if s.get("sessions") and not result.get("pauses_known"):
        out.append("")
        out.append("(this state.db has no message timestamps, so cache-cold pauses could not be counted)")
    if s.get("background_share"):
        out.append("")
        out.append("%s of calls came from background runs (cron, subagents, webhooks, API)." % _pct(s["background_share"]))
    if s.get("sessions") and not comparison:
        out.extend(_tools_lines(None, s))
    if result.get("markers_note"):
        out.append("")
        out.append("(note: %s; before/after comparisons use the usable entries only)" % result["markers_note"])
    if comparison:
        out.append("")
        try:
            out.extend(_comparison_lines(comparison))
        except Exception as exc:  # noqa: BLE001 - a marker whose numbers cannot be shown is reported, not a traceback
            out.append("(note: %s)" % _unusable_entry(comparison, exc))
    elif s.get("sessions") and "weeks" in result:
        out.extend(_idea_lines(result))
    return out


def _verdict_lines(c: Dict[str, Any], lfl: Optional[Dict[str, Any]], before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """What the before/after numbers allow us to say - only as much as the like-for-like figure carries."""
    out: List[str] = []
    if lfl is None:
        b, a = before.get("cost_per_call"), after.get("cost_per_call")
        if b and a is not None:
            out.append("Cost per call %s -> %s (%s) over all models, which is not like for like."
                       % (_usd(b, True), _usd(a, True), _signed(a / b - 1.0)))
        out.append("No verdict: %s." % (c.get("like_for_like_gap") or "there is no like-for-like figure"))
        return out
    out.append("Cost per call %s -> %s (%s, same models)." % (
        _usd(lfl["before_per_call"], True), _usd(lfl["after_per_call"], True), _signed(lfl["change"])))
    chats = int(lfl.get("priced_chats", lfl.get("sessions")) or 0)
    if lfl["calls"] < MIN_CALLS_FOR_VERDICT or chats < MIN_SESSIONS_FOR_VERDICT:
        out.append("No verdict yet: that figure rests on %s priced calls in %d priced chat%s."
                   % (_n(lfl["calls"]), chats, "" if chats == 1 else "s"))
        out.append("It takes about %s calls in at least %d chats (a chat Hermes compressed counts once)."
                   % (_n(MIN_CALLS_FOR_VERDICT), MIN_SESSIONS_FOR_VERDICT))
        return out
    interval = c.get("weekly_interval")
    if not interval:
        if c.get("upgrade_note"):
            out.append("No verdict: there is no usual range to compare against for this older marker.")
        else:
            out.append("No verdict: too few earlier weeks with these models to say what a normal week-to-week swing is (%d of the 3 needed)."
                       % int(c.get("weekly_n") or 0))
        return out
    n, k = int(interval["n"]), float(interval.get("weeks_after") or 1.0)
    low, high, new = max(0.0, interval["low"]), interval["high"], lfl["after_per_call"]
    out.append("Before the change, weekly cost per call for these models averaged %s over %d weeks."
               % (_usd(interval["mean"], True), n))
    out.append("19 of 20 ordinary %s would stay between %s and %s (worked out from those %d weeks)."
               % ("weeks" if k <= 1.0 else "%.1f-week stretches" % k, _usd(low, True), _usd(high, True), n))
    # compared as printed: a figure that shows as the edge of the range is inside it
    shown_new, shown_low, shown_high = (float("%.4f" % v) for v in (new, low, high))
    if shown_low <= shown_new <= shown_high:
        out.append("The new figure is inside that range, so there is no clear effect yet.")
        if interval["mean"] > 0:        # what this much history can and cannot show, so "inside" is not read as "almost"
            reach = int(round(100.0 * (high - interval["mean"]) / interval["mean"]))
            out.append("(With %d weeks of history only %s of more than about %d%% from the usual %s would stand out%s.)" % (
                n, "a move" if low > 0 else "a rise", reach, _usd(interval["mean"], True), "" if low > 0 else "; no drop could"))
    else:
        below = shown_new < shown_low
        out.append("The new figure is %s that range. That points to a %s cost," % (
            "below" if below else "above", "lower" if below else "higher"))
        out.append("but %streat it as a lead, not a proof." % ("%d weeks of history is thin, so " % n if n < THIN_HISTORY_WEEKS else ""))
    days = c.get("days_after")
    if _finite(days) and days < 7.0:
        out.append("(Only %.1f days have passed since the change, less than the week this range is made for: it is a little too" % days)
        out.append("narrow for so short a time.)")
    if c.get("cache_ttl") in CACHE_SCOPE_TTLS:
        out.append("(List-price estimates book cache writes at the 5-minute price, so this cost can leave out what 1-hour writes add.)")
    return out


def _comparison_lines(c: Dict[str, Any]) -> List[str]:
    before, after = c.get("before"), c["after"]
    out = ["Before / after  \"%s\"  (marked %s)" % (c["label"], _day(c["at"]))]
    then = c.get("settings_then") or {}
    shown = ["%s=%s" % (k.replace("prompt_caching.", ""), v) for k, v in then.items()
             if v is not None and k.startswith(("prompt_caching", "compression"))]
    if then and "prompt_caching.cache_ttl" in then and then["prompt_caching.cache_ttl"] is None:
        shown.insert(0, "cache_ttl=default (5m)")
    if shown:
        out.append("  settings when marked: " + ", ".join(shown))
    if not before:
        out.append("  No earlier chats were recorded when the marker was set, so there is nothing to compare against.")
        out.append("  After the marker: %s sessions, %s calls." % (_n(after["sessions"]), _n(after["calls"])))
        out.extend(_tools_lines(None, after))
        return out
    lfl = c.get("like_for_like")
    head = ["before (%sd)" % (c.get("baseline_days") or "?"), "after (%.1fd)" % c["days_after"]]
    # with no like-for-like figure the all-models cost per call is a different mix of models, not a change
    out.extend(_table_lines(head, [before, after], True, blank=() if lfl else ("cost_per_call",)))
    scope = c.get("scope")
    ttl = c.get("cache_ttl")
    kind = "Claude " if scope else ""
    if lfl:
        out.append("%-36s%18s%18s%10s" % ("  same %smodels only (%d)" % (kind, len(lfl["models"])), _usd(lfl["before_per_call"], True),
                                          _usd(lfl["after_per_call"], True), _signed(lfl["change"])))
    writes = c.get("scope_writes_per_call")
    if writes:
        out.append("%-36s%18s%18s%10s" % ("  cache-write tokens/call, in scope", _k(writes.get("before")), _k(writes.get("after")),
                                          _change(writes.get("before"), writes.get("after"))))
    unpriced = _unpriced_lines(("before", before), ("after", after))
    if unpriced:
        out.append("")
        out.extend(unpriced)
    left_out = _left_out_lines(before, after)
    if left_out:
        out.append("")
        out.extend(left_out)
    if c.get("upgrade_note"):
        out.append("")
        out.append("(%s.)" % c["upgrade_note"])
    out.append("")
    if scope:
        share = " (%s)" % _pct(c["scope_calls"] / float(after["calls"])) if after["calls"] else ""
        out.append("A cache-lifetime setting mainly changes how Claude models are cached, so only Claude models are counted here.")
        if ttl == "auto":
            out.append("Under auto, only chats a person paces get the 1-hour cache; scheduled, subagent and other machine-paced")
            out.append("runs stay on 5 minutes, so they are not counted here either.")
        elif ttl == "1h":
            out.append("With 1h, Hermes still keeps subagent runs on 5 minutes, so they are not counted here either.")
        if after["calls"]:
            reached = " in chats a person paces" if ttl == "auto" else " outside subagent runs" if ttl == "1h" else ""
            out.append("Since the change, %s of %s calls%s are in scope: on Claude models%s." % (
                _n(c["scope_calls"]), _n(after["calls"]), share, reached))
    if ttl in CACHE_SCOPE_TTLS:
        out.append("Cost note: Hermes' list-price estimates book cache writes at the 5-minute price. At the 1-hour price every")
        out.append("written token costs more, so a lower cost per call shown here is an upper bound on the saving, and a higher")
        out.append("one understates the rise. The cache-write row above is what this setting acts on; the cost rows cannot show")
        out.append("the extra price of 1-hour writes.")
    if scope:
        out.append("")
    if not c.get("enough"):
        out.append("Too early to judge: %d %ssessions, %s %scalls so far. Wait for about %d sessions / %s calls "
                   "(a week of normal use)." % (c["scope_sessions"], kind, _n(c["scope_calls"]), kind,
                                                MIN_SESSIONS_FOR_VERDICT, _n(MIN_CALLS_FOR_VERDICT)))
        if scope and not c["scope_calls"] and after["calls"]:
            out.append("No Claude chat this setting reaches has run since the change, so it has had nothing to act on yet.")
    else:
        out.extend(_verdict_lines(c, lfl, before, after))
    if after["sessions"] and not c.get("pauses_known"):
        out.append("(message timestamps are missing, so pause counts are not shown)")
    out.extend(_tools_lines(before, after, c.get("tools_tracked")))
    out.append("")
    out.append("Note: the cache rows are what a cache-lifetime setting acts on; cost also moves with the work done.")
    out.append("Chats count where they started, so a chat already running at the marker is in 'before' only.")
    return out


def _idea_lines(result: Dict[str, Any]) -> List[str]:
    """One suggestion, only when the numbers back it: the 5-minute cache is going cold in the middle of Claude chats
    that a person is having (under ``auto``, machine-paced runs keep the 5-minute cache)."""
    idea = result.get("idea") or {}
    ttl = (result.get("settings") or {}).get("prompt_caching.cache_ttl")
    if ttl not in (None, "", "5m"):
        return []
    pauses = idea.get("pauses_5_60") or 0
    if not result.get("pauses_known") or pauses < 20 or (idea.get("write_share") or 0) < 0.03:
        return []
    return [
        "",
        "An idea worth measuring",
        "  In this period %d pauses of 5-60 minutes let the 5-minute prompt cache go cold mid-chat in Claude chats," % pauses,
        "  and cache writes were %s of their input tokens. Hermes can keep the cache warm for an hour during chats:"
        % _pct(idea["write_share"], 1),
        "      hermes config set prompt_caching.cache_ttl auto",
        "  Longer-lived writes cost more per token, so this only pays off when pauses cause most of the writes. To watch",
        "  it:  hermes memorybank usage --mark cache-ttl-auto   (right after changing it), then run  hermes memorybank usage",
        "  again after a week. It shows cache-write tokens per call, which is what the setting acts on; its list-price cost",
        "  rows book writes at the 5-minute price, so they cannot show the extra price of 1-hour writes.",
        "  To undo:  hermes config unset prompt_caching.cache_ttl",
    ]


def run(home: Any, *, weeks: int = 8, since: Optional[str] = None, mark_label: Optional[str] = None,
        vs: Optional[str] = None, baseline_days: int = BASELINE_DAYS, tools: Optional[str] = None,
        as_json: bool = False, out: Callable[[str], None] = print, now: Optional[float] = None) -> int:
    """Entry point shared by ``hermes memorybank usage`` and ``run.py usage``."""
    now = time.time() if now is None else float(now)
    names = tuple(t.strip() for t in tools.split(",") if t.strip()) if tools else DEFAULT_TOOLS
    try:
        try:
            weeks, baseline_days = int(weeks), int(baseline_days)
        except (TypeError, ValueError):
            raise UsageError("--weeks and --baseline-days want whole numbers")
        if not 1 <= weeks <= MAX_WEEKS:
            raise UsageError("--weeks wants a number from 1 to %d" % MAX_WEEKS)
        if not 1 <= baseline_days <= MAX_BASELINE_DAYS:
            raise UsageError("--baseline-days wants a number from 1 to %d" % MAX_BASELINE_DAYS)
        if mark_label:
            marker = mark(home, mark_label, now=now, baseline_days=baseline_days, tools=names)
            if as_json:
                out(json.dumps(marker, indent=1, default=str))
                return 0
            before = marker.get("before")
            out("Marked \"%s\" at %s (saved in %s)." % (marker["label"], marker["at_text"], markers_path(home)))
            if before:
                out("Baseline kept: the %d days before now - %s sessions, %s calls, %s per call (estimated)." % (
                    marker["baseline_days"], _n(before["sessions"]), _n(before["calls"]), _usd(before["cost_per_call"], True)))
            else:
                out("There are no earlier chats to keep as a baseline.")
            out("Run  hermes memorybank usage  again in about a week to see the difference.")
            return 0
        since_ts = None
        if since:
            try:
                since_ts = time.mktime(time.strptime(since, "%Y-%m-%d"))
            except (ValueError, OverflowError, OSError):
                raise UsageError("--since wants a date like 2026-10-07")
        result = build(home, now=now, weeks=weeks, since=since_ts, vs=vs, tools=names)
    except UsageError as exc:
        out("Usage: %s" % exc)
        return 1
    if as_json:
        out(json.dumps(result, indent=1, default=str))
        return 0
    for line in render(result):
        out(line)
    return 0
