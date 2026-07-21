import importlib.util
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_growth_report_queue.py"


def load_module():
    spec = importlib.util.spec_from_file_location("build_growth_report_queue", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_weekend_window_starts_from_last_trade_day_close():
    module = load_module()
    start, end, trade_day = module.after_close_window(
        now=datetime(2026, 7, 4, 10, 30),
        trade_days=["2026-07-01", "2026-07-02", "2026-07-03"],
    )
    assert trade_day == "2026-07-03"
    assert start == datetime(2026, 7, 3, 15, 0)
    assert end == datetime(2026, 7, 4, 10, 30)


def test_selects_growth_events_and_orders_by_universe_priority():
    module = load_module()
    events = [
        {"code": "000905.SZ", "name": "C500", "ann_date": "2026-07-04", "type": "业绩预告", "dedt_yoy": 35, "prev_dedt_yoy": 15},
        {"code": "000300.SZ", "name": "C300", "ann_date": "2026-07-04", "type": "业绩快报", "dedt_yoy": 25, "prev_dedt_yoy": 21},
        {"code": "000852.SZ", "name": "C1000", "ann_date": "2026-07-04", "type": "半年度报告", "dedt_yoy": 80, "prev_dedt_yoy": 40},
        {"code": "000001.SZ", "name": "LOW", "ann_date": "2026-07-04", "type": "业绩预告", "dedt_yoy": 19, "prev_dedt_yoy": 5},
        {"code": "000002.SZ", "name": "DOWN", "ann_date": "2026-07-04", "type": "业绩预告", "dedt_yoy": 30, "prev_dedt_yoy": 35},
    ]
    memberships = {
        "csi300": {"000300"},
        "csi500": {"000905"},
        "csi1000": {"000852"},
    }
    out = module.select_growth_events(events, memberships, start_date="2026-07-03", min_growth=20)
    assert [x["code"] for x in out] == ["000300.SZ", "000905.SZ", "000852.SZ"]
    assert [x["universe"] for x in out] == ["沪深300", "中证500", "中证1000"]


def test_after_close_cninfo_time_moves_event_to_next_workday():
    module = load_module()
    events = [
        {"code": "301308.SZ", "name": "江波龙", "idx": "csi1000", "ann_date": "2026-07-03",
         "type": "业绩预告", "dedt_yoy": 55, "prev_dedt_yoy": 20},
    ]
    cninfo_times = {
        ("301308", "2026-07-03"): {
            "dt": datetime(2026, 7, 3, 18, 47, 7),
            "ann_date": "2026-07-03",
            "ann_datetime": "2026-07-03 18:47:07",
            "title": "2026年半年度业绩预告",
            "url": "https://static.cninfo.com.cn/example.pdf",
        }
    }
    out = module.select_growth_events(events, {}, start_date="2026-07-06",
                                      min_growth=20, cninfo_times=cninfo_times)
    assert len(out) == 1
    assert out[0]["ann_date"] == "2026-07-06"
    assert out[0]["raw_ann_date"] == "2026-07-03"
    assert out[0]["cninfo_ann_datetime"] == "2026-07-03 18:47:07"


def test_missing_cninfo_time_keeps_original_announcement_date():
    module = load_module()
    events = [
        {"code": "301165.SZ", "name": "锐捷网络", "idx": "csi1000", "ann_date": "2026-07-03",
         "type": "业绩预告", "dedt_yoy": 35, "prev_dedt_yoy": 15},
    ]
    out = module.select_growth_events(events, {}, start_date="2026-07-03",
                                      min_growth=20, cninfo_times={})
    assert len(out) == 1
    assert out[0]["ann_date"] == "2026-07-03"
    assert out[0]["raw_ann_date"] == "2026-07-03"
    assert out[0]["cninfo_ann_datetime"] == ""
    assert out[0]["cninfo_ann_date_match"] == "missing"


def test_select_growth_events_requires_announcements_after_window_start():
    module = load_module()
    events = [
        {"code": "000001.SZ", "name": "BEFORE", "idx": "csi300", "ann_date": "2026-07-03",
         "type": "业绩预告", "dedt_yoy": 60, "prev_dedt_yoy": 20},
        {"code": "000002.SZ", "name": "AFTER", "idx": "csi300", "ann_date": "2026-07-03",
         "type": "业绩预告", "dedt_yoy": 55, "prev_dedt_yoy": 20},
        {"code": "000003.SZ", "name": "NEXTDAY_NO_TIME", "idx": "csi300", "ann_date": "2026-07-04",
         "type": "业绩预告", "dedt_yoy": 50, "prev_dedt_yoy": 20},
        {"code": "000004.SZ", "name": "SAMEDAY_NO_TIME", "idx": "csi300", "ann_date": "2026-07-03",
         "type": "业绩预告", "dedt_yoy": 45, "prev_dedt_yoy": 20},
    ]
    cninfo_times = {
        ("000001", "2026-07-03"): {
            "dt": datetime(2026, 7, 3, 14, 30),
            "ann_date": "2026-07-03",
            "ann_datetime": "2026-07-03 14:30:00",
        },
        ("000002", "2026-07-03"): {
            "dt": datetime(2026, 7, 3, 18, 30),
            "ann_date": "2026-07-03",
            "ann_datetime": "2026-07-03 18:30:00",
        },
    }
    out = module.select_growth_events(
        events, {}, start_dt=datetime(2026, 7, 3, 15, 0), min_growth=20, cninfo_times=cninfo_times
    )
    assert [x["c6"] for x in out] == ["000002", "000003"]


def test_build_queue_uses_same_job_id_for_queue_and_batch_request(tmp_path):
    module = load_module()
    (tmp_path / "forecast_browse.json").write_text(json.dumps({"items": [{
        "code": "000300.SZ", "name": "TEST", "idx": "csi300",
        "ann_date": "2026-07-11", "type": "业绩预告",
        "dedt_yoy": 35, "prev_dedt_yoy": 20,
    }]}), encoding="utf-8")

    payload = module.build_queue(
        tmp_path, tmp_path, write_batch=True, now=datetime(2026, 7, 12, 16, 23, 7)
    )
    request = json.loads((tmp_path / "batch_gen_request.json").read_text(encoding="utf-8"))

    assert payload["n"] == 1
    assert payload["job_id"].startswith("growth-20260712162307")
    assert request["job_id"] == payload["job_id"]
    assert request["requested_at"] == payload["updated"]
    assert request["source"] == "growth_after_close"


if __name__ == "__main__":
    test_weekend_window_starts_from_last_trade_day_close()
    test_selects_growth_events_and_orders_by_universe_priority()
    test_after_close_cninfo_time_moves_event_to_next_workday()
    test_missing_cninfo_time_keeps_original_announcement_date()
    test_select_growth_events_requires_announcements_after_window_start()
    print("ok")
