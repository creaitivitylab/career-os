"""Versioned independent labels and selective deterministic-field metrics."""
import argparse
import csv
from datetime import datetime
from decimal import Decimal
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import JobProfile, Model, ValueState, Fact


LABEL_VERSION = 'job-profile-human-v1.2'
FIELDS = ('normalized_role', 'job_family', 'career_level', 'workplace', 'employment_schedule',
          'relationship', 'compensation', 'technologies', 'languages', 'experience',
          'opportunity_type', 'is_normal_vacancy', 'people_management', 'city', 'country',
          'duration', 'base_compensation', 'other_compensation_benefits', 'required_technologies',
          'required_skills', 'preferred_skills', 'base_compensation_present')
COLLECTIONS = {'compensation', 'technologies', 'languages', 'experience', 'city', 'country',
               'base_compensation', 'other_compensation_benefits', 'required_technologies', 'required_skills', 'preferred_skills'}
LEGACY_FIELDS = FIELDS[:10]
V11_FIELDS = FIELDS[:19]


class LabelLanguage(Model):
    language: str = Field(pattern=r'^[a-z]{2}$')
    requirement: Literal['required','preferred','unknown'] = 'unknown'
    cefr: Literal['A1','A2','B1','B2','C1','C2'] | None = None
    cefr_or_higher: bool | None = None
    cefr_comparator: Literal['at_least','at_most','range','exact','unknown'] | None = None
    cefr_max: Literal['A1','A2','B1','B2','C1','C2'] | None = None
    original_wording: str | None = None
    cefr_state: Literal['known','unknown','not_mentioned','ambiguous'] = 'unknown'

    @model_validator(mode='after')
    def consistent_bound(self):
        if self.cefr is None and (self.cefr_comparator in {'at_least','exact'} or self.cefr_or_higher is True):
            raise ValueError('A CEFR comparator needs an explicit CEFR level')
        if self.cefr_comparator == 'at_least' and self.cefr_or_higher is False or self.cefr_comparator in {'exact','unknown'} and self.cefr_or_higher is True:
            raise ValueError('Contradictory CEFR comparator/legacy bound')
        if self.cefr_comparator in {'at_most','range'} and self.cefr is None:
            raise ValueError('CEFR bounds require a level')
        if self.cefr_comparator == 'range':
            levels = ['A1','A2','B1','B2','C1','C2']
            if not self.cefr_max or levels.index(self.cefr_max) < levels.index(self.cefr):
                raise ValueError('CEFR range needs an ordered upper bound')
        elif self.cefr_max is not None:
            raise ValueError('Upper CEFR belongs only to a range')
        if self.cefr is not None:
            if self.cefr_state in {'not_mentioned','ambiguous'}:
                raise ValueError('Unavailable CEFR cannot contain a level')
            self.cefr_state = 'known'
        elif self.cefr_state == 'known':
            raise ValueError('Known CEFR needs a level')
        return self


class LabelExperience(Model):
    min_years: Decimal = Field(ge=0, le=60)
    max_years: Decimal | None = Field(default=None, ge=0, le=60)

    @model_validator(mode='after')
    def ordered(self):
        if self.max_years is not None and self.max_years < self.min_years:
            raise ValueError('Inverted human experience range')
        return self


class LabelCompensation(Model):
    min_amount: Decimal | None = Field(default=None, gt=0)
    max_amount: Decimal | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    period: Literal['hour','day','month','year','task','one_time','unknown'] = 'unknown'
    gross_net_status: Literal['gross','net','unknown'] = 'unknown'
    component: Literal['base','bonus','equity','task_reward','other','unknown'] = 'unknown'
    applicable_locations: list[str] | None = None

    @model_validator(mode='after')
    def ordered(self):
        if self.min_amount is None and self.max_amount is None:
            raise ValueError('Human offer needs an amount')
        if self.min_amount is not None and self.max_amount is not None and self.max_amount < self.min_amount:
            raise ValueError('Inverted human offer range')
        return self


class LabelItemDetail(Model):
    normalized_value: str
    original_wording: str | None = None
    requirement: Literal['required','preferred','unknown'] = 'unknown'


class HumanLabel(Model):
    state: Literal['unlabeled', 'known', 'not_mentioned', 'unknown', 'conflict', 'ambiguous'] = 'unlabeled'
    value: Any = None
    notes: str | None = None
    evidence_text: str | None = None
    item_details: list[LabelItemDetail] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_state(self):
        if self.state == 'known' and (self.value is None or self.value == [] or self.value == ''):
            raise ValueError('Known human labels require a value; use not_mentioned for absence')
        if self.state != 'known' and self.value is not None:
            raise ValueError('Unavailable labels cannot carry values')
        return self


class HumanReview(Model):
    label_schema_version: Literal['job-profile-human-v1.0','job-profile-human-v1.1','job-profile-human-v1.2'] = LABEL_VERSION
    job_id: str
    profile_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_snapshot: str
    reviewer_id: str | None = None
    reviewed_at: datetime | None = None
    reviewer_notes: str | None = None
    review_complete: bool = False
    frozen_versions: dict[str, str] = Field(default_factory=dict)
    cohort_hash: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    labels: dict[str, HumanLabel] = Field(default_factory=lambda: {field: HumanLabel() for field in FIELDS})

    @model_validator(mode='before')
    @classmethod
    def legacy_labels(cls, data):
        if isinstance(data, dict):
            previous = {'job-profile-human-v1.0': LEGACY_FIELDS, 'job-profile-human-v1.1': V11_FIELDS}.get(data.get('label_schema_version'))
            if previous and set(data.get('labels', {})) == set(previous):
                data = {**data, 'labels': {**data['labels'], **{field: {} for field in FIELDS if field not in previous}}}
        return data

    @model_validator(mode='after')
    def validate_labels(self):
        if set(self.labels) != set(FIELDS):
            raise ValueError('All reviewed fields must be explicit; unexpected label field')
        if (self.review_complete or any(label.state != 'unlabeled' for label in self.labels.values())) and (not self.reviewer_id or not self.reviewed_at):
            raise ValueError('Reviewed labels require reviewer identity and timestamp')
        enums = {
            'career_level': {'entry','junior','mid','senior','staff','principal'},
            'workplace': {'onsite','hybrid','remote'},
            'employment_schedule': {'full_time','part_time','shift'},
            'relationship': {'employee','contractor','intern','temporary'},
            'duration': {'permanent','fixed_term'},
            'opportunity_type': {'vacancy','talent_pool','internship_program','event','hackathon','other'},
        }
        for field, label in self.labels.items():
            if label.state != 'known':
                continue
            if field in COLLECTIONS:
                if not isinstance(label.value, list):
                    raise ValueError('Collection labels must contain a list')
                if field in {'technologies','required_technologies','required_skills','preferred_skills','city','country'} and not all(isinstance(x, str) and x for x in label.value):
                    raise ValueError('Name collections must contain nonempty normalized strings')
                if field == 'country' and any(not re.fullmatch(r'[A-Z]{2}', x) for x in label.value):
                    raise ValueError('Country labels use ISO alpha-2')
                if field == 'other_compensation_benefits':
                    if not all(isinstance(x, str) and x or isinstance(x, dict) and x for x in label.value):
                        raise ValueError('Benefits require literal text or separate offer objects')
                    label.value = [LabelCompensation.model_validate(x).model_dump(mode='json') if isinstance(x, dict) else x for x in label.value]
                if field in {'compensation','base_compensation','languages','experience'} and not all(isinstance(x, dict) and x for x in label.value):
                    raise ValueError('Structured labels must contain objects')
                shape = {'languages':LabelLanguage, 'experience':LabelExperience,
                         'compensation':LabelCompensation, 'base_compensation':LabelCompensation}.get(field)
                if shape:
                    label.value = [shape.model_validate(item).model_dump(mode='json') for item in label.value]
            elif field in {'is_normal_vacancy','people_management','base_compensation_present'}:
                if not isinstance(label.value, bool):
                    raise ValueError('Boolean labels require true/false')
            elif not isinstance(label.value, str) or field in enums and label.value not in enums[field]:
                raise ValueError('Invalid categorical human label')
        return self


def base_present(profile):
    offers = prediction_fact(profile, 'base_compensation')
    return Fact(state='known', value=True, evidence_ids=offers.evidence_ids) if offers.state == ValueState.KNOWN else Fact()


def prediction_fact(profile, field):
    if field in {'city', 'country'}:
        locations = profile.workplace.locations
        facts = [getattr(loc, field) for loc in locations.value or [] if getattr(loc, field).value]
        if locations.state == ValueState.CONFLICT:
            return Fact(state=ValueState.CONFLICT, evidence_ids=locations.evidence_ids)
        return Fact(state='known', value=sorted(set(f.value for f in facts)), evidence_ids=list(dict.fromkeys(i for f in facts for i in f.evidence_ids))) if facts else Fact()
    if field in {'base_compensation','other_compensation_benefits'}:
        offers = profile.compensation.offers
        if offers.state == ValueState.CONFLICT:
            return Fact(state=ValueState.CONFLICT, evidence_ids=offers.evidence_ids)
        values = [o for o in offers.value or [] if (o.component == 'base' if field == 'base_compensation' else o.component in {'bonus','equity','task_reward','other'})]
        ids = [i for o in values for i in o.evidence_ids]
        if field == 'other_compensation_benefits':
            benefits = profile.compensation.monetary_benefits
            values += benefits.value or []
            ids += benefits.evidence_ids
        return Fact(state='known', value=values, evidence_ids=ids) if values else Fact()
    mapping = {
        'opportunity_type': profile.metadata.opportunity_type, 'is_normal_vacancy': profile.metadata.is_normal_vacancy,
        'people_management': profile.role.people_management, 'duration': profile.employment.duration,
        'required_technologies': Fact(),
        'required_skills': profile.requirements.required_skills, 'preferred_skills': profile.requirements.preferred_skills,
        'base_compensation_present': base_present(profile),
        'normalized_role': profile.role.normalized_role, 'job_family': profile.role.job_family,
        'career_level': profile.role.career_level, 'workplace': profile.workplace.mode,
        'employment_schedule': profile.employment.schedule, 'relationship': profile.employment.relationship,
        'compensation': profile.compensation.offers, 'technologies': profile.requirements.technologies,
        'languages': profile.requirements.languages, 'experience': profile.requirements.experience,
    }
    return mapping[field]


def tokens(field, value):
    if value is None:
        return set()
    items = value if field in COLLECTIONS else [value]
    result = set()
    for item in items:
        if hasattr(item, 'model_dump'):
            item = item.model_dump(mode='json')
        if field == 'technologies' and isinstance(item, dict):
            item = item['technology']
        elif field == 'languages':
            next_item_max = item.get('cefr_max')
            comparator = item.get('cefr_comparator') or ('at_least' if item.get('cefr_or_higher') else 'unknown' if item.get('cefr') else None)
            item = {k: item.get(k) for k in ('language','requirement','cefr','cefr_or_higher')}
            item['cefr_comparator'] = comparator
            item['cefr_or_higher'] = (comparator == 'at_least') if item['cefr'] else None
            if comparator == 'range':
                item['cefr_max'] = next_item_max

        elif field == 'experience':
            item = {k: str(Decimal(str(item[k])).normalize()) if item.get(k) is not None else None for k in ('min_years','max_years')}
        elif field in {'compensation','base_compensation','other_compensation_benefits'} and isinstance(item, dict):
            item = {k: item.get(k) for k in ('min_amount','max_amount','currency','period','gross_net_status','component','applicable_locations')}
            for key in ('min_amount','max_amount'):
                if item[key] is not None:
                    item[key] = str(Decimal(str(item[key])).normalize())
            if item['applicable_locations']:
                item['applicable_locations'] = sorted(set(item['applicable_locations']))
        result.add(json.dumps(item, sort_keys=True, ensure_ascii=False))
    return result


def evaluate_labels(profiles, labels):
    rows = list(labels)
    if len({row.job_id for row in rows}) != len(rows):
        raise ValueError('Duplicate human reviews; adjudicate rather than overwrite')
    result = {}
    for field in FIELDS:
        counts = dict(labeled=0, emitted=0, abstained=0, conflicts=0, true_positive=0, false_positive=0, false_negative=0)
        for review in rows:
            label = review.labels[field]
            if label.state not in {'known','not_mentioned'}:
                continue
            profile = profiles[review.job_id]
            if profile.metadata.input_hash != review.profile_input_hash:
                raise ValueError('Human label refers to a different prediction input version')
            fact = prediction_fact(profile, field)
            counts['labeled'] += 1
            gold = tokens(field, label.value)
            prediction = tokens(field, fact.value) if fact.state == ValueState.KNOWN else set()
            # not_mentioned is an explicit absence decision, not abstention.
            abstention = fact.state not in {ValueState.KNOWN, ValueState.NOT_MENTIONED}
            counts['abstained'] += abstention
            counts['emitted'] += not abstention
            counts['conflicts'] += fact.state == ValueState.CONFLICT
            counts['true_positive'] += len(prediction & gold)
            counts['false_positive'] += len(prediction - gold)
            counts['false_negative'] += len(gold - prediction)
        def ratio(n, d):
            return n/d if d else None
        tp, fp, fn = counts['true_positive'], counts['false_positive'], counts['false_negative']
        result[field] = {**counts, 'f1': ratio(2*tp, 2*tp+fp+fn), 'decision_coverage': ratio(counts['emitted'], counts['labeled']), 'precision': ratio(tp, tp+fp), 'recall': ratio(tp, tp+fn),
            'coverage': ratio(counts['emitted'], counts['labeled']),
            'abstention_rate': ratio(counts['abstained'], counts['labeled']),
            'conflict_rate': ratio(counts['conflicts'], counts['labeled'])}
    return result


def parse_complete(value):
    if str(value).lower() not in {'true','false'}:
        raise ValueError('Review completion must be true/false')
    return str(value).lower() == 'true'


def load_reviews(path):
    """Read independent labels in JSONL or the generated editable CSV format."""
    path = Path(path)
    if path.suffix.lower() != '.csv':
        return [HumanReview.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]
    reviews = []
    with path.open(newline='') as stream:
        for row in csv.DictReader(stream):
            labels = {}
            for field in FIELDS:
                state = row.get(field+'_state') or 'unlabeled'
                raw = row.get(field+'_value') or ''
                value = None
                if raw:
                    if field in COLLECTIONS or field in {'is_normal_vacancy','people_management','base_compensation_present'}:
                        value = json.loads(raw)
                    else:
                        value = raw
                labels[field] = HumanLabel(state=state, value=value, notes=row.get(field+'_notes') or None,
                    evidence_text=row.get(field+'_evidence') or None,
                    item_details=json.loads(row.get(field+'_items') or '[]'))
            reviews.append(HumanReview(job_id=row['job_id'], profile_input_hash=row['profile_input_hash'],
                sample_snapshot=row['sample_snapshot'], label_schema_version=row['label_schema_version'],
                reviewer_id=row.get('reviewer_id') or None, reviewed_at=row.get('reviewed_at') or None,
                reviewer_notes=row.get('reviewer_notes') or None, labels=labels,
                review_complete=parse_complete(row.get('review_complete') or 'false'),
                frozen_versions=json.loads(row.get('frozen_versions') or '{}'), cohort_hash=row.get('cohort_hash') or None))
    return reviews


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    template = sub.add_parser('template')
    template.add_argument('--review', required=True, type=Path)
    template.add_argument('--profiles', required=True, type=Path)
    template.add_argument('--snapshot', required=True)
    template.add_argument('--output', required=True, type=Path)
    metrics = sub.add_parser('metrics')
    metrics.add_argument('--labels', required=True, type=Path)
    metrics.add_argument('--profiles', required=True, type=Path)
    metrics.add_argument('--output', required=True, type=Path)
    metrics.add_argument('--completed-only', action='store_true')
    args = parser.parse_args(argv)
    profiles = {p.metadata.job_id: p for p in (JobProfile.model_validate_json(line) for line in args.profiles.read_text().splitlines())}
    if args.command == 'template':
        reviews = [json.loads(line) for line in args.review.read_text().splitlines()]
        rows = [HumanReview(job_id=row['job_id'], profile_input_hash=profiles[row['job_id']].metadata.input_hash,
                            sample_snapshot=args.snapshot) for row in reviews]
        args.output.write_text(''.join(row.model_dump_json()+'\n' for row in rows))
    else:
        labels = load_reviews(args.labels)
        if args.completed_only:
            labels = [row for row in labels if row.review_complete]
        args.output.write_text(json.dumps(evaluate_labels(profiles, labels), indent=2)+'\n')


if __name__ == '__main__':
    main()
