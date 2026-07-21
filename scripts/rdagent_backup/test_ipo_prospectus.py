import unittest

try:
    from scripts.rdagent_backup.ipo_prospectus import (
        build_business_context,
        parse_prospectus_text,
        validate_identity,
    )
except ImportError:
    def parse_prospectus_text(text):
        return {}

    def validate_identity(facts, ts_code, name):
        return False

    def build_business_context(code, name, tushare_mainbz, prospectus):
        return "", "missing"


class IpoProspectusTest(unittest.TestCase):
    def test_parses_wind_and_solar_business(self):
        facts = parse_prospectus_text(
            "华润新能源控股有限公司 股票代码001248。"
            "公司主营业务包括风力发电和光伏发电，并开展新能源项目开发运营。"
            "募集资金用于风电场和光伏电站项目。"
        )
        self.assertEqual(facts.get("company_name"), "华润新能源控股有限公司")
        self.assertIn("风力发电", facts.get("business", ""))
        self.assertIn("光伏发电", facts.get("business", ""))

    def test_rejects_mismatched_company_identity(self):
        facts = {
            "company_name": "华润新能源控股有限公司",
            "stock_code": "001248",
        }
        self.assertTrue(validate_identity(facts, "001248.SZ", "华润新能源"))
        self.assertFalse(validate_identity(facts, "001399.SZ", "惠科股份"))

    def test_prospectus_business_overrides_empty_tushare_business(self):
        ctx, source = build_business_context(
            code="001248.SZ",
            name="华润新能源",
            tushare_mainbz=[],
            prospectus={
                "business": "主营风力发电和光伏发电",
                "source_url": "https://static.cninfo.com.cn/example.pdf",
            },
        )
        self.assertEqual(source, "prospectus")
        self.assertIn("风力发电", ctx)
        self.assertIn("光伏发电", ctx)


if __name__ == "__main__":
    unittest.main()
