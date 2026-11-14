# Installing this with an AI assistant

This page is for the assistant (Claude or another) that has been asked to add the Memory Bank to someone's Hermes
Agent. It is a summary; [README.md](README.md) has the full install guide and [memorybank/README.md](memorybank/README.md)
the details. Where they disagree with this page, the READMEs and the installer's own output are right.

## What you are installing

A Hermes memory-provider plugin, version 1.4.0 (`version:` in `memorybank/plugin.yaml` says which one this folder
holds). Its network view is 3D and drawn as a small solar system (planets, golden suns for topics, rings for
always-on memories); [CHANGELOG.md](CHANGELOG.md) says what changed in each version.

It is local only: one SQLite file per Hermes profile, no network, no model calls. It stores text from the profile's
memory files, skills and conversations in that file, so tell the owner before you install.

It was built and used on Windows with Hermes 0.21.5. It is written for macOS and Linux too, but nobody has run it
there: on those systems be extra careful with the dry run and read "On macOS and Linux" in README.md first.

## Ask the owner first

1. Which Hermes profile should get it? `hermes profile list` shows them; `default` is the main profile. If they have
   a less important profile, suggest that one first. One install covers one profile; repeat it for another.
2. Is Hermes closed? The desktop app and any gateway must be quit before the install. On macOS and Linux the
   installer cannot tell that Hermes is running, so this is the owner's word.
3. Are they fine with it indexing that profile's memory files, skills and conversation history into a local file?

## Steps

You need a terminal on the machine where Hermes is installed, running as the owner; if you have none, walk the owner
through "Quick start" in README.md instead. Work from the folder this page is in. Hermes must be 0.21.5 or newer
(`hermes --version`) and Python 3.8 or newer. On Windows type `python` or `py -3` where this says `python3`; with no
Python on the PATH see "Quick start" in README.md.

Naming the profile: `--root` is the installer's flag for the main profile. In every `hermes` command on this page
write `--profile default` for the main profile, and never leave `--profile` out: without it Hermes uses the profile
picked earlier with `hermes profile use`, which may be a different one.

1. Optional: `python3 tests/run_tests.py` (needs Python 3.9 or newer, the installer itself runs on 3.8; several
   minutes, so give it a long timeout). It never touches a profile, a
   config file or the `hermes` command: every test works in throwaway temporary folders and the installer tests use a
   stand-in for `hermes`. If Hermes is installed in its usual place, the "Hermes integration" test also loads
   Hermes' own code in-process against a temporary folder; that is safe, and it is not what the "Do not" list below
   is about. That one test needs Hermes' own Python, and every FAIL of it prints a `--python` hint. If it failed
   and you did not pass `--python`, run it again with `--python <Hermes' Python>`; if you cannot, say so and carry
   on. If it still fails with Hermes' own Python, or any other test shows FAIL: stop and tell the owner. SKIP
   means not run.
2. Look at the plan. A dry run changes no setting, copies no plugin file and writes no backup. It does start
   `hermes` twice (to read its version and where its config is), and Hermes may write its own log or cache files
   when it starts.

   ```
   python3 install.py --profile NAME --dry-run
   ```

   For the main profile use `--root` instead of `--profile NAME`. The installer looks for `hermes` on the PATH and in
   Hermes' usual places; if the plan says it found none, find the file and add `--hermes /full/path/to/hermes`, and
   use that full path in place of `hermes` in every other command on this page.

   Show the owner the plan. Go on only if both are true:
   - The folder after `->` is the profile they meant. Normally that is `~/.hermes/profiles/NAME` on macOS and Linux
     or `%LOCALAPPDATA%\hermes\profiles\NAME` on Windows; the Hermes folder itself for the main profile; or under
     `HERMES_HOME` if the owner has set one.
   - The `switch on` line says `with Hermes' own commands`. If it says `by hand` or `through Hermes' config writer`,
     stop: the installer could not use the owner's `hermes` for that profile. Read the reason on that line, fix it
     with `--hermes <path>` (or `--hermes-root <folder>` if it names a different Hermes folder) and dry-run again.
3. Install:

   ```
   python3 install.py --profile NAME
   ```

   It backs up `config.yaml` and any earlier copy of the plugin, copies the plugin, switches it on through Hermes' own
   commands, builds the bank and checks it. Indexing a profile with a long history can take many minutes (the
   installer waits up to 30): run it with a long timeout or in the background and do not interrupt it. If it was cut
   off, run the same command again. If it answers that another install seems to be running, make sure no installer
   process is left, then wait 15 minutes (the lock frees itself) or delete only the lock file it names.

   Exit codes:
   - 0: done and verified. It prints the backup folder and a `ROLLBACK.md` path: give both to the owner.
   - 1: stopped, nothing was changed (usually Hermes is still open, or another installer run holds the lock). Fix
     what it names and run the same command again.
   - 2: nothing was changed: wrong profile name (it lists the ones it found), Hermes older than 0.21.5, or run as
     root on someone else's profile.
   - 3: the plugin IS installed and a backup exists, but something needs attention. The line starting
     `Installed, but:` and the lines above it name the exact `hermes ...` commands to run. Show the owner that text,
     run only the commands it printed, and do not re-run the installer to "fix" it.
4. Check:

   ```
   hermes --profile NAME memory status
   hermes --profile NAME memorybank status
   hermes --profile NAME memorybank check
   ```

   The first should say `Provider: memorybank`. The last should end with `RESULT: everything is in the bank`; if it
   says `GAPS FOUND - run: import --force`, run `hermes --profile NAME memorybank import --force` and check again.

## What only the owner can do

- Reopen Hermes. In the desktop app: main sidebar > Capabilities > Plugins > Memory Bank > switch on "Desktop". Then
  "Memory Bank" appears in the sidebar; that page is the network view.
- In the view: drag to turn, scroll to zoom, click an item for its details. The "3D" button next to the kind chips
  switches to a flat view and back.
- Without the desktop app, the owner can run `hermes --profile NAME memorybank ui` in their own terminal. It serves
  the same view on 127.0.0.1 until stopped with Ctrl+C and opens the browser (on Linux it prints the link instead;
  add `--open`). Do not run it yourself: it does not return.

## Do not

- Do not run the `hermes` program with `HERMES_HOME` set to an empty or temporary folder to "try it out", do not
  set, change or unset `HERMES_HOME` at all, and do not create a throwaway profile. Use `--dry-run`.
- Do not run `hermes profile use` to aim at the profile (it changes the owner's standing choice). Name the profile
  with `--profile` every time.
- Do not edit `config.yaml` by hand. The installer changes exactly two things (enables `memorybank` as a plugin, sets
  `memory.provider`) and reads them back.
- Do not add `--python`, `--no-cli`, `--no-activate`, `--no-index` or `--purge` unless the owner asks.
- Do not open or print the profile's secrets (`.env`, `auth.json`, token folders). Nothing here needs them.
- Do not run it with `sudo` or as another user.
- Do not delete the installer's lock file while an installer may still be running.
- Do not run `tests/verify_live.py`, `tests/rehearse_overflow.py` or `tests/seed_preview.py` unasked: unlike
  `run_tests.py` they read a real profile, and the first can write to it.
- Do not start `hermes memorybank ui`, `hermes dashboard` or the desktop app yourself. Reopening Hermes is the
  owner's step.
- Do not replace it with an older copy unless the owner wants that: 1.3.0 and earlier have a flat network view. If
  the plugin is offered by the Hermes catalog one day (`hermes plugins install memorybank`; [CATALOG.md](CATALOG.md)
  describes the submission), compare its version with this folder's before switching.

## Undo

Quit Hermes first (desktop app and gateway), exactly as for the install. Ask the owner whether they want anything out
of the bank first: after the undo `hermes memorybank ...` no longer exists (`hermes --profile NAME memorybank export`
writes a readable copy; `ROLLBACK.md` in the backup folder says how to read the bank afterwards). Then:

```
python3 install.py --profile NAME --uninstall
```

(`--root --uninstall` for the main profile; add `--dry-run` to see the plan first.) It puts the previous settings
back and moves the plugin into a new backup folder. Nothing is deleted and the bank stays. Add `--purge` only if the
owner asks: it moves the bank and their `memorybank.json` into that backup folder too. Exit code 3 means the undo is
not finished: it prints what to run, then run it again.
