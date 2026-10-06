import hashlib
import json
from typing import Any

from .models import Evidence, Fact, Method, SourceInput, Span, ValueState


def semantic_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class EvidenceCollector:
    def __init__(self):
        self.records: dict[str, Evidence] = {}

    def add(self, source: SourceInput, field: str, value: Any, *, path=None,
            span: Span | None = None, method=Method.NATIVE, input_value=None,
            explicitness="explicit", confidence="high", validation="validated", raw_value=None) -> str:
        input_hash = semantic_hash(input_value if input_value is not None else value)
        identity = semantic_hash([source.source_name, source.source_job_id, field, value,
                                  path, span.model_dump() if span else None, method, input_hash])[:24]
        self.records[identity] = Evidence(
            id=identity, field=field, value=value, raw_value=raw_value, source_name=source.source_name,
            source_job_id=source.source_job_id, extraction_method=method,
            native_field_path=path, text_span=span, observed_at=source.last_seen_at,
            input_hash=input_hash, explicitness=explicitness, validation_state=validation,
            confidence_band=confidence, source_active=source.is_active,
        )
        return identity

    def fact(self, source, field, value, **kwargs):
        return Fact(state=ValueState.KNOWN, value=value,
                    evidence_ids=[self.add(source, field, value, **kwargs)])


def known_list(values, evidence_ids):
    return Fact(state=ValueState.KNOWN, value=values, evidence_ids=list(dict.fromkeys(evidence_ids)))
