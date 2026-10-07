"""Project actual stored payload shapes into facts, without database access."""
from dataclasses import dataclass, field
from decimal import Decimal
import re

from .evidence import EvidenceCollector
from .models import CompensationOffer, Fact, Location, Method, RawLocationLabel, SourceInput
from .geography import (COUNTRIES, CITY_ALIASES, city, country_code, label_components,
                        non_geographic_reason, parse_label, region, fold as geography_fold)
from .parsers import (amount, employment_values, fold, native_level,
                      pay_period, workplace_mode, PAY_NUMBER)
from .selection import DescriptionCandidate


def get(raw, path):
    parts = re.sub(r"\[(\d+)\]", r".\1", path).split(".")
    value = raw
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def text(value):
    if isinstance(value, dict):
        return text(value.get("descriptor") or value.get("name") or value.get("label"))
    if not isinstance(value, str) or fold(value).strip() in {"", "unknown", "unspecified", "not applicable", "n/a", "all levels"}:
        return None
    return value.strip()


def objects(value):
    return value if isinstance(value, list) else []


@dataclass
class Projection:
    source: SourceInput
    facts: dict[str, list[Fact]] = field(default_factory=dict)
    locations: list[Location] = field(default_factory=list)
    raw_location_labels: list[RawLocationLabel] = field(default_factory=list)
    offers: list[CompensationOffer] = field(default_factory=list)
    description: DescriptionCandidate | None = None


class Projector:
    def __init__(self, source, collector):
        self.source, self.raw, self.evidence = source, source.raw_payload, collector
        self.result = Projection(source)
        self.method = Method.PROVIDER if source.source_name in {"fantastic_jobs_apify", "jooble_direct"} else Method.NATIVE

    def value(self, path):
        return get(self.raw, path)

    def fact(self, field, value, path, method=None, **kwargs):
        if value is None or value == "" or value == []:
            return None
        fact = self.evidence.fact(self.source, field, value, path="raw_payload."+path,
                                  method=method or self.method, input_value=self.value(path), **kwargs)
        self.result.facts.setdefault(field, []).append(fact)
        return fact

    def label(self, field, path):
        value = self.value(path)
        value = str(value) if field == "native_id" and type(value) is int else text(value)
        return self.fact(field, value, path)

    def labels(self, mappings):
        for field, path in mappings.items():
            self.label(field, path)

    def employment(self, path):
        value = text(self.value(path))
        if value:
            for field, normalized in employment_values(value).items():
                self.fact(field, normalized, path)

    def workplace(self, path):
        self.fact("workplace", workplace_mode(self.value(path)), path)

    def level(self, path):
        value = text(self.value(path))
        if value:
            self.label("native_level", path)
            self.fact("career_level", native_level(value), path)

    def location(self, label_path=None, country_path=None, city_path=None, region_path=None, kind="primary"):
        """Typed address fields are trusted; mixed labels require recognition.

        A navigation/domain label identifies a non-place record, even when its
        enclosing native object contains an anomalous address (e.g. job-board
        categories exported as Ashby secondary locations). Retain every value
        as rejected raw evidence instead of publishing that object as a place.
        """
        values = {}
        label = text(self.value(label_path)) if label_path else None
        # Delimited multi-place labels are a layout, not long prose. Parse each
        # independently only when every segment establishes some geography;
        # never flatten several countries onto one city.
        segments = [part.strip() for part in label.split(";") if part.strip()] if label else []
        if (len(segments) > 1 and not any((country_path, city_path, region_path))
                and all(parse_label(part) for part in segments)):
            for part in segments:
                parsed = parse_label(part)
                item = {}
                for name, value in parsed.items():
                    inferred_country = name == "country" and value not in {
                        country_code(component) for component in label_components(label)}
                    item[name] = self.evidence.fact(self.source, name, value, path="raw_payload."+label_path,
                        method=Method.PARSER, input_value=self.value(label_path), raw_value=self.value(label_path),
                        confidence="medium", explicitness="inferred" if inferred_country else "explicit")
                    if name == "country":
                        item["country_name"] = self.evidence.fact(self.source, "country_name", COUNTRIES[value],
                            path="raw_payload."+label_path, method=Method.PARSER, input_value=self.value(label_path),
                            raw_value=self.value(label_path), confidence="medium",
                            explicitness="inferred" if inferred_country else "explicit")
                item["text"] = self.evidence.fact(self.source, "text", part, path="raw_payload."+label_path,
                    method=Method.PARSER, input_value=self.value(label_path), raw_value=self.value(label_path), confidence="medium")
                self.result.locations.append(Location(kind="unspecified", **item))
            return
        label_reason = non_geographic_reason(label) if label else None
        quarantine = label_reason in {"url_or_domain", "navigation_or_marketplace", "prose_or_long_label"}

        def raw(path, semantics, reason):
            original = text(self.value(path)) if path else None
            if not original:
                return
            rejected = reason != "unrecognized_geography"
            key = self.evidence.add(self.source, "raw_location_label", original,
                path="raw_payload."+path, method=self.method, input_value=self.value(path),
                raw_value=self.value(path), confidence="low", explicitness="unknown",
                validation="rejected" if rejected else "unvalidated")
            self.result.raw_location_labels.append(RawLocationLabel(raw_value=original,
                field_semantics=semantics, kind=kind, reason=reason, evidence_ids=[key]))

        def assertion(name, value, path, parsed=False):
            normalized_code = country_code(value) if name == "country_name" else value
            inferred_country = parsed and name in {"country", "country_name"} and normalized_code not in {
                country_code(part) for part in label_components(text(self.value(path)) or "")}
            values[name] = self.evidence.fact(self.source, name, value, path="raw_payload."+path,
                method=Method.PARSER if parsed else self.method, input_value=self.value(path),
                raw_value=self.value(path), confidence="medium" if parsed else "high",
                explicitness="inferred" if inferred_country else "explicit")
            if name == "country":
                assertion("country_name", COUNTRIES[value], path, parsed)

        if quarantine:
            for semantics, path in [("label", label_path), ("country", country_path), ("city", city_path), ("region", region_path)]:
                raw(path, semantics, label_reason if semantics == "label" else "non_geographic_location_record")
            return
        code = country_code(text(self.value(country_path))) if country_path else None
        for name, path, normalized in [
            ("country", country_path, code),
            ("city", city_path, city(text(self.value(city_path)), code) if city_path and text(self.value(city_path)) else None),
            ("region", region_path, region(text(self.value(region_path)), code) if region_path and text(self.value(region_path)) else None),
        ]:
            if normalized:
                assertion(name, normalized, path)
            elif path and text(self.value(path)):
                original = text(self.value(path))
                reason = non_geographic_reason(original)
                if not reason and name in {"city", "region"} and country_code(original):
                    reason = "country_in_"+name
                raw(path, name, reason or "unrecognized_geography")
        if label:
            parsed = parse_label(label, code)
            for name, value in parsed.items():
                if name not in values:
                    assertion(name, value, label_path, parsed=True)
            # A raw label needs independent geographic meaning; merely sharing
            # an object with an address does not make arbitrary text a place.
            if parsed or (label_path == city_path and "city" in values):
                assertion("text", label, label_path, parsed=True)
            else:
                raw(label_path, "label", label_reason or "unrecognized_geography")
        if any(name in values for name in ("country", "city", "region")):
            self.result.locations.append(Location(kind=kind, **values))

    def description(self, paths):
        for path in paths:
            value = text(self.value(path))
            if value:
                self.result.description = DescriptionCandidate(self.source, value, "raw_payload."+path)
                return

    def offer(self, path, pay, *, minor_units=False):
        if not isinstance(pay, dict):
            return
        lo, hi = amount(pay.get("min")), amount(pay.get("max"))
        currency = text(pay.get("currency"))
        if currency:
            currency = currency.upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                currency = None
        if minor_units:
            # Only currencies with verified two-decimal minor units in V1.
            if currency not in {"CZK", "EUR", "USD", "GBP", "SEK", "PLN", "CHF"}:
                return
            lo = lo / Decimal(100) if lo else None
            hi = hi / Decimal(100) if hi else None
        if lo is None and hi is None or lo is not None and hi is not None and lo > hi:
            return
        period = pay_period(pay.get("period"))
        component = pay.get("component", "unknown")
        if period in {"one_time", "task"} and component == "unknown":
            component = "task_reward"
        key = self.evidence.add(self.source, "compensation", {"min": str(lo) if lo else None,
            "max": str(hi) if hi else None, "currency": currency, "period": period, "component": component,
            "locations": pay.get("locations"),
            **({"original_location_labels": pay["location_labels"]} if pay.get("location_labels") else {}),
            **({"native_offer_label": pay["offer_label"]} if pay.get("offer_label") else {})}, path="raw_payload."+path, method=self.method,
            input_value=self.value(path), explicitness="unknown" if self.method == Method.PROVIDER else "explicit",
            confidence="medium" if self.method == Method.PROVIDER else "high")
        self.result.offers.append(CompensationOffer(min_amount=lo, max_amount=hi, currency=currency,
            period=period, component=component, applicable_locations=pay.get("locations"),
            original_text=pay.get("text"), original_location_labels=pay.get("location_labels"), explicitness="unknown" if self.method == Method.PROVIDER else "explicit", evidence_ids=[key]))


def smartrecruiters(p):
    p.labels({"title": "name", "company": "company.name", "native_id": "id", "url": "postingUrl",
              "published_at": "releasedDate", "department": "department.label", "employer_industry": "industry.label"})
    p.location("location.fullLocation", "location.country", "location.city", "location.region")
    if p.value("location.hybrid") is True:
        p.fact("workplace", "hybrid", "location.hybrid")
    elif p.value("location.remote") is True:
        p.fact("workplace", "remote", "location.remote")
    # The SR identifier permanent is deliberately ignored; use semantic label.
    p.employment("typeOfEmployment.label")
    p.level("experienceLevel.label")
    pieces = []
    for key, heading in [("jobDescription", "Job description"), ("qualifications", "Qualifications"), ("additionalInformation", "Additional information")]:
        value = p.value(f"jobAd.sections.{key}.text")
        if text(value):
            pieces.append(f"<h2>{heading}</h2>\n{value}")
    if pieces:
        p.result.description = DescriptionCandidate(p.source, "\n".join(pieces), "raw_payload.jobAd.sections")
    pay = p.value("compensation")
    if isinstance(pay, dict):
        p.offer("compensation", {**pay, "text": str(pay), "component": "unknown"})


def greenhouse(p):
    p.labels({"title": "title", "company": "company_name", "native_id": "id", "url": "absolute_url",
              "published_at": "first_published", "expires_at": "application_deadline"})
    p.location("location.name")
    for i, office in enumerate(objects(p.value("offices"))):
        if isinstance(office, dict):
            p.location(f"offices[{i}].location", kind="additional")
    for i, department in enumerate(objects(p.value("departments"))):
        if isinstance(department, dict):
            p.label("department", f"departments[{i}].name")
    for i, meta in enumerate(objects(p.value("metadata"))):
        if not isinstance(meta, dict):
            continue
        label, path = fold(meta.get("name", "")).strip(" :"), f"metadata[{i}].value"
        if label in {"employment type", "work type", "time type", "type of contract", "contract type"}:
            p.employment(path)
        elif label in {"location type", "workplace type", "work arrangement", "remote status"}:
            p.workplace(path)
        elif label in {"level", "seniority level", "career stream/level", "workday p level"}:
            p.level(path)
        elif label == "team":
            p.label("team", path)
    for i, pay in enumerate(objects(p.value("pay_input_ranges"))):
        if isinstance(pay, dict):
            title = text(pay.get("title")) or ""
            component = "base" if re.search(r"base salary", title, re.I) else "unknown"
            # The title belongs to this native pay range. Exact recognized
            # geography establishes a scope; arbitrary range titles do not.
            code = country_code(title.strip())
            city = CITY_ALIASES.get(geography_fold(title))
            if geography_fold(title) == "paris":
                city = "Paris"
            scope = [COUNTRIES[code]] if code else [city] if city else None
            p.offer(f"pay_input_ranges[{i}]", {"min": pay.get("min_cents"), "max": pay.get("max_cents"),
                "currency": pay.get("currency_type"), "component": component, "text": pay.get("title") if isinstance(pay.get("title"), str) else title,
                "locations": scope, "location_labels": [pay["title"]] if scope else None,
                "offer_label": pay.get("title") if isinstance(pay.get("title"), str) else None}, minor_units=True)
    p.description(["content"])


def workable(p):
    p.labels({"title": "title", "native_id": "shortcode", "url": "url", "published_at": "published_on",
              "department": "department", "employer_industry": "industry"})
    # Account name was added from native account response by ingestion.
    p.label("company", "_account_name")
    p.location("city", "country", "city", "state")
    for i, location in enumerate(objects(p.value("locations"))):
        if isinstance(location, dict):
            prefix = f"locations[{i}]"
            p.location(prefix+".city", prefix+".countryCode", prefix+".city", prefix+".region", "additional")
    if p.value("telecommuting") is True:
        p.fact("workplace", "remote", "telecommuting")
    p.employment("employment_type")
    p.level("experience")
    value = text(p.value("education"))
    if value:
        p.fact("education", [value], "education")
    p.description(["description", "description_text"])


def ashby(p):
    p.labels({"title": "title", "native_id": "id", "url": "jobUrl", "published_at": "publishedAt", "department": "department", "team": "team"})
    p.location("location", "address.postalAddress.addressCountry", "address.postalAddress.addressLocality", "address.postalAddress.addressRegion")
    for i, location in enumerate(objects(p.value("secondaryLocations"))):
        if isinstance(location, dict):
            prefix = f"secondaryLocations[{i}]"
            p.location(prefix+".location", prefix+".address.postalAddress.addressCountry", prefix+".address.postalAddress.addressLocality", prefix+".address.postalAddress.addressRegion", "additional")
    p.workplace("workplaceType")
    if not p.result.facts.get("workplace") and p.value("isRemote") is True:
        p.fact("workplace", "remote", "isRemote")
    p.employment("employmentType")
    for i, pay in enumerate(objects(p.value("compensation.summaryComponents"))):
        if isinstance(pay, dict):
            component = {"Salary": "base", "Bonus": "bonus", "Equity": "equity"}.get(pay.get("compensationType"), "unknown")
            p.offer(f"compensation.summaryComponents[{i}]", {"min": pay.get("minValue"), "max": pay.get("maxValue"),
                "currency": pay.get("currencyCode"), "period": pay.get("interval"), "component": component,
                "text": text(p.value("compensation.scrapeableCompensationSalarySummary"))})
    p.description(["descriptionPlain", "descriptionHtml"])


def lever(p):
    p.labels({"title": "text", "native_id": "id", "url": "hostedUrl", "department": "categories.department", "team": "categories.team"})
    p.location("categories.location", "country")
    for i, label in enumerate(objects(p.value("categories.allLocations"))):
        if isinstance(label, str):
            p.location(f"categories.allLocations[{i}]", kind="additional")
    p.workplace("workplaceType")
    p.employment("categories.commitment")
    pay = p.value("salaryRange")
    if isinstance(pay, dict):
        p.offer("salaryRange", {**pay, "period": pay.get("interval"), "text": text(p.value("salaryDescriptionPlain"))})
    pieces = []
    # descriptionBody is distinct from opening and additional. Prefer it to
    # descriptionPlain when both exist to avoid repeating opening paragraphs.
    for paths in [["openingPlain", "opening"], ["descriptionBodyPlain", "descriptionBody", "descriptionPlain", "description"]]:
        for path in paths:
            value = text(p.value(path))
            if value:
                if value not in pieces:
                    pieces.append(value)
                break
    for section in objects(p.value("lists")):
        if isinstance(section, dict) and text(section.get("content")):
            pieces.append(str(section.get("text") or "")+"\n"+section["content"])
    for path in ["additionalPlain", "additional"]:
        if text(p.value(path)):
            pieces.append(p.value(path))
            break
    if pieces:
        p.result.description = DescriptionCandidate(p.source, "\n".join(pieces), "raw_payload.description+opening+lists+additional")


def workday(p):
    prefix = "detail.jobPostingInfo" if isinstance(p.value("detail.jobPostingInfo"), dict) else "jobPostingInfo"
    p.labels({"title": prefix+".title", "native_id": prefix+".id", "url": prefix+".externalUrl",
              "published_at": prefix+".startDate", "expires_at": prefix+".endDate", "company": "detail.hiringOrganization.name"})
    p.location(prefix+".location", prefix+".country.descriptor")
    for i, label in enumerate(objects(p.value(prefix+".additionalLocations"))):
        if isinstance(label, str):
            p.location(prefix+f".additionalLocations[{i}]", kind="additional")
    p.employment(prefix+".timeType")
    p.workplace(prefix+".remoteType")
    p.description([prefix+".jobDescription"])


def successfactors(p):
    p.labels({"title": "detail.title", "native_id": "detail.posting_id", "url": "detail.url", "published_at": "detail.posted_date",
              "company": "detail.native_fields.companyname", "department": "detail.native_fields.department", "travel": "detail.native_fields.travel"})
    for i, location in enumerate(objects(p.value("detail.locations"))):
        if isinstance(location, dict):
            prefix = f"detail.locations[{i}]"
            p.location(prefix+".text", prefix+".country", prefix+".native.addressLocality", prefix+".native.addressRegion", "primary" if i == 0 else "additional")
    if not p.result.locations:
        p.location("detail.native_fields.city", "detail.native_fields.country", "detail.native_fields.city", "detail.native_fields.state")
    p.employment("detail.employment_type")
    p.workplace("detail.workplace")
    p.description(["detail.description"])
    # Raw native_html and unlabelled customfield/shift are deliberately ignored.


def fantastic(p):
    p.labels({"title": "title", "company": "organization", "url": "url", "published_at": "date_posted", "expires_at": "date_valid_through"})
    for i, location in enumerate(objects(p.value("locations"))):
        if isinstance(location, dict):
            address = "Address" if isinstance(location.get("Address"), dict) else "address"
            prefix = f"locations[{i}].{address}"
            p.location(None, prefix+".addressCountry", prefix+".addressLocality", prefix+".addressRegion", "primary" if i == 0 else "additional")
    for i, label in enumerate(objects(p.value("locations_alt"))):
        if isinstance(label, str):
            p.location(f"locations_alt[{i}]", kind="unspecified")
    p.employment("employment_type")
    p.workplace("location_type")
    pay = p.value("salary.value")
    if isinstance(pay, dict):
        p.offer("salary", {"min": pay.get("minValue") or pay.get("value"), "max": pay.get("maxValue") or pay.get("value"),
            "currency": p.value("salary.currency"), "period": pay.get("unitText"), "text": str(p.value("salary"))})
    p.description(["description_text", "description_html"])
    value = text(p.value("org_linkedin_industry"))
    if value:
        p.fact("employer_industry", value, "org_linkedin_industry", explicitness="unknown",
               confidence="medium", validation="unvalidated")
    # Suggestions are retained separately and never admitted as known facts.
    for field, path in {"provider_experience_level": "ai_experience_level", "workplace": "ai_work_arrangement", "schedule": "ai_employment_type",
                        "required_skills": "ai_key_skills", "education": "ai_education", "responsibilities": "ai_core_responsibilities",
                        "requirements": "ai_requirements_summary", "benefits": "ai_benefits", "document_language": "ai_job_language",
                        "compensation": "ai_salary_value"}.items():
        value = p.value(path)
        if value is not None and value != "" and value != []:
            p.evidence.add(p.source, field, value, path="raw_payload."+path, method=Method.SUGGESTION,
                          explicitness="unknown", confidence="low", validation="unvalidated")


def jooble(p):
    p.labels({"title": "title", "company": "company", "url": "link"})
    p.location("location")
    p.employment("type")
    p.description(["snippet"])
    # A dedicated provider salary string is useful even with an unknown pay
    # interval/origin. It is not an employer-native or estimated-salary claim.
    value = text(p.value("salary"))
    if value:
        pattern = (r"\s*(?P<min>"+PAY_NUMBER+r")(?:\s*[-–—]\s*(?P<max>"+PAY_NUMBER+r"))?"
                   r"\s*(?P<currency>CZK|Kč|EUR|USD|GBP)\s*")
        match = re.fullmatch(pattern, value, re.I)
        if match:
            def numeric(raw):
                if raw is None:
                    return None
                multiplier = 1000 if raw.strip().lower().endswith("k") else 1
                parsed = amount(re.sub(r"[ ,\u00a0kK]", "", raw))
                return parsed * multiplier if parsed is not None else None
            p.offer("salary", {"min": numeric(match.group("min")), "max": numeric(match.group("max")),
                "currency": "CZK" if match.group("currency").lower() == "kč" else match.group("currency"), "text": value})


PROJECTORS = {
    "smartrecruiters_direct": smartrecruiters, "greenhouse_direct": greenhouse,
    "workable_direct": workable, "ashby_direct": ashby, "lever_direct": lever,
    "workday_direct": workday, "successfactors_direct": successfactors,
    "fantastic_jobs_apify": fantastic, "jooble_direct": jooble,
}


def project_source(source: SourceInput, collector: EvidenceCollector) -> Projection:
    p = Projector(source, collector)
    handler = PROJECTORS.get(source.source_name)
    if handler:
        handler(p)
    return p.result
