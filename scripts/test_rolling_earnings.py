import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_rolling_earnings.py"


def load_module():
    spec = importlib.util.spec_from_file_location("build_rolling_earnings", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selects_accelerating_announced_earnings_and_orders_by_index():
    module = load_module()
    rows = [
        {"code": "000905", "name": "C500", "idx": "csi500", "type": "业绩预告", "ann_date": "20260704", "q2_yoy": 55, "q1_yoy": 25},
        {"code": "000300", "name": "C300", "idx": "csi300", "type": "业绩快报", "ann_date": "20260704", "q2_yoy": 30, "q1_yoy": 21},
        {"code": "000852", "name": "C1000", "idx": "csi1000", "type": "半年度报告", "ann_date": "20260704", "q2_yoy": 200, "q1_yoy": 40},
        {"code": "000001", "name": "LOW", "idx": "csi300", "type": "业绩预告", "ann_date": "20260704", "q2_yoy": 19, "q1_yoy": 1},
        {"code": "000002", "name": "DOWN", "idx": "csi300", "type": "业绩预告", "ann_date": "20260704", "q2_yoy": 50, "q1_yoy": 60},
    ]
    out = module.select_rolling_candidates(rows, min_growth=20)
    assert [x["code"] for x in out] == ["000300", "000905", "000852"]
    assert [x["source"] for x in out] == ["业绩快报", "业绩预告", "半年度报告"]


def test_compares_with_advisor_pro_basket():
    module = load_module()
    rolling = [
        {"code": "000300", "name": "C300"},
        {"code": "000905", "name": "C500"},
    ]
    advisor = {"current": {"basket": [{"code": "000300", "name": "C300"}, {"code": "000001", "name": "A"}]}}
    compared = module.compare_with_advisor(rolling, advisor)
    assert compared["overlap_codes"] == ["000300"]
    assert compared["rolling_only_codes"] == ["000905"]
    assert compared["advisor_only_codes"] == ["000001"]
    assert compared["items"][0]["in_advisor_pro"] is True
    assert compared["items"][1]["in_advisor_pro"] is False


if __name__ == "__main__":
    test_selects_accelerating_announced_earnings_and_orders_by_index()
    test_compares_with_advisor_pro_basket()
    print("ok")
