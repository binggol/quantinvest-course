"""Pure helpers for auditable cross-market sector signals."""

from datetime import datetime, timedelta


REQUIRED_RESULT_KEYS = {
    "schema_version",
    "generated_at",
    "decision_at",
    "sector",
    "mode",
    "data_health",
    "leaders",
    "upside",
    "downside",
    "holdings",
    "gate",
    "charts",
}


def visible_korea_points(points, decision_at, delay_minutes=15):
    """Return only Korean points visible at the A-share decision time."""
    cutoff = (decision_at - timedelta(minutes=delay_minutes)).strftime("%H:%M")
    return [
        point
        for point in points
        if str(point.get("market_time", "")) <= cutoff
    ]


def compare_sources(primary, backup, tolerance_pct=0.5):
    """Compare two positive prices and report whether they agree."""
    primary = float(primary)
    backup = float(backup)
    if primary <= 0 or backup <= 0:
        return {"ok": False, "difference_pct": None, "reason": "invalid_price"}
    difference = abs(backup - primary) / primary * 100
    return {
        "ok": difference <= tolerance_pct,
        "difference_pct": round(difference, 4),
        "reason": "" if difference <= tolerance_pct else "source_conflict",
    }


def classify_data_health(decision_at, actual_market_at, max_age_minutes=20):
    """Classify a timestamp using timezone-aware ISO-8601 values."""
    decision = datetime.fromisoformat(decision_at)
    actual = datetime.fromisoformat(actual_market_at)
    age = (decision - actual).total_seconds() / 60
    status = "ok" if 0 <= age <= max_age_minutes else "stale"
    return {"status": status, "age_minutes": round(age, 1)}


def validate_result(result):
    """Validate the stable JSON contract consumed by Flask and the browser."""
    missing = sorted(REQUIRED_RESULT_KEYS - set(result))
    if missing:
        raise ValueError("missing result keys: " + ", ".join(missing))
    if result["mode"] not in {"research", "live"}:
        raise ValueError("mode must be research or live")
    return result


def score_stock(row, us_score, korea_score, direction):
    """Calculate an explainable directional score on a 0-100 scale."""
    if direction not in {"up", "down"}:
        raise ValueError("direction must be up or down")
    sensitivity_key = (
        "positive_beta_score" if direction == "up" else "negative_beta_score"
    )
    weights = {
        "us": 0.25,
        "korea": 0.20,
        "business_purity": 0.20,
        "sensitivity": 0.20,
        "stability": 0.10,
        "liquidity": 0.05,
    }
    values = {
        "us": us_score,
        "korea": korea_score,
        "business_purity": row["business_purity"],
        "sensitivity": row[sensitivity_key],
        "stability": row["stability"],
        "liquidity": row["liquidity"],
    }
    contributions = {
        name: round(float(values[name]) * weight, 2)
        for name, weight in weights.items()
    }
    return {
        "score": round(sum(contributions.values()), 2),
        "contributions": contributions,
    }


def rank_stocks(rows, min_amount=100_000_000, limit=20):
    """Return the highest-scoring liquid and tradable non-ST stocks."""
    eligible = [
        row
        for row in rows
        if not row.get("is_st")
        and row.get("tradable")
        and float(row.get("amount") or 0) >= min_amount
    ]
    return sorted(
        eligible, key=lambda row: row["score"], reverse=True
    )[:limit]


def evaluate_live_gate(metrics):
    """Keep action guidance closed until every research gate passes."""
    checks = {
        "sample_years": metrics.get("sample_years", 0) >= 3,
        "win_rate": metrics.get("win_rate", 0) >= 0.55,
        "sharpe": metrics.get("sharpe", 0) >= 1,
        "mean_excess": metrics.get("mean_excess", 0) > 0,
        "recent_12m_valid": bool(metrics.get("recent_12m_valid")),
        "forward_days": metrics.get("forward_days", 0) >= 30,
        "forward_valid": bool(metrics.get("forward_valid")),
        "data_ok": bool(metrics.get("data_ok")),
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "allow_live": not failed,
        "failed": failed,
        "mode": "live" if not failed else "research",
    }


def build_monthly_universe(
    rows, month_start, candidates, min_amount=100_000_000
):
    """Freeze a monthly universe using observations strictly before it."""
    prior = [
        row
        for row in rows
        if row["date"] < month_start and row["code"] in candidates
    ]
    latest = {}
    for row in sorted(prior, key=lambda item: item["date"]):
        latest[row["code"]] = row
    result = []
    for code, row in latest.items():
        if float(row.get("amount") or 0) < min_amount:
            continue
        result.append(
            {
                **row,
                **candidates[code],
                "code": code,
                "universe_month": month_start[:7],
                "last_history_date": row["date"],
            }
        )
    return sorted(
        result,
        key=lambda row: (
            row["business_purity"] * 0.4
            + row["stability"] * 0.3
            + row["liquidity"] * 0.3
        ),
        reverse=True,
    )


def match_holdings(ranked_rows, positions, direction):
    """Attach portfolio quantities to ranked six-digit A-share codes."""
    positions_by_code = {
        "".join(char for char in str(row.get("code", "")) if char.isdigit())[-6:]: row
        for row in positions
    }
    matched = []
    for row in ranked_rows:
        code = "".join(
            char for char in str(row.get("code", "")) if char.isdigit()
        )[:6]
        position = positions_by_code.get(code)
        if position:
            matched.append({**row, **position, "code": row["code"],
                            "direction": direction})
    return matched
