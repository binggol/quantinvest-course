from __future__ import annotations

import argparse
import json
import re
import time
import unicodedata
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests
from PyPDF2 import PdfReader


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
CNINFO_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
EASTMONEY_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
CNINFO_PDF_HOST = "static.cninfo.com.cn"
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_BATCH_PDFS = 6
LOCK_PARSER_VERSION = 2
PLACEMENT_TERMS = (
    "向特定对象发行",
    "非公开发行",
    "发行股份购买资产",
    "定向增发",
)
PLAN_WORDS = ("预案", "草案")
REGULATORY_APPROVAL_WORDS = ("同意注册", "注册批复", "核准批复", "证监会核准")
ISSUE_WORDS = ("发行结果", "发行情况报告", "上市公告", "新增股份", "股份登记")

_CNINFO_PDF_PATH_RE = re.compile(r"^/finalpage/\d{4}-\d{2}-\d{2}/\d+\.pdf$", re.I)
_NUMBER_TOKEN = r"(?:\d{1,3}(?:\.\d+)?|[零〇一二两三四五六七八九十百]{1,6})"
_ANCHOR_TOKEN = (
    r"自(?:本次)?(?:股份)?(?:发行结束|发行完成|发行新增股份上市|新增股份上市|"
    r"股份上市|上市|股份登记完成|登记完成)(?:之)?日起"
)
_LOCK_PATTERNS = (
    re.compile(
        rf"(?P<anchor>{_ANCHOR_TOKEN})[,，:]?"
        rf"(?P<number>{_NUMBER_TOKEN})(?P<unit>年|个?月)(?:内)?(?:不得转让|不予转让|锁定)"
    ),
    re.compile(
        rf"(?P<anchor>{_ANCHOR_TOKEN})[,，:]?"
        rf"(?:限售期|锁定期|限售期限|锁定期限)?(?:为|是|:)?"
        rf"(?P<number>{_NUMBER_TOKEN})(?P<unit>年|个?月)"
    ),
    re.compile(
        rf"(?:限售期|锁定期|限售期限|锁定期限)(?:均)?(?:为|是|:)?"
        rf"(?P<number>{_NUMBER_TOKEN})(?P<unit>年|个?月)"
    ),
    re.compile(
        rf"(?P<number>{_NUMBER_TOKEN})(?P<unit>年|个?月)(?:内)?不得转让"
    ),
)
_DATE_TOKEN = (
    r"(?P<year>20\d{2})\s*(?:年|[-/.])\s*"
    r"(?P<month>\d{1,2})\s*(?:月|[-/.])\s*"
    r"(?P<day>\d{1,2})\s*日?"
)
_ISSUE_END_DATE_PATTERNS = (
    re.compile(rf"(?:本次)?(?:发行结束日|发行结束日期|发行完成日|发行完成日期)(?:为|是|:)?{_DATE_TOKEN}"),
    re.compile(rf"{_DATE_TOKEN}(?:为|是)?(?:本次)?(?:发行结束日|发行完成日)"),
)
_LIST_DATE_PATTERNS = (
    re.compile(
        rf"(?:本次发行)?(?:新增股份|股份)?(?:上市日|上市日期|上市时间|上市首日)"
        rf"(?:为|是|:)?{_DATE_TOKEN}"
    ),
    re.compile(rf"{_DATE_TOKEN}(?:为|是)?(?:本次发行)?(?:新增股份|股份)?(?:上市日|上市首日)"),
)
_TRADABLE_DATE_PATTERNS = (
    re.compile(
        rf"(?:限售股份|新增股份)?(?:解除限售)?(?:上市流通日|可上市流通日|预计上市流通时间)"
        rf"(?:为|是|:)?{_DATE_TOKEN}"
    ),
    re.compile(rf"{_DATE_TOKEN}(?:为|是)?(?:限售股份|新增股份)?(?:解除限售)?(?:上市流通日|可上市流通日)"),
)
_REGISTRATION_DATE_PATTERNS = (
    re.compile(
        rf"(?:本次发行)?(?:新增股份)?(?:股份登记日|登记完成日|登记完成日期)"
        rf"(?:为|是|:)?{_DATE_TOKEN}"
    ),
    re.compile(rf"{_DATE_TOKEN}(?:为|是)?(?:本次发行)?(?:新增股份)?(?:股份登记日|登记完成日)"),
)
_APPROVAL_DATE_PATTERNS = (
    re.compile(
        rf"{_DATE_TOKEN}[^。；;]{{0,180}}(?:收到|取得|获得)"
        rf"[^。；;]{{0,220}}(?:同意[^。；;]{{0,80}}注册|注册[^。；;]{{0,40}}批复|核准[^。；;]{{0,40}}批复)"
    ),
)


def normalize_code(value: object) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:6]


def clean_title(value: object) -> str:
    return re.sub(r"<[^>]+>", "", str(value or "")).strip()


def normalize_date(value: object) -> str:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d")
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    match = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if not match:
        return ""
    return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def parse_date(value: object) -> datetime | None:
    text = normalize_date(value)
    try:
        return datetime.strptime(text, "%Y-%m-%d") if text else None
    except ValueError:
        return None


def _compact_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text).replace("：", ":")


def _chinese_number(value: str) -> float | None:
    text = str(value or "").strip()
    try:
        return float(text)
    except ValueError:
        pass
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    units = {"十": 10, "百": 100}
    if not text or any(char not in digits and char not in units for char in text):
        return None
    total = 0
    current = 0
    for char in text:
        if char in digits:
            current = digits[char]
        else:
            unit = units[char]
            total += (current or 1) * unit
            current = 0
    return float(total + current)


def _period_months(number: str, unit: str) -> int | None:
    value = _chinese_number(number)
    if value is None or value <= 0:
        return None
    months = value * 12 if unit == "年" else value
    rounded = int(round(months))
    return rounded if 0 < rounded <= 1200 else None


def _date_from_match(match: re.Match) -> str:
    try:
        return f"{int(match.group('year')):04d}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"
    except (IndexError, TypeError, ValueError):
        return ""


def _first_named_date(text: str, patterns: tuple[re.Pattern, ...]) -> dict:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            value = _date_from_match(match)
            if parse_date(value):
                return {
                    "date": value,
                    "evidence": text[max(0, match.start() - 70):min(len(text), match.end() + 70)],
                }
    return {}


def _lock_anchor(match: re.Match, text: str) -> tuple[str, str]:
    anchor_text = str(match.groupdict().get("anchor") or "")
    if not anchor_text:
        before = text[max(0, match.start() - 70):match.start()]
        candidates = []
        for token in ("自发行结束之日起", "自本次发行结束之日起", "自发行完成之日起",
                      "自本次发行完成之日起", "自新增股份上市之日起", "自股份上市之日起",
                      "自本次发行新增股份上市之日起", "自股份登记完成之日起",
                      "自登记完成之日起", "自上市之日起"):
            position = before.rfind(token)
            if position >= 0:
                candidates.append((position, token))
        if candidates:
            anchor_text = max(candidates)[1]
    if "上市" in anchor_text:
        return "listing_date", anchor_text
    if "登记完成" in anchor_text:
        return "share_registration_date", anchor_text
    if "发行结束" in anchor_text or "发行完成" in anchor_text:
        return "issue_end_date", anchor_text
    return "not_explicit_in_clause", anchor_text


def _holder_scope(text: str, position: int) -> tuple[str, str]:
    before = text[max(0, position - 180):position]
    clause = re.split(r"[。；;]", before)[-1][-120:]
    if any(word in clause for word in ("其他发行对象", "其余发行对象", "其他认购对象")):
        return "other_placement_subscribers", clause
    if any(word in clause for word in ("控股股东", "实际控制人")):
        return "controlling_shareholder_or_controller", clause
    if any(word in clause for word in ("交易对方", "业绩承诺方")):
        return "asset_transaction_counterparty", clause
    markers = (
        ("controlling_shareholder_or_controller", ("控股股东", "实际控制人")),
        ("other_placement_subscribers", ("其他发行对象", "其余发行对象", "其他认购对象")),
        ("asset_transaction_counterparty", ("交易对方", "业绩承诺方")),
        ("placement_subscribers", ("发行对象", "认购对象", "获配对象", "认购方")),
    )
    ranked = []
    for scope, words in markers:
        for word in words:
            found = before.rfind(word)
            if found >= 0:
                ranked.append((found, scope))
    scope = max(ranked)[1] if ranked else "unknown"
    return scope, clause


def _transaction_scope(text: str, position: int) -> str:
    context = text[max(0, position - 320):position]
    supporting = max(context.rfind("募集配套资金"), context.rfind("配套融资"))
    consideration = max(context.rfind("发行股份购买资产"), context.rfind("购买资产对价"))
    if supporting >= 0 and consideration >= 0:
        if abs(supporting - consideration) < 20:
            return "unknown"
        return "supporting_finance" if supporting > consideration else "asset_consideration"
    if supporting >= 0:
        return "supporting_finance"
    if consideration >= 0:
        return "asset_consideration"
    if any(word in context for word in (
        "向特定对象发行", "非公开发行", "发行对象", "认购",
        "通过本次发行获得", "公司新发行股份",
    )):
        return "cash_subscription"
    return "unknown"


def _share_applicability(text: str, start: int, end: int) -> tuple[bool, str, str]:
    left = max(text.rfind(mark, 0, start) for mark in ("。", "；", ";"))
    right_candidates = [text.find(mark, end) for mark in ("。", "；", ";")]
    right_candidates = [position for position in right_candidates if position >= 0]
    right = min(right_candidates) if right_candidates else min(len(text), end + 220)
    clause = text[max(0, left + 1):right]
    pre_existing = bool(re.search(
        r"(?:本次发行|本次认购|发行)?前.{0,45}(?:已持有|原持有|持有的).{0,35}股份",
        clause,
    )) or any(phrase in clause for phrase in (
        "本次发行前已持有股份", "本次发行前已持有的股份", "原已持有的股份",
    ))
    if pre_existing:
        return False, "pre_existing_shares", "related_commitment"
    new_share_phrases = (
        "认购的本次发行股份", "认购本次发行股份", "认购的本次发行的股份",
        "本次发行认购的股份", "本次认购的股份", "本次发行的股份",
        "新增股份", "获配股份", "获配的股份",
    )
    if any(phrase in clause for phrase in new_share_phrases):
        return True, "newly_issued_shares", "new_share_lock"
    return True, "newly_issued_shares", "new_share_lock"


def _is_transaction_lock_context(text: str, start: int, end: int) -> bool:
    context = text[max(0, start - 190):min(len(text), end + 80)]
    positive = ("本次发行", "发行对象", "认购", "获配", "新增股份", "交易对方")
    if not any(word in context for word in positive):
        return False
    return True


def _term_holder_key(term: dict) -> str:
    holder_scope = str(term.get("holder_scope") or "unknown")
    value = _compact_text(term.get("holder_or_group") or term.get("holder") or "")
    value = re.split(r"自(?:本次)?", value, maxsplit=1)[0]
    value = re.sub(r"^[（(]?[一二三四五六七八九十0-9]+[)）、,.]", "", value)
    for heading in (
        "新增股份的限售安排", "新增股份限售安排", "限售期安排", "限售安排",
    ):
        value = value.replace(heading, "")
    if "承诺," in value:
        value = value.rsplit("承诺,", 1)[-1]
    elif "承诺，" in value:
        value = value.rsplit("承诺，", 1)[-1]
    for phrase in (
        "通过本次发行获得的公司新发行股份", "通过本次发行获得的新增股份",
        "认购的本次发行的股份", "认购的本次发行股份", "认购本次发行股份",
        "本次发行前已持有的股份", "本次发行前已持有股份", "认购的股份",
        "所持有的股份", "持有的股份", "股份",
    ):
        value = value.replace(phrase, "")
    value = value.strip(" ,，:：")
    generic = {
        "", "发行对象", "认购对象", "获配对象", "其他发行对象", "其余发行对象",
        "其他认购对象", "认购方",
    }
    if value not in generic:
        return value[-80:]
    return holder_scope


def _term_semantic_key(term: dict) -> tuple:
    return (
        int(term.get("months") or 0),
        term.get("applies_to_new_shares"),
        str(term.get("applicable_shares") or "unknown"),
        str(term.get("scope") or "unknown"),
        _term_holder_key(term),
        str(term.get("start_event") or term.get("lock_start_basis") or ""),
    )


def _merge_term_records(terms: list[dict]) -> list[dict]:
    merged: dict[tuple, dict] = {}
    evidence_seen: dict[tuple, set[tuple]] = {}
    for raw in terms:
        if not isinstance(raw, dict):
            continue
        term = dict(raw)
        evidence_rows = term.get("evidence_items")
        if not isinstance(evidence_rows, list) or not evidence_rows:
            evidence_rows = [term.get("evidence")] if isinstance(term.get("evidence"), dict) else []
        key = _term_semantic_key(term)
        current = merged.get(key)
        if current is None:
            current = term
            current["evidence_items"] = []
            merged[key] = current
            evidence_seen[key] = set()
        else:
            for field in (
                "lock_start_date", "lock_start_date_source", "url", "title",
                "announcement_id", "page",
            ):
                if current.get(field) in (None, "") and term.get(field) not in (None, ""):
                    current[field] = term[field]
        for evidence in evidence_rows:
            if not isinstance(evidence, dict):
                continue
            evidence_key = (
                str(evidence.get("announcement_id") or ""),
                evidence.get("page"),
                str(evidence.get("url") or ""),
                str(evidence.get("text") or ""),
            )
            if evidence_key in evidence_seen[key]:
                continue
            evidence_seen[key].add(evidence_key)
            current["evidence_items"].append(dict(evidence))
    result = []
    for term in merged.values():
        evidence_items = term.get("evidence_items") or []
        term["evidence_count"] = len(evidence_items)
        term["evidence"] = evidence_items[0] if evidence_items else {}
        term["pages"] = sorted({row.get("page") for row in evidence_items if row.get("page") is not None})
        term["urls"] = list(dict.fromkeys(
            str(row.get("url")) for row in evidence_items if row.get("url")
        ))
        result.append(term)
    return result


def parse_placement_lock_terms(page_texts: list[str]) -> dict:
    """Extract only explicit batch lock clauses; regulations are never used as defaults."""
    periods = []
    related_commitments = []
    issue_end_dates = []
    listing_dates = []
    tradable_dates = []
    registration_dates = []
    approval_dates = []
    for page_number, raw_text in enumerate(page_texts, 1):
        text = _compact_text(raw_text)
        if not text:
            continue
        matched_ranges: list[tuple[int, int, int]] = []
        issue_end = _first_named_date(text, _ISSUE_END_DATE_PATTERNS)
        listing = _first_named_date(text, _LIST_DATE_PATTERNS)
        tradable = _first_named_date(text, _TRADABLE_DATE_PATTERNS)
        registration = _first_named_date(text, _REGISTRATION_DATE_PATTERNS)
        approval = _first_named_date(text, _APPROVAL_DATE_PATTERNS)
        if issue_end:
            issue_end_dates.append(dict(issue_end, page=page_number))
        if listing:
            listing_dates.append(dict(listing, page=page_number))
        if tradable:
            tradable_dates.append(dict(tradable, page=page_number))
        if registration:
            registration_dates.append(dict(registration, page=page_number))
        if approval:
            approval_dates.append(dict(approval, page=page_number))
        for pattern in _LOCK_PATTERNS:
            for match in pattern.finditer(text):
                if not _is_transaction_lock_context(text, match.start(), match.end()):
                    continue
                months = _period_months(match.group("number"), match.group("unit"))
                if months is None:
                    continue
                if any(
                    old_months == months
                    and old_start <= match.start()
                    and old_end >= match.end()
                    for old_start, old_end, old_months in matched_ranges
                ):
                    continue
                basis, anchor_text = _lock_anchor(match, text)
                scope, scope_text = _holder_scope(text, match.start())
                transaction_scope = _transaction_scope(text, match.start())
                applies_to_new_shares, applicable_shares, term_category = _share_applicability(
                    text, match.start(), match.end()
                )
                matched_ranges.append((match.start(), match.end(), months))
                start_date = ""
                start_date_source = ""
                clause_context = text[max(0, match.start() - 220):min(len(text), match.end() + 220)]
                nearby_issue_end = _first_named_date(clause_context, _ISSUE_END_DATE_PATTERNS)
                nearby_listing = _first_named_date(clause_context, _LIST_DATE_PATTERNS)
                nearby_registration = _first_named_date(clause_context, _REGISTRATION_DATE_PATTERNS)
                if basis == "issue_end_date" and nearby_issue_end:
                    start_date = nearby_issue_end["date"]
                    start_date_source = "cninfo_pdf_explicit_issue_end_date"
                elif basis == "listing_date" and nearby_listing:
                    start_date = nearby_listing["date"]
                    start_date_source = "cninfo_pdf_explicit_listing_date"
                elif basis == "share_registration_date" and nearby_registration:
                    start_date = nearby_registration["date"]
                    start_date_source = "cninfo_pdf_explicit_share_registration_date"
                term = {
                    "months": months,
                    "period": f"{months}个月",
                    "lock_period": f"{months}个月",
                    "term_text": match.group(0),
                    "holder_scope": scope,
                    "scope": transaction_scope if applies_to_new_shares else "related_commitment",
                    "transaction_scope": transaction_scope,
                    "holder_or_group": scope_text or scope,
                    "holder": scope_text if scope_text else "",
                    "holder_scope_text": scope_text,
                    "applies_to_new_shares": applies_to_new_shares,
                    "applicable_shares": applicable_shares,
                    "term_category": term_category,
                    "lock_start_basis": basis,
                    "basis": basis,
                    "start_event": basis,
                    "lock_start_anchor_text": anchor_text,
                    "lock_start_date": start_date,
                    "lock_start_date_source": start_date_source,
                    "page": page_number,
                    "evidence": {
                        "page": page_number,
                        "text": text[max(0, match.start() - 120):min(len(text), match.end() + 120)],
                    },
                }
                if applies_to_new_shares:
                    periods.append(term)
                else:
                    related_commitments.append(term)
    unique_issue_end = sorted({row["date"] for row in issue_end_dates})
    unique_listing = sorted({row["date"] for row in listing_dates})
    unique_tradable = sorted({row["date"] for row in tradable_dates})
    unique_registration = sorted({row["date"] for row in registration_dates})
    unique_approval = sorted({row["date"] for row in approval_dates})
    return {
        "lock_periods": _merge_term_records(periods),
        "related_commitments": _merge_term_records(related_commitments),
        "issue_end_date": unique_issue_end[0] if len(unique_issue_end) == 1 else "",
        "issue_end_date_evidence": issue_end_dates,
        "pdf_listing_date": unique_listing[0] if len(unique_listing) == 1 else "",
        "pdf_listing_date_evidence": listing_dates,
        "tradable_date_official": unique_tradable[0] if len(unique_tradable) == 1 else "",
        "tradable_date_evidence": tradable_dates,
        "share_registration_date": unique_registration[0] if len(unique_registration) == 1 else "",
        "share_registration_date_evidence": registration_dates,
        "regulatory_approval_date": unique_approval[0] if len(unique_approval) == 1 else "",
        "regulatory_approval_date_evidence": approval_dates,
        "date_conflict": (
            len(unique_issue_end) > 1 or len(unique_listing) > 1 or len(unique_tradable) > 1
            or len(unique_registration) > 1 or len(unique_approval) > 1
        ),
    }


def validate_cninfo_pdf_url(url: object) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() != CNINFO_PDF_HOST
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not _CNINFO_PDF_PATH_RE.fullmatch(parsed.path)
    ):
        raise ValueError("only official cninfo finalpage PDF URLs are allowed")
    return value


def download_cninfo_pdf(url: object, timeout: float, session=requests) -> bytes:
    safe_url = validate_cninfo_pdf_url(url)
    response = session.get(
        safe_url,
        timeout=timeout,
        allow_redirects=False,
        stream=True,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; quantinvest-placement-terms/1.0)",
            "Referer": "https://www.cninfo.com.cn/",
            "Accept": "application/pdf",
        },
    )
    try:
        status = int(getattr(response, "status_code", 200))
        if 300 <= status < 400:
            raise ValueError("redirects are not accepted for cninfo PDF downloads")
        response.raise_for_status()
        headers = getattr(response, "headers", {}) or {}
        try:
            declared_size = int(headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared_size = 0
        if declared_size > MAX_PDF_BYTES:
            raise ValueError("PDF exceeds size limit")
        content = bytearray()
        if hasattr(response, "iter_content"):
            chunks = response.iter_content(chunk_size=64 * 1024)
        else:
            chunks = (getattr(response, "content", b""),)
        for chunk in chunks:
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > MAX_PDF_BYTES:
                raise ValueError("PDF exceeds size limit")
        result = bytes(content)
        if not result.startswith(b"%PDF"):
            raise ValueError("cninfo response is not a PDF")
        return result
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def extract_pdf_pages(pdf_bytes: bytes) -> list[str]:
    reader = PdfReader(BytesIO(pdf_bytes))
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError("PDF page count exceeds limit")
    return [(page.extract_text() or "") for page in reader.pages]


def market_column(code: str) -> str:
    return "sse" if normalize_code(code).startswith(("6", "9")) else "szse"


def announcement_row(raw: dict) -> dict:
    date = normalize_date(
        raw.get("announcementTime") or raw.get("announcementDate") or raw.get("date")
    )
    adjunct = str(raw.get("adjunctUrl") or "").lstrip("/")
    return {
        "code": normalize_code(raw.get("secCode") or raw.get("code")),
        "date": date,
        "title": clean_title(raw.get("announcementTitle") or raw.get("title")),
        "announcement_id": str(raw.get("announcementId") or ""),
        "org_id": str(raw.get("orgId") or ""),
        "url": f"https://static.cninfo.com.cn/{adjunct}" if adjunct else "",
    }


def dedupe_announcements(rows: list[dict]) -> list[dict]:
    seen: set[tuple[str, str, str]] = set()
    result = []
    for row in rows:
        key = (str(row.get("announcement_id") or ""), row.get("date", ""), row.get("title", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return sorted(result, key=lambda row: (row.get("date", ""), row.get("title", "")))


def is_placement_title(title: object) -> bool:
    text = clean_title(title)
    return any(word in text for word in PLACEMENT_TERMS)


def is_regulatory_approval(title: object) -> bool:
    text = clean_title(title)
    if not is_placement_title(text):
        return False
    approval_phrase = any(word in text for word in REGULATORY_APPROVAL_WORDS) or bool(
        re.search(r"同意.{0,100}注册(?:的)?批复|注册(?:的)?批复", text)
    )
    return approval_phrase and (
        "证监会" in text or "中国证券监督管理委员会" in text or "同意注册批复" in text
    )


def is_exchange_approval(title: object) -> bool:
    text = clean_title(title)
    return is_placement_title(text) and "审核通过" in text and any(
        word in text for word in ("交易所", "上交所", "深交所", "北交所")
    )


def _dated(rows: list[dict], predicate, *, start: str = "", end: str = "") -> list[dict]:
    result = []
    for row in rows:
        date = row.get("date") or ""
        if not date or (start and date < start) or (end and date > end):
            continue
        if predicate(row.get("title") or ""):
            result.append(row)
    return result


def _evidence(row: dict | None, basis: str, confidence: str) -> dict:
    if not row:
        return {}
    return {
        "date": row.get("date") or "",
        "title": row.get("title") or "",
        "url": row.get("url") or "",
        "announcement_id": row.get("announcement_id") or "",
        "basis": basis,
        "confidence": confidence,
    }


def _select_initial_plan(plan_candidates: list[dict], anchor_date: datetime | None) -> dict | None:
    candidates = list(plan_candidates)
    if anchor_date:
        candidates = [
            row for row in candidates
            if parse_date(row.get("date"))
            and 0 <= (anchor_date - parse_date(row.get("date"))).days <= 365 * 3
        ]
    version_words = ("修订", "更新", "注册稿", "上会稿", "审核稿", "申报稿", "反馈稿")
    ancillary_words = ("差异", "对比", "说明", "提示性", "摘要", "回复", "问询")
    original_preplans = [
        row for row in candidates
        if "预案" in row["title"]
        and not any(word in row["title"] for word in version_words + ancillary_words)
    ]
    plain_plans = [
        row for row in candidates
        if not any(word in row["title"] for word in version_words + ancillary_words)
    ]
    if original_preplans:
        return original_preplans[-1]
    if plain_plans:
        return plain_plans[-1]
    if not candidates:
        return None
    cluster = [candidates[-1]]
    for candidate in reversed(candidates[:-1]):
        newer = parse_date(cluster[-1].get("date"))
        older = parse_date(candidate.get("date"))
        if not newer or not older or (newer - older).days > 540:
            break
        cluster.append(candidate)
    return cluster[-1]


def _current_batch_issue_documents(
    seed: dict,
    announcements: list[dict],
    lifecycle: dict,
) -> list[dict]:
    reference = parse_date(seed.get("list_date") or seed.get("issue_date"))
    issue_evidence = (lifecycle.get("stage_evidence") or {}).get("issue")
    batch_reference = parse_date(issue_evidence.get("date")) if isinstance(issue_evidence, dict) else None
    batch_reference = batch_reference or reference
    start = parse_date(lifecycle.get("approval_date") or lifecycle.get("plan_date"))
    if reference and not start:
        start = reference - timedelta(days=240)
    end = reference + timedelta(days=60) if reference else None
    candidates = []
    for row in dedupe_announcements(announcements):
        title = clean_title(row.get("title"))
        row_date = parse_date(row.get("date"))
        if not is_placement_title(title) or not any(word in title for word in ISSUE_WORDS):
            continue
        if start and row_date and row_date < start:
            continue
        if end and row_date and row_date > end:
            continue
        if batch_reference and row_date and abs((row_date - batch_reference).days) > 90:
            continue
        try:
            validate_cninfo_pdf_url(row.get("url"))
        except ValueError:
            continue
        candidates.append(row)

    def rank(row: dict) -> tuple[int, int, str, str]:
        title = clean_title(row.get("title"))
        kind_rank = 0 if ("发行情况报告" in title or "上市公告" in title) else 1
        row_date = parse_date(row.get("date"))
        distance = abs((row_date - reference).days) if row_date and reference else 999999
        return kind_rank, distance, row.get("date", ""), row.get("announcement_id", "")

    ordered = sorted(candidates, key=rank)
    if batch_reference and ordered:
        distances = [
            abs((parse_date(row.get("date")) - batch_reference).days)
            for row in ordered if parse_date(row.get("date"))
        ]
        if distances:
            nearest = min(distances)
            ordered = [
                row for row in ordered
                if not parse_date(row.get("date"))
                or abs((parse_date(row.get("date")) - batch_reference).days) <= nearest + 45
            ]
    return ordered[:MAX_BATCH_PDFS]


def collect_official_lock_evidence(
    seed: dict,
    announcements: list[dict],
    lifecycle: dict,
    *,
    timeout: float,
    session=requests,
) -> dict:
    documents = []
    periods = []
    related_commitments = []
    issue_end_dates = set()
    listing_dates = set()
    tradable_dates = set()
    registration_dates = set()
    approval_dates = set()
    approval_evidence = []
    date_conflict = False
    for row in _current_batch_issue_documents(seed, announcements, lifecycle):
        document = {
            "announcement_id": str(row.get("announcement_id") or ""),
            "date": row.get("date") or "",
            "title": row.get("title") or "",
            "url": row.get("url") or "",
        }
        try:
            pages = extract_pdf_pages(download_cninfo_pdf(row.get("url"), timeout, session=session))
            parsed = parse_placement_lock_terms(pages)
            document["status"] = (
                "parsed" if parsed["lock_periods"] or parsed["related_commitments"]
                else "no_explicit_lock_term"
            )
            document["page_count"] = len(pages)
            document["term_count"] = len(parsed["lock_periods"])
            document["related_commitment_count"] = len(parsed["related_commitments"])
            if parsed.get("issue_end_date"):
                issue_end_dates.add(parsed["issue_end_date"])
            if parsed.get("pdf_listing_date"):
                listing_dates.add(parsed["pdf_listing_date"])
            if parsed.get("tradable_date_official"):
                tradable_dates.add(parsed["tradable_date_official"])
            if parsed.get("share_registration_date"):
                registration_dates.add(parsed["share_registration_date"])
            if parsed.get("regulatory_approval_date"):
                approval_dates.add(parsed["regulatory_approval_date"])
                for evidence in parsed.get("regulatory_approval_date_evidence") or []:
                    value = dict(evidence)
                    value.update({
                        "announcement_id": document["announcement_id"],
                        "announcement_date": document["date"],
                        "title": document["title"],
                        "url": document["url"],
                    })
                    approval_evidence.append(value)
            date_conflict = date_conflict or bool(parsed.get("date_conflict"))
            for term in parsed["lock_periods"] + parsed["related_commitments"]:
                value = dict(term)
                if value.get("scope") == "unknown":
                    has_consideration = "发行股份购买资产" in document["title"]
                    has_supporting = any(word in document["title"] for word in ("募集配套资金", "配套融资"))
                    if has_consideration != has_supporting:
                        value["scope"] = "asset_consideration" if has_consideration else "supporting_finance"
                    elif is_placement_title(document["title"]):
                        value["scope"] = "cash_subscription"
                raw_evidence = value.get("evidence_items")
                if not isinstance(raw_evidence, list) or not raw_evidence:
                    raw_evidence = [value.get("evidence") or {}]
                evidence_items = []
                for raw_item in raw_evidence:
                    evidence = dict(raw_item) if isinstance(raw_item, dict) else {}
                    evidence.update({
                        "announcement_id": document["announcement_id"],
                        "announcement_date": document["date"],
                        "title": document["title"],
                        "url": document["url"],
                    })
                    evidence_items.append(evidence)
                value["evidence_items"] = evidence_items
                value["evidence"] = evidence_items[0] if evidence_items else {}
                value["source"] = "cninfo_official_pdf"
                value["announcement_id"] = document["announcement_id"]
                value["title"] = document["title"]
                value["url"] = document["url"]
                if value.get("applies_to_new_shares") is False:
                    related_commitments.append(value)
                else:
                    periods.append(value)
        except Exception as exc:
            document["status"] = "error"
            document["error"] = str(exc)[:300]
        documents.append(document)

    return {
        "lock_periods": _merge_term_records(periods),
        "related_commitments": _merge_term_records(related_commitments),
        "documents": documents,
        "issue_end_dates": sorted(issue_end_dates),
        "listing_dates": sorted(listing_dates),
        "tradable_dates": sorted(tradable_dates),
        "registration_dates": sorted(registration_dates),
        "approval_dates": sorted(approval_dates),
        "approval_evidence": approval_evidence,
        "date_conflict": (
            date_conflict or len(issue_end_dates) > 1 or len(listing_dates) > 1
            or len(tradable_dates) > 1 or len(registration_dates) > 1
            or len(approval_dates) > 1
        ),
    }


def _secondary_lock_term(value: object, source: str) -> list[dict]:
    text = _compact_text(value)
    if not text:
        return []
    match = re.search(rf"(?P<number>{_NUMBER_TOKEN})(?P<unit>年|个?月)", text)
    if match:
        number = match.group("number")
        unit = match.group("unit")
        term_text = match.group(0)
    elif re.fullmatch(r"\d{1,3}(?:\.\d+)?", text):
        number = text
        unit = "个月"
        term_text = text
    else:
        return []
    months = _period_months(number, unit)
    if months is None:
        return []
    return [{
        "months": months,
        "period": f"{months}个月",
        "lock_period": f"{months}个月",
        "term_text": term_text,
        "holder_scope": "unknown",
        "scope": "unknown",
        "holder_or_group": "unknown",
        "applies_to_new_shares": True,
        "applicable_shares": "newly_issued_shares",
        "term_category": "secondary_new_share_lock",
        "lock_start_basis": "not_provided_by_secondary_source",
        "basis": "not_provided_by_secondary_source",
        "start_event": "not_provided_by_secondary_source",
        "lock_start_date": "",
        "lock_start_date_source": "",
        "source": source,
        "evidence": {"field_value": str(value)},
    }]


def resolve_lock_term_evidence(
    seed: dict,
    official: dict | None,
    *,
    eastmoney_lock_period: object = "",
    eastmoney_source: str = "eastmoney:RPT_SEO_DETAIL.LOCKIN_PERIOD",
) -> dict:
    """Select batch evidence without applying current rules to historical transactions."""
    official = official if isinstance(official, dict) else {}
    official_periods = [
        dict(row) for row in official.get("lock_periods") or []
        if isinstance(row, dict) and row.get("applies_to_new_shares") is not False
    ]
    related_commitments = [
        dict(row) for row in official.get("related_commitments") or [] if isinstance(row, dict)
    ]
    secondary_value = eastmoney_lock_period or seed.get("eastmoney_lock_period")
    secondary_source = eastmoney_source
    if not secondary_value:
        secondary_value = seed.get("lock_period") or seed.get("lock") or ""
        secondary_source = str(seed.get("lock_term_source") or "seed:lock_period")
    secondary_periods = _secondary_lock_term(secondary_value, secondary_source)

    reference_date = parse_date(seed.get("issue_date") or seed.get("list_date"))

    if reference_date is not None:
        official_periods = [
            term for term in official_periods
            if not parse_date(term.get("lock_start_date"))
            or abs((parse_date(term.get("lock_start_date")) - reference_date).days) <= 370
        ]

    def batch_near_dates(values: object) -> list[str]:
        result = set()
        for value in values or []:
            parsed = parse_date(value)
            if parsed and (reference_date is None or abs((parsed - reference_date).days) <= 370):
                result.add(str(value))
        return sorted(result)

    issue_end_dates = batch_near_dates(official.get("issue_end_dates"))
    pdf_listing_dates = batch_near_dates(official.get("listing_dates"))
    tradable_dates = sorted({str(value) for value in official.get("tradable_dates") or [] if parse_date(value)})
    registration_dates = batch_near_dates(official.get("registration_dates"))
    trusted_list_date = normalize_date(seed.get("list_date"))
    trusted_list_source = str(seed.get("list_date_source") or "")
    if len(issue_end_dates) == 1:
        issue_end_date = issue_end_dates[0]
    else:
        issue_end_date = ""

    for term in official_periods:
        if term.get("lock_start_date"):
            continue
        if term.get("start_event") == "issue_end_date" and issue_end_date:
            term["lock_start_date"] = issue_end_date
            term["lock_start_date_source"] = "cninfo_pdf_explicit_issue_end_date"
        elif term.get("start_event") == "listing_date":
            if len(pdf_listing_dates) == 1:
                term["lock_start_date"] = pdf_listing_dates[0]
                term["lock_start_date_source"] = "cninfo_pdf_explicit_listing_date"
            elif trusted_list_date and trusted_list_source:
                term["lock_start_date"] = trusted_list_date
                term["lock_start_date_source"] = trusted_list_source
        elif term.get("start_event") == "share_registration_date" and len(registration_dates) == 1:
            term["lock_start_date"] = registration_dates[0]
            term["lock_start_date_source"] = "cninfo_pdf_explicit_share_registration_date"

    official_months = sorted({int(row["months"]) for row in official_periods if row.get("months")})
    secondary_months = sorted({int(row["months"]) for row in secondary_periods if row.get("months")})
    source_mismatch = bool(official_months and secondary_months and not set(official_months).intersection(secondary_months))
    date_conflict = bool(official.get("date_conflict"))
    conflict = source_mismatch or date_conflict
    conflict_detail = {}
    if source_mismatch:
        conflict_detail["period_source_mismatch"] = {
            "cninfo_official_pdf_months": official_months,
            "eastmoney_or_seed_months": secondary_months,
        }
    if date_conflict:
        conflict_detail["official_date_conflict"] = {
            "issue_end_dates": issue_end_dates,
            "listing_dates": pdf_listing_dates,
            "tradable_dates": tradable_dates,
            "registration_dates": registration_dates,
        }

    selected = official_periods or secondary_periods
    if official_periods:
        source = "cninfo_official_pdf"
        confidence = "high" if all(row.get("start_event") != "not_explicit_in_clause" for row in official_periods) else "medium"
    elif secondary_periods:
        source = secondary_source
        confidence = "medium"
    else:
        source = "pending_official_evidence"
        confidence = "pending"

    selected_months = sorted({int(row["months"]) for row in selected if row.get("months")})
    if len(selected_months) == 1:
        legacy_period = f"{selected_months[0]}个月"
    elif len(selected_months) > 1:
        legacy_period = "多个限售期限（详见公告证据）"
    else:
        legacy_period = ""

    start_events = sorted({str(row.get("start_event")) for row in selected if row.get("start_event")})
    start_dates = sorted({str(row.get("lock_start_date")) for row in selected if parse_date(row.get("lock_start_date"))})
    start_sources = sorted({str(row.get("lock_start_date_source")) for row in selected if row.get("lock_start_date_source")})
    return {
        "lock_period": legacy_period,
        "lock_periods": selected,
        "lock_tranches": selected,
        "related_commitments": related_commitments,
        "lock_term_source": source,
        "lock_term_confidence": confidence,
        "lock_term_evidence": official.get("documents") or [],
        "lock_term_conflict": conflict,
        "lock_term_conflict_detail": conflict_detail,
        "lock_start_basis": start_events[0] if len(start_events) == 1 else ("multiple" if start_events else "pending"),
        "lock_start_date": start_dates[0] if len(start_dates) == 1 else "",
        "lock_start_date_source": start_sources[0] if len(start_sources) == 1 else "",
        "issue_end_date": issue_end_date,
        "issue_end_date_source": "cninfo_official_pdf" if issue_end_date else "",
        "share_registration_date": registration_dates[0] if len(registration_dates) == 1 else "",
        "share_registration_date_source": "cninfo_official_pdf" if len(registration_dates) == 1 else "",
        "lock_expiry_date_estimated": "",
        "tradable_date_official": tradable_dates[0] if len(tradable_dates) == 1 else "",
        "tradable_date_source": "cninfo_official_pdf" if len(tradable_dates) == 1 else "",
        "lock_term_policy": "batch_announcement_evidence_only_no_current_rule_backfill",
        "eastmoney_lock_period": str(eastmoney_lock_period or ""),
    }


def build_lifecycle(seed: dict, announcements: list[dict]) -> dict:
    """Aggregate one completed placement batch without inventing regulatory dates."""
    code = normalize_code(seed.get("code") or seed.get("ts_code"))
    list_date = normalize_date(seed.get("list_date") or seed.get("listing_date"))
    issue_date = normalize_date(seed.get("issue_date"))
    issue_date_source = str(seed.get("issue_date_source") or "")
    reference = list_date or issue_date
    rows = [row for row in dedupe_announcements(announcements) if normalize_code(row.get("code")) == code]

    placement_rows = [row for row in rows if is_placement_title(row.get("title"))]
    reference_date = parse_date(reference)
    project_floor = (reference_date - timedelta(days=365 * 3)).strftime("%Y-%m-%d") if reference_date else ""
    exchange_candidates = _dated(
        placement_rows,
        is_exchange_approval,
        start=project_floor,
        end=reference,
    )
    exchange_approval = exchange_candidates[-1] if exchange_candidates else None
    exchange_date = exchange_approval.get("date", "") if exchange_approval else ""

    approval_candidates = _dated(
        placement_rows,
        is_regulatory_approval,
        start=exchange_date or project_floor,
        end=reference,
    )
    approval = approval_candidates[-1] if approval_candidates else None
    approval_date = approval.get("date", "") if approval else ""

    plan_candidates = _dated(
        placement_rows,
        lambda title: any(word in clean_title(title) for word in PLAN_WORDS),
        end=exchange_date or approval_date or reference,
    )
    plan = _select_initial_plan(
        plan_candidates, parse_date(exchange_date or approval_date or reference)
    )
    plan_date = plan.get("date", "") if plan else ""

    board_candidates = _dated(
        rows,
        lambda title: "董事会" in clean_title(title) and "决议" in clean_title(title),
        start=plan_date,
        end=approval_date or reference,
    )
    board = None
    if plan_date:
        same_day = [row for row in board_candidates if row.get("date") == plan_date]
        board = same_day[-1] if same_day else None
    if board:
        board_evidence = _evidence(board, "same_day_board_resolution_and_plan", "medium")
    elif plan:
        board_evidence = _evidence(plan, "same_day_plan_announcement", "medium")
    else:
        board_evidence = {}

    shareholder_candidates = _dated(
        rows,
        lambda title: (
            ("股东大会" in clean_title(title) or "股东会" in clean_title(title))
            and "决议" in clean_title(title)
        ),
        start=plan_date,
        end=exchange_date or approval_date or reference,
    )
    shareholder = shareholder_candidates[0] if shareholder_candidates else None

    issue_candidates = _dated(
        placement_rows,
        lambda title: any(word in clean_title(title) for word in ISSUE_WORDS),
        start=approval_date or plan_date,
    )
    issue_event = None
    if issue_candidates:
        if reference:
            issue_event = min(
                issue_candidates,
                key=lambda row: abs((parse_date(row.get("date")) - parse_date(reference)).days),
            )
        else:
            issue_event = issue_candidates[-1]

    evidence = {
        "plan": _evidence(plan, "placement_plan_announcement", "high"),
        "board": board_evidence,
        "shareholder": _evidence(
            shareholder, "first_shareholder_resolution_between_plan_and_approval", "medium"
        ),
        "exchange_approval": _evidence(
            exchange_approval, "exchange_review_passed_announcement", "high"
        ),
        "approval": _evidence(approval, "csrc_registration_or_approval_announcement", "high"),
        "issue": _evidence(issue_event, "placement_issue_or_listing_announcement", "high"),
    }
    latest_evidence = issue_event or approval or exchange_approval or shareholder or plan
    project_anchor = str((plan or {}).get("announcement_id") or plan_date or reference or "pending")
    batch_anchor = str((issue_event or {}).get("announcement_id") or issue_date or list_date or project_anchor)
    result = {
        "code": code,
        "ts_code": seed.get("ts_code") or "",
        "name": seed.get("name") or "",
        "project_id": f"{code}:project:{project_anchor}",
        "batch_id": f"{code}:batch:{batch_anchor}",
        "plan_date": plan_date,
        "board_date": board_evidence.get("date", ""),
        "shareholder_date": shareholder.get("date", "") if shareholder else "",
        "exchange_approval_date": exchange_approval.get("date", "") if exchange_approval else "",
        "approval_date": approval_date,
        "issue_date": issue_date,
        "issue_date_source": issue_date_source,
        "issue_announcement_date": issue_event.get("date", "") if issue_event else "",
        "list_date": list_date,
        "list_date_source": seed.get("list_date_source") or "",
        "issue_price": seed.get("issue_price"),
        "stage_evidence": evidence,
        "lifecycle_source": "cninfo",
        "lifecycle_status": "complete" if all(
            (plan_date, board_evidence, shareholder, approval)
        ) else "partial",
        "title": latest_evidence.get("title", "") if latest_evidence else "",
        "url": latest_evidence.get("url", "") if latest_evidence else "",
        "announcement_id": latest_evidence.get("announcement_id", "") if latest_evidence else "",
    }
    result.update(resolve_lock_term_evidence(seed, None))
    return result


class PlacementClient:
    def __init__(self, timeout: float = 12.0, sleep_s: float = 0.15):
        self.timeout = timeout
        self.sleep_s = sleep_s
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.cninfo.com.cn/new/index",
            "Accept": "application/json,text/plain,*/*",
        })

    def _cninfo_pages(self, params: dict, max_pages: int = 5) -> list[dict]:
        rows = []
        for page in range(1, max_pages + 1):
            payload = dict(params, pageNum=page, pageSize=30)
            response = self.session.post(CNINFO_URL, data=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            announcements = data.get("announcements") or []
            rows.extend(announcement_row(row) for row in announcements)
            total = int(data.get("totalAnnouncement") or 0)
            if not announcements or page * 30 >= total:
                break
            time.sleep(self.sleep_s)
        return rows

    def search_fulltext(self, code: str, keyword: str, start: str, end: str) -> list[dict]:
        return self._cninfo_pages({
            "column": market_column(code),
            "tabName": "fulltext",
            "plate": "",
            "stock": "",
            "searchkey": f"{code} {keyword}",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": f"{start}~{end}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        })

    def search_company(self, code: str, org_id: str, keyword: str, start: str, end: str) -> list[dict]:
        if not org_id:
            return []
        return self._cninfo_pages({
            "column": market_column(code),
            "tabName": "fulltext",
            "plate": "",
            "stock": f"{code},{org_id}",
            "searchkey": keyword,
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": f"{start}~{end}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        })

    def eastmoney_record(self, seed: dict) -> dict:
        code = normalize_code(seed.get("code") or seed.get("ts_code"))
        params = {
            "pageSize": 50,
            "pageNumber": 1,
            "reportName": "RPT_SEO_DETAIL",
            "columns": "ALL",
            "filter": f'(SECURITY_CODE="{code}")(SEO_TYPE="1")',
            "source": "WEB",
            "client": "WEB",
        }
        response = self.session.get(EASTMONEY_URL, params=params, timeout=self.timeout)
        response.raise_for_status()
        rows = ((response.json().get("result") or {}).get("data") or [])
        target = normalize_date(seed.get("list_date"))
        matches = [row for row in rows if normalize_date(row.get("ISSUE_LISTING_DATE")) == target]
        row = matches[0] if matches else ((rows[0] if rows else {}) if not target else {})
        issue_date = normalize_date(row.get("ISSUE_DATE"))
        list_date = normalize_date(row.get("ISSUE_LISTING_DATE"))
        lock_period = row.get("LOCKIN_PERIOD") or ""
        return {
            "issue_date": issue_date,
            "issue_date_source": "eastmoney:RPT_SEO_DETAIL.ISSUE_DATE" if issue_date else "",
            "list_date": list_date,
            "list_date_source": "eastmoney:RPT_SEO_DETAIL.ISSUE_LISTING_DATE" if list_date else "",
            "issue_price": row.get("ISSUE_PRICE"),
            "eastmoney_lock_period": lock_period,
            "price_principle": row.get("PRICE_PRINCIPLE") or "",
        }

    def collect(self, seed: dict) -> list[dict]:
        reference = parse_date(seed.get("list_date") or seed.get("issue_date")) or datetime.now()
        start = (reference - timedelta(days=365 * 5)).strftime("%Y-%m-%d")
        end = (reference + timedelta(days=45)).strftime("%Y-%m-%d")
        code = normalize_code(seed.get("code") or seed.get("ts_code"))
        rows = []
        for keyword in PLACEMENT_TERMS[:3]:
            rows.extend(self.search_fulltext(code, keyword, start, end))
            time.sleep(self.sleep_s)
        rows = dedupe_announcements(rows)
        org_id = next((row.get("org_id") for row in rows if row.get("org_id")), "")
        plan_rows = [
            row for row in rows
            if is_placement_title(row.get("title")) and any(word in row.get("title", "") for word in PLAN_WORDS)
        ]
        initial_plan = _select_initial_plan(plan_rows, reference)
        generic_start = initial_plan.get("date") if initial_plan else start
        for keyword in ("董事会", "股东大会", "股东会"):
            rows.extend(self.search_company(code, org_id, keyword, generic_start, end))
            time.sleep(self.sleep_s)
        return dedupe_announcements(rows)


def load_seeds(path: Path, codes: set[str]) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("items") if isinstance(payload, dict) else []
    result = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = normalize_code(row.get("code") or row.get("ts_code"))
        if code and (not codes or code in codes):
            result.append(dict(row, code=code))
    return result


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export evidence-backed private-placement lifecycle dates.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--seed-file", default="", help="Defaults to DATA_DIR/asset_injection.json.")
    parser.add_argument("--output", default="", help="Defaults to DATA_DIR/cninfo_placement.json.")
    parser.add_argument("--codes", nargs="*", default=[])
    parser.add_argument("--sleep", type=float, default=0.15)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--max-items", type=int, default=0)
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    seed_path = Path(args.seed_file) if args.seed_file else data_dir / "asset_injection.json"
    seeds = load_seeds(seed_path, {normalize_code(code) for code in args.codes if normalize_code(code)})
    if args.max_items > 0:
        seeds = seeds[:args.max_items]
    client = PlacementClient(timeout=args.timeout, sleep_s=args.sleep)
    items = []
    errors = []
    warnings = []
    for index, raw_seed in enumerate(seeds, 1):
        code = normalize_code(raw_seed.get("code"))
        try:
            seed = dict(raw_seed)
            eastmoney = {}
            try:
                eastmoney = client.eastmoney_record(seed)
            except Exception as exc:
                warnings.append({
                    "code": code,
                    "list_date": raw_seed.get("list_date") or "",
                    "source": "eastmoney",
                    "error": str(exc)[:300],
                })
            for key, value in eastmoney.items():
                if value not in (None, ""):
                    seed[key] = value
            announcements = client.collect(seed)
            item = build_lifecycle(seed, announcements)
            official = collect_official_lock_evidence(
                seed,
                announcements,
                item,
                timeout=args.timeout,
                session=client.session,
            )
            approval_dates = official.get("approval_dates") or []
            if not item.get("approval_date") and len(approval_dates) == 1:
                item["approval_date"] = approval_dates[0]
                evidence = next((
                    row for row in official.get("approval_evidence") or []
                    if row.get("date") == approval_dates[0]
                ), {})
                item.setdefault("stage_evidence", {})["approval"] = {
                    "date": approval_dates[0],
                    "title": evidence.get("title") or "",
                    "url": evidence.get("url") or "",
                    "announcement_id": evidence.get("announcement_id") or "",
                    "page": evidence.get("page"),
                    "basis": "approval_date_stated_in_official_issue_document",
                    "confidence": "high",
                }
                item["lifecycle_status"] = "complete" if all((
                    item.get("plan_date"), item.get("board_date"),
                    item.get("shareholder_date"), item.get("approval_date"),
                )) else "partial"
            item.update(resolve_lock_term_evidence(
                seed,
                official,
                eastmoney_lock_period=eastmoney.get("eastmoney_lock_period") or "",
            ))
            item["price_principle"] = eastmoney.get("price_principle") or ""
            items.append(item)
            parsed_docs = sum(1 for row in official["documents"] if row.get("status") == "parsed")
            print(
                f"{index}/{len(seeds)} {code} lifecycle={item['lifecycle_status']} "
                f"announcements={len(announcements)} lock_terms={len(item['lock_periods'])} "
                f"pdfs={parsed_docs}/{len(official['documents'])}"
            )
        except Exception as exc:
            errors.append({
                "code": code,
                "list_date": raw_seed.get("list_date") or "",
                "source": "cninfo_lifecycle",
                "error": str(exc)[:300],
            })
            pending = build_lifecycle(raw_seed, [])
            pending["lifecycle_status"] = "error"
            items.append(pending)
            print(f"{index}/{len(seeds)} {code} ERROR {exc}")
        time.sleep(args.sleep)

    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "cninfo_official_pdf+cninfo_lifecycle+eastmoney_secondary",
        "lock_parser_version": LOCK_PARSER_VERSION,
        "lock_term_policy": "batch_announcement_evidence_only_no_current_rule_backfill",
        "items": items,
        "errors": errors,
        "warnings": warnings,
        "count": len(items),
    }
    output = Path(args.output) if args.output else data_dir / "cninfo_placement.json"
    _atomic_write_json(output, payload)
    print(f"wrote {output} items={len(items)} errors={len(errors)}")


if __name__ == "__main__":
    main()
