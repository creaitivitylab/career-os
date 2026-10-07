"""Freeze an independent review subset of an existing evaluation population.

Offline only: no database, network, generated human labels or provider AI truth.
"""
import argparse
from collections import Counter
import csv
import html
import json
from pathlib import Path

from .evaluate import choose_review_jobs, SEED as BASE_SELECTION_SEED
from .evidence import semantic_hash
from .human_evaluation import FIELDS, HumanReview, evaluate_labels
from .models import JobProfile
from .selection import DIRECT


SEED = 'job-profile-v1.2-review60-v1'


def source_class(job):
    names = {s['source_name'] for s in job['sources']}
    return 'multi_source' if len(names)>1 else 'direct_only' if names & DIRECT else 'provider_only'


def choose_subset(jobs, profiles, snapshot, count=60):
    if not 1 <= count <= len(jobs) or len({j['id'] for j in jobs}) != len(jobs):
        raise ValueError('Review count/population is invalid')
    # Reserve up to four explicitly signaled non-standard records already in
    # the review population. Predictions affect sampling, never human labels.
    special = sorted((j for j in jobs if profiles[j['id']].metadata.opportunity_type.value
        in {'talent_pool','internship_program','hackathon','event'}),
        key=lambda j: semantic_hash([SEED, j['id']]))[:min(4, count)]
    reserved = {j['id'] for j in special}
    pool = []
    for j in jobs:
        if j['id'] in reserved:
            continue
        p = profiles[j['id']]
        item = {**j, '_strata': {**j.get('_strata', {}),
            'source_class': source_class(j),
            'description_quality': p.content.description_quality,
            'language_statement': bool(p.requirements.languages.value),
            'technology_mention': bool(p.requirements.technologies.value),
            'seniority_marker': bool(p.role.career_level.value),
            'opportunity_signal': p.metadata.opportunity_type.value or 'unknown'}}
        pool.append(item)
    selected = special + choose_review_jobs(pool, profiles, snapshot, count-len(special)) if count > len(special) else special
    if len(selected) != count:
        raise ValueError('Not enough archived active/recent jobs in review population')
    return selected


def freeze(sample, population, profiles, output, count=60):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Frozen review output exists; choose a new output directory')
    output.mkdir(parents=True, exist_ok=True)
    ids = [r['job_id'] for r in population]
    if len(ids) != 200 or len(set(ids)) != 200:
        raise ValueError('Expected the original distinct 200-job review population')
    jobs = {j['id']: j for j in sample['jobs']}
    selected = choose_subset([jobs[key] for key in ids], profiles, sample['snapshot'], count)
    labels = [HumanReview(job_id=j['id'], profile_input_hash=profiles[j['id']].metadata.input_hash,
                         sample_snapshot=sample['snapshot']) for j in selected]
    (output/'human-labels.jsonl').write_text(''.join(r.model_dump_json()+'\n' for r in labels))
    with (output/'human-labels.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['job_id','profile_input_hash','sample_snapshot','label_schema_version','reviewer_id','reviewed_at','reviewer_notes',
            *[key for field in FIELDS for key in (field+'_state',field+'_value',field+'_notes')]])
        writer.writeheader()
        for r in labels:
            writer.writerow({'job_id':r.job_id,'profile_input_hash':r.profile_input_hash,'sample_snapshot':r.sample_snapshot,'label_schema_version':r.label_schema_version,
                **{field+'_state':'unlabeled' for field in FIELDS}})
    references = {r['job_id']: r for r in population}
    pages = ["<!doctype html><meta charset='utf-8'><title>Job Profile V1.2 independent review</title>",
        "<style>body{font:16px system-ui;max-width:1100px;margin:30px auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}article{border-top:2px solid #bbb;margin:25px 0}details{margin:12px 0}</style>",
        '<h1>60-job independent human review</h1><p>Label human-labels.jsonl from original evidence before opening predictions. All labels are blank. Unknown and ambiguous are valid. Provider suggestions are never ground truth.</p>',
        '<p>Known: independently supported value. Not mentioned: reviewed absence. Unknown: insufficient input. Ambiguous: competing/unclear interpretation. Preserve separate salary offers, literal proficiency and CEFR bounds. Do not infer people management from Manager/Lead.</p>',
        '<p>Fields: '+html.escape(', '.join(FIELDS))+'. Reviewer notes are supported per field and per job.</p>']
    with (output/'predictions.jsonl').open('w') as predictions, (output/'review-inputs.jsonl').open('w') as inputs:
        for j in selected:
            p = profiles[j['id']]
            predictions.write(p.model_dump_json()+'\n')
            original = references[j['id']]
            row = {k:original[k] for k in ('job_id','company','title','description','description_source','structured_source_inputs')}
            inputs.write(json.dumps(row, ensure_ascii=False)+'\n')
            pages += ['<article><h2>'+html.escape(str(j.get('company') or ''))+' — '+html.escape(j['title'])+'</h2>',
                '<p>'+html.escape(j['id'])+' · '+html.escape(', '.join(sorted(s['source_name'] for s in j['sources'])))+'</p>',
                '<pre>'+html.escape(original['description'] or 'No usable text')+'</pre>',
                '<details><summary>Original structured evidence</summary><pre>'+html.escape(json.dumps(original['structured_source_inputs'],ensure_ascii=False,indent=2))+'</pre></details>',
                '<details><summary>Separate deterministic prediction — NOT a label</summary><pre>'+html.escape(p.model_dump_json(indent=2))+'</pre></details>',
                '<details><summary>Separate provider suggestions — NOT ground truth</summary><pre>'+html.escape(json.dumps(original['provider_suggestions_reference_only'],ensure_ascii=False,indent=2))+'</pre></details></article>']
    (output/'review.html').write_text('\n'.join(pages))
    manifest = {'seed':SEED, 'base_selection_seed':BASE_SELECTION_SEED, 'snapshot':sample['snapshot'], 'sample_hash':semantic_hash(sample),
        'population_hash':semantic_hash(population), 'selected_ids':[j['id'] for j in selected],
        'versions':profiles[selected[0]['id']].metadata.versions,
        'primary_source_counts':Counter(j['primary_stratum'] for j in selected),
        'source_memberships':Counter(name for j in selected for name in {s['source_name'] for s in j['sources']}),
        'source_class':Counter(source_class(j) for j in selected),
        'description_quality':Counter(profiles[j['id']].content.description_quality for j in selected),
        'opportunity_type':Counter(profiles[j['id']].metadata.opportunity_type.value or 'unknown' for j in selected),
        'strata':{key:Counter(str(j.get('_strata',{}).get(key)) for j in selected) for key in ('language_proxy','location','title_family_proxy','title_level_proxy','workplace','employer_catalog_size')},
        'salary_available':sum(bool(profiles[j['id']].compensation.offers.value) for j in selected),
        'language_statements':sum(bool(profiles[j['id']].requirements.languages.value) for j in selected),
        'technology_mentions':sum(bool(profiles[j['id']].requirements.technologies.value) for j in selected),
        'note':'Sampling proxies/predictions are not labels or accuracy. No completed human labels.'}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    (output/'unlabeled-metrics.json').write_text(json.dumps(evaluate_labels(profiles, labels),indent=2)+'\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('sample','population','profiles','output'):
        parser.add_argument('--'+arg, required=True, type=Path)
    args = parser.parse_args()
    sample = json.loads(args.sample.read_text())
    population = [json.loads(line) for line in args.population.read_text().splitlines()]
    profiles = {p.metadata.job_id:p for p in (JobProfile.model_validate_json(line) for line in args.profiles.read_text().splitlines())}
    print(json.dumps(freeze(sample,population,profiles,args.output),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
