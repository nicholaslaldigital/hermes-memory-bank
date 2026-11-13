"""Build a preview bank from a real Hermes profile WITHOUT touching it.

Reads the profile's skills, MEMORY.md / USER.md and session database (read-only) and writes only to a throwaway
bank, by default in a new temporary folder (its path is printed; delete it when you are done - it holds text from
your memory files and conversations):

    python tests/seed_preview.py <profile-home> [<preview-folder>]

A <preview-folder> you name must be new or empty (a file manager's .DS_Store / Thumbs.db / desktop.ini do not count),
or one an earlier run of this script made. The script refuses the profile itself, anything inside or above it, a
folder that has config.yaml, state.db, memories or skills, a folder that already holds a bank it did not make, and
any other folder that is not empty - so it can never replace a real bank or add a bank to a folder of your files.
"""

from __future__ import annotations

import os

import importlib.util
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = Path(os.environ.get("MEMORYBANK_PLUGIN_DIR") or ROOT / "memorybank").resolve()
spec = importlib.util.spec_from_file_location(
    "_memorybank_core", PLUGIN / "core" / "__init__.py", submodule_search_locations=[str(PLUGIN / "core")]
)
core = importlib.util.module_from_spec(spec)
sys.modules["_memorybank_core"] = core
spec.loader.exec_module(core)

MARKER = ".memorybank-preview"
LITTER = {".ds_store", "thumbs.db", "desktop.ini"}      # what a file manager adds to a folder it has shown


def refusal(source: Path, preview: Path) -> str:
    """Why ``preview`` may not be used for the throwaway bank, or '' if it may."""
    if preview == source or source in preview.parents or preview in source.parents:
        return "it is the profile itself, or inside it, or above it"
    if preview.exists() and not preview.is_dir():
        return "it is a file, not a folder"
    if any((preview / name).exists() for name in ("config.yaml", "state.db", "memories", "skills")):
        return "it looks like a Hermes profile (it has config.yaml, state.db, memories or skills)"
    if core.db_path(preview).exists() and not (preview / MARKER).exists():
        return "a bank is already there that this script did not make"
    if preview.is_dir() and not (preview / MARKER).exists():
        held = sorted(p.name for p in preview.iterdir() if p.name.lower() not in LITTER and not p.name.startswith("._"))
        if held:
            return "it is not empty (it holds %s) and was not made by this script" % ", ".join(held[:3])
    return ""


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    source = Path(sys.argv[1]).resolve()
    if len(sys.argv) > 2:
        preview = Path(sys.argv[2]).resolve()
        why = refusal(source, preview)
        if why:
            print("refusing to use %s: %s." % (preview, why))
            print("Pass a new or empty folder, or leave it out to use a temporary one.")
            return 2
    else:
        preview = Path(tempfile.mkdtemp(prefix="memorybank-preview-"))
    preview.mkdir(parents=True, exist_ok=True)
    (preview / MARKER).write_text("throwaway bank made by seed_preview.py - safe to delete\n", encoding="utf-8")
    target = core.db_path(preview)
    if target.exists():
        for suffix in ("", "-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
    from _memorybank_core import cli as bank_cli
    scan = bank_cli._scanner()  # Hermes' own memory scanner, as a real install uses it
    store = core.Store(target)
    started = time.perf_counter()
    result = core.ingest.seed(store, source, limit=500, scan=scan)
    elapsed = time.perf_counter() - started
    print(f"scanner  {'Hermes threat scanner (strict)' if scan else 'not available'}")
    stats = store.stats()
    print(f"source   {source}")
    print(f"bank     {target}")
    print(f"preview  {preview}   (a throwaway: it holds text from your profile - delete it when you are done)")
    for name, counts in result.items():
        print(f"  {name:<9} " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"  nodes    {stats['nodes']}")
    print(f"  links    {stats['edges']}   always-on memories: {stats['in_core']}")
    print(f"  size     {stats['db_bytes'] / 1024:.0f} KB   built in {elapsed:.2f}s")
    nodes, edges = store.export_graph(min_entity_degree=2)
    shown = {}
    for node in nodes:
        shown[node["kind"]] = shown.get(node["kind"], 0) + 1
    print(f"  graph    {len(nodes)} nodes / {len(edges)} links shown (entities with 2+ links): {shown}")
    store.close()
    # The independent recount: is everything the profile has in this bank?
    report = core.coverage.check(source, bank_path=target)
    core.coverage.show(report, lambda line: print(line.encode("ascii", "replace").decode("ascii")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
