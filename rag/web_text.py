"""웹 원문 정제·발췌.

Tavily raw_content 는 메뉴, 쿠키 안내, 스크립트 경고가 섞인 페이지 전체다. 그대로 두면
근거 하나가 12,000자까지 늘어나 노드 입력이 수 MB가 되고, 생성 호출이 제한 시간을 넘긴다
(live 점검에서 report 노드가 OpenAITimeoutError 로 실패). 질문과 관련된 문단만 남긴다.
"""

from __future__ import annotations

import re

BOILERPLATE = re.compile(
    r"javascript is (disabled|required)|enable javascript|cookie|개인정보\s*처리방침|"
    r"all rights reserved|sign\s?in|log\s?in|subscribe|newsletter|privacy policy|"
    r"terms of (use|service)|skip to (main )?content|advertisement",
    re.IGNORECASE,
)
WORD = re.compile(r"[A-Za-z][A-Za-z0-9+\-]{1,}|[가-힣]{2,}|\d+(?:\.\d+)?")


def terms(text: str) -> set[str]:
    return {w.lower() for w in WORD.findall(text)}


CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: str) -> str:
    """상용구·메뉴 줄을 버리고 문단만 남긴다.

    PDF 에서 긁힌 웹 페이지에는 폼피드 같은 제어문자가 섞여 있다. 그대로 넘기면 모델
    출력까지 오염된다 (live 점검에서 등급 칸이 제어문자가 낀 문자열로 나왔다).
    """
    text = CONTROL.sub(" ", text)
    kept = []
    for block in re.split(r"\n\s*\n", text):
        block = re.sub(r"[ \t]+", " ", block).strip()
        if not block or BOILERPLATE.search(block):
            continue
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        # 짧은 줄만 모인 덩어리는 대개 내비게이션 목록이다.
        body = [ln for ln in lines if len(ln) >= 40]
        if not body and len(" ".join(lines)) < 80:
            continue
        kept.append(" ".join(body or lines))
    return "\n\n".join(kept)


def focus(text: str, query: str, limit: int) -> str:
    """질문 용어와 겹치는 문단부터 limit 자까지 모은다."""
    blocks = [b for b in text.split("\n\n") if b]
    if not blocks:
        return text[:limit]
    wanted = terms(query)
    scored = sorted(
        enumerate(blocks),
        key=lambda pair: (-len(wanted & terms(pair[1])) / (1 + len(pair[1]) / 2000), pair[0]),
    )
    chosen, total = [], 0
    for index, block in scored:
        if total >= limit:
            break
        room = limit - total
        chosen.append((index, block[:room]))
        total += min(len(block), room)
    return "\n\n".join(block for _, block in sorted(chosen))


def on_topic(text: str, title: str, context_terms: list[str]) -> bool:
    """도메인 용어가 하나도 없으면 같은 이름의 무관한 문서로 본다.

    KIVI(과일 kiwi, Kivy 프레임워크, 동명 회사), ITME(일반 IT 관리 도구)처럼 기술명이
    흔한 단어와 겹쳐 무관한 페이지가 검색된다. 기술명만으로는 걸러지지 않는다.
    """
    if not context_terms:
        return True
    haystack = f"{title}\n{text}".lower()
    return any(term.lower() in haystack for term in context_terms)
