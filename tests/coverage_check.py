"""Is everything this Hermes profile knows inside its Memory Bank?

    python tests/coverage_check.py <profile-home> [--json]

Thin wrapper around the plugin's own ``core/coverage.py`` (the same check as
``hermes memorybank check``). Exit code 0 only when nothing is missing and the
bank is internally sound.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

PLUGIN = Path(os.environ.get("MEMORYBANK_PLUGIN_DIR") or Path(__file__).resolve().parent.parent).resolve()
spec = importlib.util.spec_from_file_location(
    "_memorybank_core", PLUGIN / "core" / "__init__.py", submodule_search_locations=[str(PLUGIN / "core")]
)
core = importlib.util.module_from_spec(spec)
sys.modules["_memorybank_core"] = core
spec.loader.exec_module(core)

from _memorybank_core import coverage  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    report = coverage.check(Path(args[0]))
    if "--json" in sys.argv:
        print(json.dumps(report, indent=1, ensure_ascii=True))
    else:
        coverage.show(report, lambda line: print(line.encode("ascii", "replace").decode("ascii")))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
