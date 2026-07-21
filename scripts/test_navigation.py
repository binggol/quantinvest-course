"""Regression tests for the permission-driven shared navigation."""

import unittest
from pathlib import Path

from scripts.access_policy import NAV_GROUPS, PAGE_FEATURES


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "templates"


class NavigationTest(unittest.TestCase):
    def test_routes_are_unique_and_have_features(self):
        routes = [link.path for _, links in NAV_GROUPS for link in links]
        self.assertEqual(len(routes), len(set(routes)))
        self.assertEqual(set(routes), set(PAGE_FEATURES) - {"/readme"})

    def test_member_and_internal_groups_are_separate(self):
        groups = {label: links for label, links in NAV_GROUPS}
        self.assertTrue(all(link.feature == "market_data" or link.feature == "member_workspace"
                            for link in groups["数据服务"]))
        self.assertTrue(all(link.feature == "advanced_data" for link in groups["高级数据"]))
        self.assertTrue(all(link.feature == "internal_operations" for link in groups["内部持仓"]))

    def test_templates_use_shared_navigation(self):
        legacy = []
        includes = 0
        for path in TEMPLATES.glob("*.html"):
            if path.name == "_nav.html":
                continue
            html = path.read_text(encoding="utf-8-sig")
            if '<nav class="topnav"' in html:
                legacy.append(path.name)
            if '{% include "_nav.html" %}' in html:
                includes += 1
        self.assertEqual(legacy, [])
        self.assertGreaterEqual(includes, 70)

    def test_shared_navigation_marks_current_page(self):
        nav = (TEMPLATES / "_nav.html").read_text(encoding="utf-8")
        self.assertIn("request.path == link.path", nav)
        self.assertIn('aria-current="page"', nav)
        self.assertIn('<details class="nav-group"', nav)
        self.assertIn('group.links | map(attribute=\'path\') | list', nav)

    def test_no_page_keeps_a_legacy_navigation_definition(self):
        html = (TEMPLATES / "forecast.html").read_text(encoding="utf-8-sig")
        self.assertNotIn("const NAVG", html)
        self.assertNotIn('id="nav"', html)

    def test_member_admin_page_uses_shared_navigation(self):
        html = (TEMPLATES / "admin_members.html").read_text(encoding="utf-8")
        self.assertIn('{% include "_nav.html" %}', html)


if __name__ == "__main__":
    unittest.main()
