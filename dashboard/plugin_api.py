"""Memory Bank backend, mounted by Hermes at ``/api/plugins/memorybank/``.

Serves the network view in the desktop app and the dashboard. Hermes handles
authentication and profile scoping; each request opens the bank of whichever
profile it is scoped to.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

router = APIRouter()

_ROOT = Path(__file__).resolve().parent.parent
_PKG = "_memorybank_core"


def _core():
    """The engine package, loaded by path (this file is imported on its own)."""
    module = sys.modules.get(_PKG)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            _PKG, _ROOT / "core" / "__init__.py", submodule_search_locations=[str(_ROOT / "core")]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_PKG, None)
            raise
    return module


def _home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


class _Bank:
    """Open the scoped profile's bank for one request.

    Looking never creates anything: a profile that has no bank yet (the plugin
    is not its memory provider) is answered from an empty one in memory.
    """

    def __enter__(self):
        self.core = _core()
        self.home = _home()
        path = self.core.db_path(self.home)
        self.store = self.core.Store(path if path.exists() else ":memory:")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.store.close()


@router.get("/graph")
def graph(limit: int = 4000, min_degree: int = 2, archived: bool = False) -> Dict[str, Any]:
    with _Bank() as bank:
        return bank.core.api.graph(
            bank.store, limit=limit, min_entity_degree=min_degree, include_archived=archived
        )


@router.get("/stats")
def stats() -> Dict[str, Any]:
    with _Bank() as bank:
        return bank.core.api.stats(bank.store)


@router.get("/events")
def events(after: int = 0, limit: int = 100) -> Dict[str, Any]:
    with _Bank() as bank:
        return bank.core.api.events(bank.store, after, limit=limit)


@router.get("/search")
def search(q: str = "", limit: int = 20) -> Dict[str, Any]:
    with _Bank() as bank:
        return bank.core.api.search(bank.store, bank.core.load_config(bank.home), q, limit=limit)


def _valid_id(node_id: int) -> int:
    if not 0 < node_id < 2 ** 62:
        raise HTTPException(status_code=404, detail="not found")
    return node_id


@router.get("/node/{node_id}")
def node(node_id: int) -> Dict[str, Any]:
    _valid_id(node_id)
    with _Bank() as bank:
        item = bank.core.api.node(bank.store, node_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    return item


@router.post("/node/{node_id}/{action}")
def mutate(node_id: int, action: str) -> Dict[str, Any]:
    if action not in ("forget", "restore"):
        raise HTTPException(status_code=404, detail="not found")
    _valid_id(node_id)
    with _Bank() as bank:
        result = bank.core.api.mutate(bank.store, action, node_id)
    if result.get("error"):
        missing = str(result["error"]).startswith("no item")
        raise HTTPException(status_code=404 if missing else 409, detail=result["error"])
    return result
