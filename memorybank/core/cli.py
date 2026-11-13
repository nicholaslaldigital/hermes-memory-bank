"""Command line for the Memory Bank.

Reached as ``hermes memorybank <command>`` while the provider is active, or
directly as ``python run.py <command>`` (no Hermes needed).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import coverage, ingest, usage
from .config import LIMIT_CAP, config_path, db_path, load_config
from .recall import RecallEngine
from .store import Store

_HOME_OVERRIDE: Optional[Path] = None


def resolve_home(explicit: Optional[str] = None, profile: Optional[str] = None) -> Path:
    """The Hermes home to work on: explicit path, the active Hermes scope, or the platform default."""
    if explicit:
        base = Path(explicit).expanduser()
    elif _HOME_OVERRIDE is not None:
        return _HOME_OVERRIDE
    else:
        base = None
        try:
            from hermes_constants import get_hermes_home
            base = Path(get_hermes_home())
        except Exception:
            pass
        if base is None:
            env = os.environ.get("HERMES_HOME")
            local = os.environ.get("LOCALAPPDATA")
            if env:
                base = Path(env)
            elif os.name == "nt" and local and (Path(local) / "hermes").is_dir():
                base = Path(local) / "hermes"
            else:
                base = Path.home() / ".hermes"
    if profile and profile not in ("default", "root"):
        base = base / "profiles" / profile
        if not base.is_dir():
            raise SystemExit(f"no such profile: {profile} (looked in {base})")
    return base


_SCANNER_WARNED = False


def _scanner_down(why: str) -> None:
    """Say once, on stderr, that Hermes' scanner is not screening: without it nothing that is imported is screened by
    it, which is not something to leave unsaid."""
    global _SCANNER_WARNED
    if not _SCANNER_WARNED:
        _SCANNER_WARNED = True
        _err("Memory Bank: Hermes' text scanner %s, so text is not being screened by Hermes' scanner during this run." % why)


def _scanner():
    """Hermes' text scanner, or None. When it cannot be loaded, or fails when it is called, the first time says so
    (what is allowed does not change: a scanner that is not there or that fails flags nothing)."""
    try:
        from tools.threat_patterns import first_threat_message
    except Exception:
        _scanner_down("could not be loaded here")
        return None

    def scan(text: str) -> Any:
        try:
            return first_threat_message(text, scope="strict")
        except Exception as exc:
            _scanner_down("failed (%s)" % type(exc).__name__)
            return None

    return scan


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")   # all but tab and newline: no escape codes reach a terminal


def _out(text: str = "") -> None:
    text = _CONTROL.sub("", text)   # stored text is not trusted with the terminal (it can clear it, retitle it...)
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def _err(text: str = "") -> None:
    """``_out`` for a notice that must not be mixed into what a command prints (``usage --json`` is read by scripts)."""
    text = _CONTROL.sub("", text)
    try:
        print(text, file=sys.stderr)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), file=sys.stderr)


def _when(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "-"


def _history_limit(args: Any, home: Optional[Path] = None) -> int:
    """Conversations to index: --limit, else the profile's capture.history_limit, else 5000 (0 = every one)."""
    value = getattr(args, "limit", None)
    if value is None:
        try:
            if home is None:
                return 5000
            return min(int(load_config(home).get("capture", {}).get("history_limit", 5000)), LIMIT_CAP)
        except (TypeError, ValueError, OverflowError, AttributeError):
            return 5000
    return min(int(value), LIMIT_CAP)   # a number SQLite cannot hold is "all of them" anyway


def _bank_missing(home: Path) -> bool:
    """Looking never creates a bank: say so and let the caller stop."""
    path = db_path(home)
    if path.exists():
        return False
    _out(f"Memory Bank: no bank yet at {path}")
    _out("It is created the first time a session starts with memory.provider: memorybank, or with:  import")
    return True


def cmd_status(home: Path, args: Any) -> int:
    path = db_path(home)
    if not path.exists():
        _out(f"Memory Bank: no bank yet at {path}")
        _out("It is created the first time a session starts with memory.provider: memorybank,")
        _out("or right now with:  import")
        return 0
    store = Store(path)
    try:
        stats = store.stats()
        nodes = stats["nodes"]
        _out(f"Memory Bank  ({home})")
        _out(f"  memories   {nodes.get('memory', 0):>6}   ({stats['in_core']} always-on in MEMORY.md/USER.md, "
             f"{stats['overflow']} saved beyond them)")
        for label, key in (("entities", "entity"), ("skills", "skill"), ("sessions", "session"), ("topics", "topic")):
            _out(f"  {label:<10} {nodes.get(key, 0):>6}")
        _out(f"  links      {stats['edges']:>6}")
        _out(f"  recalls    {stats['recalls']:>6}   (last: {_when(stats['last_recall'])})")
        status = stats["memory_status"]
        if status:
            _out("  memory status: " + ", ".join(f"{k} {v}" for k, v in sorted(status.items())))
        _out(f"  size       {stats['db_bytes'] / 1024:.0f} KB   {path}")
        cfg = config_path(home)
        _out(f"  settings   {cfg if cfg.exists() else 'defaults (no ' + cfg.name + ')'}")
    finally:
        store.close()
    return 0


def cmd_import(home: Path, args: Any) -> int:
    store = Store(db_path(home))
    try:
        started = time.time()
        result = ingest.seed(
            store, home, limit=_history_limit(args, home), scan=_scanner(),
            force=bool(getattr(args, "force", False)),
        )
        for name, counts in result.items():
            _out(f"  {name:<9} " + ", ".join(
                f"{k}={v}" for k, v in counts.items() if not isinstance(v, (list, tuple, set, dict))))   # counts, not id lists
        failed = _count(result.get("failed")) + sum(
            _count(counts.get("failed")) for counts in result.values() if isinstance(counts, dict))
        if failed:
            _out(f"{failed} conversation(s) could not be read and were left out (see the log)")
        _out(f"done in {time.time() - started:.1f}s")
    finally:
        store.close()
    return cmd_status(home, args)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def cmd_check(home: Path, args: Any) -> int:
    """Recount this profile's memory files, skills and sessions and confirm
    every one of them is in the bank."""
    report = coverage.check(home)
    coverage.show(report, _out)
    return 0 if report["ok"] else 1


def cmd_search(home: Path, args: Any) -> int:
    if _bank_missing(home):
        return 1
    store = Store(db_path(home))
    try:
        engine = RecallEngine(store, load_config(home))
        hits = engine.search(" ".join(args.query), limit=int(args.limit), gate=False, include_core=True)   # the store clamps it
        if not hits:
            _out("no matches")
        for hit in hits:
            brief = hit.brief(200)
            _out(f"#{brief['id']:<5} {brief['kind']:<10} {brief['when']}  {brief['score']:.2f}  {brief['text']}")
    finally:
        store.close()
    return 0


def cmd_recall(home: Path, args: Any) -> int:
    """Dry run of the automatic recall: nothing is logged or marked as told."""
    if _bank_missing(home):
        return 1
    store = Store(db_path(home))
    try:
        engine = RecallEngine(store, load_config(home))
        started = time.perf_counter()
        text, hits = engine.recall(" ".join(args.query))
        elapsed = (time.perf_counter() - started) * 1000.0
        if not hits:
            _out(f"(nothing relevant enough - nothing would be injected)   {elapsed:.1f} ms")
        else:
            _out(text)
            _out(f"\n{len(hits)} items, {len(text)} chars (~{len(text) // 4} tokens), {elapsed:.1f} ms")
    finally:
        store.close()
    return 0


def cmd_ui(home: Path, args: Any) -> int:
    from .server import serve

    if _bank_missing(home):
        return 1

    port = getattr(args, "port", None) or int(load_config(home).get("ui", {}).get("port", 8765))
    label = "default" if home.name in ("hermes", ".hermes") else home.name   # the main profile's folder is just Hermes' own
    return serve(home, port=int(port), open_browser=not getattr(args, "no_open", False), title=label,
                 force_open=bool(getattr(args, "open", False)))


def cmd_export(home: Path, args: Any) -> int:
    if _bank_missing(home):   # looking never creates a bank - not even the backups folder
        return 1
    store = Store(db_path(home))
    try:
        nodes, edges = store.export_graph(limit=10 ** 9, min_entity_degree=0, include_archived=True)
        full = store.get_many([n["id"] for n in nodes])
        payload: Dict[str, Any] = {
            "format": "hermes-memorybank/1", "exported_at": time.time(), "home": str(home),
            "nodes": [full[n["id"]] for n in nodes if n["id"] in full],
            "edges": [{"src": s, "dst": d, "rel": r, "weight": w} for s, d, r, w in edges],
        }
        # Everything the bank holds, readable by anyone who gets the file: it
        # goes next to the profile's other backups unless a path is given.
        given = getattr(args, "path", None)
        target = Path(given) if given else home / "backups" / f"memorybank-export-{time.strftime('%Y%m%d-%H%M%S')}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Everything the bank holds, in plain text: readable by its owner only (where the system has owners). A new file
        # is created 0600; one that was there already keeps its old mode when it is reopened, so it is narrowed before
        # a byte of the new text is written, not after.
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        if os.name != "nt" and hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=1))
        _out(f"wrote {len(payload['nodes'])} items and {len(payload['edges'])} links to {target.resolve()}")
        _out("(plain text, and it includes items that were forgotten or retired)")
    finally:
        store.close()
    return 0


def cmd_purge(home: Path, args: Any) -> int:
    """Delete items for good. "forget" only retires an item. The memory_bank tool and the viewer cannot reach this;
    an agent with a terminal tool could run the command (and pass --yes)."""
    path = db_path(home)
    if not path.exists():
        _out(f"Memory Bank: no bank at {path}")
        return 1
    store = Store(path)
    try:
        yes = bool(getattr(args, "yes", False))
        ids = {int(i) for i in (getattr(args, "ids", None) or [])}
        if getattr(args, "retired", False):
            ids.update(store.retired_ids())
        nodes = store.get_many(sorted(ids)) if ids else {}
        if not nodes:
            if yes and store.cleanup_pending():   # nothing left to delete, but an earlier purge did not finish
                return _finish_cleanup(store)
            if not ids:
                _out("nothing chosen: give item ids (see 'search'), or --retired for everything that was forgotten")
                return 2
            _out("no such items")
            return 1
        for node in list(nodes.values())[:15]:
            _out(f"  #{node['id']:<6} {node['kind']:<8} {node['status']:<10} {(node['title'] or '')[:70]}")
        if len(nodes) > 15:
            _out(f"  ... and {len(nodes) - 15} more")
        if not yes:
            _out(f"{len(nodes)} item(s) would be deleted from the bank for good. Run it again with --yes to do it.")
            return 0
        _out("(a purge also rewrites the bank's file, so that text deleted earlier leaves it too; on a big bank that "
             "takes a moment)")
        report: Dict[str, Any] = {}
        try:
            count = store.purge(nodes.keys(), report=report)
        except sqlite3.Error as exc:   # the work is done in batches: what was deleted before this stays deleted
            _out(f"stopped before the end: {exc}")
            _out("What an earlier batch deleted stays deleted; the batch that failed was undone. Run the same command "
                 "again to finish.")
            return 1
        if report.get("clean", True):
            _out(f"deleted {count} item(s) from the bank for good")
        else:
            _out(f"deleted {count} item(s) from the bank")
            _unfinished(report)
        _out("(Hermes keeps its own copies - the transcripts of conversations and the system prompt each one started "
             "with, in state.db - and purge does not reach them.)")
        if any(node["in_core"] or node["kind"] == "skill" for node in nodes.values()):
            _out("(entries still in MEMORY.md / USER.md and skills still on disk are indexed again next time)")
        return 0 if report.get("clean", True) else 1
    finally:
        store.close()


def _unfinished(report: Dict[str, Any]) -> None:
    """Say what a purge could not clean up, and the one thing that finishes it."""
    _out("Not everything could be cleaned up, so the deleted text may still be in the bank's files:")
    for line in report.get("left", []):
        _out(f"  - {line}")
    _out("Once that is dealt with (close Hermes and every other program that uses the bank), run  purge --yes  "
         "again to finish; it deletes nothing more.")


def _finish_cleanup(store: Store) -> int:
    """``purge --yes`` with nothing left to delete finishes the clean-up an earlier purge could not complete."""
    _out("An earlier purge could not finish cleaning up; doing that now (nothing more is deleted).")
    report: Dict[str, Any] = {}
    if store.finish_cleanup(report):
        _out("finished: the names nothing links to are swept, the search index is merged, the file is rewritten and the log file is empty.")
        return 0
    _unfinished(report)
    return 1


def cmd_usage(home: Path, args: Any) -> int:
    """Tokens, cost and cache behaviour by week, from Hermes' own records (state.db is only read);
    ``--mark`` records a change (in usage-markers.json) so a later run can show before and after."""
    return usage.run(
        home, weeks=int(getattr(args, "weeks", 8)), since=getattr(args, "since", None),
        mark_label=getattr(args, "mark", None), vs=getattr(args, "vs", None),
        baseline_days=int(getattr(args, "baseline_days", usage.BASELINE_DAYS)),
        tools=getattr(args, "tools", None), as_json=bool(getattr(args, "json", False)), out=_out,
    )


_COMMANDS = {
    "status": cmd_status, "import": cmd_import, "check": cmd_check, "search": cmd_search,
    "recall": cmd_recall, "ui": cmd_ui, "export": cmd_export, "purge": cmd_purge, "usage": cmd_usage,
}


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``memorybank`` argparse tree (called by Hermes and by ``main``)."""
    subs = subparser.add_subparsers(dest="memorybank_command")
    subs.add_parser("status", help="Counts and location of the bank")
    p = subs.add_parser("import", help="Index skills, MEMORY.md/USER.md and past sessions")
    p.add_argument("--force", action="store_true", help="Re-read everything, not only what changed")
    p.add_argument("--limit", type=int, default=None,
                   help="Most recent conversations to index (default: capture.history_limit, 5000; 0 = all)")
    subs.add_parser("check", help="Confirm every memory entry, skill and session of this profile is in the bank")
    p = subs.add_parser("search", help="Search memories, sessions and skills")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=10)
    p = subs.add_parser("recall", help="Show what the engine would recall for a message on its own, without the live-chat filters (dry run)")
    p.add_argument("query", nargs="+")
    p = subs.add_parser("ui", help="Open the network view in a browser")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-open", action="store_true", help="Do not open a browser window")
    p.add_argument("--open", action="store_true",
                   help="Open the browser on Linux too (not done by default: the key would appear on its command line)")
    p = subs.add_parser("export", help="Write every item and link to a JSON file (default: the profile's backups folder); "
                                       "the long searchable text of sessions and skills is left out")
    p.add_argument("path", nargs="?")
    p = subs.add_parser("purge", help="Delete items from the bank for good (forget only retires them)")
    p.add_argument("ids", nargs="*", type=int, help="Item ids, as shown by search")
    p.add_argument("--retired", action="store_true", help="Everything that was forgotten or retired")
    p.add_argument("--yes", action="store_true", help="Really delete (without it, only shows what would go); with "
                   "nothing to delete it finishes a clean-up that an earlier purge could not complete")
    p = subs.add_parser("usage", help="Tokens, cost and cache behaviour by week; compare before/after a change (reads state.db only; --mark saves usage-markers.json)")
    p.add_argument("--weeks", type=int, default=8, help="How many weeks to show (default 8)")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="One summary of everything since this date instead of weeks")
    p.add_argument("--mark", metavar="LABEL", help="Record that you changed a setting now; the weeks before it become the baseline")
    p.add_argument("--vs", metavar="LABEL", help="Compare against this marker instead of the latest one")
    p.add_argument("--baseline-days", dest="baseline_days", type=int, default=usage.BASELINE_DAYS,
                   help="With --mark: how many days before now are kept as the baseline (default %d)" % usage.BASELINE_DAYS)
    p.add_argument("--tools", help="Comma-separated tool names to track (default: %s)" % ", ".join(usage.DEFAULT_TOOLS))
    p.add_argument("--json", action="store_true", help="Print the numbers as JSON")
    subparser.set_defaults(func=memorybank_command)


def memorybank_command(args: Any) -> int:
    name = getattr(args, "memorybank_command", None)
    handler = _COMMANDS.get(name or "status")
    if handler is None:
        _out("usage: memorybank {status,import,check,search,recall,ui,export,purge,usage}")
        return 2
    return handler(resolve_home(), args) or 0


def _installed_home() -> Optional[Path]:
    """The Hermes home this copy of the plugin is installed in (``<home>/plugins/<name>``), or None."""
    plugin = Path(__file__).resolve().parent.parent
    home = plugin.parent.parent
    if plugin.parent.name == "plugins" and any((home / name).exists() for name in ("config.yaml", "state.db", "memories")):
        return home
    return None


def _same_folder(a: Any, b: Any) -> bool:
    try:
        return Path(a).expanduser().resolve() == Path(b).resolve()
    except (OSError, RuntimeError, ValueError):
        return str(a) == str(b)


def main(argv: Optional[List[str]] = None) -> int:
    """Standalone entry point (``python run.py ...``)."""
    global _HOME_OVERRIDE
    parser = argparse.ArgumentParser(prog="memorybank", description="Hermes Memory Bank")
    parser.add_argument("--home", help="Hermes home folder (default: the profile this plugin is installed in, "
                                       "else the default profile)")
    parser.add_argument("--profile", help="Hermes profile name under <home>/profiles/")
    register_cli(parser)
    args = parser.parse_args(argv)
    explicit = args.home
    if not explicit and not args.profile:
        # Run from <profile>/plugins/memorybank, it is that profile's bank that is meant - not the default profile's.
        inferred = _installed_home()
        if inferred is not None:
            explicit = str(inferred)
            # Always said - also before a command that deletes - but on stderr: stdout is the command's own output
            # (``usage --json`` is read by scripts). The installed profile wins over HERMES_HOME, so when the two differ
            # the line says which one is not used.
            note = f"(the profile this plugin is installed in: {inferred})"
            env = os.environ.get("HERMES_HOME", "").strip()
            if env and not _same_folder(env, inferred):
                note += f" - HERMES_HOME is set to {env} and is not used here; pass --home to use it"
            _err(note)
    _HOME_OVERRIDE = resolve_home(explicit, args.profile)
    try:
        return memorybank_command(args)
    finally:
        _HOME_OVERRIDE = None


if __name__ == "__main__":
    sys.exit(main())
