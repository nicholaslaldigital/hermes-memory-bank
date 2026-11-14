# Hermes Memory Bank

Unlimited local long-term memory for [Hermes Agent](https://hermes-agent.nousresearch.com), kept as a knowledge
graph you can see - plus a usage meter that shows what the main-loop calls of your chats cost and whether a setting
change helped.

- Memory that **never fills up**: a new entry that does not fit in `MEMORY.md` / `USER.md` goes to the bank instead
  of being refused. (A `replace` that makes an existing entry longer than the room left is still refused by Hermes;
  save the corrected wording as a new entry instead.)
- Your skills (the first 12,000 characters of each) and your conversations (the newest 5,000; not scheduled or
  delegated runs, hidden chats or one-message chats) are **indexed**; relevant items are **recalled automatically**
  before each turn.
- A live **network view** in 3D, drawn as a small solar system (every item a planet, topics the suns), inside the
  Hermes desktop app, the dashboard, or a local page.
- `hermes memorybank usage`: tokens, cache behaviour and estimated cost **by week**, and before/after a change. It
  covers the main-loop calls of each chat only (not compression, vision, title or background-review calls).
- Local only: one SQLite file per profile, no network, no model calls. Reversible: every install writes a backup
  and a one-command rollback.

Details, commands, settings, safety notes and limits: [memorybank/README.md](memorybank/README.md).

## Quick start

You need Hermes 0.21.5 or newer with the `hermes` command working (`hermes --version`), and Python 3.8 or newer.
Quit Hermes first (the desktop app and any gateway), then work in a terminal from this folder.

To get this folder, clone the repository (`git clone https://github.com/nicholaslaldigital/hermes-memory-bank`) or
use GitHub's Code > Download ZIP and unpack it. If an AI assistant does the install for you, point it at
[INSTALL-WITH-CLAUDE.md](INSTALL-WITH-CLAUDE.md).

**On Windows** type `python` (or `py -3`) where this page says `python3`. If Windows answers "Python was not found"
or opens the Microsoft Store, that is only a placeholder shortcut, not Python: install Python 3.8 or newer
(python.org or the Store) and open a new terminal, or run the installer with the full path of a Python 3.8+ you
already have, for example in PowerShell `& "C:\path\to\python.exe" install.py --profile NAME`. (Hermes' own installer
does not put a Python on your PATH.)

First find your profile name; `default` is the main profile:

```
hermes profile list
```

Then look at the plan, with your profile's name in place of NAME:

```
python3 install.py --profile NAME --dry-run
```

For the main profile write `--root` instead of `--profile NAME` (`--profile default` means the same). Profile names
are lowercase letters, digits, `-` and `_` (the installer refuses anything else and says so), so they never need
quoting. The dry run changes no setting and no plugin file and writes no backup; it does start `hermes` (to read its
version and where its config is), and Hermes may write its own log or cache files when it starts. If the plan looks
right, install - this copies the plugin, switches it on, builds the bank from your skills, memory files and
conversation history, and checks it:

```
python3 install.py --profile NAME
```

Then reopen Hermes. In the desktop app: main sidebar > Capabilities > Plugins > Memory Bank > switch on "Desktop",
and "Memory Bank" appears in the sidebar. Check it from a terminal:

```
hermes --profile NAME memory status
hermes --profile NAME memorybank status
hermes --profile NAME memorybank usage
```

The first should say `Provider: memorybank`, the second shows what is in the bank, the third what your chats cost,
week by week. For the main profile write `--profile default`. Without `--profile`, Hermes uses the profile you chose
earlier with `hermes profile use` (the main profile only if you chose none), which may not be the one you installed
into - then `hermes memorybank` is not found at all.

To undo, look at what the bank holds first (after the rollback `hermes memorybank` no longer exists; `ROLLBACK.md`
in the backup folder the installer printed says how to read the bank afterwards), then:

```
python3 install.py --profile NAME --uninstall
```

(`--root --uninstall` for the main profile, or follow `ROLLBACK.md`.) Nothing is ever deleted: the plugin folder, and
with `--purge` the bank and your `memorybank.json`, are moved into a new backup folder.

### On macOS and Linux

This was built and run on Windows with Hermes 0.21.5, and only there. It is written for macOS and Linux too (Hermes
keeps its files in `~/.hermes`, the installer finds them the way Hermes does, and everything is standard-library
Python), but nobody has run it on a Mac or a Linux machine. The code that exists only for those systems - owner-only
file modes for the bank, its folder and exports, a stricter SQLite durability setting on macOS, and no automatic
browser start on Linux - has only been exercised by a simulation on Windows (`tests/test_posix_sim.py`); the list is
in [memorybank/README.md](memorybank/README.md) under "Platforms".

So begin with `--dry-run`. If the `hermes` command is not on your PATH, add `--hermes /full/path/to/hermes`. If
`python3` only offers to install Apple's Command Line Tools, accept, or use Python from python.org or Homebrew. Run
the installer as yourself, not with `sudo`, and quit Hermes first: on macOS and Linux an open file does not stop a
folder from being moved, so the installer cannot notice a running Hermes.

The installer switches the plugin on through Hermes' own commands (`hermes plugins enable`, `hermes config set
memory.provider`) and reads the result back. If no `hermes` command can be run at all but Hermes' own Python is
found, it uses Hermes' config writer in that Python instead and reads `config.yaml` back; if neither is possible, or
the `hermes` that runs works on a different Hermes home, it leaves `config.yaml` alone and prints the two commands
that finish the job by hand. The plan it prints first (also with `--dry-run`) says which of these applies.

## What is in this folder

```
install.py          the installer (and uninstaller)
INSTALL-WITH-CLAUDE.md  a short brief for an AI assistant asked to do the install
memorybank/         the plugin - this is what gets installed into a profile
tests/              run_tests.py and the test files (see below)
catalog/            a draft entry and pull-request text for the Hermes plugin catalog
CATALOG.md          how to submit it to the Hermes plugin catalog
CHANGELOG.md        what changed in each version
LICENSE             the MIT License
```

If you were given a `dist/` folder next to these, its zips were built by the author's packaging step, which is not
part of this folder; a zip matches this folder only if it was built after the folder's last edit. Use the folder.

## Tests

```
python3 tests/run_tests.py
```

runs everything that can run on your machine: the engine, the usage meter, the installer (against a stand-in for the
`hermes` command, so a real Hermes is never touched), the store, importer and POSIX-simulation reviews, the folder
hygiene check, and the Hermes integration test when a Hermes source folder is found (`%LOCALAPPDATA%\hermes\hermes-agent`
on Windows, `~/.hermes/hermes-agent` on macOS and Linux, or set `HERMES_AGENT_DIR` or pass `--hermes-agent`). That
last test needs Hermes' own Python: pass it with `--python`. Anything it cannot run is reported as skipped, not
passed, and a test that fails is reported as failed. Two checks you can run against your own installed profile, with
Hermes' own Python in place of `python3`:

```
python3 tests/verify_live.py PROFILE_FOLDER
python3 tests/rehearse_overflow.py PROFILE_FOLDER
```

`verify_live.py` asks "will a new session use the bank?" (13 checks); it creates and fills that profile's bank if it
has none yet, as a first session would (its header says what it writes). `rehearse_overflow.py` plays "memory is
full" on a COPY of your memory files.

`tests/seed_preview.py` and `tests/bench_recall.py` are optional and used one after the other: `seed_preview.py` builds
a preview bank from your profile (read-only on the profile; the bank goes into a new temporary folder whose path it
prints, or an empty folder you name - it refuses anything that looks like a Hermes profile, and the bank holds text
from your memory files and conversations, so delete it when you are done), and `bench_recall.py` times and scores
recall on a temporary copy of an existing bank (see each file's header).

## Sharing it

A listing in the Hermes plugin catalog would let anyone install it with `hermes plugins install memorybank`.
[CATALOG.md](CATALOG.md) walks through what that takes. The code is released under the MIT License (see
[LICENSE](LICENSE)).
