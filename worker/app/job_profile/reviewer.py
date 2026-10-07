"""Offline frozen-cohort reviewer builder and completed-label evaluation.

No database connection, source discovery, projection generation or network IO.
"""
import argparse
import hashlib
import json
from pathlib import Path

from .evidence import semantic_hash
from .human_evaluation import (FIELDS, HumanReview, evaluate_labels, load_reviews)
from .models import JobProfile, ValueState
from .selection import DIRECT


FIELD_CONFIG = [
    ('opportunity_type','Opportunity','Opportunity type','enum',['vacancy','talent_pool','internship_program','event','hackathon','other']),
    ('is_normal_vacancy','Opportunity','Is this a normal vacancy?','boolean',[]),
    ('normalized_role','Role','Normalized role','text',[]),
    ('job_family','Role','Job family','text',[]),
    ('career_level','Role','Career level','enum',['entry','junior','mid','senior','staff','principal']),
    ('people_management','Role','People management','boolean',[]),
    ('workplace','Workplace','Workplace mode','enum',['onsite','hybrid','remote']),
    ('city','Workplace','Cities / municipalities','strings',[]),
    ('country','Workplace','Countries (ISO alpha-2, e.g. CZ)','strings',[]),
    ('employment_schedule','Employment','Schedule','enum',['full_time','part_time','shift']),
    ('relationship','Employment','Relationship','enum',['employee','contractor','intern','temporary']),
    ('duration','Employment','Duration','enum',['permanent','fixed_term']),
    ('base_compensation_present','Compensation','Explicit base compensation present?','boolean',[]),
    ('base_compensation','Compensation','Base compensation offers','offers',[]),
    ('other_compensation_benefits','Compensation','Other monetary benefits / components','benefits',[]),
    ('technologies','Technologies and skills','Technologies mentioned','strings',[]),
    ('required_technologies','Technologies and skills','Required technologies','strings',[]),
    ('required_skills','Technologies and skills','Required skills','strings',[]),
    ('preferred_skills','Technologies and skills','Preferred skills','strings',[]),
    ('languages','Languages','Language statements','languages',[]),
    ('experience','Experience','Explicit experience constraints','experience',[]),
]


def json_lines(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def load_cohort(directory):
    directory = Path(directory)
    manifest = json.loads((directory/'manifest.json').read_text())
    ids = manifest['selected_ids']
    if len(ids) != 60 or len(set(ids)) != 60:
        raise ValueError('Expected the exact distinct frozen 60-job cohort')
    rows = json_lines(directory/'review-inputs.jsonl')
    predictions = [JobProfile.model_validate(row) for row in json_lines(directory/'predictions.jsonl')]
    for records, get_id in [(rows, lambda row:row['job_id']), (predictions, lambda p:p.metadata.job_id)]:
        if len(records) != 60 or {get_id(row) for row in records} != set(ids):
            raise ValueError('Input/prediction identity does not match the frozen manifest')
    profiles = {p.metadata.job_id:p for p in predictions}
    originals = {r['job_id']:r for r in rows}
    templates = load_reviews(directory/'human-labels.jsonl')
    if len(templates) != 60 or {r.job_id for r in templates} != set(ids):
        raise ValueError('Human template cohort identity mismatch')
    for r in templates:
        if r.profile_input_hash != profiles[r.job_id].metadata.input_hash or r.sample_snapshot != manifest['snapshot']:
            raise ValueError('Frozen human template input identity mismatch')
    families = {i.source_name for p in predictions for i in p.metadata.identities}
    if families != DIRECT | {'fantastic_jobs_apify','jooble_direct'}:
        raise ValueError('All nine expected source families must be represented')
    for p in predictions:
        if p.metadata.versions != manifest['versions']:
            raise ValueError('Prediction versions differ from frozen manifest')
    fingerprint = semantic_hash({'ids':ids,'snapshot':manifest['snapshot'],'versions':manifest['versions'],
        'inputs':[(key,profiles[key].metadata.input_hash) for key in ids]})
    return manifest, originals, profiles, fingerprint


def build(directory, output):
    manifest, originals, profiles, fingerprint = load_cohort(directory)
    jobs = []
    for key in manifest['selected_ids']:
        p=profiles[key]
        # Deliberately generate only blank templates. Existing human annotations
        # are imported explicitly; predictions never supply label values.
        label=HumanReview(job_id=key,profile_input_hash=p.metadata.input_hash,sample_snapshot=manifest['snapshot'],
                          frozen_versions=manifest['versions'],cohort_hash=fingerprint)
        jobs.append({'input':originals[key], 'sources':sorted({i.source_name for i in p.metadata.identities}),
            'blank_review':label.model_dump(mode='json'), 'prediction':p.model_dump(mode='json'),
            'provider_suggestions':[e.model_dump(mode='json') for e in p.evidence if e.extraction_method.value=='provider_suggestion']})
    payload={'cohort_hash':fingerprint,'manifest':manifest,'label_fields':list(FIELDS),
        'fields':[{'key':k,'group':g,'label':l,'type':t,'options':o} for k,g,l,t,o in FIELD_CONFIG], 'jobs':jobs}
    assets=Path(__file__).parent/'reviewer_assets'
    text=(assets/'reviewer.html').read_text()
    encoded=json.dumps(payload,ensure_ascii=False).replace('&','\\u0026').replace('<','\\u003c').replace('>','\\u003e').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    text=text.replace('/* REVIEWER_CSS */',(assets/'reviewer.css').read_text()).replace('/* REVIEWER_JS */',(assets/'reviewer.js').read_text()).replace('REVIEWER_DATA',encoded)
    output=Path(output)
    if output.exists():
        raise ValueError('Output exists; do not overwrite a reviewer artifact')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(text)
    return {'jobs':60,'cohort_hash':fingerprint,'source_families':sorted({s for j in jobs for s in j['sources']}),
            'output':str(output),'artifact_sha256':hashlib.sha256(output.read_bytes()).hexdigest()}


def cefr_metrics(profiles, reviews):
    """Score explicit human CEFR decisions; unknown individual languages abstain."""
    counts=dict(labeled=0,emitted=0,abstained=0,conflicts=0,true_positive=0,false_positive=0,false_negative=0)
    for review in reviews:
        label=review.labels['languages']
        if label.state not in {'known','not_mentioned'}:
            continue
        gold_items=label.value or []
        scorable={r['language'] for r in gold_items if r.get('cefr') or r.get('cefr_state')=='not_mentioned'}
        if label.state=='known' and not scorable:
            continue
        p=profiles[review.job_id]
        fact=p.requirements.languages
        def token(r):
            if hasattr(r,'model_dump'):r=r.model_dump(mode='json')
            comparator=r.get('cefr_comparator') or ('at_least' if r.get('cefr_or_higher') else 'unknown')
            return json.dumps([r['language'],r['cefr'],comparator,r.get('cefr_max') if comparator=='range' else None])
        gold={token(r) for r in gold_items if r.get('cefr')}
        prediction={token(r) for r in fact.value or [] if fact.state==ValueState.KNOWN and r.cefr and (not scorable or r.language in scorable)}
        counts['labeled']+=1
        decision=bool(prediction) or fact.state==ValueState.NOT_MENTIONED
        counts['emitted']+=decision;counts['abstained']+=not decision
        counts['conflicts']+=fact.state==ValueState.CONFLICT
        counts['true_positive']+=len(gold & prediction);counts['false_positive']+=len(prediction-gold);counts['false_negative']+=len(gold-prediction)
    def ratio(n,d):return n/d if d else None
    tp,fp,fn=counts['true_positive'],counts['false_positive'],counts['false_negative']
    return {**counts,'precision':ratio(tp,tp+fp),'recall':ratio(tp,tp+fn),'f1':ratio(2*tp,2*tp+fp+fn),
        'decision_coverage':ratio(counts['emitted'],counts['labeled']),'abstention_rate':ratio(counts['abstained'],counts['labeled']),
        'conflict_rate':ratio(counts['conflicts'],counts['labeled'])}


def language_metrics(profiles, reviews, requirements=False):
    """Score language presence and known requirement flags independently of CEFR."""
    counts=dict(labeled=0,emitted=0,abstained=0,conflicts=0,true_positive=0,false_positive=0,false_negative=0)
    for review in reviews:
        label=review.labels['languages']
        if label.state not in {'known','not_mentioned'}:
            continue
        items=label.value or []
        if requirements:
            items=[r for r in items if r.get('requirement') in {'required','preferred'}]
            if label.state=='known' and not items:
                continue
        scorable={r['language'] for r in items}
        fact=profiles[review.job_id].requirements.languages
        gold={(r['language'],r['requirement']) if requirements else r['language'] for r in items}
        predicted=[r for r in fact.value or [] if fact.state==ValueState.KNOWN and
            (not requirements or r.requirement in {'required','preferred'} and (not scorable or r.language in scorable))]
        prediction={(r.language,r.requirement) if requirements else r.language for r in predicted}
        counts['labeled']+=1
        decision=bool(prediction) or fact.state==ValueState.NOT_MENTIONED
        counts['emitted']+=decision;counts['abstained']+=not decision;counts['conflicts']+=fact.state==ValueState.CONFLICT
        counts['true_positive']+=len(prediction&gold);counts['false_positive']+=len(prediction-gold);counts['false_negative']+=len(gold-prediction)
    def ratio(n,d):return n/d if d else None
    tp,fp,fn=counts['true_positive'],counts['false_positive'],counts['false_negative']
    return {**counts,'precision':ratio(tp,tp+fp),'recall':ratio(tp,tp+fn),'f1':ratio(2*tp,2*tp+fp+fn),
        'decision_coverage':ratio(counts['emitted'],counts['labeled']),'abstention_rate':ratio(counts['abstained'],counts['labeled']),
        'conflict_rate':ratio(counts['conflicts'],counts['labeled'])}


def evaluate(directory, labels):
    manifest,_,profiles,fingerprint=load_cohort(directory)
    reviews=load_reviews(labels)
    if len({r.job_id for r in reviews}) != len(reviews):raise ValueError('Duplicate reviews need adjudication')
    for r in reviews:
        if r.job_id not in profiles or r.profile_input_hash != profiles[r.job_id].metadata.input_hash:
            raise ValueError('Imported labels do not belong to this frozen input')
        if r.sample_snapshot != manifest['snapshot'] or r.cohort_hash not in {None,fingerprint}:
            raise ValueError('Imported labels do not belong to this cohort')
        if r.frozen_versions and r.frozen_versions != manifest['versions']:
            raise ValueError('Imported label versions mismatch')
    completed=[r for r in reviews if r.review_complete]
    metrics=evaluate_labels(profiles,completed)
    metrics['legacy_language_statements']=metrics['languages']
    metrics['languages']=language_metrics(profiles,completed)
    metrics['language_requirement']=language_metrics(profiles,completed,requirements=True)
    metrics['explicit_cefr']=cefr_metrics(profiles,completed)
    return {'cohort_hash':fingerprint,'cohort_jobs':60,'imported_reviews':len(reviews),'completed_reviews':len(completed),
            'incomplete_reviews_excluded':len(reviews)-len(completed),'metrics':metrics,
            'note':'Exact normalized/literal facts; unlabeled/ambiguous gold is excluded. No semantic similarity or fabricated accuracy.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    builder=sub.add_parser('build');builder.add_argument('--cohort',type=Path,required=True);builder.add_argument('--output',type=Path,required=True)
    metrics=sub.add_parser('evaluate');metrics.add_argument('--cohort',type=Path,required=True);metrics.add_argument('--labels',type=Path,required=True);metrics.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='build':result=build(args.cohort,args.output)
    else:
        result=evaluate(args.cohort,args.labels);args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
