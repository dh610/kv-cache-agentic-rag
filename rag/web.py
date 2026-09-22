from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from langsmith import traceable

from runtime.settings import Settings, require_key
from schemas.contracts import Evidence, Question


class WebSource:
    retryable = True

    def __init__(self, settings: Settings):
        self.key = require_key("TAVILY_API_KEY")
        self.timeout = settings.models.timeout_seconds
        self.k = min(settings.retrieval.top_k, 5)

    @traceable(run_type="retriever", name="web_search")
    def search(self, question: Question, attempt: int) -> list[Evidence]:
        query = f"{question.technology} {question.text}"
        if attempt > 1:
            query += " limitations evidence official" if attempt == 2 else " deployment evaluation"
        # Client does not receive API key via trace arguments.
        response = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self.key,
                "query": query,
                "max_results": self.k,
                "include_raw_content": True,
                "search_depth": "basic",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        out = []
        for item in response.json().get("results", []):
            # Snippets alone are not treated as verified page evidence.
            text = item.get("raw_content")
            if not text:
                continue
            url = item["url"]
            out.append(
                Evidence(
                    id="web-"
                    + hashlib.sha256((question.technology + url + text).encode()).hexdigest()[:16],
                    text=text[:12000],
                    title=item.get("title", url),
                    url=url,
                    technology=question.technology,
                    source_type="web",
                    scope="context",
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    published_at=item.get("published_date"),
                    authors=item.get("author"),
                    publisher=item.get("publisher"),
                    site=urlparse(url).hostname,
                )
            )
        return out
