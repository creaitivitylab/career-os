import os
from typing import Any

import httpx


class JoobleDirectAdapter:
    def __init__(self) -> None:
        self.api_key = os.environ["JOOBLE_API_KEY"]
        self.base_url = f"https://cz.jooble.org/api/{self.api_key}"

    def search(
        self,
        keywords: str,
        location: str = "Czech Republic",
        page: int = 1,
        radius: int | None = None,
        salary: int | None = None,
        results_per_page: int = 20,
    ) -> dict[str, Any]:

        payload: dict[str, Any] = {
            "keywords": keywords,
            "location": location,
            "page": page,
            "ResultOnPage": results_per_page,
            "companysearch": False,
        }

        if radius is not None:
            payload["radius"] = str(radius)

        if salary is not None:
            payload["salary"] = salary

        headers = {
            "content-type": "application/json",
        }

        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                self.base_url,
                json=payload,
                headers=headers,
            )

        if response.status_code >= 400:
            raise RuntimeError(
                f"Jooble API error {response.status_code}: "
                f"{response.text[:500]}"
            )

        data = response.json()

        return {
            "data": data,
            "rate_limit": None,
        }
