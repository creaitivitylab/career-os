"""Field-specific evidence ranking, independent of ingestion source priority."""
from dataclasses import dataclass

from .cleaning import clean_description, description_hash, description_quality
from .models import DescriptionAlternative, Evidence, Fact, Method, SourceInput, ValueState


PROVIDERS = {"fantastic_jobs_apify", "jooble_direct"}
DIRECT = {
    "smartrecruiters_direct", "greenhouse_direct", "workable_direct", "ashby_direct",
    "lever_direct", "workday_direct", "successfactors_direct",
}


def evidence_rank(field: str, evidence: Evidence) -> tuple[int, int]:
    method = evidence.extraction_method
    direct = evidence.source_name in DIRECT
    if method == Method.SUGGESTION:
        score = 0
    elif field in {"normalized_role", "career_level", "leadership_markers"}:
        score = (50 if method == Method.PARSER else 40) if direct else 30
    elif method == Method.NATIVE and direct:
        score = 50
    elif method == Method.PARSER and direct:
        score = 40
    elif method == Method.PROVIDER:
        score = 30
    else:
        score = 20
    # Explicit field-specific exception: matching native evidence must precede
    # validated provider addresses; title rules can precede broad native levels.
    if evidence.validation_state != "validated":
        score -= 10
    return (score, 1 if evidence.source_active is not False else 0)


def select_fact(field: str, candidates: list[Fact], collector) -> Fact:
    candidates = [c for c in candidates if c.value is not None]
    if not candidates:
        return Fact()
    def rank(candidate):
        return max(evidence_rank(field, collector.records[i]) for i in candidate.evidence_ids)
    candidates.sort(key=lambda c: (rank(c), str(c.value)), reverse=True)
    best = candidates[0]
    best_rank = rank(best)
    tied = [c for c in candidates if rank(c) == best_rank]
    conflict = any(c.value != best.value for c in tied)
    # Retain all alternatives even when lower-quality evidence loses selection.
    ids = list(dict.fromkeys(i for c in candidates for i in c.evidence_ids))
    return Fact(state=ValueState.CONFLICT if conflict else ValueState.KNOWN,
                value=None if conflict else best.value, evidence_ids=ids)


@dataclass
class DescriptionCandidate:
    source: SourceInput
    raw_text: str
    native_path: str


def select_description(candidates: list[DescriptionCandidate]):
    scored = []
    for candidate in candidates:
        text = clean_description(candidate.raw_text)
        quality, reason = description_quality(text, candidate.source.source_name)
        quality_rank = {"complete": 4, "short": 2, "snippet": 1, "template": 0, "empty": 0}[quality]
        native_rank = 2 if candidate.source.source_name in DIRECT else 1 if candidate.source.source_name == "fantastic_jobs_apify" else 0
        rank = (quality_rank, candidate.source.is_active is not False, native_rank, len(text),
                candidate.source.source_name, candidate.source.source_job_id)
        scored.append((rank, candidate, text, quality, reason))
    scored.sort(key=lambda item: item[0], reverse=True)
    usable = [s for s in scored if s[3] not in {"empty", "template"}]
    selected = usable[0] if usable else None
    alternatives = [DescriptionAlternative(
        source_name=c.source.source_name, source_job_id=c.source.source_job_id,
        quality=q, reason=r if selected and c is selected[1] else r + "; lower-ranked alternative",
        cleaned_hash=description_hash(text), selected=bool(selected and c is selected[1]),
    ) for _, c, text, q, r in scored]
    return selected, alternatives
