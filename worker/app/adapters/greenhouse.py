from app.operation_budget import check_deadline
from app.transient_retry import UpstreamHTTPError
from typing import Any

import httpx


class GreenhouseAdapter:
    BASE_URL = (
        "https://boards-api.greenhouse.io/"
        "v1/boards"
    )

    def list_jobs(
        self,
        board_token: str,
    ) -> list[dict[str, Any]]:

        url = (
            f"{self.BASE_URL}/"
            f"{board_token}/jobs"
        )

        with httpx.Client(timeout=60.0) as client:
            check_deadline()
            response = client.get(
                url,
                params={
                    "content": "true",
                },
                headers={
                    "Accept": "application/json",
                },
            )

        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code, 'Greenhouse list', response.headers.get('Retry-After'))

        data = response.json()

        jobs = data.get("jobs")

        if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
            raise RuntimeError(
                "Unexpected Greenhouse jobs response"
            )

        meta = data.get("meta")
        if isinstance(meta, dict) and "total" in meta and meta["total"] != len(jobs):
            raise RuntimeError("Greenhouse inventory count contradicts returned jobs")

        return jobs

    def get_job(
        self,
        board_token: str,
        job_id: str,
    ) -> dict[str, Any]:

        url = (
            f"{self.BASE_URL}/"
            f"{board_token}/jobs/"
            f"{job_id}"
        )

        with httpx.Client(timeout=60.0) as client:
            check_deadline()
            response = client.get(
                url,
                params={
                    "pay_transparency": "true",
                },
                headers={
                    "Accept": "application/json",
                },
            )

        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code, 'Greenhouse detail', response.headers.get('Retry-After'))

        data = response.json()

        if not isinstance(data, dict):
            raise RuntimeError(
                "Unexpected Greenhouse detail response"
            )

        return data
