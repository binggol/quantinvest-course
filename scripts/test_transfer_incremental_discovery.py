import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_transfer_events.py"


def load_module():
    spec = importlib.util.spec_from_file_location("export_transfer_events", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discover_incremental_codes_uses_recent_announcements_only():
    module = load_module()
    calls = []

    def fake_query(searchkey, start, end, page=1, column="szse"):
        calls.append((searchkey, start, end, page, column))
        if searchkey == module.KEYWORDS[1] and column == "szse" and page == 1:
            return {
                "totalAnnouncement": 1,
                "announcements": [{
                    "secCode": "301308",
                    "announcementTitle": module.KEYWORDS[1] + "结果报告书",
                    "announcementTime": 1769126400000,
                    "announcementId": "x1",
                    "adjunctUrl": "finalpage/x1.PDF",
                }],
            }
        return {"totalAnnouncement": 0, "announcements": []}

    module.cninfo_fulltext_query = fake_query
    codes, rows = module.discover_incremental_codes("2026-01-01", "2026-01-31", 0, max_pages=2)

    assert codes == ["301308"]
    assert len(rows) == 1
    assert rows[0]["code"] == "301308"
    assert rows[0]["title"].endswith("结果报告书")
    assert all(call[3] == 1 for call in calls)


def test_ann_codes_parses_lists_and_strings():
    module = load_module()
    row = {
        "secCode": "",
        "secCodeList": [{"secCode": "301308"}, "688326.SH", "abc600519xyz"],
    }
    assert module.ann_codes(row) == ["301308", "600519", "688326"]


if __name__ == "__main__":
    test_discover_incremental_codes_uses_recent_announcements_only()
    test_ann_codes_parses_lists_and_strings()
    print("ok")
