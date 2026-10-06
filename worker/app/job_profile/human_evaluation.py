"""Versioned independent labels and selective deterministic-field metrics."""
import argparse
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from .models import JobProfile, Model, ValueState


LABEL_VERSION = 'job-profile-human-v1.0'
FIELDS = ('normalized_role', 'job_family', 'career_level', 'workplace', 'employment_schedule',
          'relationship', 'compensation', 'technologies', 'languages', 'experience')
COLLECTIONS = {'compensation', 'technologies', 'languages', 'experience'}


class LabelLanguage(Model):
    language: str = Field(pattern=r'^[a-z]{2}$')
    requirement: Literal['required','preferred','unknown'] = 'unknown'
    cefr: Literal['A1','A2','B1','B2','C1','C2'] | None = None
    cefr_or_higher: bool | None = None


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


class HumanLabel(Model):
    state: Literal['unlabeled', 'known', 'not_mentioned', 'unknown', 'conflict'] = 'unlabeled'
    value: Any = None
    notes: str | None = None

    @model_validator(mode='after')
    def validate_state(self):
        if self.state == 'known' and (self.value is None or self.value == [] or self.value == ''):
            raise ValueError('Known human labels require a value; use not_mentioned for absence')
        if self.state != 'known' and self.value is not None:
            raise ValueError('Unavailable labels cannot carry values')
        return self


class HumanReview(Model):
    label_schema_version: Literal['job-profile-human-v1.0'] = LABEL_VERSION
    job_id: str
    profile_input_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    sample_snapshot: str
    reviewer_id: str | None = None
    reviewed_at: datetime | None = None
    reviewer_notes: str | None = None
    labels: dict[str, HumanLabel] = Field(default_factory=lambda: {field: HumanLabel() for field in FIELDS})

    @model_validator(mode='after')
    def validate_labels(self):
        if set(self.labels) != set(FIELDS):
            raise ValueError('All reviewed fields must be explicit; unexpected label field')
        if any(label.state != 'unlabeled' for label in self.labels.values()) and (not self.reviewer_id or not self.reviewed_at):
            raise ValueError('Reviewed labels require reviewer identity and timestamp')
        enums = {
            'career_level': {'entry','junior','mid','senior','staff','principal'},
            'workplace': {'onsite','hybrid','remote'},
            'employment_schedule': {'full_time','part_time','shift'},
            'relationship': {'employee','contractor','intern','temporary'},
        }
        for field, label in self.labels.items():
            if label.state != 'known':
                continue
            if field in COLLECTIONS:
                if not isinstance(label.value, list):
                    raise ValueError('Collection labels must contain a list')
                if field == 'technologies' and not all(isinstance(x, str) and x for x in label.value):
                    raise ValueError('Technology labels must be normalized names')
                if field != 'technologies' and not all(isinstance(x, dict) and x for x in label.value):
                    raise ValueError('Structured labels must contain objects')
                shape = {'languages':LabelLanguage, 'experience':LabelExperience,
                         'compensation':LabelCompensation}.get(field)
                if shape:
                    label.value = [shape.model_validate(item).model_dump(mode='json') for item in label.value]
            elif not isinstance(label.value, str) or field in enums and label.value not in enums[field]:
                raise ValueError('Invalid categorical human label')
        return self


def prediction_fact(profile, field):
    mapping = {
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
            item = {k: item.get(k) for k in ('language','requirement','cefr','cefr_or_higher')}
            item['cefr_or_higher'] = bool(item['cefr_or_higher']) if item['cefr'] else None
        elif field == 'experience':
            item = {k: str(Decimal(str(item[k])).normalize()) if item.get(k) is not None else None for k in ('min_years','max_years')}
        elif field == 'compensation':
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
        result[field] = {**counts, 'precision': ratio(tp, tp+fp), 'recall': ratio(tp, tp+fn),
            'coverage': ratio(counts['emitted'], counts['labeled']),
            'abstention_rate': ratio(counts['abstained'], counts['labeled']),
            'conflict_rate': ratio(counts['conflicts'], counts['labeled'])}
    return result


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
    args = parser.parse_args(argv)
    profiles = {p.metadata.job_id: p for p in (JobProfile.model_validate_json(line) for line in args.profiles.read_text().splitlines())}
    if args.command == 'template':
        reviews = [json.loads(line) for line in args.review.read_text().splitlines()]
        rows = [HumanReview(job_id=row['job_id'], profile_input_hash=profiles[row['job_id']].metadata.input_hash,
                            sample_snapshot=args.snapshot) for row in reviews]
        args.output.write_text(''.join(row.model_dump_json()+'\n' for row in rows))
    else:
        labels = [HumanReview.model_validate_json(line) for line in args.labels.read_text().splitlines()]
        args.output.write_text(json.dumps(evaluate_labels(profiles, labels), indent=2)+'\n')


if __name__ == '__main__':
    main()
