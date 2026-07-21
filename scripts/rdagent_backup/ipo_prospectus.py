"""Official prospectus facts for newly listed-stock reports."""
from __future__ import annotations

import re
from typing import Any

import requests


KNOWN_PROSPECTUSES = {
    "001248": "https://static.cninfo.com.cn/finalpage/2026-06-26/1225389172.PDF",
}


def _c6(code: str) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())[:6]


def _sentences(text: str, keywords: tuple[str, ...], limit: int = 12) -> list[str]:
    clean = re.sub(r"\s+", " ", text or "")
    parts = re.split(r"(?<=[。；！？])", clean)
    return [p.strip() for p in parts if any(k in p for k in keywords)][:limit]


def parse_prospectus_text(text: str) -> dict[str, Any]:
    """Extract conservative, source-backed facts without using an LLM."""
    company = ""
    match = re.search(
        r"([\u4e00-\u9fffA-Za-z（）()]{4,40}?(?:股份|控股)?有限公司)",
        text,
    )
    if match:
        company = match.group(1)
    code_match = re.search(r"(?:股票|证券)代码[：:\s]*([0-9]{6})", text)
    business = _sentences(
        text, ("主营业务", "风力发电", "太阳能发电", "光伏发电",
               "业务包括", "新能源项目"))
    fundraising = _sentences(text, ("募集资金", "募投项目", "投资项目"))
    advantages = _sentences(text, ("竞争优势", "核心竞争力", "行业地位"))
    risks = _sentences(text, ("风险因素", "主要风险", "风险提示"))
    return {
        "company_name": company,
        "stock_code": code_match.group(1) if code_match else "",
        "business": "\n".join(business),
        "fundraising": "\n".join(fundraising),
        "advantages": "\n".join(advantages),
        "risks": "\n".join(risks),
    }


def validate_identity(facts: dict[str, Any], ts_code: str, name: str) -> bool:
    fact_code = _c6(facts.get("stock_code", ""))
    code_ok = not fact_code or _c6(ts_code) == fact_code
    fact_name = str(facts.get("company_name") or "")
    short_name = str(name or "").replace("N", "", 1)
    name_ok = bool(short_name and short_name in fact_name)
    return code_ok and name_ok


def build_business_context(
    code: str,
    name: str,
    tushare_mainbz: list[dict[str, Any]],
    prospectus: dict[str, Any] | None,
) -> tuple[str, str]:
    if tushare_mainbz:
        lines = [
            f"{row.get('item')}: 营收{row.get('sales')}亿/"
            f"占比{row.get('pct')}%/毛利率{row.get('gm')}%"
            for row in tushare_mainbz
        ]
        return "\n".join(lines), "tushare"
    prospectus = prospectus or {}
    business = str(prospectus.get("business") or "").strip()
    if business:
        source = str(prospectus.get("source_url") or "")
        return f"{business}\n官方来源:{source}", "prospectus"
    return "官方主营资料不足，禁止推测主营业务。", "insufficient_data"


def load_prospectus(
    ts_code: str,
    name: str,
    source_url: str = "",
    timeout: int = 30,
) -> dict[str, Any]:
    """Download, extract and identity-check an official CNINFO PDF."""
    url = source_url or KNOWN_PROSPECTUSES.get(_c6(ts_code), "")
    if not url.startswith("https://static.cninfo.com.cn/") or not url.lower().endswith(".pdf"):
        return {}
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    import fitz

    doc = fitz.open(stream=response.content, filetype="pdf")
    page_text = []
    identity_text = []
    keywords = ("主营业务", "风力发电", "光伏发电", "募集资金",
                "募投项目", "竞争优势", "核心竞争力", "风险因素")
    for index, page in enumerate(doc):
        text = page.get_text("text")
        if index < 12:
            identity_text.append(text)
        if any(keyword in text for keyword in keywords):
            page_text.append(text)
    text = "\n".join(identity_text + page_text)
    facts = parse_prospectus_text(text)
    if not validate_identity(facts, ts_code, name):
        return {}
    facts["source_url"] = url
    return facts
