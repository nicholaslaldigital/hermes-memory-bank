# Changelog

## 1.5.0 - a plain repository link installs it

The plugin itself did not change: same code, same bank, same data format, commands and settings as 1.4.0.

**What changed is where the files are.** The plugin used to live in a `memorybank/` subfolder, so Hermes' "install from
Git" box and `hermes plugins install <owner>/<repo>` found nothing at the top of the repository ("No agent or desktop
plugin artifacts found in this repository"). The plugin's files (`plugin.yaml`, `__init__.py`, `cli.py`, `run.py`,
`core/`, `dashboard/`, `desktop/`, `skills/`, `ui/`) are now the repository's top folder, so
`https://github.com/nicholaslaldigital/hermes-memory-bank` installs as it is.
- `install.py` copies only those plugin files into a profile (not itself, `tests/`, `docs/`, `catalog/` or the notes),
  so it installs the same files as 1.4.0, byte for byte, except `README.md` (now the repository's front page, which
  describes the whole repository) and the version number. It also refuses to run from a folder that does not hold the
  plugin's `plugin.yaml` and `__init__.py` (a lone copy of the installer, such as the one kept in a backup folder),
  so it can never replace an installed plugin with nothing.
- One wording change in the installer's own text: it no longer prints the word "sudo" (it says "not as root"), because
  Hermes' install scanner flags that word as a caution when the whole repository is installed from a link.
- The long README moved to `docs/DETAILS.md`; `README.md` is the front page and now has an "Install from the repository
  link" section.
- Upgrade: as for 1.3.0 below (quit Hermes, run the installer again, reopen Hermes). A profile that has 1.4.0 behaves
  the same as one that has 1.5.0.
- The version-1.4.0 entry in Nous Research's plugin catalog still works: it is pinned to the 1.4.0 commit and to the
  `memorybank/` subfolder of that commit.

## 1.4.0 - the network view in 3D, drawn as a small solar system

Upgrade: as for 1.3.0 below (quit Hermes, run the installer again, reopen Hermes). Only the network view changed;
the bank, its data format, the commands and the settings are the same as in 1.3.0.

**The network view is now 3D.** The graph is laid out in three dimensions and drawn in perspective
(near items bigger, far ones smaller and fainter). It is still the one dependency-free `ui/engine.js`
drawing on an ordinary 2D canvas - no WebGL, no library, nothing new loaded at run time - so the desktop page, the
dashboard tab and the standalone page all get it.
- Drag empty space to turn the view, Shift+drag (or the right or middle button) to slide it, scroll or pinch to
  zoom towards the pointer, drag an item to move it. Arrow keys turn, `+`/`-` zoom; `/`, `F` and Esc as before. On
  touch, one finger turns and two fingers zoom and slide.
- A "3D"/"2D" button next to the kind chips switches to a flat view (the earlier layout, pan and zoom, drawn with
  the same pictures) and back. The choice is remembered in the browser under `memorybank-view` where storage is
  available.
- When nothing is touched for a few seconds the 3D view turns slowly. Any input stops it; it is off in the flat
  view, in a hidden tab and when the system asks for reduced motion (`prefers-reduced-motion`).
- Everything else is as it was: data format, API calls, search, kind chips, timeline and replay, detail panel,
  Forget/Undo.
- Fixed along the way (the flat view had it too): a view that loaded while it had no size - a hidden pane, a
  minimised window - fitted the picture to nothing and showed a speck until Fit was pressed. It now fits itself
  as soon as it gets a size.

**And it looks like a small solar system.** Same data, same controls, a new picture (still nothing but a 2D canvas):
- Every item is a planet: a matte world with a surface of its own, lit from the upper left, its far side in
  shadow - no shine, no glow. Each kind is a sort of world in the kind's colour, muted like a photograph:
  memories are banded giants in amber and ochre, entities blue worlds of sea and land, skills pale violet ice
  giants, sessions grey cratered moons. A kind has five different worlds and an item always gets the same one.
  More links or more importance make a planet bigger, not brighter. Far planets are dimmer and a little bluer
  instead of see-through. Zoomed in, the surface shows: belts and storms, coasts and ice caps, craters.
- Topics are the suns, and the suns are golden: soft discs, warm white in the middle, gold round that and a
  deeper orange towards the edge, with a restrained warm glow - the only bodies that give light. They are gold
  whatever the topic colour is, and so are the topics' mark in the legend, their links and the lights on them.
- An always-on memory wears a planetary ring, tilted, passing behind its planet and in front of it (one thin line
  of the same shape when the planet is only a few pixels across); a click on the ring selects the memory. The
  selected item keeps a crisp ring in the accent colour, the pointed-at one a fainter ring, and a recall is a
  widening ring.
- A deep-space background, painted by the view itself: a soft gradient, a few faint colour clouds and a field of
  stars that turns with the view. It starts from the colour of the page the view sits in (or `--mb-bg`), and
  fades out at its border, so it sits in any host page without a hard edge.
- Links are gentle arcs in the colour of their busier end, quieter than the planets they join. The links of the
  selected or pointed-at item are drawn clearly, with a soft wider line under them. The flat view has the arcs
  too. A link's arc stays the same from one refresh of the data to the next.
- While something is selected, pointed at or searched for, everything else steps back as a dark, solid shape of
  itself under what is lit (it used to turn see-through), and the selection is not dimmed for being at the back.
- Small points of light travel along the links, in the link's direction - quiet traffic, at most 320 at once; a
  recall sends a burst of light out along the recalled item's links. Names have an outline, so they read over
  whatever is behind them; two names no longer print over each other.
- The light theme gets the same planets on a pale background: no stars, a soft shadow under each planet, golden
  suns without a glow, darker arcs, small dark points of light.
- Everything that moves by itself (the light, a few twinkling stars, the idle turn) is paced by one timer at
  about 30 frames a second, stops in a hidden tab, and is off altogether when the system asks for reduced motion
  - the picture then stands still and the view idles once the layout has settled, as before. It also idles
  whenever nothing on show would move (an item without links selected, a search that lights no link).
- A big bank turns the effects down by itself (far links straight and thin, a crowd of links dimmed, never more
  than 320 points of light), by degrees, so that no link jumps as the view turns: at 4,000 items and 8,000 links
  a frame issues about as many canvas calls as before.
- The planets are painted from the kind colours (`--mb-memory` ... `--mb-session`), so a custom colour gives
  planets to match. `--mb-topic` is the exception: it does not change the golden suns, nor what goes by their
  colour; it applies when topics are drawn as planets (`suns: false`). Every number behind the look, the suns'
  gold included, is in one block, `LOOK`, near the top of `ui/engine.js`.

## 1.3.0 - fixes and hardening on 1.2.0

Upgrade: quit Hermes, then run `python3 install.py --profile NAME` again (`python` or `py -3` on Windows, `--root` for
the main profile). It is idempotent: it changes no setting that is already right, keeps a backup of the old plugin,
and re-checks the bank. A profile where `memorybank` sits in both `plugins.enabled` and `plugins.disabled` (what the
1.2.0 Hermes-Python route could leave) is put right by `plugins enable`. A usage marker made by 1.2.0 is upgraded by
its first 1.3.0 report only if the earlier chats still add up to what it recorded (within 2%); otherwise it keeps its
own summary and the report says the usual range cannot be rebuilt. An existing bank keeps the permissions it has (see
"macOS and Linux" below).

**Memory that never fills up** now says what it covers: a new entry (an `add`) that does not fit goes to the bank; a
`replace` that makes an entry longer than the room left is still refused by Hermes, and the README says what to do
then.

**Recall and safety**
- A conversation is no longer offered its own earlier turns as a "past session": after Hermes compacts it, and also
  after a restart, a resume or a rebuilt agent, and for the chat a `/branch` started from. The conversations before a
  gateway `/new` are other conversations and are still recalled.
- Stored text can no longer rebuild Hermes' `<memory-context>` fence tag (it was removed in one pass only); every
  recall line and always-on note is cleaned of such tags until none is left.
- The write-approval check fails closed: a save is refused when approval is on (or cannot be told to be off) and the
  check fails while it runs; a Hermes with no write-approval module saves as its own memory tool does.
- A scanner that cannot be loaded, or that fails, is reported once per process in Hermes' log (once on stderr in the
  standalone command line) instead of silently: until it works again, text is not screened by it.
- A bank that cannot be opened or written is no longer reported as "busy": Hermes' own refusal stands, and the tool
  and note stay off for that session. Any other failure while the provider starts leaves it off in the same way, and
  the command-line chat is told once. A bank another program holds for a moment (a backup copy, a virus scan, a lock
  while the bank is being created) is tried again a few times, pausing half a second between tries, before the
  provider gives up for the session. Starting a cron or webhook run no longer writes to the bank or waits for another process's write lock.
- After `/undo` (also `/undo N` and several in a row) the memories whose recall block left the transcript can be
  recalled again, and none whose block is still in it is repeated. This reads Hermes' session database; where it
  cannot be read, only the last undone turn's block is released until the next turn. A hidden conversation that was
  captured live is retired too.
- Hostile or huge messages no longer crash or stall indexing (nested JSON, e-mail-shaped text, key/value lines), and
  an import with many new names no longer slows down as it goes.
- Photo and other structured messages, as Hermes stores them, are indexed and checked. Rows nobody typed (hidden
  scaffolding, sub-agent results, notices) are no longer prompts, titles or entities. A conversation you unhide or pin
  comes back at the next import. `hermes memorybank check` follows `capture.history_limit` and its remedy is `import
  --force`; a conversation that cannot be indexed is counted as failed, logged, retried by every import and shown by
  `check` as unreadable; `state.db` text that is not valid UTF-8 no longer stops the import or the check.
- `purge` overwrites what it deletes (secure delete, merged search index, emptied log) and removes names and topics
  that nothing links to any more, and the per-conversation copies of the always-on notes that hold a purged item. It
  runs in batches of 250 (other programs can write meanwhile; the recall log is now indexed by item), rewrites the
  bank file (VACUUM) at every purge, and says plainly when the search index or the log file could not be cleaned
  (another program has the bank open): `purge --yes` then finishes it. Its wording is "from the bank" because Hermes
  keeps its own copies in `state.db`. `export` and `purge --retired` work on banks with tens of thousands of items;
  `export` with no bank says so and creates nothing.
- A setting of the wrong kind in `memorybank.json` is ignored with a one-time warning instead of breaking recall or
  capture; numbers are capped.
- Terminal escape codes in stored text are stripped from command-line output; the viewer page escapes the folder name.
- `run.py` started from `<profile>/plugins/memorybank` acts on the profile it is installed in (a line on stderr says
  which), not on `HERMES_HOME`; its stdout holds only the command's own output.
- macOS and Linux: a bank folder and database that 1.3.0 creates are private to the user (0700 / 0600); one that
  already exists keeps its modes (`chmod 700 <profile>/plugin-data/memorybank` and `chmod 600
  <profile>/plugin-data/memorybank/bank.db*` tighten it). An export file is always narrowed to 0600. The viewer is not
  auto-opened on Linux (the access key would sit on a browser's command line). The bank uses WAL only where that is
  safe, and macOS gets `synchronous=FULL`. None of this has been run on a real Mac or Linux machine.
- `hermes memorybank check` reports skills whose stored text is missing (it does not look at the searchable text of
  sessions). A bank that 1.2.0 already migrated is not re-read by upgrading; `hermes memorybank import --force
  --limit 0` re-reads every skill and conversation and fills it in. Only a bank still at schema version 1 is re-read
  automatically.

**Usage meter**
- Every report says it covers main-loop chat calls only (compression, vision, title and background-review calls are
  not included).
- Before/after verdicts compare only models used on both sides, in the same mix. From at least 3 earlier weeks the
  report works out the range that 19 of 20 ordinary weeks would stay inside and says how many weeks that rests on: a
  figure inside it is "no clear effect yet"; outside it the report says it points to a lower or higher cost - a
  lead, not a proof - and only with about 500 priced calls in at least 5 priced chats (a chat Hermes compressed counts
  once). Otherwise it says "no verdict" and why. It never says "real". Chats that used more than one model or
  provider are counted in the totals but left out of the same-model figures, and the report says how many. Chats
  Hermes recorded no price for (included in a plan, unpriced) are left out of cost figures and counted.
- For a cache-lifetime marker (`1h` or `auto`) only the chats the setting reaches count (under `auto`, only chats a
  person paces). The report shows cache-write tokens per call and says that Hermes' list-price estimates book every
  cache write at the 5-minute price, so a lower cost per call is an upper bound on the saving. The cache-lifetime
  "idea" counts only human-paced Claude chats; tool habits are shown in every view.
- `--mark` refuses an unreadable `state.db` and a folder that does not exist; if `state.db` cannot be opened read-only
  and a non-empty `-wal` or `-journal` beside it may hold more chats, the report stops and says so (and `--mark`
  refuses). The marker file is validated, locked while written and never overwritten when damaged; entries and keys
  it does not understand are kept when `--mark` adds one; a stale lock is taken over by one run only; a damaged entry,
  or a failure while working out the report, ends in a note ("1 entry ... could not be used"), never a traceback.
  Weeks are counted from Mondays, option ranges are checked, and a locked `state.db` is reported.

**Installer**
- The Hermes-config-writer route reads `config.yaml` back; the `hermes` route refuses a Hermes older than the plugin's
  floor (and says when it cannot read the version); `--profile default` means the main profile; a `hermes` command
  that works on another home is not gone round unasked; running as root on someone else's profile is refused;
  profile names are checked against Hermes' rule; relative paths are made absolute; the reason a `hermes` call failed
  is shown.
- Every `hermes` call the installer makes, and every one it prints, names the profile (`--profile NAME`, `--profile
  default` for the main one), so a profile chosen earlier with `hermes profile use` cannot redirect an install or a
  rollback. PowerShell gets single quotes and Command Prompt double quotes (with a warning about `%`).
- One install or uninstall at a time per profile (a lock file); the plugin copy is staged and checked before it
  replaces the old one; `--purge` probes the bank and `memorybank.json` first, stops before changing anything if they
  are open, and exits 3 rather than "Done." if it could not move them afterwards (run it again with `--purge`); the
  uninstall reads the provider back and stops (exit 3) if the setting did not go back; an uninstall without a usable
  `hermes` carries on when `config.yaml` no longer names the plugin; a failed re-install no longer loses the record of
  the provider you had before.
- `ROLLBACK.md` says where things land, to look at the bank BEFORE rolling back (`hermes memorybank` no longer exists
  afterwards) and how to read it afterwards with the removed plugin's `run.py`, and how to go back to an earlier
  build. An uninstall that cannot move the plugin back keeps it in the backup instead of deleting it, and one that
  stops half-way says to run it again. On Windows a folder a virus scanner holds for a moment is waited for.
- `--dry-run` says that it changes no setting, plugin file or backup, and that it starts `hermes` (which may write
  its own logs). A console that cannot show a path's characters no longer crashes the installer.

**Docs and hygiene**
- The README and catalog text say what is stored (excerpts of what you type), which threads run, which refusal
  messages the hook rewrites, what `memorybank.json` is written by, which code depends on the operating system and has
  never run on a Mac or Linux machine, what `recall.cron` does, how many conversations are indexed, and what the tests
  do not cover. Commands in the README no longer carry trailing `#` comments (zsh passes them to the program) and name
  the Windows form of `python3`.
- Test fixtures contain only made-up people, offices and hostnames (public product, vendor and city names are used as
  ordinary words); `.gitignore` keeps banks, previews and builds out of a repository; `tests/seed_preview.py` can no
  longer delete a real bank.

## 1.2.0

Usage meter (`hermes memorybank usage`), cache-lifetime trial support, a cross-platform installer, the shareable package.
