"""Does tests/seed_preview.py refuse the folders it must refuse, and accept the ones it may? (standard library only)

    python3 tests/test_seed_preview.py

seed_preview.py writes a throwaway bank into a folder. It must never do that inside a Hermes profile, on top of a bank
it did not make, or into a folder that holds somebody's files. It is run in a child process because importing it
registers a module name that other tests also use.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

DRIVER = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import seed_preview
source = Path(sys.argv[2])
cases = json.loads(sys.argv[3])
print(json.dumps({name: seed_preview.refusal(source, Path(path)) for name, path in cases.items()}))
"""


def touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    world = Path(tempfile.mkdtemp(prefix="mb-seed-test-")).resolve()
    source = world / "profiles" / "work"
    touch(source / "config.yaml")
    (source / "memories").mkdir()
    touch(world / "other" / "config.yaml")
    touch(world / "foreign" / "plugin-data" / "memorybank" / "bank.db", "not made by the script")
    touch(world / "docs" / "my-notes.txt")
    touch(world / "container" / "profiles" / "work" / "config.yaml")
    touch(world / "afile.txt")
    (world / "empty").mkdir()
    touch(world / "litter" / ".DS_Store")
    touch(world / "litter" / "Thumbs.db")
    touch(world / "litter" / "._notes")
    touch(world / "marked" / ".memorybank-preview")
    touch(world / "marked" / "plugin-data" / "memorybank" / "bank.db", "made by an earlier run")

    refused = {            # name -> text the reason must contain
        "the profile itself": ("profile itself", source),
        "inside the profile": ("profile itself", source / "sub"),
        "above the profile": ("profile itself", world),
        "another profile": ("looks like a Hermes profile", world / "other"),
        "a bank this script did not make": ("already there", world / "foreign"),
        "a folder of someone's files": ("not empty", world / "docs"),
        "a folder that contains a profile": ("not empty", world / "container"),
        "a file": ("a file, not a folder", world / "afile.txt"),
    }
    accepted = {
        "a new folder": world / "new",
        "an empty folder": world / "empty",
        "a folder with only file-manager leftovers": world / "litter",
        "a folder an earlier run made": world / "marked",
    }
    cases = {name: str(path) for name, (_why, path) in refused.items()}
    cases.update({name: str(path) for name, path in accepted.items()})
    done = subprocess.run([sys.executable, "-B", "-c", DRIVER, str(HERE), str(source), json.dumps(cases)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=dict(os.environ))
    failed = 0
    if done.returncode != 0:
        print("FAIL  seed_preview could not be loaded\n      " + (done.stderr or done.stdout).strip().replace("\n", "\n      "))
        return 1
    answers = json.loads(done.stdout.strip().splitlines()[-1])
    for name, (why, _path) in refused.items():
        text = answers[name]
        if text and why in text:
            print("PASS  refuses %s (%s)" % (name, text))
        else:
            failed += 1
            print("FAIL  refuses %s: expected a reason containing %r, got %r" % (name, why, text))
    for name in accepted:
        if answers[name] == "":
            print("PASS  accepts %s" % name)
        else:
            failed += 1
            print("FAIL  accepts %s: refused with %r" % (name, answers[name]))
    total = len(refused) + len(accepted)
    print("\n%d/%d passed" % (total - failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
