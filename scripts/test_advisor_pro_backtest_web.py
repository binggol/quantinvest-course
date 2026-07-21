from __future__ import annotations

import json
from pathlib import Path

import app as app_module
from scripts.access_policy import required_feature


def _summary(annualized: float = 0.12) -> dict:
    metrics = {
        "n": 726,
        "annualized_return": annualized,
        "sharpe": 0.78,
        "max_drawdown": -0.16,
        "annual_returns": {"2022": 0.01},
    }
    return {
        "generated_at": "2026-07-13T09:30:00+08:00",
        "source": "C:/private/detailed.json",
        "portfolio_spec": {
            "portfolio_topn": 8,
            "frequency_days": 15,
            "frequency_step": 3,
            "max_replacements": 2,
            "rebalance_mode": "replace_only",
            "account": 33333333.33,
        },
        "execution_parameters": {
            "commission": 0.0003,
            "max_volume_participation": 0.1,
            "impact_cost": 0.1,
            "risk_degree": 0.95,
            "retry_days": 5,
            "private_setting": "hidden",
        },
        "offsets": {"count": 3},
        "staggered": {
            "long_only": {
                "full": metrics,
                "development_2017_2021": metrics,
                "validation_2022_2024": metrics,
                "recent_2025_plus": metrics,
            }
        },
        "double_cost": {
            "staggered": {
                "long_only": {
                    "full": metrics,
                    "development_2017_2021": metrics,
                    "validation_2022_2024": {**metrics, "annualized_return": 0.11},
                    "recent_2025_plus": metrics,
                }
            }
        },
        "turnover": {"median": 3.97},
        "execution_quality": {
            "available": True,
            "all_liquidated": True,
            "aggregate": {
                "attempts": 100,
                "trades": 90,
                "no_fill_count": 10,
                "no_fill_rate": 0.10,
                "partial_count": 5,
                "partial_rate": 0.05,
                "incomplete_count": 15,
                "incomplete_rate": 0.15,
            },
        },
        "daily_path": [{"date": "2024-01-01", "net_return": 0.01}],
    }


def _configure_preview(monkeypatch) -> None:
    monkeypatch.setenv("QI_AUTH_ENABLED", "0")
    monkeypatch.setenv("QI_DEV_ROLE", "admin")
    monkeypatch.setenv("QI_DEV_PLAN", "enterprise")
    app_module.app.config.update(TESTING=True)


def test_backtest_page_and_api_are_internal() -> None:
    assert required_feature("/advisor-pro/backtest") == "internal_operations"
    assert required_feature("/api/advisor-pro/backtest/summary") == "internal_operations"
    assert required_feature("/api/advisor-pro/backtest/chart/overview") == "internal_operations"


def test_summary_api_returns_only_safe_fields(tmp_path: Path, monkeypatch) -> None:
    _configure_preview(monkeypatch)
    paths = {}
    for key in ("standard", "stress", "top20"):
        path = tmp_path / f"{key}.json"
        path.write_text(json.dumps(_summary(), ensure_ascii=False), encoding="utf-8")
        paths[key] = path
    monkeypatch.setattr(app_module, "ADVISOR_PRO_BACKTEST_FILES", paths)

    response = app_module.app.test_client().get("/api/advisor-pro/backtest/summary")
    assert response.status_code == 200
    data = response.get_json()
    assert data["scenarios"][0]["periods"]["validation_2022_2024"]["annualized_return"] == 0.12
    assert data["scenarios"][0]["execution_quality"]["incomplete_fill_rate"] == 0.15
    serialized = json.dumps(data, ensure_ascii=False)
    assert "source" not in serialized
    assert "daily_path" not in serialized
    assert "private_setting" not in serialized
    assert "C:/private" not in serialized


def test_chart_api_is_allowlisted_and_protected_by_route(tmp_path: Path, monkeypatch) -> None:
    _configure_preview(monkeypatch)
    (tmp_path / "advisor_pro_backtest_overview.png").write_bytes(b"\x89PNG\r\n\x1a\nchart")
    monkeypatch.setattr(app_module, "ADVISOR_PRO_BACKTEST_CHART_DIR", tmp_path)
    client = app_module.app.test_client()

    response = client.get("/api/advisor-pro/backtest/chart/overview")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert client.get("/api/advisor-pro/backtest/chart/private").status_code == 404
    assert client.get("/api/advisor-pro/backtest/chart/capacity").status_code == 404


def test_backtest_template_has_complete_loading_surfaces(monkeypatch) -> None:
    _configure_preview(monkeypatch)
    response = app_module.app.test_client().get("/advisor-pro/backtest")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "顾问 Pro · 组合参数回测" in html
    assert "/api/advisor-pro/backtest/summary" in html
    assert "/api/advisor-pro/backtest/chart/${name}" in html
    assert "scenario-rows" in html
    assert "execution-rows" in html
    assert '{% include "_nav.html" %}' not in html
