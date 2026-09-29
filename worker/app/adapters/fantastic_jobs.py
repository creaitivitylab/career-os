import os
from typing import Any

import httpx


class FantasticJobsApifyAdapter:
    BASE_URL = (
        "https://api.apify.com/v2/actors/"
        "fantastic-jobs~career-site-job-listing-api/"
        "run-sync-get-dataset-items"
    )

    def __init__(self) -> None:
        self.token = os.environ["APIFY_TOKEN"]

    def fetch(
        self,
        time_range: str = "24h",
        location: str = "Czechia",
        limit: int = 10,
    ) -> list[dict[str, Any]]:

        payload = {
            "timeRange": time_range,
            "limit": limit,
            "locationSearch": [location],
            "descriptionType": "text",
            "includeCompanyDetails": True,
            "populateAiRemoteLocationDerived": True,
        }

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=300.0) as client:
            response = client.post(
                self.BASE_URL,
                json=payload,
                headers=headers,
            )

        if response.status_code >= 400:
            raise RuntimeError(
                f"Fantastic Jobs / Apify error "
                f"{response.status_code}: "
                f"{response.text[:500]}"
            )

        data = response.json()

        if not isinstance(data, list):
            raise RuntimeError(
                "Unexpected Fantastic Jobs response format"
            )

        return data
