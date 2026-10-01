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
            raise RuntimeError(
                "Greenhouse list error "
                f"{response.status_code}: "
                f"{response.text[:500]}"
            )

        data = response.json()

        jobs = data.get("jobs") or []

        if not isinstance(jobs, list):
            raise RuntimeError(
                "Unexpected Greenhouse jobs response"
            )

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
            raise RuntimeError(
                "Greenhouse detail error "
                f"{response.status_code}: "
                f"{response.text[:500]}"
            )

        data = response.json()

        if not isinstance(data, dict):
            raise RuntimeError(
                "Unexpected Greenhouse detail response"
            )

        return data
