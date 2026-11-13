"""Ranking and the per-turn recall block.

Retrieval is lexical (FTS5 BM25 with porter stemming) plus the graph's own
enrichment (linked entity names are indexed with each item). What decides
whether anything is injected at all is an explicit relevance gate, because an
irrelevant recall is paid for on every later request of the session:

* ``coverage``  - share of the query's distinctive terms the item matches,
  weighted by how rare each term is in this bank;
* ``mass``      - absolute rarity matched, so one common word is never enough.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .entities import is_generic
from .extract import clip, content_terms, fence_safe, is_common_word, jaccard, query_text, strip_refs, term_set, trips
from .store import RECALLABLE, Store

HEADER = "## Memory Bank (long-term recall)"
STANDING_HEADER = ("Saved beyond MEMORY.md / USER.md (profile and preference items first, then newest; "
                   "correct with memory_bank update/forget and the #id):")
_LABELLED = {"decision", "lesson", "procedure", "preference", "person", "project", "reference"}
_MAX_QUERY_TERMS = 64
_MATCH_TERMS = 24
_TOP_MASS_TERMS = 8
_COMMON_CAP = 0.45     # most an everyday word may count, as a share of a unique term
_COMMON_UNSEEN = 0.15  # what an everyday word the bank never used adds to the bar
_KIND_ORDER = {"memory": 0, "session": 1, "skill": 2}
_TOLD_LINE = re.compile(r"^- #(\d+) ")


def _left_file(meta: Dict[str, Any]) -> str:
    return "USER.md" if meta.get("core") == "user" else "MEMORY.md"


def told_ids(text: str) -> Optional[Set[int]]:
    """Ids of the items named by the recall blocks inside ``text``; None when
    the text holds no recall block at all. Every line of a block starts with
    ``- #<id>``, which is what makes a transcript say what it was told."""
    if not text or HEADER not in text:
        return None
    found: Set[int] = set()
    for chunk in text.split(HEADER)[1:]:
        for line in chunk.split("\n")[1:]:
            match = _TOLD_LINE.match(line)
            if match:
                found.add(int(match.group(1)))
            elif not line.startswith("- "):
                break
    return found


def told_in_transcript(messages: Iterable[Any]) -> Optional[Set[int]]:
    """What the user turns of a live transcript still carry of earlier recalls
    (None when no recall block is left in it). Hermes keeps the injected block
    beside the user message (``api_content``) or, for a message with
    attachments, as one of its text parts."""
    found: Optional[Set[int]] = None
    for message in list(messages or ()):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        texts: List[str] = []
        for key in ("api_content", "content"):
            value = message.get(key)
            if isinstance(value, str):
                texts.append(value)
            elif isinstance(value, list):
                texts.extend(
                    part["text"] for part in value if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
        for text in texts:
            ids = told_ids(text)
            if ids is not None:
                found = ids if found is None else (found | ids)
    return found


@dataclass
class Hit:
    id: int
    uid: str
    kind: str
    subtype: str
    title: str
    content: str
    status: str
    in_core: bool
    created_at: float
    score: float
    coverage: float
    mass: float
    meta: Dict[str, Any] = field(default_factory=dict)

    def brief(self, limit: int = 240) -> Dict[str, Any]:
        """Compact form for tool results and the viewer."""
        text = self.content if self.kind == "memory" else (self.title or self.content)
        out: Dict[str, Any] = {
            "id": self.id,
            "kind": self.kind if self.kind != "memory" else (self.subtype or "memory"),
            "text": clip(text, limit),
            "when": time.strftime("%Y-%m-%d", time.localtime(self.created_at)),
            "score": round(self.score, 3),
        }
        if self.kind == "session" and self.content:
            out["about"] = clip(self.content, limit)
        if self.kind == "skill" and self.content:
            out["about"] = clip(self.content, 160)
        if self.status == "evicted":
            out["status"] = f"no longer in {_left_file(self.meta)} - may be outdated"
        elif self.status != "active":
            out["status"] = self.status
        if self.in_core:
            out["in_core"] = True
        return out


def _date(ts: float, now: float) -> str:
    then, today = time.localtime(ts), time.localtime(now)
    if then.tm_year == today.tm_year:
        return time.strftime("%b %d", then).replace(" 0", " ")
    return time.strftime("%b %d %Y", then).replace(" 0", " ")


def _prior(node: Dict[str, Any], now: float) -> float:
    """How much to trust and favour an item independent of the query."""
    age_days = max(0.0, (now - float(node["created_at"])) / 86400.0)
    kind = node["kind"]
    if kind == "memory":
        prior = (0.7 + 0.6 * float(node["importance"])) * (0.6 + 0.4 * float(node["confidence"]))
        prior *= 0.88 + 0.12 * math.exp(-age_days / 180.0)
        if node["status"] == "evicted":
            prior *= 0.92
        return prior
    if kind == "session":
        return 0.72 * (0.65 + 0.35 * math.exp(-age_days / 60.0))
    if kind == "skill":
        return 0.8 * (0.8 + 0.4 * float(node["importance"]))
    return 0.5


def standing_notes(store: Store, max_chars: int, scan: Any = None) -> Tuple[str, List[int]]:
    """What should be in view in every session although it is not in the
    built-in files: entries that overflowed from MEMORY.md / USER.md and
    standing preferences. Returns the text and the ids it contains.

    This is the always-on tier beyond the built-in files; it is small on
    purpose. Whatever does not fit here is still recalled when relevant.
    """
    if max_chars <= len(STANDING_HEADER) + 20:
        return "", []
    lines: List[str] = []
    ids: List[int] = []
    used = len(STANDING_HEADER)
    for node in store.pinned_memories():
        line = fence_safe(f"- #{node['id']} {clip(node['content'], 240)}")
        if trips(scan, line):
            continue  # screened again on the way out: this text goes into the system prompt
        if used + len(line) + 1 > max_chars:
            continue  # a shorter, older one may still fit
        lines.append(line)
        ids.append(int(node["id"]))
        used += len(line) + 1
    if not lines:
        return "", []
    return STANDING_HEADER + "\n" + "\n".join(lines), ids


class RecallEngine:
    def __init__(self, store: Store, cfg: Optional[Dict[str, Any]] = None) -> None:
        self.store = store
        self.cfg = dict((cfg or {}).get("recall", cfg or {}))

    def _opt(self, key: str, default: Any) -> Any:
        value = self.cfg.get(key, default)
        if value is None:
            return default
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            try:
                return type(default)(value)
            except (TypeError, ValueError, OverflowError):
                return default   # "lots" where a number belongs: the default, not a failed recall on every turn
        return value

    # -- search -------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        kinds: Optional[Sequence[str]] = None,
        limit: int = 8,
        gate: bool = True,
        exclude: Optional[Set[int]] = None,
        include_core: bool = False,
        statuses: Sequence[str] = RECALLABLE,
        now: Optional[float] = None,
        allow: Optional[Callable[[Hit], bool]] = None,
    ) -> List[Hit]:
        """Best matches for ``query``, most relevant first.

        ``gate=True`` applies the relevance thresholds (automatic recall);
        ``gate=False`` returns whatever matches at all (explicit search).
        ``allow`` can veto single items; it runs before near-duplicates are
        collapsed and before the result is cut to ``limit``, so a vetoed item
        can neither hide an allowed look-alike nor use up one of the places.
        """
        now = time.time() if now is None else now
        kinds = list(kinds or self._opt("kinds", ["memory", "session", "skill"]))
        terms = content_terms(query, max_terms=_MAX_QUERY_TERMS)
        if not terms:
            return []
        store = self.store
        total = max(2, store.fts_total())
        dfs = {t: store.fts_df(t) for t in terms}
        idf_ref = math.log(1.0 + (total - 0.5) / 1.5)  # a term found in exactly one item
        idf: Dict[str, float] = {}
        for term, df in dfs.items():
            weight = math.log(1.0 + (total - min(df, total) + 0.5) / (df + 0.5))
            if is_common_word(term):
                # Everyday vocabulary: never decisive when it matches, and no
                # evidence of a gap when the bank has simply never used it.
                weight = min(weight, _COMMON_CAP * idf_ref) if df > 0 else _COMMON_UNSEEN * idf_ref
            idf[term] = weight
        # What matching "everything distinctive" would be worth: the rarest few
        # terms of the query. A topical word this bank has never seen counts at
        # full weight, so a match on the leftovers cannot look complete.
        denom = sum(sorted(idf.values(), reverse=True)[:_TOP_MASS_TERMS]) or 1.0
        match_terms = sorted((t for t in terms if dfs[t] > 0), key=lambda t: -idf[t])[:_MATCH_TERMS]
        if not match_terms:
            return []

        ranked = store.fts_search(
            match_terms, kinds, limit=max(40, limit * 6), statuses=statuses, include_core=include_core
        )
        if exclude:
            ranked = [(i, r) for i, r in ranked if i not in exclude]
        if not ranked:
            return []
        ids = [i for i, _ in ranked]
        mass: Dict[int, float] = {}
        matched_terms: Dict[int, int] = {}
        topical: Set[int] = set()        # matched a word that is not everyday English
        anchors: Dict[int, int] = {}     # specific (non-generic) words shared with its title
        anchor_idf: Dict[int, float] = {}  # ... and the rarest of them
        for term in match_terms:
            found = store.fts_hits(term, ids)
            if not found:
                continue
            is_topical = not is_common_word(term)
            is_specific = is_topical and not is_generic(term)
            for node_id in found:
                mass[node_id] = mass.get(node_id, 0.0) + idf[term]
                matched_terms[node_id] = matched_terms.get(node_id, 0) + 1
            if is_topical:
                topical |= found
            if is_specific:
                for node_id in store.fts_hits(term, found, columns=("title", "tags")):
                    anchors[node_id] = anchors.get(node_id, 0) + 1
                    anchor_idf[node_id] = max(anchor_idf.get(node_id, 0.0), idf[term])
        best_rank = min(r for _, r in ranked)
        nodes = store.get_many(ids)
        min_cov = float(self._opt("min_coverage", 0.34))
        min_mass = float(self._opt("min_mass", 0.5))
        anchor_rarity = float(self._opt("anchor_rarity", 0.62))

        hits: List[Hit] = []
        for node_id, rank in ranked:
            node = nodes.get(node_id)
            if node is None:
                continue
            matched = mass.get(node_id, 0.0)
            coverage = min(1.0, matched / denom)
            rel_mass = matched / idf_ref if idf_ref > 0 else 0.0
            if gate:
                curated = node["kind"] == "memory"
                strict = 1.0 if curated else 1.25
                if coverage < min_cov * strict or rel_mass < min_mass * strict:
                    continue
                # Everyday words alone never recall anything. A curated memory
                # needs one topical word in common with the message. A past
                # session or a skill - long, loosely worded text - must be
                # *about* something the message names: its title has to share
                # two specific words with the message (names, products,
                # acronyms...), or one that is rare in this bank ("AWS" alone
                # says little when a tenth of the bank is about AWS).
                if curated:
                    if node_id not in topical:
                        continue
                else:
                    named = anchors.get(node_id, 0)
                    if named < 1 or (named < 2 and anchor_idf.get(node_id, 0.0) < anchor_rarity * idf_ref):
                        continue
                    if len(terms) > 1 and matched_terms.get(node_id, 0) < 2:
                        continue
            rel_rank = (rank / best_rank) if best_rank < 0 else 1.0
            score = coverage * (0.7 + 0.3 * max(0.0, min(1.0, rel_rank))) * _prior(node, now)
            hits.append(Hit(
                id=node_id, uid=node["uid"], kind=node["kind"], subtype=node["subtype"], title=node["title"],
                content=node["content"], status=node["status"], in_core=bool(node["in_core"]),
                created_at=float(node["created_at"]), score=score, coverage=coverage, mass=rel_mass,
                meta=node["meta"],
            ))
        hits.sort(key=lambda h: -h.score)
        if allow is not None:
            hits = [h for h in hits if allow(h)]

        # Drop near-duplicates so the budget is spent on distinct facts. Of two
        # memories that say almost the same thing the newer one is shown: it is
        # usually the correction ("25 seconds" became "15 seconds").
        kept: List[Hit] = []
        kept_sets: List[Any] = []
        for hit in hits:
            tokens = term_set(hit.content or hit.title)
            twin = next(
                (i for i, other in enumerate(kept)
                 if other.kind == hit.kind and jaccard(tokens, kept_sets[i]) >= 0.8),
                None,
            )
            if twin is not None:
                if hit.kind == "memory" and hit.created_at > kept[twin].created_at:
                    kept[twin], kept_sets[twin] = hit, tokens
                continue
            if len(kept) < limit:
                kept.append(hit)
                kept_sets.append(tokens)
        return kept

    # -- recall block -------------------------------------------------------

    def format_line(self, hit: Hit, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        when = _date(hit.created_at, now)
        # Every line starts with the item's id: it lets the model look deeper,
        # correct or retire exactly this item, and lets a transcript be read
        # back for what it was told (see ``told_ids``).
        if hit.kind == "memory":
            label = f"[{hit.subtype}] " if hit.subtype in _LABELLED else ""
            if hit.status == "evicted":
                label = f"[no longer in {_left_file(hit.meta)}, may be outdated] " + label
            return f"- #{hit.id} {label}{clip(hit.content, 260)} ({when})"
        if hit.kind == "session":
            # A pointer, not an excerpt: what was typed in an earlier
            # conversation is not replayed into this one unasked. The title
            # goes out without links, paths or addresses.
            title = "" if hit.meta.get("flagged") else clip(strip_refs(hit.title), 70)
            line = f'- #{hit.id} Past session "{title or "Session"}" ({when})'
            skills = [clip(str(s), 40) for s in (hit.meta.get("skills") or [])[:3] if s]
            if skills and not hit.meta.get("flagged"):
                line += f" [skills used: {', '.join(skills)}]"
            return line
        if hit.kind == "skill":
            about = f": {clip(hit.content, 110)}" if hit.content else ""
            return f"- #{hit.id} Skill {clip(hit.title, 80)}{about}"
        return f"- #{hit.id} {clip(hit.title or hit.content, 160)}"

    def recall(
        self,
        query: str,
        *,
        already: Optional[Set[int]] = None,
        now: Optional[float] = None,
        scan: Any = None,
        allow: Optional[Callable[[Hit], bool]] = None,
    ) -> Tuple[str, List[Hit]]:
        """The text to inject for this turn and the items it contains.

        ``scan`` (Hermes' memory scanner) screens every line on its way out,
        whatever its source and however it got into the bank; ``allow`` can
        veto single items (used to keep one chat's sessions out of another).
        """
        now = time.time() if now is None else now
        max_chars = int(self._opt("max_chars", 1100))
        max_items = int(self._opt("max_items", 6))
        caps = {
            "memory": max_items,
            "session": int(self._opt("max_sessions", 2)),
            "skill": int(self._opt("max_skills", 1)),
        }
        # Search on what the user wrote, not on attachment markers, paths or links.
        hits = self.search(query_text(query), limit=max_items * 3, gate=True, exclude=already, now=now, allow=allow)
        if not hits:
            return "", []
        # One question about one specific thing is not a request for every
        # memory that happens to share a rare word with it: when the best
        # saved memory matches almost everything asked, much weaker ones stay out.
        best = next((h for h in hits if h.kind == "memory"), None)
        if best is not None and best.coverage >= 0.8:
            floor = 0.45 * best.score
            hits = [h for h in hits if h.kind != "memory" or h.score >= floor]
        chosen: List[Tuple[Hit, str]] = []
        counts: Dict[str, int] = {}
        used = len(HEADER)
        for hit in hits:  # best first, so the budget goes to the strongest matches
            if len(chosen) >= max_items:
                break
            if counts.get(hit.kind, 0) >= caps.get(hit.kind, 0):
                continue
            if allow is not None and not allow(hit):
                continue
            line = fence_safe(self.format_line(hit, now))
            if trips(scan, line):
                continue
            room = max_chars - used - 1
            if len(line) > room:
                if chosen or room < 60:
                    continue
                line = fence_safe(clip(line, room))
            chosen.append((hit, line))
            counts[hit.kind] = counts.get(hit.kind, 0) + 1
            used += len(line) + 1
        if not chosen:
            return "", []
        # Shown as: curated memories, then past sessions, then skills.
        chosen.sort(key=lambda pair: (_KIND_ORDER.get(pair[0].kind, 9), -pair[0].score))
        return HEADER + "\n" + "\n".join(line for _, line in chosen), [hit for hit, _ in chosen]
