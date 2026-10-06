"""Reproducible local-only baseline and blank human review set.

Usage: python -m app.job_profile.evaluate --sample sample.json --output /tmp/review
Input is an archived snapshot containing jobs and their attached sources.
"""
import argparse
import csv
import html
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .evidence import semantic_hash
from .models import Fact, Method, ValueState
from .pipeline import build_profile
from .projectors import get
from .selection import evidence_rank


SEED = "career-os-job-profile-human-v1"
MANUAL_FIELDS = ["normalized_role", "job_family", "career_level", "workplace", "employment_schedule",
                 "relationship", "compensation", "technologies", "languages", "experience", "reviewer_notes"]


def fields(profile):
    locations = profile.workplace.locations.value or []
    return {
        "country": [loc.country for loc in locations], "city": [loc.city for loc in locations],
        "region": [loc.region for loc in locations], "workplace": [profile.workplace.mode],
        "employment_schedule": [profile.employment.schedule], "relationship": [profile.employment.relationship],
        "career_level": [profile.role.career_level], "role_marker": [profile.role.leadership_markers, profile.role.career_level],
        "compensation": [profile.compensation.offers], "technologies": [profile.requirements.technologies],
        "languages": [profile.requirements.languages], "experience": [profile.requirements.experience],
        "content_sections": [profile.content.sections],
    }


def coverage_category(profile, field, facts):
    available = [f for f in facts if f.state == ValueState.KNOWN]
    if not available:
        if any(f.state == ValueState.CONFLICT for f in facts):
            return "conflict"
        aliases = {"workplace": "workplace", "employment_schedule": "schedule", "career_level": "career_level",
                   "compensation": "compensation", "languages": "languages"}
        if any(e.extraction_method == Method.SUGGESTION and e.field == aliases.get(field, field) for e in profile.evidence):
            return "provider_suggestion_only"
        return "unknown_or_not_mentioned"
    records = {e.id: e for e in profile.evidence}
    # For scalar resolution, lower-quality alternatives are retained in evidence
    # but must not become the reported winning method.
    ids = [i for f in available for i in f.evidence_ids if i in records]
    ranked = sorted((records[i] for i in ids), key=lambda e: evidence_rank(field, e), reverse=True)
    if not ranked:
        return "unknown_or_not_mentioned"
    return {Method.NATIVE: "native_structured", Method.PARSER: "deterministic_parsed",
            Method.PROVIDER: "provider_structured", Method.SUGGESTION: "provider_suggestion_only"}[ranked[0].extraction_method]


def choose_review_jobs(jobs, profiles, snapshot, count=200):
    """Greedy source quotas plus inverse-frequency diversity, deterministic ties."""
    now = datetime.fromisoformat(snapshot.replace("Z", "+00:00"))
    eligible = []
    for job in jobs:
        recent = any(s.get("is_active") is True and s.get("last_seen_at")
            and datetime.fromisoformat(s["last_seen_at"].replace("Z", "+00:00")) >= now-timedelta(days=7)
            for s in job.get("sources", []))
        if job.get("status") == "active" and recent:
            eligible.append(job)
    sources = sorted({j.get("primary_stratum", "unknown") for j in eligible})
    quotas = {s: count//len(sources) + (i < count % len(sources)) for i, s in enumerate(sources)} if sources else {}
    selected, taken = [], set()
    used = Counter()
    employers = Counter()
    def buckets(job):
        result = list((job.get("_strata") or {}).items())
        p = profiles[job["id"]]
        result.append(("salary", "available" if p.compensation.offers.state == ValueState.KNOWN else "unknown"))
        return result
    def score(job):
        return sum(1/(1+used[(k, str(v))]) for k, v in buckets(job)) + 1/(1+employers[job.get("company_key") or job.get("company")])
    for source in sources:
        pool = [j for j in eligible if j.get("primary_stratum", "unknown") == source]
        for _ in range(min(quotas[source], len(pool))):
            candidates = [j for j in pool if j["id"] not in taken]
            best = max(candidates, key=lambda j: (score(j), semantic_hash([SEED, j["id"]])))
            selected.append(best)
            taken.add(best["id"])
            used.update((k, str(v)) for k, v in buckets(best))
            employers[best.get("company_key") or best.get("company")] += 1
    # Small pools may not fill quota: fill remaining seats with the same diversity rule.
    while len(selected) < min(count, len(eligible)):
        best = max((j for j in eligible if j["id"] not in taken), key=lambda j: (score(j), semantic_hash([SEED, j["id"]])))
        selected.append(best)
        taken.add(best["id"])
        used.update((k, str(v)) for k, v in buckets(best))
        employers[best.get("company_key") or best.get("company")] += 1
    return selected


def evaluate(sample_path, output, review_count=200):
    sample_path, output = Path(sample_path), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    data = json.loads(sample_path.read_text())
    jobs = data["jobs"]
    profiles, counts, quality, selected_sources = {}, {}, Counter(), Counter()
    generated = datetime.fromisoformat(data["snapshot"].replace("Z", "+00:00"))
    with (output/"profiles.jsonl").open("w") as stream:
        for job in jobs:
            profile = build_profile(job, generated_at=generated)
            profiles[job["id"]] = profile
            stream.write(profile.model_dump_json()+"\n")
            for field, facts in fields(profile).items():
                counts.setdefault(field, Counter()).update([coverage_category(profile, field, facts)])
            quality.update([profile.content.description_quality])
            selected_sources.update([profile.content.selected_source_name or "none"])
    review = choose_review_jobs(jobs, profiles, data["snapshot"], review_count)
    with (output/"review.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["job_id", "company", "title", "source", "review_status", *MANUAL_FIELDS])
        writer.writeheader()
        for job in review:
            writer.writerow({"job_id": job["id"], "company": job.get("company"), "title": job["title"],
                "source": profiles[job["id"]].content.selected_source_name, "review_status": "pending"})
    pages = ["<!doctype html><meta charset='utf-8'><title>Job Profile V1 human review</title>",
        "<style>body{font:16px system-ui;max-width:1000px;margin:30px auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}article{border-top:2px solid #aaa;padding:20px 0}details{margin:12px 0}</style>",
        "<h1>Human evaluation set</h1><p>Label review.csv independently. Predictions and provider AI are references, not ground truth. This is an archived active/recent snapshot.</p>"]
    with (output/"review.jsonl").open("w") as stream:
        for job in review:
            p = profiles[job["id"]]
            suggestions = [e.model_dump(mode="json") for e in p.evidence if e.extraction_method == Method.SUGGESTION]
            native_inputs = []
            for e in p.evidence:
                if (e.extraction_method in {Method.NATIVE, Method.PROVIDER} and e.native_field_path
                        and e.field in {"workplace", "schedule", "relationship", "duration", "compensation", "city", "region", "country", "education", "native_level"}):
                    source = next((s for s in job["sources"] if s["source_name"] == e.source_name and s["source_job_id"] == e.source_job_id), None)
                    if source:
                        native_inputs.append({"source_name": e.source_name, "source_job_id": e.source_job_id,
                            "path": e.native_field_path, "raw_value": get(source.get("raw_payload") or {}, e.native_field_path.removeprefix("raw_payload."))})
            item = {"job_id": job["id"], "company": job.get("company"), "title": job["title"],
                "description": p.content.cleaned_description.value, "description_source": p.content.selected_source_name,
                "review_status": "pending", "manual_labels": {field: None for field in MANUAL_FIELDS},
                "structured_source_inputs": native_inputs,
                "baseline_prediction": {"role": p.role.model_dump(mode="json"), "workplace": p.workplace.model_dump(mode="json"),
                    "employment": p.employment.model_dump(mode="json"), "compensation": p.compensation.model_dump(mode="json"),
                    "requirements": p.requirements.model_dump(mode="json")}, "provider_suggestions_reference_only": suggestions}
            stream.write(json.dumps(item, ensure_ascii=False)+"\n")
            pages.extend(["<article><h2>"+html.escape(str(job.get("company") or ""))+" — "+html.escape(job["title"])+"</h2>",
                "<p>Job "+html.escape(job["id"])+" · "+html.escape(p.content.selected_source_name or "No text")+"</p>",
                "<pre>"+html.escape(p.content.cleaned_description.value or "No usable description")+"</pre>",
                "<details><summary>Original structured source fields</summary><pre>"+html.escape(json.dumps(native_inputs,ensure_ascii=False,indent=2))+"</pre></details>",
                "<details><summary>Baseline prediction (not ground truth)</summary><pre>"+html.escape(json.dumps(item["baseline_prediction"],ensure_ascii=False,indent=2))+"</pre></details>",
                "<details><summary>Provider suggestions (not ground truth)</summary><pre>"+html.escape(json.dumps(suggestions,ensure_ascii=False,indent=2))+"</pre></details></article>"])
    (output/"review.html").write_text("\n".join(pages))
    unresolved = sum(any(f.state != ValueState.KNOWN for f in [p.role.job_family, p.role.people_management,
        p.requirements.required_skills, p.requirements.preferred_skills, p.requirements.certifications]) for p in profiles.values())
    summary = {"sample_jobs": len(jobs), "snapshot": data["snapshot"], "input_file_hash": semantic_hash(data), "seed": SEED,
        "coverage_counts": counts, "description_quality": quality, "selected_description_sources": selected_sources,
        "review_jobs": len(review), "review_primary_source_counts": Counter(j.get("primary_stratum", "unknown") for j in review),
        "review_strata": {key: Counter(str((j.get("_strata") or {}).get(key)) for j in review)
                         for key in ("language_proxy", "location", "title_family_proxy", "title_level_proxy", "workplace", "employer_catalog_size")},
        "jobs_with_at_least_one_unresolved_semantic_target": unresolved,
        "note": "Coverage, not accuracy. Exclusive route per field/job; provider_structured separate from employer native. Unknown includes not_mentioned/insufficient. No human labels completed; no provider AI used as truth."}
    (output/"baseline.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n")
    categories = ["native_structured", "deterministic_parsed", "provider_structured", "provider_suggestion_only", "unknown_or_not_mentioned", "conflict"]
    with (output/"coverage.csv").open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["field", *categories])
        for field, values in counts.items():
            writer.writerow([field, *[values[k] for k in categories]])
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--review-count", type=int, default=200)
    args = parser.parse_args()
    if not 1 <= args.review_count <= 1000:
        parser.error("review-count must be between 1 and 1000")
    evaluate(args.sample, args.output, args.review_count)
