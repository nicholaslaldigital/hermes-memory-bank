"""Run every test that can run on this machine and say plainly what did not.

    python3 tests/run_tests.py [--python <interpreter>] [--hermes-agent <Hermes source folder>]

--python picks the interpreter for the Hermes integration test (use Hermes' own Python if the default one lacks
Hermes' dependencies); everything else runs with the interpreter running this script. The Hermes integration test
needs a Hermes source folder (~/.hermes/hermes-agent, on Windows %LOCALAPPDATA%\hermes\hermes-agent, or --hermes-agent / HERMES_AGENT_DIR) and is skipped without
one. Nothing here touches a real Hermes profile: tests build their own throwaway folders.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# (what, file, needs a Hermes source folder)
TESTS = [
    ("engine", "test_core.py", False),
    ("usage meter", "test_usage.py", False),
    ("installer", "test_install.py", False),
    ("store and purge review", "test_store_review.py", False),
    ("importer and check review", "test_ingest_review.py", False),
    ("POSIX simulation", "test_posix_sim.py", False),
    ("seed preview refusals", "test_seed_preview.py", False),
    ("portability and hygiene", "test_portability.py", False),
    ("Hermes integration", "test_hermes_integration.py", True),
]


def agent_dir(given: str) -> Path:
    if given:
        return Path(given)
    if os.environ.get("HERMES_AGENT_DIR"):
        return Path(os.environ["HERMES_AGENT_DIR"])
    base = (Path(os.environ.get("LOCALAPPDATA", "")) / "hermes") if os.name == "nt" else (Path.home() / ".hermes")
    return base / "hermes-agent"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--python", default="", help="interpreter for the Hermes integration test")
    parser.add_argument("--hermes-agent", default="", help="Hermes source folder")
    args = parser.parse_args()
    agent = agent_dir(args.hermes_agent)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.setdefault("HERMES_AGENT_DIR", str(agent))
    results = []
    for what, name, needs_hermes in TESTS:
        if needs_hermes and not (agent / "hermes_cli").is_dir():
            results.append((what, "SKIP", "no Hermes source folder at %s (pass --hermes-agent)" % agent))
            continue
        python = (args.python or sys.executable) if needs_hermes else sys.executable
        started = time.time()
        command = [python, "-B", str(HERE / name)] + ([str(agent)] if needs_hermes else [])
        try:
            done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=1800, env=env, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            results.append((what, "FAIL", "timed out"))
            continue
        out = done.stdout + done.stderr
        took = "%.0fs" % (time.time() - started)
        passes = sum(1 for line in out.splitlines() if line.startswith("PASS"))
        skips = [line for line in out.splitlines() if line.startswith("SKIP")]
        if done.returncode != 0:
            tail = "\n".join(out.strip().splitlines()[-12:])
            hint = "\n      (this test needs Hermes' own Python: --python <it>)" if needs_hermes else ""
            results.append((what, "FAIL", "exit %d after %s\n%s%s" % (done.returncode, took, tail, hint)))
        elif passes == 0:
            results.append((what, "SKIP", (skips[0][5:].strip() if skips else "nothing ran")))
        else:
            note = "%d checks, %s" % (passes, took) + ("; skipped: " + "; ".join(s[5:].strip() for s in skips) if skips else "")
            results.append((what, "ok", note))
    width = max(len(r[0]) for r in results)
    print("")
    for what, status, note in results:
        print("%-*s  %-4s  %s" % (width, what, status, note))
    failed = [r for r in results if r[1] == "FAIL"]
    skipped = [r for r in results if r[1] == "SKIP"]
    print("\n%d passed, %d failed, %d skipped%s" % (len(results) - len(failed) - len(skipped), len(failed), len(skipped),
                                                    "  (a skipped test was NOT run)" if skipped else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
