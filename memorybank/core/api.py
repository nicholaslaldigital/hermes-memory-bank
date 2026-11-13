"""JSON payloads for the network view.

Shared by every host of the viewer - the standalone server, the Hermes
dashboard backend and (through that) the desktop app - so they all show the
same thing.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from . import ingest
from .extract import clip, norm_entity
from .recall import RecallEngine
from .store import Store

KINDS = ["memory", "entity", "skill", "session", "topic"]
STATUSES = ["active", "evicted", "superseded", "archived"]
_META_KEYS = (
    "prompts", "outcome", "skills", "category", "use_count", "view_count", "last_used_at", "path",
    "model", "messages", "turns", "core", "created_by", "state", "flagged", "overflow", "pinned",
)


def graph(
    store: Store,
    *,
    limit: int = 4000,
    min_entity_degree: int = 2,
    include_archived: bool = False,
) -> Dict[str, Any]:
    """The whole network in a compact, array-based form."""
    limit = max(50, min(int(limit), 20000))
    nodes, edges = store.export_graph(
        limit=limit, min_entity_degree=max(0, int(min_entity_degree)), include_archived=include_archived
    )
    rels: List[str] = []
    rel_index: Dict[str, int] = {}
    out_edges = []
    for src, dst, rel, weight in edges:
        idx = rel_index.get(rel)
        if idx is None:
            idx = rel_index[rel] = len(rels)
            rels.append(rel)
        out_edges.append([src, dst, idx, round(weight, 3)])
    out_nodes = [
        [
            n["id"],
            KINDS.index(n["kind"]) if n["kind"] in KINDS else 1,
            clip(n["title"], 80),
            round(float(n["importance"]), 3),
            n["degree"],
            int(n["created_at"]),
            STATUSES.index(n["status"]) if n["status"] in STATUSES else 0,
            int(n["in_core"]),
            int(n["recall_count"]),
            n["subtype"],
            int(n["last_recalled"]),
        ]
        for n in nodes
    ]
    return {
        "v": 1,
        "generated": time.time(),
        "kinds": KINDS,
        "statuses": STATUSES,
        "rels": rels,
        "fields": ["id", "kind", "title", "importance", "degree", "created", "status", "in_core",
                   "recalls", "subtype", "last_recalled"],
        "nodes": out_nodes,
        "edges": out_edges,
        "stats": stats(store),
        "last_event": store.last_event_id(),
    }


def node(store: Store, node_id: int) -> Optional[Dict[str, Any]]:
    """Everything about one item, plus what it connects to."""
    item = store.get(int(node_id))
    if item is None:
        return None
    links = [
        {
            "id": n["id"], "kind": n["kind"], "title": clip(n["title"], 90), "rel": n["rel"],
            "dir": n["dir"], "weight": round(float(n["weight"]), 3), "status": n["status"],
        }
        for n in store.neighbors(int(node_id), limit=120)
        if n["status"] != "archived"
    ]
    return {
        "id": item["id"], "uid": item["uid"], "kind": item["kind"], "subtype": item["subtype"],
        "title": item["title"], "content": item["content"], "tags": item["tags"], "status": item["status"],
        "in_core": bool(item["in_core"]), "importance": item["importance"], "confidence": item["confidence"],
        "source": item["source"], "created_at": item["created_at"], "updated_at": item["updated_at"],
        "recall_count": item["recall_count"], "last_recalled": item["last_recalled"],
        "meta": {k: v for k, v in item["meta"].items() if k in _META_KEYS},
        "links": links,
    }


def search(store: Store, cfg: Optional[Dict[str, Any]], query: str, *, limit: int = 20) -> Dict[str, Any]:
    """Free-text lookup across everything (no relevance gate)."""
    query = (query or "").strip()
    results: List[Dict[str, Any]] = []
    if query:
        direct = store.entity_index().get(norm_entity(query))
        if direct is not None:
            results.append({"id": direct[0], "kind": "entity", "title": direct[1], "score": 1.0})
        engine = RecallEngine(store, cfg or {})
        for hit in engine.search(
            query, kinds=KINDS, limit=max(1, min(int(limit), 60)), gate=False, include_core=True,
            statuses=("active", "evicted", "superseded"),
        ):
            if any(r["id"] == hit.id for r in results):
                continue
            results.append({
                "id": hit.id, "kind": hit.kind, "title": clip(hit.title or hit.content, 90),
                "score": round(hit.score, 3),
            })
    return {"query": query, "results": results}


def events(store: Store, after: int = 0, *, limit: int = 100) -> Dict[str, Any]:
    return {"events": store.events_since(int(after), limit=max(1, min(int(limit), 500))),
            "last": store.last_event_id()}


def stats(store: Store) -> Dict[str, Any]:
    data = store.stats()
    data.pop("path", None)
    return data


def mutate(store: Store, action: str, node_id: int) -> Dict[str, Any]:
    if action == "forget":
        return ingest.forget(store, int(node_id))
    if action == "restore":
        return ingest.restore(store, int(node_id))
    return {"error": f"unknown action {action!r}"}
