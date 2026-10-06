"""Pure offline projection. No HTTP, SQL, queue or ingestion side effects."""
from datetime import datetime, timezone
import re

from .cleaning import description_hash
from .evidence import EvidenceCollector, known_list, semantic_hash
from .models import (CareerContext, Compensation, Content, Employment, Fact, Identity, Evidence,
                     JobProfile, Metadata, Method, Requirements, Role, SourceInput,
                     Span, ValueState, Workplace, TechnologyMention, LanguageRequirement,
                     ExperienceConstraint, ContentSection, CompensationOffer)
from .parsers import (employment_values, experience, languages, salary, sections,
                      technologies, title_facts, workplace_mode)
from .projectors import project_source
from .selection import DIRECT, select_description, select_fact
from .versions import versions


def text_facts(text, source, collector):
    """A small set of explicit work-arrangement clauses, not generic mentions."""
    facts = {}
    pattern = r"(?:this (?:role|position|job) is|workplace(?: type)?\s*:|work arrangement\s*:)\s*(?:a\s+)?(?P<value>hybrid|remote|on[ -]?site)\b"
    for match in re.finditer(pattern, text, re.I):
        mode = workplace_mode(match.group("value"))
        if mode:
            facts.setdefault("workplace", []).append(collector.fact(source, "workplace", mode,
                span=Span(start=match.start(), end=match.end(), text=match.group()), method=Method.PARSER, input_value=text))
    pattern = r"(?:employment type|time type|schedule|contract type|typ úvazku)\s*:\s*([^\n.;]+)"
    for match in re.finditer(pattern, text, re.I):
        for field, value in employment_values(match.group(1)).items():
            facts.setdefault(field, []).append(collector.fact(source, field, value,
                span=Span(start=match.start(), end=match.end(), text=match.group()), method=Method.PARSER, input_value=text))
    return facts


def prepare_inputs(job: dict) -> dict:
    """Cheap native projection/cleanup/hash pass; no text extraction rules."""
    collector = EvidenceCollector()
    sources = [SourceInput.model_validate({k: s.get(k) for k in (
        "source_name", "source_job_id", "raw_payload", "source_url", "is_active", "last_seen_at"
    ) if s.get(k) is not None}) for s in job.get("sources", [])]
    sources.sort(key=lambda s: (s.source_name, s.source_job_id))
    projections = [project_source(source, collector) for source in sources]
    native_candidates = {}
    for projection in projections:
        for field, candidates in projection.facts.items():
            native_candidates.setdefault(field, []).extend(candidates)
    # Hash the projected semantic inputs, not whole raw_payload or its sessions,
    # lifecycle timestamps, provider contacts and arbitrary HTML attributes.
    native_records = [e for e in collector.records.values()]
    semantic_metadata_hash = semantic_hash(sorted([
        [e.source_name, e.source_job_id, e.field, e.value, e.native_field_path, e.extraction_method]
        for e in native_records
    ], key=lambda item: semantic_hash(item)))
    selected, alternatives = select_description([p.description for p in projections if p.description])
    cleaned = selected[2] if selected else ""
    source_inputs_hash = semantic_hash(sorted([
        [s.source_name, s.source_job_id, s.is_active,
         next((a.cleaned_hash for a in alternatives if a.source_name == s.source_name and a.source_job_id == s.source_job_id), None)]
        for s in sources
    ]))
    description_digest = description_hash(cleaned)
    version_metadata = versions()
    hashes = dict(semantic_metadata_hash=semantic_metadata_hash,
                  cleaned_description_hash=description_digest, source_inputs_hash=source_inputs_hash)
    hashes['input_hash'] = semantic_hash([semantic_metadata_hash, description_digest, source_inputs_hash, version_metadata])
    return dict(collector=collector, sources=sources, projections=projections,
                native_candidates=native_candidates, selected=selected, alternatives=alternatives,
                cleaned=cleaned, hashes=hashes, versions=version_metadata)


def input_fingerprint(job: dict) -> dict:
    prepared = prepare_inputs(job)
    return {**prepared['hashes'], 'versions': prepared['versions']}


def build_profile(job: dict, *, generated_at: datetime | None = None,
                  text_cache: dict | None = None, cache_out: dict | None = None) -> JobProfile:
    prepared = prepare_inputs(job)
    collector, sources = prepared['collector'], prepared['sources']
    projections, native_candidates = prepared['projections'], prepared['native_candidates']
    selected, alternatives, cleaned = prepared['selected'], prepared['alternatives'], prepared['cleaned']
    title_candidates = list(native_candidates.get("title", []))
    for projection in projections:
        for title in projection.facts.get("title", []):
            path = collector.records[title.evidence_ids[0]].native_field_path
            derived = title_facts(title.value, projection.source, collector, path=path)
            for field, value in derived.items():
                native_candidates.setdefault(field, []).extend(value if isinstance(value, list) else [value])
    quality = selected[3] if selected else "empty"
    desc_fact = Fact(state=ValueState.INSUFFICIENT)
    parsed = {"technologies": [], "languages": [], "experience": [], "sections": [], "compensation": []}
    if selected:
        source, path = selected[1].source, selected[1].native_path
        desc_fact = collector.fact(source, "cleaned_description", cleaned, path=path,
            method=Method.NATIVE if source.source_name in DIRECT else Method.PROVIDER, input_value=cleaned)
        # Selected source identity is part of this key: never reuse spans from a
        # different source merely because its description text happens to match.
        cache_key = semantic_hash([description_hash(cleaned), source.source_name, source.source_job_id,
            {k: prepared['versions'][k] for k in ('cleaner', 'parser', 'dictionary')}])
        models = dict(technologies=TechnologyMention, languages=LanguageRequirement,
                      experience=ExperienceConstraint, sections=ContentSection, compensation=CompensationOffer)
        if text_cache and text_cache.get('key') == cache_key:
            for item in text_cache['evidence']:
                record = Evidence.model_validate(item)
                record.source_active = source.is_active
                collector.records[record.id] = record
            parsed = {field: [models[field].model_validate(item) for item in items]
                      for field, items in text_cache['parsed'].items()}
            clauses = {field: [Fact.model_validate(item) for item in items]
                       for field, items in text_cache['clauses'].items()}
            layer = {**text_cache, 'evidence': [collector.records[item['id']].model_dump(mode='json')
                                               for item in text_cache['evidence']]}
        else:
            before = set(collector.records)
            for field, fn in [("technologies", technologies), ("languages", languages), ("experience", experience), ("sections", sections), ("compensation", salary)]:
                parsed[field] = fn(cleaned, source, collector)
            clauses = text_facts(cleaned, source, collector)
            layer = {'key': cache_key,
                'parsed': {field: [item.model_dump(mode='json') for item in items] for field, items in parsed.items()},
                'clauses': {field: [item.model_dump(mode='json') for item in items] for field, items in clauses.items()},
                'evidence': [record.model_dump(mode='json') for key, record in collector.records.items() if key not in before]}
        if cache_out is not None:
            cache_out.update(layer)
        for field, facts in clauses.items():
            native_candidates.setdefault(field, []).extend(facts)
    unavailable = ValueState.NOT_MENTIONED if quality == "complete" else ValueState.INSUFFICIENT
    def selected_fact(field):
        return select_fact(field, native_candidates.get(field, []), collector)
    def parsed_fact(field):
        values = parsed[field]
        if not values:
            return Fact(state=unavailable)
        return known_list(values, [i for value in values for i in value.evidence_ids])
    locations = [loc for p in projections for loc in p.locations]
    location_ids = [i for loc in locations for name in ("text", "country", "city", "region")
                    for i in getattr(loc, name).evidence_ids]
    # Retain source-specific locations instead of flattening foreign primary and
    # Czech secondary addresses. Contradictory top-tier primary countries flag a
    # conflict while leaving all location evidence available for review.
    primary_countries = {loc.country.value for p in projections if p.source.source_name in DIRECT
                         for loc in p.locations if loc.kind == "primary" and loc.country.value}
    location_fact = known_list(locations, location_ids) if locations else Fact()
    if len(primary_countries) > 1:
        location_fact.state = ValueState.CONFLICT
    offers = [known_list(p.offers, [i for offer in p.offers for i in offer.evidence_ids])
              for p in projections if p.offers]
    if parsed["compensation"]:
        offers.append(parsed_fact("compensation"))
    compensation = select_fact("compensation", offers, collector) if offers else Fact(state=unavailable)
    section_fact = parsed_fact("sections")
    def section_blocks(kinds):
        blocks = [section for section in parsed["sections"] if section.kind in kinds]
        return known_list([s.content for s in blocks], [i for s in blocks for i in s.evidence_ids]) if blocks else Fact(state=unavailable)
    semantic_metadata_hash = prepared['hashes']['semantic_metadata_hash']
    source_inputs_hash = prepared['hashes']['source_inputs_hash']
    description_digest = prepared['hashes']['cleaned_description_hash']
    version_metadata = prepared['versions']
    digest = prepared['hashes']['input_hash']
    identities = [Identity(source_name=p.source.source_name, source_job_id=p.source.source_job_id,
        native_id=select_fact("native_id", p.facts.get("native_id", []), collector),
        url=select_fact("url", p.facts.get("url", []), collector)) for p in projections]
    return JobProfile(
        metadata=Metadata(job_id=str(job["id"]), versions=version_metadata,
            semantic_metadata_hash=semantic_metadata_hash, cleaned_description_hash=description_digest,
            source_inputs_hash=source_inputs_hash, input_hash=digest,
            generated_at=generated_at or datetime.now(timezone.utc), identities=identities,
            title=select_fact("title", title_candidates, collector), company=selected_fact("company"),
            published_at=selected_fact("published_at"), expires_at=selected_fact("expires_at")),
        role=Role(original_title=select_fact("title", title_candidates, collector),
            normalized_role=selected_fact("normalized_role"), career_level=selected_fact("career_level"),
            leadership_markers=selected_fact("leadership_markers"), native_level=selected_fact("native_level")),
        workplace=Workplace(locations=location_fact, mode=selected_fact("workplace")),
        employment=Employment(schedule=selected_fact("schedule"), relationship=selected_fact("relationship"), duration=selected_fact("duration")),
        compensation=Compensation(offers=compensation),
        requirements=Requirements(technologies=parsed_fact("technologies"), languages=parsed_fact("languages"),
            experience=parsed_fact("experience"), education=selected_fact("education")),
        content=Content(cleaned_description=desc_fact, selected_source_name=selected[1].source.source_name if selected else None,
            selected_source_job_id=selected[1].source.source_job_id if selected else None, description_quality=quality,
            alternatives=alternatives, sections=section_fact, responsibilities=section_blocks({"responsibilities"}),
            requirements=section_blocks({"requirements", "qualifications"}), benefits=section_blocks({"benefits"})),
        career_context=CareerContext(department=selected_fact("department"), team=selected_fact("team"),
            employer_industry=selected_fact("employer_industry"), travel=selected_fact("travel")),
        evidence=sorted(collector.records.values(), key=lambda e: e.id),
    )


def reprocessing_layers(previous: Metadata | None, current: Metadata) -> set[str]:
    """Planning contract for a future asynchronous worker; no queue execution."""
    if previous is None:
        return {"native", "text", "selection", "profile"}
    if previous.input_hash == current.input_hash:
        return set()
    layers = {"selection", "profile"}
    if (previous.semantic_metadata_hash != current.semantic_metadata_hash
            or previous.versions.get("projector") != current.versions.get("projector")
            or previous.versions.get("parser") != current.versions.get("parser")):
        layers.add("native")
    if (previous.cleaned_description_hash != current.cleaned_description_hash
            or any(previous.versions.get(k) != current.versions.get(k) for k in ("cleaner", "parser", "dictionary"))):
        layers.add("text")
    return layers
