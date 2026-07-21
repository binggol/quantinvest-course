"""Run a resumable coarse Advisor Pro portfolio-parameter sweep.

The point-in-time ranking cache is loaded once and reused by every portfolio
specification.  Each completed run is checkpointed independently so chunks can
be resumed without repeating successful work.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import pickle
import sys
import tempfile
import traceback
from typing import Any, Iterable, Mapping, Sequence


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


DEFAULT_TOPNS = (8, 10, 15, 20, 30)
DEFAULT_FREQUENCIES = (5, 10, 20, 60)
DEFAULT_REPLACEMENT_RATIOS = (0.10, 0.20, 0.40, 1.00)
DEFAULT_ACCOUNTS = (100_000_000.0,)
CHECKPOINT_SCHEMA_VERSION = 1
RESULT_DROP_KEYS = frozenset({"signals", "daily_path"})


@dataclass(frozen=True)
class SweepRun:
    """One unique, effective portfolio configuration."""

    run_id: str
    spec: Mapping[str, Any]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def stable_run_id(spec: Mapping[str, Any]) -> str:
    """Return a readable ID whose digest covers every result-affecting setting."""

    digest = hashlib.sha256(_canonical_json(spec).encode("utf-8")).hexdigest()[:16]
    ratio_basis_points = round(float(spec["replacement_ratio"]) * 10_000)
    mode = "full" if spec["rebalance_mode"] == "target_weight" else "replace"
    prefix = (
        f"n{int(spec['portfolio_topn'])}"
        f"-f{int(spec['frequency_days'])}"
        f"-o{int(spec['frequency_offset'])}"
        f"-r{ratio_basis_points:05d}"
        f"-{mode}"
    )
    return f"{prefix}-{digest}"


def parse_int_list(raw: str, *, name: str, minimum: int = 1) -> tuple[int, ...]:
    try:
        values = sorted({int(item.strip()) for item in raw.split(",") if item.strip()})
    except ValueError as exc:
        raise ValueError(f"{name} must be a comma-separated integer list") from exc
    if not values or any(value < minimum for value in values):
        raise ValueError(f"{name} values must be >= {minimum}")
    return tuple(values)


def parse_float_list(raw: str, *, name: str) -> tuple[float, ...]:
    try:
        values = sorted({float(item.strip()) for item in raw.split(",") if item.strip()})
    except ValueError as exc:
        raise ValueError(f"{name} must be a comma-separated number list") from exc
    if not values or any(not math.isfinite(value) or value <= 0 for value in values):
        raise ValueError(f"{name} values must be finite and positive")
    return tuple(values)


def parse_ratio_list(raw: str) -> tuple[float, ...]:
    values: set[float] = set()
    try:
        for item in raw.split(","):
            text = item.strip()
            if not text:
                continue
            explicit_percent = text.endswith("%")
            value = float(text[:-1] if explicit_percent else text)
            if explicit_percent or value > 1:
                value /= 100.0
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError
            values.add(round(value, 10))
    except ValueError as exc:
        raise ValueError(
            "replacement ratios must be in (0, 1], optionally written as percentages"
        ) from exc
    if not values:
        raise ValueError("replacement ratios cannot be empty")
    return tuple(sorted(values))


def _offsets_for_frequency(raw: str, frequency_days: int) -> tuple[int, ...]:
    step = max(1, round(int(frequency_days) / 5))
    if raw.strip().lower() == "all":
        return tuple(range(step))
    offsets = parse_int_list(raw, name="offsets", minimum=0)
    invalid = [offset for offset in offsets if offset >= step]
    if invalid:
        raise ValueError(
            f"offsets {invalid} are invalid for {frequency_days}-day frequency; "
            f"expected 0..{step - 1}"
        )
    return offsets


def cache_identity(payload: Mapping[str, Any], path: Path) -> str:
    signature = payload.get("signature")
    if isinstance(signature, Mapping):
        source: Mapping[str, Any] = signature
    else:
        stat = path.stat()
        source = {
            "resolved_path": str(path.expanduser().resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }
    return hashlib.sha256(_canonical_json(source).encode("utf-8")).hexdigest()


def generate_grid(
    *,
    topns: Iterable[int] = DEFAULT_TOPNS,
    frequencies: Iterable[int] = DEFAULT_FREQUENCIES,
    replacement_ratios: Iterable[float] = DEFAULT_REPLACEMENT_RATIOS,
    accounts: Iterable[float] = DEFAULT_ACCOUNTS,
    offsets: str = "0",
    run_parameters: Mapping[str, Any] | None = None,
    signal_cache_identity: str = "unspecified",
) -> list[SweepRun]:
    """Build a deterministic grid, deduplicating equal effective replacement caps."""

    parameters = dict(run_parameters or {})
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for topn in sorted({int(value) for value in topns}):
        if topn < 1:
            raise ValueError("topn values must be positive")
        for frequency in sorted({int(value) for value in frequencies}):
            if frequency < 1:
                raise ValueError("frequencies must be positive")
            for offset in _offsets_for_frequency(offsets, frequency):
                for account in sorted({float(value) for value in accounts}):
                    if not math.isfinite(account) or account <= 0:
                        raise ValueError("accounts must be finite and positive")
                    for requested_ratio in sorted({float(value) for value in replacement_ratios}):
                        if not math.isfinite(requested_ratio) or not 0 < requested_ratio <= 1:
                            raise ValueError("replacement ratios must be in (0, 1]")
                        if math.isclose(requested_ratio, 1.0):
                            max_replacements = None
                            mode = "target_weight"
                        else:
                            max_replacements = min(
                                topn, max(1, math.ceil(topn * requested_ratio))
                            )
                            mode = "replace_only"
                        effective_key = (
                            topn,
                            frequency,
                            offset,
                            account,
                            max_replacements,
                            mode,
                        )
                        # Sorted ratios make collision resolution independent of CLI order.
                        unique.setdefault(
                            effective_key,
                            {
                                "portfolio_topn": topn,
                                "frequency_days": frequency,
                                "frequency_offset": offset,
                                "replacement_ratio": requested_ratio,
                                "effective_replacement_ratio": (
                                    1.0
                                    if max_replacements is None
                                    else max_replacements / topn
                                ),
                                "max_replacements": max_replacements,
                                "rebalance_mode": mode,
                                "account": account,
                                "run_parameters": parameters,
                                "signal_cache_identity": signal_cache_identity,
                            },
                        )
    runs = [SweepRun(stable_run_id(spec), spec) for spec in unique.values()]
    return sorted(runs, key=lambda run: run.run_id)


def select_chunk(
    runs: Sequence[SweepRun], *, chunk_index: int, chunk_count: int
) -> list[SweepRun]:
    if chunk_count < 1:
        raise ValueError("chunk-count must be positive")
    if not 0 <= chunk_index < chunk_count:
        raise ValueError("chunk-index must be between 0 and chunk-count - 1")

    def bucket(run: SweepRun) -> int:
        digest = hashlib.sha256(run.run_id.encode("ascii")).digest()
        return int.from_bytes(digest[:8], "big") % chunk_count

    return [run for run in runs if bucket(run) == chunk_index]


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat") and not isinstance(value, (str, bytes)):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def compact_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Remove per-signal and per-day payloads while preserving audit metrics."""

    def compact(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): compact(item)
                for key, item in value.items()
                if str(key) not in RESULT_DROP_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [compact(item) for item in value]
        return _json_safe(value)

    return compact(result)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(
                _json_safe(payload),
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def load_signal_cache(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        payload = pickle.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ValueError(f"invalid point-in-time signal cache: {path}")
    if not payload["records"]:
        raise ValueError(f"point-in-time signal cache has no records: {path}")
    incomplete = sum(
        not isinstance(record, Mapping)
        or not isinstance(record.get("ranked_codes"), (list, tuple))
        for record in payload["records"]
    )
    if incomplete:
        raise ValueError(
            f"signal cache lacks complete ranked_codes in {incomplete} records; "
            "rebuild it with the current backtest script before sweeping TopN"
        )
    return payload


def _successful_checkpoint(path: Path, run_id: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if (
        isinstance(payload, dict)
        and payload.get("schema_version") == CHECKPOINT_SCHEMA_VERSION
        and payload.get("run_id") == run_id
        and payload.get("status") == "success"
        and isinstance(payload.get("result"), dict)
    ):
        return payload
    return None


def _run_one(
    *,
    run: SweepRun,
    args: argparse.Namespace,
    records: list[dict[str, Any]],
    backtest_module: Any,
) -> dict[str, Any]:
    started_at = _now()
    spec = run.spec
    try:
        portfolio_spec = backtest_module.PortfolioSpec(
            portfolio_topn=spec["portfolio_topn"],
            max_replacements=spec["max_replacements"],
            rebalance_mode=spec["rebalance_mode"],
            account=spec["account"],
        )
        run_args = argparse.Namespace(**vars(args))
        run_args.account = float(spec["account"])
        run_args.portfolio_topn = int(spec["portfolio_topn"])
        run_args.max_replacements = spec["max_replacements"]
        run_args.rebalance_mode = spec["rebalance_mode"]
        result = backtest_module.run_frequency(
            run_args,
            records,
            int(spec["frequency_days"]),
            frequency_offset=int(spec["frequency_offset"]),
            spec=portfolio_spec,
        )
        if not isinstance(result, Mapping):
            raise TypeError("run_frequency must return a mapping")
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": run.run_id,
            "status": "success",
            "spec": dict(spec),
            "started_at": started_at,
            "completed_at": _now(),
            "result": compact_result(result),
        }
    except Exception as exc:  # Continue the remaining independent grid runs.
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": run.run_id,
            "status": "failed",
            "spec": dict(spec),
            "started_at": started_at,
            "completed_at": _now(),
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        }


def _chunk_output_path(path: Path, *, chunk_index: int, chunk_count: int) -> Path:
    if chunk_count == 1:
        return path
    suffix = path.suffix or ".json"
    return path.with_name(
        f"{path.stem}.chunk-{chunk_index:03d}-of-{chunk_count:03d}{suffix}"
    )


def execute_sweep(
    args: argparse.Namespace,
    *,
    qlib_module: Any | None = None,
    backtest_module: Any | None = None,
    cache_loader: Any = load_signal_cache,
) -> dict[str, Any]:
    """Initialize/load once, execute the selected chunk, and checkpoint each run."""

    if qlib_module is None:
        qlib_module = importlib.import_module("qlib")
    if backtest_module is None:
        backtest_module = importlib.import_module(
            "scripts.backtest_advisor_pro_frequency"
        )

    qlib_module.init(provider_uri=str(Path(args.qlib_data)), region="cn")
    signal_cache_path = Path(args.signal_cache)
    cached = cache_loader(signal_cache_path)
    records = cached["records"]
    identity = cache_identity(cached, signal_cache_path)

    topns = parse_int_list(args.portfolio_topns, name="portfolio-topns")
    frequencies = parse_int_list(args.frequencies, name="frequencies")
    ratios = parse_ratio_list(args.replacement_ratios)
    accounts = parse_float_list(args.accounts, name="accounts")
    run_parameters = {
        "backtest_end": args.backtest_end,
        "rank_buffer": args.rank_buffer,
        "risk_degree": args.risk_degree,
        "retry_days": args.retry_days,
        "liquidation_retry_days": args.liquidation_retry_days,
        "residual_policy": args.residual_policy,
        "commission": args.commission,
        "max_volume_participation": args.max_volume_participation,
        "impact_cost": args.impact_cost,
        "hedge_yearly_cost": args.hedge_yearly_cost,
    }
    all_runs = generate_grid(
        topns=topns,
        frequencies=frequencies,
        replacement_ratios=ratios,
        accounts=accounts,
        offsets=args.offsets,
        run_parameters=run_parameters,
        signal_cache_identity=identity,
    )
    selected = select_chunk(
        all_runs, chunk_index=args.chunk_index, chunk_count=args.chunk_count
    )
    checkpoint_dir = (
        Path(args.checkpoint_dir)
        if args.checkpoint_dir
        else Path(str(Path(args.out)) + ".checkpoints")
    )
    checkpoints: list[dict[str, Any]] = []
    resumed_count = 0
    for run in selected:
        checkpoint_path = checkpoint_dir / f"{run.run_id}.json"
        checkpoint = (
            _successful_checkpoint(checkpoint_path, run.run_id) if args.resume else None
        )
        if checkpoint is not None:
            resumed_count += 1
        else:
            checkpoint = _run_one(
                run=run,
                args=args,
                records=records,
                backtest_module=backtest_module,
            )
            atomic_write_json(checkpoint_path, checkpoint)
        checkpoints.append(checkpoint)

    failed_count = sum(item["status"] != "success" for item in checkpoints)
    summary = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "generated_at": _now(),
        "status": "failed" if failed_count else "done",
        "chunk": {
            "index": args.chunk_index,
            "count": args.chunk_count,
            "total_grid": len(all_runs),
            "selected": len(selected),
        },
        "signal_cache": {
            "path": str(signal_cache_path),
            "identity": identity,
            "signature": cached.get("signature"),
            "record_count": len(records),
        },
        "checkpoint_dir": str(checkpoint_dir),
        "success_count": len(checkpoints) - failed_count,
        "failed_count": failed_count,
        "resumed_count": resumed_count,
        "runs": checkpoints,
    }
    output_path = _chunk_output_path(
        Path(args.out), chunk_index=args.chunk_index, chunk_count=args.chunk_count
    )
    atomic_write_json(output_path, summary)
    summary["out"] = str(output_path)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qlib-data", default=r"C:\qlib_data\cn_data")
    parser.add_argument(
        "--signal-cache", default="data/advisor_pro_weekly_signal_cache.pkl"
    )
    parser.add_argument("--out", default="data/advisor_pro_portfolio_sweep.json")
    parser.add_argument("--checkpoint-dir", default=None)
    parser.add_argument(
        "--portfolio-topns", "--topns", dest="portfolio_topns", default="8,10,15,20,30"
    )
    parser.add_argument("--frequencies", default="5,10,20,60")
    parser.add_argument("--replacement-ratios", default="10%,20%,40%,100%")
    parser.add_argument("--accounts", "--account", dest="accounts", default="100000000")
    parser.add_argument(
        "--offsets",
        default="0",
        help="Comma-separated zero-based frequency offsets, or 'all'",
    )
    parser.add_argument("--chunk-index", type=int, default=0)
    parser.add_argument("--chunk-count", type=int, default=1)
    parser.add_argument("--resume", action="store_true")

    parser.add_argument("--backtest-end", default="2026-05-18")
    parser.add_argument("--rank-buffer", type=int, default=0)
    parser.add_argument("--risk-degree", type=float, default=0.95)
    parser.add_argument("--retry-days", type=int, default=5)
    parser.add_argument("--liquidation-retry-days", type=int, default=30)
    parser.add_argument(
        "--residual-policy", choices=("strict", "mark_to_market"), default="strict"
    )
    parser.add_argument("--commission", type=float, default=0.0003)
    parser.add_argument("--max-volume-participation", type=float, default=0.10)
    parser.add_argument("--impact-cost", type=float, default=0.10)
    parser.add_argument("--hedge-yearly-cost", type=float, default=0.01)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = execute_sweep(args)
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": {"type": type(exc).__name__, "message": str(exc)}},
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                key: summary[key]
                for key in (
                    "status",
                    "out",
                    "checkpoint_dir",
                    "success_count",
                    "failed_count",
                    "resumed_count",
                    "chunk",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if summary["failed_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
