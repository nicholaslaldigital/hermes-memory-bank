# Submitting the Memory Bank to the Hermes plugin catalog

The [plugin catalog](https://hermes-agent.nousresearch.com/docs/plugins) is a curated, human-reviewed list kept in
the `NousResearch/hermes-agent` repository. A listing lets anyone install the plugin by name
(`hermes plugins install memorybank`) at the exact commit a maintainer read. The full rules are in
`plugin-catalog/README.md` of that repository and in its developer guide ("Submitting to the plugin catalog");
this page is the short version for this plugin.

Submitting is a public act, so it is yours to decide and to do: it needs a public repository under your account and
a pull request under your name.

## What the rules mean for this plugin

| Rule | Where this plugin stands |
|---|---|
| Entries are added only by a reviewed pull request, pinned to an exact 40-character commit | You open the PR; every later change is a new PR that moves the pin |
| Submitted by the repository's owner or a major contributor | The repository has to be yours |
| No self-updating code | None: no downloads, no network at all |
| Declared capabilities must match what it registers | `provides_hooks: transform_tool_result, on_skill_lifecycle`; no `register_tool` tools, middleware or env vars (checked by `hermes plugins validate`). The model-facing `memory_bank` tool and the `curate` skill come through the memory-provider API and `register_skill`, which the validator's probe does not see - so the PR text names them (see `catalog/PR_DESCRIPTION.md`) |
| The install scanner and the desktop-surface / no-core-override checks pass | `tests/test_hermes_integration.py` runs Hermes' own validator on the installed copy; run `hermes plugins validate` again on the commit you pin (see below) |
| Risky behaviour is disclosed | `catalog/PR_DESCRIPTION.md` lists it; it is also in `memorybank/README.md` |
| Truthful compatibility metadata | `requires_hermes: ">=0.21.5"` - the version it was tested with |
| One listing per plugin lineage | It is not a fork of a listed plugin |

## Decide first

1. **Where it is hosted:** the public repository https://github.com/nicholaslaldigital/hermes-memory-bank (the
   catalog clones it at the pinned commit).
2. **A license** (this repository uses the MIT License, see `LICENSE`).
3. **The name shown as maintainer** and the `author` line in `memorybank/plugin.yaml` (now "Hermes Memory Bank").
4. **Your commit e-mail.** Commits are public. Use GitHub's no-reply address
   (`git config user.email "<id>+<username>@users.noreply.github.com"`) if you do not want a real one shown.
5. If the code was written for an employer or on their equipment, check you are free to publish it.

## Before every push (a public repository's history cannot be taken back)

- Run `python3 tests/test_portability.py` on the folder you are about to commit (on Windows `python`, see the
  README). It fails on a database, bytecode (outside `__pycache__`), key file, absolute user folder, e-mail address
  or credential-shaped string in any file git would add. It deliberately does not read `.claude/`, `dist/` or
  `__pycache__/` (your own tool settings, build zips and bytecode, which can carry your user folder): the
  `.gitignore` keeps them out of a repository, and the test fails if it stops doing so. So keep the `.gitignore`,
  create the repository with git from this folder (`git init`, `git add .`), and never upload or zip the folder as it
  stands. It is a net for the patterns it knows, not a review.
- Check the test fixtures and comments contain only made-up people, devices, offices and hostnames; public product,
  vendor and city names are used in them as ordinary words, and none belongs to you, your employer or a colleague.
  The ones in this folder are made up; anything you add from your own work is yours to check. A search of the whole
  tree for your own name, employer, colleagues, hostnames and office names takes a minute. Clean the folder before
  the first commit.
- Take the screenshot from a demo profile (`tests/test_core.py` builds one with `make_home`), not from a real profile.
- Reorder `memorybank/README.md` for readers who arrive through the catalog: the catalog's docs page renders that file,
  so put `hermes plugins install memorybank` first and the installer (which a catalog install does not contain) in
  an "Alternative: from a clone of the repository" paragraph, and drop the "once listed" wording.
- Optional, and worth a re-run of `hermes plugins validate`: add `kind: exclusive` to `memorybank/plugin.yaml` so the
  loader does not have to infer it from the source, and use the real owner in `author`.

## Steps

1. Commit and push the release you want listed to the public repository, keeping the plugin in the `memorybank/`
   folder (the catalog entry points there with `subdir: memorybank`).
2. Run the same checks the catalog runs, on a checkout of the commit you will pin:

   ```
   hermes plugins validate path/to/your-checkout/memorybank --install-deps
   python3 tests/run_tests.py
   ```

   Both should pass (the test run reports anything it had to skip, and a skipped test was not run).
3. Get the commit id: `git rev-parse HEAD` (40 hex characters). Pin that exact commit; later commits do not count
   until you re-pin.
4. Copy `catalog/memorybank.yaml` and fill in `sha` (`repo`, `maintainer` and `docs_url` already name this
   repository), keeping `version` equal to the one in `memorybank/plugin.yaml`.
5. Fork `NousResearch/hermes-agent`, add that one file as `plugin-catalog/memorybank.yaml` (the PR must touch
   nothing else), and open a pull request using `catalog/PR_DESCRIPTION.md` as the description. Add a screenshot of
   the network view.
6. Wait for the catalog CI to go green: it clones your repository at the pin and runs `hermes plugins validate`.
   A maintainer then reviews the tree and merges, asks for changes, or declines. If they ask for changes, push a new
   commit and update `sha` (and `version`) in the same PR.
7. After the merge, anyone can run `hermes plugins install memorybank`.

To update later: push the change, then open a new PR that moves `sha` and `version`.

## What was checked on this version

`hermes plugins validate memorybank` (Hermes 0.21.5): manifest, `requires_hermes`, loadable, capability probe
(declared hooks match registrations), install security scan, no core override, desktop surface inside the plugin
SDK. `tests/test_hermes_integration.py` runs Hermes' own validator on the installed copy, prints every check and
fails on any that is not ok; it does not record whether the scan's verdict was "safe" or "caution", so read that
line yourself. Re-run `hermes plugins validate` on the commit you pin; the result is what counts.
