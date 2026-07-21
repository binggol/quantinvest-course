"""Strict, retry-bounded CNInfo announcement pagination.

Legacy exporters used to skip a failed page and still publish a partial result.
This helper makes every requested exchange/page all-or-nothing so the outer
refresh runner can preserve the previous production snapshot on provider errors.
"""

from __future__ import annotations

import time
from typing import Callable

import requests


class CNInfoQueryError(RuntimeError):
    pass


def query_announcements(
    keyword: str,
    date_range: str,
    column: str,
    *,
    max_pages: int,
    page_size: int = 30,
    timeout: int = 25,
    retries: int = 3,
    pause: float = 0.5,
    request_post: Callable | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[dict]:
    if column not in {"szse", "sse"}:
        raise ValueError(f"unsupported CNInfo column: {column}")
    if max_pages < 1 or page_size < 1 or retries < 1:
        raise ValueError("max_pages, page_size and retries must be positive")

    post = request_post or requests.post
    url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    headers = {"User-Agent": "Mozilla/5.0"}
    output: list[dict] = []
    for page in range(1, max_pages + 1):
        announcements: list[dict] | None = None
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = post(
                    url,
                    headers=headers,
                    data={
                        "pageNum": page,
                        "pageSize": page_size,
                        "column": column,
                        "tabName": "fulltext",
                        "searchkey": keyword,
                        "seDate": date_range,
                        "isHLtitle": "false",
                    },
                    timeout=timeout,
                )
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or "announcements" not in payload:
                    raise ValueError("response has no announcements field")
                raw_value = payload.get("announcements")
                raw_rows = [] if raw_value is None else raw_value
                if not isinstance(raw_rows, list) or any(
                    not isinstance(row, dict) for row in raw_rows
                ):
                    raise ValueError("announcements must be a list of objects")
                announcements = raw_rows
                break
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    sleep(min(3.0 * attempt, 6.0))
        if announcements is None:
            raise CNInfoQueryError(
                f"CNInfo query failed after {retries} attempts: "
                f"keyword={keyword!r}, column={column}, page={page}: "
                f"{type(last_error).__name__}: {last_error}"
            ) from last_error
        output.extend(announcements)
        if not announcements or len(announcements) < page_size:
            break
        if page == max_pages:
            raise CNInfoQueryError(
                "CNInfo pagination saturated before exhaustion: "
                f"keyword={keyword!r}, column={column}, "
                f"page={page}, page_size={page_size}"
            )
        if pause:
            sleep(pause)
    return output
