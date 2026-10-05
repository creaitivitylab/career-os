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
        seen = set()
        pages = 0

        with httpx.Client(timeout=60.0) as client:

            while True:
                pages += 1
                if pages > 1000:
                    raise RuntimeError("SmartRecruiters pagination safety limit reached")

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

                if not isinstance(data, dict):
                    raise RuntimeError("Unexpected SmartRecruiters inventory")
                content, total = data.get("content"), data.get("totalFound")
                if (not isinstance(content, list) or type(total) is not int or total < 0
                        or any(not isinstance(job, dict) or not job.get("id") for job in content)):
                    raise RuntimeError("Incomplete SmartRecruiters inventory metadata")
                identities = {str(job["id"]) for job in content}
                if content and (len(identities) != len(content) or identities & seen):
                    raise RuntimeError("SmartRecruiters pagination repeated identities")
                seen.update(identities)
                jobs.extend(content)

                if not content:
                    if len(jobs) != total:
                        raise RuntimeError("SmartRecruiters empty page contradicts native total")
                    break

                offset += len(content)

                if offset >= total:
                    if offset != total:
                        raise RuntimeError("SmartRecruiters listing count contradicts native total")
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
