"""웹 출처 메타데이터: 페이지가 밝힌 값만 읽고, 없으면 비워 둔다."""

from rag.web_text import page_metadata


def test_reads_standard_meta_tags_and_normalizes_date():
    html = (
        '<html><head><meta property="og:site_name" content="Samsung Semiconductor">'
        '<meta property="article:published_time" content="2025-03-12T09:00:00+09:00">'
        '<meta name="citation_author" content="Kim, A"><meta name="citation_author" content="Lee, B">'
        '</head><body><time datetime="2024-01-01">old</time></body></html>'
    )
    assert page_metadata(html) == {
        "published_at": "2025-03-12",
        "publisher": "Samsung Semiconductor",
        "authors": "Kim, A; Lee, B",
    }


def test_falls_back_to_json_ld_then_time_tag():
    ld = (
        '<script type="application/ld+json">{"@type":"NewsArticle",'
        '"datePublished":"2026-03-16T13:00:00Z","publisher":{"name":"Business Wire"}}</script>'
    )
    assert page_metadata(ld) == {
        "published_at": "2026-03-16",
        "publisher": "Business Wire",
        "authors": None,
    }
    assert page_metadata('<time datetime="2023-11-02T10:00">x</time>')["published_at"] == "2023-11-02"


def test_missing_metadata_stays_none_not_guessed():
    assert page_metadata("<html><body>Published yesterday by us.</body></html>") == {
        "published_at": None,
        "publisher": None,
        "authors": None,
    }
    # 날짜처럼 보여도 범위 밖이면 쓰지 않는다.
    assert page_metadata('<meta name="date" content="0001-13-45">')["published_at"] is None


def test_manual_source_skips_network_fetch():
    from rag.web import WebSource

    source = WebSource.__new__(WebSource)
    assert source.metadata_fetch is False
    assert source.fill_metadata([]) == []
