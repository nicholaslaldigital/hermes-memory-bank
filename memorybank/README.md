# Memory Bank for Hermes

Unlimited, local long-term memory for [Hermes Agent](https://hermes-agent.nousresearch.com), kept as a
knowledge graph you can look at - plus a usage meter that shows what the main-loop calls of your chats cost.

Hermes' built-in memory is two small files (`MEMORY.md` 2,200 characters, `USER.md` 1,375). Once they are
full, a new save is refused, and Hermes' background review (which may only add) quietly drops what it wanted
to keep. The Memory Bank sits behind those files:

- **Memory never fills up.** When a new entry (an `add` with the built-in `memory` tool) does not fit in
  `MEMORY.md` / `USER.md`, it is stored in the bank instead and Hermes is told it succeeded. Those entries are
  shown at the start of every session (profile and preference items first, then the newest, within a small budget)
  and recalled whenever they are relevant. This holds for a new save only: a `replace` that makes an existing entry
  longer than the room left is still refused by Hermes (see "Known limits").
- **Both files are mirrored.** An entry that gets reworded keeps a link to its old wording. An entry that
  falls out of a file is kept and can still be recalled, marked as possibly outdated. An entry you (or Hermes)
  remove on purpose is retired: kept in the bank's history, not recalled.
- **Unlimited store.** Hermes saves durable facts, decisions, lessons and procedures with one tool,
  `memory_bank`. Nothing is ever cut to fit.
- **It indexes what already exists**: your skills (the first 12,000 characters of each `SKILL.md`) and your
  conversations (the newest 5,000; not scheduled or delegated runs, hidden chats or chats with fewer than two
  messages; with excerpts of what was asked - see "Safety and what it touches"), linked to the people, systems and
  projects they mention. New skills are indexed the moment they are created; conversations are indexed turn by turn.
- **Recall is automatic and cheap.** Before each turn, items relevant to your message are injected - only when
  something actually matches, never twice in one conversation.
- **You can look at it.** A live network of memories, entities, skills and sessions, inside the desktop app,
  the dashboard, or a local page.
- **You can measure Hermes' cost.** `hermes memorybank usage` reads Hermes' own records and shows tokens,
  cache behaviour and estimated cost by week, and compares before/after a setting change. It covers the main-loop
  calls of each chat only: compression, vision, title and background-review calls, which Hermes books separately,
  are not included.

Everything stays on your machine: one SQLite file per profile, no model calls, no network. It does not make the
model itself faster; it makes Hermes stop forgetting, and find things again.

Tested with Hermes 0.21.5 on Windows. It is written for macOS and Linux too (standard library only; the few places
that depend on the operating system are listed under "Platforms"), but it has not been run there by its author.

## Install

You need Hermes 0.21.5 or newer, and the `hermes` command working in a terminal (try `hermes --version`).

### With the installer (works today)

Run it from the project folder - the one that contains `install.py` next to this `memorybank/` folder (a catalog
install fetches only the plugin folder, which does not include the installer). Quit Hermes first. On Windows write
`python` (or `py -3`) where this page says `python3`; if Windows says "Python was not found" or opens the Microsoft
Store, that is only a placeholder: install Python 3.8 or newer, or use the full path of a Python 3.8+ you already
have (Hermes' own installer does not put one on your PATH). `hermes profile list` shows your profile names; replace
NAME below with yours (lowercase letters, digits, `-` and `_`).

Look at the plan first:

```
python3 install.py --profile NAME --dry-run
```

It changes no setting and no plugin file and writes no backup; it does start `hermes` (to read its version and where
its config is), and Hermes may write its own log or cache files when it starts. Then install:

```
python3 install.py --profile NAME
```

For the default (main) profile write `--root` instead of `--profile NAME`; `--profile default` means the same. The
installer switches the plugin on through Hermes' own commands (`hermes plugins enable memorybank`, `hermes config
set memory.provider memorybank`), after checking that the `hermes` command really points at the profile it is
changing (every call names the profile, `--profile default` for the main one, so a profile chosen earlier with
`hermes profile use` cannot redirect it), then reads the result back to confirm, builds the bank and checks that
everything within `capture.history_limit` is in it. The plan it prints first (also with `--dry-run`) says which of
three ways applies: the `hermes` commands above; or - only when no `hermes` command can be run at all but Hermes'
own Python is found (`<hermes root>/hermes-agent/venv`, or `--python`) - Hermes' own config writer running in that
Python, after which it reads `config.yaml` back and says so if the change is not there; or, when neither is
possible (or the `hermes` that runs works on a different Hermes home), no change to `config.yaml` at all: it still
copies and indexes, and prints the two commands to finish by hand. Every install writes a backup folder
(`<profile>/backups/memorybank-<time>/`) with your `config.yaml` as it was, a `ROLLBACK.md` and a copy of the
installer, so the rollback works wherever the download has gone. Nothing is deleted. One install or uninstall at a
time is allowed per profile (a lock file, `plugin-data/.memorybank-install.lock`; if a run was killed, delete it or
wait 15 minutes), and the new plugin copy is made beside the old one and checked against the source before it
replaces it.

Then **restart Hermes**. To get the network page in the desktop app: main sidebar > Capabilities > Plugins >
Memory Bank > switch on "Desktop".

### From the Hermes plugin catalog (once listed)

```
hermes plugins install memorybank
hermes plugins enable memorybank
hermes config set memory.provider memorybank
```

(for a named profile put `--profile NAME` after `hermes`, `--profile default` for the main one). The first session
builds the bank from your skills, memory files and history by itself; `hermes memorybank import` does it right away.

### Check it

```
hermes --profile NAME memory status
hermes --profile NAME memorybank status
hermes --profile NAME memorybank check
```

The first should say `Provider: memorybank`; the second shows counts, size and location; the third recounts memory
files, skills and conversations against the bank. For the main profile write `--profile default`. Without
`--profile`, Hermes uses the profile chosen earlier with `hermes profile use` (the main profile only if none was
chosen); if that profile is not the one that has the bank, `hermes memorybank` is not registered at all (Hermes
answers "invalid choice") and `hermes memory status` shows another provider.

### Undo it

```
python3 install.py --profile NAME --uninstall
```

or run the command written in `<profile>/backups/memorybank-<time>/ROLLBACK.md`. Look at what the bank holds first:
after the rollback `hermes memorybank` no longer exists, because Hermes only offers the commands of the memory
provider that is switched on. (`ROLLBACK.md` lists the commands, and how to read the bank afterwards with the removed
copy's `run.py --home <profile folder> search <words>`.) The command restores `memory.provider` to what it was
(only if it is still `memorybank`), takes the plugin off `plugins.enabled` (through the `hermes` command it is also
listed under `plugins.disabled`; through Hermes' own Python it is only taken off `plugins.enabled`) and moves the
plugin folder into a new `<profile>/backups/memorybank-removed-<time>/plugin` (the command prints the exact path).
It then reads the provider back and stops, with exit code 3 and no "Done.", if the setting did not go back. The
bank's data stays unless you add `--purge`, which moves the bank into `data` in that same folder and your settings
file `memorybank.json` with it (both are probed first, and nothing is changed if they are open). If the bank cannot
be moved after the settings were restored, the command exits 3 and says to run it again WITH `--purge`. If the
command stops part-way it says which step failed, and running it again finishes the job. `hermes plugins enable
memorybank` turns the plugin back on only while its folder is still in `plugins/`; after an uninstall run the
installer again. Without `--purge` the removed copy can still read the bank (see `ROLLBACK.md`); after `--purge`
put the bank back as `plugin-data/memorybank` first.

### Platforms

Windows is what it was built and run on. On macOS and Linux Hermes keeps its files in `~/.hermes` (on Windows in
`%LOCALAPPDATA%\hermes`), and the installer follows Hermes' own rule for finding them (`HERMES_HOME`, else that
default); the installer needs Python 3.8 or newer. On a Mac, `python3` may only be Apple's Command Line Tools stub
(it offers to install them the first time) - or use Python from python.org or Homebrew. If `hermes` is not on your
PATH, pass `--hermes /full/path/to/hermes`. Start with `--dry-run`: it prints what it found and what it would do.
If the `hermes` that runs works on a different Hermes home than the profile you named, `config.yaml` is left alone,
but the plugin is still copied and indexed, and the printed commands finish the job by hand.

Quit Hermes before installing over a running copy or using `--purge`. On Windows a file that is open stops the move
(after a few seconds' patience, in case a virus scanner is holding a folder that was written a moment ago) and the
installer stops before changing anything - for the plugin folder and, with `--purge`, for the bank and
`memorybank.json` too; macOS and Linux allow moving a folder with open files, so there the installer cannot notice
and Hermes would keep writing to the moved bank. Run the installer as the user who runs Hermes, not with `sudo` (it
refuses to run as root on a profile that belongs to someone else).

**Code that depends on the operating system** (everything else is plain standard-library Python):

- Windows: the default Hermes folder lookup (`%LOCALAPPDATA%\hermes`, `core/cli.py`) and an exclusive port bind
  for the viewer (`core/server.py`); in the installer, a retry when a folder move is blocked, and PowerShell and
  Command Prompt quoting.
- macOS and Linux: a bank folder and database that this version creates are made private to the user (mode 0700 and
  0600; `core/store.py`, and `core/usage.py` when `usage --mark` creates the folder), and an export file is set to
  0600 (`core/cli.py`).
- macOS: the bank runs `synchronous=FULL` with full-fsync checkpoints (`core/store.py`).
- Linux: the viewer prints its link instead of opening a browser, unless you pass `--open` (`core/server.py`).
- Everywhere: the bank uses WAL (its write-ahead journal) only where this SQLite and the file system make that safe.
  On a SQLite with the WAL-reset bug (versions 3.7.0 to 3.51.2, except the patch releases 3.44.6 and later 3.44.x
  and 3.50.7 and later 3.50.x) a NEW bank stays in the rollback journal (a bank that is already in WAL keeps WAL), so
  a reader can wait behind a writer; a file system that refuses WAL (some network and FUSE mounts) leaves the bank in
  the rollback journal too. Keep `HERMES_HOME` on a native volume or a local disk (a Docker named volume, not a bind
  mount from a Mac or Windows host).

None of the macOS and Linux code has been run on a real Mac or Linux machine. `tests/test_posix_sim.py` simulates
it on any system, and its one real-mode test is skipped on Windows and runs on macOS and Linux.

## How it plugs into Hermes

It is a standard Hermes memory-provider plugin (`memory.provider: memorybank`) plus two hooks.

| Hermes does | The bank does |
|---|---|
| session start | opens `plugin-data/memorybank/bank.db` (if the bank is held by another program for a moment - a backup copy, a virus scan - it tries again a few times, pausing half a second between tries); on short-lived background threads inside Hermes mirrors MEMORY.md/USER.md, refreshes the skill index, and (first run only) indexes past sessions |
| builds the system prompt | adds a short note about the bank, plus the "saved beyond MEMORY.md / USER.md" entries (fixed for the session, so the prompt cache holds) |
| before each turn (`prefetch`) | finds what is relevant to your message and returns a short block; Hermes shows "Memory Bank - recalled N memories" |
| each turn start | two file checks: picks up MEMORY.md/USER.md edits made by anyone (background review, another session, you); the mirroring runs beside the turn |
| built-in `memory` tool writes | mirrored immediately; a removal retires the entry, a replacement supersedes it |
| built-in `memory` tool refuses an add for lack of room | the entry is stored in the bank and the answer becomes "saved" (`transform_tool_result` hook) |
| a skill is created, edited or installed | indexed right away (`on_skill_lifecycle` hook) |
| after each turn (`sync_turn`) | updates that session's node (first prompts, where it ended, skills used, entities), and re-reads the transcript to see which recall blocks are still in it |
| once a day (first session start after 24 h) | re-reads what changed in Hermes' session database: picks up anything live capture missed, lets hidden conversations leave recall, brings back the ones you unhide or pin, and retries conversations it could not index last time |
| `memory_bank` tool | remember / search / related / update / forget / status |
| retry, regenerate, edit, `/undo`, compression | the recall block left with that turn, so those memories may be recalled again |
| cron runs, subagents, webhook deliveries | by default: no tool, no note, nothing recalled, no memory written - and starting such a run does not write to the bank (Hermes starts no provider for delegated subagents, so for them this always holds; for cron and webhook runs see `recall.cron` under "Settings") |

If the bank cannot be opened or written when a session starts (after those few retries), or anything else goes
wrong while the provider starts, it stays off for that session - no tool, no note - and Hermes' own memory behaves
as usual. In the command-line chat you are told once; elsewhere only Hermes' log says so.

## What gets recalled, and when

The block is appended to your message (Hermes' design, cache-friendly), so everything injected is replayed for the
rest of the session. The gate is therefore strict:

- everyday words alone never recall anything;
- a **memory** needs one topical word in common with your message;
- a **past session or skill** must be *about* something your message names: its title has to share two specific
  words with it (names, products, acronyms), or one that is rare in your bank;
- entries currently in MEMORY.md/USER.md, and the ones already shown at session start, are skipped - they are in
  the prompt already;
- the conversation itself is never recalled into itself - not its own session, and not the earlier parts of it
  that Hermes carried over when it compacted the conversation, also after a restart, a `/resume` or a rebuilt
  agent (those parts, and the chat a `/branch` was started from, are found from Hermes' session database). The
  conversations before a gateway `/new` or an automatic reset are different conversations and ARE recalled. If
  Hermes has pruned the earlier parts from its database they cannot be recognised and may be offered again;
- budget: 1,100 characters, 6 items, at most 2 sessions and 1 skill.

Every line starts with the item's id (`- #412 ...`), so Hermes can dig deeper (`memory_bank related`, id 412) or
correct exactly that item. A past session is shown as a pointer - its title and date - not as an excerpt of what
was typed in it; for a conversation Hermes has not given a title, that title is the opening words of its first
message (links, paths and addresses removed). Every line is screened by Hermes' own memory scanner on the way out,
and no line can contain a `<memory-context>` tag (Hermes' recall fence) however it is written.

`hermes memorybank recall "<message>"` shows what the engine would pick for that message on its own, with its size.
It is a dry run, not a copy of the live path. A live chat also skips what it was already told in the conversation and
the always-on notes in its system prompt (so a pinned preference shows in the dry run but is not injected), keeps out
other chats' sessions and its own, drops lines that Hermes' memory scanner flags (the command does not run it),
injects nothing when `recall.enabled` is false, and searches only the first 1,600 and the last 800 characters of a
message longer than 2,400. So the live block can lack a line the dry run showed, and can hold one it did not (a cap
that a skipped line freed).

## Usage meter

| Command | What it does |
|---|---|
| `hermes memorybank usage` | by week: calls, context size, cache reads/writes, estimated cost |
| `hermes memorybank usage --since 2026-10-07` | one summary since a date |
| `hermes memorybank usage --mark my-change` | record that you changed a setting now |
| `hermes memorybank usage` | a week later: before / after the latest marker |
| `hermes memorybank usage --json` | the same numbers for scripts |

It reads Hermes' own `state.db` and talks to no model. It covers the main-loop calls of each chat only; compression,
vision, title and background-review calls are not included, and every report says so. The file is opened read-only
and never modified. (Reading a database that Hermes keeps in WAL mode can leave `state.db-wal` and `state.db-shm`
side files next to it; the `-shm` file is about 32 KB, not empty.) If another program has `state.db` locked, the
report says so and asks you to try again, rather than read a file that is being written. If `state.db` cannot be
opened read-only in place (a read-only copy, a hot journal) and a non-empty `state.db-wal` or `state.db-journal`
next to it may hold chats the main file does not, the report stops and says so, and `--mark` refuses; a database with
no such side file is read as it is. Costs are the figures Hermes recorded - list-price estimates unless the provider
reported a real cost - so they are good for comparing one week with another, not for reconciling an invoice. Hermes'
list-price estimates book every cache write at the 5-minute price, so the cost rows cannot show the higher price of
1-hour cache writes.
Chats Hermes recorded no price for (included in a plan, or on a model it could not price) are left out of every cost
figure, and the report says how many calls that is.

What it shows: sessions and API calls; the average context sent per call; how much of the input was served from
the prompt cache (reads), newly written to it (writes) or fresh; pauses of 5-60 minutes and over an hour (a pause
longer than the cache lifetime makes the whole context be written again - the weekly table adds the two into one
column, the other views list both); estimated cost per call; and how often the model used a few tools whose
results are large (`read_file`, `skill_view`, `session_search`, `web_extract`, `memory_bank`; change the list with
`--tools`; shown in every view, also the before/after view of a marker with no earlier chats - a tool that was not in
the marker's list shows "(not tracked then)"). A chat counts in the week where it started - and, for a before/after
report, on the side of the marker where it started, so a chat that was already running when you set the marker is in
"before" only.

**Judging a change.** Run `--mark <label>` right after you change a Hermes setting. The weeks before it are kept as
the baseline, model by model (so old sessions being pruned later does not matter); a baseline of `--baseline-days`
(default 28) that is not a whole number of weeks still includes the week the marker is in. A later `usage` puts
before and after side by side and compares cost per call **for the same models only**, weighted by how much each was
used afterwards - so switching to a cheaper model cannot pass for a saving. Chats that used more than one model or
provider (a `/model` switch or a fallback) are counted in the totals but left out of the same-model figures, and the
report says how many.

The report never says "real". From the weekly figures of the earlier weeks (at least 3, for the same models in the
same mix) it works out the range that 19 of 20 ordinary weeks would stay inside, and prints how many weeks that rests
on. A new figure inside it is "no clear effect yet" (the report adds how large a move that history could show). A
figure outside it "points to a lower cost" (or higher) - and the report calls that a lead, not a proof, adding that
the history is thin when it is under 8 weeks. It says even that only when the same-models figure rests on about 500
priced calls in at least 5 priced chats (a chat Hermes compressed counts once); otherwise it says "No verdict yet"
and what it takes, and when no model has enough calls on both sides, or too few earlier weeks stand behind the range,
"No verdict" and why. It says "too early to judge" until there are about 5 sessions and 500 calls after the change.

For a cache-lifetime change (`prompt_caching.cache_ttl` set to `1h` or `auto`) mainly Claude models count, because
that is what the setting mainly affects (Hermes also sends the cache-lifetime marker on a few other routes, which the
meter does not count): the report says how many of the calls since the change were in scope, and keeps saying "too
early" until there are enough of those. Under `auto` only chats a person paces count - subagent, cron, oneshot,
webhook, kanban, API, tool and batch runs stay on the 5-minute cache; with an explicit `1h` every chat except
delegated subagent runs counts. For such a marker the report also shows cache-write tokens per call (before -> after,
for the counted chats) - the number the setting acts on - and says that a lower cost per call is an upper bound on the
saving, because Hermes' list-price estimates book every cache write at the 5-minute price. Against 3-5 weeks of history the
range is wide, so a saving of 5-10% will usually read "inside that range"; the cache-write row is the direct measure,
and a longer `--baseline-days` (more weeks) narrows the range.

When the numbers back it, a plain report also mentions one idea worth measuring: if many pauses let Hermes' 5-minute
prompt cache go cold mid-chat in Claude chats that a person is having, `prompt_caching.cache_ttl: auto` keeps it warm
for an hour. Hermes books cache writes at the 5-minute price, so the cost rows cannot show what 1-hour writes cost
extra: watch the cache-write row, and measure instead of trusting (for a named profile put `--profile NAME` after
`hermes`):

```
hermes config set prompt_caching.cache_ttl auto
hermes memorybank usage --mark cache-ttl-auto
```

Use Hermes normally for a week, then:

```
hermes memorybank usage
```

To undo:

```
hermes config unset prompt_caching.cache_ttl
```

## Commands

| Command | What it does |
|---|---|
| `hermes memorybank status` | counts, size, location |
| `hermes memorybank check` | recounts memory files, skills and conversations against the bank (details below) |
| `hermes memorybank recall "<message>"` | dry run of the automatic recall (not the live path: see "What gets recalled") |
| `hermes memorybank search <words>` | search memories, sessions, skills (shows ids) |
| `hermes memorybank import [--force] [--limit N]` | re-index skills, memory files, conversations (`--limit`: how many conversations; default `capture.history_limit`, 5000; 0 = all) |
| `hermes memorybank usage [...]` | the usage meter above |
| `hermes memorybank ui [--no-open] [--open] [--port N]` | the network view, served on 127.0.0.1 until stopped; it opens your browser unless `--no-open` (on Linux it only prints the link unless `--open`) |
| `hermes memorybank export [file]` | what the viewer shows, as JSON: every item's title, text, links and first prompts, retired ones included (not the long searchable text of sessions and skills); default: the profile's `backups` folder; the file is set to owner-only (0600) on macOS/Linux, also one that already existed; with no bank yet it says so and creates nothing |
| `hermes memorybank purge <ids>` or `purge --retired` (add `--yes` to do it) | delete from the bank for good (the only thing that does); with `--yes` and nothing left to delete it finishes a clean-up that an earlier purge could not complete |

`check` counts memory entries, skills and the newest `capture.history_limit` conversations. Older conversations that
are not in the bank are reported as "older than history_limit, not indexed on purpose" (`import --limit 0` adds
them); a conversation the importer could not read is listed as "unreadable, see the log" and keeps `check` from
saying everything is in the bank; a conversation Hermes shows again that the bank still has retired is flagged; skills
whose stored text is missing are listed (it does not look at the searchable text of sessions). It is strict in the same
way as `import`: it expects those conversations whatever `capture.sessions` and `capture.import_history_on_first_run`
say. When it finds gaps, it says to run `import --force`. The `import` output also shows how many conversations were
revived (unhidden or pinned again) and how many failed; a conversation that cannot be indexed is retried by every
import (the log says "could not index session").

Without Hermes running: `python3 run.py [--profile NAME] COMMAND` inside the plugin folder. Run from
`<profile>/plugins/memorybank` without `--home` or `--profile`, it acts on the profile the copy is installed in, not
on `HERMES_HOME`: a line on stderr names that profile, and says so when `HERMES_HOME` points elsewhere and is not
used. `--home FOLDER` or `--profile NAME` choose another. Stdout holds only the command's own output, so
`run.py usage --json` parses.

### The `memory_bank` tool

`memory_bank(action=...)`

| action | arguments | does |
|---|---|---|
| `remember` | `content`, optional `kind` (fact, preference, decision, lesson, procedure, person, project, reference), `entities`, `importance` 1-5 | stores one durable statement (up to 8,000 characters, never cut). The same statement again reinforces the first copy; a statement close to an existing one but not the same is stored next to it, and the answer names the older one so it can be retired. A `preference`, or importance 5, is also shown at the start of every session |
| `search` | `query`, optional `limit` | memories, past sessions and skills, best first, with ids |
| `related` | `id` or `query` (an entity name) | what is linked to it |
| `update` | `id`, `content` | corrects a memory |
| `forget` | `id` | retires an item (kept in history, never recalled) |
| `status` | | counts |

`memory.write_approval` is honoured by `remember`, `update` and `forget`: if Hermes' approval check fails while it
runs, the save is refused with a message to the model (when approval is on, or cannot be told to be off) rather than
let through. A Hermes that has no write-approval module, or whose module cannot be imported, has nothing to ask, so
the save goes ahead - as it does in Hermes' own memory tool. (Capturing a conversation's own text into the bank - see
"Safety and what it touches" - is not a memory write and is not subject to it.) Entries that live in MEMORY.md /
USER.md are changed with the built-in `memory` tool; the bank follows.

## The network view

- Desktop app: Capabilities > Plugins > Memory Bank (switch on "Desktop"), then "Memory Bank" in the sidebar.
- Dashboard: the "Memory Bank" tab.
- Anywhere: `hermes memorybank ui` (localhost only; open the link it prints - the access key is in the link; on
  Linux it only prints the link, because a browser started with the link on its command line would show the key to
  other users of the machine - add `--open` to have it opened anyway).

Colours: memories (amber; a ring means always-on in MEMORY.md/USER.md), entities (cyan), skills (violet), sessions
(slate), topics (green). Click for details and links, drag to rearrange, scroll to zoom, `/` to search, `F` to fit,
and the play button replays how the bank grew. Recalls pulse on the graph as they happen.

## Removing things

- **Forget** (the `memory_bank` tool, the viewer's Forget button with Undo, or removing an entry from MEMORY.md /
  USER.md through Hermes) retires an item: never recalled, not returned by search, still in the bank's history,
  restorable.
- **Purge** (command line only; the `memory_bank` tool cannot call it, though an agent with a terminal tool could
  run the command - do not count on Hermes' command approvals to stop that) deletes text, links and index entries
  from the bank - and any name or topic that nothing links to any more, and the per-conversation copies of the
  always-on notes that hold a purged item - and the bank overwrites the freed space instead of keeping the old text
  there. It works in batches, so other programs can keep writing meanwhile. Every purge also rewrites the bank file
  (VACUUM), so text that an earlier version deleted, or that earlier edits left inside the search index, leaves the
  file too; on a big bank quit Hermes first (the rewrite holds the write lock for as long as the file is big, about
  1.3 seconds per 180 MB). The clean-up
  (swept names, merged search index, rewritten file, emptied log file) is complete when the command prints `deleted N item(s) from
  the bank for good`. When another program has the bank open it prints "Not everything could be cleaned up..." and
  exits with 1; `purge --yes` with nothing else, run after closing Hermes, finishes it. Hermes keeps its own copies
  that purge cannot reach: the transcripts of conversations and the system prompt each one started with, in
  `state.db`. Copies you made earlier - an export, a backup folder, a copy of the file - still hold whatever they
  held.
- A conversation you **hide** in Hermes is retired from the bank at the next daily refresh (or
  `hermes memorybank import`), also when the bank first saw it live and the daily refresh never saw it visible. When
  you unhide it (or pin it, which unhides it) the next daily refresh or import reads it again and brings it back,
  including what was said while it was hidden, unless you forgot it in the viewer meanwhile.
  One that is simply **gone** from Hermes' database stays in the bank on purpose:
  Hermes deletes old conversations by itself (90 days by default), and the bank exists to keep what Hermes lets
  go of. To remove one for good, find its #id with `hermes memorybank search <words>` and run
  `hermes memorybank purge <id> --yes`.
- Entries that live in MEMORY.md / USER.md are best changed through Hermes (its `memory` tool): the bank then retires
  a removed entry or supersedes a reworded one. If you delete or reword a line **by hand** in the file, the bank
  notices at the next turn but keeps the old text as an entry marked "no longer in MEMORY.md, may be outdated", and
  it can still be recalled until you forget it (`memory_bank forget <id>`, or find the id with `hermes memorybank
  search` and `purge` it).
- **Backing the bank up.** It is one SQLite file, `plugin-data/memorybank/bank.db`, and it is the only copy of what
  was saved beyond the files and of conversations Hermes has since pruned. To back it up, quit Hermes and copy
  `bank.db` (and `bank.db-wal` / `bank.db-shm` if they are there). `hermes memorybank export` writes a readable JSON
  copy of what the viewer shows, but nothing reads it back in.

## Safety and what it touches

- **Network:** none. It makes no outbound requests and calls no model. The viewer (`hermes memorybank ui`) listens
  on 127.0.0.1 only, only while you run it, and its access key is in the link it prints, not in the page.
- **Processes:** no separate background process. After a session starts, short-lived threads inside the Hermes
  process do the work that should not hold up the first turn (the first-session history import, the skill refresh,
  mirroring MEMORY.md/USER.md, the recall log). The plugin runs no shell commands of its own. The only long-running
  thing is `hermes memorybank ui` when you run it: a local web server (until you stop it) that, unless you pass
  `--no-open`, asks your operating system to open its link in your default browser through Python's standard
  `webbrowser` module (which starts helper programs such as `open`, `osascript` or `xdg-open`; on Linux this is
  skipped unless you pass `--open`). There is no telemetry and no self-update.
- **Reads** (inside the profile only): `memories/MEMORY.md` and `USER.md`, the profile's `skills/` folder,
  `state.db` (sessions and messages; opened read-only) and five settings in `config.yaml` (for the usage meter).
- **Writes:** its own folder `plugin-data/memorybank/` (the bank, and `usage-markers.json` if you use `--mark` - a
  report also saves that file once when it upgrades a marker made by 1.2.0, if the folder is writable);
  `memorybank.json` in the profile folder - written when you finish `hermes memory setup`, or by you; an export
  file only when you ask for one. It never writes `MEMORY.md`, `USER.md`, skills, `state.db` or `config.yaml`.
- **Permissions on macOS and Linux:** a bank folder and database that this version creates are private to the user
  (mode 0700 and 0600; the `-wal` and `-shm` files follow the database). A bank that already exists - for example one
  made by 1.2.0 - keeps the modes it has. To tighten it by hand: `chmod 700 <profile>/plugin-data/memorybank` and
  `chmod 600 <profile>/plugin-data/memorybank/bank.db*`. Hermes keeps its own home folder private by default (unless `HERMES_HOME_MODE` says otherwise). An
  export file is always narrowed to 0600, also one that existed; the `backups/` folder that `export` creates follows
  your umask (the installer makes its backup folders private).
- **Hooks and surfaces:** `transform_tool_result` changes only the answer of the built-in `memory` tool when it
  refuses an add for lack of room - Hermes' "would exceed the limit" refusal, the "over the limit" refusal of a
  batch, and its "consolidation failed" stop-retrying message (the entry goes to the bank instead; for a batch
  that mixes adds and edits the answer says the edits were not applied) - every other tool result is untouched.
  `on_skill_lifecycle` indexes a skill when it is created, edited or installed. Besides the provider hooks it
  offers the model one tool (`memory_bank`, through the memory-provider API), registers one skill (`curate`: how to
  keep the bank tidy) and a command-palette entry in the desktop app, and has a dashboard API whose only routes
  that change anything are Forget and Restore of a single item.
- Whatever is written through the tool, mirrored from the files or captured from a conversation is screened with
  Hermes' own memory threat scanner; flagged text is not stored (or, for file entries, tracked but never
  recalled). Recall lines and search results are screened again on the way out. If the scanner cannot be loaded, or
  fails when it is called, Hermes' log gets one warning per Hermes process ("... could not be loaded ..." or "...
  failed ...", then "text is not being screened by it") and the screening is off until it works again. The standalone
  command line (`run.py import`, or any run without Hermes' scanner on the path) prints one line on stderr the first
  time it cannot load the scanner or it fails; what is allowed does not change. It is a pattern scanner: it catches
  the usual injection and exfiltration phrasings, not a determined attacker.
- A conversation held in one messaging chat is not surfaced in another chat (`recall.share_across_chats`); saved
  memories are profile-wide, like MEMORY.md.
- **Privacy of the bank itself:** it is a plain, unencrypted SQLite file. Besides the text of your memories it
  holds, for each conversation it indexes, the first six things you typed in it (up to 280 characters each, as
  typed), a searchable copy of up to 60 later messages (240 characters each, about 9,000 in all) and a
  300-character excerpt of the last reply; for each skill, the first 12,000 characters of its `SKILL.md`; and, for
  the newest 200 conversations, a copy of the always-on notes (pinned and saved-beyond-the-files entries) that were
  shown at its start. Only the `memory_bank` tool, the viewer and `export` show the excerpts (automatic recall injects
  a title pointer, no excerpt), but nothing removes secrets from them: the safety scanner only catches
  instruction-like text, so a password typed in a chat is in the bank as well. Conversation capture follows
  `capture.sessions` and `capture.import_history_on_first_run` in `memorybank.json` (set both to `false` to stop it),
  is not a memory write and therefore ignores `memory.write_approval`, and what it kept stays after Hermes prunes the
  original (that is the point; `purge` removes it from the bank). Hermes keeps its own copies of conversations, and of
  each conversation's system prompt, in `state.db`. `hermes memorybank export` writes the items - forgotten ones
  included - as readable JSON: treat that file, and the bank, like your memory files and `state.db`.

## Settings

Optional file `<profile>/memorybank.json` (the settings you are most likely to want, with their defaults):

```json
{
  "recall":  { "enabled": true, "max_chars": 1100, "max_items": 6, "max_sessions": 2, "max_skills": 1,
               "min_coverage": 0.34, "min_mass": 0.5, "anchor_rarity": 0.62, "cron": false,
               "standing_chars": 1200, "share_across_chats": false },
  "capture": { "core_mirror": true, "overflow": true, "sessions": true, "skills": true,
               "import_history_on_first_run": true, "history_limit": 5000, "skills_refresh_hours": 6 },
  "ui":      { "port": 8765 }
}
```

Cheaper: lower `max_chars` or `standing_chars`, or set `max_sessions` and `max_skills` to 0 to recall saved memories
only. Chattier: lower `anchor_rarity` / `min_coverage`. `capture.overflow: false` goes back to Hermes refusing saves
that do not fit. `standing_chars` is the room for the entries shown at the start of every session although they
are not in MEMORY.md / USER.md (0 turns the section off). The `capture` switches govern what Hermes does by itself
(`history_limit` also the default of `hermes memorybank import --limit`); `hermes memorybank import` is an explicit
request and indexes skills, memory files and sessions whatever the switches say (and `check` is strict in the same
way). A few more keys exist (`recall.kinds`, `ui.graph_limit`); `recall.kinds` is the default kind filter of every
search, the tool's and the command's too, so narrowing it narrows those as well.

A setting of the wrong kind - a section that is not an object, a number that is a word, a list that is not a list - is
ignored with a one-time warning and the built-in value is used. Two exceptions fail closed: `"capture": false` (or
`"recall": false`) switches every on/off setting of that section off, and the words `false`, `no`, `off`, `0` / `true`,
`yes`, `on`, `1` count as off / on for an on/off setting. Numbers are capped at 1,000,000,000.

**`recall.cron`** (default `false`) lets scheduled runs use the bank read-only. Cron runs then get the note (including
the always-on saved entries), the `memory_bank` tool (search, related and status work; remember, update and forget are
refused - although the note and tool text still describe them) and automatic recall. Webhook deliveries get recall,
and the tool and note only if you added the memory toolset to the webhook platform (Hermes' default webhook toolset
has none). These runs write no memories, but their recalls are logged in the bank (recall log, counters, event feed,
and a copy of the always-on notes when the bank has pinned ones). Delegated subagents are not affected: Hermes starts
them without a memory provider. Leave it off if any of these runs handle input you do not control, because `search`
returns excerpts of your past conversations.

## Measured

Two real profiles, banks built read-only (profile A: 17 memory entries, 189 skills, 134 sessions; profile B: 15,
109, 27):

| | Profile A | Profile B |
|---|---|---|
| In the bank after the first index | 17 of 17 memory entries, 189 of 189 skills (first 12,000 characters each), 134 of 134 sessions | 15 of 15, 109 of 109, 27 of 27 |
| First index | 1-2.5 s, 5.2 MB | 1.6 s, 2.9 MB |
| Text withheld by the safety scanner | 0 pieces | 0 pieces |
| Recall latency (real prompts) | median 6.2 ms, p95 29.5 ms (492 prompts) | median 4.0 ms, p95 25.5 ms (53 prompts) |
| Turns where recall fires | 25% | 34% |
| Size when it fires (the block itself) | median ~42 tokens | median ~53 tokens |
| Average added per turn, with Hermes' own wrapper of about 56 tokens around each block | ~25 tokens | ~37 tokens |
| Unrelated / filler messages that recalled something | 0 of 24 | 0 of 24 |
| Explicit search: a held-out prompt finds its own session | #1 41%, top 3 51%, top 10 64% (39 sessions) | #1 25%, top 3 75% (4 sessions) |

Fixed cost per request (in the cached prefix): about 430 tokens - the system note and the tool schema - plus up to
1,200 characters of "saved beyond the files" entries once there are any. Automatic recall is deliberately
conservative; search is lexical, so the last line is its real ceiling today.

## Known limits

- Retrieval is lexical (SQLite FTS5 + stemming). No embeddings: a paraphrase with no shared distinctive word is
  missed.
- Entities come from capitalisation and spelling rules, not a model. They are conservative but not perfect; Hermes
  can pass exact `entities` when it calls `remember`.
- The bank grows through MEMORY.md/USER.md, saves that did not fit there, the `memory_bank` tool, sessions and
  skills. It does not distil facts out of conversations by itself (that would need a model call per session);
  Hermes' own background review does that, and its new entries now always land.
- Only adds are rescued. A `replace`, alone or as the only operation of a batch, that makes an entry longer than the
  room left is refused by Hermes ("Replacement would put memory at N/L chars ...") and the plugin leaves that answer
  as it is; Hermes' "Memory consolidation failed N times this turn" stop message is likewise only rescued for an
  add. When that happens, save the corrected wording as a new entry (it is kept in the bank) and then remove only the
  old entry it corrects; do not shorten or remove other entries. A batch that mixes a replace or remove with adds
  fails as a whole: the adds are kept in the bank, the edits must be sent again.
- Conversations indexed: the newest `capture.history_limit` (5,000) of them, and not scheduled or delegated runs
  (cron, subagent and webhook sessions), hidden conversations or ones with fewer than two messages. `check` counts
  only those.
- Only the profile's own `skills/` folder is indexed - not skills from `skills.external_dirs`,
  `skills.create_dir` or a trusted project, and not the toolkit folders some profiles carry (`rules/`,
  `commands/`, `.agents/`), which Hermes itself never reads. A skill folder that is a symbolic link is skipped (the
  importer and `check` do not follow links), and only the first 12,000 characters of a `SKILL.md` are indexed: a word
  that occurs only after that point is not found by search or recall.
- The always-on section is small on purpose (1,200 characters by default): profile and preference items first, then
  the newest. Older overflow entries leave it as newer ones arrive; they stay in the bank and are recalled when
  relevant.
- Automatic recall injects a title pointer for a past session, never an excerpt; but for a conversation without a
  Hermes-written title that title is the opening words of its first message, and `memory_bank search`, the viewer
  and `export` show excerpts of what was typed (see "Privacy of the bank itself").
- Hermes counts every refused save, rescued or not, towards its own "stop retrying" cap for a turn. After three
  rescued saves in one turn, another memory edit in that same turn can be refused by Hermes until the next turn.
  Nothing is lost; the model just has to wait.
- When a messaging gateway rebuilds its agent after `/undo` or `/retry`, the memories recalled for the undone turn
  can be held back on the very next turn (they come back after it). In the command-line chat `/undo` no longer does
  this - except where `state.db` cannot be read, then the memories of all but the last undone turn are held back for
  one turn.
- With `memory.write_approval` on, a save that was queued for approval and later does not fit in the file is not
  rescued (the rescue happens when Hermes' memory tool answers, not when a queued write is applied later).
- The usage meter's cache-pause counts come from message timestamps in `state.db`; a Hermes version that stores
  them differently is measured as far as possible and the report says what is missing. A `state.db` whose sessions
  table lacks the call counter (a different Hermes version) cannot be measured and is reported, never guessed at.
- Costs in the usage meter are Hermes' own estimates (cache writes at the 5-minute price), and what "included in a
  plan" means is Hermes' call: those chats are counted as calls but have no price. The meter covers main-loop calls
  only. A before/after verdict says nothing about a change that also changed what you were doing - cost moves with
  the work as well.
- Developed and run on Windows only. macOS and Linux are expected to work but have not been run; the code that
  exists only there (owner-only file modes, macOS durability settings, no browser auto-open on Linux, WAL only where
  safe) has never been executed on a real Mac or Linux machine (see "Platforms").

## Layout

```
__init__.py        Hermes adapter: MemoryProvider + the two hooks
cli.py             what Hermes imports for `hermes memorybank ...` (the code is in core/cli.py)
plugin.yaml        the plugin's manifest (name, version, Hermes floor, hooks)
core/              the engine: store, recall, entities, extract, ingest, coverage, usage, api, config, cli, server
                   (stdlib only)
dashboard/         backend routes + dashboard tab
desktop/plugin.js  desktop app page
ui/                viewer source (engine.js); `python3 ui/build.py` regenerates dashboard/dist and desktop/plugin.js
skills/curate/     how Hermes should curate the bank
run.py             the same commands without Hermes
```
