from pathlib import Path

from scripts.member_data import MemberDataStore


def test_documents_are_isolated_by_member(tmp_path: Path) -> None:
    store = MemberDataStore(tmp_path / "members.db")
    store.put(1, "watchlist", ["600519.SH"])
    store.put(2, "watchlist", ["000001.SZ"])

    assert store.get(1, "watchlist") == ["600519.SH"]
    assert store.get(2, "watchlist") == ["000001.SZ"]


def test_update_is_scoped_and_persistent(tmp_path: Path) -> None:
    store = MemberDataStore(tmp_path / "members.db")

    result = store.update(7, "watchlist", lambda rows: [*(rows or []), "600000.SH"], default=[])

    assert result == ["600000.SH"]
    assert store.get(7, "watchlist") == ["600000.SH"]


def test_put_many_commits_related_documents(tmp_path: Path) -> None:
    store = MemberDataStore(tmp_path / "members.db")
    store.put_many(
        3,
        {
            ("positions", "default"): [{"code": "600519.SH", "qty": 100}],
            ("sells", "default"): [{"code": "000001.SZ", "qty": 20}],
        },
    )

    assert store.get(3, "positions")[0]["qty"] == 100
    assert store.get(3, "sells")[0]["qty"] == 20
