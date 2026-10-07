"""이해관계자 검색에서 논문 미러 출처만 걸러내는지 확인한다."""

import pytest

from rag.web import is_paper_mirror, publisher_for


@pytest.mark.parametrize(
    "url",
    [
        "https://arxiv.org/abs/2402.02750",
        "https://ar5iv.labs.arxiv.org/html/2402.02750",
        "https://www.alphaxiv.org/abs/2402.02750",
        "https://liner.com/review/kivi",
    ],
)
def test_paper_mirrors_are_dropped_for_stakeholder_criteria(url):
    for criterion in ("competitors", "adopters", "industry"):
        assert is_paper_mirror(url, criterion)


def test_discussion_sources_are_kept_for_stakeholder_criteria():
    for url in (
        "https://news.example.com/kivi-review",
        "https://github.com/some-org/some-repo/issues/12",
        "https://www.reddit.com/r/LocalLLaMA/comments/abc",
    ):
        assert not is_paper_mirror(url, "adopters")


def test_other_criteria_keep_paper_mirrors():
    # 기술·시장 평가는 논문 근거를 정당하게 쓰므로 필터를 적용하지 않는다.
    assert not is_paper_mirror("https://arxiv.org/abs/2402.02750", "performance")
    assert not is_paper_mirror("https://arxiv.org/abs/2402.02750", "cost")


def test_lookalike_hosts_are_not_dropped():
    # 호스트 접미사 비교라서 "notarxiv.org" 같은 이름은 걸리지 않아야 한다.
    assert not is_paper_mirror("https://notarxiv.org/post", "adopters")


def test_publisher_falls_back_to_site_host_when_api_omits_it():
    # Tavily 기본 응답에는 publisher가 없어서 호스트명으로 채운다.
    assert publisher_for({}, "https://www.example.org/post/1") == "www.example.org"


def test_api_publisher_wins_when_present():
    assert (
        publisher_for({"publisher": "Example Press"}, "https://x.example.org/a") == "Example Press"
    )
