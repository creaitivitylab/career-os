"""Explicit deterministic rules. A mention is never a required-skill claim."""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from .models import (CompensationOffer, ContentSection, ExperienceConstraint,
                     LanguageRequirement, Method, Span, TechnologyMention)


def fold(text):
    return "".join(c for c in unicodedata.normalize("NFD", str(text).lower())
                   if not unicodedata.combining(c))


def country_code(value):
    if isinstance(value, dict):
        value = value.get("descriptor") or value.get("name")
    if not isinstance(value, str):
        return None
    aliases = {
        "cz": "CZ", "cze": "CZ", "czechia": "CZ", "czech republic": "CZ", "ceska republika": "CZ", "cesko": "CZ",
        "de": "DE", "germany": "DE", "deutschland": "DE", "nemecko": "DE",
        "fr": "FR", "france": "FR", "gb": "GB", "uk": "GB", "united kingdom": "GB",
        "us": "US", "usa": "US", "united states": "US", "united states of america": "US",
        "pl": "PL", "poland": "PL", "sk": "SK", "slovakia": "SK",
        "at": "AT", "austria": "AT", "es": "ES", "spain": "ES", "ie": "IE", "ireland": "IE",
        "se": "SE", "sweden": "SE", "nl": "NL", "netherlands": "NL", "ch": "CH", "switzerland": "CH",
    }
    return aliases.get(fold(value).strip())


CITY_ALIASES = {
    "prague": ("Prague", "CZ"), "praha": ("Prague", "CZ"), "brno": ("Brno", "CZ"),
    "ostrava": ("Ostrava", "CZ"), "pardubice": ("Pardubice", "CZ"), "kutna hora": ("Kutná Hora", "CZ"),
    "plzen": ("Plzeň", "CZ"), "pilsen": ("Plzeň", "CZ"), "liberec": ("Liberec", "CZ"),
    "olomouc": ("Olomouc", "CZ"), "hradec kralove": ("Hradec Králové", "CZ"),
    "ceske budejovice": ("České Budějovice", "CZ"), "zlin": ("Zlín", "CZ"),
}


def parse_location_label(text):
    """Only explicit country labels and a modest reviewed Czech city dictionary."""
    f = fold(text)
    cities = [(label, code) for alias, (label, code) in CITY_ALIASES.items()
              if re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", f)]
    cities = list(dict.fromkeys(cities))
    countries = [country_code(part.strip()) for part in re.split(r"[,;/|]|\s+-\s+", text)]
    countries = list(dict.fromkeys(c for c in countries if c))
    result = {}
    if len(cities) == 1:
        result["city"] = cities[0][0]
        if not countries or countries == [cities[0][1]]:
            result["country"] = cities[0][1]
    if len(countries) == 1:
        result["country"] = countries[0]
    return result


def workplace_mode(value):
    f = fold(value).strip().replace("_", " ")
    if f in {"remote", "fully remote", "work from home", "on remote"}:
        return "remote"
    if f in {"hybrid", "hybrid remote", "hybrid working"}:
        return "hybrid"
    if f in {"on site", "on-site", "onsite", "on-site only", "office"}:
        return "onsite"
    return None


def employment_values(value):
    f = fold(value).replace("_", " ")
    out = {}
    if re.search(r"\bfull[ -]?time\b|plny uvazek", f):
        out["schedule"] = "full_time"
    elif re.search(r"\bpart[ -]?time\b|castecny uvazek|zkraceny uvazek", f):
        out["schedule"] = "part_time"
    elif re.search(r"\bshift work\b|smenny provoz", f):
        out["schedule"] = "shift"
    if re.search(r"\bintern(?:ship)?\b|\btrainee\b|\bstaz\b", f):
        out["relationship"] = "intern"
    elif re.search(r"\bcontractor\b|\bfreelance\b|self.employed|\bico\b", f):
        out["relationship"] = "contractor"
    elif re.search(r"\btemporary\b|docasn", f):
        out["relationship"] = "temporary"
    elif re.search(r"\bemployee\b|\bzamestnanec\b", f):
        out["relationship"] = "employee"
    if re.search(r"\bfixed[ -]?term\b|doba urcita", f):
        out["duration"] = "fixed_term"
    elif re.search(r"\bpermanent\b|indefinite|doba neurcita", f):
        out["duration"] = "permanent"
    return out


def native_level(value):
    f = fold(value).strip()
    return {"junior": "junior", "entry level": "entry", "entry-level": "entry", "senior": "senior",
            "mid": "mid", "intermediate": "mid", "staff": "staff", "principal": "principal"}.get(f)


def title_facts(title, source, evidence, path="title"):
    result = {}
    levels = []
    for value, pattern in [("junior", r"\b(?:junior|jr)\b\.?"), ("senior", r"\b(?:senior|sr)\b\.?"),
                           ("staff", r"\bstaff\b"), ("principal", r"\bprincipal\b")]:
        match = re.search(pattern, title, re.I)
        if match:
            span = Span(start=match.start(), end=match.end(), text=match.group())
            levels.append(evidence.fact(source, "career_level", value, path=path, span=span, method=Method.PARSER, input_value=title))
    result["career_level"] = levels
    markers = []
    for marker, pattern in [("lead", r"\blead\b(?!\s+generation)"), ("head", r"\bhead\b"),
                            ("director", r"\bdirector\b|\bředitel\w*|\breditel\w*"),
                            ("manager", r"\bmanager\b|\bmanažer\w*|\bmanazer\w*"),
                            ("vedouci", r"\bvedouc[íi]\b")]:
        match = re.search(pattern, title, re.I)
        if match:
            markers.append((marker, match))
    if markers:
        ids = [evidence.add(source, "leadership_markers", marker, path=path, span=Span(start=m.start(), end=m.end(), text=m.group()),
                            method=Method.PARSER, input_value=title) for marker, m in markers]
        from .evidence import known_list
        result["leadership_markers"] = known_list([marker for marker, _ in markers], ids)
    # Minimal surface cleanup, not a semantic taxonomy or removal of manager roles.
    base = re.sub(r"^(?:senior|sr\.?|junior|jr\.?)\s+", "", title, flags=re.I)
    base = re.sub(r"\s*\((?:m/f/d|f/m/d|m/f|w/m/d)\)\s*$", "", base, flags=re.I).strip()
    if base:
        result["normalized_role"] = evidence.fact(source, "normalized_role", base, path=path, method=Method.PARSER, input_value=title)
    return result


TECHNOLOGIES = {
    "Python": r"python", "SQL": r"sql", "Excel": r"(?:microsoft\s+)?excel", "SAP": r"sap",
    "AWS": r"aws|amazon web services", "Azure": r"(?:microsoft\s+)?azure",
    "GCP": r"gcp|google cloud(?: platform)?", "Kubernetes": r"kubernetes|k8s", "Docker": r"docker",
    "Java": r"java", "JavaScript": r"javascript", "TypeScript": r"typescript",
    "React": r"react(?:\.js|js)?", "Power BI": r"power\s*bi", "C++": r"c\+\+(?:\d{2})?", "C#": r"c#",
    "Linux": r"linux", "Git": r"git", "Node.js": r"node\.?js", "Salesforce": r"salesforce",
}


def technologies(text, source, evidence):
    values = []
    for technology, pattern in TECHNOLOGIES.items():
        for match in re.finditer(r"(?<!\w)(?:" + pattern + r")(?!\w)", text, re.I):
            # Bare lowercase verbs are not explicit tool references. Keep
            # lowercase tool usage when a local technical qualifier supports it.
            if technology in {"React", "Excel"} and match.group() == technology.lower():
                context = text[max(0, match.start()-40):match.end()+15]
                if not re.search(r"microsoft|react native|skills|proficien|experience with|knowledge of|using|\btools?\b", context, re.I):
                    continue
            span = Span(start=match.start(), end=match.end(), text=match.group())
            key = evidence.add(source, "technologies", technology, span=span, method=Method.PARSER, input_value=text)
            values.append(TechnologyMention(technology=technology, matched_text=match.group(), evidence_ids=[key]))
    return values


LANGUAGES = {
    "en": r"english|angličtin\w*|anglictin\w*", "cs": r"czech(?!\s+republic)|češtin\w*|cestin\w*",
    "de": r"german|němčin\w*|nemcin\w*", "fr": r"french|francouzštin\w*",
    "es": r"spanish|španělštin\w*", "pl": r"polish|polštin\w*",
}
LANGUAGE_CONTEXT = re.compile(r"fluent|fluency|proficien|advanced|professional|speaking|spoken|written|native|\b[A-C][12]\+?\b|\brequired\b|\bpreferred\b|essential|advantage|plynul|znalost|úrov|urov|výhod|vyhod|podmín|podmin", re.I)


def languages(text, source, evidence):
    values = []
    # Clause boundaries prevent a requirement in another bullet becoming a label.
    for clause in re.finditer(r"[^\n;,]+", text):
        body = clause.group()
        if not LANGUAGE_CONTEXT.search(body):
            continue
        matches = sorted((m.start(), m, code) for code, pattern in LANGUAGES.items()
                         for m in re.finditer(r"(?<!\w)(?:" + pattern + r")(?!\w)", body, re.I))
        for index, (_, match, code) in enumerate(matches):
            before = body[matches[index-1][1].end() if index else 0:match.start()]
            after = body[match.end():matches[index+1][0] if index+1 < len(matches) else len(body)]
            context = before[-45:] + match.group() + after[:65]
            # Prefer qualifiers after this language; use shared clause evidence
            # only for coordinated language lists with one common qualifier.
            qualifier = after[:65]
            if not re.search(r"required|preferred|essential|advantage|výhod|vyhod|podmín|podmin", qualifier, re.I):
                qualifier = body if len(matches) > 1 and re.search(r"\band\b|\ba\b", body, re.I) else before[-40:]
            required = bool(re.search(r"required|essential|must|podmín|podmin|nutn", qualifier, re.I))
            preferred = bool(re.search(r"preferred|advantage|a plus|výhod|vyhod", qualifier, re.I))
            requirement = "required" if required and not preferred else "preferred" if preferred and not required else "unknown"
            level_pattern = r"(?<!\w)([ABC][12])(?!\w)(\+|\s+(?:level\s+)?or\s+(?:above|higher))?"
            level_context = after[:65]
            level = re.search(level_pattern, level_context, re.I)
            if level is None and index == 0:
                level_context = before[-30:]
                level = re.search(level_pattern, level_context, re.I)
            lower_bound = bool(level and (level.group(2) or re.search(
                r"(?:minimum(?: of)?|at least|min\.?|minimálně|alespoň)\s*$", level_context[:level.start()], re.I)))
            comparator = "at_least" if lower_bound else "exact" if level and re.search(
                r"exactly\s*$", level_context[:level.start()], re.I) else "unknown" if level else None
            span = Span(start=clause.start(), end=clause.end(), text=body)
            key = evidence.add(source, "languages", {"language": code, "requirement": requirement, "cefr": level.group(1).upper() if level else None, "cefr_comparator": comparator},
                               span=span, method=Method.PARSER, input_value=text)
            values.append(LanguageRequirement(language=code, requirement=requirement,
                cefr=level.group(1).upper() if level else None, cefr_or_higher=lower_bound if level else None, cefr_comparator=comparator,
                proficiency_wording=body.strip(), evidence_ids=[key]))
    return values


NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
EXPERIENCE = re.compile(r"(?<!\w)(?:(?:at least|minimum(?: of)?|min\.?|alespoň|alespon|minimálně|minimalne)\s+)?"
    r"(?P<min>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
    r"\s*(?:\+|(?:[-–—]|to|až|az)\s*(?P<max>\d{1,2}))?\s*(?:years?|let|roky|roků|roku)\b", re.I)


def experience(text, source, evidence):
    values = []
    for match in EXPERIENCE.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start:line_end if line_end >= 0 else len(text)]
        context = text[max(line_start, match.start()-55):min(line_start+len(line), match.end()+70)]
        if not re.search(r"experience|praxe|zkušenost|zkusenost", context, re.I) and line.strip() != match.group().strip():
            continue
        if re.search(r"founded|anniversary|company history|revenue|contract duration|our company|we have|we bring", context, re.I):
            continue
        number = match.group("min").lower()
        minimum = Decimal(NUMBER_WORDS[number] if number in NUMBER_WORDS else number)
        maximum = Decimal(match.group("max")) if match.group("max") else None
        if minimum > 60 or maximum is not None and (maximum > 60 or maximum < minimum):
            continue
        key = evidence.add(source, "experience", {"min_years": str(minimum), "max_years": str(maximum) if maximum is not None else None},
                           span=Span(start=match.start(), end=match.end(), text=match.group()), method=Method.PARSER, input_value=text)
        values.append(ExperienceConstraint(min_years=minimum, max_years=maximum,
                                          original_wording=context.strip(), evidence_ids=[key]))
    return values


HEADINGS = {
    "responsibilities": ["responsibilities", "what you'll do", "what you will do", "náplň práce", "co vás čeká", "your responsibilities"],
    "requirements": ["requirements", "what we're looking for", "what we are looking for", "what you'll bring", "what you will bring", "požadujeme", "co požadujeme", "co očekáváme"],
    "qualifications": ["qualifications", "your qualifications", "kvalifikace"],
    "benefits": ["benefits", "what we offer", "nabízíme", "co nabízíme", "co vám nabízíme", "perks"],
}


def sections(text, source, evidence):
    headings = []
    lookup = {fold(label): kind for kind, labels in HEADINGS.items() for label in labels}
    for line in re.finditer(r"[^\n]+", text):
        label = line.group().strip().strip("# :")
        kind = lookup.get(fold(label.replace("’", "'")))
        if kind:
            headings.append((line.start(), line.end(), label, kind))
    values = []
    for i, (start, heading_end, label, kind) in enumerate(headings):
        end = headings[i+1][0] if i+1 < len(headings) else len(text)
        body = text[heading_end:end].strip()
        if not body:
            continue
        key = evidence.add(source, "sections", kind, span=Span(start=start, end=end, text=text[start:end]), method=Method.PARSER, input_value=text)
        values.append(ContentSection(kind=kind, heading=label, content=body, start=start, end=end, evidence_ids=[key]))
    return values


def amount(value):
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def pay_period(value):
    return {"hour": "hour", "hourly": "hour", "1 hour": "hour", "per hour": "hour",
            "day": "day", "daily": "day", "month": "month", "monthly": "month", "1 month": "month",
            "year": "year", "yearly": "year", "annual": "year", "annually": "year", "1 year": "year",
            "one-time": "one_time", "one time": "one_time", "task": "task"}.get(fold(value).strip(), "unknown")


PAY_NUMBER = r"\d+(?:[ ,\u00a0]\d{3})*(?:\.\d{1,2})?\s*[kK]?"
PAY = re.compile(r"(?<!\w)(?P<prefix>€\s*)?(?P<min>" + PAY_NUMBER + r")"
    r"(?:\s*[-–—]\s*(?P<max>" + PAY_NUMBER + r"))?\s*"
    r"(?P<currency>CZK|Kč|EUR|USD|GBP)?\s*"
    r"(?:(?:/|per\s+)\s*(?P<unit>month|year|hour|day)|(?P<period>monthly|annually|annual|yearly|hourly|měsíčně|mesicne|ročně|rocne|hodinu))\b", re.I)
NON_SALARY = re.compile(r"bonus|referr|reward|voucher|revenue|budget|meal|benefit|sign.on|signing|stipend|cafeteria|multisport|pension|retirement contribution|wellness allowance|commuting allowance|travel allowance|příspěv|prispev|straven|obrat|odměn|odmen", re.I)


def salary(text, source, evidence):
    values = []
    for match in PAY.finditer(text):
        start = text.rfind("\n", 0, match.start()) + 1
        end = text.find("\n", match.end())
        line = text[start:end if end >= 0 else len(text)]
        if NON_SALARY.search(line):
            continue
        currency = "EUR" if match.group("prefix") else match.group("currency")
        if not currency:
            continue
        currency = "CZK" if currency.lower() == "kč" else currency.upper()
        def parse_number(value):
            if value is None:
                return None
            value = value.strip().lower()
            multiplier = 1000 if value.endswith("k") else 1
            parsed = amount(re.sub(r"[ ,\u00a0k]", "", value))
            return parsed * multiplier if parsed is not None else None
        minimum, maximum = parse_number(match.group("min")), parse_number(match.group("max"))
        if minimum is None or maximum is not None and maximum < minimum:
            continue
        period = pay_period(match.group("unit") or match.group("period"))
        if period == "unknown":
            period = {"měsíčně": "month", "mesicne": "month", "ročně": "year", "rocne": "year", "hodinu": "hour"}.get((match.group("period") or "").lower(), "unknown")
        # Location-specific lines are kept on their own offers; do not blend.
        locations = [label for alias, (label, _) in CITY_ALIASES.items()
                     if re.search(r"(?<!\w)"+re.escape(alias)+r"(?!\w)", fold(line))]
        if re.search(r"\bparis\b", line, re.I):
            locations.append("Paris")
        basis = "gross" if re.search(r"gross|hrub", line, re.I) else "net" if re.search(r"\bnet\b|čist|cist", line, re.I) else "unknown"
        key = evidence.add(source, "compensation", {"min": str(minimum), "max": str(maximum) if maximum else None, "currency": currency, "period": period},
                           span=Span(start=match.start(), end=match.end(), text=match.group()), method=Method.PARSER, input_value=text)
        values.append(CompensationOffer(min_amount=minimum, max_amount=maximum, currency=currency,
            period=period, gross_net_status=basis, component="base" if re.search(r"salary|base pay|mzda|plat\b", line, re.I) else "unknown",
            applicable_locations=list(dict.fromkeys(locations)) or None, original_text=line, evidence_ids=[key]))
    return values


def monetary_benefits(text, source, evidence):
    """Retain explicit monetary benefit lines without claiming salary offers."""
    values = []
    seen = set()
    benefit = re.compile(r"pension|retirement contribution|meal allowance|wellness allowance|commuting allowance|referr|voucher|příspěv|prispev|straven", re.I)
    for match in PAY.finditer(text):
        start = text.rfind("\n", 0, match.start()) + 1
        end = text.find("\n", match.end())
        end = end if end >= 0 else len(text)
        line = text[start:end]
        # Some native descriptions are one long line. Do not put an entire
        # job ad into a benefit assertion or link distant unrelated amounts.
        if len(line) > 400:
            start = max(start, match.start()-140)
            end = min(end, match.end()+80)
            if start and not text[start-1].isspace():
                boundary = text.find(" ", start, match.start())
                start = boundary+1 if boundary >= 0 else start
            line = text[start:end]
        if (start, end) in seen or not benefit.search(line) or not (match.group("prefix") or match.group("currency")):
            continue
        seen.add((start, end))
        values.append(evidence.fact(source, "monetary_benefits", line.strip(),
            span=Span(start=start, end=end, text=line), method=Method.PARSER, input_value=text))
    return values
