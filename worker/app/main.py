import os
from typing import Literal

import psycopg
from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.merge import merge_duplicate_candidate

from app.ingestion import ingest_jooble_search
from app.fantastic_ingestion import (
    ingest_fantastic_jobs,
)

from app.smartrecruiters_ingestion import (
    ingest_smartrecruiters_jobs,
)

from app.greenhouse_ingestion import (
    ingest_greenhouse_jobs,
)

from app.dedupe import (
    rebuild_duplicate_candidates,
    get_safe_auto_merge_candidates,
)

app = FastAPI(
    title="Career OS Job Engine",
    version="0.3.0",
)


class JoobleIngestionRequest(BaseModel):
    keywords: str
    location: str = "Czech Republic"
    page: int = 1


class FantasticIngestionRequest(BaseModel):
    time_range: Literal[
        "1h",
        "24h",
        "7d",
        "6m",
    ] = "24h"

    location: str = "Czechia"

    limit: int = Field(
        default=10,
        ge=10,
        le=5000,
    )

class SmartRecruitersIngestionRequest(BaseModel):
    company_identifier: str | None = None
    country: str = "cz"

    max_companies: int | None = Field(
        default=None,
        ge=1,
        le=500,
    )


class GreenhouseIngestionRequest(BaseModel):
    board_token: str | None = None

    max_boards: int | None = Field(
        default=None,
        ge=1,
        le=500,
    )


class DuplicateMergeRequest(BaseModel):
    candidate_id: str
    dry_run: bool = True


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "career-os-job-engine",
    }


@app.get("/health/db")
def database_health():
    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select current_database(), now()"
            )

            database, server_time = cur.fetchone()

    return {
        "status": "ok",
        "database": database,
        "server_time": server_time,
    }


@app.post("/ingest/jooble")
def ingest_jooble(
    request: JoobleIngestionRequest
):
    return ingest_jooble_search(
        keywords=request.keywords,
        location=request.location,
        page=request.page,
    )


@app.post("/ingest/fantastic")
def ingest_fantastic(
    request: FantasticIngestionRequest
):
    return ingest_fantastic_jobs(
        time_range=request.time_range,
        location=request.location,
        limit=request.limit,
    )

@app.post("/ingest/ats/smartrecruiters")
def ingest_smartrecruiters(
    request: SmartRecruitersIngestionRequest
):
    return ingest_smartrecruiters_jobs(
        company_identifier=(
            request.company_identifier
        ),
        country=request.country,
        max_companies=request.max_companies,
    )


@app.post("/ingest/ats/greenhouse")
def ingest_greenhouse(
    request: GreenhouseIngestionRequest
):
    return ingest_greenhouse_jobs(
        board_token=request.board_token,
        max_boards=request.max_boards,
    )


@app.post("/dedupe/merge")
def dedupe_merge(
    request: DuplicateMergeRequest
):
    return merge_duplicate_candidate(
        candidate_id=request.candidate_id,
        dry_run=request.dry_run,
    )

@app.post("/dedupe/rebuild")
def dedupe_rebuild():
    return rebuild_duplicate_candidates()


@app.get("/dedupe/safe-candidates")
def dedupe_safe_candidates():
    candidates = get_safe_auto_merge_candidates()

    return {
        "count": len(candidates),
        "candidates": candidates,
    }
