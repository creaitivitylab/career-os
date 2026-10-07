"""Exact production-pattern regressions; fixtures are offline public excerpts."""
from datetime import datetime, timezone
import json
import csv
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from app.job_profile.evidence import EvidenceCollector
from app.job_profile.human_evaluation import (FIELDS, HumanLabel, HumanReview,
    evaluate_labels, prediction_fact, tokens, load_reviews)
from app.job_profile.models import JobProfile, SourceInput, ValueState
from app.job_profile.parsers import languages, salary
from app.job_profile.pipeline import build_profile, reprocessing_layers
from app.job_profile.review_freeze import choose_subset
from app.job_profile.versions import versions

FIXTURES = json.loads((Path(__file__).parent/'fixtures/job_profile_v12.json').read_text())
NOW = datetime(2026,10,6,tzinfo=timezone.utc)


def profile(name=None, title='Engineer', description='', source='ashby_direct', raw=None):
    if name:
        data = FIXTURES[name]
        source, raw = data['source_name'], data['raw_payload']
    raw = raw if raw is not None else {'title':title,'descriptionPlain':description}
    return build_profile({'id':'example','sources':[{'source_name':source,'source_job_id':'test:123','raw_payload':raw,'is_active':True}]},generated_at=NOW)


class OpportunityTests(unittest.TestCase):
    def test_exact_talent_pool_invitation(self):
        p=profile('talent_pool')
        self.assertEqual(p.metadata.opportunity_type.value,'talent_pool')
        self.assertFalse(p.metadata.is_normal_vacancy.value)
        self.assertTrue(any('nemáme zrovna volnou pozici' in e.text_span.text for e in p.evidence if e.field=='opportunity_type'))

    def test_exact_hackathon_record(self):
        p=profile('hackathon')
        self.assertEqual(p.metadata.opportunity_type.value,'hackathon')
        self.assertFalse(p.metadata.is_normal_vacancy.value)

    def test_explicit_talent_community_title(self):
        self.assertEqual(profile(title='Join our Talent Community').metadata.opportunity_type.value,'talent_pool')

    def test_generic_join_team_abstains(self):
        self.assertEqual(profile(title='Join our team',description='Join our team of professionals.').metadata.opportunity_type.state,ValueState.UNKNOWN)

    def test_cv_invitation_without_no_vacancy_evidence_abstains(self):
        self.assertIsNone(profile(title='Pošlete nám svůj životopis',description='Pošlete nám svůj životopis.').metadata.opportunity_type.value)

    def test_ordinary_internship_not_program(self):
        self.assertIsNone(profile(title='Software Engineering Internship').metadata.opportunity_type.value)

    def test_explicit_program(self):
        self.assertEqual(profile(title='Graduate Programme 2026').metadata.opportunity_type.value,'internship_program')

    def test_event_organizer_is_not_event(self):
        self.assertIsNone(profile(title='Hiring Event Coordinator').metadata.opportunity_type.value)

    def test_hackathon_engineer_is_not_hackathon(self):
        self.assertIsNone(profile(title='Hackathon 2026 Software Engineer').metadata.opportunity_type.value)

    def test_explicit_recruiting_event(self):
        self.assertEqual(profile(title='Career Fair 2026').metadata.opportunity_type.value,'event')

    def test_explicit_role_recruiting_clause(self):
        p=profile(title='Python Engineer',description='We are hiring a Python Engineer to build production services.')
        self.assertEqual(p.metadata.opportunity_type.value,'vacancy')
        self.assertTrue(p.metadata.is_normal_vacancy.value)

    def test_unmarked_evergreen_ad_abstains(self):
        self.assertIsNone(profile(title='Software Engineer',description='Join our team. Build reliable software.').metadata.opportunity_type.value)

    def test_provider_suggestion_does_not_classify(self):
        p=profile(source='fantastic_jobs_apify',raw={'title':'Engineer','ai_opportunity_type':'talent_pool'})
        self.assertIsNone(p.metadata.opportunity_type.value)


class CompensationTests(unittest.TestCase):
    def test_exact_pension_line_is_not_offer(self):
        p=profile('pension')
        self.assertIsNone(p.compensation.offers.value)
        self.assertIn('1000 CZK/month',p.compensation.monetary_benefits.value[0])
        self.assertTrue(p.compensation.monetary_benefits.evidence_ids)

    def test_benefit_contexts_do_not_produce_salary(self):
        for line in ('Meal allowance','Wellness allowance','Referral bonus','Commuting allowance','Pension contribution','Voucher','Revenue','Budget'):
            with self.subTest(line=line):
                self.assertEqual(salary(line+': 1000 CZK/month',SourceInput(source_name='ashby_direct',source_job_id='x'),EvidenceCollector()),[])

    def test_legitimate_salary_preserved(self):
        p=profile(description='Base salary: 80,000–100,000 CZK/month')
        self.assertEqual(str(p.compensation.offers.value[0].min_amount),'80000')
        self.assertEqual(p.compensation.offers.value[0].component,'base')

    def test_separate_benefit_line_does_not_suppress_salary(self):
        p=profile(description='Salary: 80,000 CZK/month\nMeal allowance: 1000 CZK/month')
        self.assertEqual(len(p.compensation.offers.value),1)
        self.assertEqual(len(p.compensation.monetary_benefits.value),1)

    def test_exact_greenhouse_country_scopes(self):
        p=profile('scoped_offers')
        self.assertEqual(len(p.compensation.offers.value),8)
        scopes={o.original_text.strip():o.applicable_locations for o in p.compensation.offers.value}
        self.assertEqual(scopes['UK'],['United Kingdom'])
        self.assertEqual(scopes['United States'],['United States'])
        self.assertEqual(scopes['Netherlands'],['Netherlands'])
        self.assertEqual(scopes['France'],['France'])
        self.assertEqual(scopes['Germany'],['Germany'])
        self.assertEqual([o.original_location_labels for o in p.compensation.offers.value],[[r['title']] for r in FIXTURES['scoped_offers']['raw_payload']['pay_input_ranges']])

    def test_unlinked_offer_does_not_copy_job_location(self):
        p=profile(source='greenhouse_direct',raw={'title':'Engineer','location':{'name':'Prague'},'pay_input_ranges':[{'title':'Base Salary Range','min_cents':8000000,'currency_type':'CZK'}]})
        self.assertIsNone(p.compensation.offers.value[0].applicable_locations)
        self.assertEqual(p.compensation.offers.value[0].original_text,'Base Salary Range')
        self.assertIsNone(p.compensation.offers.value[0].original_location_labels)

    def test_substring_country_is_not_scope(self):
        p=profile(source='greenhouse_direct',raw={'pay_input_ranges':[{'title':'UK hiring bonus and worldwide pay','min_cents':100000,'currency_type':'GBP'}]})
        self.assertIsNone(p.compensation.offers.value[0].applicable_locations)

    def test_offer_currencies_not_combined(self):
        p=profile('scoped_offers')
        self.assertEqual({o.currency for o in p.compensation.offers.value},{'GBP','EUR','SEK','USD'})


class LanguageTests(unittest.TestCase):
    def parse(self,text):
        return languages(text,SourceInput(source_name='ashby_direct',source_job_id='x'),EvidenceCollector())

    def test_exact_b2_level_or_above(self):
        p=profile('language');v=p.requirements.languages.value[0]
        self.assertEqual(v.cefr,'B2'); self.assertEqual(v.cefr_comparator,'at_least');self.assertTrue(v.cefr_or_higher)

    def test_explicit_lower_bound_forms(self):
        for wording,level in [('English B2 or above','B2'),('English B2+','B2'),('English minimum B2','B2'),('English at least B2','B2'),('English C1 or higher','C1'),('minimum B2 English','B2')]:
            with self.subTest(wording=wording):
                v=self.parse(wording)[0];self.assertEqual(v.cefr,level);self.assertTrue(v.cefr_or_higher);self.assertEqual(v.cefr_comparator,'at_least')

    def test_non_cefr_never_inferred(self):
        for text in ('fluent English','professional English','advanced English'):
            v=self.parse(text)[0];self.assertIsNone(v.cefr);self.assertIsNone(v.cefr_comparator)

    def test_bare_level_not_exact_or_lower_bound(self):
        self.assertEqual(self.parse('English B2')[0].cefr_comparator,'unknown')

    def test_exact_qualifier(self):
        self.assertEqual(self.parse('English exactly B2')[0].cefr_comparator,'exact')

    def test_separate_language_bounds(self):
        values=self.parse('Proficiency in Czech (C2 level) and English (minimum B2 level)')
        self.assertEqual([(v.cefr,v.cefr_comparator) for v in values],[('C2','unknown'),('B2','at_least')])

    def test_requirement_preserved(self):
        values=self.parse('English B2 or above required; German C1 preferred')
        self.assertEqual([v.requirement for v in values],['required','preferred'])


class FreezeAndCompatibilityTests(unittest.TestCase):
    def review(self,p,**values):
        labels={f:HumanLabel() for f in FIELDS}
        labels.update({f:HumanLabel(state='known',value=v) for f,v in values.items()})
        return HumanReview(job_id=p.metadata.job_id,profile_input_hash=p.metadata.input_hash,sample_snapshot='2026-10-06',labels=labels,reviewer_id='human',reviewed_at=NOW)

    def test_existing_jsonb_shape_loads(self):
        p=profile('language');old=p.model_dump(mode='json')
        for key in ('opportunity_type','is_normal_vacancy'):old['metadata'].pop(key)
        old['compensation'].pop('monetary_benefits')
        old['requirements']['languages']['value'][0].pop('cefr_comparator')
        self.assertIsNone(JobProfile.model_validate(old).metadata.opportunity_type.value)

    def test_geography_cleaner_dictionary_versions_unchanged(self):
        v=versions();self.assertEqual(v['geography'],'geography-v1.1');self.assertEqual(v['cleaner'],'description-v1.0');self.assertEqual(v['dictionary'],'technology-v1.0')

    def test_projector_only_change_does_not_invalidate_text(self):
        p=profile();old=p.metadata.model_copy(deep=True);old.versions['projector']='native-v1.1';old.input_hash='old'
        self.assertEqual(reprocessing_layers(old,p.metadata),{'native','selection','profile'})

    def test_parser_change_invalidates_text(self):
        p=profile();old=p.metadata.model_copy(deep=True);old.versions['parser']='old';old.input_hash='old'
        self.assertIn('text',reprocessing_layers(old,p.metadata))

    def test_unchanged_no_work(self):
        p=profile();self.assertEqual(reprocessing_layers(p.metadata,p.metadata),set())

    def test_no_labels_no_accuracy_or_threshold(self):
        p=profile();r=HumanReview(job_id='example',profile_input_hash=p.metadata.input_hash,sample_snapshot='snapshot')
        self.assertTrue(all(v['precision'] is None and v['f1'] is None for v in evaluate_labels({'example':p},[r]).values()))

    def test_false_is_valid_human_vacancy_label(self):
        p=profile('hackathon');r=self.review(p,is_normal_vacancy=False,opportunity_type='hackathon')
        self.assertEqual(evaluate_labels({'example':p},[r])['is_normal_vacancy']['precision'],1)

    def test_ambiguous_labels_excluded(self):
        p=profile();r=self.review(p,career_level='senior');r.labels['career_level']=HumanLabel(state='ambiguous')
        self.assertEqual(evaluate_labels({'example':p},[r])['career_level']['labeled'],0)

    def test_cefr_comparator_mismatch_is_scored(self):
        p=profile('language');r=self.review(p,languages=[{'language':'en','cefr':'B2','cefr_or_higher':False,'cefr_comparator':'exact'}])
        self.assertEqual(evaluate_labels({'example':p},[r])['languages']['recall'],0)

    def test_set_f1_and_decision_coverage(self):
        p=profile(description='Python SQL');r=self.review(p,technologies=['Python','Docker'])
        m=evaluate_labels({'example':p},[r])['technologies'];self.assertEqual(m['f1'],.5);self.assertEqual(m['decision_coverage'],1)

    def test_mentions_do_not_become_required_technologies(self):
        self.assertIsNone(prediction_fact(profile(description='Python SQL'),'required_technologies').value)

    def test_legacy_label_format_loads_without_prefilling_new_truth(self):
        p=profile();r=self.review(p,career_level='senior').model_dump(mode='json');r['label_schema_version']='job-profile-human-v1.0';r['labels']={k:v for k,v in r['labels'].items() if k in FIELDS[:10]}
        loaded=HumanReview.model_validate(r);self.assertTrue(all(loaded.labels[k].state=='unlabeled' for k in FIELDS[10:]))

    def test_invalid_review_subset_count_rejected(self):
        with self.assertRaises(ValueError):choose_subset([],{},'2026-10-06',60)

    def test_foreign_fields_unchanged_by_opportunity_signal(self):
        p=profile(title='Manager',raw={'title':'Manager','location':'Prague','workplaceType':'Hybrid','employmentType':'FullTime'})
        self.assertIsNone(p.role.people_management.value);self.assertEqual(p.workplace.mode.value,'hybrid');self.assertEqual(p.workplace.locations.value[0].country.value,'CZ')


class IncrementalQualityTests(unittest.TestCase):
    def test_metadata_only_reuses_new_text_signals(self):
        job={'id':'example','sources':[{'source_name':'ashby_direct','source_job_id':'x','raw_payload':{'title':'Engineer','department':'Engineering','descriptionPlain':'We are hiring a Python Engineer.\nPension contribution: 1000 CZK/month'}}]}
        cache={}; first=build_profile(job,cache_out=cache)
        job['sources'][0]['raw_payload']['department']='Platform'
        with patch('app.job_profile.pipeline.monetary_benefits',side_effect=AssertionError('text parser re-executed')), patch('app.job_profile.pipeline.opportunity_description',side_effect=AssertionError('description classification re-executed')):
            updated=build_profile(job,text_cache=cache)
        self.assertEqual(updated.compensation.monetary_benefits,first.compensation.monetary_benefits)
        self.assertEqual(updated.metadata.opportunity_type,first.metadata.opportunity_type)

    def test_explicit_comparator_label_needs_no_duplicate_boolean(self):
        self.assertEqual(tokens('languages',[{'language':'en','cefr':'B2','cefr_comparator':'at_least'}]),tokens('languages',[{'language':'en','cefr':'B2','cefr_comparator':'at_least','cefr_or_higher':True}]))

    def test_mixed_amount_line_abstains_conservatively(self):
        p=profile(description='Salary 80000 CZK/month including a pension contribution 1000 CZK/month')
        self.assertIsNone(p.compensation.offers.value)


class EvaluationWorkflowTests(unittest.TestCase):
    def test_csv_labels_round_trip_without_prediction_prefill(self):
        p=profile('language')
        row={'job_id':p.metadata.job_id,'profile_input_hash':p.metadata.input_hash,'sample_snapshot':'2026-10-06','label_schema_version':'job-profile-human-v1.1','reviewer_id':'human','reviewed_at':NOW.isoformat(),'languages_state':'known','languages_value':json.dumps([{'language':'en','cefr':'B2','cefr_comparator':'at_least'}])}
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'labels.csv'
            with path.open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
            reviews=load_reviews(path)
        self.assertEqual(evaluate_labels({'example':p},reviews)['languages']['precision'],1)
        self.assertEqual(reviews[0].labels['opportunity_type'].state,'unlabeled')

    def test_bad_benefit_label_shape_rejected(self):
        p=profile();labels={f:HumanLabel() for f in FIELDS};labels['other_compensation_benefits']=HumanLabel(state='known',value=[1000])
        with self.assertRaises(ValueError):HumanReview(job_id='example',profile_input_hash=p.metadata.input_hash,sample_snapshot='snapshot',labels=labels,reviewer_id='human',reviewed_at=NOW)

    def test_long_collapsed_description_not_entire_benefit_assertion(self):
        text=('Responsibilities and requirements for this role. '*40)+'Pension contribution 1000 CZK/month '+('More information. '*50)
        p=profile(description=text)
        self.assertLess(len(p.compensation.monetary_benefits.value[0]),400)
        self.assertIn('Pension contribution 1000 CZK/month',p.compensation.monetary_benefits.value[0])

    def test_hackathon_lead_role_abstains(self):
        self.assertIsNone(profile(title='Hackathon 2026 Lead').metadata.opportunity_type.value)

    def test_review_subset_is_deterministic_and_keeps_non_vacancy(self):
        jobs=[];profiles={}
        for i,title in enumerate(['Engineer','SF Quant Hackathon 2026','Join our Talent Community','Junior Engineer','Analyst','Specialist']):
            key=str(i);p=profile(title=title);p.metadata.job_id=key;profiles[key]=p
            jobs.append({'id':key,'title':title,'status':'active','primary_stratum':'ashby_direct','sources':[{'source_name':'ashby_direct','is_active':True,'last_seen_at':NOW.isoformat()}]})
        first=choose_subset(jobs,profiles,NOW.isoformat(),4);second=choose_subset(list(reversed(jobs)),profiles,NOW.isoformat(),4)
        self.assertEqual([j['id'] for j in first],[j['id'] for j in second])
        self.assertTrue({'1','2'}.issubset({j['id'] for j in first}))


class LabelBoundaryTests(unittest.TestCase):
    def test_implicit_fluency_cannot_have_human_cefr_comparator(self):
        from app.job_profile.human_evaluation import LabelLanguage
        with self.assertRaises(ValueError):LabelLanguage(language='en',cefr_comparator='at_least')

    def test_contradictory_human_bound_rejected(self):
        from app.job_profile.human_evaluation import LabelLanguage
        with self.assertRaises(ValueError):LabelLanguage(language='en',cefr='B2',cefr_comparator='exact',cefr_or_higher=True)


class OfferHashTests(unittest.TestCase):
    def test_unresolved_native_offer_label_changes_only_metadata_layers(self):
        raw={'title':'Engineer','content':'Role description.','pay_input_ranges':[{'title':'Band A','min_cents':8000000,'currency_type':'CZK'}]}
        a=profile(source='greenhouse_direct',raw=raw)
        raw['pay_input_ranges'][0]['title']='Band B'
        b=profile(source='greenhouse_direct',raw=raw)
        self.assertNotEqual(a.metadata.semantic_metadata_hash,b.metadata.semantic_metadata_hash)
        self.assertEqual(a.metadata.cleaned_description_hash,b.metadata.cleaned_description_hash)
        self.assertEqual(reprocessing_layers(a.metadata,b.metadata),{'native','selection','profile'})
        self.assertIsNone(b.compensation.offers.value[0].applicable_locations)
