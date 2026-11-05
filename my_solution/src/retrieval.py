"""Index approved policy sources, retrieve evidence, and enforce citations.

Evidence rule (no-answer rule)
------------------------------
A passage counts as *accepted evidence* only if it matches the query well enough:
  * at least MIN_MATCHED_TERMS distinct query terms appear in the passage, and
  * those terms cover at least MIN_COVERAGE of the query's terms.
Ranking among accepted passages uses BM25. Passages below the rule are never
returned as evidence (only listed as near misses for the trace), so the agent
cannot answer from the "nearest weak match". If a request yields no accepted
evidence, the agent must abstain (needs_info / refused) or hand off (escalate).

Citation rule
-------------
Answers cite source IDs in square brackets, e.g. "[refunds]". A citation is valid
only if that source was returned as accepted evidence earlier in the same run.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

STEMMER_VERSION = 2   # bump when stem() changes: retrieval results change with it
MIN_MATCHED_TERMS = 2
MIN_COVERAGE = 0.6
BM25_K1, BM25_B = 1.5, 0.75

CITATION = re.compile(r"\[([a-z0-9][a-z0-9_-]*)\]")
_WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_STOP = set("""a an and are as at be by can could do does for from had has have how i if in is it
its me my no not of on or our please should so than that the their them then there these they this
to was we were what when where which who why will with would you your can't i'm""".split())


def stem(w: str) -> str:
    """Light English suffix stripping. Plural and singular map to the same stem
    (address/addresses, policy/policies, lockout/lockouts), and words ending in
    -ss/-us/-is keep their final s (access, status, analysis)."""
    if len(w) <= 4:
        return w
    if w.endswith("ies") and len(w) > 5:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "zes", "ches", "shes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    if w.endswith("ion") and len(w) - 3 >= 4:  # deletion -> delet, escalation -> escalat
        return w[:-3]
    if w.endswith("ied") and len(w) > 5:
        return w[:-3] + "y"
    for suf in ("ing", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[: -len(suf)]
            break
    if w.endswith("e") and len(w) > 4:  # charge/charged/charging -> charg
        w = w[:-1]
    return w


def terms(text: str) -> list[str]:
    return [stem(w) for w in _WORD.findall(text.lower()) if len(w) > 1 and w not in _STOP]


def extract_citations(text: str) -> list[str]:
    return list(dict.fromkeys(CITATION.findall(text)))  # unique, in order


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------
@dataclass
class Passage:
    evidence_id: str        # "<source_id>#<n>"
    source_id: str
    title: str
    text: str
    tf: Counter = field(default_factory=Counter)
    length: int = 0


class Evidence(BaseModel):
    evidence_id: str
    source_id: str
    title: str
    text: str
    score: float
    coverage: float
    matched_terms: list[str]


class NearMiss(BaseModel):
    source_id: str
    coverage: float
    matched_terms: list[str]


class PolicyIndex:
    def __init__(self, passages: list[Passage]):
        self.passages = passages
        self.source_ids = {p.source_id for p in passages}
        self.avg_len = sum(p.length for p in passages) / max(len(passages), 1)
        df = Counter(t for p in passages for t in p.tf)
        n = len(passages)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    @classmethod
    def from_dir(cls, kb_dir: str | Path) -> "PolicyIndex":
        """Index every *.md with a 'Source ID:' line. A file with a 'Status:' line is
        indexed only if the status is 'approved' (files without one count as approved)."""
        passages = []
        for f in sorted(Path(kb_dir).glob("*.md")):
            raw = f.read_text(encoding="utf-8")
            sid = re.search(r"^Source ID:\s*(\S+)\s*$", raw, re.M)
            status = re.search(r"^Status:\s*(\S+)", raw, re.M | re.I)
            if not sid or (status and status.group(1).lower() != "approved"):
                continue
            lines = [ln for ln in raw.splitlines()
                     if not re.match(r"^(Source ID|Status):", ln, re.I)]
            title = next((ln.strip().lstrip("#").strip() for ln in lines if ln.strip()), sid.group(1))
            body = "\n".join(lines).strip().split("\n", 1)[-1]
            for i, para in enumerate(p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()):
                toks = terms(title + " " + para)
                passages.append(Passage(f"{sid.group(1)}#{i}", sid.group(1), title,
                                        " ".join(para.split()), Counter(toks), len(toks)))
        return cls(passages)

    def search(self, query: str, top_k: int = 3, min_matched: int | None = None,
               min_coverage: float | None = None) -> tuple[list[Evidence], list[NearMiss]]:
        """min_matched/min_coverage override the module thresholds (used for sweeps)."""
        min_matched = MIN_MATCHED_TERMS if min_matched is None else min_matched
        min_coverage = MIN_COVERAGE if min_coverage is None else min_coverage
        q = set(terms(query))
        if not q:
            return [], []
        accepted, near = [], {}
        for p in self.passages:
            matched = sorted(q & p.tf.keys())
            if not matched:
                continue
            coverage = len(matched) / len(q)
            if len(matched) >= min_matched and coverage >= min_coverage:
                score = sum(self.idf[t] * p.tf[t] * (BM25_K1 + 1) /
                            (p.tf[t] + BM25_K1 * (1 - BM25_B + BM25_B * p.length / self.avg_len))
                            for t in matched)
                accepted.append(Evidence(evidence_id=p.evidence_id, source_id=p.source_id,
                                         title=p.title, text=p.text, score=round(score, 3),
                                         coverage=round(coverage, 2), matched_terms=matched))
            elif coverage > near.get(p.source_id, NearMiss(source_id="", coverage=-1, matched_terms=[])).coverage:
                near[p.source_id] = NearMiss(source_id=p.source_id, coverage=round(coverage, 2),
                                             matched_terms=matched)
        accepted.sort(key=lambda e: -e.score)
        accepted_sources = {e.source_id for e in accepted}
        misses = sorted((m for s, m in near.items() if s not in accepted_sources),
                        key=lambda m: -m.coverage)
        return accepted[:top_k], misses[:3]


# --------------------------------------------------------------------------
# Citation check
# --------------------------------------------------------------------------
def citation_problems(text: str, accepted_sources: set[str], require_citation: bool) -> list[str]:
    cited = extract_citations(text)
    problems = [f"citation [{c}] is not in accepted evidence for this request"
                for c in cited if c not in accepted_sources]
    if require_citation and not cited:
        problems.append("answer must cite at least one accepted source, e.g. [refunds]")
    return problems
