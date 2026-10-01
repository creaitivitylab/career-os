from typing import Any

import httpx


class SmartRecruitersAdapter:
    BASE_URL = (
        "https://api.smartrecruiters.com/v1/companies"
    )

    def list_postings(
        self,
        company_identifier: str,
        country: str = "cz",
    ) -> list[dict[str, Any]]:

        jobs: list[dict[str, Any]] = []

        limit = 100
        offset = 0

        with httpx.Client(timeout=60.0) as client:

            while True:

                url = (
                    f"{self.BASE_URL}/"
                    f"{company_identifier}/postings"
                )

                response = client.get(
                    url,
                    params={
                        "limit": limit,
                        "offset": offset,
                        "country": country,
                        "destination": "PUBLIC",
                    },
                    headers={
                        "Accept": "application/json",
                    },
                )

                if response.status_code >= 400:
                    raise RuntimeError(
                        "SmartRecruiters list error "
                        f"{response.status_code}: "
                        f"{response.text[:500]}"
                    )

                data = response.json()

                content = data.get("content") or []

                jobs.extend(content)

                total = int(
                    data.get(
                        "totalFound",
                        len(jobs),
                    )
                )

                if not content:
                    break

                offset += len(content)

                if offset >= total:
                    break

        return jobs

    def get_posting(
        self,
        company_identifier: str,
        posting_id: str,
    ) -> dict[str, Any]:

        url = (
            f"{self.BASE_URL}/"
            f"{company_identifier}/postings/"
            f"{posting_id}"
        )

        with httpx.Client(timeout=60.0) as client:

            response = client.get(
                url,
                headers={
                    "Accept": "application/json",
                },
            )

        if response.status_code >= 400:
            raise RuntimeError(
                "SmartRecruiters detail error "
                f"{response.status_code}: "
                f"{response.text[:500]}"
            )

        data = response.json()

        if not isinstance(data, dict):
            raise RuntimeError(
                "Unexpected SmartRecruiters response"
            )

        return data
