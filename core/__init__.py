"""Memory Bank engine: a local knowledge graph with relevance-gated recall.

Pure standard library and independent of Hermes, so the same code runs inside
the agent, behind the dashboard / desktop API, in the standalone viewer and in
the tests.
"""

from . import api, coverage, entities, extract, ingest, usage  # noqa: F401
from .config import DEFAULTS, config_path, data_dir, db_path, load_config, save_config  # noqa: F401
from .recall import (  # noqa: F401
    HEADER, STANDING_HEADER, Hit, RecallEngine, standing_notes, told_ids, told_in_transcript,
)
from .store import RECALLABLE, Store, fts5_available  # noqa: F401

__version__ = "1.5.0"
