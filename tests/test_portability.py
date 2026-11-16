"""Portability and hygiene checks for the shareable package (standard library only, any Python 3.8+).

    python3 tests/test_portability.py

Checks that the folder is safe to hand to someone else: it parses as the Python versions it promises, versions
and declared hooks agree everywhere, the generated UI bundles are current, nothing is shipped that should not
be (databases, bytecode, credentials, a bank export), and no file that reads as text carries an absolute user
path, an e-mail address or a credential-shaped string. It looks inside the folder as it is: it skips
__pycache__, .git, .claude and the build folder dist/ on purpose (the zip builder and .gitignore keep those out
of a package or a repository), and it knows only the patterns below - it is a net, not a review.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT      # the repository's top folder IS the plugin (what installs from a plain repository link)
PASSED: List[str] = []

SKIP_PARTS = {"__pycache__", ".git", "node_modules", ".claude", "dist-packages"}
SKIP_ROOT = {"dist"}     # the build output folder next to install.py - not dashboard/dist, which IS shipped
FORBIDDEN_SUFFIXES = {".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3", ".pyc", ".pyo", ".log", ".pem", ".key",
                      ".p12", ".pfx", ".zip"}
FORBIDDEN_NAMES = {".env", "auth.json", "credentials.json", "secrets.json"}
# The first key `hermes memorybank export` writes: a file carrying it is a copy of a bank (every memory, forgotten ones
# included), whatever it is called and wherever it was written. Code and docs mention the string, so they are exempt.
EXPORT_SIGNATURE = '"format": "hermes-memorybank/1"'
# What a file manager drops into a folder it has shown (Finder, Explorer): not part of the package. The zip builder
# (package.py) and the installer (install.py) both skip them, so they must not fail the check on the machine of
# someone who simply opened the unzipped folder.
OS_LEFTOVERS = {".DS_Store", "Thumbs.db", "desktop.ini"}

# Absolute home folders of a real person, e-mail addresses, and credential-shaped strings.
PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "a Windows user folder": re.compile(r"[A-Za-z]:\\{1,2}Users\\{1,2}[A-Za-z0-9_.~-]+", re.I),
    # also the Git Bash (/c/Users/name) and WSL (/mnt/c/Users/name) spellings, with or without a trailing slash
    "a macOS/Linux home folder": re.compile(r"(?<![A-Za-z0-9_.~-])(?:/mnt/[a-z]|/[a-z])?/(?:Users|home)/[A-Za-z0-9_.~-]+"),
    "an e-mail address": re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
    "an API-key-like string": re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{22,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"
        r"|AIza[0-9A-Za-z_-]{35}|xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.)"),
    "a private key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----"),
}


# Domains that are plainly invented: the whole domain must be one of the usual placeholders or a reserved name
# (a real company whose name merely contains "example" or "acme" is not).
FICTIONAL = re.compile(
    r"(?:^|\.)(?:example\.(?:com|net|org)|contoso\.com|fabrikam\.com|users\.noreply\.github\.com)$"
    r"|\.(?:test|example|invalid|localhost)$", re.I)


def passed(text: str) -> None:
    PASSED.append(text)
    print("PASS  " + text)


def files() -> List[Path]:
    return sorted(p for p in ROOT.rglob("*")
                  if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_PARTS)
                  and p.relative_to(ROOT).parts[0] not in SKIP_ROOT
                  and p.name not in OS_LEFTOVERS and not p.name.startswith("._"))


def t_syntax() -> None:
    lean = {ROOT / "install.py", PLUGIN / "core" / "usage.py", ROOT / "tests" / "fake_hermes.py"}
    for path in files():
        if path.suffix != ".py":
            continue
        version = (3, 8) if path in lean else (3, 9)
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=version)
        except SyntaxError as exc:
            raise AssertionError("%s does not parse as Python %d.%d: line %s: %s"
                                 % (path.relative_to(ROOT), version[0], version[1], exc.lineno, exc.msg))
    passed("every .py file parses as Python 3.9 (install.py, the usage meter and the test stand-in as 3.8)")


def registered_hooks(source: str) -> set:
    """Hook names the plugin's register() hands to ctx.register_hook (directly, or from a literal tuple in a loop)."""
    tree = ast.parse(source)
    names = set()
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == "register"):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
                for item in node.iter.elts:
                    if isinstance(item, ast.Tuple) and item.elts and isinstance(item.elts[0], ast.Constant) \
                            and isinstance(item.elts[0].value, str):
                        names.add(item.elts[0].value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "register_hook" \
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names.add(node.args[0].value)
    return names


def manifest() -> Dict[str, str]:
    data: Dict[str, str] = {}
    hooks: List[str] = []
    in_hooks = False
    for line in (PLUGIN / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("  - ") and in_hooks:
            hooks.append(line[4:].strip())
            continue
        in_hooks = line.startswith("provides_hooks:")
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            data[key.strip()] = value.strip().strip('"')
    data["hooks"] = ",".join(hooks)
    return data


def t_versions_and_hooks() -> None:
    m = manifest()
    for key in ("name", "version", "description", "requires_hermes"):
        assert m.get(key), "plugin.yaml has no %s" % key
    assert re.match(r"^>=\d+\.\d+(\.\d+)?$", m["requires_hermes"]), m["requires_hermes"]
    core = re.search(r'__version__ = "([^"]+)"', (PLUGIN / "core" / "__init__.py").read_text(encoding="utf-8"))
    assert core and core.group(1) == m["version"], "core/__init__.py version %s != plugin.yaml %s" % (core and core.group(1), m["version"])
    dash = re.search(r'"version":\s*"([^"]+)"', (PLUGIN / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    assert dash and dash.group(1) == m["version"], "dashboard manifest version differs"
    entry = ROOT / "catalog" / "memorybank.yaml"
    if entry.exists():
        text = entry.read_text(encoding="utf-8")
        assert re.search(r'^version:\s*"%s"' % re.escape(m["version"]), text, re.M), "catalog entry version differs"
        assert re.search(r'^requires_hermes:\s*"%s"' % re.escape(m["requires_hermes"]), text, re.M), "catalog entry floor differs"
        for hook in m["hooks"].split(","):
            assert ("- " + hook) in text, "catalog entry does not declare hook " + hook
    registered = registered_hooks((PLUGIN / "__init__.py").read_text(encoding="utf-8"))
    assert registered == set(m["hooks"].split(",")), "registered hooks %s != declared %s" % (sorted(registered), m["hooks"])
    passed("one version everywhere (%s), Hermes floor %s, declared hooks == registered hooks (%s)"
           % (m["version"], m["requires_hermes"], m["hooks"].replace(",", ", ")))


def t_bundles_current() -> None:
    done = subprocess.run([sys.executable, "-B", str(PLUGIN / "ui" / "build.py"), "--check"], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    passed("the generated UI bundles match ui/engine.js")


def t_required_files() -> None:
    for rel in ("plugin.yaml", "__init__.py", "cli.py", "run.py", "README.md", "core/store.py", "core/recall.py", "core/usage.py",
                "dashboard/manifest.json", "dashboard/plugin_api.py", "dashboard/dist/index.js", "desktop/plugin.js",
                "ui/engine.js", "skills/curate/SKILL.md"):
        assert (PLUGIN / rel).is_file(), "missing from the plugin: " + rel
    for rel in ("install.py", "README.md", "docs/DETAILS.md", "CATALOG.md", "catalog/memorybank.yaml", "catalog/PR_DESCRIPTION.md", "tests/run_tests.py"):
        assert (ROOT / rel).is_file(), "missing: " + rel
    passed("every file the installer and the docs rely on is present")


def read_text(path: Path) -> Optional[str]:
    """The text of a file, or None when it is binary (a NUL in its first 4 KB, unless it is UTF-16 with a byte-order mark)."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def t_nothing_that_should_not_ship() -> None:
    for path in files():
        rel = path.relative_to(ROOT)
        assert path.suffix.lower() not in FORBIDDEN_SUFFIXES, "should not be shipped: %s" % rel
        assert path.name not in FORBIDDEN_NAMES, "should not be shipped: %s" % rel
        if path.suffix.lower() not in (".py", ".md"):
            text = read_text(path)
            assert not (text and EXPORT_SIGNATURE in text), "looks like an export of a bank (it holds what was said): %s" % rel
    passed("no databases, bytecode, logs, key files, bank exports or editor leftovers")


def gitignored(rel: str, pattern: str) -> bool:
    """Does a .gitignore line of the simple kinds used here (name, name/, /name/, *.ext) match this relative path?"""
    anchored = pattern.startswith("/")
    body = pattern.lstrip("/")
    directories_only = body.endswith("/")
    body = body.rstrip("/")
    parts = rel.split("/")
    candidates = parts[:-1] if directories_only else parts
    if anchored:
        return bool(candidates) and fnmatch.fnmatch(candidates[0], body)
    return any(fnmatch.fnmatch(part, body) for part in candidates)


def t_gitignore_hides_nothing_shipped() -> None:
    lines = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
    patterns = [line for line in lines if line and not line.startswith("#")]
    assert patterns, ".gitignore is empty"
    hidden = []
    for path in files():
        rel = path.relative_to(ROOT).as_posix()
        for pattern in patterns:
            if gitignored(rel, pattern):
                hidden.append("%s (by %s)" % (rel, pattern))
    assert not hidden, "a git repository made from this folder would leave these out: " + ", ".join(hidden)
    for needed in ("dashboard/dist/index.js", "desktop/plugin.js"):
        assert not any(gitignored(needed, p) for p in patterns), needed
    for leak in ("preview-home/plugin-data/memorybank/bank.db", "x/bank.db-wal", "x/__pycache__/m.pyc", ".claude/launch.json",
                 "dist/hermes-memorybank-1.3.0.zip", ".DS_Store", "core/.DS_Store", "memorybank-export-20261112-101500.json"):
        assert any(gitignored(leak, p) for p in patterns), "not ignored: " + leak
    passed(".gitignore leaves out banks, bytecode, previews, builds and OS leftovers - and nothing that is shipped")


def t_no_personal_details() -> None:
    hits: List[Tuple[str, str, str]] = []
    me = Path(__file__).resolve()
    for path in files():
        text = None if path == me else read_text(path)
        if text is None:
            continue                       # this file itself (it holds the patterns), or binary
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS.items():
                for match in pattern.finditer(line):
                    if label == "an e-mail address" and FICTIONAL.search(match.group(0).split("@", 1)[1]):
                        continue                      # a made-up company used in a test fixture
                    hits.append(("%s:%d" % (path.relative_to(ROOT), number), label, match.group(0)))
                    break
    assert not hits, "\n".join("%s: %s %r" % h for h in hits[:20])
    passed("no absolute user folders, e-mail addresses or credential-shaped strings in any file that reads as text")


def main() -> int:
    failed = 0
    for fn in (t_syntax, t_versions_and_hooks, t_bundles_current, t_required_files, t_nothing_that_should_not_ship,
               t_gitignore_hides_nothing_shipped, t_no_personal_details):
        try:
            fn()
        except Exception as exc:
            failed += 1
            print("FAIL  %s\n      %s" % (fn.__name__, str(exc).replace("\n", "\n      ")))
    print("\n%d/%d passed" % (len(PASSED), len(PASSED) + failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
