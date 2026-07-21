from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parent / "rdagent_backup" / "build_all_mine_history.py"
SPEC = importlib.util.spec_from_file_location("rdagent_build_mine_history", MODULE_PATH)
assert SPEC and SPEC.loader
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)


def test_watcher_no_winner_status_says_detailed_run_was_recorded():
    source = (Path(__file__).parent / "watch_predict_pc.ps1").read_text(
        encoding="utf-8"
    )

    assert "正常,未记账" not in source
    assert "跑通无winner(正常,不记账)" not in source
    assert "已记账（含有效回测明细" in source


def test_qlib_path_failure_is_not_reported_as_no_winner():
    state, outcome, code = history.classify_run(
        "Experiment execution ... /workspace/qlib_workspace/C:/qlib_data/cn_data "
        "does not contain data for day",
        loops=4,
        evaluations_done=0,
        n_pass=None,
    )
    assert state == "error"
    assert code == "qlib_data_path"
    assert "无赢家" not in outcome


def test_clean_evaluations_without_winner_are_done():
    state, outcome, code = history.classify_run(
        "Experiment execution ... Generating feedback...",
        loops=3,
        evaluations_done=3,
        n_pass=None,
    )
    assert state == "done"
    assert code == ""
    assert outcome == "无赢家(3次有效回测)"


def test_terminal_model_failure_after_valid_evaluation_is_partial():
    state, outcome, code = history.classify_run(
        "Generating feedback...\nRuntimeError: Failed to create chat completion after 10 retries.\n"
        "Invalid token",
        loops=2,
        evaluations_done=1,
        n_pass=None,
    )
    assert state == "partial"
    assert code == "model_auth"
    assert "1次有效回测" in outcome


def test_pruned_legacy_log_is_unknown_not_failed():
    state, outcome, code = history.classify_run("", loops=3, evaluations_done=0, n_pass=None)
    assert state == "unknown"
    assert code == "log_missing"
    assert "无法核验" in outcome


def test_parse_backtests_keeps_metrics_factors_and_current_decision_together():
    text = """
evolving workspace: File Factor[first_5d]: C:\\ws\\one
evolving workspace: File Factor[first_10d]: C:\\ws\\two
2026-07-18 23:08:17.000 | Start Loop 0, Step 2: running
Combined Results:
IC of Current Result is 0.017679, of SOTA Result is 0.008480; 1day.excess_return_with_cost.annualized_return of Current Result is 0.063752, of SOTA Result is -0.060609; 1day.excess_return_with_cost.max_drawdown of Current Result is -0.081478, of SOTA Result is -0.123782
2026-07-18 23:08:21.167 | Response:
{"Replace Best Result": "yes"}
2026-07-18 23:08:22.000 | Start Loop 0, Step 4: record
prompt history repeats {"Replace Best Result": "yes"}
evolving workspace: File Factor[second]: C:\\ws\\three
2026-07-18 23:11:20.000 | Start Loop 1, Step 2: running
Combined Results:
IC of Current Result is 1.2e-2, of SOTA Result is 0.017679; 1day.excess_return_with_cost.annualized_return of Current Result is -0.01, of SOTA Result is 0.063752; 1day.excess_return_with_cost.max_drawdown of Current Result is -0.2, of SOTA Result is -0.081478
2026-07-18 23:11:25.000 | Response:
{"Replace Best Result": "no"}
evolving workspace: File Factor[unfinished]: C:\\ws\\four
"""

    backtests, unevaluated = history.parse_backtests(text)

    assert [row["round"] for row in backtests] == [1, 2]
    assert backtests[0]["factors"] == ["first_5d", "first_10d"]
    assert backtests[0]["loop_index"] == 0
    assert backtests[0]["evaluated_at"] == "2026-07-18 23:08:21"
    assert backtests[0]["ic"] == pytest.approx(0.017679)
    assert backtests[0]["annualized_return"] == pytest.approx(0.063752)
    assert backtests[0]["accepted"] is True
    assert backtests[1]["factors"] == ["second"]
    assert backtests[1]["ic"] == pytest.approx(0.012)
    assert backtests[1]["accepted"] is False
    assert unevaluated == ["unfinished"]


def test_parse_backtests_supports_recorded_decision_and_missing_numbers():
    text = """
File Factor[alpha]: C:\\ws\\alpha
Combined Results:
IC of Current Result is nan, of SOTA Result is 0.01; 1day.excess_return_with_cost.annualized_return of Current Result is None, of SOTA Result is -1e-2; 1day.excess_return_with_cost.max_drawdown of Current Result is -.05, of SOTA Result is -.10
Decision (Whether this experiment is SOTA): True
"""

    backtests, unevaluated = history.parse_backtests(text)

    assert len(backtests) == 1
    assert backtests[0]["ic"] is None
    assert backtests[0]["annualized_return"] is None
    assert backtests[0]["max_drawdown"] == pytest.approx(-0.05)
    assert backtests[0]["accepted"] is True
    assert unevaluated == []


def test_evaluated_factor_count_excludes_crashed_tail_and_deduplicates_names():
    backtests = [
        {"factors": ["tested_a", "tested_b"]},
        {"factors": ["tested_b", "tested_c"]},
        "invalid legacy row",
    ]

    assert history._evaluated_factor_names(backtests) == [
        "tested_a",
        "tested_b",
        "tested_c",
    ]
    assert "untested_tail" not in history._evaluated_factor_names(backtests)


def test_worker_assertion_after_valid_evaluation_is_partial():
    state, outcome, code = history.classify_run(
        "Combined Results ...\nProcess SpawnPoolWorker-7:\nTraceback ...\nAssertionError",
        loops=15,
        evaluations_done=15,
        n_pass=0,
    )

    assert state == "partial"
    assert code == "worker_process"
    assert "15次有效回测" in outcome


def test_backtest_archive_round_trip_is_json_only_and_trace_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "RESID_ARCHIVE", tmp_path)
    name = "mine_csi300_20260718_230444"
    rows = [{"round": 1, "factors": ["alpha"], "ic": 0.01, "accepted": True}]

    path = history._write_backtest_archive(name, rows, ["unfinished"])

    payload = path.read_text(encoding="utf-8")
    assert '"source": "daily_log"' in payload
    assert history._read_backtest_archive(name) == (rows, ["unfinished"])

    wrong = tmp_path / "backtests_mine_csi300_20260718_194227.json"
    wrong.write_text(payload, encoding="utf-8")
    assert history._read_backtest_archive("mine_csi300_20260718_194227") == ([], [])


def test_missing_log_with_archived_evaluations_is_not_unknown():
    state, outcome, code = history.classify_run(
        "",
        loops=3,
        evaluations_done=2,
        n_pass=0,
    )

    assert state == "done"
    assert outcome == "无赢家(2次有效回测)"
    assert code == ""


def test_build_reads_split_stdout_and_archives_completed_backtests(tmp_path, monkeypatch):
    name = "mine_csi300_20260719_203214"
    traces = tmp_path / "log"
    logs = tmp_path / "daily_logs"
    final = tmp_path / "final"
    session = traces / name / "__session__"
    for loop_index in (0, 1):
        (session / str(loop_index) / "4_record").mkdir(parents=True)
    logs.mkdir()
    final.mkdir()
    (logs / f"{name}.log").write_text("tqdm progress only\n", encoding="utf-8")
    (logs / f"{name}.log.stdout.log").write_text(
        """
Using chat model openai/k3
File Factor[first_5d]: C:\\ws\\one
File Factor[first_10d]: C:\\ws\\two
File Factor[first_volume]: C:\\ws\\three
2026-07-19 20:45:00 | Start Loop 0, Step 2: running
Combined Results:
IC of Current Result is 0.017679, of SOTA Result is 0.008480; 1day.excess_return_with_cost.annualized_return of Current Result is 0.063752, of SOTA Result is -0.060609; 1day.excess_return_with_cost.max_drawdown of Current Result is -0.081478, of SOTA Result is -0.123782
2026-07-19 20:45:11 | {"Replace Best Result": "yes"}
2026-07-19 20:45:12 | Start Loop 0, Step 4: record
File Factor[second_5d]: C:\\ws\\four
File Factor[second_10d]: C:\\ws\\five
File Factor[second_norm_5d]: C:\\ws\\six
File Factor[second_norm_10d]: C:\\ws\\seven
2026-07-19 20:52:30 | Start Loop 1, Step 2: running
Combined Results:
IC of Current Result is 0.017906, of SOTA Result is 0.017679; 1day.excess_return_with_cost.annualized_return of Current Result is 0.027797, of SOTA Result is 0.063752; 1day.excess_return_with_cost.max_drawdown of Current Result is -0.071339, of SOTA Result is -0.081478
2026-07-19 20:52:42 | {"Replace Best Result": "yes"}
2026-07-19 20:52:43 | Start Loop 1, Step 4: record
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(history, "TRACES", traces)
    monkeypatch.setattr(history, "LOGDIR", logs)
    monkeypatch.setattr(history, "RESID_ARCHIVE", final)
    monkeypatch.setattr(history, "OUT", tmp_path / "mine_all_history.json")
    monkeypatch.setattr(history, "LEGACY_NAS_OUT", tmp_path / "nas_history.json")

    row = history.build()[0]

    assert row["loops_done"] == 2
    assert row["evaluations_done"] == 2
    assert row["n_evaluated_factors"] == 7
    assert row["n_unevaluated_factors"] == 0
    assert row["factor_count_exact"] is True
    assert row["accepted_backtests"] == 2
    assert row["model"] == "k3"
    assert row["state"] == "done"
    assert row["outcome"] == "无赢家(2次有效回测)"
    assert row["backtests"][1]["annualized_return"] == pytest.approx(0.027797)
    assert (final / f"backtests_{name}.json").exists()


def test_read_run_logs_keeps_legacy_single_log_compatible(tmp_path, monkeypatch):
    name = "mine_csi300_20260718_010000"
    monkeypatch.setattr(history, "LOGDIR", tmp_path)
    legacy = tmp_path / f"{name}.log"
    legacy.write_text(
        "File Factor[legacy_factor]: C:\\ws\\legacy\n"
        "Combined Results:\n"
        "IC of Current Result is 0.01, of SOTA Result is 0.00; "
        "1day.excess_return_with_cost.annualized_return of Current Result is 0.02, "
        "of SOTA Result is 0.01; "
        "1day.excess_return_with_cost.max_drawdown of Current Result is -0.03, "
        "of SOTA Result is -0.04\n"
        '{"Replace Best Result": "yes"}\n',
        encoding="utf-8",
    )

    diagnostic, failure_tail, semantic, backtests, unevaluated = history._read_run_logs(name)

    assert diagnostic == semantic
    assert failure_tail == semantic
    assert len(backtests) == 1
    assert backtests[0]["factors"] == ["legacy_factor"]
    assert unevaluated == []


def test_read_run_logs_keeps_legacy_factors_when_stdout_is_empty(tmp_path, monkeypatch):
    name = "mine_csi300_20260718_020000"
    monkeypatch.setattr(history, "LOGDIR", tmp_path)
    (tmp_path / f"{name}.log").write_text(
        "File Factor[unfinished_legacy]: C:\\ws\\legacy\n", encoding="utf-8"
    )
    (tmp_path / f"{name}.log.stdout.log").write_text("", encoding="utf-8")

    _, _, semantic, backtests, unevaluated = history._read_run_logs(name)

    assert "unfinished_legacy" in semantic
    assert backtests == []
    assert unevaluated == ["unfinished_legacy"]


def test_split_failure_tails_do_not_hide_stderr_terminal_error(tmp_path, monkeypatch):
    name = "mine_csi300_20260718_030000"
    monkeypatch.setattr(history, "LOGDIR", tmp_path)
    (tmp_path / f"{name}.log").write_text(
        "BrokenProcessPool: worker stopped\n", encoding="utf-8"
    )
    (tmp_path / f"{name}.log.stdout.log").write_text(
        "x" * 150_000
        + "\nFile Factor[valid_factor]: C:\\ws\\valid\n"
        + "Combined Results:\n"
        + "IC of Current Result is 0.01, of SOTA Result is 0.00; "
        + "1day.excess_return_with_cost.annualized_return of Current Result is 0.02, "
        + "of SOTA Result is 0.01; "
        + "1day.excess_return_with_cost.max_drawdown of Current Result is -0.03, "
        + "of SOTA Result is -0.04\n",
        encoding="utf-8",
    )

    diagnostic, failure_tail, _, backtests, _ = history._read_run_logs(name)
    state, outcome, code = history.classify_run(
        diagnostic,
        loops=1,
        evaluations_done=len(backtests),
        n_pass=None,
        failure_tail=failure_tail,
    )

    assert len(backtests) == 1
    assert state == "partial"
    assert code == "worker_process"
    assert "1次有效回测" in outcome


def test_residual_screen_requires_matching_universe(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "RESID_ARCHIVE", tmp_path)
    name = "minefund_csi300_20260710_132641"
    path = tmp_path / f"fund_resid_{name}.json"
    path.write_text(
        '{"universe":"csi1000","factors":[{"factor":"X","pass":true}]}',
        encoding="utf-8",
    )
    assert history._read_residual_screen(name, "fund", "csi300") == (
        None,
        None,
        [],
        "missing_or_invalid",
    )

    path.write_text(
        '{"universe":"csi300","factors":[{"factor":"X","pass":true}]}',
        encoding="utf-8",
    )
    assert history._read_residual_screen(name, "fund", "csi300") == (
        1,
        1,
        ["X"],
        "legacy_global",
    )


def test_winner_residual_screen_is_preferred_and_requires_exact_identity(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(history, "RESID_ARCHIVE", tmp_path)
    name = "mine_csi300_20260718_230444"
    legacy = tmp_path / f"ohlcv_resid_{name}.json"
    winner = tmp_path / f"ohlcv_winner_resid_{name}.json"
    legacy.write_text(
        '{"universe":"csi300","factors":[{"factor":"LEGACY","pass":true}]}',
        encoding="utf-8",
    )
    winner.write_text(
        '{"scope":"exact_workspace","workspace":"D:/rdagent_workspace/'
        '0123456789abcdef0123456789abcdef",'
        '"universe":"csi300","screened":2,"n_pass":1,'
        '"factors":[{"factor":"WINNER","pass":true},'
        '{"factor":"REJECTED","pass":false}]}',
        encoding="utf-8",
    )

    assert history._read_residual_screen(name, "ohlcv", "csi300") == (
        1,
        2,
        ["WINNER"],
        "exact_winner",
    )

    # A present but invalid winner artifact must fail closed.  Falling back to
    # LEGACY here would authorize a different historical workspace.
    winner.write_text(
        '{"scope":"workspace_root","workspace":"D:/rdagent_workspace/'
        '0123456789abcdef0123456789abcdef",'
        '"universe":"csi300","factors":[{"factor":"WINNER","pass":true}]}',
        encoding="utf-8",
    )
    assert history._read_residual_screen(name, "ohlcv", "csi300") == (
        None,
        None,
        [],
        "missing_or_invalid",
    )


def test_legacy_residual_screen_is_used_only_when_winner_archive_is_absent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(history, "RESID_ARCHIVE", tmp_path)
    name = "minefund_csi300_20260710_132641"
    legacy = tmp_path / f"fund_resid_{name}.json"
    legacy.write_text(
        '{"universe":"csi300","factors":[{"factor":"OLD","pass":true}]}',
        encoding="utf-8",
    )

    assert history._read_residual_screen(name, "fund", "csi300") == (
        1,
        1,
        ["OLD"],
        "legacy_global",
    )


def test_history_publish_uses_resolved_shared_dir(tmp_path, monkeypatch):
    local_out = tmp_path / "local" / "mine_all_history.json"
    shared_dir = tmp_path / "shared"
    local_out.parent.mkdir()
    shared_dir.mkdir()
    monkeypatch.setattr(history, "OUT", local_out)
    monkeypatch.setenv("SHARED_DIR", str(shared_dir))

    target = history._publish_payload({"runs": [{"time": "2026-07-18 19:42:27"}], "n": 1})

    assert target == shared_dir / "mine_all_history.json"
    assert local_out.read_bytes() == target.read_bytes()
    assert "2026-07-18 19:42:27" in target.read_text(encoding="utf-8")
    assert list(local_out.parent.glob(".*.tmp")) == []
    assert list(shared_dir.glob(".*.tmp")) == []


def test_history_publish_failure_is_not_silenced(tmp_path, monkeypatch):
    local_out = tmp_path / "local" / "mine_all_history.json"
    local_out.parent.mkdir()
    monkeypatch.setattr(history, "OUT", local_out)
    monkeypatch.setenv("SHARED_DIR", str(tmp_path / "missing"))

    with pytest.raises(FileNotFoundError):
        history._publish_payload({"runs": [], "n": 0})

    assert local_out.exists()


def _write_pareto_manifest(root: Path, name: str, candidates: list[dict]) -> Path:
    path = root / f"research_candidates_{name}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "rdagent_accepted_research_candidates",
                "trace": f"C:/rdagent/log/{name}",
                "candidates": candidates,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_pareto_no_winner_keeps_every_exact_factor_metric(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "RESID_ARCHIVE", tmp_path)
    name = "mine_csi300_20260718_194227"
    candidate_id = "a" * 64
    workspace_id = "1" * 32
    workspace = f"D:/rdagent_workspace/{workspace_id}"
    manifest_path = _write_pareto_manifest(
        tmp_path,
        name,
        [
            {
                "candidate_id": candidate_id,
                "history_index": 3,
                "workspace": workspace,
                "pareto_research_candidate": True,
                "metrics": {
                    "net_annualized_return": 0.0926,
                    "net_information_ratio": 1.137,
                    "max_drawdown": -0.055,
                    "ic": 0.015,
                    "rank_ic": 0.013,
                },
            }
        ],
    )
    (tmp_path / f"pareto_queue_{manifest_path.stem}.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "rdagent_pareto_evaluation_queue",
                "research_manifest": str(manifest_path).replace("\\", "/"),
                "universe": "csi300",
                "items": [
                    {
                        "candidate_id": candidate_id,
                        "workspace": workspace,
                        "status": "no_factors",
                        "stage": "no_factors",
                        "terminal_reason": "exact_screen_no_pass",
                        "attempts": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    factor_rows = [
        {
            "factor": "alpha_one",
            "ic60": 0.0034,
            "gain": -0.004,
            "maxcorr": 0.91,
            "redundant_with": "base_alpha",
            "resid_ic": -0.0033,
            "coverage": 0.99,
            "decay_pass": False,
            "base_pass": False,
            "pass": False,
        },
        {
            "factor": "alpha_two",
            "ic60": 0.0025,
            "gain": -0.0041,
            "maxcorr": 0.41,
            "resid_ic": -0.005,
            "coverage": 0.98,
            "decay_pass": False,
            "base_pass": False,
            "pass": False,
        },
    ]
    (tmp_path / f"pareto_screen_{candidate_id}.json").write_text(
        json.dumps(
            {
                "scope": "exact_workspace",
                "workspace": workspace,
                "universe": "csi300",
                "screened": 2,
                "distinct_total": 2,
                "n_pass": 0,
                "passed_factors": [],
                "factors": factor_rows,
            }
        ),
        encoding="utf-8",
    )

    candidates = history._read_pareto_candidates(name, "csi300")

    assert len(candidates) == 1
    row = candidates[0]
    assert row["research_round"] == 4
    assert row["pareto_rank"] == 1
    assert row["status"] == "no_factors"
    assert row["terminal_reason"] == "exact_screen_no_pass"
    assert row["workspace_id"] == workspace_id
    assert "workspace" not in row
    assert row["exact_screen"]["valid"] is True
    assert row["exact_screen"]["n_pass"] == 0
    assert row["exact_screen"]["screened"] == 2
    assert row["exact_screen"]["factors"][0]["redundant_with"] == "base_alpha"
    assert row["exact_screen"]["factors"][1]["resid_ic"] == pytest.approx(-0.005)


@pytest.mark.parametrize(
    ("field", "bad_value", "error"),
    [
        ("workspace", f"D:/rdagent_workspace/{'2' * 32}", "workspace_mismatch"),
        ("universe", "csi1000", "universe_mismatch"),
        ("screened", 2, "count_mismatch"),
    ],
)
def test_pareto_exact_screen_identity_and_counts_fail_closed(
    tmp_path, monkeypatch, field, bad_value, error
):
    monkeypatch.setattr(history, "RESID_ARCHIVE", tmp_path)
    name = "mine_csi300_20260718_194227"
    candidate_id = "b" * 64
    workspace = f"D:/rdagent_workspace/{'3' * 32}"
    _write_pareto_manifest(
        tmp_path,
        name,
        [
            {
                "candidate_id": candidate_id,
                "history_index": 1,
                "workspace": workspace,
                "pareto_research_candidate": True,
                "metrics": {},
            }
        ],
    )
    screen = {
        "scope": "exact_workspace",
        "workspace": workspace,
        "universe": "csi300",
        "screened": 1,
        "n_pass": 0,
        "factors": [{"factor": "alpha", "pass": False}],
    }
    screen[field] = bad_value
    (tmp_path / f"pareto_screen_{candidate_id}.json").write_text(
        json.dumps(screen), encoding="utf-8"
    )

    row = history._read_pareto_candidates(name, "csi300")[0]

    assert row["exact_screen"] == {
        "valid": False,
        "error": error,
        "artifact": f"pareto_screen_{candidate_id}.json",
    }


def test_pareto_candidates_are_deduplicated_and_ranked_by_research_return(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(history, "RESID_ARCHIVE", tmp_path)
    name = "mine_csi300_20260718_194227"
    low_id, high_id = "c" * 64, "d" * 64

    def row(candidate_id, annualized, workspace_id):
        return {
            "candidate_id": candidate_id,
            "history_index": 1,
            "workspace": f"D:/rdagent_workspace/{workspace_id * 32}",
            "pareto_research_candidate": True,
            "metrics": {
                "net_annualized_return": annualized,
                "net_information_ratio": 0.5,
            },
        }

    low = row(low_id, 0.03, "4")
    high = row(high_id, 0.09, "5")
    _write_pareto_manifest(tmp_path, name, [low, high, dict(low)])

    candidates = history._read_pareto_candidates(name, "csi300")

    assert [item["candidate_id"] for item in candidates] == [high_id, low_id]
    assert [item["pareto_rank"] for item in candidates] == [1, 2]


def test_pareto_backtest_link_requires_loop_and_metric_identity():
    candidates = [
        {
            "history_index": 3,
            "metrics": {
                "net_annualized_return": 0.092611869,
                "max_drawdown": -0.054953342,
                "ic": 0.015274417,
            },
        },
        {
            "history_index": 5,
            "metrics": {
                "net_annualized_return": 0.99,
                "max_drawdown": -0.046517584,
                "ic": 0.017873206,
            },
        },
    ]
    backtests = [
        {
            "round": 4,
            "loop_index": 3,
            "annualized_return": 0.092612,
            "max_drawdown": -0.054953,
            "ic": 0.015274,
        },
        {
            "round": 6,
            "loop_index": 5,
            "annualized_return": 0.070701,
            "max_drawdown": -0.046518,
            "ic": 0.017873,
        },
    ]

    history._link_pareto_backtests(candidates, backtests)

    assert candidates[0]["backtest_round"] == 4
    assert candidates[0]["backtest_link_valid"] is True
    assert candidates[1]["backtest_round"] is None
    assert candidates[1]["backtest_link_valid"] is False


def test_trace_scoped_inline_screen_wins_over_reused_global_artifact(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(history, "RESID_ARCHIVE", tmp_path)
    name = "mine_csi300_20260718_194227"
    candidate_id = "e" * 64
    workspace_id = "6" * 32
    workspace = f"D:/rdagent_workspace/{workspace_id}"
    manifest_path = _write_pareto_manifest(
        tmp_path,
        name,
        [
            {
                "candidate_id": candidate_id,
                "history_index": 1,
                "workspace": workspace,
                "pareto_research_candidate": True,
                "metrics": {},
            }
        ],
    )
    inline = {
        "status": "valid",
        "artifact": f"pareto_screen_{candidate_id}.json",
        "evaluated_at": "2026-07-19 12:30",
        "scope": "exact_workspace",
        "universe": "csi300",
        "workspace_id": workspace_id,
        "screened": 1,
        "distinct_total": 1,
        "n_pass": 0,
        "passed_factors": [],
        "factors": [{"factor": "trace_scoped_alpha", "pass": False}],
    }
    queue_path = tmp_path / f"pareto_queue_{manifest_path.stem}.json"
    queue = {
        "schema_version": 2,
        "kind": "rdagent_pareto_evaluation_queue",
        "research_manifest": str(manifest_path),
        "universe": "csi300",
        "items": [
            {
                "candidate_id": candidate_id,
                "workspace": workspace,
                "status": "no_factors",
                "exact_screen": inline,
            }
        ],
    }
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    (tmp_path / f"pareto_screen_{candidate_id}.json").write_text(
        json.dumps(
            {
                "scope": "exact_workspace",
                "workspace": workspace,
                "universe": "csi300",
                "screened": 1,
                "n_pass": 1,
                "factors": [{"factor": "later_trace_alpha", "pass": True}],
            }
        ),
        encoding="utf-8",
    )

    row = history._read_pareto_candidates(name, "csi300")[0]

    assert row["exact_screen"]["n_pass"] == 0
    assert row["exact_screen"]["factors"][0]["factor"] == "trace_scoped_alpha"

    queue["items"][0]["exact_screen"] = {"status": "invalid"}
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    invalid = history._read_pareto_candidates(name, "csi300")[0]["exact_screen"]
    assert invalid["valid"] is False
    assert invalid["error"] == "inline_invalid"


def test_mining_pages_expose_shared_per_backtest_details():
    root = Path(__file__).parents[1]
    helper = (root / "static" / "mine_history.js").read_text(encoding="utf-8")
    assert "成本后年化超额" in helper
    assert "当时SOTA" in helper
    assert "unevaluated_factors" in helper
    assert "n_evaluated_factors" in helper
    assert "n_unevaluated_factors" in helper
    assert "factorCountText" in helper
    assert "evaluatedFactors" in helper
    assert "RD接纳" in helper
    assert "pareto_candidates" in helper
    assert "逐因子精确终筛" in helper
    assert "redundant_with" in helper

    for template_name in ("rdagent.html", "mine_pool.html"):
        source = (root / "templates" / template_name).read_text(encoding="utf-8")
        assert '/static/mine_history.js' in source
        assert "MineHistory.renderDetails(r)" in source
        assert "MineHistory.factorCountText(r)" in source
        assert "MineHistory.evaluatedFactors(r)" in source
