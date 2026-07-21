"""Tests for enriching individual-report requests with IPO identity."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class ReportRequestTest(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_ipo_report_request_includes_canonical_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ipo_path = root / "ipo.json"
            request_path = root / "report_request.json"
            ipo_path.write_text(json.dumps({
                "just_listed": [{
                    "code": "001248.SZ",
                    "name": "华润新能源",
                }],
            }, ensure_ascii=False), encoding="utf-8")

            with patch.object(app, "IPO_JSON", ipo_path), \
                 patch.object(app, "REPORT_REQUEST", request_path):
                response = self.client.post(
                    "/api/report/request", json={"code": "001248.SZ"})

            written = json.loads(request_path.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(written["code"], "001248.SZ")
        self.assertEqual(written["c6"], "001248")
        self.assertEqual(written.get("ts_code"), "001248.SZ")
        self.assertEqual(written.get("name"), "华润新能源")
        self.assertTrue(written.get("is_ipo"))


if __name__ == "__main__":
    unittest.main()
