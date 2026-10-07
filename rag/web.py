from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from langsmith import traceable

from rag.annotations import annotate
from rag.web_text import clean, focus, on_topic
from runtime.settings import Settings, require_key
from schemas.contracts import Evidence, Question

# 이해관계자 평가에서 논문 원문·미러·요약 사이트는 저자 자기 평가의 재게시라서 근거가 될 수 없다
# (prompts/stakeholder 규칙 7-1). 생성 단계에서 버리기 전에 검색 단계에서 걸러서, 그 자리를 실제 토론·비교 자료가 채우게 한다.
STAKEHOLDER_CRITERIA = {"competitors", "adopters", "industry"}
PAPER_MIRROR_HOSTS = ("arxiv.org", "ar5iv.org", "alphaxiv.org", "liner.com")


def is_paper_mirror(url: str, criterion: str) -> bool:
    if criterion not in STAKEHOLDER_CRITERIA:
        return False
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in PAPER_MIRROR_HOSTS)


class WebSource:
    retryable = True

    def __init__(self, settings: Settings):
        self.key = require_key("TAVILY_API_KEY")
        self.timeout = settings.models.timeout_seconds
        self.k = min(settings.retrieval.top_k, 5)
        self.context_terms = list(settings.retrieval.web_context_terms)
        self.excerpt_chars = settings.retrieval.web_excerpt_chars

    @traceable(run_type="retriever", name="web_search")
    def search(self, question: Question, attempt: int, scope: str = "target") -> list[Evidence]:
        # 질의는 호출자가 층별 검색어까지 붙여 넘긴다. 기술명 단독 검색은 하지 않는다.
        anchor = (
            ""
            if any(t.lower() in question.text.lower() for t in self.context_terms)
            else " ".join(self.context_terms[:2])
        )
        prefix = question.technology if scope == "target" else ""
        query = f"{prefix} {question.text} {anchor}".strip()
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
            raw = item.get("raw_content")
            if not raw:
                continue
            url = item["url"]
            if is_paper_mirror(url, question.criterion):
                continue
            title = item.get("title", url)
            body = clean(raw)
            if not body or not on_topic(body, title, self.context_terms):
                continue
            text = focus(body, f"{question.technology} {question.text}", self.excerpt_chars)
            if not text:
                continue
            out.append(
                Evidence(
                    id="web-"
                    + hashlib.sha256(
                        (question.technology + scope + url + text).encode()
                    ).hexdigest()[:16],
                    text=text,
                    title=title,
                    url=url,
                    technology=question.technology,
                    source_type="web",
                    scope=scope,  # 검색어 층: direct=target, background=context
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    published_at=item.get("published_date"),
                    authors=item.get("author"),
                    publisher=item.get("publisher"),
                    site=urlparse(url).hostname,
                )
            )
        return [annotate(e) for e in out]
