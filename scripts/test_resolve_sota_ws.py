import pickle
from pathlib import Path
from types import SimpleNamespace

import pytest

from rdagent_backup import resolve_sota_ws


class FakeRunner:
    def develop(self, experiment):
        return experiment

    def get_cache_key(self, experiment):
        return "exact-cache-key"


class FakeTask:
    def __init__(self, name):
        self.name = name

    def get_task_information(self):
        return f"factor_name: {self.name}"


def make_experiment(workspace: Path, result=None, task_names=("factor-a",)):
    return SimpleNamespace(
        experiment_workspace=SimpleNamespace(workspace_path=workspace),
        result={"IC": 0.017906} if result is None else result,
        based_experiments=[],
        sub_tasks=[FakeTask(name) for name in task_names],
    )


def make_evaluated_workspace(root: Path, *, net_return=0.03, ir=0.4, maxdd=-0.08, ic=0.017906, rank_ic=0.016):
    config = root / "mlruns" / "1" / "run" / "artifacts" / "config"
    config.parent.mkdir(parents=True)
    config.write_bytes(b"config")
    (root / "qlib_res.csv").write_text(
        ",0\n"
        f"1day.excess_return_with_cost.annualized_return,{net_return}\n"
        f"1day.excess_return_with_cost.information_ratio,{ir}\n"
        f"1day.excess_return_with_cost.max_drawdown,{maxdd}\n"
        f"IC,{ic}\n"
        f"Rank IC,{rank_ic}\n",
        encoding="utf-8",
    )
    (root / "ret.pkl").write_bytes(b"result")
    return root


def write_cache(cache_root: Path, runner: FakeRunner, experiment):
    folder = cache_root / f"{runner.develop.__module__}.{runner.develop.__name__}"
    folder.mkdir(parents=True)
    path = folder / "exact-cache-key.pkl"
    path.write_bytes(pickle.dumps(experiment))
    return path


def test_resolver_uses_evaluated_session_workspace(tmp_path):
    workspace = make_evaluated_workspace(tmp_path / "evaluated")
    experiment = make_experiment(workspace)
    loop = SimpleNamespace(runner=FakeRunner())

    resolved, source = resolve_sota_ws.resolve_evaluated_workspace(
        loop, experiment, cache_root=tmp_path / "cache"
    )

    assert resolved == workspace
    assert source == "session"


def test_resolver_recovers_exact_evaluated_runner_cache(tmp_path):
    template_only = tmp_path / "template-only"
    template_only.mkdir()
    cached_workspace = make_evaluated_workspace(tmp_path / "cached-evaluated")
    session_experiment = make_experiment(template_only)
    cached_experiment = make_experiment(cached_workspace)
    runner = FakeRunner()
    write_cache(tmp_path / "cache", runner, cached_experiment)

    resolved, source = resolve_sota_ws.resolve_evaluated_workspace(
        SimpleNamespace(runner=runner),
        session_experiment,
        cache_root=tmp_path / "cache",
    )

    assert resolved == cached_workspace
    assert source == "runner-cache"


def test_resolver_rejects_cache_with_different_result(tmp_path):
    template_only = tmp_path / "template-only"
    template_only.mkdir()
    cached_workspace = make_evaluated_workspace(tmp_path / "cached-evaluated")
    runner = FakeRunner()
    write_cache(
        tmp_path / "cache",
        runner,
        make_experiment(cached_workspace, result={"IC": 0.1}),
    )

    with pytest.raises(RuntimeError, match="does not match"):
        resolve_sota_ws.resolve_evaluated_workspace(
            SimpleNamespace(runner=runner),
            make_experiment(template_only),
            cache_root=tmp_path / "cache",
        )


def test_resolver_rejects_cache_with_different_tasks(tmp_path):
    template_only = tmp_path / "template-only"
    template_only.mkdir()
    cached_workspace = make_evaluated_workspace(tmp_path / "cached-evaluated")
    runner = FakeRunner()
    write_cache(
        tmp_path / "cache",
        runner,
        make_experiment(cached_workspace, task_names=("factor-b",)),
    )

    with pytest.raises(RuntimeError, match="tasks do not match"):
        resolve_sota_ws.resolve_evaluated_workspace(
            SimpleNamespace(runner=runner),
            make_experiment(template_only, task_names=("factor-a",)),
            cache_root=tmp_path / "cache",
        )


def test_workspace_validation_requires_unique_config_and_results(tmp_path):
    workspace = make_evaluated_workspace(tmp_path / "evaluated")
    second = workspace / "mlruns" / "2" / "run" / "artifacts" / "config"
    second.parent.mkdir(parents=True)
    second.write_bytes(b"config")

    resolved, reason = resolve_sota_ws.validate_evaluated_workspace(workspace)

    assert resolved is None
    assert "exactly one" in reason


def test_session_sorting_is_numeric(tmp_path):
    for loop, step in ((1, 9), (1, 10), (2, 0)):
        path = tmp_path / "__session__" / str(loop) / f"{step}_record"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"session")

    sessions = resolve_sota_ws.list_session_files(tmp_path)

    assert [(loop, step) for loop, step, _ in sessions] == [(1, 9), (1, 10), (2, 0)]


def test_accepted_manifest_preserves_all_resolved_and_marks_pareto(tmp_path):
    stronger = make_evaluated_workspace(
        tmp_path / "stronger", net_return=0.08, ir=0.8, maxdd=-0.05, ic=0.03, rank_ic=0.03
    )
    weaker = make_evaluated_workspace(
        tmp_path / "weaker", net_return=0.03, ir=0.4, maxdd=-0.08, ic=0.02, rank_ic=0.02
    )
    rejected = make_evaluated_workspace(tmp_path / "rejected")
    trace = SimpleNamespace(
        hist=[
            (make_experiment(weaker, task_names=("weak",)), SimpleNamespace(decision="yes")),
            (make_experiment(rejected, task_names=("reject",)), SimpleNamespace(decision="no")),
            (make_experiment(stronger, task_names=("strong",)), SimpleNamespace(decision=True)),
        ]
    )
    loop = SimpleNamespace(trace=trace, runner=FakeRunner())

    manifest = resolve_sota_ws.build_accepted_manifest(loop, tmp_path / "trace")

    assert manifest["accepted_history_count"] == 2
    assert manifest["resolved_candidate_count"] == 2
    assert manifest["pareto_candidate_count"] == 1
    assert [
        row["task_signatures"] for row in manifest["candidates"] if row["pareto_research_candidate"]
    ] == [["factor_name: strong"]]
    assert manifest["production_ready"] is False


def test_resolve_sota_experiment_does_not_treat_no_string_as_true(tmp_path):
    rejected = make_experiment(make_evaluated_workspace(tmp_path / "rejected"))
    accepted = make_experiment(make_evaluated_workspace(tmp_path / "accepted"))
    trace = SimpleNamespace(
        hist=[
            (accepted, SimpleNamespace(decision="yes")),
            (rejected, SimpleNamespace(decision="no")),
        ]
    )

    assert resolve_sota_ws.resolve_sota_experiment(trace) is accepted
