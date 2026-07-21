"""Screen RD-Agent factor files against the matching universe evaluator."""

from __future__ import annotations

import ast
import glob
import importlib
import io
import json
import os
import sys
import warnings
from multiprocessing import freeze_support
from typing import NamedTuple

import numpy as np
import pandas as pd


warnings.filterwarnings("ignore")
WORKSPACES = os.environ.get("RDAGENT_SCREEN_WS", r"D:\rdagent_workspace")
EXACT_WORKSPACE = os.environ.get("RDAGENT_SCREEN_EXACT_WORKSPACE", "").strip()
OUTPUT = os.environ.get("RDAGENT_SCREEN_OUT", r"C:\rdagent\rdagent_screen.json")
UNIVERSE = os.environ.get("RDAGENT_SCREEN_UNIVERSE", "csi300").strip().lower()
EVALUATORS = {"csi300": "mine_eval", "csi1000": "mine_eval_1000"}
BASE_KEYS = ["MOM", "VAL", "negTOstd", "negTURN", "ONM", "negASR"]
HORIZONS = [1, 2, 3, 5, 10, 20]
COMBINED_FACTORS_FILE = "combined_factors_df.parquet"


class FactorSource(NamedTuple):
    path: str
    modified: float
    kind: str
    column: str | None = None


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def _zscore(series):
    series = pd.to_numeric(series, errors="coerce")
    standard_deviation = series.std()
    return (series - series.mean()) / standard_deviation if standard_deviation else series * 0


def _factor_name_from_parquet_column(value) -> str | None:
    """Return the Qlib feature name represented by one parquet field."""

    parsed = value
    if isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            parsed = value
    if isinstance(parsed, (tuple, list)):
        if len(parsed) < 2 or str(parsed[0]).lower() != "feature":
            return None
        parsed = parsed[-1]
    name = str(parsed).strip()
    return name if name and name not in {"datetime", "instrument"} else None


def _parquet_factor_columns(path: str) -> list[tuple[str, str]]:
    """Inspect a materialized StaticDataLoader parquet without reading its rows."""

    import pyarrow.parquet as parquet

    schema = parquet.ParquetFile(path).schema_arrow
    pandas_metadata = {}
    if schema.metadata and schema.metadata.get(b"pandas"):
        pandas_metadata = json.loads(schema.metadata[b"pandas"].decode("utf-8"))
    index_fields = set()
    for value in pandas_metadata.get("index_columns", []):
        if isinstance(value, str):
            index_fields.add(value)
        elif isinstance(value, dict) and value.get("field_name"):
            index_fields.add(str(value["field_name"]))
    metadata_by_field = {
        str(item.get("field_name")): item.get("name")
        for item in pandas_metadata.get("columns", [])
        if item.get("field_name") is not None
    }

    factors = []
    seen = set()
    for field_name in schema.names:
        if field_name in index_fields or field_name in {"datetime", "instrument"}:
            continue
        factor_name = _factor_name_from_parquet_column(
            metadata_by_field.get(field_name, field_name)
        )
        if factor_name is None:
            continue
        if factor_name in seen:
            raise RuntimeError(
                f"duplicate factor name {factor_name!r} in {path}"
            )
        seen.add(factor_name)
        factors.append((factor_name, field_name))
    return factors


def _hdf_factor_name(path: str) -> str | None:
    import h5py

    with h5py.File(path, "r") as handle:
        if not handle.keys():
            return None
        group_name = list(handle.keys())[0]
        group = handle[group_name]
        if "axis0" not in group:
            return None
        columns = [
            item.decode() if isinstance(item, bytes) else str(item)
            for item in group["axis0"][:]
        ]
        return columns[0] if columns else None


def _load_distinct_factors(limit: int):
    latest_by_name = {}
    paths = []
    if EXACT_WORKSPACE:
        parquet_path = os.path.join(EXACT_WORKSPACE, COMBINED_FACTORS_FILE)
        exact_result = os.path.join(EXACT_WORKSPACE, "result.h5")
        paths = [
            path
            for path in (parquet_path, exact_result)
            if os.path.isfile(path)
        ]
    else:
        paths = glob.glob(os.path.join(WORKSPACES, "*", "result.h5"))
    for path in paths:
        try:
            modified = os.path.getmtime(path)
            if path.lower().endswith(".parquet"):
                discovered = [
                    (name, FactorSource(path, modified, "parquet", column))
                    for name, column in _parquet_factor_columns(path)
                ]
            else:
                name = _hdf_factor_name(path)
                discovered = (
                    [(name, FactorSource(path, modified, "hdf"))]
                    if name
                    else []
                )
            for name, source in discovered:
                previous = latest_by_name.get(name)
                # The combined parquet is the materialized input used by the
                # evaluated Qlib workspace, so it wins a duplicate over a loose
                # result.h5 even when filesystem timestamps are close.
                if previous is None or (
                    source.kind == "parquet" and previous.kind != "parquet"
                ) or (
                    source.kind == previous.kind
                    and source.modified > previous.modified
                ):
                    latest_by_name[name] = source
        except Exception as exc:
            if EXACT_WORKSPACE:
                raise RuntimeError(f"could not inspect exact factor artifact {path}: {exc}") from exc
            continue
    if EXACT_WORKSPACE and not paths:
        raise RuntimeError(
            f"exact workspace has neither {COMBINED_FACTORS_FILE} nor result.h5: "
            f"{EXACT_WORKSPACE}"
        )
    if EXACT_WORKSPACE and not latest_by_name:
        raise RuntimeError(f"exact workspace contains no readable factors: {EXACT_WORKSPACE}")
    distinct = sorted(
        latest_by_name.items(),
        key=lambda item: (item[1].modified, item[0]),
        reverse=True,
    )
    # A limit is useful only for the optional historical/global diagnostic.
    # An exact publication gate must cover every factor in the winner workspace.
    if not EXACT_WORKSPACE:
        distinct = distinct[:limit]
    return paths, latest_by_name, distinct


def _align_panel(panel: pd.DataFrame, evaluator) -> pd.DataFrame:
    panel.index = [str(date)[:10] for date in panel.index]
    panel.columns = [str(code).lower() for code in panel.columns]
    snapshot_dates = list(dict.fromkeys(snapshot["de"] for snapshot in evaluator.snapshots))
    return panel.reindex(index=snapshot_dates, columns=evaluator.C.columns)


def _iter_factor_panels(distinct, evaluator):
    """Yield materialized factor panels while keeping parquet reads batched."""

    parquet_groups = {}
    for name, source in distinct:
        if source.kind == "parquet":
            parquet_groups.setdefault(source.path, []).append((name, source))
            continue
        frame = pd.read_hdf(source.path)
        if not hasattr(frame, "columns") or len(frame.columns) == 0:
            raise RuntimeError(f"factor HDF is empty: {source.path}")
        matching = [
            column
            for column in frame.columns
            if str(column) == name
            or (
                isinstance(column, tuple)
                and len(column) > 0
                and str(column[-1]) == name
            )
        ]
        column = matching[0] if matching else frame.columns[0]
        yield name, source, _align_panel(
            frame[column].unstack(level="instrument"), evaluator
        )

    if not parquet_groups:
        return

    import pyarrow.parquet as parquet

    snapshot_dates = sorted(
        {pd.Timestamp(snapshot["de"]) for snapshot in evaluator.snapshots}
    )
    if not snapshot_dates:
        raise RuntimeError("evaluator contains no snapshots")
    for path, factors in parquet_groups.items():
        physical_columns = list(dict.fromkeys(source.column for _, source in factors))
        table = parquet.read_table(
            path,
            columns=physical_columns + ["datetime", "instrument"],
            filters=[("datetime", "in", snapshot_dates)],
        )
        frame = table.to_pandas(ignore_metadata=True)
        if frame.empty:
            raise RuntimeError(f"no evaluator snapshot rows found in {path}")
        required = {"datetime", "instrument", *physical_columns}
        missing = required.difference(frame.columns)
        if missing:
            raise RuntimeError(f"missing parquet columns {sorted(missing)} in {path}")
        dates = pd.to_datetime(frame["datetime"], errors="coerce").dt.strftime("%Y-%m-%d")
        instruments = frame["instrument"].astype(str).str.lower()
        index = pd.MultiIndex.from_arrays(
            [dates, instruments], names=["datetime", "instrument"]
        )
        if index.has_duplicates:
            raise RuntimeError(f"duplicate datetime/instrument rows in {path}")
        for name, source in factors:
            values = pd.to_numeric(frame[source.column], errors="coerce")
            series = pd.Series(values.to_numpy(copy=False), index=index, name=name)
            yield name, source, _align_panel(
                series.unstack(level="instrument"), evaluator
            )


def _residual_ic(evaluator, factor):
    values = []
    for snapshot in evaluator.snapshots:
        date, members, forward_return = snapshot["de"], snapshot["mem"], snapshot["fr"]
        if date not in factor.index:
            continue
        y = _zscore(factor.loc[date].reindex(members))
        x = pd.DataFrame({key: snapshot[key].reindex(members) for key in BASE_KEYS})
        data = pd.concat([y.rename("y"), x], axis=1).dropna()
        if len(data) < 60:
            continue
        matrix = np.column_stack([np.ones(len(data)), data[BASE_KEYS].values])
        try:
            beta, *_ = np.linalg.lstsq(matrix, data["y"].values, rcond=None)
        except Exception:
            continue
        residual = pd.Series(data["y"].values - matrix @ beta, index=data.index)
        common = residual.index.intersection(forward_return.index)
        if len(common) >= 60:
            values.append(residual[common].rank().corr(forward_return[common]))
    return float(np.mean(values)) if values else float("nan")


def _horizon_ic(evaluator, factor, horizon):
    values = []
    for snapshot in evaluator.snapshots:
        date, members = snapshot["de"], snapshot["mem"]
        if date not in factor.index:
            continue
        index = evaluator.tdays.index(date)
        if index + horizon >= len(evaluator.tdays):
            continue
        exit_date = evaluator.tdays[index + horizon]
        forward = evaluator.C.loc[exit_date, members] / evaluator.C.loc[date, members] - 1
        current = factor.loc[date].reindex(members)
        common = current.dropna().index.intersection(forward.dropna().index)
        if len(common) >= 60:
            values.append(current[common].rank().corr(forward[common].rank()))
    return float(np.mean(values)) if values else float("nan")


def _aligned_decay(decay):
    values = [
        (horizon, float(decay[str(horizon)]))
        for horizon in HORIZONS
        if decay.get(str(horizon)) is not None
        and np.isfinite(float(decay[str(horizon)]))
    ]
    if not values or values[0][1] == 0:
        return []
    direction = 1.0 if values[0][1] > 0 else -1.0
    return [(horizon, value * direction) for horizon, value in values]


def _half_life_details(decay):
    """Return a direction-aware half-life and whether it is right-censored."""

    values = _aligned_decay(decay)
    if len(values) < 2 or values[0][1] <= 0:
        return None, False
    half = values[0][1] / 2
    for index in range(1, len(values)):
        if values[index][1] <= half:
            prior_horizon, prior_ic = values[index - 1]
            horizon, ic = values[index]
            if prior_ic > ic:
                value = prior_horizon + (prior_ic - half) / (prior_ic - ic + 1e-9) * (horizon - prior_horizon)
                return round(value, 1), False
            return float(horizon), False
    # The IC did not halve in the measured horizon. The last observed horizon is
    # therefore a conservative lower bound, rather than an unknown/failed value.
    return float(values[-1][0]), True


def _half_life(decay):
    return _half_life_details(decay)[0]


def _decay_retention(decay, horizon):
    values = _aligned_decay(decay)
    if not values or values[0][1] <= 0:
        return None
    by_horizon = dict(values)
    if horizon not in by_horizon:
        return None
    return float(by_horizon[horizon] / values[0][1])


def _decay_gate(decay, minimum_half_life, retention_horizon, minimum_retention):
    half_life, censored = _half_life_details(decay)
    retention = _decay_retention(decay, retention_horizon)
    passed = bool(
        half_life is not None
        and half_life >= minimum_half_life
        and retention is not None
        and retention >= minimum_retention
    )
    return {
        "passed": passed,
        "half_life": half_life,
        "half_life_censored": censored,
        "retention": retention,
    }


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if UNIVERSE not in EVALUATORS:
        raise SystemExit(f"No deterministic evaluator is configured for {UNIVERSE}")
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("RDAGENT_SCREEN_K", "40"))
    decay_gate_enabled = _env_bool("RDAGENT_SCREEN_DECAY_GATE", True)
    minimum_half_life = float(os.environ.get("RDAGENT_SCREEN_MIN_HALF_LIFE", "2.0"))
    retention_horizon = int(os.environ.get("RDAGENT_SCREEN_DECAY_HORIZON", "5"))
    minimum_retention = float(os.environ.get("RDAGENT_SCREEN_MIN_DECAY_RETENTION", "0.25"))
    if retention_horizon not in HORIZONS:
        raise SystemExit(
            f"RDAGENT_SCREEN_DECAY_HORIZON must be one of {HORIZONS}"
        )
    paths, all_factors, distinct = _load_distinct_factors(limit)
    print(f"workspace files={len(paths)}, distinct={len(all_factors)}, screening={len(distinct)}", flush=True)

    evaluator_module = EVALUATORS[UNIVERSE]
    evaluator = importlib.import_module(evaluator_module).Evaluator()
    rows = []
    factor_errors = []
    for name, source, panel in _iter_factor_panels(distinct, evaluator):
        try:
            if panel.notna().sum().sum() == 0:
                raise RuntimeError("factor is all-NaN on evaluator snapshots")
            result = evaluator.eval_panel(panel)
            if "error" in result:
                raise RuntimeError(str(result["error"]))

            correlations = result.get("corr") or {}
            redundant_with = max(correlations, key=lambda key: abs(correlations[key])) if correlations else "-"
            decay = {}
            for horizon in HORIZONS:
                value = _horizon_ic(evaluator, panel, horizon)
                decay[str(horizon)] = None if value != value else round(value, 4)
            residual_ic = _residual_ic(evaluator, panel)
            residual_ic = None if residual_ic != residual_ic else round(residual_ic, 4)
            residual_ratio = (
                round(residual_ic / result["ic60"], 2)
                if residual_ic is not None and result.get("ic60")
                else None
            )
            max_correlation_value = abs(float(result.get("maxcorr_sat", float("nan"))))
            max_correlation = None if max_correlation_value != max_correlation_value else max_correlation_value
            decay_result = _decay_gate(
                decay,
                minimum_half_life=minimum_half_life,
                retention_horizon=retention_horizon,
                minimum_retention=minimum_retention,
            )
            base_passed = bool(
                result.get("gain", 0) > 0.003
                and max_correlation is not None
                and max_correlation < 0.5
                and not result.get("suspect", False)
                and not result.get("style_proxy", False)
                and result.get("cov", 1.0) >= 0.5
            )
            passed = bool(
                base_passed
                and (decay_result["passed"] or not decay_gate_enabled)
            )
            rows.append(
                {
                    "factor": name,
                    "ic60": round(result["ic60"], 4),
                    "gain": round(result["gain"], 4),
                    "maxcorr": round(max_correlation, 2) if max_correlation is not None else None,
                    "maxcorr_sat": round(max_correlation, 2) if max_correlation is not None else None,
                    "redundant_with": redundant_with,
                    "ic_decay": decay,
                    "half_life": decay_result["half_life"],
                    "half_life_censored": decay_result["half_life_censored"],
                    "decay_retention": (
                        round(decay_result["retention"], 4)
                        if decay_result["retention"] is not None
                        else None
                    ),
                    "decay_pass": decay_result["passed"],
                    "resid_ic": residual_ic,
                    "resid_ratio": residual_ratio,
                    "coverage": result.get("cov"),
                    "style_r2": result.get("style_r2"),
                    "suspect": bool(result.get("suspect", False)),
                    "style_proxy": bool(result.get("style_proxy", False)),
                    "base_pass": base_passed,
                    "pass": passed,
                }
            )
        except Exception as exc:
            print(f"{name}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            factor_errors.append({"factor": name, "error": f"{type(exc).__name__}: {exc}"})

    if EXACT_WORKSPACE and factor_errors:
        raise RuntimeError(
            f"exact workspace screen was incomplete: {len(factor_errors)}/"
            f"{len(distinct)} factors failed; first={factor_errors[0]}"
        )
    if EXACT_WORKSPACE and len(rows) != len(distinct):
        raise RuntimeError(
            f"exact workspace screen count mismatch: discovered={len(distinct)}, "
            f"screened={len(rows)}"
        )

    rows.sort(key=lambda row: row["gain"], reverse=True)
    payload = {
        "updated": __import__("time").strftime("%Y-%m-%d %H:%M"),
        "universe": UNIVERSE,
        "evaluator": evaluator_module,
        "scope": "exact_workspace" if EXACT_WORKSPACE else "workspace_root",
        "workspace": EXACT_WORKSPACE or WORKSPACES,
        "factors": rows,
        "n_pass": sum(1 for row in rows if row["pass"]),
        "passed_factors": [row["factor"] for row in rows if row["pass"]],
        "base_ic": round(evaluator.base_ic, 4),
        "screened": len(rows),
        "distinct_total": len(all_factors),
        "horizons": HORIZONS,
        "decay_gate": {
            "enabled": decay_gate_enabled,
            "minimum_half_life": minimum_half_life,
            "retention_horizon": retention_horizon,
            "minimum_retention": minimum_retention,
        },
    }
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(
        f"universe={UNIVERSE}, base_ic={evaluator.base_ic:.4f}, "
        f"passed={payload['n_pass']}/{len(rows)} -> {OUTPUT}",
        flush=True,
    )


if __name__ == "__main__":
    freeze_support()
    main()
