import app as app_module
from scripts.access_policy import csrf_required, required_feature


def test_unlisted_routes_are_internal_by_default() -> None:
    assert required_feature("/new-future-model") == "internal_operations"
    assert required_feature("/api/new-future-model") == "internal_operations"


def test_member_data_routes_are_explicitly_allowed() -> None:
    assert required_feature("/") == "market_data"
    assert required_feature("/screen") == "advanced_data"
    assert required_feature("/api/watchlist/add", "POST") == "market_data"


def test_legacy_mutating_get_requires_csrf() -> None:
    assert csrf_required("/api/rdagent/request", "GET") is True
    assert csrf_required("/api/search", "GET") is False
    assert csrf_required("/api/watchlist/add", "POST") is True


def test_csrf_required_action_routes_do_not_allow_get() -> None:
    violations = sorted(
        rule.rule
        for rule in app_module.app.url_map.iter_rules()
        if "GET" in rule.methods and csrf_required(rule.rule, "GET")
    )
    assert violations == []
