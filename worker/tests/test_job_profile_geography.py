"""Geography semantics, source trust, raw evidence and version invalidation."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from app.job_profile.evidence import EvidenceCollector
from app.job_profile.geography import city, country_code, parse_label, region
from app.job_profile.models import Fact, JobProfile, Location, SourceInput, ValueState
from app.job_profile.pipeline import build_profile, reprocessing_layers
from app.job_profile.projectors import project_source
from app.job_profile.versions import versions


def source(name="ashby_direct", **raw):
    return SourceInput(source_name=name, source_job_id="site:native", is_active=True, raw_payload=raw)


def profile(*sources):
    return build_profile({"id": "offline", "sources": [s.model_dump() for s in sources]})


class GeographyNormalizerTests(unittest.TestCase):
    def test_country_forms(self):
        for raw in ("CZ", "CZE", "Czech Republic", "Czechia", "Česká republika"):
            with self.subTest(raw=raw):
                self.assertEqual(country_code(raw), "CZ")
                self.assertIsNone(region(raw, "CZ"))

    def test_general_iso_countries(self):
        for raw, expected in (("Zambia", "ZM"), ("Singapore", "SG"), ("Brazil", "BR"), ("JPN", "JP")):
            self.assertEqual(country_code(raw), expected)

    def test_czech_city_aliases(self):
        for raw, expected in (("Praha", "Prague"), ("Prague", "Prague"), ("Pilsen", "Plzeň"),
                ("Plzeň", "Plzeň"), ("Kutná Hora", "Kutná Hora"), ("Kutna Hora", "Kutná Hora"),
                ("Rožnov pod Radhoštěm", "Rožnov pod Radhoštěm"), ("roznov pod radhostem", "Rožnov pod Radhoštěm"),
                ("Brno", "Brno"), ("Ostrava", "Ostrava"), ("Pardubice", "Pardubice")):
            self.assertEqual(parse_label(raw), {"city": expected, "country": "CZ"})

    def test_region_and_city_semantics(self):
        self.assertEqual(region("South Moravian Region", "CZ"), "Jihomoravský kraj")
        self.assertEqual(region("CZ-64", "CZ"), "Jihomoravský kraj")
        self.assertEqual(region("Prague", "CZ"), "Praha, Hlavní město")
        self.assertEqual(city("Prague", "CZ"), "Prague")
        self.assertIsNone(city("Jihomoravský kraj", "CZ"))
        self.assertIsNone(region("Brno", "CZ"))

    def test_iso_district_is_not_a_city_dictionary(self):
        self.assertIsNone(parse_label("Benešov").get("city"))
        self.assertEqual(region("CZ-201", "CZ"), "Benešov")

    def test_foreign_subdivision_country_name_collision(self):
        self.assertEqual(region("Georgia", "US"), "Georgia")
        self.assertIsNone(region("Georgia", "CZ"))
        self.assertEqual(region("Scotland", "GB"), "Scotland")

    def test_weak_label_requires_recognition(self):
        for value in ("Unverified facility", "Northern campus", "Engineering", "Jobs.cz", "Other.jobs.example",
                      "Group career pages", "Careers portal", "Job board", "https://example.org/jobs"):
            with self.subTest(value=value):
                self.assertEqual(parse_label(value), {})

    def test_workplace_and_broad_regions_are_not_geography(self):
        for value in ("Remote", "Hybrid", "Onsite", "Europe", "EU", "EMEA", "Worldwide"):
            self.assertEqual(parse_label(value), {})
            self.assertIsNone(city(value, "CZ"))
            self.assertIsNone(region(value, "CZ"))

    def test_country_context_does_not_make_unknown_label_a_place(self):
        self.assertEqual(parse_label("Engineering", "CZ"), {})

    def test_prose_city_mention_is_not_geography(self):
        self.assertEqual(parse_label("Our teams work with colleagues in Prague"), {})

    def test_multiple_cities_not_flattened(self):
        self.assertEqual(parse_label("Prague / Brno"), {})

    def test_foreign_country_does_not_acquire_czech_city(self):
        self.assertEqual(parse_label("Prague, Germany"), {"country": "DE"})
        self.assertIsNone(city("Prague", "DE"))

    def test_explicit_country_with_remote_scope_survives(self):
        self.assertEqual(parse_label("Czech Republic, EMEA"), {"country": "CZ"})

    def test_numbered_city_district_keeps_parent_city(self):
        self.assertEqual(parse_label("Praha 4"), {"city": "Prague", "country": "CZ"})
        self.assertEqual(city("Prague 1", "CZ"), "Prague")
        self.assertEqual(parse_label("České Budějovice 6")["city"], "České Budějovice")

    def test_remote_wrapper_requires_explicit_country(self):
        for value in ("Remote in Czech Republic", "Czechia (Remote)", "Remote (Czech Republic)", "CZ Remote"):
            self.assertEqual(parse_label(value), {"country": "CZ"})
        self.assertEqual(parse_label("Remote in Europe"), {})

    def test_compact_geography_layout_preserves_hyphenated_names(self):
        self.assertEqual(parse_label("Praha-Czechia-Czechia"), {"city": "Prague", "country": "CZ"})
        self.assertEqual(city("Brandýs nad Labem-Stará Boleslav", "CZ"), "Brandýs nad Labem-Stará Boleslav")

    def test_geographic_hierarchy_and_exact_country_city_layout(self):
        for value in ("NCEE > Czech Republic > Praha", "Czechia Praha"):
            self.assertEqual(parse_label(value), {"city": "Prague", "country": "CZ"})
        self.assertEqual(parse_label("NCEE > Europe > Remote"), {})

    def test_city_qualifier_preserves_conflicting_country(self):
        self.assertEqual(parse_label("Prague (Sandoz)"), {"city": "Prague", "country": "CZ"})
        self.assertEqual(parse_label("Prague (Germany)"), {"country": "DE"})
        self.assertEqual(parse_label("We recruit in Prague (Sandoz)"), {})


class GeographyProjectionTests(unittest.TestCase):
    def test_country_never_region_and_original_evidence_retained(self):
        p = profile(source(address={"postalAddress": {"addressCountry": "Czech Republic", "addressRegion": "Czech Republic"}}))
        location = p.workplace.locations.value[0]
        self.assertEqual(location.country.value, "CZ")
        self.assertEqual(location.country_name.value, "Czechia")
        self.assertEqual(location.region.state, ValueState.UNKNOWN)
        evidence = {e.id: e for e in p.evidence}
        self.assertEqual(evidence[location.country.evidence_ids[0]].raw_value, "Czech Republic")
        self.assertEqual(p.workplace.raw_location_labels.value[0].field_semantics, "region")

    def test_trusted_country_city_fields(self):
        p = profile(source("smartrecruiters_direct", location={"country": "CZ", "city": "Rožnov pod Radhoštěm"}))
        location = p.workplace.locations.value[0]
        self.assertEqual(location.city.value, "Rožnov pod Radhoštěm")
        self.assertEqual(location.country.value, "CZ")
        self.assertTrue(any(e.native_field_path == "raw_payload.location.city" for e in p.evidence))

    def test_typed_locality_outside_alias_dictionary(self):
        p = profile(source("workable_direct", city="Dnešice", country="CZ"))
        self.assertEqual(p.workplace.locations.value[0].city.value, "Dnešice")

    def test_typed_locality_postal_district_and_layout(self):
        for raw, expected in (("Jičín 1", "Jičín"), ("Otrokovice 2", "Otrokovice"),
                              ("Bohumín (Office/Factory)", "Bohumín"), ("Prague, Czech Republic", "Prague")):
            self.assertEqual(city(raw, "CZ"), expected)
        self.assertEqual(city("São Paulo, São Paulo, Brazil", "BR"), "São Paulo")
        self.assertIsNone(city("Berlin, Prague, Germany", "DE"))

    def test_generic_array_not_implicitly_locations(self):
        p = profile(source("fantastic_jobs_apify", locations_alt=["Engineering", "Jobs.cz", "Worldwide", "Kutná Hora"]))
        self.assertEqual(len(p.workplace.locations.value), 1)
        self.assertEqual(p.workplace.locations.value[0].city.value, "Kutná Hora")
        self.assertEqual({l.raw_value for l in p.workplace.raw_location_labels.value}, {"Engineering", "Jobs.cz", "Worldwide"})

    def test_navigation_object_address_is_quarantined(self):
        p = profile(source(location="Prague, Czech Republic", secondaryLocations=[
            {"location": "Group career pages", "address": {"postalAddress": {"addressCountry": "Zambia"}}},
            {"location": "Jobs.cz"}, {"location": "Brno"}]))
        self.assertEqual([l.city.value for l in p.workplace.locations.value], ["Prague", "Brno"])
        self.assertEqual({l.raw_value for l in p.workplace.raw_location_labels.value}, {"Group career pages", "Zambia", "Jobs.cz"})
        self.assertTrue(any(e.value == "Zambia" and e.validation_state == "rejected" for e in p.evidence))

    def test_remote_label_does_not_discard_explicit_country(self):
        p = profile(source("lever_direct", country="CZ", categories={"location": "Remote"}))
        self.assertEqual(p.workplace.locations.value[0].country.value, "CZ")
        self.assertIsNone(p.workplace.locations.value[0].text.value)
        self.assertEqual(p.workplace.raw_location_labels.value[0].raw_value, "Remote")

    def test_unknown_raw_label_kept_without_normalized_location(self):
        p = profile(source("jooble_direct", location="Unverified facility"))
        self.assertEqual(p.workplace.locations.state, ValueState.UNKNOWN)
        label = p.workplace.raw_location_labels.value[0]
        self.assertEqual(label.raw_value, "Unverified facility")
        self.assertEqual(label.reason, "unrecognized_geography")
        self.assertIn(label.evidence_ids[0], {e.id for e in p.evidence})

    def test_typed_country_in_city_rejected(self):
        p = profile(source("workable_direct", city="Czechia", country="CZ", state="EMEA"))
        self.assertIsNone(p.workplace.locations.value[0].city.value)
        self.assertIsNone(p.workplace.locations.value[0].region.value)

    def test_native_geography_precedes_provider(self):
        p = profile(source("jooble_direct", location="Brno"),
                    source("smartrecruiters_direct", location={"city": "Prague", "country": "CZ"}))
        self.assertEqual(p.workplace.locations.value[0].city.value, "Prague")
        self.assertEqual(p.workplace.locations.state, ValueState.KNOWN)
        self.assertIn("Brno", {l.city.value for l in p.workplace.locations.value})

    def test_conflicting_native_cities_preserved(self):
        p = profile(source(address={"postalAddress": {"addressCountry": "CZ", "addressLocality": "Prague"}}),
                    source("smartrecruiters_direct", location={"country": "CZ", "city": "Brno"}))
        self.assertEqual(p.workplace.locations.state, ValueState.CONFLICT)
        self.assertEqual({l.city.value for l in p.workplace.locations.value}, {"Prague", "Brno"})

    def test_conflicting_native_countries_preserved(self):
        p = profile(source(address={"postalAddress": {"addressCountry": "CZ"}}), source("lever_direct", country="DE"))
        self.assertEqual(p.workplace.locations.state, ValueState.CONFLICT)
        self.assertEqual({l.country.value for l in p.workplace.locations.value}, {"CZ", "DE"})

    def test_additional_city_does_not_conflict_with_primary(self):
        p = profile(source(location="Prague", secondaryLocations=[{"location": "Brno"}]))
        self.assertEqual(p.workplace.locations.state, ValueState.KNOWN)

    def test_all_source_location_paths_remain_supported(self):
        fixtures = [source("greenhouse_direct", location={"name": "Praha"}),
            source("workable_direct", city="Praha", country="CZ"), source("ashby_direct", location="Praha"),
            source("lever_direct", country="CZ", categories={"location": "Praha"}),
            source("workday_direct", detail={"jobPostingInfo": {"location": "Praha", "country": {"descriptor": "Czech Republic"}}}),
            source("successfactors_direct", detail={"locations": [{"text": "Praha", "country": "CZ", "native": {"addressLocality": "Praha"}}]}),
            source("fantastic_jobs_apify", locations=[{"address": {"addressCountry": "CZ", "addressLocality": "Praha"}}]),
            source("jooble_direct", location="Praha"), source("smartrecruiters_direct", location={"city": "Praha", "country": "CZ"})]
        for s in fixtures:
            with self.subTest(source=s.source_name):
                p = project_source(s, EvidenceCollector())
                self.assertEqual(p.locations[0].city.value, "Prague")
                self.assertEqual(p.locations[0].country.value, "CZ")

    def test_unmapped_label_array_never_emitted(self):
        p = profile(source(labels=["Prague", "Jobs.cz"], categories=["Brno"]))
        self.assertIsNone(p.workplace.locations.value)

    def test_delimited_multiple_places_not_flattened(self):
        p = profile(source("greenhouse_direct", location={"name": "Berlin, Germany; Prague, Czech Republic"}))
        locations = p.workplace.locations.value
        self.assertEqual(len(locations), 2)
        self.assertEqual([(l.city.value,l.country.value) for l in locations], [(None,"DE"),("Prague","CZ")])
        self.assertTrue(all(l.kind == "unspecified" for l in locations))

    def test_delimited_city_only_country_is_inferred(self):
        p = profile(source("greenhouse_direct", location={"name": "Prague; Brno"}))
        for loc in p.workplace.locations.value:
            self.assertEqual(next(e for e in p.evidence if e.id==loc.country.evidence_ids[0]).explicitness, "inferred")

    def test_long_delimited_places_are_not_quarantined_as_prose(self):
        p = profile(source("greenhouse_direct", location={"name": "; ".join(["Amsterdam, Netherlands", "Berlin, Germany", "Paris, France", "Prague, Czech Republic"] * 3)}))
        self.assertTrue(any(l.city.value == "Prague" and l.country.value == "CZ" for l in p.workplace.locations.value))

    def test_inferred_czech_country_retains_explicit_city_evidence(self):
        p = profile(source("jooble_direct", location="Rožnov pod Radhoštěm"))
        country = p.workplace.locations.value[0].country
        self.assertEqual(next(e for e in p.evidence if e.id == country.evidence_ids[0]).explicitness, "inferred")

    def test_explicit_country_in_label_is_not_inferred(self):
        p = profile(source("jooble_direct", location="Prague, Czech Republic"))
        country = p.workplace.locations.value[0].country
        self.assertEqual(next(e for e in p.evidence if e.id == country.evidence_ids[0]).explicitness, "explicit")


class GeographyVersionTests(unittest.TestCase):
    def test_normalized_country_contract_requires_iso_code(self):
        with self.assertRaises(ValueError):
            Location(country=Fact(state="known", value="Czech Republic", evidence_ids=["offline"]))

    def test_legacy_profile_accepts_additive_contract(self):
        p = profile(source(location="Prague"))
        old = p.model_dump()
        old["workplace"].pop("raw_location_labels")
        for loc in old["workplace"]["locations"]["value"]:
            loc.pop("country_name")
        for e in old["evidence"]:
            e.pop("raw_value")
        self.assertIsInstance(JobProfile.model_validate(old), JobProfile)

    def test_geography_bump_invalidates_native_without_text(self):
        p = profile(source(location="Prague", descriptionPlain="English B2. Python. " * 40))
        old = p.metadata.model_copy(deep=True)
        old.versions["geography"] = "geography-v1.0"
        old.input_hash = "old"
        self.assertEqual(reprocessing_layers(old, p.metadata), {"native", "selection", "profile"})

    def test_geography_change_reuses_text_cache(self):
        s = source(location="Prague", descriptionPlain="Requirements\nEnglish B2. Python. " * 40)
        data = {"id": "offline", "sources": [s.model_dump()]}
        cache = {}
        old_versions = {**versions(), "geography": "geography-v1.0", "projector": "native-v1.0", "schema": "job-profile-v1.0"}
        with patch("app.job_profile.pipeline.versions", return_value=old_versions):
            first = build_profile(data, cache_out=cache)
        with patch("app.job_profile.pipeline.languages", side_effect=AssertionError("text rerun")):
            second = build_profile(data, text_cache=cache)
        self.assertEqual(first.requirements, second.requirements)
        self.assertNotEqual(first.metadata.input_hash, second.metadata.input_hash)
        self.assertEqual(first.metadata.cleaned_description_hash, second.metadata.cleaned_description_hash)

    def test_geography_hash_ignores_volatile_payload(self):
        s = source(location="Prague")
        before = profile(s)
        s.raw_payload.update(session="ignored", csrf="ignored", last_seen_at="ignored")
        s.last_seen_at = datetime(2026, 10, 6, tzinfo=timezone.utc)
        self.assertEqual(before.metadata.semantic_metadata_hash, profile(s).metadata.semantic_metadata_hash)

    def test_unresolved_label_change_affects_semantic_hash(self):
        self.assertNotEqual(profile(source(location="Unknown facility A")).metadata.semantic_metadata_hash,
                            profile(source(location="Unknown facility B")).metadata.semantic_metadata_hash)
