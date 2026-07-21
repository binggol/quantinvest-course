"""Resolve the evaluated SOTA workspace from a completed RD-Agent factor trace.

RD-Agent's factor runner caches an evaluated experiment, but its generic cache-hit
handler only copies ``result`` onto the newly generated experiment.  Consequently,
the session can point at a fresh template-only workspace while the evaluated
workspace still lives in the runner's pickle cache.  This resolver validates the
session workspace first and, when necessary, follows the exact runner cache key to
the evaluated workspace.  It never reruns factor code or calls an LLM.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import math
import pickle
import re
import sys
from pathlib import Path
from typing import Any


SESSION_NAME = re.compile(r"^(\d+)_")
REQUIRED_RESULT_FILES = ("qlib_res.csv", "ret.pkl")
RESEARCH_OBJECTIVES = (
    "net_annualized_return",
    "net_information_ratio",
    "max_drawdown",
    "ic",
    "rank_ic",
)


def list_session_files(log_path: Path) -> list[tuple[int, int, Path]]:
    """Return session snapshots ordered by loop and step number."""
    session_root = log_path / "__session__"
    if not session_root.is_dir():
        raise RuntimeError(f"no __session__ directory under {log_path}")

    sessions: list[tuple[int, int, Path]] = []
    for loop_dir in session_root.iterdir():
        if not (loop_dir.is_dir() and loop_dir.name.isdigit()):
            continue
        for step in loop_dir.iterdir():
            match = SESSION_NAME.match(step.name)
            if match and step.is_file():
                sessions.append((int(loop_dir.name), int(match.group(1)), step))
    sessions.sort(key=lambda item: (item[0], item[1]))
    if not sessions:
        raise RuntimeError(f"no session snapshots under {session_root}")
    return sessions


def resolve_sota_experiment(trace: Any) -> Any:
    """Resolve the accepted SOTA experiment, with a history fallback."""
    experiment = None
    getter = getattr(trace, "get_sota_experiment", None)
    if callable(getter):
        try:
            experiment = getter()
        except Exception as exc:  # noqa: BLE001 - history remains authoritative fallback
            print(f"WARN: get_sota_experiment failed: {exc}", file=sys.stderr)

    if experiment is None:
        for entry in reversed(getattr(trace, "hist", [])):
            try:
                candidate, feedback = entry[0], entry[1]
            except (IndexError, TypeError):
                continue
            if decision_is_accepted(getattr(feedback, "decision", False)):
                experiment = candidate
                break

    if experiment is None or getattr(experiment, "experiment_workspace", None) is None:
        raise RuntimeError("could not resolve an accepted SOTA experiment from the trace")
    return experiment


def decision_is_accepted(value: Any) -> bool:
    """Interpret RD-Agent feedback decisions without treating ``"no"`` as true."""

    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "accept", "accepted"}


def accepted_experiments(trace: Any) -> list[tuple[int, Any]]:
    """Return every accepted research experiment in trace order.

    RD-Agent keeps only one continuation SOTA, but production selection needs all
    accepted experiments.  Keeping this enumeration separate prevents the LLM's
    single-metric continuation decision from silently becoming a production
    promotion decision.
    """

    accepted: list[tuple[int, Any]] = []
    for history_index, entry in enumerate(getattr(trace, "hist", ()) or ()):
        try:
            experiment, feedback = entry[0], entry[1]
        except (IndexError, TypeError):
            continue
        if decision_is_accepted(getattr(feedback, "decision", False)):
            accepted.append((history_index, experiment))
    return accepted


def validate_evaluated_workspace(path_value: Any) -> tuple[Path | None, str]:
    """Validate the minimal artifact contract consumed by factor_analysis.py."""
    if path_value is None or not str(path_value).strip():
        return None, "workspace path is empty"
    workspace = Path(path_value)
    if not workspace.is_dir():
        return None, f"workspace does not exist: {workspace}"

    configs = [path for path in workspace.glob("mlruns/*/*/artifacts/config") if path.is_file()]
    if len(configs) != 1:
        return None, f"expected exactly one evaluated config, found {len(configs)}"
    for filename in REQUIRED_RESULT_FILES:
        artifact = workspace / filename
        if not artifact.is_file() or artifact.stat().st_size <= 0:
            return None, f"missing or empty {filename}"
    return workspace, "ok"


def results_equivalent(left: Any, right: Any) -> bool:
    """Compare cached and session results without importing pandas eagerly."""
    if left is right:
        return True
    if left is None or right is None:
        return False

    equals = getattr(left, "equals", None)
    if callable(equals):
        try:
            return bool(equals(right))
        except Exception:  # noqa: BLE001 - fall through to scalar/container comparison
            pass
    try:
        comparison = left == right
        if isinstance(comparison, bool):
            return comparison
        all_method = getattr(comparison, "all", None)
        if callable(all_method):
            return bool(all_method())
    except Exception:  # noqa: BLE001 - incomparable result types are a mismatch
        pass
    return False


def experiment_task_signatures(experiment: Any) -> tuple[str, ...]:
    """Return the task inputs used by ``CachedRunner.get_cache_key``."""
    tasks: list[Any] = []
    for based_experiment in getattr(experiment, "based_experiments", ()) or ():
        tasks.extend(getattr(based_experiment, "sub_tasks", ()) or ())
    tasks.extend(getattr(experiment, "sub_tasks", ()) or ())

    signatures: list[str] = []
    for task in tasks:
        task_information = getattr(task, "get_task_information", None)
        if not callable(task_information):
            raise RuntimeError("experiment contains a task without get_task_information()")
        signatures.append(str(task_information()))
    return tuple(signatures)


def runner_cache_file(loop: Any, experiment: Any, cache_root: Path | None = None) -> Path:
    """Return the exact cache file used by ``cache_with_pickle`` for this run."""
    runner = getattr(loop, "runner", None)
    develop = getattr(runner, "develop", None)
    get_cache_key = getattr(runner, "get_cache_key", None)
    if not callable(develop) or not callable(get_cache_key):
        raise RuntimeError("loaded loop does not expose a cache-aware runner")

    cache_key = get_cache_key(experiment)
    if not isinstance(cache_key, str) or not cache_key:
        raise RuntimeError("runner returned an empty cache key")

    if cache_root is None:
        from rdagent.core.conf import RD_AGENT_SETTINGS

        cache_root = Path(RD_AGENT_SETTINGS.pickle_cache_folder_path_str)
    function_folder = f"{develop.__module__}.{develop.__name__}"
    return cache_root / function_folder / f"{cache_key}.pkl"


def resolve_evaluated_workspace(
    loop: Any,
    experiment: Any,
    cache_root: Path | None = None,
) -> tuple[Path, str]:
    """Resolve a validated evaluated workspace from the session or its cache."""
    session_path = getattr(experiment.experiment_workspace, "workspace_path", None)
    workspace, reason = validate_evaluated_workspace(session_path)
    if workspace is not None:
        return workspace, "session"

    cache_file = runner_cache_file(loop, experiment, cache_root=cache_root)
    if not cache_file.is_file():
        raise RuntimeError(
            f"session workspace is not evaluated ({reason}); runner cache is missing: {cache_file}"
        )
    try:
        with cache_file.open("rb") as stream:
            cached_experiment = pickle.load(stream)  # noqa: S301 - trusted local RD-Agent cache
    except Exception as exc:  # noqa: BLE001 - report corrupt/incompatible cache cleanly
        raise RuntimeError(f"could not load runner cache {cache_file}: {exc}") from exc

    if experiment_task_signatures(experiment) != experiment_task_signatures(cached_experiment):
        raise RuntimeError(f"runner cache tasks do not match the accepted session experiment: {cache_file}")
    if not results_equivalent(
        getattr(experiment, "result", None),
        getattr(cached_experiment, "result", None),
    ):
        raise RuntimeError(f"runner cache result does not match the accepted session result: {cache_file}")

    cached_workspace_obj = getattr(cached_experiment, "experiment_workspace", None)
    cached_path = getattr(cached_workspace_obj, "workspace_path", None)
    workspace, cached_reason = validate_evaluated_workspace(cached_path)
    if workspace is None:
        raise RuntimeError(f"runner cache workspace is not evaluated ({cached_reason}): {cached_path}")
    return workspace, "runner-cache"


def _finite_metric(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid {name} in qlib_res.csv: {value!r}") from exc
    if not math.isfinite(number):
        raise RuntimeError(f"non-finite {name} in qlib_res.csv")
    return number


def read_workspace_metrics(workspace: Path) -> dict[str, float]:
    """Read the deterministic research metrics used for Pareto preservation."""

    result_path = workspace / "qlib_res.csv"
    raw: dict[str, str] = {}
    with result_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.reader(stream):
            if len(row) >= 2 and row[0].strip():
                raw[row[0].strip()] = row[1].strip()
    names = {
        "net_annualized_return": "1day.excess_return_with_cost.annualized_return",
        "net_information_ratio": "1day.excess_return_with_cost.information_ratio",
        "max_drawdown": "1day.excess_return_with_cost.max_drawdown",
        "ic": "IC",
        "rank_ic": "Rank IC",
    }
    missing = [source for source in names.values() if source not in raw]
    if missing:
        raise RuntimeError(f"qlib_res.csv is missing required metrics: {', '.join(missing)}")
    return {
        target: _finite_metric(raw[source], source)
        for target, source in names.items()
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dominates(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Return true when left is no worse on every research objective."""

    left_metrics = left["metrics"]
    right_metrics = right["metrics"]
    no_worse = all(left_metrics[name] >= right_metrics[name] for name in RESEARCH_OBJECTIVES)
    strictly_better = any(left_metrics[name] > right_metrics[name] for name in RESEARCH_OBJECTIVES)
    return no_worse and strictly_better


def mark_pareto_candidates(candidates: list[dict[str, Any]]) -> None:
    """Mark the non-dominated accepted research candidates in place."""

    for candidate in candidates:
        candidate["pareto_research_candidate"] = not any(
            other is not candidate and _dominates(other, candidate)
            for other in candidates
        )


def build_accepted_manifest(loop: Any, log_path: Path) -> dict[str, Any]:
    """Resolve all accepted workspaces and preserve their Pareto frontier.

    Resolution failures are retained as explicit errors rather than silently
    disappearing.  At least one usable candidate is required by the CLI.
    """

    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for history_index, experiment in accepted_experiments(loop.trace):
        try:
            workspace, source = resolve_evaluated_workspace(loop, experiment)
            metrics = read_workspace_metrics(workspace)
            task_signatures = experiment_task_signatures(experiment)
            identity_material = json.dumps(
                {
                    "workspace": str(workspace).replace("\\", "/"),
                    "tasks": task_signatures,
                    "qlib_res_sha256": _sha256(workspace / "qlib_res.csv"),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            candidate_id = hashlib.sha256(identity_material).hexdigest()
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            candidates.append(
                {
                    "candidate_id": candidate_id,
                    "history_index": history_index,
                    "workspace": str(workspace).replace("\\", "/"),
                    "workspace_source": source,
                    "task_signatures": list(task_signatures),
                    "metrics": metrics,
                    "artifacts": {
                        "qlib_res_sha256": _sha256(workspace / "qlib_res.csv"),
                        "ret_sha256": _sha256(workspace / "ret.pkl"),
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001 - manifest must retain every failure
            errors.append({"history_index": history_index, "error": str(exc)})
    mark_pareto_candidates(candidates)
    return {
        "schema_version": 1,
        "kind": "rdagent_accepted_research_candidates",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "trace": str(log_path).replace("\\", "/"),
        "accepted_history_count": len(accepted_experiments(loop.trace)),
        "resolved_candidate_count": len(candidates),
        "pareto_candidate_count": sum(
            bool(candidate["pareto_research_candidate"]) for candidate in candidates
        ),
        "production_ready": False,
        "production_blocker": (
            "Research metrics do not include the independent multi-seed production gate; "
            "run exact screening, factor analysis and production backtests before promotion."
        ),
        "candidates": candidates,
        "resolution_errors": errors,
    }


def load_latest_loop(log_path: Path) -> Any:
    """Load the final session snapshot without checking it out or mutating the trace."""
    latest = list_session_files(log_path)[-1][2]
    from rdagent.app.qlib_rd_loop.factor import FactorRDLoop

    return FactorRDLoop.load(str(latest), checkout=False, replace_timer=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve RD-Agent research workspaces without mutating the trace."
    )
    parser.add_argument("log_path", type=Path)
    parser.add_argument(
        "--accepted-manifest",
        type=Path,
        help="write every accepted/Pareto research candidate to this JSON file",
    )
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        loop = load_latest_loop(args.log_path)
        if args.accepted_manifest is not None:
            manifest = build_accepted_manifest(loop, args.log_path)
            if not manifest["candidates"]:
                raise RuntimeError("no accepted experiment resolved to an evaluated workspace")
            args.accepted_manifest.parent.mkdir(parents=True, exist_ok=True)
            args.accepted_manifest.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(str(args.accepted_manifest).replace("\\", "/"))
            return 0
        experiment = resolve_sota_experiment(loop.trace)
        workspace, source = resolve_evaluated_workspace(loop, experiment)
    except Exception as exc:  # noqa: BLE001 - CLI must return a single actionable error
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if source != "session":
        print(f"INFO: recovered evaluated workspace from {source}", file=sys.stderr)
    print(str(workspace).replace("\\", "/"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
