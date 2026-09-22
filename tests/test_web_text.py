"""웹 원문 정제·발췌·주제 필터."""

from rag.web_text import clean, focus, on_topic

TERMS = ["KV cache", "CXL", "양자화"]

PAGE = """Skip to main content

Home | Products | Pricing | Contact

JavaScript is disabled in your browser. Please enable JavaScript to continue.

KIVI는 KV cache 를 2비트로 양자화해 A100 단일 GPU 에서 처리량을 2.35~3.47배 높였다고 보고한다.

회사 연혁과 투자 단계 정보를 제공하는 일반 기업 정보 페이지 안내 문단입니다.

© 2026 Example Inc. All rights reserved.
"""


def test_clean_drops_boilerplate_and_navigation():
    body = clean(PAGE)
    assert "JavaScript is disabled" not in body
    assert "All rights reserved" not in body
    assert "Home | Products" not in body
    assert "2.35~3.47배" in body


def test_focus_keeps_the_question_related_paragraph_within_the_limit():
    excerpt = focus(clean(PAGE), "KIVI KV cache 양자화 처리량", 200)
    assert len(excerpt) <= 200
    assert "양자화" in excerpt


def test_on_topic_rejects_same_name_pages_without_domain_terms():
    assert on_topic("KV cache 를 다루는 문서", "KIVI", TERMS)
    assert not on_topic("스마트 도어락 신제품 출시", "KIWI 도어락", TERMS)
    # 용어 목록이 비어 있으면 필터를 적용하지 않는다.
    assert on_topic("스마트 도어락", "KIWI", [])
