"""Offline discovery acknowledgement; never acquisition or lifecycle approval."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.adapters.successfactors import RMKSite
from app.direct_freshness import aggregate_status, normalize_outcome
from app.freshness_report import generate_report
from app.heavy_scope_health import evaluate_source, load_policy
from app.rmk_ownership import reviewed_identity
from app.successfactors_scopes import load_scope_gate


SOURCE = 'successfactors_direct'
NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
FIXTURE = json.loads((Path(__file__).parent / 'fixtures/rmk_discovery_backlog.json').read_text())
NEW = FIXTURE['new_scopes']
ALDI = ['jobs.aldi-hofer.com', 'aldi-karrier-hu']


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def scope_result():
    scope = ['kariera.spravazeleznic.cz', 'spravazel', '']
    return {'host': scope[0], 'brand': '', 'status': 'success', 'failed': 0,
            'listing_records': 2, 'lifecycle': {'scope': scope, 'successful': True,
            'complete': True, 'deactivated': 0, 'reactivated': 0, 'reason': 'exact complete native inventory'}}


def native_result():
    row = scope_result()
    return {'status': 'success', 'run_id': 'offline-only', 'sites': 1, 'failed': 0,
            'listing_records': 2, 'created': 0, 'updated': 0, 'attached': 0,
            'site_results': [row], 'site_errors': [], 'unapproved_discovered_scopes': [],
            'excluded_scopes': [], 'lifecycle': {'scopes': [row['lifecycle']], 'deactivated': 0, 'reactivated': 0}}


def review_for(scope):
    return next(r for r in load_policy()['discovery_backlog'] if r['scope'] == scope)


def with_discovery(scope, category=None):
    result = native_result()
    review = review_for(scope)
    category = category or review['category']
    row = {'host': scope[0], 'brand': scope[1]}
    if category == 'excluded_scopes':
        row['reason'] = review['reason']
    result[category] = [row]
    return result


class DiscoveryBacklogTests(unittest.TestCase):
    def test_new_unreviewed_discovery_is_partial(self):
        result = with_discovery(NEW[0])
        p = load_policy(); p['discovery_backlog'] = []
        evaluation = evaluate_source(SOURCE, result, NOW, p)
        self.assertEqual(evaluation['status'], 'PARTIAL')
        self.assertTrue(evaluation['discovery_backlog'][0]['alert_worthy'])

    def test_reviewed_needs_review_is_visible_known_gap(self):
        evaluation = evaluate_source(SOURCE, with_discovery(NEW[0]), NOW)
        self.assertEqual(evaluation['status'], 'SUCCESS_WITH_KNOWN_GAPS')
        gap = evaluation['discovery_backlog'][0]
        self.assertEqual(gap['state'], 'NEEDS_REVIEW')
        self.assertTrue(gap['known_gap'])
        self.assertEqual(gap['coverage'], 'NON_AUTHORITATIVE_EXPECTED')
        self.assertFalse(gap['acquisition_allowed'])
        self.assertFalse(gap['lifecycle_eligible'])

    def test_reviewed_exclusion_retains_category_and_reason(self):
        evaluation = evaluate_source(SOURCE, with_discovery(ALDI), NOW)
        self.assertEqual(evaluation['status'], 'SUCCESS_WITH_KNOWN_GAPS')
        gap = evaluation['discovery_backlog'][0]
        self.assertEqual(gap['category'], 'excluded_scopes')
        self.assertEqual(gap['reason'], review_for(ALDI)['reason'])
        self.assertTrue(gap['known_gap'])
        self.assertFalse(gap['lifecycle_eligible'])

    def test_known_unavailable_uses_existing_state_vocabulary(self):
        p = load_policy()
        next(r for r in p['discovery_backlog'] if r['scope'] == NEW[0])['state'] = 'KNOWN_UNAVAILABLE'
        evaluation = evaluate_source(SOURCE, with_discovery(NEW[0]), NOW, p)
        self.assertEqual(evaluation['discovery_backlog'][0]['state'], 'KNOWN_UNAVAILABLE')
        self.assertEqual(evaluation['status'], 'SUCCESS_WITH_KNOWN_GAPS')

    def test_review_expiry_is_partial_and_does_not_lift_gate(self):
        expiry = datetime.fromisoformat(review_for(NEW[0])['review_expires_at'])
        evaluation = evaluate_source(SOURCE, with_discovery(NEW[0]), expiry)
        self.assertEqual(evaluation['status'], 'PARTIAL')
        self.assertFalse(evaluation['discovery_backlog'][0]['known_gap'])
        self.assertFalse(evaluation['discovery_backlog'][0]['lifecycle_eligible'])

    def test_future_review_is_not_effective(self):
        evaluation = evaluate_source(SOURCE, with_discovery(NEW[0]), NOW - timedelta(seconds=1))
        self.assertEqual(evaluation['status'], 'PARTIAL')

    def test_changed_host_alerts(self):
        result = with_discovery(NEW[0]); result['unapproved_discovered_scopes'][0]['host'] = 'other.example.com'
        self.assertEqual(evaluate_source(SOURCE, result, NOW)['status'], 'PARTIAL')

    def test_changed_brand_alerts(self):
        result = with_discovery(NEW[0]); result['unapproved_discovered_scopes'][0]['brand'] = 'other-market'
        self.assertEqual(evaluate_source(SOURCE, result, NOW)['status'], 'PARTIAL')

    def test_new_reported_coverage_is_not_silently_covered(self):
        result = with_discovery(NEW[0])
        result['unapproved_discovered_scopes'][0]['coverage_signature'] = {'locale': 'unreviewed'}
        self.assertEqual(evaluate_source(SOURCE, result, NOW)['status'], 'PARTIAL')

    def test_changed_exclusion_reason_alerts(self):
        result = with_discovery(ALDI); result['excluded_scopes'][0]['reason'] = 'changed evidence'
        self.assertEqual(evaluate_source(SOURCE, result, NOW)['status'], 'PARTIAL')

    def test_changed_category_requires_review(self):
        self.assertEqual(evaluate_source(SOURCE, with_discovery(ALDI, 'unapproved_discovered_scopes'), NOW)['status'], 'PARTIAL')

    def test_wrong_source_review_cannot_cover_rmk_backlog(self):
        p = load_policy(); row = next(r for r in p['discovery_backlog'] if r['scope'] == NEW[0])
        row['source_name'] = 'workday_direct'
        self.assertEqual(evaluate_source(SOURCE, with_discovery(NEW[0]), NOW, p)['status'], 'PARTIAL')

    def test_invalid_backlog_category_is_rejected(self):
        p = load_policy(); p['discovery_backlog'][0]['category'] = 'automatic_approval'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'policy.json'; path.write_text(json.dumps(p))
            with self.assertRaises(ValueError): load_policy(path)

    def test_each_concrete_scope_has_bounded_explicit_review(self):
        for scope in NEW:
            with self.subTest(scope=scope):
                review = review_for(scope)
                self.assertEqual(review['reviewed_at'], NOW.isoformat())
                self.assertEqual(review['review_expires_at'], '2026-11-19T00:00:00+00:00')
                self.assertTrue(review['reason'] and review['evidence'])
                self.assertEqual(review['state'], 'NEEDS_REVIEW')

    def test_no_discovery_is_promoted_to_validated_gate(self):
        gate = load_scope_gate()
        approved = {(r.host, r.brand) for r in gate.scopes}
        self.assertTrue(all(tuple(scope) not in approved for scope in NEW))
        self.assertEqual(len(gate.scopes), 62)
        discovered = [RMKSite(r.host, r.brand, 'https://' + r.host + '/', 'fixture') for r in gate.scopes]
        discovered += [RMKSite(host, brand, 'https://' + host + '/', 'fixture') for host, brand in NEW]
        self.assertEqual(len(gate.select(discovered)), 62)
        self.assertTrue(all((r.host, r.brand) not in {tuple(s) for s in NEW} for r in gate.select(discovered)))

    def test_aldi_exclusion_is_exact_brand_not_entire_shared_host(self):
        gate = load_scope_gate()
        self.assertEqual([(r.host, r.brand) for r in gate.excluded_scopes if r.host == ALDI[0]], [tuple(ALDI)])
        self.assertEqual(len(gate.excluded_scopes), 8)

    def test_review_does_not_grant_ownership_authority(self):
        for evidence in FIXTURE['public_evidence']:
            if evidence['search_url']:
                self.assertFalse(reviewed_identity([evidence['scope'][0], evidence['tenant'], evidence['native_brand']]))

    def test_all_existing_scope_health_policies_unchanged(self):
        raw = json.loads((Path(__file__).parents[1] / 'app/config/heavy_scope_health.json').read_text())
        self.assertEqual(digest(raw['scopes']), FIXTURE['existing_policy_scope_reviews_sha256'])
        self.assertEqual(digest(raw['review']), FIXTURE['existing_default_review_sha256'])
        self.assertEqual(digest(raw['discovery_backlog'][:-4]), FIXTURE['existing_backlog_reviews_sha256'])

    def test_validated_and_previous_excluded_scopes_unchanged(self):
        raw = json.loads((Path(__file__).parents[1] / 'app/config/successfactors_scopes.json').read_text())
        self.assertEqual(digest(raw['scopes']), FIXTURE['validated_rmk_sha256'])
        self.assertEqual(digest(raw['excluded_scopes'][:-1]), FIXTURE['old_excluded_rmk_sha256'])

    def test_all_four_produce_known_gaps_without_mutating_input(self):
        native = native_result()
        for scope in NEW:
            review = review_for(scope); row = {'host': scope[0], 'brand': scope[1]}
            if review['category'] == 'excluded_scopes': row['reason'] = review['reason']
            native[review['category']].append(row)
        before = deepcopy(native)
        self.assertEqual(evaluate_source(SOURCE, native, NOW)['status'], 'SUCCESS_WITH_KNOWN_GAPS')
        self.assertEqual(native, before)
        outcome = normalize_outcome(SOURCE, native, now=NOW)
        self.assertEqual(outcome['status'], 'SUCCESS_WITH_KNOWN_GAPS')
        self.assertEqual(aggregate_status([outcome]), 'SUCCESS_WITH_KNOWN_GAPS')

    def test_real_quality_fault_is_not_hidden_by_reviewed_discovery(self):
        native = with_discovery(NEW[0]); row = native['site_results'][0]
        row.update(failed=1, missing_identity=1); native['failed'] = 1
        row['lifecycle']['complete'] = False
        self.assertEqual(evaluate_source(SOURCE, native, NOW)['status'], 'PARTIAL')

    def workday_discovery_result(self, complete):
        # One of the 16 newly selected scopes in the captured deployment plan.
        scope = ['3m.wd1.myworkdayjobs.com', '3m', 'Search']
        native = native_result()
        row = native['site_results'][0]
        row.update(host=scope[0], tenant=scope[1], site=scope[2],
                   discovery_mode='unfiltered' if complete else 'facets')
        row['lifecycle'].update(scope=scope, complete=complete)
        return native

    def test_new_workday_exact_complete_scope_does_not_require_backlog_review(self):
        result = evaluate_source('workday_direct', self.workday_discovery_result(True), NOW)
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertTrue(result['scopes'][0]['lifecycle_eligible'])
        self.assertEqual(result['discovery_backlog'], [])

    def test_new_workday_filtered_scope_without_review_remains_partial(self):
        result = evaluate_source('workday_direct', self.workday_discovery_result(False), NOW)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertFalse(result['scopes'][0]['lifecycle_eligible'])
        self.assertIn('unexpected_coverage_gap', result['scopes'][0]['alerts'])

    def test_report_keeps_reviewed_exclusion_visible(self):
        native = with_discovery(ALDI)
        run = {'id': 'offline-only', 'source_name': SOURCE, 'status': 'success', 'started_at': NOW, 'metadata': native}
        report = generate_report([], [], [run], NOW)
        backlog = report['heavy_scope_health'][SOURCE]['discovery_backlog'][0]
        self.assertEqual(backlog['category'], 'excluded_scopes')
        self.assertTrue(backlog['known_gap'])
        self.assertEqual(backlog['review']['review_expires_at'], '2026-11-19T00:00:00+00:00')
        self.assertFalse(backlog['lifecycle_eligible'])


if __name__ == '__main__':
    unittest.main()
