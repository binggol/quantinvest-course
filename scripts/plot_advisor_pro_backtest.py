"""Plot audited Advisor Pro staggered backtest results."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd


COLORS = {
    "top8": "#087E8B",
    "double_cost": "#F28E2B",
    "top20": "#4E79A7",
    "stress": "#C44E52",
    "positive": "#2A9D8F",
    "negative": "#D1495B",
    "grid": "#D9DEE5",
    "text": "#20262E",
}


def _mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be an object")
    return value


def load_payload(path: str | Path) -> Mapping[str, Any]:
    source = Path(path)
    return _mapping(json.loads(source.read_text(encoding="utf-8-sig")), str(source))


def combine_equal_capital(
    payload: Mapping[str, Any], *, return_field: str = "net_return", double_cost: bool = False
) -> pd.Series:
    raw_runs = payload.get("runs")
    if not isinstance(raw_runs, list) or not raw_runs:
        raise ValueError("runs must be a non-empty array")

    offsets: list[int] = []
    sleeve_returns: list[pd.Series] = []
    all_dates = pd.DatetimeIndex([])
    for index, raw_run in enumerate(raw_runs):
        run = _mapping(raw_run, f"runs[{index}]")
        if "result" in run:
            run = _mapping(run["result"], f"runs[{index}].result")
        offsets.append(int(run["frequency_offset"]))
        raw_path = run.get("daily_path")
        if not isinstance(raw_path, list) or not raw_path:
            raise ValueError(f"runs[{index}].daily_path must be a non-empty array")
        frame = pd.DataFrame(raw_path)
        if return_field not in frame:
            raise ValueError(f"runs[{index}].daily_path is missing {return_field}")
        values = pd.to_numeric(frame[return_field], errors="raise").astype(float)
        if double_cost:
            if "cost_rate" not in frame:
                raise ValueError(f"runs[{index}].daily_path is missing cost_rate")
            values = values - pd.to_numeric(frame["cost_rate"], errors="raise").astype(float)
        dates = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
        series = pd.Series(values.to_numpy(), index=dates, dtype=float)
        if not series.index.is_monotonic_increasing or series.index.has_duplicates:
            raise ValueError(f"runs[{index}].daily_path dates must be unique and increasing")
        sleeve_returns.append(series)
        all_dates = all_dates.union(series.index)

    frequency_step = max(1, round(int(raw_runs[0].get("frequency_days", 5)) / 5))
    expected = list(range(frequency_step))
    if sorted(offsets) != expected:
        raise ValueError(f"offsets must be complete: expected {expected}, found {sorted(offsets)}")

    all_dates = all_dates.sort_values()
    sleeve_navs = [
        (1.0 + series.reindex(all_dates, fill_value=0.0)).cumprod()
        for series in sleeve_returns
    ]
    combined_nav = pd.concat(sleeve_navs, axis=1).mean(axis=1)
    combined_returns = combined_nav.pct_change()
    combined_returns.iloc[0] = combined_nav.iloc[0] - 1.0
    combined_returns.name = "return"
    return combined_returns


def nav_path(returns: pd.Series, *, rebase_at: str | None = None) -> pd.Series:
    values = returns.copy()
    if rebase_at is not None:
        values = values.loc[pd.Timestamp(rebase_at) :]
    nav = (1.0 + values).cumprod()
    if nav.empty:
        return nav
    baseline_date = (
        pd.Timestamp(rebase_at)
        if rebase_at is not None and pd.Timestamp(rebase_at) < nav.index[0]
        else nav.index[0] - pd.Timedelta(days=1)
    )
    return pd.concat([pd.Series([1.0], index=[baseline_date]), nav])


def drawdown_path(returns: pd.Series) -> pd.Series:
    nav = nav_path(returns)
    return nav / nav.cummax() - 1.0


def annual_returns(returns: pd.Series) -> pd.Series:
    return (1.0 + returns).resample("YE").prod() - 1.0


def period_metrics(returns: pd.Series, start: str, end: str) -> dict[str, float]:
    values = returns.loc[pd.Timestamp(start) : pd.Timestamp(end)].fillna(0.0)
    nav = nav_path(values)
    years = len(values) / 252.0
    annualized = float(nav.iloc[-1] ** (1.0 / years) - 1.0) if years > 0 else 0.0
    volatility = float(values.std(ddof=1) * np.sqrt(252.0)) if len(values) > 1 else 0.0
    sharpe = float(values.mean() / values.std(ddof=1) * np.sqrt(252.0)) if volatility > 0 else 0.0
    max_drawdown = float((nav / nav.cummax() - 1.0).min())
    return {
        "annualized_return": annualized,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
    }


def _configure_style() -> None:
    available = {item.name for item in font_manager.fontManager.ttflist}
    for candidate in ("Microsoft YaHei", "DengXian", "SimHei"):
        if candidate in available:
            plt.rcParams["font.family"] = candidate
            break
    plt.rcParams.update(
        {
            "axes.unicode_minus": False,
            "axes.edgecolor": "#B8C0CA",
            "axes.labelcolor": COLORS["text"],
            "axes.titlecolor": COLORS["text"],
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "figure.facecolor": "#F7F8FA",
            "axes.facecolor": "#FFFFFF",
            "font.size": 10,
            "grid.color": COLORS["grid"],
            "grid.linewidth": 0.7,
            "legend.frameon": False,
            "text.color": COLORS["text"],
        }
    )


def _format_time_axis(axis: plt.Axes) -> None:
    axis.xaxis.set_major_locator(mdates.YearLocator())
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axis.grid(axis="y", alpha=0.85)
    axis.spines[["top", "right"]].set_visible(False)


def _percent_axis(axis: plt.Axes) -> None:
    axis.yaxis.set_major_formatter(lambda value, _: f"{value:.0%}")


def plot_overview(
    top8: pd.Series,
    top8_double: pd.Series,
    top20: pd.Series,
    output: str | Path,
) -> None:
    _configure_style()
    figure, axes = plt.subplots(2, 2, figsize=(16, 10))
    figure.subplots_adjust(left=0.06, right=0.99, top=0.91, bottom=0.10, hspace=0.28, wspace=0.12)
    figure.suptitle("顾问 Pro 回测总览", fontsize=19, fontweight="bold", x=0.06, y=0.975, ha="left")

    full = axes[0, 0]
    full.plot(nav_path(top8), color=COLORS["top8"], linewidth=2.2, label="Top8 / 15日 / 换2只（1亿元）")
    full.plot(
        nav_path(top8_double),
        color=COLORS["double_cost"],
        linewidth=1.6,
        label="Top8 双倍成本",
    )
    full.plot(nav_path(top20), color=COLORS["top20"], linewidth=1.7, label="Top20 低换手备选（3000万元）")
    full.axvspan(pd.Timestamp("2022-01-01"), pd.Timestamp("2025-01-01"), color="#EAF2F2", alpha=0.8)
    full.set_title("全区间累计净值（初始值 = 1）", loc="left")
    full.set_ylabel("累计净值")
    full.legend(loc="upper left")
    _format_time_axis(full)

    validation = axes[0, 1]
    validation_end = "2024-12-31"
    for values, color, label, width in (
        (top8, COLORS["top8"], "Top8 标准", 2.2),
        (top8_double, COLORS["double_cost"], "Top8 双倍成本", 1.6),
        (top20, COLORS["top20"], "Top20 低换手（3000万元）", 1.7),
    ):
        path = nav_path(values.loc[:validation_end], rebase_at="2022-01-01")
        validation.plot(path, color=color, linewidth=width, label=label)
    validation.set_title("2022–2024 参数评价区间（重新归一）", loc="left")
    validation.set_ylabel("累计净值")
    validation.legend(loc="upper left")
    _format_time_axis(validation)

    drawdown = axes[1, 0]
    drawdown.plot(drawdown_path(top8), color=COLORS["top8"], linewidth=1.7, label="Top8 标准")
    drawdown.plot(drawdown_path(top20), color=COLORS["top20"], linewidth=1.5, label="Top20 低换手")
    drawdown.fill_between(
        drawdown_path(top8).index,
        drawdown_path(top8).to_numpy(),
        0,
        color=COLORS["top8"],
        alpha=0.10,
    )
    drawdown.set_title("历史回撤", loc="left")
    drawdown.set_ylabel("回撤")
    drawdown.legend(loc="lower left")
    _percent_axis(drawdown)
    _format_time_axis(drawdown)

    annual = axes[1, 1]
    top8_year = annual_returns(top8)
    top20_year = annual_returns(top20).reindex(top8_year.index)
    x = np.arange(len(top8_year))
    width = 0.36
    annual.bar(x - width / 2, top8_year.to_numpy(), width, color=COLORS["top8"], label="Top8 标准")
    annual.bar(x + width / 2, top20_year.to_numpy(), width, color=COLORS["top20"], label="Top20 低换手")
    annual.axhline(0, color="#7D8793", linewidth=0.8)
    year_labels = [
        f"{value.year}*" if value.year in (2017, 2026) else str(value.year)
        for value in top8_year.index
    ]
    annual.set_xticks(x, year_labels)
    annual.set_title("年度收益", loc="left")
    annual.set_ylabel("年度收益率")
    annual.legend(loc="upper left")
    annual.grid(axis="y", alpha=0.85)
    annual.spines[["top", "right"]].set_visible(False)
    _percent_axis(annual)

    figure.text(
        0.06,
        0.025,
        "注：三组等初始资金错峰组合；阴影为 2022–2024 参数评价区间；* 为不完整年度。历史结果不代表未来收益。",
        fontsize=9,
        color="#5C6673",
    )
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(target, dpi=170, facecolor=figure.get_facecolor())
    plt.close(figure)


def plot_capacity(
    standard: pd.Series,
    standard_double: pd.Series,
    stress: pd.Series,
    stress_double: pd.Series,
    output: str | Path,
) -> None:
    _configure_style()
    figure, axes = plt.subplots(2, 2, figsize=(16, 10))
    figure.subplots_adjust(left=0.06, right=0.99, top=0.91, bottom=0.10, hspace=0.28, wspace=0.12)
    figure.suptitle("顾问 Pro · 1亿元容量压力测试", fontsize=19, fontweight="bold", x=0.06, y=0.975, ha="left")

    full = axes[0, 0]
    for values, color, label, style in (
        (standard, COLORS["top8"], "标准成交约束", "-"),
        (stress, COLORS["stress"], "5%成交量上限 / 更高冲击", "--"),
    ):
        full.plot(nav_path(values), color=color, linewidth=2.0, linestyle=style, label=label)
    full.set_title("全区间累计净值", loc="left")
    full.set_ylabel("累计净值")
    full.legend(loc="upper left")
    _format_time_axis(full)

    validation = axes[0, 1]
    for values, color, label, style in (
        (standard, COLORS["top8"], "标准", "-"),
        (standard_double, COLORS["double_cost"], "标准 + 双倍成本", "--"),
        (stress, COLORS["stress"], "严格容量", "-"),
        (stress_double, "#7A5195", "严格容量 + 双倍成本", ":"),
    ):
        path = nav_path(values.loc[:"2024-12-31"], rebase_at="2022-01-01")
        validation.plot(path, color=color, linewidth=1.8, linestyle=style, label=label)
    validation.set_title("2022–2024 净值压力对比", loc="left")
    validation.set_ylabel("累计净值")
    validation.legend(loc="upper left", ncol=2)
    _format_time_axis(validation)

    drawdown = axes[1, 0]
    drawdown.plot(drawdown_path(standard), color=COLORS["top8"], linewidth=1.7, label="标准")
    drawdown.plot(drawdown_path(stress), color=COLORS["stress"], linewidth=1.5, label="严格容量")
    drawdown.set_title("容量情景回撤", loc="left")
    drawdown.set_ylabel("回撤")
    drawdown.legend(loc="lower left")
    _percent_axis(drawdown)
    _format_time_axis(drawdown)

    metrics_axis = axes[1, 1]
    metrics = {
        "标准": period_metrics(standard, "2022-01-01", "2024-12-31"),
        "标准双倍成本": period_metrics(standard_double, "2022-01-01", "2024-12-31"),
        "严格容量": period_metrics(stress, "2022-01-01", "2024-12-31"),
        "严格容量双倍成本": period_metrics(stress_double, "2022-01-01", "2024-12-31"),
    }
    labels = list(metrics)
    returns = [metrics[label]["annualized_return"] for label in labels]
    drawdowns = [abs(metrics[label]["max_drawdown"]) for label in labels]
    x = np.arange(len(labels))
    width = 0.34
    metrics_axis.bar(x - width / 2, returns, width, color=COLORS["positive"], label="年化收益")
    metrics_axis.bar(x + width / 2, drawdowns, width, color=COLORS["negative"], label="最大回撤绝对值")
    metrics_axis.set_xticks(x, ["标准", "标准\n双倍成本", "严格容量", "严格容量\n双倍成本"])
    metrics_axis.set_title("2022–2024 收益与回撤", loc="left")
    metrics_axis.legend(loc="upper right")
    metrics_axis.grid(axis="y", alpha=0.85)
    metrics_axis.spines[["top", "right"]].set_visible(False)
    _percent_axis(metrics_axis)

    figure.text(
        0.06,
        0.025,
        "严格容量：每个子组合约3333万元，最多参与当日成交量5%，冲击参数0.20。",
        fontsize=9,
        color="#5C6673",
    )
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(target, dpi=170, facecolor=figure.get_facecolor())
    plt.close(figure)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--top8-detailed",
        default="data/advisor_pro_n8_f15_d2_total100m_detailed.json",
    )
    parser.add_argument(
        "--stress-detailed",
        default="data/advisor_pro_n8_f15_d2_total100m_stress_detailed.json",
    )
    parser.add_argument(
        "--top20-detailed",
        default="data/advisor_pro_n20_f15_d2_all_offsets_detailed.json",
    )
    parser.add_argument("--out-dir", default="data/charts")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    top8_payload = load_payload(args.top8_detailed)
    stress_payload = load_payload(args.stress_detailed)
    top20_payload = load_payload(args.top20_detailed)

    top8 = combine_equal_capital(top8_payload)
    top8_double = combine_equal_capital(top8_payload, double_cost=True)
    stress = combine_equal_capital(stress_payload)
    stress_double = combine_equal_capital(stress_payload, double_cost=True)
    top20 = combine_equal_capital(top20_payload)

    output_dir = Path(args.out_dir)
    overview = output_dir / "advisor_pro_backtest_overview.png"
    capacity = output_dir / "advisor_pro_capacity_stress.png"
    plot_overview(top8, top8_double, top20, overview)
    plot_capacity(top8, top8_double, stress, stress_double, capacity)
    print(
        json.dumps(
            {
                "overview": str(overview.resolve()),
                "capacity": str(capacity.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
