from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests
from PyPDF2 import PdfReader


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data" / "cninfo_transfer.json"
DEFAULT_OUTPUT = ROOT / "data" / "transfer_terms_overlay.json"
PARSER_VERSION = 1
MAX_PDF_BYTES = 25 * 1024 * 1024
CNINFO_HOST = "static.cninfo.com.cn"

INQUIRY = "\u8be2\u4ef7\u8f6c\u8ba9"
AGREEMENT = "\u534f\u8bae\u8f6c\u8ba9"
SHARE_TRANSFER_AGREEMENT = "\u80a1\u4efd\u8f6c\u8ba9\u534f\u8bae"

_PDF_PATH_RE = re.compile(r"^/finalpage/\d{4}-\d{2}-\d{2}/\d+\.pdf$", re.I)
_PRICE_PATTERNS = (
    re.compile(
        r"(?:\u672c\u6b21)?(?:\u8be2\u4ef7|\u534f\u8bae)?\u8f6c\u8ba9(?:\u80a1\u4efd)?(?:\u7684)?"
        r"\u4ef7\u683c(?:\u786e\u5b9a)?(?:\u4e3a|:)(?:\u4eba\u6c11\u5e01)?"
        r"(?P<value>\d{1,6}(?:\.\d{1,6})?)\u5143(?:/|\u6bcf)?\u80a1"
    ),
    re.compile(
        r"(?:\u6bcf\u80a1)?(?:\u80a1\u4efd)?\u8f6c\u8ba9(?:\u5355\u4ef7|\u4ef7\u683c)"
        r"(?:\u786e\u5b9a)?(?:\u4e3a|:)(?:\u4eba\u6c11\u5e01)?"
        r"(?P<value>\d{1,6}(?:\.\d{1,6})?)\u5143(?:/|\u6bcf)?\u80a1"
    ),
)
_RATIO_RE = re.compile(
    r"\u5360(?:\u516c\u53f8)?(?:\u5f53\u524d|\u73b0\u6709|\u76ee\u524d)?\u603b\u80a1\u672c"
    r"(?:\d[\d,]*\u80a1)?(?:\u6bd4\u4f8b)?(?:\u7684|\u4e3a|:)?"
    r"(?P<value>\d{1,3}(?:\.\d{1,8})?)%"
)
_TRANSFER_SHARES_PATTERNS = (
    re.compile(
        r"(?:\u672c\u6b21)?(?:\u8be2\u4ef7|\u534f\u8bae)?\u8f6c\u8ba9(?:\u7684)?(?:\u80a1\u4efd)?"
        r"(?:\u6570\u91cf)?(?:\u4e3a|:)(?P<value>\d[\d,]*)\u80a1"
    ),
    re.compile(
        r"(?:\u62df|\u7ea6\u5b9a)?\u5c06\u5176\u6301\u6709\u7684(?:\u516c\u53f8)?"
        r"(?P<value>\d[\d,]*)\u80a1.{0,80}?(?:\u534f\u8bae\u8f6c\u8ba9|\u8f6c\u8ba9\u7ed9)"
    ),
)
_TOTAL_SHARES_RE = re.compile(
    r"(?:\u516c\u53f8)?\u603b\u80a1\u672c(?:\u4e3a|:)?(?P<value>\d[\d,]*)\u80a1"
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _rows(payload: dict) -> list[dict]:
    items = payload.get("items") if isinstance(payload, dict) else []
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def normalize_code(value: object) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:6]


def overlay_key(row: dict) -> str:
    announcement_id = str(row.get("announcement_id") or row.get("announcementId") or "").strip()
    if announcement_id:
        return f"announcement:{announcement_id}"
    url = normalize_url(row.get("url") or row.get("announcement_url") or "")
    if url:
        return f"url:{url}"
    code = normalize_code(row.get("code") or row.get("symbol") or row.get("secCode"))
    date = str(row.get("ann_date") or row.get("date") or "")[:10]
    title = str(row.get("title") or row.get("announcementTitle") or "").strip()
    return f"row:{code}|{date}|{title}"


def normalize_url(value: object) -> str:
    return str(value or "").strip()


def validate_cninfo_pdf_url(url: str) -> str:
    value = normalize_url(url)
    parsed = urlparse(value)
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != CNINFO_HOST
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not _PDF_PATH_RE.fullmatch(parsed.path)
    ):
        raise ValueError("only official cninfo finalpage PDF URLs are allowed")
    return value


def document_kind(title: str) -> str:
    value = str(title or "")
    if INQUIRY in value and "\u7ed3\u679c\u62a5\u544a\u4e66" in value:
        return "inquiry_result"
    if INQUIRY in value and "\u5b9a\u4ef7" in value:
        return "inquiry_pricing"
    if AGREEMENT in value or SHARE_TRANSFER_AGREEMENT in value:
        return "agreement_transfer"
    return ""


def is_candidate(row: dict) -> bool:
    title = str(row.get("title") or row.get("announcementTitle") or "")
    kind = document_kind(title)
    if not kind:
        return False
    if any(word in title for word in (
        "\u7ec8\u6b62", "\u53d6\u6d88", "\u571f\u5730", "\u53d8\u7535\u7ad9",
        "\u57fa\u91d1\u4efd\u989d", "\u5408\u4f19\u4f01\u4e1a\u4efd\u989d", "\u53c2\u80a1\u516c\u53f8",
    )):
        return False
    if kind == "agreement_transfer" and not any(word in title for word in (
        "\u80a1\u4e1c", "\u516c\u53f8\u80a1\u4efd", "\u90e8\u5206\u80a1\u4efd",
        "\u6743\u76ca\u53d8\u52a8", "\u63a7\u5236\u6743", SHARE_TRANSFER_AGREEMENT,
    )):
        return False
    try:
        validate_cninfo_pdf_url(row.get("url") or row.get("announcement_url") or "")
    except ValueError:
        return False
    return True


def _compact(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    return re.sub(r"\s+", "", value).replace("\uff05", "%").replace("\uff0f", "/")


def _evidence(text: str, start: int, end: int, radius: int = 90) -> str:
    return text[max(0, start - radius):min(len(text), end + radius)]


def _price_score(text: str, start: int, end: int, pattern_index: int) -> int:
    context = text[max(0, start - 90):min(len(text), end + 60)]
    local_context = text[max(0, start - 20):min(len(text), end + 12)]
    score = 100 - pattern_index
    if any(word in context for word in (
        "\u672c\u6b21", "\u786e\u5b9a", "\u8be2\u4ef7\u7ed3\u679c", "\u80a1\u4efd\u8f6c\u8ba9\u534f\u8bae",
    )):
        score += 8
    if any(word in local_context for word in (
        "\u4ef7\u683c\u4e0b\u9650", "\u6700\u4f4e\u8f6c\u8ba9\u4ef7\u683c", "\u4e0d\u4f4e\u4e8e",
        "\u6536\u76d8\u4ef7", "\u4ea4\u6613\u5747\u4ef7", "\u53c2\u8003\u4ef7",
    )):
        score -= 120
    return score


def _find_price(pages: list[str]) -> dict:
    best: tuple[int, dict] | None = None
    for page_number, text in enumerate(pages, 1):
        for pattern_index, pattern in enumerate(_PRICE_PATTERNS):
            for match in pattern.finditer(text):
                value = float(match.group("value"))
                if value <= 0 or value > 100000:
                    continue
                score = _price_score(text, match.start(), match.end(), pattern_index)
                candidate = {
                    "transfer_price": value,
                    "price_page": page_number,
                    "price_evidence": _evidence(text, match.start(), match.end()),
                    "price_method": "direct",
                }
                if best is None or score > best[0]:
                    best = (score, candidate)
    return best[1] if best is not None and best[0] > 0 else {}


def _ratio_score(text: str, start: int, end: int) -> int:
    before = text[max(0, start - 180):start]
    context = before + text[start:min(len(text), end + 50)]
    if not any(word in context for word in ("\u8f6c\u8ba9", "\u53d7\u8ba9")):
        return -100
    score = 50
    if any(word in context for word in (
        "\u672c\u6b21\u8be2\u4ef7\u8f6c\u8ba9\u80a1\u4efd\u6570\u91cf",
        "\u672c\u6b21\u534f\u8bae\u8f6c\u8ba9", "\u7ea6\u5b9a\u5c06\u5176\u6301\u6709",
    )):
        score += 25
    elif "\u672c\u6b21" in context:
        score += 8
    if any(word in before[-70:] for word in (
        "\u8f6c\u8ba9\u524d", "\u8f6c\u8ba9\u540e", "\u6743\u76ca\u53d8\u52a8\u524d",
        "\u6743\u76ca\u53d8\u52a8\u540e", "\u5254\u9664", "\u56de\u8d2d\u4e13\u7528",
    )):
        score -= 45
    return score


def _find_ratio(pages: list[str]) -> dict:
    best: tuple[int, dict] | None = None
    for page_number, text in enumerate(pages, 1):
        for match in _RATIO_RE.finditer(text):
            value = float(match.group("value"))
            if value <= 0 or value > 100:
                continue
            score = _ratio_score(text, match.start(), match.end())
            candidate = {
                "transfer_ratio": value,
                "ratio_page": page_number,
                "ratio_evidence": _evidence(text, match.start(), match.end()),
                "ratio_method": "direct",
            }
            if best is None or score > best[0]:
                best = (score, candidate)
    if best is not None and best[0] > 0:
        return best[1]
    return _derive_ratio(pages)


def _derive_ratio(pages: list[str]) -> dict:
    for page_number, text in enumerate(pages, 1):
        transfer_match = None
        for pattern in _TRANSFER_SHARES_PATTERNS:
            transfer_match = pattern.search(text)
            if transfer_match:
                break
        if not transfer_match:
            continue
        transfer_shares = int(transfer_match.group("value").replace(",", ""))
        totals = []
        for match in _TOTAL_SHARES_RE.finditer(text):
            total = int(match.group("value").replace(",", ""))
            if total >= transfer_shares > 0:
                totals.append((abs(match.start() - transfer_match.start()), total, match))
        if not totals:
            continue
        _distance, total_shares, total_match = min(totals, key=lambda item: item[0])
        ratio = round(transfer_shares / total_shares * 100, 6)
        if 0 < ratio <= 100:
            start = min(transfer_match.start(), total_match.start())
            end = max(transfer_match.end(), total_match.end())
            return {
                "transfer_ratio": ratio,
                "ratio_page": page_number,
                "ratio_evidence": _evidence(text, start, end),
                "ratio_method": "derived_shares_over_total",
                "transfer_shares": transfer_shares,
                "total_shares": total_shares,
            }
    return {}


def parse_transfer_terms(page_texts: list[str]) -> dict:
    pages = [_compact(text) for text in page_texts if str(text or "").strip()]
    if not pages:
        return {}
    result = {}
    result.update(_find_price(pages))
    result.update(_find_ratio(pages))
    if result:
        direct = result.get("price_method") == "direct" and result.get("ratio_method") == "direct"
        result["confidence"] = "high" if direct else "medium"
    return result


def download_pdf(url: str, timeout: float, session=requests) -> bytes:
    safe_url = validate_cninfo_pdf_url(url)
    response = session.get(
        safe_url,
        timeout=timeout,
        allow_redirects=False,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; quantinvest-transfer-terms/1.0)",
            "Referer": "https://www.cninfo.com.cn/",
            "Accept": "application/pdf",
        },
    )
    response.raise_for_status()
    if 300 <= response.status_code < 400:
        raise ValueError("redirects are not accepted for cninfo PDF downloads")
    content = response.content
    if len(content) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds size limit")
    if not content.startswith(b"%PDF"):
        raise ValueError("cninfo response is not a PDF")
    return content


def extract_pdf_pages(pdf_bytes: bytes) -> list[str]:
    reader = PdfReader(BytesIO(pdf_bytes))
    return [(page.extract_text() or "") for page in reader.pages]


def enrich_row(row: dict, timeout: float, session=requests) -> dict:
    url = validate_cninfo_pdf_url(row.get("url") or row.get("announcement_url") or "")
    terms = parse_transfer_terms(extract_pdf_pages(download_pdf(url, timeout, session=session)))
    record = {
        "key": overlay_key(row),
        "announcement_id": str(row.get("announcement_id") or row.get("announcementId") or ""),
        "code": normalize_code(row.get("code") or row.get("symbol") or row.get("secCode")),
        "ann_date": str(row.get("ann_date") or row.get("date") or "")[:10],
        "title": str(row.get("title") or row.get("announcementTitle") or ""),
        "url": url,
        "document_kind": document_kind(str(row.get("title") or row.get("announcementTitle") or "")),
        "parser_version": PARSER_VERSION,
        "parsed_at": _now(),
        "status": "parsed" if terms else "no_terms",
    }
    record.update(terms)
    return record


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def _write_overlay(path: Path, records: dict[str, dict], stats: dict) -> None:
    items = sorted(
        records.values(),
        key=lambda row: (str(row.get("ann_date") or ""), str(row.get("key") or "")),
        reverse=True,
    )
    payload = {
        "updated": _now(),
        "source": "cninfo_official_pdf",
        "parser_version": PARSER_VERSION,
        "items": items,
        "stats": stats,
    }
    _atomic_write(path, payload)


def run(
    source: Path,
    output: Path,
    limit: int = 30,
    timeout: float = 20.0,
    sleep_seconds: float = 0.4,
    flush_every: int = 5,
    codes: set[str] | None = None,
    force: bool = False,
    retry_errors: bool = False,
    session=requests,
) -> dict:
    source_rows = _rows(_read_json(source))
    existing_rows = _rows(_read_json(output))
    records = {overlay_key(row): dict(row) for row in existing_rows}
    selected = []
    queued_keys = set()
    for row in source_rows:
        if not is_candidate(row):
            continue
        if codes and normalize_code(row.get("code") or row.get("symbol")) not in codes:
            continue
        key = overlay_key(row)
        if key in queued_keys:
            continue
        old = records.get(key) or {}
        current_parser = int(old.get("parser_version") or 0) == PARSER_VERSION
        status = str(old.get("status") or "")
        attempts = int(old.get("attempt_count") or 0)
        if not force and current_parser and status in {"parsed", "no_terms"}:
            continue
        if not force and not retry_errors and current_parser and status == "error" and attempts >= 3:
            continue
        selected.append(row)
        queued_keys.add(key)
    selected.sort(
        key=lambda row: (
            str(row.get("ann_date") or row.get("date") or "")[:10],
            {"inquiry_result": 3, "inquiry_pricing": 2, "agreement_transfer": 1}.get(
                document_kind(str(row.get("title") or "")), 0
            ),
            overlay_key(row),
        ),
        reverse=True,
    )
    if limit > 0:
        selected = selected[:limit]

    stats = {
        "source_rows": len(source_rows),
        "candidate_rows": sum(1 for row in source_rows if is_candidate(row)),
        "selected_rows": len(selected),
        "processed": 0,
        "parsed": 0,
        "no_terms": 0,
        "errors": 0,
    }
    for position, row in enumerate(selected, 1):
        key = overlay_key(row)
        old = records.get(key) or {}
        try:
            record = enrich_row(row, timeout=timeout, session=session)
            record["attempt_count"] = int(old.get("attempt_count") or 0) + 1
            stats[record["status"]] += 1
        except Exception as exc:
            record = {
                "key": key,
                "announcement_id": str(row.get("announcement_id") or row.get("announcementId") or ""),
                "code": normalize_code(row.get("code") or row.get("symbol") or row.get("secCode")),
                "ann_date": str(row.get("ann_date") or row.get("date") or "")[:10],
                "title": str(row.get("title") or row.get("announcementTitle") or ""),
                "url": normalize_url(row.get("url") or row.get("announcement_url") or ""),
                "document_kind": document_kind(str(row.get("title") or row.get("announcementTitle") or "")),
                "parser_version": PARSER_VERSION,
                "parsed_at": _now(),
                "status": "error",
                "attempt_count": int(old.get("attempt_count") or 0) + 1,
                "error": str(exc)[:300],
            }
            stats["errors"] += 1
        records[key] = record
        stats["processed"] = position
        if flush_every > 0 and position % flush_every == 0:
            _write_overlay(output, records, stats)
        if sleep_seconds > 0 and position < len(selected):
            time.sleep(sleep_seconds)
    _write_overlay(output, records, stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Incrementally extract transfer price and ratio from official cninfo PDFs."
    )
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--sleep", type=float, default=0.4)
    parser.add_argument("--flush-every", type=int, default=5)
    parser.add_argument("--codes", nargs="*", default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retry-errors", action="store_true")
    args = parser.parse_args()
    codes = {normalize_code(code) for code in args.codes if normalize_code(code)}
    stats = run(
        source=Path(args.source),
        output=Path(args.output),
        limit=max(0, args.limit),
        timeout=max(1.0, args.timeout),
        sleep_seconds=max(0.0, args.sleep),
        flush_every=max(0, args.flush_every),
        codes=codes or None,
        force=args.force,
        retry_errors=args.retry_errors,
    )
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
