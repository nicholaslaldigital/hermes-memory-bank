"""Text utilities for the Memory Bank: FTS-aligned tokenizing, query terms and
similarity. (Entity detection lives in ``entities.py``.)

Pure stdlib with no Hermes imports, so the engine loads anywhere: the agent
process, the dashboard backend, the standalone server and the tests.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence

_STOP = """
a about above after again against all also am an and any are as at be because been before being
below between both but by can cannot could did do does doing down during each few for from further
had has have having he her here hers herself him himself his how i if in into is it its itself just
me more most my myself no nor not now of off on once only or other ought our ours ourselves out over
own same she should so some such than that the their theirs them themselves then there these they
this those through to too under until up very was we were what when where which while who whom why
will with would you your yours yourself yourselves
ll ve re don didn doesn isn aren wasn weren won wouldn couldn shouldn hasn haven hadn
please thanks thank hi hello hey ok okay yes yeah yep nope sure want wants wanted need needs needed
like get gets got getting make makes made making use uses used using go goes going gone know knows
think see look looks looking still really actually maybe well one two three new old good great
way thing things something anything everything nothing lot lots bit kind sort etc via per vs
dont doesnt didnt cant wont im ive id thats whats lets let us tell show give help try
able today tomorrow yesterday right much many every
hes shes theyre youre youve weve theyve itll theres heres wasnt werent couldnt shouldnt wouldnt
havent hasnt hadnt arent isnt aint ill youll well theyll hed shed wed theyd gonna wanna gotta
"""
STOPWORDS: FrozenSet[str] = frozenset(_STOP.split())

# FTS5's unicode61 tokenizer splits on everything that is not a letter or a
# digit (underscore included), so query terms are built the same way.
_FTS_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_WS = re.compile(r"\s+")

_SUFFIXES = ("ing", "ies", "ed", "es", "s")

# Everyday English that says little about *what* a message is about. A match
# on one of these alone is never enough to recall something, and one of these
# missing from the bank is not evidence that the bank has nothing relevant.
_COMMON = """
accept achieve act add admit affect agree aim allow answer appear apply argue arrive ask assume
attempt avoid base beat become begin believe belong bring build buy call care carry catch cause
change check choose claim clean clear close come compare complete concern consider contain continue
control cost count cover create cut deal decide describe design determine develop die discover
discuss draw drive drop eat end enjoy enter establish exist expect experience explain express face
fail fall feel fight fill find finish fit follow force forget form gain grow handle happen hear hit
hold hope identify imagine improve include increase indicate intend introduce involve join keep kill
lead learn leave lie listen live lose love maintain manage matter mean measure meet mention mind
miss move notice obtain occur offer open order own pass pay perform pick place plan play point
prepare present prevent produce prove provide pull push put raise reach read realize receive
recognize reduce refer reflect relate release remain remember remove repeat replace reply report
represent require rest result return reveal rise run save say seem sell send serve set share shoot
shut sit sleep sound speak spend stand start state stay stick stop study suffer suggest suppose take
talk teach tend test throw touch train travel treat turn understand visit wait walk watch wear win
wish wonder work worry write
kept made went took gave came said saw told found thought left felt brought began ran broke broken
wrote written sent built bought lost paid met sat stood heard held led meant spent taken given done
known seen shown
actual additional available bad basic best better big black blue brief broad busy certain cheap
cold common correct current dark dead deep different difficult direct due early easy entire equal
exact extra fair false familiar fast fine first free fresh full funny general happy hard heavy high
hot huge important impossible interesting large last late least less likely little local long low
main major minor missing modern natural necessary nice normal obvious odd original overall
particular past perfect personal plain poor popular possible pretty previous primary private proper
public quick quiet rare ready real recent regular relevant rich rough round sad safe serious several
sharp short similar simple single slow small soft solid special specific standard strange strong
sudden tiny total true typical unable unique unusual useful usual various weak whole wide wrong
young
ago ahead almost alone along already always anyway apparently around away back basically behind
beyond certainly clearly completely currently definitely directly easily either else enough
especially even eventually ever exactly finally forward fully generally hardly however immediately
indeed instead later mostly nearly never next often otherwise perhaps possibly probably quickly
quite rather recently simply sometimes soon suddenly together twice usually whether yet
ability amount area attention case chance choice couple course day degree detail difference
direction effect effort example fact feeling focus front future half hand head hour idea interest
job level life line matter middle minute moment month morning name night note number option part
people person piece problem purpose question rate reason sense series side sign situation size
someone step stuff thought time top type version view week word year
anyone anybody appreciate asap awesome cool curious double guess hmm issue kindly oops shall sorry
suggestion tip unsure wondering faster slower bigger smaller longer shorter easier harder
summarize summarise sentence paragraph bullet outline rewrite rephrase shorten expand translate
format draft edit proofread brainstorm recommend generate list simple quick detailed brief plot
story poem joke recipe weekend dinner lunch breakfast morning evening tonight
"""


def fts_tokens(text: str) -> List[str]:
    """Lower-cased tokens exactly as FTS5 ``unicode61`` would split them."""
    return _FTS_TOKEN.findall((text or "").lower())


def light_stem(word: str) -> str:
    """Cheap suffix strip, used only to dedupe terms and compare token sets.

    The index itself uses FTS5's porter stemmer; this only needs to agree with
    it often enough that "configs" and "config" are not counted twice.
    """
    stem = word
    if len(stem) > 4:
        for suffix in _SUFFIXES:
            if not stem.endswith(suffix) or len(stem) - len(suffix) < 3:
                continue
            if suffix == "s" and stem.endswith(("ss", "us", "is")):
                continue
            stem = stem[: -len(suffix)] + ("y" if suffix == "ies" else "")
            break
        if len(stem) >= 4 and stem[-1] == stem[-2] and stem[-1] in "bdgmnprt":
            stem = stem[:-1]
    if len(stem) > 3 and stem.endswith("e"):
        stem = stem[:-1]
    return stem


COMMON_STEMS: FrozenSet[str] = frozenset(light_stem(w) for w in _COMMON.split())


def is_common_word(token: str) -> bool:
    """True for everyday vocabulary that carries no topic on its own."""
    if light_stem(token) in COMMON_STEMS:
        return True
    # Adverbs of common adjectives: "perfectly", "quickly", "exactly".
    return len(token) > 5 and token.endswith("ly") and light_stem(token[:-2]) in COMMON_STEMS


def _is_term(token: str) -> bool:
    if len(token) < 2 or token in STOPWORDS:
        return False
    if token.isdigit() and len(token) < 3:
        return False
    return True


def content_terms(text: str, *, max_terms: Optional[int] = None) -> List[str]:
    """Distinct stopword-free tokens in first-appearance order (deduped by stem)."""
    seen = set()
    out: List[str] = []
    for token in fts_tokens(text):
        if not _is_term(token):
            continue
        key = light_stem(token)
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
        if max_terms and len(out) >= max_terms:
            break
    return out


def term_set(text: str) -> FrozenSet[str]:
    """Stemmed token set, for similarity."""
    return frozenset(light_stem(t) for t in fts_tokens(text) if _is_term(t))


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / float(len(sa) + len(sb) - inter)


_FILLER = frozenset({"a", "an", "the", "is", "are"})


def token_bag(text: str) -> FrozenSet[str]:
    """Every token except articles and "is/are" - numbers, negations and small
    words included. Two texts with the same bag say the same thing in other
    punctuation or word order; ``term_set`` is looser (it drops short numbers
    and "not", so "25 seconds" and "15 seconds" look alike to it)."""
    return frozenset(t for t in fts_tokens(text) if t not in _FILLER)


def trips(scan: Any, text: str) -> bool:
    """True when ``scan`` (Hermes' memory-content scanner, or None) says the
    text looks like injected instructions or an exfiltration attempt."""
    if scan is None or not text:
        return False
    try:
        return bool(scan(text))
    except Exception:
        return False


def norm_text(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())


def content_hash(text: str) -> str:
    """Stable id for a piece of content, insensitive to case and whitespace."""
    return hashlib.sha1(norm_text(text).lower().encode("utf-8", "replace")).hexdigest()[:16]


_FENCE_TAG = re.compile(r"<\s*/?\s*memory-?\s*context\s*>", re.I)


def fence_safe(text: str) -> str:
    """``text`` without anything that reads as a ``<memory-context>`` fence tag, however it is nested.

    Hermes wraps recall in that fence and strips fence tags out of what a provider returns in a single pass, so
    stored text such as ``</memory</memory-context>-context>`` comes out as a real closing tag and everything after
    it would sit outside the fence. Removing the tags until none is left (each pass shortens the text, so this ends)
    keeps stored text from doing that, and leaves every other ``<`` and ``>`` alone.
    """
    previous = None
    while previous != text:
        previous, text = text, _FENCE_TAG.sub("", text)
    return text


def clip(text: str, limit: int) -> str:
    """Single-line clip with an ellipsis, cut on a word boundary when possible."""
    flat = norm_text(text)
    if len(flat) <= limit:
        return flat
    cut = flat[: max(1, limit - 1)]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


def make_title(text: str, limit: int = 72) -> str:
    """Short label: the first sentence or line of ``text``."""
    flat = (text or "").strip()
    first_line = flat.split("\n", 1)[0].strip()
    match = re.search(r"(?<=[.!?])\s", first_line)
    if match and match.start() >= 12:
        first_line = first_line[: match.start()]
    return clip(first_line or flat, limit)


_CTX_OPEN = re.compile(r"<\s*memory-context\s*>", re.I)
_CTX_CLOSE = re.compile(r"</\s*memory-context\s*>", re.I)
# A message longer than this is only read as far as this (it is long before it is interesting); it keeps every
# pattern below from being run over megabytes of text on the turn that Hermes is waiting for.
PROMPT_READ_LIMIT = 24000


def strip_context_blocks(text: str) -> str:
    """Remove injected recall blocks, in time proportional to the text (many opening tags and no closing tag
    used to make a single regular expression quadratic)."""
    out, pos = [], 0
    while True:
        opening = _CTX_OPEN.search(text, pos)
        if not opening:
            break
        closing = _CTX_CLOSE.search(text, opening.end())
        if not closing:
            break   # an unclosed block is left as it is
        out.append(text[pos:opening.start()])
        pos = closing.end()
    out.append(text[pos:])
    return "".join(out)
# Hermes prepends its own bracketed notices to some user turns
# ("[The user attached an image: ...] [Examine it with ...] actual text").
_NOTICE_RE = re.compile(r"^\s*(?:\[[^\[\]]{12,4000}\]\s*)+")
_ATTACH_RE = re.compile(r"@(image|file|folder|url|doc|pdf):(\"[^\"]+\"|'[^']+'|\S+)", re.I)
_REF_RE = re.compile(
    r"\bhttps?://\S+"                                   # links
    r"|(?:\b[A-Za-z]:\\|\\\\)[^\s\"'<>|]+"              # C:\... and \\server\...
    r"|(?<![\w.])~?/(?:[\w.~-]+/)+[\w.~-]*"             # /unix/style/paths
    r"|\b[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}\b"   # e-mail addresses (bounded: no quadratic backtracking)
)
_NOISE_HEADS = (
    "[system", "<system", "[context", "[compaction", "[summary", "[conversation summary", "[important:",
    "[async delegation", "[the user attached", "[background", "[tool", "[reminder", "[note:", "[notice",
)


_AUTO_NAME_RE = re.compile(r"^(?:pasted|paste|composer|image|img|screenshot|screen|clipboard|capture|untitled)\b", re.I)
_ATTACHED_MARK = "--- Attached Context ---"
_CAMEL_SPLIT_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _attachment_words(match: "re.Match[str]") -> str:
    """A referenced file keeps the words of its name; an image or an
    auto-named paste keeps nothing."""
    if match.group(1).lower() != "file":
        return " "
    name = re.split(r"[\\/]", match.group(2).strip("\"'"))[-1]
    name = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name)
    words = re.sub(r"[_\-.]+", " ", name).strip()
    return " " if _AUTO_NAME_RE.match(words) else " " + words + " "


def clean_prompt(text: str) -> str:
    """What the user actually wrote: injected recall blocks, Hermes' bracketed
    notices, attachment markers and attached file contents removed."""
    out = strip_context_blocks((text or "")[:PROMPT_READ_LIMIT])
    out = out.split(_ATTACHED_MARK, 1)[0]
    out = _NOTICE_RE.sub("", out)
    out = _ATTACH_RE.sub(_attachment_words, out)
    return out.strip()


def split_camel(text: str) -> str:
    """"AcmeQuestionnaire" -> "Acme Questionnaire" ("" when nothing to split)."""
    parts = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9]{5,}", text or ""):
        spaced = _CAMEL_SPLIT_RE.sub(" ", token)
        if " " in spaced:
            parts.append(spaced)
    return " ".join(parts)


def is_noise_prompt(text: str) -> bool:
    """A user-role message that is really a system notice, or empty."""
    return len(text) < 3 or text[:60].lower().lstrip().startswith(_NOISE_HEADS)


def strip_refs(text: str) -> str:
    """Drop links, paths and addresses: their pieces ("users", "appdata", "com")
    match everything and mean nothing."""
    return _REF_RE.sub(" ", (text or "")[:PROMPT_READ_LIMIT])


def query_text(text: str) -> str:
    """A message reduced to the words worth searching the bank for."""
    return strip_refs(clean_prompt(text))


def norm_entity(name: str) -> str:
    """Lookup key for an entity name."""
    flat = _WS.sub(" ", (name or "").strip().strip(".,;:!?()[]{}\"'`“”’"))
    flat = re.sub(r"['’]s$", "", flat)
    return flat.lower()


def split_core_entries(raw: str, delimiter: str = "\n§\n") -> List[str]:
    """Entries of a Hermes MEMORY.md / USER.md file (``§``-delimited)."""
    if not raw:
        return []
    text = raw.replace("\r\n", "\n")
    return [part.strip() for part in text.split(delimiter) if part.strip()]


def parse_frontmatter(text: str) -> Dict[str, str]:
    """Minimal YAML-frontmatter reader: top-level ``key: value`` pairs only."""
    out: Dict[str, str] = {}
    if not text.startswith("---"):
        return out
    end = text.find("\n---", 3)
    if end < 0:
        return out
    key = None
    for line in text[3:end].split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match and not line[0].isspace():
            key = match.group(1).lower()
            value = match.group(2).strip()
            if value in (">", "|", ">-", "|-", ">+", "|+"):
                value = ""
            out[key] = value.strip("\"'")
        elif key and line[0].isspace():
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def join_tags(existing: str, additions: Sequence[str]) -> str:
    """Merge names into a ``; ``-separated tag string without duplicates."""
    have = [t.strip() for t in (existing or "").split(";") if t.strip()]
    keys = {norm_entity(t) for t in have}
    for name in additions:
        key = norm_entity(name)
        if key and key not in keys:
            keys.add(key)
            have.append(name.strip())
    return "; ".join(have)
