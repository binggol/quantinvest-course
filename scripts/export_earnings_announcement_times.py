from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
REQUEST_TIMEOUT = 10
PAGE_SIZE = 50
REQUEST_RETRIES = 3
KEYWORDS = ("业绩预告", "业绩快报", "季度报告", "半年度报告", "年度报告", "一季报", "三季报", "半年报", "中期报告", "年报")
REPORT_KEYWORDS = ("季度报告", "半年度报告", "年度报告", "一季报", "三季报", "半年报", "中期报告", "年报")
NOISE = ("摘要", "取消", "英文", "审计报告", "社会责任", "独立董事",
         "问询函", "监管工作函", "回复", "专项说明")


def normalize_code(code: str) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())[:6]


def code_market(code: str) -> str:
    c = normalize_code(code)
    if c.startswith(("920", "8", "4")):
        return "bj"
    if c.startswith(("6", "9")):
        return "sse"
    return "szse"


def title_text(raw: str) -> str:
    return re.sub(r"<[^>]+>", "", str(raw or "")).strip()


def announcement_datetime(row: dict) -> tuple[str, str]:
    value = row.get("announcementTime")
    if isinstance(value, (int, float)) and value > 0:
        dt = datetime.fromtimestamp(value / 1000)
        return dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m-%d %H:%M:%S")
    text = str(row.get("announcementDate") or row.get("date") or "").strip()
    date = text[:10].replace("/", "-")
    return date, ""


def announcement_type(title: str) -> str:
    for key in KEYWORDS:
        if key in title:
            return key
    return ""


def is_earnings_title(title: str) -> bool:
    typ = announcement_type(title)
    if not typ:
        return False
    if typ in REPORT_KEYWORDS:
        suffix = title.split(typ, 1)[1]
        if "公告" in suffix:
            return False
    return not any(x in title for x in NOISE)


def load_codes(data_dir: Path, explicit: list[str]) -> list[str]:
    codes = {normalize_code(x) for x in explicit if normalize_code(x)}
    if codes:
        return sorted(codes)
    for name in ("rolling_earnings.json", "runup.json", "forecast_browse.json", "regime_advisor_pro.json"):
        path = data_dir / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        stack = [payload]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                code = normalize_code(obj.get("code") or obj.get("ts_code") or obj.get("symbol") or "")
                if code:
                    codes.add(code)
                stack.extend(obj.values())
            elif isinstance(obj, list):
                stack.extend(obj)
    return sorted(codes)


def cninfo_query(code: str, start: str, end: str, page: int = 1) -> dict:
    market = code_market(code)
    params = {
        "pageNum": page,
        "pageSize": PAGE_SIZE,
        "column": market,
        "tabName": "fulltext",
        "plate": "",
        "stock": f"{code},{market}",
        "searchkey": " ".join(KEYWORDS),
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": f"{start}~{end}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.cninfo.com.cn/new/index",
        "Accept": "application/json,text/plain,*/*",
    }
    return cninfo_post(params, headers)


def cninfo_fulltext_query(searchkey: str, start: str, end: str, page: int = 1, column: str = "szse") -> dict:
    params = {
        "pageNum": page,
        "pageSize": PAGE_SIZE,
        "column": column,
        "tabName": "fulltext",
        "plate": "",
        "stock": "",
        "searchkey": searchkey,
        "secid": "",
        "category": "",
        "trade": "",
        "seDate": f"{start}~{end}",
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.cninfo.com.cn/new/index",
        "Accept": "application/json,text/plain,*/*",
    }
    return cninfo_post(params, headers)


def cninfo_post(params: dict, headers: dict) -> dict:
    last_exc: Exception | None = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = requests.post(CNINFO_QUERY_URL, data=params, headers=headers, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_exc = exc
            if attempt >= REQUEST_RETRIES:
                break
            time.sleep(0.4 * attempt)
    raise last_exc


def validated_announcement_page(
    data: dict,
    *,
    page: int,
    max_pages: int,
    context: str,
) -> tuple[list[dict], bool]:
    """Validate one page and report whether the provider result is exhausted."""
    if not isinstance(data, dict) or "announcements" not in data:
        raise RuntimeError(f"{context}: response has no announcements field")
    raw_value = data.get("announcements")
    raw = [] if raw_value is None else raw_value
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise RuntimeError(f"{context}: announcements must be a list of objects")

    total: int | None = None
    raw_total = data.get("totalAnnouncement")
    if raw_total not in (None, ""):
        try:
            total = int(raw_total)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{context}: invalid totalAnnouncement={raw_total!r}") from exc
        if total < 0:
            raise RuntimeError(f"{context}: negative totalAnnouncement={total}")

    consumed_before = (page - 1) * PAGE_SIZE
    if not raw:
        if total is not None and consumed_before < total:
            raise RuntimeError(
                f"{context}: empty page {page} before totalAnnouncement={total}"
            )
        return raw, True

    if total is not None:
        if total < consumed_before + len(raw):
            raise RuntimeError(
                f"{context}: totalAnnouncement={total} is smaller than returned rows"
            )
        if page * PAGE_SIZE >= total:
            return raw, True
        if len(raw) < PAGE_SIZE:
            raise RuntimeError(
                f"{context}: short page {page} before totalAnnouncement={total}"
            )
    elif len(raw) < PAGE_SIZE:
        return raw, True

    if page >= max_pages:
        raise RuntimeError(
            f"{context}: pagination saturated at max_pages={max_pages}, "
            f"page_size={PAGE_SIZE}, totalAnnouncement={total}"
        )
    return raw, False


def row_from_announcement(ann: dict, fallback_code: str = "", output_code: str = "") -> dict | None:
    code = normalize_code(output_code or ann.get("secCode") or fallback_code)
    if not code:
        return None
    title = title_text(ann.get("announcementTitle") or "")
    if not is_earnings_title(title):
        return None
    date, dt = announcement_datetime(ann)
    if not date:
        return None
    return {
        "code": code,
        "symbol": code,
        "name": title_text(ann.get("secName") or ""),
        "market": code_market(code),
        "type": announcement_type(title),
        "ann_date": date,
        "ann_datetime": dt,
        "announcement_time_ms": ann.get("announcementTime"),
        "title": title,
        "announcement_id": ann.get("announcementId"),
        "url": ("https://static.cninfo.com.cn/" + ann.get("adjunctUrl")) if ann.get("adjunctUrl") else "",
        "source": "cninfo",
    }


def collect_for_code(code: str, start: str, end: str, sleep_s: float, max_pages: int, name: str = "") -> list[dict]:
    rows: list[dict] = []
    seen = set()

    def add_ann(ann: dict, output_code: str = "") -> None:
        row = row_from_announcement(ann, fallback_code=code, output_code=output_code)
        if not row or row["code"] != code:
            return
        key = (row["code"], row["ann_date"], row["title"])
        if key in seen:
            return
        seen.add(key)
        rows.append(row)

    def scan_query(query_fn) -> None:
        for page in range(1, max_pages + 1):
            data = query_fn(page)
            anns, exhausted = validated_announcement_page(
                data,
                page=page,
                max_pages=max_pages,
                context=f"code={code}",
            )
            for ann in anns:
                add_ann(ann)
            if exhausted:
                break
            time.sleep(sleep_s)

    scan_query(lambda page: cninfo_query(code, start, end, page=page))
    if rows:
        return rows
    name = title_text(name)
    if code.startswith("920") and name:
        for keyword in KEYWORDS:
            for page in range(1, max_pages + 1):
                data = cninfo_fulltext_query(f"{name} {keyword}", start, end, page=page, column="bj")
                anns, exhausted = validated_announcement_page(
                    data,
                    page=page,
                    max_pages=max_pages,
                    context=f"bse-name={name!r} keyword={keyword!r}",
                )
                for ann in anns:
                    add_ann(ann, output_code=code)
                if exhausted:
                    break
                time.sleep(sleep_s)
            time.sleep(sleep_s)
        if rows:
            return rows
    for keyword in KEYWORDS:
        scan_query(lambda page, keyword=keyword: cninfo_fulltext_query(f"{code} {keyword}", start, end, page=page, column=code_market(code)))
        time.sleep(sleep_s)
    return rows


def collect_global_one(column: str, keyword: str, start: str, end: str, sleep_s: float, max_pages: int) -> list[dict]:
    rows: list[dict] = []
    seen = set()
    for page in range(1, max_pages + 1):
        data = cninfo_fulltext_query(keyword, start, end, page=page, column=column)
        anns, exhausted = validated_announcement_page(
            data,
            page=page,
            max_pages=max_pages,
            context=f"global column={column} keyword={keyword!r}",
        )
        for ann in anns:
            row = row_from_announcement(ann)
            if not row:
                continue
            key = (row["code"], row["ann_date"], row["title"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        if exhausted:
            break
        time.sleep(sleep_s)
    return rows


def collect_global(start: str, end: str, sleep_s: float, max_pages: int, workers: int) -> list[dict]:
    tasks = [(column, keyword) for column in ("szse", "sse", "bj") for keyword in KEYWORDS]
    rows: list[dict] = []
    seen = set()
    failures: list[tuple[str, str, str]] = []
    if workers <= 1:
        results = [collect_global_one(c, k, start, end, sleep_s, max_pages) for c, k in tasks]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(collect_global_one, c, k, start, end, sleep_s, max_pages): (c, k) for c, k in tasks}
            for fut in as_completed(futures):
                c, k = futures[fut]
                try:
                    part = fut.result()
                    print(f"global {c} {k} {len(part)}", flush=True)
                    results.append(part)
                except Exception as exc:
                    failures.append((c, k, str(exc)))
                    print(f"global {c} {k} failed: {exc}", flush=True)
        if failures:
            sample = "; ".join(
                f"{column}/{keyword}: {message}"
                for column, keyword, message in failures[:3]
            )
            raise RuntimeError(
                f"cninfo global scan incomplete ({len(failures)}/{len(tasks)} failed): {sample}"
            )
    for part in results:
        for row in part:
            key = (row["code"], row["ann_date"], row["title"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def merge_items(old_items: list[dict], new_items: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str, str], dict] = {}
    for row in old_items + new_items:
        if not isinstance(row, dict):
            continue
        code = normalize_code(row.get("code") or row.get("symbol") or "")
        date = str(row.get("ann_date") or row.get("date") or "")[:10]
        title = title_text(row.get("title") or "")
        if not code or not date or not title:
            continue
        row = dict(row)
        row["code"] = code
        row["ann_date"] = date
        merged[(code, date, title)] = row
    return sorted(merged.values(), key=lambda x: (str(x.get("ann_date") or ""), str(x.get("code") or ""), str(x.get("title") or "")))


def build_payload(items: list[dict], errors: list[dict], start: str, end: str,
                  codes: list[str], incremental: bool, overlap_days: int,
                  processed_codes: set[str] | None = None) -> dict:
    return {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
        "errors": errors,
        "query": {
            "source": "cninfo",
            "start": start,
            "end": end,
            "n_codes": len(codes),
            "processed_codes": sorted(processed_codes or []),
            "incremental": incremental,
            "overlap_days": overlap_days,
            "keywords": list(KEYWORDS),
        },
    }


def write_payload(out_path: Path, items: list[dict], errors: list[dict], start: str, end: str,
                  codes: list[str], incremental: bool, overlap_days: int,
                  processed_codes: set[str] | None = None) -> dict:
    payload = build_payload(
        items, errors, start, end, codes, incremental, overlap_days, processed_codes
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = out_path.with_name(
        f".{out_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, out_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return payload


def export(data_dir: Path, start: str, end: str, codes: list[str], incremental: bool,
           overlap_days: int, sleep_s: float, max_pages: int, flush_every: int, workers: int) -> dict:
    out_path = data_dir / "cninfo_earnings_announcements.json"
    old = {}
    old_items: list[dict] = []
    if out_path.exists():
        try:
            old = json.loads(out_path.read_text(encoding="utf-8"))
            old_items = old.get("items") or []
        except Exception:
            old = {}
    if incremental and old_items:
        dates = [str(x.get("ann_date") or "")[:10] for x in old_items if x.get("ann_date")]
        if dates:
            latest = max(dates)
            start = (datetime.strptime(latest, "%Y-%m-%d") - timedelta(days=overlap_days)).strftime("%Y-%m-%d")

    data_dir.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict] = []
    errors: list[dict] = []
    old_query = old.get("query") if isinstance(old, dict) else {}
    if not isinstance(old_query, dict):
        old_query = {}
    processed_codes = set()
    if codes:
        if incremental and old_query.get("start") == start and old_query.get("end") == end:
            processed_codes = {normalize_code(x) for x in old_query.get("processed_codes") or [] if normalize_code(x)}
            failed_codes = {
                normalize_code(row.get("code") or "")
                for row in (old.get("errors") or [])
                if isinstance(row, dict) and normalize_code(row.get("code") or "")
            }
            processed_codes.difference_update(failed_codes)
        scan_codes = [code for code in codes if code not in processed_codes]
        for i, code in enumerate(scan_codes, 1):
            try:
                rows = collect_for_code(code, start, end, sleep_s=sleep_s, max_pages=max_pages)
                all_rows.extend(rows)
                print(f"{i}/{len(scan_codes)} {code} earnings announcements {len(rows)}", flush=True)
            except Exception as exc:
                errors.append({"code": code, "error": str(exc)})
                print(f"{i}/{len(scan_codes)} {code} failed: {exc}", flush=True)
            else:
                processed_codes.add(code)
            if incremental and flush_every > 0 and i % flush_every == 0:
                items = merge_items(old_items if incremental else [], all_rows)
                write_payload(out_path, items, errors, start, end, codes, incremental, overlap_days, processed_codes)
            time.sleep(sleep_s)
    else:
        try:
            all_rows = collect_global(start, end, sleep_s=sleep_s, max_pages=max_pages, workers=workers)
            print(f"global earnings announcements {len(all_rows)}", flush=True)
        except Exception as exc:
            errors.append({"code": "GLOBAL", "error": str(exc)})
            print(f"global earnings announcements failed: {exc}", flush=True)

    items = merge_items(old_items if incremental else [], all_rows)
    if errors and not incremental:
        return build_payload(
            items, errors, start, end, codes, incremental, overlap_days, processed_codes
        )
    return write_payload(out_path, items, errors, start, end, codes, incremental, overlap_days, processed_codes)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export cninfo earnings announcement timestamps.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--code", action="append", default=[])
    parser.add_argument("--full-refresh", action="store_true")
    parser.add_argument("--incremental-overlap-days", type=int, default=2)
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--flush-every", type=int, default=10)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    codes = load_codes(data_dir, args.code) if args.code else []
    payload = export(
        data_dir=data_dir,
        start=args.start,
        end=args.end,
        codes=codes,
        incremental=not args.full_refresh,
        overlap_days=args.incremental_overlap_days,
        sleep_s=args.sleep,
        max_pages=args.max_pages,
        flush_every=args.flush_every,
        workers=args.workers,
    )
    print(json.dumps({
        "items": len(payload.get("items") or []),
        "errors": len(payload.get("errors") or []),
        "updated": payload.get("updated"),
    }, ensure_ascii=False))
    return 1 if payload.get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
