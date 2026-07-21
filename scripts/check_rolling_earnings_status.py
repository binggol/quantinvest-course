from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parse_dt(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def load_status(data_dir: Path) -> dict:
    backtest = read_json(data_dir / "rolling_earnings_backtest_top50.json")
    status = read_json(data_dir / "cninfo_earnings_event_backfill_status.json")
    auto = read_json(data_dir / "earnings_event_times_auto.json")
    match = backtest.get("announcement_time_match_counts") or {}

    def param(name: str):
        value = status.get(name)
        return auto.get(name) if value is None else value

    last_run = auto.get("last_run") or ""
    status_updated = status.get("updated") or ""
    status_dt = parse_dt(status_updated)
    auto_dt = parse_dt(last_run)
    if status_dt and auto_dt and status_dt > auto_dt:
        aborted = bool(status.get("aborted"))
    else:
        aborted = bool(status.get("aborted") or auto.get("aborted"))
    next_due_at = ""
    if last_run:
        try:
            wait_hours = 12 if bool(auto.get("aborted")) else 4
            next_due_at = (datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S") + timedelta(hours=wait_hours)).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            next_due_at = ""

    return {
        "data_dir": str(data_dir),
        "same": match.get("same"),
        "nearby": match.get("nearby"),
        "missing": match.get("missing"),
        "updated": status_updated,
        "n_done_tasks": status.get("n_done_tasks"),
        "n_items": status.get("n_items"),
        "aborted": aborted,
        "errors_count": len(status.get("errors") or []),
        "last_run": last_run,
        "next_due_at": next_due_at,
        "added": auto.get("added"),
        "workers": param("workers"),
        "limit": param("limit"),
        "sleep": param("sleep"),
        "max_403": param("max_403"),
        "reason": auto.get("reason") or "",
    }


def format_summary(status: dict) -> str:
    state = "403冷却" if status.get("aborted") else "运行正常"
    params = (
        f"workers={status.get('workers')} "
        f"limit={status.get('limit')} "
        f"sleep={status.get('sleep')} "
        f"max_403={status.get('max_403')}"
    )
    return "\n".join([
        f"滚动业绩巨潮补漏: {state}",
        f"匹配: same={status.get('same')} nearby={status.get('nearby')} missing={status.get('missing')}",
        f"补漏: done_tasks={status.get('n_done_tasks')} items={status.get('n_items')} errors={status.get('errors_count')}",
        f"自动任务: last_run={status.get('last_run') or '-'} 新增={status.get('added')} {params}",
        f"下次自动补漏: {status.get('next_due_at') or '-'}",
        f"原因: {status.get('reason') or '-'}",
        f"数据目录: {status.get('data_dir')}",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description="Show rolling earnings cninfo event-time backfill status.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Directory containing shared JSON outputs.")
    args = parser.parse_args()
    status = load_status(Path(args.data_dir))
    print(format_summary(status))
    return 2 if status.get("aborted") else 0


if __name__ == "__main__":
    raise SystemExit(main())
