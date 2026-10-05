from typing import Any

import httpx


class WorkableAdapter:
    BASE_URL = "https://www.workable.com/api/accounts"

    def get_account(
        self,
        tenant_slug: str,
    ) -> dict[str, Any]:

        url = (
            f"{self.BASE_URL}/"
            f"{tenant_slug}"
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
                    "details": "true",
                },
            )

        if response.status_code >= 400:
            raise RuntimeError(
                "Workable account error "
                f"{response.status_code}: "
                f"{response.text[:500]}"
            )

        data = response.json()

        if not isinstance(data, dict):
            raise RuntimeError(
                "Unexpected Workable response"
            )

        jobs = data.get("jobs")

        if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
            raise RuntimeError("Unexpected Workable jobs response; inventory completeness unknown")

        return data
