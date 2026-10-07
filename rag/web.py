from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from langchain_core.runnables.config import ContextThreadPoolExecutor
from langsmith import traceable

from rag.annotations import annotate
from rag.web_text import clean, focus, on_topic, page_metadata
from runtime.settings import Settings, require_key
from schemas.contracts import Evidence, Question


class WebSource:
    retryable = True
    # __init__ 를 거치지 않고 만든 객체(테스트의 수동 구성)는 네트워크 메타데이터 조회를 하지 않는다.
    metadata_fetch = False
    _metadata: dict[str, dict] | None = None

    def __init__(self, settings: Settings):
        self.key = require_key("TAVILY_API_KEY")
        self.timeout = settings.models.timeout_seconds
        self.k = settings.retrieval.web_top_k
        self.context_terms = list(settings.retrieval.web_context_terms)
        self.excerpt_chars = settings.retrieval.web_excerpt_chars
        self.metadata_fetch = settings.retrieval.web_metadata_fetch
        self._metadata: dict[str, dict] = {}  # url -> 페이지에서 읽은 메타데이터 (실행 내 캐시)

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
        return [annotate(e) for e in self.fill_metadata(out)]

    def fill_metadata(self, evidence: list[Evidence]) -> list[Evidence]:
        """비어 있는 발행일·발행 기관·저자를 페이지 메타 태그에서 채운다.

        Tavily 응답의 해당 필드는 거의 비어 있어 REFERENCE 검사에서 웹 출처마다 3건씩 걸렸다.
        페이지가 밝힌 값만 쓰고, 읽지 못하면 비워 둔다. 사람 주석(annotate)이 있으면 그 값이 우선한다.
        """
        if not self.metadata_fetch:
            return evidence
        if self._metadata is None:
            self._metadata = {}
        missing = [e for e in evidence if not (e.published_at and (e.publisher or e.authors))]
        urls = list(dict.fromkeys(e.url for e in missing if e.url not in self._metadata))
        if urls:
            with ContextThreadPoolExecutor(max_workers=4) as pool:
                for url, meta in zip(urls, pool.map(self._page_metadata, urls)):
                    self._metadata[url] = meta
        filled = []
        for e in evidence:
            meta = self._metadata.get(e.url) or {}
            updates = {k: v for k, v in meta.items() if v and not getattr(e, k)}
            filled.append(e.model_copy(update=updates) if updates else e)
        return filled

    @staticmethod
    def _page_metadata(url: str) -> dict:
        try:
            response = httpx.get(
                url,
                follow_redirects=True,
                timeout=8.0,
                headers={"User-Agent": "Mozilla/5.0 (kv-cache-agentic-rag reference check)"},
            )
            if response.status_code != 200 or "html" not in response.headers.get(
                "content-type", ""
            ):
                return {}
            return page_metadata(response.text[:400_000])
        except Exception:  # 네트워크 실패는 메타데이터 없음으로 둔다. 검색 결과 자체는 유지한다.
            return {}
