"""Measured numbers for the recall path, on a real bank.

    python tests/bench_recall.py <home-with-a-bank>

What it measures (nothing is written to the bank; work happens on a copy):

1. Latency of the automatic recall, using the user's own past prompts as queries.
2. Cost: how often anything is injected and how large the injection is.
3. Retrieval quality, held-out style: for every indexed session with enough
   prompts, hide one prompt from the index and ask whether that prompt, used as
   a query, finds its own session again.
4. False positives: unrelated everyday questions that should recall nothing.
"""

from __future__ import annotations

import os

import importlib.util
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = Path(os.environ.get("MEMORYBANK_PLUGIN_DIR") or ROOT).resolve()
spec = importlib.util.spec_from_file_location(
    "_memorybank_core", PLUGIN / "core" / "__init__.py", submodule_search_locations=[str(PLUGIN / "core")]
)
core = importlib.util.module_from_spec(spec)
sys.modules["_memorybank_core"] = core
spec.loader.exec_module(core)

from _memorybank_core.extract import clip, content_terms  # noqa: E402

UNRELATED = [
    "what's a good recipe for sourdough bread?", "who won the world cup in 2018",
    "explain how a transformer neural network works", "write a limerick about a cat who loves tuna",
    "how do I center a div with flexbox", "what is the capital of Australia",
    "give me a 4 day workout split for beginners", "translate 'good morning' into Japanese",
    "how many ounces are in a pound", "summarize the plot of Hamlet in two sentences",
    "what's the difference between a stock and a bond", "suggest names for a golden retriever puppy",
    "how does photosynthesis work", "plan a weekend itinerary for Lisbon",
    "why is the sky blue", "what should I cook for dinner tonight with chicken and rice",
    "thanks, that worked perfectly", "ok go ahead and do that", "can you try again please",
    "make it shorter", "yes", "what do you think?", "looks good to me, continue", "hmm not quite right",
]


def pct(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def main() -> int:
    home = Path(sys.argv[1])
    source = core.db_path(home)
    tmp = Path(tempfile.mkdtemp(prefix="mb-bench-"))
    try:
        target = tmp / "plugin-data" / "memorybank" / "bank.db"
        target.parent.mkdir(parents=True)
        src = core.Store(source, readonly=True)
        src._conn.execute("VACUUM INTO ?", (str(target),))
        src.close()
        store = core.Store(target)
        cfg = core.load_config(home)
        engine = core.RecallEngine(store, cfg)
        from _memorybank_core import cli as bank_cli
        scan = bank_cli._scanner()  # every recall line is screened on output, as in a live session
        print('output screening:', 'Hermes threat scanner' if scan else 'not available')
        stats = store.stats()
        print(f"bank: {stats['nodes']}  links={stats['edges']}  always-on={stats['in_core']}")

        sessions = store.list_nodes("session")
        prompts = [(s, p) for s in sessions for p in (s["meta"].get("prompts") or [])]
        print(f"queries: {len(prompts)} real prompts from {len(sessions)} sessions\n")

        # 1 + 2: latency and injection size over every real prompt.
        latencies, sizes, items = [], [], []
        for _, prompt in prompts:
            started = time.perf_counter()
            text, hits = engine.recall(prompt, scan=scan)
            latencies.append((time.perf_counter() - started) * 1000.0)
            if hits:
                sizes.append(len(text))
                items.append(len(hits))
        engine.recall(prompts[0][1])
        print("1. latency of automatic recall (ms)")
        print(f"   median {statistics.median(latencies):.1f}   p95 {pct(latencies, 0.95):.1f}   "
              f"max {max(latencies):.1f}   (n={len(latencies)})")
        print("2. what gets injected")
        print(f"   turns with any recall: {len(sizes)}/{len(prompts)} ({100.0 * len(sizes) / len(prompts):.0f}%)")
        if sizes:
            print(f"   when it fires: median {statistics.median(sizes):.0f} chars (~{statistics.median(sizes) / 4:.0f} tokens), "
                  f"p95 {pct(sizes, 0.95):.0f} chars, max {max(sizes)} chars (budget {cfg['recall']['max_chars']})")
            print(f"   items per recall: median {statistics.median(items):.0f}, max {max(items)}")
            print(f"   mean over ALL turns: {sum(sizes) / len(prompts):.0f} chars (~{sum(sizes) / len(prompts) / 4:.0f} tokens) per turn "
                  "before per-session de-duplication")

        # 3: held-out retrieval.
        ranks_open, gated_hits, trials = [], 0, 0
        unseen_sizes, unseen_latency, samples = [], [], []
        for session in sessions:
            kept = list(session["meta"].get("prompts") or [])
            if len(kept) < 3:
                continue
            for index in (len(kept) - 1, 1):
                query = kept[index]
                if len(content_terms(query)) < 6:
                    continue
                rest = kept[:index] + kept[index + 1:]
                body = " ¶ ".join(rest)
                store.upsert_node(session["uid"], "session", content=body)
                found = engine.search(query, kinds=["session"], limit=10, gate=False)
                order = [h.id for h in found]
                ranks_open.append(order.index(session["id"]) + 1 if session["id"] in order else 0)
                gated = engine.search(query, kinds=["session"], limit=5, gate=True)
                gated_hits += int(any(h.id == session["id"] for h in gated))
                trials += 1
                # The same message as a brand-new one: its own session is not in the bank.
                started = time.perf_counter()
                text, hits = engine.recall(query, already={session["id"]}, scan=scan)
                unseen_latency.append((time.perf_counter() - started) * 1000.0)
                unseen_sizes.append(len(text))
                if hits and len(samples) < 8:
                    samples.append((query, text))
                store.upsert_node(session["uid"], "session", content=session["content"])
                break
        if trials:
            at = lambda k: 100.0 * sum(1 for r in ranks_open if 0 < r <= k) / trials
            print("3. held-out retrieval: one prompt hidden from the index, then used as the query")
            print(f"   sessions tested: {trials}")
            print(f"   its own session ranked #1: {at(1):.0f}%   in top 3: {at(3):.0f}%   in top 5: {at(5):.0f}%   in top 10: {at(10):.0f}%")
            print(f"   passes the relevance gate (would be auto-recalled): {100.0 * gated_hits / trials:.0f}%")

        if unseen_sizes:
            fired_n = sum(1 for s in unseen_sizes if s)
            print("   the same prompts as NEW messages (own session excluded):")
            print(f"     recall fires on {fired_n}/{len(unseen_sizes)} ({100.0 * fired_n / len(unseen_sizes):.0f}%), "
                  f"mean {sum(unseen_sizes) / len(unseen_sizes):.0f} chars (~{sum(unseen_sizes) / len(unseen_sizes) / 4:.0f} tokens) per turn, "
                  f"median latency {statistics.median(unseen_latency):.1f} ms")
        if "--samples" in sys.argv:
            for query, text in samples:
                print("\n   Q:", clip(query, 150).encode("ascii", "replace").decode())
                for line in text.split("\n")[1:]:
                    print("     ", clip(line, 170).encode("ascii", "replace").decode())

        # 4: false positives.
        fired = [(q, engine.recall(q, scan=scan)[0]) for q in UNRELATED]
        fired = [(q, t) for q, t in fired if t]
        print("4. unrelated / filler messages that should recall nothing")
        print(f"   recalled something: {len(fired)}/{len(UNRELATED)}")
        for query, text in fired:
            first = text.split("\n")[1] if "\n" in text else text
            print(f"     ! {query!r} -> {clip(first, 90)}")
        store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
