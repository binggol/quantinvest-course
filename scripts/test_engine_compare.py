from scripts.backtest_engine.engine_compare import ComparisonTolerance, compare_results


def result(account=100.0, reason="filled"):
    daily = [{"date": "2026-01-02", "account": account, "net_return": 0.0, "stock_exposure": 0.0, "hedged_return": 0.0}]
    metrics = {
        key: {"total_return": 0.0, "annualized_return": 0.0, "max_drawdown": 0.0, "sharpe": 0.0}
        for key in ("long_only_full", "long_only_2022_plus", "exposure_matched_hedged_full", "exposure_matched_hedged_2022_plus")
    }
    order = {"trade_date": "2026-01-02", "instrument": "SH600000", "side": "buy", "reason": reason, "requested_raw_shares": 100.0, "deal_raw_shares": 100.0, "trade_price": 10.0, "trade_cost": 5.0, "market_rule_source": "explicit"}
    return {"daily_path": daily, "execution_audit": [order], "execution_metrics": metrics, "final_position": {"holding_count": 0}}


def test_comparison_passes_identical_results_and_hash():
    comparison = compare_results(result(), result(), qlib_result_sha256="abc", expected_source_sha256="abc")
    assert comparison["execution_reproduction_passed"]
    assert comparison["publication_gate"]["passed"]


def test_comparison_rejects_one_day_shift_even_if_metrics_match():
    right = result()
    right["daily_path"][0]["date"] = "2026-01-05"
    comparison = compare_results(result(), right)
    assert not comparison["daily"]["passed"]
    assert not comparison["execution_reproduction_passed"]


def test_comparison_rejects_order_reason_and_account_drift():
    right = result(account=102.0, reason="limit_buy")
    comparison = compare_results(result(), right, tolerance=ComparisonTolerance(account=1.0))
    assert not comparison["orders"]["passed"]
    assert not comparison["daily"]["passed"]


def test_board_fallback_blocks_publication_without_invalidating_reproduction():
    left = result()
    right = result()
    left["execution_audit"][0]["market_rule_source"] = "board_fallback"
    right["execution_audit"][0]["market_rule_source"] = "board_fallback"
    comparison = compare_results(left, right)
    assert comparison["execution_reproduction_passed"]
    assert not comparison["publication_gate"]["passed"]
    assert comparison["publication_gate"]["board_fallback_attempts"] == 1
