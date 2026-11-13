"""Finding the people, systems and projects a piece of text talks about.

No model is involved, so this is deliberately conservative: it prefers to miss
a name than to invent a hub out of a heading, a log line or a shouted word.
What it relies on:

* only prose is read - code blocks, console output, key/value dumps, paths,
  URLs and e-mail addresses are skipped;
* a capital letter means something only where it is a choice: not at the start
  of a sentence, and not in a Title Case line (a heading or a session title);
* everyday and generic IT vocabulary is never a name on its own;
* anything the bank already knows is recognised wherever it appears.
"""

from __future__ import annotations

import re
from typing import Dict, FrozenSet, Iterator, List, Optional, Tuple

from .extract import COMMON_STEMS, STOPWORDS, light_stem, norm_entity

# Capitalised words that are almost never the name of something.
_GENERIC_WORDS = """
next step steps quick question questions key finding findings summary overview note notes update
updates status plan plans action actions item items option options issue issues problem problems
solution solutions result results detail details background context goal goals task tasks part
phase section example examples important warning error errors fix fixes todo done true false
morning afternoon evening night week weeks month months year years day days time
monday tuesday wednesday thursday friday saturday sunday january february march april june
july august september october november december jan feb mar apr jun jul aug sep sept oct nov dec
check create add remove run open find write send review set keep continue start stop remember
first second third finally however although because since while before after once again
great perfect nice sorry everything nothing question answer request response
here there this that these those what when where why how which who whose
english version file files folder folders page pages line lines list table report reports
user users admin team teams company customer client project name names number numbers
method approach process system service services tool tools data info information change changes
test tests testing build work works working current latest final main default general
hermes claude assistant agent model memory session sessions skill skills
security access code output input email mail infrastructure configuration config disk script server
network device account password policy backup database firewall domain cloud storage application
app endpoint laptop desktop mobile browser portal console dashboard support helpdesk ticket incident
disaster recovery compliance audit risk vendor contract invoice budget finance sales marketing legal
office remote internal external production staging development engineering operations management
department manager director employee staff partner meeting calendar schedule document spreadsheet
presentation drive link website site web internet online offline login logout username credential
token certificate encryption alert notification message chat video audio image photo picture
screenshot download upload install upgrade release deploy deployment migration integration
automation workflow pipeline procedure checklist template survey questionnaire analysis assessment
strategy roadmap objective metric contains contents distinct doctor choose instance hybrid routing
enabled disabled copyright connection settings setting properties property description summary
subject body header footer title content source target destination origin region zone tier group
role permission rule filter query search index cache queue job cron trigger event log logs monitor
host port path home temp root local global public private shared personal business enterprise pro
standard premium basic free trial beta alpha stable legacy custom native hybrid virtual physical
select usage wipe flag value timestamp duration commitment administrator principal reader owner
contributor viewer editor member guest secret behavior capability correlation continuity auth
authentication authorization register none null hub tower stack bucket adapter provider
click tab window button screen menu icon cursor keyboard mouse scroll drag paste typing typed
loading loaded logging logged signing signed website webpage popup dialog prompt field checkbox
dropdown toggle sidebar toolbar shortcut command terminal shell editor program software hardware
computer machine phone tablet monitor printer cable wifi bluetooth battery charger speaker camera
confused confusing stuck broken weird wrong correct proper clearly plainly manually automatically
location order step guide tutorial instruction walkthrough explanation overview comparison
""".split()

# CamelCase that is an identifier or a field name, not a product.
_TECH_STOP = frozenset("""
readonly readwrite localappdata appdata fullname domainname categoryinfo fullyqualifiederrorid
erroraction silentlycontinue invalidargument datetime timestamp username hostname filename pathname
typename displayname firstname lastname childitem expandpropertynotfound javascript typescript
""".split())

_GENERIC_STEMS: FrozenSet[str] = frozenset(light_stem(w) for w in _GENERIC_WORDS) | COMMON_STEMS

# All-caps tokens that are not acronyms for anything worth a node.
_ACRONYM_STOP = frozenset("""
OK ID IDS API APIS URL URLS URI PDF CSV TSV JSON YAML YML XML HTML CSS SQL UI UX OS PC IT AI LLM
USB CPU RAM GPU FAQ CLI GUI IDE SDK KB MB GB TB MS PR PRS QA VS IP IPS TODO NOTE NOT MUST DO DONT
ERROR TRUE FALSE NULL NONE YES NO GET POST PUT PATCH DELETE HEAD HTTP HTTPS AND OR THE FOR ALL ANY
NEW OLD ADD FIX BUG END RUN SET USE WHY HOW WHO ASAP FYI IMO BTW TBD EOD EOW ETA AM PM EST EDT PST
PDT CST CDT MST MDT UTC GMT USD README TLDR PS RE FW FWD CC BCC NA TBA WIP LGTM
HIGH LOW MEDIUM CRITICAL INFO WARN DEBUG FAILED FAIL PASS PASSED DEAD ALIVE OPEN CLOSED DONE PENDING
ACTIVE MAX MIN AVG SUM HOME PATH TEMP TMP USER ROOT HOST PORT NAME TYPE DATA FILE TEST PROD DEV MAIN
LOCAL ASYNC SYNC OWNS BAND ALSO ONLY JUST VERY MORE LESS THAN THEN WHEN WHAT WITH FROM THIS THAT
HAVE BEEN WILL EACH BOTH SAME LAST NEXT FIRST STOP START WAIT HELP NEVER ALWAYS CEO CFO CTO COO VP
DB AZ AP DOM
""".split())

# Verbs that commonly open a note or a request right before a proper noun.
_ACTION_VERBS = frozenset("""
deploy install configure migrate rotate enable disable reset restart reboot upgrade verify audit
investigate troubleshoot diagnose resolve escalate connect disconnect sync backup restore export
import generate draft summarize schedule cancel approve deny block grant revoke assign unassign
purge delete archive rename copy download upload scan patch monitor log document confirm validate
debug analyze list fetch query launch execute compile ship publish merge revert rollback wipe
enroll provision decommission onboard offboard lock unlock renew switch email ping notify remind
track chose chosen got moved uses used runs ran added fixed set sent built switched assess locate
extend convert reconcile recheck rerun modify rank pilot optimize edit expand rebuild refresh
recreate redeploy reinstall retry revisit rewrite tighten harden clean compare explain summarise
""".split())

_UNITS = frozenset("min sec hr day week month year percent gb mb kb tb ms am pm of to or and x".split())

# Words allowed *inside* a name ("Bank of America"). "and" / "the" are left
# out on purpose: they far more often join two separate things.
_CONNECTORS = frozenset({"of", "de", "&"})

_CODE_BLOCK_RE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]{1,200}`")
_URL_RE = re.compile(r"\bhttps?://([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,24})[^\s<>\"')\]]*")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PATH_RE = re.compile(r"(?:\b[A-Za-z]:\\|\\\\|~/|(?<![\w/.])/(?=[\w.~-]+/))[^\s\"'<>|]+")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:['’][A-Za-z]+)?|\d+(?:\.\d+)*|&")
_POSSESSIVE_RE = re.compile(r"['’][A-Za-z]+$")
_BOUNDARY_RE = re.compile(r"[.!?:;\t•|(){}\[\]<>\"“”\\/=+*#%$@^~]|\s[-–—]\s|,")
_ACRONYM_RE = re.compile(r"[A-Z][A-Z0-9]{1,5}s?")
_CAMEL_RE = re.compile(r"(?:[A-Z][a-z0-9]+){2,}[A-Z]?|[a-z]{1,3}[A-Z][A-Za-z0-9]+|[A-Z]{2,}[a-z][A-Za-z0-9]+")
_TITLE_RE = re.compile(r"[A-Z][a-z]{2,}")
_LONG_WORD_RE = re.compile(r"[A-Za-z]{4,}")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d{1,3}[.)])\s+")

# Lines that are machine output rather than something a person wrote.
_CONSOLE_RE = re.compile(
    r"^\s*(?:PS\s+[A-Za-z]:|[A-Za-z]:\\[^>\n]{0,120}>|\$ |>>> |\.\.\. |\+ |[\w.-]+@[\w.-]+[:~$#]"
    r"|at\s+[\w.$<>]+\(|File \"|Traceback|\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}|\[?\d{1,2}:\d{2}:\d{2}"
    r"|[\[{<|]|[-=_*#~]{3,}|[A-Z][a-z]+-[A-Z][A-Za-z]+\b)"          # ... or a PowerShell cmdlet
    r"|(?:\s\.){3,}|Copyright \(C\)|\bPS [A-Za-z]:\\|\s\|\s"          # dot leaders, banners, pipelines
    r"|(?:^|\s)-{1,2}[A-Za-z]{2,}[\w-]*(?:\s+\S+)?\s+-{1,2}[A-Za-z]{2,}"  # two command flags
)
# "Host Name: AB12-0034", "DHCP Enabled: Yes" - settings dumps, not sentences.
# ("Owner: Janet Quillan" is kept: its value is neither a number nor a flag.)
_KEY_VALUE_RE = re.compile(
    r"^\s*[A-Za-z][\w .()/-]{0,40}?\s*[:=]\s*"
    r"(?:\S{0,80}\d\S{0,80}(?:\s+\S+){0,2}|(?:yes|no|true|false|none|null|on|off|enabled|disabled|n/a))\s*$",
    re.I,
)


def _is_generic(token: str) -> bool:
    """Everyday or generic-IT vocabulary: never a name by itself."""
    low = token.lower()
    return low in STOPWORDS or light_stem(low) in _GENERIC_STEMS


def is_generic(token: str) -> bool:
    """Public form of the test above (the recall gate uses it too)."""
    return _is_generic(token)


def _token_class(token: str) -> str:
    """'acronym' | 'tech' | 'title' | 'num' | 'connector' | '' for one bare word."""
    if token.lower() in _CONNECTORS:
        return "connector"
    if token[0].isdigit():
        return "num"
    if _ACRONYM_RE.fullmatch(token):
        return "acronym"
    if _CAMEL_RE.fullmatch(token):
        return "tech"
    if _TITLE_RE.fullmatch(token):
        return "title"
    return ""


def _acronym(token: str) -> Optional[str]:
    """The acronym worth keeping, or None for shouting, units and labels."""
    base = token[:-1] if len(token) >= 3 and token.endswith("s") and token[:-1].isupper() else token
    if base in _ACRONYM_STOP or token in _ACRONYM_STOP:
        return None
    letters = sum(ch.isalpha() for ch in base)
    digits = len(base) - letters
    if letters < 2 and digits < 2:
        return None  # V2, P1, Q3 ...
    if digits == 0 and _is_generic(base):
        return None  # an ordinary word in capitals: FAILED, SELECT, AS ...
    return base


def _tech(token: str) -> Optional[str]:
    return None if token.lower() in _TECH_STOP else token


def _attached(line: str, start: int, end: int) -> str:
    """How a token is glued to its neighbours: 'id' (part of an identifier or
    path), 'hyphen' (inside a hyphenated compound or a flag) or ''."""
    before = line[start - 1] if start > 0 else " "
    before2 = line[start - 2] if start > 1 else " "
    after = line[end] if end < len(line) else " "
    after2 = line[end + 1] if end + 1 < len(line) else " "
    if before in "_\\/@%$#=" or after in "_\\/=(":
        return "id"
    if (before in ".:" and before2.isalnum()) or (after in ".:" and after2.isalnum()):
        return "id"
    # "Get-ChildItem", "Wi-Fi", and command flags such as "-ErrorAction".
    if before == "-" or (after == "-" and after2.isalnum()):
        return "hyphen"
    return ""


def _prose_lines(text: str) -> Iterator[Tuple[str, bool]]:
    """``(line, is_headline)`` for the lines that read like human prose."""
    for raw in text.split("\n"):
        line = _BULLET_RE.sub("", raw.strip())
        if len(line) < 2:
            continue
        if _CONSOLE_RE.search(line) or _KEY_VALUE_RE.match(line):
            continue
        if len(line) >= 40:
            wordy = sum(ch.isalpha() or ch.isspace() for ch in line)
            if wordy / len(line) < 0.62:
                continue
        # Title Case shows in the ordinary words: prose keeps "review" and
        # "access" lower-case mid-sentence, a heading capitalises them too.
        # (Counting every word would mistake a sentence full of names for one.)
        words = _LONG_WORD_RE.findall(line)
        ordinary = [w for w in words[1:] if _is_generic(w)]
        capitalised = sum(1 for w in ordinary if w[0].isupper())
        headline = len(ordinary) >= 3 and capitalised / len(ordinary) >= 0.7
        if not headline:
            # A line of nothing but capitalised words is a label or a banner
            # ("Windows IP Configuration", "Device State") unless it is purely
            # names ("Janet Quillan"). A "Label: value" line is judged by its value.
            tail = _LONG_WORD_RE.findall(line.rsplit(":", 1)[-1])
            if tail and all(w[0].isupper() for w in tail):
                headline = any(_is_generic(w) for w in tail)
        yield line, headline


def _opens_with_verb(token: str) -> bool:
    """A capitalised sentence opener that is an action, not part of a name."""
    low = token.lower()
    if low in _ACTION_VERBS or _is_generic(low):
        return True
    return len(low) >= 5 and low.endswith(("ed", "ing"))


def _singles(run: List[Tuple[str, str]], out: List[Tuple[str, str]]) -> None:
    """Keep only the tokens whose spelling is itself a signal."""
    for token, cls in run:
        kept = _acronym(token) if cls == "acronym" else _tech(token) if cls == "tech" else None
        if kept:
            out.append((kept, cls))


def _flush(run: List[Tuple[str, str]], starts_sentence: bool, headline: bool,
           out: List[Tuple[str, str]], known: Optional[Dict[str, str]] = None) -> None:
    """Turn one run of capitalised tokens into entity candidates (usually one)."""
    if headline:
        # Every word is capitalised here, so only spellings that are a choice
        # anywhere (ACRONYMS, CamelCase) say anything.
        return _singles(run, out)
    if starts_sentence and len(run) >= 2 and run[0][1] == "title":
        first = run[0][0]
        # "Deployed Addigy to ...", "Chose WireGuard over ...", and the very
        # common "Recheck XYZ ..." (a capitalised word before an acronym at the
        # start of a sentence is a verb far more often than part of a name).
        if _opens_with_verb(first) or (run[1][1] == "acronym" and not (known and first.lower() in known)):
            run = run[1:]
            starts_sentence = False
    # Leading filler and trailing glue carry no naming information.
    while run and (run[0][1] in ("connector", "num") or _is_generic(run[0][0])):
        run = run[1:]
        starts_sentence = False
    while run and (run[-1][1] == "connector" or run[-1][0].lower() in STOPWORDS):
        run = run[:-1]
    run = run[:4]
    while run and run[-1][1] == "connector":
        run = run[:-1]
    if not run:
        return
    if len(run) == 1:
        token, cls = run[0]
        if cls == "title":
            if not starts_sentence and not _is_generic(token):
                out.append((token, "name"))
            return
        return _singles(run, out)
    if sum(1 for _, c in run if c == "acronym") >= 2:
        return _singles(run, out)  # "XYZ AWS PHP" is three things, not one name
    words = [t for t, _ in run]
    if len({w.lower() for w in words}) != len(words):
        return  # "Instance ARN Instance ARN": a pasted table header, not a name
    if all(c in ("connector", "num") or _is_generic(t) or (c == "acronym" and not _acronym(t)) for t, c in run):
        return  # "HTTP 400", "Next Steps"
    out.append((" ".join(words), "name"))


def extract_entities(
    text: str,
    known: Optional[Dict[str, str]] = None,
    *,
    max_entities: int = 12,
) -> List[Tuple[str, str]]:
    """Named things in ``text``: ``[(display_name, subtype), ...]``.

    Subtypes: ``name`` (capitalised phrase), ``tech`` (CamelCase product),
    ``acronym``, ``domain``, or ``known`` for an entity already in the bank
    (``known`` maps normalised names to display names).
    """
    if not text:
        return []
    found: List[Tuple[str, str]] = []
    seen = set()

    def add(name: str, subtype: str) -> None:
        key = norm_entity(name)
        if len(key) < 2 or key in seen or len(found) >= max_entities:
            return
        seen.add(key)
        found.append((name.strip(), subtype))

    body = _CODE_BLOCK_RE.sub("\n", text[:20000])
    domains = [m.group(1).lower() for m in _URL_RE.finditer(body)]
    body = _URL_RE.sub(" . ", body)
    body = _EMAIL_RE.sub(" . ", body)
    body = _PATH_RE.sub(" . ", body)
    body = _INLINE_CODE_RE.sub(" . ", body)

    candidates: List[Tuple[str, str]] = []
    for line, headline in _prose_lines(body):
        tokens = [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(line)]

        # 1. Entities the bank already knows, matched as 1-4 word n-grams.
        if known:
            lows = [_POSSESSIVE_RE.sub("", t[0]).lower() for t in tokens]
            for size in (4, 3, 2, 1):
                for i in range(0, len(lows) - size + 1):
                    if size > 1 and tokens[i + size - 1][1] - tokens[i][2] > 40:
                        continue
                    display = known.get(" ".join(lows[i : i + size]))
                    if display:
                        add(display, "known")

        # 2. Runs of capitalised words.
        run: List[Tuple[str, str]] = []
        sentence_start = True
        run_starts_sentence = False
        prev_end = 0
        for index, (token, start, end) in enumerate(tokens):
            gap = line[prev_end:start]
            if _BOUNDARY_RE.search(gap) or len(gap) > 3:
                if run:
                    _flush(run, run_starts_sentence, headline, candidates, known)
                    run = []
                sentence_start = True
            bare = _POSSESSIVE_RE.sub("", token)
            cls = _token_class(bare)
            glue = _attached(line, start, end)
            if glue == "id" or (glue == "hyphen" and cls != "acronym"):
                cls = ""  # part of an identifier, a path, a flag or a compound
            joins = False
            if run and gap == " ":
                if cls == "num":
                    # "Microsoft 365", "SOC 2" yes; "Addigy 10 minutes ago" no:
                    # a small number followed by a plural or a unit is a count.
                    following = tokens[index + 1] if index + 1 < len(tokens) else None
                    unit = following[0].lower() if following and line[end:following[1]] == " " else ""
                    counts = (bare.isdigit() and int(bare) < 100 and unit[:1].isalpha() and unit == following[0]
                              and (unit.endswith("s") or unit in _UNITS))
                    # A dotted number is a version ("Python 3.11.15"), not part of the name.
                    joins = run[-1][1] not in ("num", "connector") and not counts and "." not in bare
                elif cls == "connector":
                    joins = run[-1][1] != "connector"
            if cls in ("acronym", "tech", "title") or joins:
                if not run:
                    run_starts_sentence = sentence_start
                run.append((bare, cls))
                if bare != token or cls == "num":
                    # A possessive or a number closes the name:
                    # "Janet Quillan's | MacBook", "Microsoft 365 | Business".
                    _flush(run, run_starts_sentence, headline, candidates, known)
                    run = []
            elif run:
                _flush(run, run_starts_sentence, headline, candidates, known)
                run = []
            sentence_start = False
            prev_end = end
        if run:
            _flush(run, run_starts_sentence, headline, candidates, known)

    for name, subtype in candidates:
        add(name, subtype)
    for host in domains[:3]:
        add(host[4:] if host.startswith("www.") else host, "domain")
    return found
