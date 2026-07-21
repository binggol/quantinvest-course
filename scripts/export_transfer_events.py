from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
KEYWORDS = (
    "协议转让", "询价转让", "权益变动", "过户", "股份转让",
    "受让", "转让完成", "股份过户完成", "控制权变更",
)
DISCOVERY_KEYWORD_INDEXES = (0, 1, 4, 6, 7)
REQUEST_TIMEOUT = 8


def code_market(code: str) -> str:
    c = "".join(ch for ch in str(code) if ch.isdigit())[:6]
    if c.startswith(("6", "9")):
        return "sse"
    if c.startswith(("4", "8")):
        return "bj"
    return "szse"


def normalize_code(code: str) -> str:
    return "".join(ch for ch in str(code) if ch.isdigit())[:6]


def load_codes(data_dir: Path, explicit: list[str]) -> list[str]:
    codes = {normalize_code(x) for x in explicit if normalize_code(x)}
    if codes:
        return sorted(codes)
    for name in ("runup.json", "forecast_browse.json", "regime_advisor_pro.json"):
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
        "pageSize": 30,
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
    response = requests.post(CNINFO_QUERY_URL, data=params, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def cninfo_fulltext_query(searchkey: str, start: str, end: str, page: int = 1, column: str = "szse") -> dict:
    params = {
        "pageNum": page,
        "pageSize": 30,
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
    response = requests.post(CNINFO_QUERY_URL, data=params, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def title_text(raw: str) -> str:
    return re.sub(r"<[^>]+>", "", raw or "").strip()


def ann_codes(row: dict, fallback: str = "") -> list[str]:
    codes: set[str] = set()
    for key in ("secCode", "code", "symbol"):
        code = normalize_code(row.get(key) or "")
        if code:
            codes.add(code)
    raw_list = row.get("secCodeList")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, dict):
                code = normalize_code(item.get("secCode") or item.get("code") or item.get("symbol") or "")
            else:
                code = normalize_code(item)
            if code:
                codes.add(code)
    else:
        for code in re.findall(r"\b\d{6}\b", str(raw_list or "")):
            codes.add(code)
    fb = normalize_code(fallback)
    if fb:
        codes.add(fb)
    return sorted(codes)


def ann_date(row: dict) -> str:
    value = row.get("announcementTime")
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d")
    value = row.get("announcementDate") or row.get("date")
    text = str(value or "").strip()
    return text[:10].replace("/", "-")


def is_transfer_title(title: str) -> bool:
    if not any(k in title for k in KEYWORDS):
        return False
    noise = ("员工持股计划", "股票期权", "可转债", "质押", "解除质押")
    return not any(k in title for k in noise)


def collect_for_code(code: str, start: str, end: str, sleep_s: float, max_pages: int = 20) -> list[dict]:
    rows: list[dict] = []
    seen = set()

    def add_ann(ann: dict) -> None:
        codes = ann_codes(ann, fallback=code)
        title = title_text(ann.get("announcementTitle") or "")
        if codes and code not in codes:
            return
        if not is_transfer_title(title):
            return
        date = ann_date(ann)
        key = (code, date, title)
        if key in seen:
            return
        seen.add(key)
        rows.append({
            "code": code,
            "symbol": code,
            "market": code_market(code),
            "date": date,
            "ann_date": date,
            "title": title,
            "announcement_id": ann.get("announcementId"),
            "url": ("https://static.cninfo.com.cn/" + ann.get("adjunctUrl")) if ann.get("adjunctUrl") else "",
            "source": "cninfo",
        })

    def scan(data: dict) -> bool:
        anns = data.get("announcements") or []
        for ann in anns:
            add_ann(ann)
        return bool(anns)

    for page in range(1, max_pages + 1):
        data = cninfo_query(code, start, end, page=page)
        if not scan(data):
            break
        total = int(data.get("totalAnnouncement") or 0)
        if page * 30 >= total:
            break
        time.sleep(sleep_s)

    if rows:
        return rows

    # Fallback: cninfo's stock parameter sometimes needs an internal orgId.
    # Use broad full-text search and then filter back to the code.
    for keyword in (KEYWORDS[i] for i in DISCOVERY_KEYWORD_INDEXES):
        for page in range(1, min(max_pages, 10) + 1):
            data = cninfo_fulltext_query(f"{code} {keyword}", start, end, page=page)
            if not scan(data):
                break
            total = int(data.get("totalAnnouncement") or 0)
            if page * 30 >= total:
                break
            time.sleep(sleep_s)
    return rows


def discover_incremental_codes(start: str, end: str, sleep_s: float, max_pages: int = 5) -> tuple[list[str], list[dict]]:
    codes: set[str] = set()
    seed_rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    columns = ("szse", "sse")
    for keyword in KEYWORDS:
        for column in columns:
            for page in range(1, max(1, max_pages) + 1):
                data = cninfo_fulltext_query(keyword, start, end, page=page, column=column)
                anns = data.get("announcements") or []
                if not anns:
                    break
                for ann in anns:
                    title = title_text(ann.get("announcementTitle") or "")
                    if not is_transfer_title(title):
                        continue
                    date = ann_date(ann)
                    url = ("https://static.cninfo.com.cn/" + ann.get("adjunctUrl")) if ann.get("adjunctUrl") else ""
                    for code in ann_codes(ann):
                        if not code:
                            continue
                        codes.add(code)
                        key = (code, date, title)
                        if key in seen:
                            continue
                        seen.add(key)
                        seed_rows.append({
                            "code": code,
                            "symbol": code,
                            "market": code_market(code),
                            "date": date,
                            "ann_date": date,
                            "title": title,
                            "announcement_id": ann.get("announcementId"),
                            "url": url,
                            "source": "cninfo",
                        })
                total = int(data.get("totalAnnouncement") or 0)
                if page * 30 >= total:
                    break
                time.sleep(sleep_s)
            time.sleep(sleep_s)
    return sorted(codes), seed_rows


def merge_rows(old_rows: list[dict], new_rows: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str, str], dict] = {}
    for row in old_rows + new_rows:
        if not isinstance(row, dict):
            continue
        code = normalize_code(row.get("code") or row.get("symbol") or "")
        title = title_text(row.get("title") or row.get("announcementTitle") or "")
        date = str(row.get("date") or row.get("ann_date") or "")[:10]
        if not code or not title:
            continue
        row = dict(row)
        row["code"] = code
        row.setdefault("date", date)
        row.setdefault("ann_date", date)
        row["title"] = title
        merged[(code, date, title)] = row
    return sorted(merged.values(), key=lambda r: (r.get("code", ""), r.get("date", ""), r.get("title", "")))


def filter_rows_by_date(rows: list[dict], start: str, end: str) -> list[dict]:
    kept: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = str(row.get("date") or row.get("ann_date") or "")[:10]
        if start <= date <= end:
            kept.append(row)
    return kept


def latest_row_date(rows: list[dict]) -> str:
    dates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = str(row.get("date") or row.get("ann_date") or "")[:10]
        if re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            dates.append(date)
    return max(dates) if dates else ""


def write_payload(out_path: Path, start: str, end: str, codes: list[str],
                  items: list[dict], errors: list[dict], done: int, mode: str = "range") -> None:
    item_dates = [str(x.get("date") or x.get("ann_date") or "")[:10] for x in items if isinstance(x, dict)]
    item_dates = [x for x in item_dates if re.match(r"^\d{4}-\d{2}-\d{2}$", x)]
    item_codes = {
        normalize_code(x.get("code") or x.get("symbol"))
        for x in items
        if isinstance(x, dict) and normalize_code(x.get("code") or x.get("symbol"))
    }
    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "cninfo",
        "query": {
            "mode": mode,
            "start": start,
            "end": end,
            "data_start": min(item_dates) if item_dates else start,
            "data_end": max(item_dates) if item_dates else end,
            "keywords": list(KEYWORDS),
            "n_codes": len(item_codes),
            "batch_n_codes": len(codes),
            "done_codes": done,
        },
        "items": items,
        "errors": errors[:50],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export agreement transfer/equity-change announcements from cninfo.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--codes", nargs="*", default=[])
    parser.add_argument("--days", type=int, default=0, help="Lookback days. 0 means use --start-date.")
    parser.add_argument("--start-date", default="2020-07-03", help="History start date when --days is 0.")
    parser.add_argument("--incremental", action="store_true", default=True, help="Only fetch new announcements and keep existing history.")
    parser.add_argument("--full-refresh", action="store_true", help="Rebuild the selected date range instead of incremental append.")
    parser.add_argument("--incremental-overlap-days", type=int, default=2, help="Overlap days before latest saved announcement.")
    parser.add_argument("--incremental-discovery-pages", type=int, default=1, help="Max full-text discovery pages per keyword in incremental mode.")
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--offset", type=int, default=0, help="Skip the first N loaded codes.")
    parser.add_argument("--limit", type=int, default=0, help="Only process the first N loaded codes.")
    parser.add_argument("--flush-every", type=int, default=10, help="Write cninfo_transfer.json every N codes.")
    parser.add_argument("--max-pages", type=int, default=20, help="Max cninfo pages per code.")
    args = parser.parse_args()
    if args.full_refresh:
        args.incremental = False

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    explicit_codes = bool(args.codes)
    codes = load_codes(data_dir, args.codes)
    if not args.incremental or explicit_codes:
        if args.offset and args.offset > 0:
            codes = codes[args.offset:]
        if args.limit and args.limit > 0:
            codes = codes[:args.limit]
    end_dt = datetime.now()
    if args.days and args.days > 0:
        start = (end_dt - timedelta(days=args.days)).strftime("%Y-%m-%d")
    else:
        start = args.start_date
    end = end_dt.strftime("%Y-%m-%d")

    out_path = data_dir / "cninfo_transfer.json"
    try:
        old_payload = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    except Exception:
        old_payload = {}
    old_rows = old_payload.get("items") if isinstance(old_payload, dict) else []
    old_rows = old_rows if isinstance(old_rows, list) else []
    if args.incremental:
        latest = latest_row_date(old_rows)
        if latest:
            start_dt = datetime.strptime(latest, "%Y-%m-%d") - timedelta(days=max(0, args.incremental_overlap_days))
            start = max(start_dt.strftime("%Y-%m-%d"), args.start_date)
        seed_rows: list[dict] = []
        if not explicit_codes:
            discovered_codes, seed_rows = discover_incremental_codes(
                start, end, args.sleep, max_pages=max(1, args.incremental_discovery_pages)
            )
            codes = discovered_codes
            if args.offset and args.offset > 0:
                codes = codes[args.offset:]
            if args.limit and args.limit > 0:
                codes = codes[:args.limit]
            if seed_rows:
                allowed = set(codes)
                seed_rows = [row for row in seed_rows if normalize_code(row.get("code") or "") in allowed]
            if not codes:
                items = merge_rows(old_rows, [])
                write_payload(out_path, start, end, [], items, [], 0, "incremental")
                print(f"incremental no new transfer announcements {start}~{end}; kept items={len(items)}")
                return
            items = merge_rows(old_rows, seed_rows)
            write_payload(out_path, start, end, codes, items, [], len(codes), "incremental")
            print(f"incremental discovered codes={len(codes)} rows={len(seed_rows)} items={len(items)}")
            return
    else:
        seed_rows = []
        old_rows = filter_rows_by_date(old_rows, start, end)

    new_rows: list[dict] = list(seed_rows)
    errors: list[dict] = []
    for i, code in enumerate(codes, 1):
        try:
            rows = collect_for_code(code, start, end, args.sleep, max_pages=max(1, args.max_pages))
            new_rows.extend(rows)
            print(f"{i}/{len(codes)} {code} transfer announcements {len(rows)}")
        except Exception as exc:
            errors.append({"code": code, "error": str(exc)})
            print(f"{i}/{len(codes)} {code} ERROR {exc}")
        if args.flush_every > 0 and (i % args.flush_every == 0 or i == len(codes)):
            items = merge_rows(old_rows, new_rows)
            write_payload(out_path, start, end, codes, items, errors, i, "incremental" if args.incremental else "range")
            print(f"checkpoint {i}/{len(codes)} wrote {out_path} items={len(items)}")
        time.sleep(args.sleep)

    items = merge_rows(old_rows, new_rows)
    write_payload(out_path, start, end, codes, items, errors, len(codes), "incremental" if args.incremental else "range")
    print(f"wrote {out_path} items={len(items)} new={len(new_rows)} errors={len(errors)}")


if __name__ == "__main__":
    main()
