"""Offline human-review tooling: synthetic labels are not cohort annotations."""
import json
from pathlib import Path
import re
import tempfile
import unittest
from datetime import datetime,timezone

from app.job_profile.human_evaluation import (FIELDS, HumanLabel, HumanReview, LabelLanguage,
    evaluate_labels, load_reviews, tokens)
from app.job_profile.models import ValueState, Fact, Identity
from app.job_profile.pipeline import build_profile
from app.job_profile.reviewer import build, evaluate, load_cohort, cefr_metrics, language_metrics
from app.job_profile.versions import versions
from app.job_profile.selection import DIRECT

NOW=datetime(2026,10,7,tzinfo=timezone.utc)


def profile(key='job-0'):
    return build_profile({'id':key,'sources':[{'source_name':'ashby_direct','source_job_id':key,'raw_payload':{
        'title':'Senior Engineer','location':'Prague','workplaceType':'Hybrid','descriptionPlain':'Requirements\nEnglish B2+ required.\nPython SQL.\n3 years of experience.\nSalary 80000 CZK/month.'}}]},generated_at=NOW)


def review(p,complete=False,**values):
    labels={key:HumanLabel() for key in FIELDS}
    labels.update({key:HumanLabel(state='known',value=value) for key,value in values.items()})
    return HumanReview(job_id=p.metadata.job_id,profile_input_hash=p.metadata.input_hash,sample_snapshot=NOW.isoformat(),
        reviewer_id='synthetic-human',reviewed_at=NOW,review_complete=complete,labels=labels)


def cohort(directory):
    names=sorted(DIRECT|{'fantastic_jobs_apify','jooble_direct'});ps={};inputs=[];templates=[]
    for i in range(60):
        p=profile('job-'+str(i));p.metadata.identities=[Identity(source_name=names[i%9],source_job_id='native-'+str(i))]
        ps[p.metadata.job_id]=p;inputs.append({'job_id':p.metadata.job_id,'company':'Synthetic Company','title':'Senior Engineer','description':'Original employer input','description_source':names[i%9],'structured_source_inputs':[]})
        templates.append(HumanReview(job_id=p.metadata.job_id,profile_input_hash=p.metadata.input_hash,sample_snapshot=NOW.isoformat()))
    manifest={'selected_ids':list(ps),'snapshot':NOW.isoformat(),'versions':versions()}
    (directory/'manifest.json').write_text(json.dumps(manifest))
    for name,rows in [('predictions.jsonl',[p.model_dump(mode='json') for p in ps.values()]),('review-inputs.jsonl',inputs),('human-labels.jsonl',[r.model_dump(mode='json') for r in templates])]:
        (directory/name).write_text(''.join(json.dumps(r)+'\n' for r in rows))
    return ps


class HumanReviewerContractTests(unittest.TestCase):
    def test_v11_labels_remain_compatible(self):
        p=profile();r=review(p,career_level='senior').model_dump(mode='json');r['label_schema_version']='job-profile-human-v1.1';r['labels']={k:v for k,v in r['labels'].items() if k in FIELDS[:19]}
        new=HumanReview.model_validate(r);self.assertEqual(new.labels['required_skills'].state,'unlabeled');self.assertFalse(new.review_complete)

    def test_states_evidence_and_items_preserved(self):
        p=profile();r=review(p,required_skills=['Data modeling']);r.labels['required_skills'].evidence_text='Data modeling required';r.labels['required_skills'].item_details=[]
        for k,state in [('career_level','unknown'),('workplace','not_mentioned'),('people_management','ambiguous')]:r.labels[k]=HumanLabel(state=state)
        self.assertEqual(HumanReview.model_validate_json(r.model_dump_json()),r)

    def test_other_opportunity_type(self):
        p=profile();self.assertEqual(review(p,opportunity_type='other').labels['opportunity_type'].value,'other')

    def test_completion_not_inferred(self):
        self.assertFalse(review(profile(),career_level='senior').review_complete)

    def test_complete_requires_identity(self):
        p=profile()
        with self.assertRaises(ValueError):HumanReview(job_id=p.metadata.job_id,profile_input_hash=p.metadata.input_hash,sample_snapshot='x',review_complete=True)

    def test_cefr_all_comparators(self):
        for comparison in ['exact','at_least','at_most','range','unknown']:
            r=LabelLanguage(language='en',cefr='B2',cefr_comparator=comparison,cefr_max='C1' if comparison=='range' else None)
            self.assertEqual(LabelLanguage.model_validate_json(r.model_dump_json()).cefr_comparator,comparison)

    def test_invalid_range_rejected(self):
        with self.assertRaises(ValueError):LabelLanguage(language='en',cefr='C1',cefr_comparator='range',cefr_max='B1')

    def test_no_cefr_from_fluent(self):
        self.assertIsNone(LabelLanguage(language='en',original_wording='fluent English',cefr_state='not_mentioned').cefr)

    def test_skills_and_required_technologies_abstain(self):
        p=profile();r=review(p,required_skills=['Data modeling'],required_technologies=['Python'])
        m=evaluate_labels({p.metadata.job_id:p},[r]);self.assertEqual(m['required_skills']['abstention_rate'],1);self.assertEqual(m['required_technologies']['recall'],0)

    def test_precision_recall_f1(self):
        p=profile();r=review(p,technologies=['Python','Docker']);m=evaluate_labels({p.metadata.job_id:p},[r])['technologies']
        self.assertEqual(m['precision'],.5);self.assertEqual(m['recall'],.5);self.assertEqual(m['f1'],.5)

    def test_unlabeled_metrics_null(self):
        p=profile();r=review(p);self.assertTrue(all(m['precision'] is None and m['decision_coverage'] is None for m in evaluate_labels({p.metadata.job_id:p},[r]).values()))

    def test_cefr_separate_metrics(self):
        p=profile();r=review(p,languages=[{'language':'en','cefr':'B2','cefr_comparator':'at_least','requirement':'required'}])
        self.assertEqual(cefr_metrics({p.metadata.job_id:p},[r])['precision'],1)

    def test_unknown_cefr_unscored(self):
        p=profile();r=review(p,languages=[{'language':'en','original_wording':'fluent English'}]);self.assertIsNone(cefr_metrics({p.metadata.job_id:p},[r])['precision'])

    def test_cefr_conflict_abstains(self):
        p=profile();r=review(p,languages=[{'language':'en','cefr':'B2'}]);p.requirements.languages=Fact(state=ValueState.CONFLICT)
        m=cefr_metrics({p.metadata.job_id:p},[r]);self.assertEqual(m['conflict_rate'],1);self.assertEqual(m['abstention_rate'],1)


class FrozenReviewerTests(unittest.TestCase):
    def test_artifact_is_blank_blind_and_offline(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);ps=cohort(root);out=root/'reviewer.html';build(root,out);text=out.read_text()
            data=json.loads(re.search(r'<script id="review-data" type="application/json">(.*?)</script>',text,re.S).group(1))
            self.assertEqual(len(data['jobs']),60)
            self.assertTrue(all(l['state']=='unlabeled' and l['value'] is None for j in data['jobs'] for l in j['blank_review']['labels'].values()))
            self.assertLess(text.index('id="human"'),text.index('id="prediction"'));self.assertNotIn('<details id="prediction" open',text)
            self.assertIn("connect-src 'none'",text);self.assertNotIn('<script src=',text)

    def test_not_overwrite_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cohort(root);out=root/'reviewer.html';out.write_text('existing reviewer')
            with self.assertRaises(ValueError):build(root,out)
            self.assertEqual(out.read_text(),'existing reviewer')

    def test_input_identity_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cohort(root);rows=[json.loads(x) for x in (root/'human-labels.jsonl').read_text().splitlines()];rows[0]['profile_input_hash']='0'*64
            (root/'human-labels.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            with self.assertRaises(ValueError):load_cohort(root)

    def test_not_select_different_cohort(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cohort(root);m=json.loads((root/'manifest.json').read_text());m['selected_ids'][0]='different-job';(root/'manifest.json').write_text(json.dumps(m))
            with self.assertRaises(ValueError):load_cohort(root)

    def test_html_input_escaped(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cohort(root);p=root/'review-inputs.jsonl';rows=[json.loads(x) for x in p.read_text().splitlines()];rows[0]['title']='</script><img src=x onerror=alert(1)>';p.write_text(''.join(json.dumps(r)+'\n' for r in rows));out=root/'review.html';build(root,out)
            self.assertNotIn('</script><img src=x',out.read_text());self.assertIn('\\u003cimg',out.read_text())

    def test_partial_completed_cohort(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);ps=cohort(root);rows=[review(p,complete=(i<10),career_level='senior') for i,p in enumerate(ps.values())];labels=root/'completed.jsonl';labels.write_text(''.join(r.model_dump_json()+'\n' for r in rows));result=evaluate(root,labels)
            self.assertEqual(result['completed_reviews'],10);self.assertEqual(result['metrics']['career_level']['labeled'],10);self.assertEqual(result['metrics']['career_level']['precision'],1)

    def test_only_subset_file_needed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);ps=cohort(root);p=next(iter(ps.values()));labels=root/'one.jsonl';labels.write_text(review(p,complete=True,workplace='hybrid').model_dump_json()+'\n')
            self.assertEqual(evaluate(root,labels)['completed_reviews'],1)

    def test_incomplete_reviews_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);ps=cohort(root);p=next(iter(ps.values()));labels=root/'partial.jsonl';labels.write_text(review(p,career_level='senior').model_dump_json()+'\n');r=evaluate(root,labels)
            self.assertEqual(r['completed_reviews'],0);self.assertIsNone(r['metrics']['career_level']['precision'])

    def test_frozen_hash_guard(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);ps=cohort(root);p=next(iter(ps.values()));r=review(p,complete=True,career_level='senior');r.cohort_hash='f'*64;labels=root/'foreign.jsonl';labels.write_text(r.model_dump_json()+'\n')
            with self.assertRaises(ValueError):evaluate(root,labels)


class LanguageDecisionTests(unittest.TestCase):
    def test_unknown_cefr_does_not_reduce_language_presence_precision(self):
        p=profile();r=review(p,languages=[{'language':'en'}])
        self.assertEqual(language_metrics({p.metadata.job_id:p},[r])['precision'],1)
        self.assertIsNone(cefr_metrics({p.metadata.job_id:p},[r])['precision'])
        self.assertIsNone(language_metrics({p.metadata.job_id:p},[r],requirements=True)['precision'])

    def test_language_requirement_metric(self):
        p=profile();r=review(p,languages=[{'language':'en','requirement':'required'}])
        self.assertEqual(language_metrics({p.metadata.job_id:p},[r],requirements=True)['precision'],1)
