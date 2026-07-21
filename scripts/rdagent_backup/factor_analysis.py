from __future__ import annotations

import datetime
import json
import math
import os
import pickle
import re
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd


DEFAULT_MIN_ABS_IC = 0.005
DEFAULT_MIN_ABS_ICIR = 0.02
DEFAULT_FDR_Q = 0.10


class NoPublishableFactors(RuntimeError):
    """The research run completed, but no factor may be published."""


def resolve_path(path_value):
    path_string = str(path_value).replace("\\", "/")
    if os.name != "nt":
        if path_string.startswith("Z:"):
            path_string = path_string.replace("Z:", "/mnt/z", 1)
        elif path_string.startswith("C:"):
            path_string = path_string.replace("C:", "/mnt/c", 1)
        elif path_string.startswith("D:"):
            path_string = path_string.replace("D:", "/mnt/d", 1)
    return Path(path_string)


def require_single_config(workspace):
    candidates = list(workspace.glob("mlruns/*/*/artifacts/config"))
    if len(candidates) != 1:
        raise RuntimeError(
            "Expected exactly 1 evaluated config in workspace "
            f"{workspace}, found {len(candidates)}: {candidates}"
        )
    return candidates[0]


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _segment_bounds(config: Mapping[str, Any], segment: str) -> tuple[Any, Any]:
    try:
        bounds = config["task"]["dataset"]["kwargs"]["segments"][segment]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Workspace config has no {segment!r} dataset segment") from exc
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
        raise RuntimeError(f"Invalid {segment!r} dataset segment: {bounds!r}")
    return bounds[0], bounds[1]


def derive_factor_periods(
    config: Mapping[str, Any], selection_segment: str = "valid"
) -> dict[str, dict[str, Any]]:
    """Return configured selection/test periods without allowing test-driven selection."""

    selection_segment = selection_segment.strip().lower()
    if selection_segment not in {"train", "valid", "test"}:
        raise RuntimeError(
            "RDAGENT_FACTOR_SELECTION_SEGMENT must be train, valid, or test"
        )
    selection_start, selection_end = _segment_bounds(config, selection_segment)
    test_start, test_end = _segment_bounds(config, "test")
    return {
        "selection": {
            "segment": selection_segment,
            "start": selection_start,
            "end": selection_end,
        },
        "test": {"segment": "test", "start": test_start, "end": test_end},
    }


def _finite_values(values) -> np.ndarray:
    return pd.to_numeric(pd.Series(values), errors="coerce").dropna().to_numpy(float)


def _hac_t_and_p(values, lags: int | None = None) -> tuple[float, float]:
    """Newey-West t-statistic for the mean and an asymptotic two-sided p-value."""

    array = _finite_values(values)
    count = len(array)
    if count < 2:
        return 0.0, 1.0
    centered = array - array.mean()
    if lags is None:
        lags = int(math.floor(4 * (count / 100) ** (2 / 9)))
    lags = max(0, min(int(lags), count - 1))
    long_run_variance = float(np.dot(centered, centered) / count)
    for lag in range(1, lags + 1):
        covariance = float(np.dot(centered[lag:], centered[:-lag]) / count)
        long_run_variance += 2 * (1 - lag / (lags + 1)) * covariance
    standard_error = math.sqrt(max(long_run_variance, 0.0) / count)
    if standard_error <= 0:
        return (0.0, 1.0) if array.mean() == 0 else (math.copysign(float("inf"), array.mean()), 0.0)
    t_stat = float(array.mean() / standard_error)
    p_value = float(math.erfc(abs(t_stat) / math.sqrt(2)))
    return t_stat, p_value


def _classic_t_and_p(values) -> tuple[float, float]:
    array = _finite_values(values)
    if len(array) < 2:
        return 0.0, 1.0
    standard_error = float(array.std(ddof=1) / math.sqrt(len(array)))
    if standard_error <= 0:
        return (0.0, 1.0) if array.mean() == 0 else (math.copysign(float("inf"), array.mean()), 0.0)
    t_stat = float(array.mean() / standard_error)
    return t_stat, float(math.erfc(abs(t_stat) / math.sqrt(2)))


def _block_bootstrap_t_and_p(
    values,
    block_size: int | None = None,
    repetitions: int = 1000,
    seed: int = 20260719,
) -> tuple[float, float]:
    """Circular block-bootstrap test of a zero mean, deterministic for auditing."""

    array = _finite_values(values)
    count = len(array)
    if count < 2:
        return 0.0, 1.0
    block_size = max(1, min(int(block_size or round(math.sqrt(count))), count))
    repetitions = max(100, int(repetitions))
    rng = np.random.default_rng(seed)
    centered = array - array.mean()
    boot_means = np.empty(repetitions, dtype=float)
    blocks_needed = math.ceil(count / block_size)
    offsets = np.arange(block_size)
    for iteration in range(repetitions):
        starts = rng.integers(0, count, size=blocks_needed)
        indices = (starts[:, None] + offsets) % count
        boot_means[iteration] = centered[indices.ravel()[:count]].mean()
    standard_error = float(boot_means.std(ddof=1))
    t_stat = float(array.mean() / standard_error) if standard_error > 0 else 0.0
    p_value = float(
        (1 + np.count_nonzero(np.abs(boot_means) >= abs(array.mean())))
        / (repetitions + 1)
    )
    return t_stat, p_value


def benjamini_hochberg(p_values) -> list[float | None]:
    """BH adjusted p-values; missing hypotheses remain missing."""

    adjusted: list[float | None] = [None] * len(p_values)
    finite = [
        (index, float(value))
        for index, value in enumerate(p_values)
        if value is not None and np.isfinite(value)
    ]
    if not finite:
        return adjusted
    ordered = sorted(finite, key=lambda item: item[1])
    total = len(ordered)
    running = 1.0
    for rank in range(total, 0, -1):
        index, p_value = ordered[rank - 1]
        running = min(running, p_value * total / rank)
        adjusted[index] = float(min(1.0, running))
    return adjusted


def _statistic(values, settings: Mapping[str, Any], seed: int) -> tuple[float, float]:
    method = settings["method"]
    if method == "hac":
        return _hac_t_and_p(values, settings.get("hac_lags"))
    if method == "block_bootstrap":
        return _block_bootstrap_t_and_p(
            values,
            block_size=settings.get("block_size"),
            repetitions=settings.get("bootstrap_repetitions", 1000),
            seed=seed,
        )
    if method == "classic":
        return _classic_t_and_p(values)
    raise RuntimeError(
        "RDAGENT_FACTOR_STAT_METHOD must be hac, block_bootstrap, or classic"
    )


def evaluate_factor_frame(
    feature_frame: pd.DataFrame,
    label_frame: pd.DataFrame,
    settings: Mapping[str, Any],
) -> pd.DataFrame:
    """Compute factor statistics and apply BH once across all attempted features."""

    if label_frame.empty or len(label_frame.columns) == 0:
        raise RuntimeError("Factor evaluation label frame is empty")
    label_column = label_frame.columns[0]
    label_series = label_frame[label_column]
    metrics: list[dict[str, Any]] = []
    for feature_index, column in enumerate(feature_frame.columns):
        factor_data = pd.concat([feature_frame[column], label_series], axis=1).dropna()
        if factor_data.empty:
            ic_series = pd.Series(dtype=float)
        else:
            ic_series = factor_data.groupby(level="datetime", group_keys=False).apply(
                lambda group: group[column].corr(
                    group[label_column], method="spearman"
                )
            )
            ic_series = pd.to_numeric(ic_series, errors="coerce").dropna()
        mean_ic = float(ic_series.mean()) if len(ic_series) else 0.0
        std_ic = float(ic_series.std()) if len(ic_series) > 1 else 0.0
        icir = mean_ic / std_ic if std_ic else 0.0
        win_rate = float(
            (ic_series > 0).mean() if mean_ic > 0 else (ic_series < 0).mean()
        ) if len(ic_series) else 0.0
        t_stat, p_value = _statistic(
            ic_series,
            settings,
            int(settings.get("seed", 20260719)) + feature_index,
        )
        metrics.append(
            {
                "Feature": str(column),
                "Rank IC": mean_ic,
                "Rank ICIR": float(icir),
                "Win Rate": win_rate,
                "Observations": int(len(ic_series)),
                "t-stat": float(t_stat),
                "p-value": float(p_value),
            }
        )

    q_values = benjamini_hochberg([row["p-value"] for row in metrics])
    for row, q_value in zip(metrics, q_values):
        row["q-value"] = q_value
        row["base_pass"] = bool(
            abs(row["Rank IC"]) >= settings["min_abs_ic"]
            and abs(row["Rank ICIR"]) >= settings["min_abs_icir"]
            and row["Observations"] >= settings["min_observations"]
        )
        row["stat_pass"] = bool(
            q_value is not None and q_value <= settings["fdr_q"]
        )
        row["is_effective"] = bool(
            row["base_pass"]
            and (row["stat_pass"] or not settings["stat_gate"])
        )
    if not metrics:
        return pd.DataFrame(
            columns=[
                "Feature", "Rank IC", "Rank ICIR", "Win Rate", "Observations",
                "t-stat", "p-value", "q-value", "base_pass", "stat_pass",
                "is_effective",
            ]
        )
    return pd.DataFrame(metrics).sort_values(
        by="Rank ICIR", key=abs, ascending=False
    )


def evaluate_selection_and_test(
    selection_features: pd.DataFrame,
    selection_labels: pd.DataFrame,
    test_features: pd.DataFrame,
    test_labels: pd.DataFrame,
    settings: Mapping[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Freeze effective factors from selection data before evaluating test data."""

    selection_metrics = evaluate_factor_frame(
        selection_features, selection_labels, settings
    )
    effective_features = selection_metrics.loc[
        selection_metrics["is_effective"], "Feature"
    ].tolist()
    test_metrics = evaluate_factor_frame(test_features, test_labels, settings)
    return selection_metrics, test_metrics, effective_features


def _settings_from_environment() -> dict[str, Any]:
    hac_lags_value = os.environ.get("RDAGENT_FACTOR_HAC_LAGS", "").strip()
    block_size_value = os.environ.get("RDAGENT_FACTOR_BLOCK_SIZE", "").strip()
    method = os.environ.get("RDAGENT_FACTOR_STAT_METHOD", "hac").strip().lower()
    settings = {
        "method": method,
        "stat_gate": _env_bool("RDAGENT_FACTOR_STAT_GATE", True),
        "fdr_q": float(os.environ.get("RDAGENT_FACTOR_FDR_Q", DEFAULT_FDR_Q)),
        "min_abs_ic": float(
            os.environ.get("RDAGENT_FACTOR_MIN_ABS_IC", DEFAULT_MIN_ABS_IC)
        ),
        "min_abs_icir": float(
            os.environ.get("RDAGENT_FACTOR_MIN_ABS_ICIR", DEFAULT_MIN_ABS_ICIR)
        ),
        "min_observations": int(
            os.environ.get("RDAGENT_FACTOR_MIN_OBSERVATIONS", "40")
        ),
        "hac_lags": int(hac_lags_value) if hac_lags_value else None,
        "block_size": int(block_size_value) if block_size_value else None,
        "bootstrap_repetitions": int(
            os.environ.get("RDAGENT_FACTOR_BOOTSTRAP_REPETITIONS", "1000")
        ),
        "seed": int(os.environ.get("RDAGENT_FACTOR_STAT_SEED", "20260719")),
    }
    if not 0 < settings["fdr_q"] <= 1:
        raise RuntimeError("RDAGENT_FACTOR_FDR_Q must be in (0, 1]")
    if method not in {"hac", "block_bootstrap", "classic"}:
        raise RuntimeError(
            "RDAGENT_FACTOR_STAT_METHOD must be hac, block_bootstrap, or classic"
        )
    return settings


def _json_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records = []
    for record in frame.to_dict(orient="records"):
        clean = {}
        for key, value in record.items():
            if isinstance(value, (np.bool_, bool)):
                clean[key] = bool(value)
            elif isinstance(value, (np.integer,)):
                clean[key] = int(value)
            elif isinstance(value, (np.floating, float)):
                clean[key] = None if not np.isfinite(value) else float(value)
            else:
                clean[key] = value
        records.append(clean)
    return records


def _display_period(period: Mapping[str, Any]) -> str:
    return f"{period['start']} to {period['end']}"


def _workspace_identity(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/").rstrip("/")
    # Windows paths are case-insensitive even when factor_analysis runs in WSL.
    if len(text) >= 2 and text[1] == ":":
        return text.casefold()
    return text


def load_exact_screen_gate(
    path_value: Any,
    expected_workspace: Any,
    expected_universe: str,
) -> dict[str, Any]:
    """Load and fail-closed validate the winner-specific screen artifact."""

    path = resolve_path(path_value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Could not read exact screen artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Exact screen artifact is not an object: {path}")
    if payload.get("scope") != "exact_workspace":
        raise RuntimeError("Exact screen artifact does not have exact_workspace scope")
    if _workspace_identity(payload.get("workspace")) != _workspace_identity(expected_workspace):
        raise RuntimeError(
            "Exact screen workspace does not match the factor-analysis workspace"
        )
    universe = str(payload.get("universe") or "").strip().lower()
    if universe != str(expected_universe or "").strip().lower():
        raise RuntimeError(
            f"Exact screen universe mismatch: expected {expected_universe}, got {universe}"
        )

    rows = payload.get("factors")
    if not isinstance(rows, list):
        raise RuntimeError("Exact screen factors must be a list")
    names = []
    passed = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("factor") or "").strip():
            raise RuntimeError("Exact screen contains an invalid factor row")
        name = str(row["factor"])
        names.append(name)
        if row.get("pass") is True:
            passed.append(name)
    if len(set(names)) != len(names):
        raise RuntimeError("Exact screen contains duplicate factor rows")
    if payload.get("screened") is not None and int(payload["screened"]) != len(rows):
        raise RuntimeError("Exact screen screened count does not match factor rows")
    if payload.get("n_pass") is None or int(payload["n_pass"]) != len(passed):
        raise RuntimeError("Exact screen n_pass does not match passing factor rows")
    declared_passed = payload.get("passed_factors")
    if declared_passed is not None and list(declared_passed) != passed:
        raise RuntimeError("Exact screen passed_factors does not match passing rows")
    return {
        "path": str(path),
        "scope": "exact_workspace",
        "workspace": str(payload["workspace"]),
        "universe": universe,
        "screened": len(rows),
        "n_pass": len(passed),
        "passed_factors": passed,
    }


def apply_exact_screen_gate(
    fdr_effective_features: list[str],
    gate: Mapping[str, Any] | None,
) -> list[str]:
    """Preserve FDR order while enforcing the exact-screen factor whitelist."""

    if gate is None:
        return list(fdr_effective_features)
    allowed = set(gate.get("passed_factors", []))
    return [name for name in fdr_effective_features if name in allowed]


def main():
    print(
        f"[{datetime.datetime.now():%H:%M:%S}] Initializing Qlib Factor Selection/Cleaning...",
        flush=True,
    )
    override = os.environ.get("RDAGENT_SOTA_WS_OVERRIDE", "").strip()
    pointer = (
        Path("C:/rdagent/sota_workspace.txt")
        if os.name == "nt"
        else Path("/mnt/c/rdagent/sota_workspace.txt")
    )
    if override:
        workspace_string = override
    elif pointer.exists():
        workspace_string = pointer.read_text(encoding="utf-8").strip()
    else:
        workspace_string = "Z:/claude/rdagent_workspace/5dcf477aca8f4ac5bbbcb53092653051"

    workspace = resolve_path(workspace_string)
    print(
        f"[factor_analysis] SOTA workspace = {workspace} "
        f"(override={'yes' if override else 'no'})",
        flush=True,
    )
    config_path = require_single_config(workspace)

    calendar_path = resolve_path("C:/qlib_data/cn_data/calendars/day.txt")
    last_line = calendar_path.read_text(encoding="utf-8").strip().splitlines()[-1].strip()
    new_end = datetime.date.fromisoformat(last_line)
    print(f"Auto-detected NEW_END: {new_end}", flush=True)

    os.chdir(str(workspace))
    with open(config_path, "rb") as config_file:
        config = pickle.load(config_file)

    selection_segment = os.environ.get(
        "RDAGENT_FACTOR_SELECTION_SEGMENT", "valid"
    ).strip().lower()
    if _env_bool("RDAGENT_FACTOR_LEGACY_TEST_SELECTION", False):
        selection_segment = "test"
        print(
            "WARNING: legacy test-driven factor selection is enabled explicitly; "
            "the resulting batch is not an untouched OOS result.",
            flush=True,
        )

    config["qlib_init"]["provider_uri"] = str(resolve_path("C:/qlib_data/cn_data"))
    config["data_handler_config"]["end_time"] = new_end
    if _env_bool("RDAGENT_FACTOR_EXTEND_TEST_END", True):
        segments = config["task"]["dataset"]["kwargs"]["segments"]
        test_start, _test_end = _segment_bounds(config, "test")
        segments["test"] = [test_start, new_end]
    periods = derive_factor_periods(config, selection_segment)
    settings = _settings_from_environment()

    # Qlib is imported only for the operational entry point so the statistical
    # helpers remain independently testable on machines without Qlib.
    import qlib
    from qlib.utils import init_instance_by_config

    qlib.init(
        provider_uri=config["qlib_init"]["provider_uri"],
        region=config["qlib_init"]["region"],
    )
    dataset = init_instance_by_config(config["task"]["dataset"])
    selection_features = dataset.prepare(selection_segment, col_set="feature")
    selection_labels = dataset.prepare(selection_segment, col_set="label")
    test_features = dataset.prepare("test", col_set="feature")
    test_labels = dataset.prepare("test", col_set="label")
    selection_metrics, test_metrics, fdr_effective_features = evaluate_selection_and_test(
        selection_features,
        selection_labels,
        test_features,
        test_labels,
        settings,
    )

    selection_dates = (
        selection_labels.iloc[:, 0]
        .dropna()
        .index.get_level_values("datetime")
        .unique()
    )
    test_dates = (
        test_labels.iloc[:, 0]
        .dropna()
        .index.get_level_values("datetime")
        .unique()
    )
    print(
        f"Selection Period ({selection_segment}): {_display_period(periods['selection'])} "
        f"({len(selection_dates)} trading days)",
        flush=True,
    )
    print(
        f"Test Report Period (never selects factors): {_display_period(periods['test'])} "
        f"({len(test_dates)} trading days)",
        flush=True,
    )

    exact_screen_gate = None
    exact_screen_value = os.environ.get(
        "RDAGENT_FACTOR_EXACT_SCREEN_PATH", ""
    ).strip()
    if (
        override
        and not exact_screen_value
        and not _env_bool("RDAGENT_FACTOR_ALLOW_UNSCREENED_OVERRIDE", False)
    ):
        raise RuntimeError(
            "An RDAGENT_SOTA_WS_OVERRIDE production batch requires "
            "RDAGENT_FACTOR_EXACT_SCREEN_PATH; set "
            "RDAGENT_FACTOR_ALLOW_UNSCREENED_OVERRIDE=1 only for an explicitly "
            "research-only legacy run."
        )
    if exact_screen_value:
        exact_screen_universe = os.environ.get(
            "RDAGENT_FACTOR_EXACT_SCREEN_UNIVERSE", ""
        ).strip()
        if not exact_screen_universe:
            raise RuntimeError(
                "RDAGENT_FACTOR_EXACT_SCREEN_UNIVERSE is required with the exact screen"
            )
        exact_screen_gate = load_exact_screen_gate(
            exact_screen_value,
            expected_workspace=workspace_string,
            expected_universe=exact_screen_universe,
        )
    if not fdr_effective_features:
        raise NoPublishableFactors(
            "No feature met the selection-split Rank IC/ICIR/FDR thresholds; "
            "no effective-factor batch was archived. Set "
            "RDAGENT_FACTOR_STAT_GATE=0 only for an explicitly legacy-compatible run."
        )
    effective_features = apply_exact_screen_gate(
        fdr_effective_features, exact_screen_gate
    )
    if not effective_features:
        raise NoPublishableFactors(
            "Selection-split FDR factors and exact-screen passing factors have no "
            "intersection; no batch was archived."
        )
    if len(effective_features) < 5:
        print(
            f"Warning: only {len(effective_features)} features met the thresholds; "
            "keeping only threshold-qualified features.",
            flush=True,
        )

    final_directory = resolve_path("C:/rdagent/final")
    final_directory.mkdir(parents=True, exist_ok=True)
    canonical_path = final_directory / "effective_factors.json"
    if not override:
        canonical_path.write_text(
            json.dumps(effective_features, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
        print(
            f"Saved {len(effective_features)} effective features to {canonical_path}",
            flush=True,
        )

    batches_directory = final_directory / "batches"
    batches_directory.mkdir(parents=True, exist_ok=True)
    requested_batch_label = os.environ.get("RDAGENT_FACTOR_BATCH_LABEL", "").strip()
    if requested_batch_label and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", requested_batch_label):
        raise RuntimeError("RDAGENT_FACTOR_BATCH_LABEL contains unsafe characters")
    batch_label = requested_batch_label or datetime.datetime.now().strftime("%Y%m%d_%H%M")
    attempt_count = len(selection_metrics)
    manifest = {
        "schema_version": 2,
        "label": batch_label,
        "workspace": workspace_string,
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "new_end": new_end.strftime("%Y-%m-%d"),
        "selection_period": periods["selection"],
        "test_report_period": periods["test"],
        "test_used_for_selection": selection_segment == "test",
        "statistics": settings,
        "attempt_count": attempt_count,
        "test_report_attempt_count": len(test_metrics),
        "fdr_effective_factors": fdr_effective_features,
        "exact_screen_gate": exact_screen_gate,
        "effective_factors": effective_features,
        "all_features": selection_metrics["Feature"].tolist(),
        "factor_metrics": _json_records(selection_metrics),
        "test_report_metrics": _json_records(test_metrics),
    }
    batch_path = batches_directory / f"{batch_label}.json"
    batch_payload = json.dumps(manifest, indent=4, ensure_ascii=False, default=str)
    batch_tmp = batch_path.with_name(f".{batch_path.name}.{os.getpid()}.tmp")
    try:
        batch_tmp.write_text(batch_payload, encoding="utf-8")
        os.replace(batch_tmp, batch_path)
    finally:
        batch_tmp.unlink(missing_ok=True)
    print(
        f"Archived batch manifest -> {batch_path} "
        f"({len(effective_features)} effective / {attempt_count} attempted)",
        flush=True,
    )

    test_by_feature = test_metrics.set_index("Feature").to_dict(orient="index")
    fdr_effective_set = set(fdr_effective_features)
    final_effective_set = set(effective_features)
    rows = []
    for _, row in selection_metrics.iterrows():
        feature_name = row["Feature"]
        if feature_name in final_effective_set:
            status = "PASS"
        elif feature_name in fdr_effective_set and exact_screen_gate is not None:
            status = "EXCLUDE_EXACT_SCREEN"
        else:
            status = "EXCLUDE_FDR"
        test_row = test_by_feature.get(row["Feature"], {})
        test_ic = test_row.get("Rank IC")
        test_ic_text = f"{test_ic:.6f}" if test_ic is not None else "NA"
        rows.append(
            f"| `{row['Feature']}` | {row['Rank IC']:.6f} | "
            f"{row['Rank ICIR']:.4f} | {row['Win Rate']:.2%} | "
            f"{int(row['Observations'])} | {row['t-stat']:.2f} | "
            f"{row['p-value']:.4g} | {row['q-value']:.4g} | "
            f"{test_ic_text} | {status} |"
        )
    report_path = final_directory / f"factor_analysis_{new_end:%Y%m%d}.md"
    report = f"""# Factor Analysis & Clean-Selection Report ({new_end})

### Evaluation Summary
- **Selection Split**: `{selection_segment}`; {_display_period(periods['selection'])} ({len(selection_dates)} trading days)
- **Test Report Only**: {_display_period(periods['test'])} ({len(test_dates)} trading days)
- **Test Used for Selection**: `{selection_segment == 'test'}`
- **Attempt Count**: {attempt_count}
- **FDR-Qualified Features**: {len(fdr_effective_features)}
- **Exact Screen Gate**: {exact_screen_gate if exact_screen_gate is not None else 'not configured'}
- **Features Passed (Effective)**: {len(effective_features)}
- **Features Excluded**: {attempt_count - len(effective_features)}

### Selection Criteria
1. **Absolute Rank IC** >= `{settings['min_abs_ic']}`
2. **Absolute Rank ICIR** >= `{settings['min_abs_icir']}`
3. **Minimum IC observations** >= `{settings['min_observations']}`
4. **{settings['method']} p-values + BH-FDR q-value** <= `{settings['fdr_q']}` (gate enabled: `{settings['stat_gate']}`)

`Test Rank IC` is reported for diagnosis and never changes PASS/EXCLUDE unless the explicit legacy test-selection switch is enabled.

| Feature | Selection Rank IC | Rank ICIR | Win Rate | N | t-stat | p-value | q-value | Test Rank IC | Selection Status |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
""" + "\n".join(rows) + "\n"
    report_path.write_text(report, encoding="utf-8")
    print(f"Saved markdown report to {report_path}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except NoPublishableFactors as exc:
        print(f"[factor_analysis] NO_PUBLISHABLE_FACTORS: {exc}", flush=True)
        raise SystemExit(3) from None
