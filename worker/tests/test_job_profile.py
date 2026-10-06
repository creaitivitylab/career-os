"""Offline profile contract/projection tests: no APIs or PostgreSQL."""
import copy
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import ValidationError

from app.job_profile.cleaning import clean_description, description_hash
from app.job_profile.evidence import EvidenceCollector
from app.job_profile.models import Fact, JobProfile, Method, SourceInput, ValueState
from app.job_profile.parsers import (employment_values, experience, languages,
    parse_location_label, salary, sections, technologies, title_facts)
from app.job_profile.pipeline import build_profile, reprocessing_layers
from app.job_profile.projectors import PROJECTORS, project_source
from app.job_profile.selection import DescriptionCandidate, select_description, select_fact
from app.job_profile.evaluate import choose_review_jobs


NOW = datetime(2026, 10, 5, tzinfo=timezone.utc)
LONG = "Responsibilities\n- Develop Python software.\nRequirements\n- English B2 required.\n" + "Build reliable services and collaborate with engineering colleagues. " * 12


def source(name="ashby_direct", raw=None, identity="board:123", **kwargs):
    return SourceInput(source_name=name, source_job_id=identity, raw_payload=raw or {},
                       is_active=kwargs.pop("is_active", True), last_seen_at=NOW, **kwargs)


def job(*sources):
    return {"id": "job-1", "sources": [s.model_dump(mode="json") for s in sources]}


class ContractTests(unittest.TestCase):
    def test_unknown_does_not_mean_empty_known_array(self):
        self.assertEqual(Fact().state, ValueState.UNKNOWN)
        for value in (None, [], ""):
            with self.assertRaises(ValidationError):
                Fact(state="known", value=value, evidence_ids=["x"])

    def test_false_is_known_when_supported(self):
        self.assertFalse(Fact[bool](state="known", value=False, evidence_ids=["x"]).value)

    def test_unavailable_states_have_no_values(self):
        for state in ("unknown", "not_mentioned", "insufficient_content"):
            with self.assertRaises(ValidationError):
                Fact(state=state, value="invented")

    def test_serialization_round_trip(self):
        profile = build_profile(job(source(raw={"title": "Senior Engineer", "descriptionPlain": LONG})), generated_at=NOW)
        self.assertEqual(JobProfile.model_validate_json(profile.model_dump_json()), profile)

    def test_evidence_requires_an_origin(self):
        with self.assertRaises(ValidationError):
            EvidenceCollector().add(source(), "test", "value")

    def test_invalid_typed_enum_rejected(self):
        profile = build_profile(job(source(raw={"title": "Engineer"}))).model_dump()
        profile["employment"]["schedule"] = {"state": "known", "value": "invented", "evidence_ids": ["x"]}
        with self.assertRaises(ValidationError):
            JobProfile.model_validate(profile)

    def test_dangling_evidence_is_rejected(self):
        profile = build_profile(job(source(raw={"title": "Engineer"}))).model_dump()
        profile["role"]["normalized_role"]["evidence_ids"] = ["missing"]
        with self.assertRaises(ValidationError):
            JobProfile.model_validate(profile)


class NativeProjectorTests(unittest.TestCase):
    def project(self, name, raw):
        evidence = EvidenceCollector()
        return project_source(source(name, raw), evidence), evidence

    def test_all_nine_sources_registered(self):
        self.assertEqual(len(PROJECTORS), 9)

    def test_smartrecruiters_hybrid_and_schedule_not_duration(self):
        p, e = self.project("smartrecruiters_direct", {"name": "Engineer", "id": "native",
            "location": {"city": "Praha", "country": "cz", "remote": False, "hybrid": True},
            "typeOfEmployment": {"id": "permanent", "label": "Full-time"}})
        self.assertEqual(p.facts["workplace"][0].value, "hybrid")
        self.assertEqual(p.facts["schedule"][0].value, "full_time")
        self.assertNotIn("duration", p.facts)
        self.assertEqual(p.locations[0].country.value, "CZ")
        self.assertTrue(all(v.native_field_path for v in e.records.values()))

    def test_greenhouse_configured_metadata_and_cents(self):
        p, _ = self.project("greenhouse_direct", {"id": 100, "content": LONG,
            "metadata": [{"name": "Location type:", "value": "Hybrid"}, {"name": "Level", "value": "P4"}],
            "pay_input_ranges": [{"min_cents": 8000000, "max_cents": 10000000, "currency_type": "CZK", "title": "Base Salary Range"}]})
        self.assertEqual(p.facts["native_id"][0].value, "100")
        self.assertEqual(p.facts["workplace"][0].value, "hybrid")
        self.assertNotIn("career_level", p.facts)
        self.assertEqual(p.offers[0].min_amount, Decimal(80000))
        self.assertEqual(p.offers[0].period, "unknown")

    def test_greenhouse_does_not_annualize_or_guess_minor_units(self):
        p, _ = self.project("greenhouse_direct", {"pay_input_ranges": [{"min_cents": 1000, "currency_type": "JPY"}]})
        self.assertEqual(p.offers, [])

    def test_workable_ignores_ingestion_czech_default(self):
        p, _ = self.project("workable_direct", {"title": "Engineer", "telecommuting": False,
            "_czech_location": True, "education": "Unspecified", "employment_type": "Part-time", "shortcode": "native"})
        self.assertNotIn("workplace", p.facts)
        self.assertNotIn("education", p.facts)
        self.assertEqual(p.locations, [])
        self.assertEqual(p.facts["schedule"][0].value, "part_time")

    def test_ashby_multiple_locations_and_components(self):
        p, _ = self.project("ashby_direct", {"title": "Engineer", "location": "Prague", "workplaceType": "OnSite",
            "secondaryLocations": [{"location": "Brno", "address": {"postalAddress": {"addressCountry": "CZ"}}}],
            "compensation": {"summaryComponents": [{"compensationType": "Salary", "minValue": 80000, "currencyCode": "CZK", "interval": "MONTHLY"},
                {"compensationType": "Bonus", "minValue": 5000, "currencyCode": "CZK", "interval": "YEARLY"}]}})
        self.assertEqual(len(p.locations), 2)
        self.assertEqual(p.locations[1].kind, "additional")
        self.assertEqual(p.facts["workplace"][0].value, "onsite")
        self.assertEqual([o.component for o in p.offers], ["base", "bonus"])

    def test_lever_identity_and_one_time_reward(self):
        p, _ = self.project("lever_direct", {"id": "native", "text": "Voice task", "country": "CZ",
            "salaryRange": {"min": 15, "max": 15, "currency": "USD", "interval": "one-time"}})
        self.assertEqual(p.facts["native_id"][0].value, "native")
        self.assertEqual(p.offers[0].component, "task_reward")

    def test_workday_native_id_not_requisition(self):
        p, _ = self.project("workday_direct", {"detail": {"jobPostingInfo": {"id": "posting", "jobReqId": "REQ123",
            "country": {"descriptor": "Germany"}, "location": "Berlin", "additionalLocations": ["Czech Republic, EMEA"], "timeType": "Full time"}}})
        self.assertEqual(p.facts["native_id"][0].value, "posting")
        self.assertEqual(p.locations[0].country.value, "DE")
        self.assertEqual(p.locations[1].country.value, "CZ")

    def test_older_workday_shape_supported(self):
        p, _ = self.project("workday_direct", {"jobPostingInfo": {"id": "posting", "title": "Engineer"}})
        self.assertEqual(p.facts["native_id"][0].value, "posting")

    def test_rmk_ignores_overloaded_custom_fields(self):
        p, _ = self.project("successfactors_direct", {"detail": {"posting_id": "100", "internal_requisition_locale_id": "200_en_US",
            "native_fields": {"shift": "Remote", "customfield2": "Full-Time"},
            "locations": [{"text": "Brno, CZ, 617 00"}], "description": LONG}})
        self.assertEqual(p.facts["native_id"][0].value, "100")
        self.assertNotIn("schedule", p.facts)
        self.assertNotIn("workplace", p.facts)
        self.assertEqual(p.locations[0].city.value, "Brno")

    def test_fantastic_ai_is_suggestion_not_known_skill(self):
        p, e = self.project("fantastic_jobs_apify", {"title": "Engineer", "ai_key_skills": ["Python"],
            "ai_experience_level": "5-10", "countries_derived": ["CZ"], "description_text": LONG})
        self.assertNotIn("required_skills", p.facts)
        self.assertEqual(p.locations, [])
        self.assertTrue(all(v.extraction_method == Method.SUGGESTION and v.validation_state == "unvalidated"
                            for v in e.records.values() if v.field in {"required_skills", "career_level"}))

    def test_fantastic_addresses_are_provider_evidence(self):
        p, e = self.project("fantastic_jobs_apify", {"locations": [{"address": {"addressCountry": "CZ", "addressLocality": "Prague"}}]})
        key = p.locations[0].country.evidence_ids[0]
        self.assertEqual(e.records[key].extraction_method, Method.PROVIDER)

    def test_jooble_identity_is_not_native_id(self):
        p, _ = self.project("jooble_direct", {"id": "provider", "title": "Engineer", "snippet": "short...", "location": "Prague"})
        self.assertNotIn("native_id", p.facts)
        self.assertEqual(p.locations[0].city.value, "Prague")

    def test_jooble_salary_fills_gap_without_inventing_period_or_origin(self):
        p, e = self.project("jooble_direct", {"salary": "29000 - 37000 Kč"})
        self.assertEqual(p.offers[0].min_amount, Decimal(29000))
        self.assertEqual(p.offers[0].max_amount, Decimal(37000))
        self.assertEqual(p.offers[0].period, "unknown")
        self.assertEqual(p.offers[0].explicitness, "unknown")
        self.assertEqual(e.records[p.offers[0].evidence_ids[0]].extraction_method, Method.PROVIDER)

    def test_provider_document_language_not_required_language(self):
        profile = build_profile(job(source("fantastic_jobs_apify", {"title": "Engineer", "ai_job_language": "en", "ai_experience_level": "5-10"})))
        self.assertIsNone(profile.requirements.languages.value)
        self.assertIsNone(profile.role.career_level.value)
        self.assertIn("document_language", {e.field for e in profile.evidence})

    def test_canonical_defaults_not_projected(self):
        data = job(source(raw={"title": "Engineer"}))
        data.update(country_code="CZ", remote_type="onsite", salary_min=80000, skills=["Python"])
        profile = build_profile(data)
        self.assertIsNone(profile.workplace.locations.value)
        self.assertIsNone(profile.workplace.mode.value)
        self.assertIsNone(profile.compensation.offers.value)
        self.assertIsNone(profile.requirements.required_skills.value)

    def test_empty_older_and_unknown_payloads_do_not_invent_values(self):
        for name in [*PROJECTORS, "unsupported"]:
            p, _ = self.project(name, {})
            self.assertEqual(p.facts, {})
            self.assertEqual(p.offers, [])


class SelectionTests(unittest.TestCase):
    def test_native_address_precedes_provider(self):
        e = EvidenceCollector()
        native = e.fact(source(), "country", "CZ", path="address.country")
        provider = e.fact(source("fantastic_jobs_apify"), "country", "DE", path="locations", method=Method.PROVIDER)
        value = select_fact("country", [provider, native], e)
        self.assertEqual(value.value, "CZ")
        self.assertEqual(len(value.evidence_ids), 2)

    def test_native_location_parser_precedes_provider_address(self):
        e = EvidenceCollector()
        parsed = e.fact(source(), "city", "Prague", path="location", method=Method.PARSER)
        provider = e.fact(source("fantastic_jobs_apify"), "city", "Brno", path="locations", method=Method.PROVIDER)
        self.assertEqual(select_fact("city", [provider, parsed], e).value, "Prague")

    def test_equal_quality_disagreement_is_conflict(self):
        e = EvidenceCollector()
        a = e.fact(source(), "workplace", "hybrid", path="workplaceType")
        b = e.fact(source("lever_direct"), "workplace", "remote", path="workplaceType")
        result = select_fact("workplace", [a, b], e)
        self.assertEqual(result.state, ValueState.CONFLICT)
        self.assertIsNone(result.value)
        self.assertEqual(len(result.evidence_ids), 2)

    def test_conflicting_locations_are_preserved(self):
        profile = build_profile(job(source(raw={"address": {"postalAddress": {"addressCountry": "CZ"}}}),
            source("lever_direct", {"country": "DE"})))
        self.assertEqual(profile.workplace.locations.state, ValueState.CONFLICT)
        self.assertEqual({l.country.value for l in profile.workplace.locations.value}, {"CZ", "DE"})

    def test_native_title_marker_precedes_provider_title_marker(self):
        profile = build_profile(job(source(raw={"title": "Junior Engineer"}),
            source("fantastic_jobs_apify", {"title": "Senior Engineer"})))
        self.assertEqual(profile.role.career_level.value, "junior")
        self.assertEqual(len(profile.role.career_level.evidence_ids), 2)

    def test_complete_native_beats_longer_fantastic_and_jooble(self):
        selected, alts = select_description([
            DescriptionCandidate(source("jooble_direct"), LONG*3, "snippet"),
            DescriptionCandidate(source("fantastic_jobs_apify"), LONG*2, "description_text"),
            DescriptionCandidate(source(), LONG, "descriptionPlain")])
        self.assertEqual(selected[1].source.source_name, "ashby_direct")
        self.assertEqual(sum(a.selected for a in alts), 1)

    def test_complete_fantastic_beats_native_snippet(self):
        selected, _ = select_description([DescriptionCandidate(source(), "Python engineer...", "description"),
            DescriptionCandidate(source("fantastic_jobs_apify"), LONG, "description_text")])
        self.assertEqual(selected[1].source.source_name, "fantastic_jobs_apify")

    def test_template_shell_rejected(self):
        selected, _ = select_description([DescriptionCandidate(source(), "Apply now", "description")])
        self.assertIsNone(selected)

    def test_snippet_only_has_insufficient_negative_state(self):
        p = build_profile(job(source("jooble_direct", {"title": "Engineer", "snippet": "Python..."})))
        self.assertEqual(p.requirements.languages.state, ValueState.INSUFFICIENT)
        self.assertEqual(p.requirements.technologies.value[0].kind, "mention")


class CleanerTests(unittest.TestCase):
    def test_double_encoded_greenhouse_html(self):
        self.assertEqual(clean_description("&amp;lt;h2&amp;gt;Requirements&amp;lt;/h2&amp;gt;&amp;lt;ul&amp;gt;&amp;lt;li&amp;gt;C++ &amp;amp; C#&amp;lt;/li&amp;gt;&amp;lt;/ul&amp;gt;"), "Requirements\n- C++ & C#")

    def test_remove_navigation_script_and_session_markup(self):
        raw = '<nav>Menu</nav><script>secret()</script><h2>Benefits</h2><p data-session="token">5 weeks</p><div class="cookie-banner">Cookies</div>'
        self.assertEqual(clean_description(raw), "Benefits\n5 weeks")

    def test_headings_bullets_breaks_preserved(self):
        self.assertEqual(clean_description("<h2>What you'll do</h2><ul><li>Python</li><li>SQL</li></ul>"), "What you'll do\n- Python\n- SQL")

    def test_hash_ignores_html_attributes_and_whitespace(self):
        self.assertEqual(description_hash(clean_description('<p class="x">Hello   world</p>')),
                         description_hash(clean_description('<p id="other">Hello world</p>')))

    def test_foreign_broad_location_not_forced_to_czech(self):
        for label in ("EMEA", "Europe", "Remote", "Worldwide"):
            self.assertEqual(parse_location_label(label), {})


class ParserTests(unittest.TestCase):
    def run_parser(self, fn, text):
        e = EvidenceCollector()
        return fn(text, source(), e), e

    def test_title_marker_variants(self):
        for title, expected in [("Senior Engineer", "senior"), ("Sr. Engineer", "senior"), ("Junior Engineer", "junior"), ("Jr Developer", "junior")]:
            values, _ = self.run_parser(title_facts, title)
            self.assertEqual(values["career_level"][0].value, expected)
            self.assertNotIn(expected, values["normalized_role"].value.lower())

    def test_manager_lead_head_director_vedouci_not_people_management(self):
        for title in ("Engineering Manager", "Lead Engineer", "Head of Engineering", "Director", "Vedoucí výroby"):
            profile = build_profile(job(source(raw={"title": title})))
            self.assertEqual(profile.role.people_management.state, ValueState.UNKNOWN)
            self.assertEqual(profile.role.management_track.state, ValueState.UNKNOWN)
            self.assertTrue(profile.role.leadership_markers.value)
            self.assertIsNone(profile.requirements.experience.value)

    def test_lead_generation_is_not_leadership_marker(self):
        value, _ = self.run_parser(title_facts, "Lead Generation Specialist")
        self.assertNotIn("leadership_markers", value)

    def test_unmarked_title_not_mid_level(self):
        value, _ = self.run_parser(title_facts, "Software Engineer")
        self.assertEqual(value["career_level"], [])

    def test_technology_boundaries_and_punctuation(self):
        values, _ = self.run_parser(technologies, "JavaScript, Java, C++, C#, SQLAlchemy, GitHub, Pythonic and Python.")
        self.assertEqual({v.technology for v in values}, {"JavaScript", "Java", "C++", "C#", "Python"})

    def test_technology_mention_not_requirement(self):
        profile = build_profile(job(source(raw={"title": "Engineer", "descriptionPlain": "Our employer uses Python in marketing."})))
        self.assertEqual(profile.requirements.technologies.value[0].kind, "mention")
        self.assertEqual(profile.requirements.required_skills.state, ValueState.UNKNOWN)

    def test_ordinary_react_and_excel_verbs_not_tool_mentions(self):
        values, _ = self.run_parser(technologies, "We excel at service and react quickly to customers.")
        self.assertEqual(values, [])
        values, _ = self.run_parser(technologies, "Experience with excel and React Native; C++17.")
        self.assertEqual({v.technology for v in values}, {"Excel", "React", "C++"})

    def test_explicit_cefr_and_no_fluent_conversion(self):
        values, _ = self.run_parser(languages, "English B2\nEnglish C1+\nfluent English\nadvanced English\nprofessional English")
        self.assertEqual([v.cefr for v in values], ["B2", "C1", None, None, None])
        self.assertTrue(values[1].cefr_or_higher)

    def test_language_required_preferred_and_country_exclusion(self):
        values, _ = self.run_parser(languages, "German required\nEnglish preferred\nCzech Republic\nCzech required")
        self.assertEqual([(v.language, v.requirement) for v in values], [("de", "required"), ("en", "preferred"), ("cs", "required")])

    def test_shared_language_clause(self):
        values, _ = self.run_parser(languages, "German and Czech language is required, English highly preferred.")
        self.assertEqual([(v.language, v.requirement) for v in values], [("de", "required"), ("cs", "required"), ("en", "preferred")])

    def test_cefr_not_borrowed_from_neighbor_language(self):
        values, _ = self.run_parser(languages, "English B2 and German C1 required")
        self.assertEqual([(v.language, v.cefr) for v in values], [("en", "B2"), ("de", "C1")])

    def test_language_course_benefit_not_language_requirement(self):
        values, _ = self.run_parser(languages, "English language courses are provided.")
        self.assertEqual(values, [])

    def test_experience_explicit_forms(self):
        for statement, minimum, maximum in [("3+ years of experience", 3, None), ("at least 5 years experience", 5, None),
            ("minimum 2 years of experience", 2, None), ("2–4 years of experience", 2, 4), ("two years of experience", 2, None)]:
            values, _ = self.run_parser(experience, statement)
            self.assertEqual(values[0].min_years, Decimal(minimum))
            self.assertEqual(values[0].max_years, Decimal(maximum) if maximum else None)

    def test_experience_does_not_parse_company_age(self):
        values, _ = self.run_parser(experience, "Our company was founded 20 years ago.\nSenior Manager")
        self.assertEqual(values, [])

    def test_salary_examples(self):
        for statement, minimum, maximum, currency, period in [
            ("80,000–100,000 CZK/month", 80000, 100000, "CZK", "month"),
            ("90k CZK monthly", 90000, None, "CZK", "month"),
            ("€60,000 per year", 60000, None, "EUR", "year"),
            ("500 CZK/hour", 500, None, "CZK", "hour")]:
            values, _ = self.run_parser(salary, statement)
            self.assertEqual((values[0].min_amount, values[0].max_amount, values[0].currency, values[0].period),
                (Decimal(minimum), Decimal(maximum) if maximum else None, currency, period))

    def test_salary_offers_remain_location_specific(self):
        values, _ = self.run_parser(salary, "Prague: 80,000–100,000 CZK/month\nParis: €60,000 per year")
        self.assertEqual(len(values), 2)
        self.assertEqual(values[0].applicable_locations, ["Prague"])
        self.assertEqual(values[1].applicable_locations, ["Paris"])

    def test_salary_rejects_benefit_revenue_reward_and_budget(self):
        for context in ("Referral bonus", "Meal vouchers", "Revenue", "Training budget", "Signing bonus", "One-time task reward"):
            values, _ = self.run_parser(salary, context+": 90k CZK monthly")
            self.assertEqual(values, [], context)

    def test_salary_unknown_symbol_and_missing_interval_not_invented(self):
        for statement in ("$90,000 monthly", "90,000 CZK", "90,000 per month"):
            values, _ = self.run_parser(salary, statement)
            self.assertEqual(values, [])

    def test_zero_pay_not_a_positive_offer(self):
        values, _ = self.run_parser(salary, "Salary: 0 CZK/month")
        self.assertEqual(values, [])

    def test_sections_czech_and_english_spans(self):
        raw = "What you'll do\n- Build Python\nPožadujeme\n- English B2\nNabízíme\n- Vacation"
        values, _ = self.run_parser(sections, raw)
        self.assertEqual([v.kind for v in values], ["responsibilities", "requirements", "benefits"])
        self.assertEqual(values[0].content, "- Build Python")
        self.assertEqual(raw[values[1].start:values[1].end].splitlines()[0], "Požadujeme")

    def test_ambiguous_paragraphs_are_not_section_classified(self):
        values, _ = self.run_parser(sections, "We offer a complex product to our customers.")
        self.assertEqual(values, [])

    def test_employment_axes_are_independent(self):
        self.assertEqual(employment_values("Full-time"), {"schedule": "full_time"})
        self.assertEqual(employment_values("Contractor fixed-term part-time"),
                         {"schedule": "part_time", "relationship": "contractor", "duration": "fixed_term"})


class HashingTests(unittest.TestCase):
    def test_volatile_source_metadata_does_not_change_hashes(self):
        original = job(source(raw={"title": "Engineer", "descriptionHtml": '<p data-session="a">'+LONG+'</p>', "_lifecycle": {"run_id": "old"}}))
        changed = copy.deepcopy(original)
        changed["sources"][0]["last_seen_at"] = "2026-10-06T00:00:00Z"
        changed["sources"][0]["raw_payload"]["_lifecycle"] = {"run_id": "new"}
        changed["sources"][0]["raw_payload"]["csrf"] = "ephemeral"
        changed["sources"][0]["raw_payload"]["descriptionHtml"] = '<p data-session="b">'+LONG+'</p>'
        a, b = build_profile(original), build_profile(changed)
        self.assertEqual(a.metadata.input_hash, b.metadata.input_hash)
        self.assertEqual(reprocessing_layers(a.metadata, b.metadata), set())

    def test_metadata_change_does_not_rerun_text(self):
        original = job(source(raw={"title": "Engineer", "descriptionPlain": LONG, "department": "Engineering"}))
        changed = copy.deepcopy(original)
        changed["sources"][0]["raw_payload"]["department"] = "Product"
        self.assertEqual(reprocessing_layers(build_profile(original).metadata, build_profile(changed).metadata), {"native", "selection", "profile"})

    def test_text_change_reruns_text_layer(self):
        a = build_profile(job(source(raw={"title": "Engineer", "descriptionPlain": LONG})))
        b = build_profile(job(source(raw={"title": "Engineer", "descriptionPlain": LONG+"\nSQL required."})))
        self.assertEqual(reprocessing_layers(a.metadata, b.metadata), {"text", "selection", "profile"})

    def test_evidence_preserves_source_path_hash_time_and_spans(self):
        profile = build_profile(job(source(raw={"title": "Engineer", "descriptionPlain": LONG})))
        for e in profile.evidence:
            self.assertEqual(e.source_name, "ashby_direct")
            self.assertEqual(e.source_job_id, "board:123")
            self.assertEqual(e.observed_at, NOW)
            self.assertEqual(len(e.input_hash), 64)
            self.assertTrue(e.native_field_path or e.text_span)
            if e.text_span and e.field != "career_level":
                text = profile.content.cleaned_description.value
                self.assertEqual(text[e.text_span.start:e.text_span.end], e.text_span.text)

    def test_source_input_order_is_stable(self):
        a = source(raw={"title": "Engineer", "descriptionPlain": LONG})
        b = source("fantastic_jobs_apify", {"title": "Engineer", "description_text": LONG})
        self.assertEqual(build_profile(job(a, b)).metadata.input_hash, build_profile(job(b, a)).metadata.input_hash)

    def test_parser_version_invalidates_title_and_text(self):
        a = build_profile(job(source(raw={"title": "Engineer", "descriptionPlain": LONG}))).metadata
        b = a.model_copy(deep=True)
        b.versions["parser"] = "new"
        b.input_hash = "changed"
        self.assertEqual(reprocessing_layers(a, b), {"native", "text", "selection", "profile"})

    def test_review_set_selection_reproducible_not_ai_labels(self):
        jobs = []
        for i in range(6):
            data = job(source(raw={"title": "Engineer", "descriptionPlain": LONG}))
            data.update(id=str(i), status="active", primary_stratum="ashby_direct", company_key=str(i),
                        _strata={"language_proxy": "en" if i % 2 else "cs"})
            jobs.append(data)
        profiles = {data["id"]: build_profile(data) for data in jobs}
        first = choose_review_jobs(jobs, profiles, NOW.isoformat(), 3)
        second = choose_review_jobs(list(reversed(jobs)), profiles, NOW.isoformat(), 3)
        self.assertEqual([j["id"] for j in first], [j["id"] for j in second])
        self.assertEqual(len(first), 3)
