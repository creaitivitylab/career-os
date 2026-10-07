"""Source-agnostic domain contract. Unknown is a state, not an empty list."""
from datetime import datetime
from decimal import Decimal
from enum import Enum
import re
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValueState(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    NOT_MENTIONED = "not_mentioned"
    CONFLICT = "conflict"
    INSUFFICIENT = "insufficient_content"


class Method(str, Enum):
    NATIVE = "native_structured"
    PARSER = "deterministic_parser"
    PROVIDER = "provider_structured"
    SUGGESTION = "provider_suggestion"


T = TypeVar("T")


class Fact(Model, Generic[T]):
    state: ValueState = ValueState.UNKNOWN
    value: T | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_state(self):
        if self.state == ValueState.KNOWN:
            if self.value is None or self.value == [] or self.value == "":
                raise ValueError("Known facts require a nonempty value")
            if not self.evidence_ids:
                raise ValueError("Known facts require evidence")
        elif self.state not in (ValueState.CONFLICT,) and self.value is not None:
            raise ValueError("Unavailable facts cannot carry a value")
        return self


class Span(Model):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("Invalid evidence span")
        return self


class Evidence(Model):
    id: str
    field: str
    value: Any  # Assertions accompany strongly typed domain fields below.
    raw_value: Any = None  # Original geography wording; never a whole source payload.
    source_name: str
    source_job_id: str
    extraction_method: Method
    native_field_path: str | None = None
    text_span: Span | None = None
    observed_at: datetime | None = None
    input_hash: str
    explicitness: Literal["explicit", "inferred", "unknown"] = "explicit"
    validation_state: Literal["validated", "unvalidated", "rejected"] = "validated"
    confidence_band: Literal["high", "medium", "low"] = "high"
    source_active: bool | None = None

    @model_validator(mode="after")
    def has_origin(self):
        if self.native_field_path is None and self.text_span is None:
            raise ValueError("Evidence requires a field path or text span")
        return self


class SourceInput(Model):
    source_name: str
    source_job_id: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    source_url: str | None = None
    is_active: bool | None = None
    last_seen_at: datetime | None = None


class Location(Model):
    kind: Literal["primary", "additional", "unspecified"] = "unspecified"
    text: Fact[str] = Field(default_factory=Fact[str])
    city: Fact[str] = Field(default_factory=Fact[str])
    region: Fact[str] = Field(default_factory=Fact[str])
    country: Fact[str] = Field(default_factory=Fact[str])
    country_name: Fact[str] = Field(default_factory=Fact[str])

    @model_validator(mode="after")
    def iso_country(self):
        if self.country.value and not re.fullmatch(r"[A-Z]{2}", self.country.value):
            raise ValueError("Normalized country must be ISO alpha-2")
        return self


class RawLocationLabel(Model):
    """Useful source input that has not established a structured place."""
    raw_value: str
    field_semantics: Literal["label", "country", "city", "region"]
    kind: Literal["primary", "additional", "unspecified"]
    reason: str
    evidence_ids: list[str] = Field(min_length=1)


class CompensationOffer(Model):
    min_amount: Decimal | None = Field(default=None, gt=0)
    max_amount: Decimal | None = Field(default=None, gt=0)
    currency: str | None = None
    period: Literal["hour", "day", "month", "year", "task", "one_time", "unknown"] = "unknown"
    gross_net_status: Literal["gross", "net", "unknown"] = "unknown"
    component: Literal["base", "bonus", "equity", "task_reward", "other", "unknown"] = "unknown"
    applicable_locations: list[str] | None = None
    original_location_labels: list[str] | None = None
    original_text: str | None = None
    explicitness: Literal["explicit", "inferred", "unknown"] = "explicit"
    evidence_ids: list[str]

    @model_validator(mode="after")
    def valid_offer(self):
        if self.min_amount is None and self.max_amount is None:
            raise ValueError("An offer needs at least one amount")
        if self.min_amount is not None and self.max_amount is not None and self.min_amount > self.max_amount:
            raise ValueError("Inverted compensation range")
        if not self.evidence_ids:
            raise ValueError("An offer requires evidence")
        return self


class TechnologyMention(Model):
    technology: str
    matched_text: str
    evidence_ids: list[str]
    kind: Literal["mention"] = "mention"


class LanguageRequirement(Model):
    language: str
    requirement: Literal["required", "preferred", "unknown"] = "unknown"
    cefr: Literal["A1", "A2", "B1", "B2", "C1", "C2"] | None = None
    cefr_or_higher: bool | None = None
    cefr_comparator: Literal["at_least", "exact", "unknown"] | None = None
    proficiency_wording: str
    evidence_ids: list[str]


class ExperienceConstraint(Model):
    min_years: Decimal = Field(ge=0, le=60)
    max_years: Decimal | None = Field(default=None, ge=0, le=60)
    original_wording: str
    evidence_ids: list[str]

    @model_validator(mode="after")
    def valid_range(self):
        if self.max_years is not None and self.max_years < self.min_years:
            raise ValueError("Inverted experience range")
        return self


class ContentSection(Model):
    kind: Literal["responsibilities", "requirements", "qualifications", "benefits", "other"]
    heading: str
    content: str
    start: int
    end: int
    evidence_ids: list[str]


class DescriptionAlternative(Model):
    source_name: str
    source_job_id: str
    quality: Literal["complete", "short", "snippet", "empty", "template"]
    reason: str
    cleaned_hash: str
    selected: bool = False


class Identity(Model):
    source_name: str
    source_job_id: str
    native_id: Fact[str] = Field(default_factory=Fact[str])
    url: Fact[str] = Field(default_factory=Fact[str])


class Metadata(Model):
    job_id: str
    versions: dict[str, str]
    semantic_metadata_hash: str
    cleaned_description_hash: str
    input_hash: str
    source_inputs_hash: str
    processing_state: Literal["pending", "processing", "completed", "failed", "stale"] = "completed"
    generated_at: datetime
    identities: list[Identity]
    title: Fact[str] = Field(default_factory=Fact[str])
    company: Fact[str] = Field(default_factory=Fact[str])
    published_at: Fact[str] = Field(default_factory=Fact[str])
    expires_at: Fact[str] = Field(default_factory=Fact[str])
    opportunity_type: Fact[Literal["vacancy", "talent_pool", "internship_program", "event", "hackathon", "unknown"]] = Field(default_factory=Fact)
    is_normal_vacancy: Fact[bool] = Field(default_factory=Fact[bool])


class Role(Model):
    original_title: Fact[str] = Field(default_factory=Fact[str])
    normalized_role: Fact[str] = Field(default_factory=Fact[str])
    job_family: Fact[str] = Field(default_factory=Fact[str])
    specialization: Fact[str] = Field(default_factory=Fact[str])
    career_level: Fact[Literal["entry", "junior", "mid", "senior", "staff", "principal"]] = Field(default_factory=Fact)
    management_track: Fact[Literal["individual_contributor", "technical_lead", "people_manager"]] = Field(default_factory=Fact)
    leadership_markers: Fact[list[str]] = Field(default_factory=Fact)
    people_management: Fact[bool] = Field(default_factory=Fact[bool])
    native_level: Fact[str] = Field(default_factory=Fact[str])


class Workplace(Model):
    locations: Fact[list[Location]] = Field(default_factory=Fact)
    raw_location_labels: Fact[list[RawLocationLabel]] = Field(default_factory=Fact)
    mode: Fact[Literal["onsite", "hybrid", "remote", "unknown"]] = Field(default_factory=Fact)


class Employment(Model):
    schedule: Fact[Literal["full_time", "part_time", "shift", "unknown"]] = Field(default_factory=Fact)
    relationship: Fact[Literal["employee", "contractor", "intern", "temporary", "unknown"]] = Field(default_factory=Fact)
    duration: Fact[Literal["permanent", "fixed_term", "unknown"]] = Field(default_factory=Fact)


class Compensation(Model):
    offers: Fact[list[CompensationOffer]] = Field(default_factory=Fact)
    # Text-only benefit evidence, deliberately separate from salary offers.
    monetary_benefits: Fact[list[str]] = Field(default_factory=Fact)


class Requirements(Model):
    required_skills: Fact[list[str]] = Field(default_factory=Fact)
    preferred_skills: Fact[list[str]] = Field(default_factory=Fact)
    technologies: Fact[list[TechnologyMention]] = Field(default_factory=Fact)
    languages: Fact[list[LanguageRequirement]] = Field(default_factory=Fact)
    experience: Fact[list[ExperienceConstraint]] = Field(default_factory=Fact)
    education: Fact[list[str]] = Field(default_factory=Fact)
    certifications: Fact[list[str]] = Field(default_factory=Fact)


class Content(Model):
    cleaned_description: Fact[str] = Field(default_factory=Fact[str])
    selected_source_name: str | None = None
    selected_source_job_id: str | None = None
    description_quality: str
    alternatives: list[DescriptionAlternative]
    sections: Fact[list[ContentSection]] = Field(default_factory=Fact)
    responsibilities: Fact[list[str]] = Field(default_factory=Fact)
    requirements: Fact[list[str]] = Field(default_factory=Fact)
    benefits: Fact[list[str]] = Field(default_factory=Fact)


class CareerContext(Model):
    department: Fact[str] = Field(default_factory=Fact[str])
    team: Fact[str] = Field(default_factory=Fact[str])
    employer_industry: Fact[str] = Field(default_factory=Fact[str])
    role_domain: Fact[str] = Field(default_factory=Fact[str])
    travel: Fact[str] = Field(default_factory=Fact[str])
    shift_work: Fact[str] = Field(default_factory=Fact[str])


class JobProfile(Model):
    metadata: Metadata
    role: Role
    workplace: Workplace
    employment: Employment
    compensation: Compensation
    requirements: Requirements
    content: Content
    career_context: CareerContext
    evidence: list[Evidence]

    @model_validator(mode="after")
    def references_exist(self):
        ids = {e.id for e in self.evidence}
        if len(ids) != len(self.evidence):
            raise ValueError("Duplicate evidence IDs")
        def walk(value):
            if isinstance(value, dict):
                if "evidence_ids" in value and not set(value["evidence_ids"]).issubset(ids):
                    raise ValueError("Dangling evidence reference")
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(self.model_dump())
        return self
