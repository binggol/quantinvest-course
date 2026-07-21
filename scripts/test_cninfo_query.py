from __future__ import annotations

import pytest

from scripts.cninfo_query import CNInfoQueryError, query_announcements


class Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_query_retries_a_page_without_skipping_it():
    calls = []

    def post(_url, **kwargs):
        calls.append(dict(kwargs["data"]))
        if len(calls) < 3:
            raise TimeoutError("provider timeout")
        return Response({"announcements": [{"secCode": "000001"}]})

    rows = query_announcements(
        "问询函",
        "2026-07-01~2026-07-18",
        "szse",
        max_pages=3,
        request_post=post,
        sleep=lambda _seconds: None,
    )

    assert [row["secCode"] for row in rows] == ["000001"]
    assert [call["pageNum"] for call in calls] == [1, 1, 1]


def test_query_posts_to_cninfo_over_https():
    urls = []

    def post(url, **_kwargs):
        urls.append(url)
        return Response({"announcements": []})

    query_announcements(
        "test",
        "2026-07-01~2026-07-18",
        "szse",
        max_pages=1,
        request_post=post,
        sleep=lambda _seconds: None,
    )

    assert urls == ["https://www.cninfo.com.cn/new/hisAnnouncement/query"]


def test_query_fails_closed_when_provider_returns_non_data_payload():
    with pytest.raises(CNInfoQueryError, match="no announcements field"):
        query_announcements(
            "立案",
            "2026-01-01~2026-07-18",
            "sse",
            max_pages=2,
            request_post=lambda *_args, **_kwargs: Response({"error": "limited"}),
            sleep=lambda _seconds: None,
        )


def test_query_rejects_an_unknown_exchange_column():
    with pytest.raises(ValueError, match="unsupported"):
        query_announcements("test", "2026-07-18~2026-07-18", "all", max_pages=1)


def test_full_final_page_fails_closed_instead_of_returning_a_truncated_result():
    def full_page(*_args, **_kwargs):
        return Response({"announcements": [{"id": index} for index in range(30)]})

    with pytest.raises(CNInfoQueryError, match="pagination saturated"):
        query_announcements(
            "test",
            "2026-07-18~2026-07-18",
            "szse",
            max_pages=2,
            request_post=full_page,
            sleep=lambda _seconds: None,
        )
