"""Run the Memory Bank tools without Hermes:

    python run.py status
    python run.py --profile work import
    python run.py recall "what did we decide about the VPN rollout"
    python run.py usage
    python run.py ui

Hermes imports every top-level module of a plugin folder, so everything here
sits behind the ``__main__`` guard.
"""

if __name__ == "__main__":
    import importlib.util
    import sys
    from pathlib import Path

    _root = Path(__file__).resolve().parent
    _name = "_memorybank_core"
    _spec = importlib.util.spec_from_file_location(
        _name, _root / "core" / "__init__.py", submodule_search_locations=[str(_root / "core")]
    )
    _core = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _core
    _spec.loader.exec_module(_core)
    from _memorybank_core.cli import main

    sys.exit(main())
