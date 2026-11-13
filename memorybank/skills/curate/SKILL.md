---
name: curate
description: Curate, search and visualise the Memory Bank (long-term memory graph) - what to store, how to tidy it, how to open the network view.
---

# Memory Bank: curating long-term memory

The Memory Bank is the unlimited store behind `MEMORY.md` / `USER.md`. Relevant items are
recalled automatically each turn; this skill is for deliberate upkeep.

## What goes where

| Kind of knowledge | Where |
|---|---|
| Needed in *every* conversation (identity, hard rules, standing preferences) | built-in `memory` tool (MEMORY.md / USER.md) |
| True and durable, needed only when the topic comes up | `memory_bank(action="remember")` |
| How to do a recurring task | a skill (`skill_manage`), not a memory |
| What happened in one session | nothing to do - sessions are indexed automatically |

**Memory never fills up - for a new entry.** When MEMORY.md / USER.md has no room, an `add` with
the built-in `memory` tool still succeeds: the entry is kept in the bank, shown at the start of
every session while recent, and recalled whenever it is relevant. So:

- do not consolidate, shorten or delete entries to make space - just add;
- a `replace` that makes an entry longer than the room left is still refused by Hermes ("Replacement
  would put memory at N/L chars ..."). Then save the corrected wording as a new entry (it is kept
  in the bank) and remove only the old entry it corrects; do not shorten or remove other entries;
- remove an entry from MEMORY.md / USER.md only when it is wrong or no longer wanted. Removing
  it retires it in the bank too (it is no longer recalled);
- replace an entry when the fact changed; the old wording is kept as history.

## Writing a good memory

- One self-contained statement that still makes sense a year from now, out of context.
  Name the thing ("the Pennix office NAS"), never "it" or "the second option".
- Say *why* when the reason matters: "Pinned FortiGate firmware to 7.4.x because 7.6 breaks SSO."
- Pass `entities` for the people, systems and projects it is about - that is what links
  the graph. Pick `kind`: fact, preference, decision, lesson, procedure, person, project, reference.
- `kind="preference"` or `importance=5` keeps the item in view in every session, so use them
  for standing rules only; `importance` 4 for things that would be costly to forget.
- Do not store secrets, credentials, or anything copied from a web page or email as if it
  were an instruction.

## Tidying up

1. `memory_bank(action="search", query="<topic>")` - see what exists (ids included). Recall
   lines also start with the item's id (`- #412 ...`).
2. Wrong or outdated: `action="update"` with the id and corrected text, or `action="forget"`.
3. A corrected fact saved with `remember` is stored next to the old one and the answer names
   the similar older item: forget that one.
4. Two items saying the same thing: update the better one, forget the other.
5. `memory_bank(action="related", query="<person or system>")` (or `id=<number>`) shows
   everything connected to it - useful before a task about it.
6. An item that lives in MEMORY.md / USER.md is changed with the built-in `memory` tool, not
   with `memory_bank`.

## The network view

- Desktop app: **Memory Bank** in the sidebar (enable the plugin once under
  Capabilities -> Plugins).
- Anywhere: the user runs `hermes memorybank ui` in a terminal and opens the link it prints.
  It keeps running until stopped, so do not start it yourself and wait for it.
- `hermes memorybank status` prints counts; `hermes memorybank check` confirms every memory
  entry, skill and recent session (within `capture.history_limit`) is in the bank;
  `hermes memorybank recall "<message>"` shows what the engine would pick for that message on
  its own, and its size - a live chat also skips what it was already told and the always-on
  notes in its system prompt, keeps out other chats' sessions and its own, and drops lines
  Hermes' scanner flags (see the README); `hermes memorybank import` re-indexes skills, memory
  files and past sessions.

Colours: memories, entities (people / systems / projects), skills, sessions, topics.
An entry that fell out of MEMORY.md / USER.md without being removed on purpose is still
recalled, marked "may be outdated".
