from __future__ import annotations

import argparse
import importlib.util
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
EXPORT_SCRIPT = ROOT / "scripts" / "export_earnings_announcement_times.py"


def load_exporter():
    spec = importlib.util.spec_from_file_location("export_earnings_announcement_times", EXPORT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def month_ranges(start: str, end: str) -> list[tuple[str, str]]:
    start_dt = datetime.strptime(start, "%Y-%m-%d").date().replace(day=1)
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()
    months = []
    cur = start_dt
    while cur <= end_dt:
        if cur.month == 12:
            nxt = cur.replace(year=cur.year + 1, month=1, day=1)
        else:
            nxt = cur.replace(month=cur.month + 1, day=1)
        a = cur.strftime("%Y-%m-%d")
        b_date = min(end_dt, nxt.fromordinal(nxt.toordinal() - 1))
        months.append((a, b_date.strftime("%Y-%m-%d")))
        cur = nxt
    return months


def merge_items(exporter, old_items: list[dict], new_items: list[dict]) -> list[dict]:
    return exporter.merge_items(old_items, new_items)


def backfill(data_dir: Path, start: str, end: str, workers: int, max_pages: int,
             sleep_s: float, force: bool = False) -> dict:
    exporter = load_exporter()
    out_path = data_dir / "cninfo_earnings_announcements.json"
    status_path = data_dir / "cninfo_earnings_backfill_status.json"
    existing = read_json(out_path) or {}
    items = existing.get("items") or []
    status = read_json(status_path) or {"done_months": []}
    done = set(status.get("done_months") or [])
    errors = [err for err in (status.get("errors") or []) if err.get("month") not in done]

    ranges = month_ranges(start, end)
    for idx, (a, b) in enumerate(ranges, 1):
        key = a[:7]
        if key in done and not force:
            print(f"[skip] {key} already done", flush=True)
            continue
        try:
            print(f"[{idx}/{len(ranges)}] pull {a}~{b}", flush=True)
            rows = exporter.collect_global(a, b, sleep_s=sleep_s, max_pages=max_pages, workers=workers)
            items = merge_items(exporter, items, rows)
            done.add(key)
            errors = [err for err in errors if err.get("month") != key]
            payload = {
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "items": items,
                "errors": errors,
                "query": {
                    "source": "cninfo",
                    "mode": "monthly_backfill",
                    "start": start,
                    "end": end,
                    "keywords": list(exporter.KEYWORDS),
                    "workers": workers,
                    "max_pages": max_pages,
                },
            }
            write_json(out_path, payload)
            write_json(status_path, {
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "start": start,
                "end": end,
                "done_months": sorted(done),
                "n_done": sum(1 for a, _b in ranges if a[:7] in done),
                "n_months": len(ranges),
                "n_items": len(items),
                "errors": errors,
            })
            print(f"[done] {key} rows={len(rows)} total={len(items)}", flush=True)
        except Exception as exc:
            errors.append({"month": key, "error": str(exc), "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            write_json(status_path, {
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "start": start,
                "end": end,
                "done_months": sorted(done),
                "n_done": sum(1 for a, _b in ranges if a[:7] in done),
                "n_months": len(ranges),
                "n_items": len(items),
                "errors": errors,
            })
            print(f"[error] {key}: {exc}", flush=True)
    write_json(status_path, {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start": start,
        "end": end,
        "done_months": sorted(done),
        "n_done": sum(1 for a, _b in ranges if a[:7] in done),
        "n_months": len(ranges),
        "n_items": len(items),
        "errors": errors,
    })
    return read_json(status_path) or {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Monthly backfill cninfo earnings announcement timestamps.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--sleep", type=float, default=0.03)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    status = backfill(Path(args.data_dir), args.start, args.end, args.workers, args.max_pages, args.sleep, args.force)
    print(json.dumps({
        "n_done": status.get("n_done"),
        "n_months": status.get("n_months"),
        "n_items": status.get("n_items"),
        "errors": len(status.get("errors") or []),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
