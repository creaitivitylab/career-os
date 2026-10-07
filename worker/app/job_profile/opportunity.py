"""Narrow recruiting-record signals, not a general vacancy classifier."""
import re

from .models import Fact, Method, Span
from .selection import select_fact


def title_candidates(title, source, path, collector):
    values = []
    patterns = {
        'talent_pool': r'\b(?:talent pool|talent community|open application|spontaneous application)\b',
        'hackathon': r'\bhackathon\b(?:\s+\d{4}\b)|^hackathon$',
        'event': r'\b(?:career fair|recruiting event|recruitment event|hiring event)\b',
        'internship_program': r'\b(?:internship|graduate|trainee) program(?:me)?\b',
    }
    # A person running an event/program is an ordinary role, not the event.
    organizer = re.search(r'\b(?:manager|organizer|coordinator|director|engineer|developer|head|lead|producer|specialist)\b', title, re.I)
    for kind, pattern in patterns.items():
        if organizer and kind != 'talent_pool':
            continue
        match = re.search(pattern, title, re.I)
        if match:
            values.append(collector.fact(source, 'opportunity_type', kind, path=path,
                span=Span(start=match.start(), end=match.end(), text=match.group()),
                method=Method.PARSER, input_value=title))
    return values


def description_candidates(description, source, collector):
    # A CV invitation alone is insufficient. Require an explicit statement that
    # no matching vacancy is available in the same passage.
    pattern = (r'[^\n.!?]*(?:nemáme zrovna volnou pozici|no (?:current|suitable|matching) (?:vacanc(?:y|ies)|openings?))'
               r'[^\n.!?]*[.!?]?\s*[^\n.!?]*(?:pošlete nám svůj životopis|send us your (?:cv|resume))[^\n.!?]*')
    match = re.search(pattern, description, re.I)
    kind = 'talent_pool'
    if not match:
        # Explicit recruiting clause only. Generic join-our-team invitations,
        # unmarked titles and employer marketing alone remain unknown.
        match = re.search(r'(?:^|\n)[^\n]*(?:we are (?:hiring|seeking|looking for) (?:a|an)\s+[^\n.]{3,100}|hledáme[^\n.]*na pozici[^\n.]+)', description, re.I)
        kind = 'vacancy'
        if match and re.search(r'\b(?:team|community|partners?|customers?|volunteers?|participants?|sponsors?)\b', match.group(), re.I):
            match = None
    if not match:
        return []
    return [collector.fact(source, 'opportunity_type', kind,
        span=Span(start=match.start(), end=match.end(), text=match.group()),
        method=Method.PARSER, input_value=description)]


def resolve_opportunity(candidates, collector):
    # Explicit non-standard-record evidence takes precedence over generic role
    # recruiting clauses; contradictory non-standard signals still conflict.
    nonstandard = [c for c in candidates if c.value != 'vacancy']
    kind = select_fact('opportunity_type', nonstandard or candidates, collector)
    normal = Fact(state=kind.state, value=(kind.value == 'vacancy') if kind.value else None,
                  evidence_ids=kind.evidence_ids)
    return kind, normal
