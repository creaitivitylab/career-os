"""Offline geography validation. Field semantics precede label recognition.

ISO subdivisions are administrative entities, not a municipality gazetteer.
Unknown labels remain evidence; this module never calls a geocoder.
"""
import json
from pathlib import Path
import re
import unicodedata


def fold(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFD", str(value).casefold())
                            if not unicodedata.combining(c)).split())


DATA = json.loads((Path(__file__).parent / "data" / "iso-geography.json").read_text())
COUNTRIES = {r["alpha_2"]: r["name"] for r in DATA["countries"]}
COUNTRY_ALIASES = {}
for row in DATA["countries"]:
    for key in ("alpha_2", "alpha_3", "name", "common_name", "official_name"):
        if row.get(key):
            COUNTRY_ALIASES[fold(row[key])] = row["alpha_2"]
COUNTRY_ALIASES.update({
    "czech republic": "CZ", "ceska republika": "CZ", "cesko": "CZ", "uk": "GB",
    "deutschland": "DE", "nemecko": "DE", "slovensko": "SK", "polsko": "PL",
    "rakousko": "AT", "holland": "NL", "south korea": "KR", "north korea": "KP",
    "russia": "RU", "taiwan": "TW", "vietnam": "VN", "turkey": "TR",
    "united states": "US", "united kingdom": "GB",
})
# Preserve the public-facing country name while retaining ISO identity.
COUNTRIES["CZ"] = "Czechia"

SUBDIVISIONS = {}
for row in DATA["subdivisions"]:
    country = row["code"].split("-")[0]
    for alias in (row["code"], row["name"], row["code"].split("-", 1)[1]):
        SUBDIVISIONS.setdefault((country, fold(alias)), row["name"])

# A reviewed seed, not a complete Czech municipality register. District names
# from ISO are deliberately not promoted to cities.
CITY_ALIASES = {
    "prague": "Prague", "praha": "Prague", "brno": "Brno", "ostrava": "Ostrava",
    "plzen": "Plzeň", "pilsen": "Plzeň", "pardubice": "Pardubice",
    "kutna hora": "Kutná Hora", "roznov pod radhostem": "Rožnov pod Radhoštěm",
    "liberec": "Liberec", "olomouc": "Olomouc", "hradec kralove": "Hradec Králové",
    "ceske budejovice": "České Budějovice", "zlin": "Zlín", "usti nad labem": "Ústí nad Labem",
}
REGION_ALIASES = {}
for row in DATA["subdivisions"]:
    if row["code"].startswith("CZ-") and row["type"] in {"Region", "Capital city"}:
        REGION_ALIASES[fold(row["name"])] = row["name"]
for english, native in {
    "prague": "Praha, Hlavní město", "praha": "Praha, Hlavní město", "hlavni mesto praha": "Praha, Hlavní město",
    "central bohemian region": "Středočeský kraj", "south bohemian region": "Jihočeský kraj",
    "south moravian region": "Jihomoravský kraj", "moravian-silesian region": "Moravskoslezský kraj",
    "pilsen region": "Plzeňský kraj", "plzen region": "Plzeňský kraj",
    "karlovy vary region": "Karlovarský kraj", "usti nad labem region": "Ústecký kraj",
    "liberec region": "Liberecký kraj", "hradec kralove region": "Královéhradecký kraj",
    "pardubice region": "Pardubický kraj", "olomouc region": "Olomoucký kraj",
    "zlin region": "Zlínský kraj", "vysocina region": "Kraj Vysočina",
}.items():
    REGION_ALIASES[english] = native


def country_code(value):
    return COUNTRY_ALIASES.get(fold(value)) if isinstance(value, str) else None


def non_geographic_reason(value):
    """Reject structural noise, rather than blacklisting two observed labels."""
    f = fold(value)
    if re.search(r"https?://|www\.|(?:[\w-]+\.)+[a-z]{2,}(?:\b|/)", f):
        return "url_or_domain"
    if re.search(r"\b(?:career|careers|job|jobs)\s*(?:pages?|boards?|portal|site|search)\b|\b(?:navigation|apply now|all jobs|vacancies|karierni stranky|pracovni portal)\b", f):
        return "navigation_or_marketplace"
    if len(f) > 160 or len(f.split()) > 16:
        return "prose_or_long_label"
    if f in {"remote", "hybrid", "onsite", "on-site", "on site", "work from home", "home office",
             "europe", "eu", "european union", "emea", "worldwide", "global", "anywhere", "countries", "multiple locations"}:
        return "workplace_or_broad_scope"
    return None


def region(value, country=None):
    f = fold(value)
    if non_geographic_reason(value):
        return None
    # Explicit region paths disambiguate the capital's city/region names.
    if country in {None, "CZ"} and f in REGION_ALIASES:
        return REGION_ALIASES[f]
    if country and (country, f) in SUBDIVISIONS:
        return SUBDIVISIONS[(country, f)]
    # Never promote a country name/code or bare municipality into a region.
    if country_code(value) or f in CITY_ALIASES:
        return None
    # Unverified arbitrary strings remain raw labels, even in a region field.
    return None


def city(value, country=None):
    if non_geographic_reason(value) or country_code(value):
        return None
    f = fold(value)
    # A known municipality followed by a numbered postal/urban district still
    # establishes the parent municipality, not a new municipality or region.
    base = re.sub(r"\s+\d+$", "", f)
    if base in CITY_ALIASES:
        return CITY_ALIASES[base] if country in {None, "CZ"} else None
    if f in CITY_ALIASES:
        return CITY_ALIASES[f] if country in {None, "CZ"} else None
    if f in REGION_ALIASES or re.search(r"\b(?:region|province|state|kraj)\b", f):
        return None
    # Typed Czech locality fields commonly append a numbered postal district.
    # The field supplies locality semantics; do not apply this to weak labels.
    if country == "CZ" and re.search(r"\s+\d+$", value):
        return city(re.sub(r"\s+\d+$", "", value), country)
    # Some typed locality fields repeat a place or append its explicit country.
    # Only discard independently validated trailing components, never an
    # arbitrary second municipality or a prose suffix.
    pieces = [p.strip() for p in value.split(",")]
    if len(pieces) > 1 and country and country_code(pieces[-1]) == country:
        if all(fold(p) == fold(pieces[0]) for p in pieces[1:-1]):
            return city(pieces[0], country)
    if country and re.search(r"\s*\((?:office|factory|office/factory)\)$", value, re.I):
        return city(re.sub(r"\s*\([^()]+\)$", "", value), country)
    # A named, typed city/locality field is evidence in its own right. Permit
    # municipalities outside the small alias dictionary, but not mixed lists,
    # facility identifiers, postal codes or work-arrangement phrases.
    if (not re.fullmatch(r"[^\W\d_][\w .’'\-]*", value, re.UNICODE)
            or any(char.isdigit() for char in value)
            or re.search(r"\b(?:remote|hybrid|onsite|office|campus|site)\b", f)
            or len(f.split()) > 8):
        return None
    return value.strip()


def label_components(value):
    """Small, anchored layout wrappers; no searching for cities in prose."""
    value = value.strip()
    value = re.sub(r"^(?:remote|hybrid|onsite)\s*\(([^()]+)\)$", r"\1", value, flags=re.I)
    value = re.sub(r"^(?:remote|hybrid|onsite)\s+(?:in\s+)?", "", value, flags=re.I)
    value = re.sub(r"\s*\((?:remote|hybrid|onsite)\)\s*$", "", value, flags=re.I)
    value = re.sub(r"\s+(?:remote|hybrid|onsite)$", "", value, flags=re.I)
    value = re.sub(r"\s*>\s*(?:remote|hybrid|onsite)$", "", value, flags=re.I)
    # Only split compact hyphen layouts if every component is independently
    # recognized. Municipal names such as Brandýs ...-Stará ... stay intact.
    compact = value.split("-")
    if len(compact) > 1 and all(country_code(p) or fold(p) in CITY_ALIASES or
                               fold(p) in {"remote", "hybrid", "onsite"} for p in compact):
        value = ",".join(compact)
    # An exact municipality followed by a non-geographic qualifier remains a
    # place label. Explicit country qualifiers still participate in validation.
    qualified = re.fullmatch(r"([^()]+)\s*\(([^()]+)\)", value)
    if qualified and fold(qualified[1]) in CITY_ALIASES:
        value = qualified[1]+(","+qualified[2] if country_code(qualified[2]) else "")
    # Layouts sometimes omit the delimiter between country and municipality.
    for alias in CITY_ALIASES:
        f = fold(value)
        if f.endswith(" "+alias) and country_code(f[:-len(alias)].strip()):
            return [f[:-len(alias)].strip(), alias]
    return [p.strip() for p in re.split(r"[,;/|>]|\s+[-–—]\s+", value) if p.strip()]


def parse_label(value, country=None):
    """Recognize only whole geographic components, never prose substrings."""
    if non_geographic_reason(value):
        return {}
    # Keep hyphens inside municipal names; split only spaced separators.
    parts = label_components(value)
    observed_countries = {country_code(p) for p in parts} - {None}
    countries = set(observed_countries)
    if country:
        countries.add(country)
    result = {}
    if len(observed_countries) == 1 and len(countries) == 1:
        result["country"] = next(iter(countries))
    cities = {CITY_ALIASES[re.sub(r"\s+\d+$", "", fold(p))] for p in parts
              if re.sub(r"\s+\d+$", "", fold(p)) in CITY_ALIASES}
    # Conflicting country evidence cannot turn a Czech city into a foreign one.
    if len(cities) == 1 and countries <= {"CZ"}:
        result["city"] = next(iter(cities))
        result.setdefault("country", "CZ")
    regions = {REGION_ALIASES[fold(p)] for p in parts
               if fold(p) in REGION_ALIASES and fold(p) not in CITY_ALIASES}
    if len(regions) == 1 and countries <= {"CZ"}:
        result["region"] = next(iter(regions))
        result.setdefault("country", "CZ")
    return result
