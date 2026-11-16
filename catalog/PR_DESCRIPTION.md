## Add `memorybank` to the plugin catalog

Adds `plugin-catalog/memorybank.yaml` (category `memory`, tier `community`), pinned to `<sha>` of
`<repo>` (version 1.5.0; the plugin is the repository's top folder, so there is no `subdir`). I am the repository's owner.

### What it is

A memory-provider plugin (`memory.provider: memorybank`) that keeps long-term memory in one local SQLite file per
profile, as a knowledge graph:

- new entries (adds) that do not fit in `MEMORY.md` / `USER.md` are stored in the bank instead of being refused (a
  replace that makes an entry longer than the room left is still refused by Hermes; the README says what to do);
- both files are mirrored; every skill (first 12,000 characters) and the newest 5,000 conversations (not scheduled or
  delegated runs, hidden or one-message chats) are indexed;
- relevance-gated recall before each turn (lexical, no model calls), screened with Hermes' own memory scanner;
- a network view (Desktop route + dashboard tab + a local page) and a usage meter (`hermes memorybank usage`: reads
  `state.db` read-only, covers main-loop chat calls only; `--mark` saves `usage-markers.json`).

It is a third-party provider, not a fork of a listed plugin. (The name is a generic one; the README says what it does
and does not do.)

### Hermes surfaces used

- Memory provider API (`initialize`, `system_prompt_block`, `prefetch`, `sync_turn`, `on_memory_write`,
  `on_session_switch`, `on_session_end`, `get_config_schema` / `save_config`) and **one model-facing tool,
  `memory_bank`** (remember / search / related / update / forget / status), offered through the memory-provider API
  rather than `register_tool` - which is why `provides_tools` in the entry is `[]` (the probe only sees
  `register_tool` calls). `remember`, `update` and `forget` honour `memory.write_approval`; if the approval check
  fails while it runs, the save is refused, not allowed (when approval is on, or cannot be told to be off). A Hermes
  without a write-approval module, or one whose module cannot be imported, has nothing to ask and saves as its own
  memory tool does.
- Hooks: `transform_tool_result` (rewrites only the built-in `memory` tool's refusals for lack of room - "would exceed
  the limit", the "over the limit" refusal of a batch, and the "consolidation failed" stop-retrying message, each only
  when it answers an add - so the entry goes to the bank instead; every other result, a refused replace included, is
  untouched) and `on_skill_lifecycle`.
- One registered skill, `curate` (how the agent should keep the bank tidy), and a command-palette entry in the Desktop
  app.
- A `cli.py` with `register_cli(subparser)` (the memory-provider CLI convention) for `hermes memorybank ...`; a
  dashboard plugin API (its only routes that change anything are Forget and Restore of one item); a Desktop route and
  sidebar entry that use only `@hermes/plugin-sdk`.

No core function is replaced or wrapped, no private dict is written, nothing is imported from the app bundle. The
engine is loaded under the plugin-private module name `_memorybank_core` (one `sys.modules` entry, needed for
relative imports of files loaded by path); no other module entry is touched.

### Disclosure (rule 13)

- **Network:** none. No outbound requests, no model calls, no telemetry, no self-update.
- **Processes and threads:** no separate background process. After a session starts, short-lived threads inside the
  Hermes process do the first-session history import, the skill refresh, the MEMORY.md/USER.md mirroring and the
  recall log. `hermes memorybank ui` (run by the user) starts a local web server on 127.0.0.1 until stopped and, unless
  `--no-open` is given, asks the operating system to open the link in the default browser through Python's
  `webbrowser` module (so `open`, `osascript` or `xdg-open` may be started on macOS/Linux; on Linux it is skipped
  unless `--open` is given, because the link carries a per-run access key).
- **Shell commands:** the plugin runs none itself. (`install.py`, which is in the repository but is never loaded by the plugin, runs the user's own
  `hermes` commands - and, only when no `hermes` command can run, Hermes' own config writer in Hermes' Python - when
  the user runs it.)
- **Reads outside the plugin's data**, inside the profile only: `memories/MEMORY.md`, `memories/USER.md`, the
  profile's `skills/` folder, `state.db` (opened read-only) and five settings in `config.yaml` (read-only).
- **Writes:** `plugin-data/memorybank/` (the bank; `usage-markers.json` when `--mark` is used, and once more when a
  report upgrades a marker made by 1.2.0), `memorybank.json` (written when the user finishes `hermes memory setup`,
  or created by the user), an export file only when the user runs `export`. On macOS/Linux a bank folder and database
  that this version creates are private to the user (0700 / 0600) and an export file is always set to 0600; a bank
  that already exists keeps its modes. It never writes `MEMORY.md`, `USER.md`, skills, `state.db` or `config.yaml`.
- **In the page (Desktop app, dashboard tab, local page):** The network view keeps one value in the `localStorage` of the page it runs in (the Desktop app, the dashboard
  tab or the local page): the key `memorybank-view`, holding `2d` or `3d` (the 2D/3D choice); nothing else is
  stored in the browser and the Python side never reads it.
  While its page is open and visible the view animates on UI timers (about 30 frames a second; it stands still
  under the system's reduced-motion setting and in a hidden window), and every timer is cleared when the page is
  left. To match the host theme it reads - never writes - the computed background colour of its own ancestors; it
  queries no app element by selector and observes nothing.
- **What it stores from conversations:** for each conversation, the first six user messages (up to 280 characters each,
  verbatim), a searchable copy of up to 60 later messages (240 characters each, about 9,000 in all) and a 300-character
  excerpt of the last reply, plus the first 12,000 characters of each skill and, for the newest 200 conversations, a
  copy of the always-on notes shown at its start - in a plain, unencrypted SQLite file. Nothing removes secrets from
  them (the Hermes scanner only catches instruction-like text). They are returned by
  `memory_bank search`, shown in the viewer and written by `export`; automatic recall injects a title pointer only.
  Capture is not a memory write and is not gated by `memory.write_approval`; `capture.sessions: false` and
  `capture.import_history_on_first_run: false` in `memorybank.json` switch it off. What was kept survives Hermes'
  own pruning, on purpose; `hermes memorybank purge` (command line only - the `memory_bank` tool cannot call it, though
  an agent with a terminal tool could run it, and Hermes' command approvals do not single it out) deletes it from the
  bank, also the per-conversation copies of the always-on notes that hold a purged item, and overwrites the freed
  space and rewrites the file at every purge. The clean-up is complete when the command reports no warning.
  Hermes' own copies - the transcripts in `state.db` and the system prompt each conversation started with - are not
  touched.
- **Screening:** text written through the tool, mirrored or captured is screened with Hermes' memory threat scanner, and
  every recall line is screened again on the way out; no line can contain a `<memory-context>` tag. If the scanner
  cannot be loaded, or fails when it is called, a warning is logged once per Hermes process and text is not screened
  until it works again (the standalone command line says so once on stderr).
- **Credentials:** none read, none stored; no `requires_env`.
- **Dependencies:** none beyond Hermes' Python (standard library only).
- **Unattended runs:** by default cron, subagent and webhook sessions get no tool, no note, no recall and no writes. If
  the user sets `recall.cron: true`, cron and webhook sessions get automatic recall, the note and read-only tool
  access (search, related and status work; remember, update and forget are refused); they still write no memories, but
  their recalls are logged (recall log, counters, event feed). Delegated sub-agents are unaffected: Hermes starts them
  without a memory provider.

### Validation

`hermes plugins validate memorybank --install-deps` on the pinned commit:

```
<paste the output here>
```

Test suite (`python3 tests/run_tests.py`, `python` on Windows): <paste the summary here>. Tested on Windows with
Hermes 0.21.5. OS-specific code: two Windows items (the default Hermes folder, an exclusive port bind for the viewer)
and macOS/Linux items (owner-only modes for the bank, its folder and exports; macOS durability settings; no auto-open
of the viewer on Linux; WAL only where SQLite and the file system make it safe). The macOS/Linux items have never been
run on a real Mac or Linux machine (they are simulated in `tests/test_posix_sim.py`); the README says so and the
entry's `known_issues` repeats it.

Derived from: nothing (not a fork of a listed plugin).

<screenshot of the network view>
