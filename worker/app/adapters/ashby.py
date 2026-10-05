from typing import Any

import httpx


class AshbyAdapter:
    BASE_URL = (
        "https://api.ashbyhq.com/"
        "posting-api/job-board"
    )

    def list_jobs(
        self,
        board_name: str,
    ) -> list[dict[str, Any]]:

        url = (
            f"{self.BASE_URL}/"
            f"{board_name}"
        )

        with httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                "User-Agent": "CareerOS/0.1",
            },
        ) as client:

            response = client.get(
                url,
                params={
                    "includeCompensation":
                        "true",
                },
            )

        if response.status_code >= 400:
            raise RuntimeError(
                "Ashby board error "
                f"{response.status_code}: "
                f"{response.text[:500]}"
            )

        data = response.json()

        if not isinstance(data, dict):
            raise RuntimeError(
                "Unexpected Ashby response"
            )

        jobs = data.get("jobs")

        if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
            raise RuntimeError(
                "Unexpected Ashby jobs response"
            )

        return jobs
