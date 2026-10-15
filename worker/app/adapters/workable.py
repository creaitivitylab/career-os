from app.operation_budget import check_deadline
from app.transient_retry import UpstreamHTTPError
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

            check_deadline()
            response = client.get(
                url,
                params={
                    "details": "true",
                },
            )

        if response.status_code >= 400:
            raise UpstreamHTTPError(response.status_code, 'Workable account', response.headers.get('Retry-After'))

        data = response.json()

        if not isinstance(data, dict):
            raise RuntimeError(
                "Unexpected Workable response"
            )

        jobs = data.get("jobs")

        if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
            raise RuntimeError("Unexpected Workable jobs response; inventory completeness unknown")

        return data
