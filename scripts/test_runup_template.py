from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app


def test_runup_page_has_intraday_and_latest_announcement_controls():
    client = app.test_client()

    response = client.get("/runup")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "showIntraday(" in html
    assert "renderLatestAnnouncements" in html
    assert "latest-date-filter" in html
    assert "latest-type-filter" in html
    assert "latest-announcements" in html
    assert "data-ann-date" in html
    assert "showIntraday(this.dataset.code,this.dataset.name,this.dataset.annDate)" in html
    assert "&date=${encodeURIComponent(annDate||\"\")}" in html


if __name__ == "__main__":
    test_runup_page_has_intraday_and_latest_announcement_controls()
    print("runup template tests ok")
