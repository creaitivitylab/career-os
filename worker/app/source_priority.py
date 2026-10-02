from collections.abc import Iterable


SOURCE_PRIORITY = {
    "smartrecruiters_direct": 300,
    "greenhouse_direct": 300,
    "workable_direct": 300,
    "ashby_direct": 300,
    "fantastic_jobs_apify": 200,
    "jooble_direct": 100,
}


def source_priority(sources: Iterable[str]) -> int:
    return max(
        (SOURCE_PRIORITY.get(source, 0) for source in sources),
        default=0,
    )
