"""Fail-closed RD-Agent production champion tournament.

RD-Agent's LLM feedback decides which experiment continues the research loop.  It
must not decide which factor batch is used by production.  This script consumes
completed batch manifests and the exact multi-seed ``run_model.py`` results, keeps
the feasible Pareto set, and selects one deterministic production champion.

The command is a dry run unless ``--commit`` is supplied.  A commit atomically
rewrites the legacy workspace/factor pointers only after every hard and relative
gate passes, and records the complete decision in ``production_champion.json``.
It never trains a model, reruns a backtest, or calls an LLM.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import statistics
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


WORKSPACE_RE = re.compile(
    r"^(?:D:/rdagent_workspace|Z:/claude/rdagent_workspace)/[0-9a-f]{32}$",
    re.IGNORECASE,
)
OBJECTIVES = ("excess_lo", "excess", "ir", "maxdd")


class PromotionError(RuntimeError):
    """Raised for an invalid or incomplete production-promotion input."""


@dataclass(frozen=True)
class PromotionPolicy:
    """Auditable hard and incumbent-relative promotion thresholds."""

    min_net_excess: float = 0.0
    min_information_ratio: float = 0.25
    max_drawdown_abs: float = 0.15
    min_seeds: int = 3
    min_worst_seed_excess: float = 0.0
    max_seed_excess_std: float = 0.05
    min_excess_improvement: float = 0.0025
    min_worst_seed_improvement: float = 0.0
    information_ratio_tolerance: float = 0.05
    max_drawdown_tolerance: float = 0.01
    required_execution_mode: str = "next_open"


def _finite(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PromotionError(f"{name} is not numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise PromotionError(f"{name} is not finite")
    return number


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise PromotionError(f"required file does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PromotionError(f"could not read valid JSON from {path}: {exc}") from exc


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _unique_strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PromotionError(f"{name} must be a non-empty list")
    items = [str(item).strip() for item in value]
    if any(not item for item in items) or len(set(items)) != len(items):
        raise PromotionError(f"{name} contains an empty or duplicate value")
    return items


def load_batch_manifest(batches_dir: Path, label: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", label):
        raise PromotionError(f"unsafe candidate batch label: {label!r}")
    path = batches_dir / f"{label}.json"
    manifest = _read_json(path)
    if not isinstance(manifest, dict):
        raise PromotionError(f"batch manifest is not an object: {path}")
    if str(manifest.get("label", "")) != label:
        raise PromotionError(f"batch manifest label mismatch: expected {label}")
    workspace = str(manifest.get("workspace", "")).replace("\\", "/")
    if not WORKSPACE_RE.fullmatch(workspace):
        raise PromotionError(f"batch {label} has an unsafe workspace: {workspace!r}")
    factors = _unique_strings(manifest.get("effective_factors"), "effective_factors")
    if manifest.get("test_used_for_selection") is not False:
        raise PromotionError(
            f"batch {label} is not production eligible: test_used_for_selection must be false"
        )
    periods: dict[str, tuple[str, str]] = {}
    for field in ("selection_period", "test_report_period"):
        value = manifest.get(field)
        if not isinstance(value, dict):
            raise PromotionError(f"batch {label} has no {field}")
        start, end = str(value.get("start", ""))[:10], str(value.get("end", ""))[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", end
        ):
            raise PromotionError(f"batch {label} has an invalid {field}")
        if start > end:
            raise PromotionError(f"batch {label} has a reversed {field}")
        periods[field] = (start, end)
    if periods["selection_period"][1] >= periods["test_report_period"][0]:
        raise PromotionError(f"batch {label} selection and report-only test periods overlap")
    fdr_factors = _unique_strings(
        manifest.get("fdr_effective_factors"), "fdr_effective_factors"
    )
    if not set(factors).issubset(fdr_factors):
        raise PromotionError(f"batch {label} effective factors are not a subset of FDR passes")
    exact_gate = manifest.get("exact_screen_gate")
    if not isinstance(exact_gate, dict):
        raise PromotionError(f"batch {label} has no exact-workspace screen gate")
    if exact_gate.get("scope") != "exact_workspace":
        raise PromotionError(f"batch {label} exact screen does not have exact_workspace scope")
    if str(exact_gate.get("universe", "")).strip().lower() != "csi300":
        raise PromotionError(
            f"batch {label} is not a csi300 production candidate; global pointer unchanged"
        )
    screened_workspace = str(exact_gate.get("workspace", "")).replace("\\", "/")
    if not WORKSPACE_RE.fullmatch(screened_workspace):
        raise PromotionError(f"batch {label} exact screen workspace is invalid")
    if screened_workspace.rsplit("/", 1)[-1].casefold() != workspace.rsplit("/", 1)[-1].casefold():
        raise PromotionError(f"batch {label} exact screen workspace identity mismatch")
    allowed = exact_gate.get("passed_factors")
    if allowed is None:
        rows = exact_gate.get("factors")
        if isinstance(rows, list):
            allowed = [row.get("factor") for row in rows if row.get("pass") is True]
    allowed_set = {str(item).strip() for item in (allowed or ()) if str(item).strip()}
    try:
        screened_count = int(exact_gate.get("screened"))
        passed_count = int(exact_gate.get("n_pass"))
    except (TypeError, ValueError) as exc:
        raise PromotionError(f"batch {label} exact screen counts are invalid") from exc
    if passed_count != len(allowed_set) or screened_count < passed_count:
        raise PromotionError(f"batch {label} exact screen counts are inconsistent")
    if not set(factors).issubset(allowed_set):
        raise PromotionError(
            f"batch {label} effective factors are not a subset of its exact-screen pass set"
        )
    return {
        **manifest,
        "workspace": workspace,
        "effective_factors": factors,
        "fdr_effective_factors": fdr_factors,
    }


def result_index(model_results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = model_results.get("results")
    if not isinstance(rows, list):
        raise PromotionError("model_results.json must contain a results list")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key", ""))
        if not key:
            continue
        if key in indexed:
            raise PromotionError(f"model_results.json contains duplicate key {key}")
        indexed[key] = row
    return indexed


def validate_backtest_record(
    record: dict[str, Any],
    *,
    expected_key: str,
    policy: PromotionPolicy,
) -> dict[str, Any]:
    """Validate and normalize a production-grade multi-seed result."""

    if str(record.get("key", "")) != expected_key:
        raise PromotionError(f"backtest key mismatch: expected {expected_key}")
    if record.get("aggregation") != "per_instrument_score_mean":
        raise PromotionError("backtest must use the per-instrument score-mean seed ensemble")
    metrics = {
        name: _finite(record.get(name), name)
        for name in ("excess", "excess_lo", "ir", "maxdd", "rank_ic")
    }
    try:
        n_seeds = int(record.get("n_seeds"))
    except (TypeError, ValueError) as exc:
        raise PromotionError("n_seeds is not an integer") from exc
    seed_rows = record.get("seed_metrics")
    if not isinstance(seed_rows, list) or len(seed_rows) != n_seeds:
        raise PromotionError("seed_metrics must contain exactly n_seeds rows")
    if n_seeds < policy.min_seeds:
        raise PromotionError(f"n_seeds={n_seeds} is below required {policy.min_seeds}")
    normalized_seeds: list[dict[str, Any]] = []
    seed_ids: list[Any] = []
    for position, row in enumerate(seed_rows):
        if not isinstance(row, dict):
            raise PromotionError(f"seed_metrics[{position}] is not an object")
        seed_id = row.get("seed")
        if seed_id in seed_ids:
            raise PromotionError("seed_metrics contains duplicate seed identifiers")
        seed_ids.append(seed_id)
        normalized_seeds.append(
            {
                "seed": seed_id,
                "excess": _finite(row.get("excess"), f"seed[{seed_id}].excess"),
                "ir": _finite(row.get("ir"), f"seed[{seed_id}].ir"),
                "maxdd": _finite(row.get("maxdd"), f"seed[{seed_id}].maxdd"),
                "rank_ic": _finite(row.get("rank_ic"), f"seed[{seed_id}].rank_ic"),
            }
        )
    calculated_lo = min(row["excess"] for row in normalized_seeds)
    if not math.isclose(metrics["excess_lo"], calculated_lo, abs_tol=5e-5):
        raise PromotionError(
            f"excess_lo={metrics['excess_lo']} does not match worst seed {calculated_lo}"
        )
    dispersion = record.get("seed_dispersion")
    if not isinstance(dispersion, dict):
        raise PromotionError("seed_dispersion is missing")
    excess_std = _finite(dispersion.get("excess_std"), "seed_dispersion.excess_std")
    calculated_std = statistics.pstdev(row["excess"] for row in normalized_seeds)
    if not math.isclose(excess_std, calculated_std, abs_tol=5e-5):
        raise PromotionError(
            f"seed excess dispersion {excess_std} does not match seed rows {calculated_std}"
        )
    execution = record.get("execution")
    if not isinstance(execution, dict):
        raise PromotionError("execution contract is missing")
    if execution.get("mode") != policy.required_execution_mode:
        raise PromotionError(
            f"execution mode must be {policy.required_execution_mode}, got {execution.get('mode')!r}"
        )
    if execution.get("only_tradable") is not True:
        raise PromotionError("execution contract must require only_tradable=true")
    participation = _finite(
        execution.get("max_volume_participation"), "max_volume_participation"
    )
    if not 0.0 < participation <= 1.0:
        raise PromotionError("max_volume_participation must be in (0, 1]")
    evaluation = record.get("evaluation")
    if not isinstance(evaluation, dict):
        raise PromotionError("OOS evaluation contract is missing")
    test_start = str(evaluation.get("test_start", ""))
    test_end = str(evaluation.get("test_end", ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", test_start) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", test_end
    ):
        raise PromotionError("OOS evaluation test period is invalid")
    if test_start > test_end:
        raise PromotionError("OOS evaluation test period is reversed")
    if not str(evaluation.get("benchmark", "")).strip():
        raise PromotionError("OOS evaluation benchmark is missing")
    if _finite(evaluation.get("account"), "evaluation.account") <= 0:
        raise PromotionError("OOS evaluation account must be positive")
    costs = evaluation.get("costs")
    if not isinstance(costs, dict):
        raise PromotionError("OOS evaluation cost contract is missing")
    if _finite(costs.get("open_cost"), "open_cost") <= 0:
        raise PromotionError("open_cost must be positive")
    if _finite(costs.get("close_cost"), "close_cost") <= 0:
        raise PromotionError("close_cost must be positive")
    if _finite(costs.get("min_cost"), "min_cost") < 0:
        raise PromotionError("min_cost must be non-negative")
    strategy = evaluation.get("strategy")
    if not isinstance(strategy, dict):
        raise PromotionError("OOS evaluation strategy contract is missing")
    try:
        topk = int(strategy.get("topk"))
        n_drop = int(strategy.get("n_drop"))
    except (TypeError, ValueError) as exc:
        raise PromotionError("OOS strategy topk/n_drop are invalid") from exc
    if topk <= 0 or not 0 <= n_drop <= topk:
        raise PromotionError("OOS strategy topk/n_drop are out of range")
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        raise PromotionError("backtest provenance is missing")
    provenance_workspace = str(provenance.get("workspace", "")).replace("\\", "/")
    if not WORKSPACE_RE.fullmatch(provenance_workspace):
        raise PromotionError("backtest provenance workspace is invalid")
    provenance_factors = _unique_strings(
        provenance.get("effective_factors"), "provenance.effective_factors"
    )
    if str(provenance.get("universe", "")).strip().lower() != "csi300":
        raise PromotionError("backtest provenance universe must be csi300")
    return {
        **metrics,
        "n_seeds": n_seeds,
        "seed_metrics": normalized_seeds,
        "seed_excess_std": excess_std,
        "execution": execution,
        "evaluation": evaluation,
        "provenance": {
            **provenance,
            "workspace": provenance_workspace,
            "effective_factors": provenance_factors,
            "universe": "csi300",
        },
        "updated_at": str(record.get("updated_at", "")),
    }


def absolute_gate(metrics: dict[str, Any], policy: PromotionPolicy) -> list[str]:
    failures: list[str] = []
    if metrics["excess"] < policy.min_net_excess:
        failures.append("net excess return below minimum")
    if metrics["ir"] < policy.min_information_ratio:
        failures.append("information ratio below minimum")
    if metrics["maxdd"] < -policy.max_drawdown_abs:
        failures.append("maximum drawdown exceeds limit")
    if metrics["excess_lo"] < policy.min_worst_seed_excess:
        failures.append("worst-seed net excess return below minimum")
    if metrics["seed_excess_std"] > policy.max_seed_excess_std:
        failures.append("seed excess-return dispersion exceeds limit")
    return failures


def relative_gate(
    candidate: dict[str, Any], incumbent: dict[str, Any], policy: PromotionPolicy
) -> list[str]:
    failures: list[str] = []
    if _sha256_json(candidate["evaluation"]) != _sha256_json(incumbent["evaluation"]):
        failures.append("OOS/cost/strategy contract differs from incumbent")
    if _sha256_json(candidate["execution"]) != _sha256_json(incumbent["execution"]):
        failures.append("execution contract differs from incumbent")
    if candidate["excess"] < incumbent["excess"] + policy.min_excess_improvement:
        failures.append("net excess return does not improve incumbent by required margin")
    if candidate["excess_lo"] < incumbent["excess_lo"] + policy.min_worst_seed_improvement:
        failures.append("worst-seed net excess return regresses versus incumbent")
    if candidate["ir"] < incumbent["ir"] - policy.information_ratio_tolerance:
        failures.append("information ratio regresses beyond tolerance")
    if candidate["maxdd"] < incumbent["maxdd"] - policy.max_drawdown_tolerance:
        failures.append("maximum drawdown regresses beyond tolerance")
    return failures


def _dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    no_worse = all(left[name] >= right[name] for name in OBJECTIVES)
    better = any(left[name] > right[name] for name in OBJECTIVES)
    return no_worse and better


def pareto_frontier(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = list(candidates)
    return [
        row
        for row in rows
        if not any(other is not row and _dominates(other["metrics"], row["metrics"]) for other in rows)
    ]


def select_winner(candidates: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Select deterministically, prioritizing the conservative seed outcome."""

    rows = list(candidates)
    if not rows:
        return None
    rows.sort(
        key=lambda row: (
            -row["metrics"]["excess_lo"],
            -row["metrics"]["excess"],
            -row["metrics"]["ir"],
            -row["metrics"]["maxdd"],
            row["metrics"]["seed_excess_std"],
            row["label"],
        )
    )
    return rows[0]


def build_decision(
    *,
    labels: list[str],
    batches_dir: Path,
    model_results_path: Path,
    incumbent_key: str,
    policy: PromotionPolicy,
) -> dict[str, Any]:
    model_results = _read_json(model_results_path)
    indexed = result_index(model_results)
    incumbent_raw = indexed.get(incumbent_key)
    if incumbent_raw is None:
        raise PromotionError(f"incumbent result is missing: {incumbent_key}")
    incumbent = validate_backtest_record(
        incumbent_raw, expected_key=incumbent_key, policy=policy
    )
    candidates: list[dict[str, Any]] = []
    for label in sorted(set(labels)):
        manifest = load_batch_manifest(batches_dir, label)
        key = f"{label}::lgb"
        raw = indexed.get(key)
        if raw is None:
            candidates.append(
                {"label": label, "eligible": False, "failures": [f"missing result {key}"]}
            )
            continue
        try:
            metrics = validate_backtest_record(raw, expected_key=key, policy=policy)
            failures = absolute_gate(metrics, policy) + relative_gate(metrics, incumbent, policy)
            if metrics["provenance"]["workspace"].casefold() != manifest["workspace"].casefold():
                failures.append("backtest workspace provenance does not match batch manifest")
            if set(metrics["provenance"]["effective_factors"]) != set(
                manifest["effective_factors"]
            ):
                failures.append("backtest factor provenance does not match batch manifest")
        except PromotionError as exc:
            metrics = None
            failures = [str(exc)]
        candidates.append(
            {
                "label": label,
                "workspace": manifest["workspace"],
                "effective_factors": manifest["effective_factors"],
                "batch_manifest_sha256": _sha256_json(manifest),
                "metrics": metrics,
                "eligible": not failures,
                "failures": failures,
            }
        )
    eligible = [row for row in candidates if row.get("eligible")]
    frontier = pareto_frontier(eligible)
    winner = select_winner(frontier)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "schema_version": 1,
        "kind": "rdagent_production_promotion_decision",
        "generated_at": now,
        "policy": asdict(policy),
        "incumbent": {"key": incumbent_key, "metrics": incumbent},
        "candidate_count": len(candidates),
        "eligible_count": len(eligible),
        "pareto_labels": sorted(row["label"] for row in frontier),
        "selected_label": winner["label"] if winner else None,
        "promote": winner is not None,
        "selection_order": (
            "max worst-seed net excess, then ensemble net excess, IR, drawdown, "
            "lower seed dispersion, label"
        ),
        "candidates": candidates,
    }


def validate_live_incumbent(
    decision: dict[str, Any], workspace_pointer: Path, factor_pointer: Path
) -> None:
    """Require the incumbent result to describe the exact live legacy pointers."""

    try:
        provenance = decision["incumbent"]["metrics"]["provenance"]
    except (KeyError, TypeError) as exc:
        raise PromotionError("incumbent decision has no backtest provenance") from exc
    try:
        live_workspace = workspace_pointer.read_text(encoding="utf-8-sig").strip().replace(
            "\\", "/"
        )
    except OSError as exc:
        raise PromotionError(f"could not read live workspace pointer: {exc}") from exc
    live_factors = _unique_strings(_read_json(factor_pointer), "live effective factors")
    if live_workspace.casefold() != provenance["workspace"].casefold():
        raise PromotionError("incumbent backtest workspace does not match live workspace pointer")
    if set(live_factors) != set(provenance["effective_factors"]):
        raise PromotionError("incumbent backtest factors do not match live factor pointer")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def commit_decision(
    decision: dict[str, Any],
    *,
    champion_path: Path,
    workspace_pointer: Path,
    factor_pointer: Path,
) -> dict[str, Any]:
    label = decision.get("selected_label")
    winner = next(
        (row for row in decision.get("candidates", ()) if row.get("label") == label), None
    )
    if not decision.get("promote") or winner is None:
        raise PromotionError("decision has no eligible promotion winner")
    lock_path = champion_path.with_suffix(champion_path.suffix + ".lock")
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise PromotionError(f"promotion lock already exists: {lock_path}") from exc
    try:
        os.close(lock_fd)
        generation = hashlib.sha256(
            f"{decision['generated_at']}|{label}|{winner['batch_manifest_sha256']}".encode("utf-8")
        ).hexdigest()
        champion = {
            **decision,
            "committed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "generation": generation,
            "champion": {
                "label": label,
                "workspace": winner["workspace"],
                "effective_factors": winner["effective_factors"],
                "metrics": winner["metrics"],
                "batch_manifest_sha256": winner["batch_manifest_sha256"],
            },
        }
        # The watcher serializes training and prediction.  Write the canonical
        # factors first, workspace pointer second, and the auditable state last.
        # A consumer that requires transaction identity should read champion state.
        _atomic_write(
            factor_pointer,
            json.dumps(winner["effective_factors"], ensure_ascii=False, indent=2) + "\n",
        )
        _atomic_write(workspace_pointer, winner["workspace"] + "\n")
        _atomic_write(champion_path, json.dumps(champion, ensure_ascii=False, indent=2) + "\n")
        return champion
    finally:
        lock_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-batch", action="append", required=True)
    parser.add_argument("--batches-dir", type=Path, default=Path("C:/rdagent/final/batches"))
    parser.add_argument("--model-results", type=Path, default=Path("C:/rdagent/model_results.json"))
    parser.add_argument(
        "--incumbent-key",
        help=(
            "explicit model_results key; otherwise use the committed champion label, "
            "falling back to default::lgb for first bootstrap"
        ),
    )
    parser.add_argument(
        "--champion-state", type=Path, default=Path("C:/rdagent/final/production_champion.json")
    )
    parser.add_argument("--workspace-pointer", type=Path, default=Path("C:/rdagent/sota_workspace.txt"))
    parser.add_argument(
        "--factor-pointer", type=Path, default=Path("C:/rdagent/final/effective_factors.json")
    )
    parser.add_argument("--decision-output", type=Path)
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--min-net-excess", type=float, default=0.0)
    parser.add_argument("--min-ir", type=float, default=0.25)
    parser.add_argument("--max-drawdown-abs", type=float, default=0.15)
    parser.add_argument("--min-seeds", type=int, default=3)
    parser.add_argument("--min-worst-seed-excess", type=float, default=0.0)
    parser.add_argument("--max-seed-excess-std", type=float, default=0.05)
    parser.add_argument("--min-excess-improvement", type=float, default=0.0025)
    parser.add_argument("--min-worst-seed-improvement", type=float, default=0.0)
    parser.add_argument("--ir-tolerance", type=float, default=0.05)
    parser.add_argument("--max-drawdown-tolerance", type=float, default=0.01)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    policy = PromotionPolicy(
        min_net_excess=args.min_net_excess,
        min_information_ratio=args.min_ir,
        max_drawdown_abs=args.max_drawdown_abs,
        min_seeds=args.min_seeds,
        min_worst_seed_excess=args.min_worst_seed_excess,
        max_seed_excess_std=args.max_seed_excess_std,
        min_excess_improvement=args.min_excess_improvement,
        min_worst_seed_improvement=args.min_worst_seed_improvement,
        information_ratio_tolerance=args.ir_tolerance,
        max_drawdown_tolerance=args.max_drawdown_tolerance,
    )
    try:
        incumbent_key = args.incumbent_key
        if not incumbent_key and args.champion_state.exists():
            state = _read_json(args.champion_state)
            try:
                incumbent_label = str(state["champion"]["label"])
            except (KeyError, TypeError) as exc:
                raise PromotionError(
                    f"invalid committed champion state: {args.champion_state}"
                ) from exc
            incumbent_key = f"{incumbent_label}::lgb"
        incumbent_key = incumbent_key or "default::lgb"
        decision = build_decision(
            labels=args.candidate_batch,
            batches_dir=args.batches_dir,
            model_results_path=args.model_results,
            incumbent_key=incumbent_key,
            policy=policy,
        )
        validate_live_incumbent(
            decision,
            workspace_pointer=args.workspace_pointer,
            factor_pointer=args.factor_pointer,
        )
        if args.decision_output is not None:
            _atomic_write(
                args.decision_output,
                json.dumps(decision, ensure_ascii=False, indent=2) + "\n",
            )
        if args.commit:
            commit_decision(
                decision,
                champion_path=args.champion_state,
                workspace_pointer=args.workspace_pointer,
                factor_pointer=args.factor_pointer,
            )
    except PromotionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(decision, ensure_ascii=False, sort_keys=True))
    return 0 if decision["promote"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
