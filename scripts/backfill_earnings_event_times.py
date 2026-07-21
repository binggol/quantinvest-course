from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from threading import Event

import pandas as pd

try:
    from scripts.process_lock import ProcessLockBusy, process_lock
except ImportError:  # direct script execution
    from process_lock import ProcessLockBusy, process_lock


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path(os.environ.get("PREDICT_DATA_DIR") or (ROOT / "data"))
DEFAULT_DB = Path(r"\/app/data\financials.db")
EXPORT_SCRIPT = ROOT / "scripts" / "export_earnings_announcement_times.py"


def c6(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:6]


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
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def month_bounds(yyyymmdd: str) -> tuple[str, str]:
    dt = datetime.strptime(yyyymmdd, "%Y%m%d").date()
    start = dt.replace(day=1)
    if start.month == 12:
        nxt = start.replace(year=start.year + 1, month=1, day=1)
    else:
        nxt = start.replace(month=start.month + 1, day=1)
    end = nxt.fromordinal(nxt.toordinal() - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def load_financial_event_keys(db_path: Path, start: str, end: str, min_growth: float) -> set[tuple[str, str]]:
    conn = sqlite3.connect(str(db_path))
    try:
        fin = pd.read_sql(
            "select ts_code, ann_date, end_date, q_dtprofit from fina_indicators "
            "where ann_date is not null and end_date is not null and q_dtprofit is not null",
            conn,
        )
    finally:
        conn.close()
    fin["ann_date"] = fin["ann_date"].astype(str)
    fin["end_date"] = fin["end_date"].astype(str)
    fin = fin.drop_duplicates(["ts_code", "end_date"]).sort_values(["ts_code", "end_date"])
    q = {(r.ts_code, r.end_date): r.q_dtprofit for r in fin.itertuples(index=False)}

    def yoy(row):
        base_end = f"{int(str(row.end_date)[:4]) - 1}{str(row.end_date)[4:]}"
        base = q.get((row.ts_code, base_end))
        if base is None or base <= 0 or pd.isna(base):
            return None
        return (row.q_dtprofit / base - 1.0) * 100.0

    fin["dedt_yoy"] = [yoy(r) for r in fin.itertuples(index=False)]
    fin["prev_dedt_yoy"] = fin.groupby("ts_code")["dedt_yoy"].shift(1)
    fin = fin.dropna(subset=["dedt_yoy", "prev_dedt_yoy"])
    fin = fin[(fin["dedt_yoy"] > min_growth) & (fin["dedt_yoy"] > fin["prev_dedt_yoy"])]
    fin = fin[
        (fin["ann_date"] >= start.replace("-", ""))
        & (fin["ann_date"] <= end.replace("-", ""))
    ]
    return {(c6(r.ts_code), str(r.ann_date)[:8]) for r in fin.itertuples(index=False) if c6(r.ts_code)}


def existing_keys(payload: dict) -> set[tuple[str, str]]:
    out = set()
    for row in payload.get("items") or []:
        code = c6(row.get("code") or row.get("symbol") or "")
        ann = str(row.get("ann_date") or row.get("date") or "")[:10].replace("-", "")
        if code and len(ann) == 8:
            out.add((code, ann))
    return out


def load_stock_names(db_path: Path) -> dict[str, str]:
    meta_path = db_path.with_name("stock_meta.db")
    if not meta_path.exists():
        return {}
    conn = sqlite3.connect(str(meta_path))
    try:
        rows = pd.read_sql("select ts_code, code, name from stock_meta", conn)
    finally:
        conn.close()
    out: dict[str, str] = {}
    for row in rows.itertuples(index=False):
        name = str(getattr(row, "name", "") or "").strip()
        if not name:
            continue
        for raw in (getattr(row, "ts_code", ""), getattr(row, "code", "")):
            code = c6(raw)
            if code:
                out.setdefault(code, name)
    return out


def backfill(data_dir: Path, db_path: Path, start: str, end: str, min_growth: float,
             workers: int, max_pages: int, sleep_s: float, limit: int = 0, max_403: int = 20,
             retry_done: bool = False) -> dict:
    exporter = load_exporter()
    out_path = data_dir / "cninfo_earnings_announcements.json"
    status_path = data_dir / "cninfo_earnings_event_backfill_status.json"
    payload = read_json(out_path) or {"items": []}
    items = payload.get("items") or []
    have = existing_keys(payload)
    event_keys = load_financial_event_keys(db_path, start, end, min_growth)
    missing = sorted(event_keys - have, key=lambda x: (x[1], x[0]))
    tasks = sorted({(code, ann[:6]) for code, ann in missing})
    names = load_stock_names(db_path)
    status = read_json(status_path) or {"done_tasks": []}
    done = {tuple(x) for x in status.get("done_tasks") or [] if isinstance(x, list) and len(x) == 2}
    if not retry_done:
        tasks = [task for task in tasks if task not in done]
    if limit > 0:
        tasks = tasks[:limit]
    errors = []

    stop = Event()

    def run_task(task: tuple[str, str]) -> tuple[tuple[str, str], list[dict], str, bool]:
        code, ym = task
        a, b = month_bounds(f"{ym}01")
        # A task may have been submitted before another worker tripped the 403
        # circuit breaker.  Re-check immediately before the network-producing
        # exporter call so queued work can retire without starting a request.
        if stop.is_set():
            return task, [], "", False
        try:
            return task, exporter.collect_for_code(
                code, a, b, sleep_s=sleep_s, max_pages=max_pages, name=names.get(code, "")), "", True
        except Exception as exc:
            return task, [], str(exc), True

    total = len(tasks)
    aborted = False
    completed = 0
    checkpoint_every = 1 if workers <= 1 else 20
    with ThreadPoolExecutor(max_workers=workers) as pool:
        next_task = 0
        in_flight = set()

        def submit_available() -> None:
            nonlocal next_task
            while not stop.is_set() and next_task < total and len(in_flight) < workers:
                in_flight.add(pool.submit(run_task, tasks[next_task]))
                next_task += 1

        submit_available()
        while in_flight:
            finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for fut in finished:
                in_flight.discard(fut)
                task, rows, err, started = fut.result()
                if not started:
                    continue
                completed += 1
                if err:
                    errors.append({"task": list(task), "error": err})
                else:
                    done.add(task)
                    items = exporter.merge_items(items, rows)
                if (
                    not aborted
                    and max_403 > 0
                    and sum(1 for e in errors if "403" in str(e.get("error") or "")) >= max_403
                ):
                    aborted = True
                    stop.set()
                    print(f"[event-backfill] stop: hit {max_403} cninfo 403 errors, wait before retry", flush=True)

            if completed and (
                completed % checkpoint_every == 0 or completed == total or aborted
            ):
                write_json(out_path, {
                    "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "items": items,
                    "errors": payload.get("errors") or [],
                    "query": {**(payload.get("query") or {}), "event_backfill": True},
                })
                write_json(status_path, {
                    "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "start": start,
                    "end": end,
                    "min_growth": min_growth,
                    "n_event_keys": len(event_keys),
                    "n_missing_keys_start": len(missing),
                    "n_tasks_remaining_start": total,
                    "n_done_tasks": len(done),
                    "done_tasks": [list(x) for x in sorted(done)],
                    "n_items": len(items),
                    "workers": workers,
                    "limit": limit,
                    "sleep": sleep_s,
                    "max_403": max_403,
                    "retry_done": retry_done,
                    "errors": errors,
                    "aborted": aborted,
                })
                print(f"[event-backfill] {completed}/{total} items={len(items)} errors={len(errors)}", flush=True)

            if aborted:
                # Futures that are already running are allowed to finish so their
                # completed rows remain checkpointed.  Futures that have not
                # started are cancelled; workers that race with cancellation see
                # the stop flag immediately before collect_for_code.
                for pending in list(in_flight):
                    if pending.cancel():
                        in_flight.discard(pending)
            else:
                submit_available()
    write_json(out_path, {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
        "errors": payload.get("errors") or [],
        "query": {**(payload.get("query") or {}), "event_backfill": True},
    })
    write_json(status_path, {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "start": start,
        "end": end,
        "min_growth": min_growth,
        "n_event_keys": len(event_keys),
        "n_missing_keys_start": len(missing),
        "n_tasks_remaining_start": total,
        "n_done_tasks": len(done),
        "done_tasks": [list(x) for x in sorted(done)],
        "n_items": len(items),
        "workers": workers,
        "limit": limit,
        "sleep": sleep_s,
        "max_403": max_403,
        "retry_done": retry_done,
        "errors": errors,
        "aborted": aborted,
    })
    return read_json(status_path) or {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Targeted cninfo announcement-time backfill for rolling earnings events.")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--min-growth", type=float, default=20.0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--max-pages", type=int, default=6)
    ap.add_argument("--sleep", type=float, default=0.03)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-403", type=int, default=20)
    ap.add_argument("--retry-done", action="store_true",
                    help="Retry tasks that are marked done but still have missing event dates.")
    ap.add_argument("--lock-file", default="")
    ap.add_argument("--lock-wait-seconds", type=float, default=0)
    args = ap.parse_args()
    data_dir = Path(args.data_dir)
    lock_path = Path(args.lock_file) if args.lock_file else data_dir / "cninfo_earnings_announcements.lock"
    try:
        with process_lock(
            lock_path,
            wait_seconds=args.lock_wait_seconds,
            reason="earnings-event-time-backfill",
        ):
            status = backfill(data_dir, Path(args.db), args.start, args.end, args.min_growth,
                              args.workers, args.max_pages, args.sleep, args.limit, args.max_403, args.retry_done)
    except ProcessLockBusy as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 75
    print(json.dumps({
        "n_done_tasks": status.get("n_done_tasks"),
        "n_items": status.get("n_items"),
        "errors": len(status.get("errors") or []),
    }, ensure_ascii=False))
    if status.get("errors") or status.get("aborted"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
