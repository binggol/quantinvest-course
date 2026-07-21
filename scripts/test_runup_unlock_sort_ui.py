from __future__ import annotations

from pathlib import Path


def test_unlock_focus_table_exposes_sort_keys_for_all_columns():
    html = Path("templates/runup.html").read_text(encoding="utf-8")
    for key in (
        "focus_window",
        "code",
        "name",
        "kind",
        "unlock_date",
        "days_to_unlock",
        "ann_date",
        "title",
        "dataset",
    ):
        assert f'["{key}",' in html
    assert 'data-sort="${c[0]}"' in html


if __name__ == "__main__":
    test_unlock_focus_table_exposes_sort_keys_for_all_columns()
    print("ok")
