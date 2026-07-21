from __future__ import annotations

import argparse
import json
from datetime import datetime, time, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
UNIVERSE_LABEL = {"csi300": "沪深300", "csi500": "中证500", "csi1000": "中证1000"}
UNIVERSE_RANK = {"csi300": 0, "csi500": 1, "csi1000": 2}
EVENT_TYPES = ("业绩预告", "业绩快报", "业绩报告", "季度报告", "半年度报告", "年度报告")


def c6(code: str) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())[:6]


def norm_date(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text[:10].replace("/", "-")


def parse_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_cninfo_times(data_dir: Path, shared_dir: Path) -> dict[tuple[str, str], dict]:
    payload = None
    for path in (shared_dir / "cninfo_earnings_announcements.json", data_dir / "cninfo_earnings_announcements.json"):
        payload = read_json(path)
        if payload:
            break
    out: dict[tuple[str, str], dict] = {}
    for row in (payload or {}).get("items") or []:
        code = c6(row.get("code") or row.get("symbol"))
        ann = norm_date(row.get("ann_date") or row.get("date"))
        if not code or not ann:
            continue
        dt = None
        raw = str(row.get("ann_datetime") or "").strip()
        if raw:
            try:
                dt = datetime.fromisoformat(raw.replace("/", "-"))
            except Exception:
                dt = None
        old = out.get((code, ann))
        if old is None or (dt is not None and (old.get("dt") is None or dt < old.get("dt"))):
            out[(code, ann)] = {
                "dt": dt,
                "ann_date": ann,
                "ann_datetime": raw,
                "title": row.get("title") or "",
                "url": row.get("url") or "",
            }
    return out


def next_workday(day: str) -> str:
    try:
        cur = datetime.strptime(day, "%Y-%m-%d").date() + timedelta(days=1)
        while cur.weekday() >= 5:
            cur += timedelta(days=1)
        return cur.strftime("%Y-%m-%d")
    except Exception:
        return day


def corrected_announcement(row: dict, cninfo_times: dict[tuple[str, str], dict]) -> dict:
    code = c6(row.get("code") or row.get("ts_code"))
    raw_ann = event_date(row)
    info = cninfo_times.get((code, raw_ann))
    if not info:
        return {
            "raw_ann_date": raw_ann,
            "effective_ann_date": raw_ann,
            "cninfo_ann_datetime": "",
            "cninfo_ann_date_match": "missing",
            "cninfo_title": "",
            "cninfo_url": "",
        }
    dt = info.get("dt")
    effective = raw_ann
    if dt is not None:
        effective = dt.strftime("%Y-%m-%d") if dt.time() <= time(15, 0) else next_workday(dt.strftime("%Y-%m-%d"))
    return {
        "raw_ann_date": raw_ann,
        "effective_ann_date": effective,
        "cninfo_ann_datetime": info.get("ann_datetime") or "",
        "cninfo_ann_date_match": "same" if info.get("ann_date") == raw_ann else "nearby",
        "cninfo_title": info.get("title") or "",
        "cninfo_url": info.get("url") or "",
    }


def load_trade_days(data_dir: Path) -> list[str]:
    candidates = [
        data_dir / "trade_calendar.json",
        data_dir / "calendar.json",
        data_dir / "runup.json",
    ]
    days: set[str] = set()
    for path in candidates:
        payload = read_json(path)
        if isinstance(payload, list):
            for item in payload:
                d = norm_date(item.get("cal_date") if isinstance(item, dict) else item)
                if d:
                    days.add(d)
        elif isinstance(payload, dict):
            for key in ("trade_days", "calendar", "dates"):
                seq = payload.get(key)
                if isinstance(seq, list):
                    for item in seq:
                        d = norm_date(item.get("cal_date") if isinstance(item, dict) else item)
                        if d:
                            days.add(d)
    return sorted(days)


def fallback_trade_days(now: datetime) -> list[str]:
    days = []
    cur = now.date() - timedelta(days=20)
    while cur <= now.date():
        if cur.weekday() < 5:
            days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return days


def after_close_window(now: datetime | None = None, trade_days: list[str] | None = None):
    now = now or datetime.now()
    trade_days = sorted(trade_days or fallback_trade_days(now))
    today = now.strftime("%Y-%m-%d")
    eligible = [d for d in trade_days if d <= today]
    trade_day = eligible[-1] if eligible else today
    close_dt = datetime.combine(datetime.strptime(trade_day, "%Y-%m-%d").date(), time(15, 0))
    if now < close_dt and len(eligible) >= 2:
        trade_day = eligible[-2]
        close_dt = datetime.combine(datetime.strptime(trade_day, "%Y-%m-%d").date(), time(15, 0))
    return close_dt, now, trade_day


def event_date(row: dict) -> str:
    return norm_date(row.get("ann_date") or row.get("report_date") or row.get("date") or row.get("pub_date"))


def event_kind(row: dict) -> str:
    text = " ".join(str(row.get(k) or "") for k in ("kind", "source", "type", "title", "summary"))
    for kind in EVENT_TYPES:
        if kind in text:
            return kind
    return str(row.get("type") or "业绩预告")


def latest_growth(row: dict):
    for key in ("q2_yoy", "q4_yoy", "q3_yoy", "q1_yoy", "dedt_q_yoy", "dedt_yoy", "dedt_h1_yoy"):
        val = parse_float(row.get(key))
        if val is not None:
            return val
    lo = parse_float(row.get("dedt_yoy_min") or row.get("dedt_lo_yoy") or row.get("p_chg_min"))
    hi = parse_float(row.get("dedt_yoy_max") or row.get("dedt_hi_yoy") or row.get("p_chg_max"))
    if lo is not None and hi is not None:
        return (lo + hi) / 2
    return lo if lo is not None else hi


def previous_growth(row: dict):
    for key in ("q1_yoy", "prev_dedt_yoy", "prev_q_yoy", "last_q_yoy"):
        val = parse_float(row.get(key))
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
            for key in ("forecast", "express", "report"):
                for item in events.get(key) or []:
                    if isinstance(item, dict):
                        row = dict(item)
                        row.setdefault("type", {"forecast": "业绩预告", "express": "业绩快报", "report": "业绩报告"}[key])
                        rows.append(row)
    return rows


def row_universe(row: dict, memberships: dict[str, set[str]]) -> str:
    idx = str(row.get("idx") or row.get("universe") or "").lower()
    if idx in UNIVERSE_RANK:
        return idx
    code = c6(row.get("code") or row.get("ts_code"))
    for key in ("csi300", "csi500", "csi1000"):
        if code in memberships.get(key, set()):
            return key
    return "other"


def announcement_after_window(ann_meta: dict, start_dt: datetime | None) -> bool:
    if start_dt is None:
        return True
    raw_dt = str(ann_meta.get("cninfo_ann_datetime") or "").strip()
    if raw_dt:
        try:
            return datetime.fromisoformat(raw_dt.replace("/", "-")) > start_dt
        except Exception:
            return False
    ann = ann_meta.get("effective_ann_date") or ann_meta.get("raw_ann_date") or ""
    return bool(ann and ann > start_dt.strftime("%Y-%m-%d"))


def select_growth_events(events: list[dict], memberships: dict[str, set[str]] | None = None,
                         start_date: str = "", min_growth: float = 20,
                         cninfo_times: dict[tuple[str, str], dict] | None = None,
                         start_dt: datetime | None = None) -> list[dict]:
    memberships = memberships or {}
    cninfo_times = cninfo_times or {}
    selected: dict[str, dict] = {}
    for row in events:
        code = c6(row.get("code") or row.get("ts_code"))
        if not code:
            continue
        ann_meta = corrected_announcement(row, cninfo_times)
        ann = ann_meta["effective_ann_date"]
        if start_dt is not None and not announcement_after_window(ann_meta, start_dt):
            continue
        if start_date and ann and ann < start_date:
            continue
        uni = row_universe(row, memberships)
        if uni not in UNIVERSE_RANK:
            continue
        cur = latest_growth(row)
        prev = previous_growth(row)
        if cur is None or prev is None:
            continue
        if cur <= min_growth or cur <= prev:
            continue
        item = {
            "code": row.get("ts_code") or row.get("code") or code,
            "c6": code,
            "name": row.get("name") or "",
            "universe": UNIVERSE_LABEL[uni],
            "universe_key": uni,
            "ann_date": ann,
            "raw_ann_date": ann_meta["raw_ann_date"],
            "cninfo_ann_datetime": ann_meta["cninfo_ann_datetime"],
            "cninfo_ann_date_match": ann_meta["cninfo_ann_date_match"],
            "cninfo_title": ann_meta["cninfo_title"],
            "cninfo_url": ann_meta["cninfo_url"],
            "period": row.get("period") or row.get("end_date") or "",
            "type": event_kind(row),
            "dedt_yoy": round(cur, 2),
            "prev_dedt_yoy": round(prev, 2),
            "delta": round(cur - prev, 2),
            "summary": row.get("summary") or row.get("title") or "",
        }
        old = selected.get(code)
        if old is None or (UNIVERSE_RANK[uni], -item["dedt_yoy"]) < (UNIVERSE_RANK[old["universe_key"]], -old["dedt_yoy"]):
            selected[code] = item
    out = list(selected.values())
    out.sort(key=lambda x: (UNIVERSE_RANK[x["universe_key"]], -x["dedt_yoy"], x["c6"]))
    return out


def build_queue(data_dir: Path, shared_dir: Path | None = None, write_batch: bool = False,
                now: datetime | None = None, min_growth: float = 20,
                output_dir: Path | None = None) -> dict:
    shared_dir = shared_dir or data_dir
    trade_days = load_trade_days(shared_dir) or load_trade_days(data_dir)
    start_dt, end_dt, trade_day = after_close_window(now=now, trade_days=trade_days or None)
    payloads = []
    for path in (shared_dir / "forecast_browse.json", shared_dir / "runup.json", data_dir / "forecast_browse.json", data_dir / "runup.json"):
        payload = read_json(path)
        if payload:
            payloads.append(payload)
    events = normalize_events(payloads)
    cninfo_times = load_cninfo_times(data_dir, shared_dir)
    selected = select_growth_events(events, {}, start_date=start_dt.strftime("%Y-%m-%d"), start_dt=start_dt,
                                    min_growth=min_growth, cninfo_times=cninfo_times)
    created_at = now or datetime.now()
    requested_at = created_at.strftime("%Y-%m-%d %H:%M:%S")
    job_id = "growth-" + created_at.strftime("%Y%m%d%H%M%S%f")
    payload = {
        "updated": requested_at,
        "job_id": job_id,
        "window": {
            "trade_day": trade_day,
            "start": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "end": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "rule": "优先使用巨潮发布时间修正公告日：15:00后/周末发布归入下一交易日；时间缺失则回退原公告日。",
            "cninfo_time_items": len(cninfo_times),
        },
        "criteria": {
            "min_dedt_yoy": min_growth,
            "require_acceleration": True,
            "order": ["沪深300", "中证500", "中证1000"],
        },
        "items": selected,
        "codes": [x["code"] for x in selected],
        "n": len(selected),
    }
    publish_dir = output_dir or shared_dir
    out_path = publish_dir / "growth_report_queue.json"
    write_json(out_path, payload)
    if write_batch and selected:
        write_json(publish_dir / "batch_gen_request.json", {
            "codes": payload["codes"],
            "report": True,
            "forecast": False,
            "source": "growth_after_close",
            "job_id": job_id,
            "requested_at": requested_at,
        })
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Build growth-after-close report queue.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--shared-dir", default="")
    parser.add_argument("--write-batch-request", action="store_true")
    parser.add_argument("--min-growth", type=float, default=20)
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()
    payload = build_queue(
        Path(args.data_dir),
        Path(args.shared_dir) if args.shared_dir else None,
        write_batch=args.write_batch_request,
        min_growth=args.min_growth,
        output_dir=Path(args.output_dir) if args.output_dir else None,
    )
    print(f"growth report queue n={payload['n']} window={payload['window']['start']}~{payload['window']['end']}")


if __name__ == "__main__":
    main()
