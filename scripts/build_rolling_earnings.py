from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
UNIVERSE_LABEL = {"csi300": "沪深300", "csi500": "中证500", "csi1000": "中证1000"}
UNIVERSE_RANK = {"csi300": 0, "csi500": 1, "csi1000": 2}
SOURCE_RANK = {"业绩报告": 0, "半年度报告": 0, "季度报告": 0, "业绩快报": 1, "业绩预告": 2}


def c6(code: str) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())[:6]


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload, ensure_ascii=False, indent=2, allow_nan=False
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        fd, raw_temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary = Path(raw_temporary)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def norm_date(value) -> str:
    text = str(value or "").strip()
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text[:10].replace("/", "-")


def fnum(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def source_type(row: dict) -> str:
    text = " ".join(str(row.get(k) or "") for k in ("source", "kind", "type", "title", "summary"))
    for key in ("业绩报告", "半年度报告", "季度报告", "业绩快报", "业绩预告"):
        if key in text:
            return key
    typ = str(row.get("type") or "")
    return typ or "业绩预告"


def latest_growth(row: dict):
    for key in ("q2_yoy", "q4_yoy", "q3_yoy", "q1_yoy", "dedt_q_yoy", "dedt_yoy", "dedt_h1_yoy"):
        val = fnum(row.get(key))
        if val is not None:
            return val
    lo = fnum(row.get("dedt_yoy_min") or row.get("dedt_lo_yoy") or row.get("p_chg_min"))
    hi = fnum(row.get("dedt_yoy_max") or row.get("dedt_hi_yoy") or row.get("p_chg_max"))
    if lo is not None and hi is not None:
        return (lo + hi) / 2
    return lo if lo is not None else hi


def previous_growth(row: dict):
    for key in ("q1_yoy", "prev_dedt_yoy", "prev_q_yoy", "last_q_yoy"):
        val = fnum(row.get(key))
        if val is not None:
            return val
    return None


def normalize_events(payloads: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for item in payload.get("items") or []:
            if isinstance(item, dict):
                rows.append(dict(item))
        events = payload.get("events") or {}
        if isinstance(events, dict):
            for key, label in (("forecast", "业绩预告"), ("express", "业绩快报"), ("report", "业绩报告")):
                for item in events.get(key) or []:
                    if isinstance(item, dict):
                        row = dict(item)
                        row.setdefault("type", label)
                        rows.append(row)
    return rows


def select_rolling_candidates(rows: list[dict], min_growth: float = 20) -> list[dict]:
    best: dict[str, dict] = {}
    for row in rows:
        code = c6(row.get("code") or row.get("ts_code"))
        idx = str(row.get("idx") or row.get("universe") or "").lower()
        if not code or idx not in UNIVERSE_RANK:
            continue
        cur = latest_growth(row)
        prev = previous_growth(row)
        if cur is None or prev is None or cur <= min_growth or cur <= prev:
            continue
        src = source_type(row)
        item = {
            "code": row.get("ts_code") or row.get("code") or code,
            "c6": code,
            "name": row.get("name") or "",
            "idx": idx,
            "universe": UNIVERSE_LABEL[idx],
            "source": src,
            "source_rank": SOURCE_RANK.get(src, 9),
            "ann_date": norm_date(row.get("ann_date") or row.get("report_date") or row.get("date")),
            "period": row.get("period") or row.get("end_date") or "",
            "dedt_yoy": round(cur, 2),
            "prev_dedt_yoy": round(prev, 2),
            "delta": round(cur - prev, 2),
            "summary": row.get("summary") or row.get("title") or "",
        }
        old = best.get(code)
        if old is None:
            best[code] = item
            continue
        old_key = (old["source_rank"], UNIVERSE_RANK[old["idx"]], -old["dedt_yoy"])
        new_key = (item["source_rank"], UNIVERSE_RANK[item["idx"]], -item["dedt_yoy"])
        if new_key < old_key:
            best[code] = item
    out = list(best.values())
    out.sort(key=lambda x: (UNIVERSE_RANK[x["idx"]], -x["dedt_yoy"], x["c6"]))
    for x in out:
        x.pop("source_rank", None)
    return out


def advisor_codes(advisor: dict) -> dict[str, dict]:
    cur = advisor.get("current") or {}
    trade = advisor.get("trade") or {}
    rows = []
    if isinstance(cur.get("basket"), list):
        rows.extend(cur["basket"])
    if isinstance(trade.get("items"), list):
        rows.extend([x for x in trade["items"] if x.get("action") in ("买入", "持有", "涔板叆", "鎸佹湁")])
    out = {}
    for row in rows:
        code = c6(row.get("code") or row.get("ts_code"))
        if code and code not in out:
            out[code] = {"code": row.get("code") or code, "name": row.get("name") or ""}
    return out


def compare_with_advisor(rolling: list[dict], advisor: dict) -> dict:
    adv = advisor_codes(advisor)
    roll_codes = [c6(x.get("code")) for x in rolling]
    roll_set = set(roll_codes)
    adv_set = set(adv)
    items = []
    for row in rolling:
        code = c6(row.get("code"))
        item = dict(row)
        item["in_advisor_pro"] = code in adv_set
        items.append(item)
    return {
        "items": items,
        "overlap_codes": [c for c in roll_codes if c in adv_set],
        "rolling_only_codes": [c for c in roll_codes if c not in adv_set],
        "advisor_only_codes": sorted(adv_set - roll_set),
        "advisor_only": [adv[c] for c in sorted(adv_set - roll_set)],
    }


def build_report(
    data_dir: Path,
    shared_dir: Path | None = None,
    min_growth: float = 20,
    output_dir: Path | None = None,
) -> dict:
    shared_dir = shared_dir or data_dir
    output_dir = output_dir or shared_dir
    shared_payloads = []
    for path in (shared_dir / "forecast_browse.json", shared_dir / "runup.json"):
        payload = read_json(path)
        if isinstance(payload, dict):
            shared_payloads.append(payload)
    if shared_dir != data_dir and not shared_payloads:
        raise RuntimeError("no readable shared rolling-earnings upstream data")
    payloads = list(shared_payloads)
    for path in (data_dir / "forecast_browse.json", data_dir / "runup.json"):
        payload = read_json(path)
        if isinstance(payload, dict):
            payloads.append(payload)
    rows = normalize_events(payloads)
    if not rows:
        raise RuntimeError("rolling-earnings upstream contains no event rows")
    rolling = select_rolling_candidates(rows, min_growth=min_growth)
    advisor = read_json(shared_dir / "regime_advisor_pro.json") or read_json(data_dir / "regime_advisor_pro.json") or {}
    compared = compare_with_advisor(rolling, advisor)
    coverage = {}
    seen_announced = {k: set() for k in UNIVERSE_RANK}
    for row in rows:
        idx = str(row.get("idx") or row.get("universe") or "").lower()
        code = c6(row.get("code") or row.get("ts_code"))
        if idx in seen_announced and code:
            seen_announced[idx].add(code)
    for idx, label in UNIVERSE_LABEL.items():
        coverage[idx] = {"label": label, "announced": len(seen_announced[idx])}
    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "criteria": {
            "min_dedt_yoy": min_growth,
            "require_acceleration": True,
            "source_priority": ["业绩报告", "业绩快报", "业绩预告", "上一季度"],
            "main_portfolio": "混合口径：已公告用最新公告/报告，未公告继续沿用顾问Pro原篮子作对照。",
        },
        "coverage": coverage,
        "source_health": {
            "shared_payloads": len(shared_payloads),
            "event_rows": len(rows),
        },
        "rolling": compared,
        "n": len(compared["items"]),
    }
    write_json(output_dir / "rolling_earnings.json", payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build rolling earnings rebalance report.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--shared-dir", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--min-growth", type=float, default=20)
    args = parser.parse_args()
    payload = build_report(
        Path(args.data_dir),
        Path(args.shared_dir) if args.shared_dir else None,
        min_growth=args.min_growth,
        output_dir=Path(args.output_dir) if args.output_dir else None,
    )
    print(f"rolling earnings n={payload['n']} updated={payload['updated']}")


if __name__ == "__main__":
    main()
