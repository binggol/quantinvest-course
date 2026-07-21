import json
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).with_name("evaluate_rdagent_pareto_queue.ps1")
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")


def candidate(token, annualized, ir, *, pareto=True):
    return {
        "candidate_id": token * 64,
        "history_index": 1,
        "workspace": f"D:/rdagent_workspace/{token * 32}",
        "pareto_research_candidate": pareto,
        "metrics": {
            "net_annualized_return": annualized,
            "net_information_ratio": ir,
            "max_drawdown": -0.08,
            "ic": 0.02,
            "rank_ic": 0.02,
        },
    }


def test_plan_is_bounded_deduplicated_and_return_prioritized(tmp_path):
    manifest = {
        "schema_version": 1,
        "kind": "rdagent_accepted_research_candidates",
        "trace": "C:/rdagent/log/mine_csi300_20260718_194227",
        "candidates": [
            candidate("a", 0.04, 0.5),
            candidate("b", 0.12, 0.8),
            candidate("c", 0.09, 1.0),
            candidate("b", 0.12, 0.8),
            candidate("d", 0.30, 2.0, pareto=False),
        ],
    }
    path = tmp_path / "research_candidates_example.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    completed = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-ResearchManifest",
            str(path),
            "-MaxCandidates",
            "2",
            "-PlanOnly",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    plan = json.loads(completed.stdout)

    assert plan["pareto_count"] == 3
    assert plan["max_candidates"] == 2
    assert [row["candidate_id"] for row in plan["selected"]] == ["b" * 64, "c" * 64]


def test_runtime_contract_has_failure_isolation_and_joint_tournament():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "[ValidateRange(1, 8)][int]$MaxCandidates" in source
    assert "stable batch-label collision" in source
    assert "$item.status = 'failed'" in source
    assert "SEEDS=0,1,2" in source
    assert "promote_production_champion.py" in source
    assert "foreach ($batch in $completedBatches)" in source
    assert "RDAGENT_FACTOR_BATCH_LABEL" in source
    assert "schema_version = 2" in source
    assert "terminal_reason = 'exact_screen_no_pass'" in source
    assert "terminal_reason = 'factor_analysis_no_pass'" in source
    assert "ConvertTo-ExactScreenAudit" in source
    assert "Write-JsonAtomic $screenArtifact $screen" in source


def test_audit_only_records_every_pending_candidate_without_running_models(tmp_path):
    manifest = {
        "schema_version": 1,
        "kind": "rdagent_accepted_research_candidates",
        "trace": "C:/rdagent/log/mine_csi300_20260718_194227",
        "candidates": [candidate("f", 0.08, 0.9), candidate("e", 0.05, 0.7)],
    }
    path = tmp_path / "research_candidates_mine_csi300_20260718_194227.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    completed = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-ResearchManifest",
            str(path),
            "-AuditOnly",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    summary = json.loads(completed.stdout)
    queue_path = tmp_path / f"pareto_queue_{path.stem}.json"
    queue = json.loads(queue_path.read_text(encoding="utf-8-sig"))
    assert queue["schema_version"] == 2
    assert queue["trace_name"] == "mine_csi300_20260718_194227"
    assert [item["rank"] for item in queue["items"]] == [1, 2]
    assert [item["status"] for item in queue["items"]] == ["pending", "pending"]
    assert summary["schema_version"] == 2
    assert summary["run_status"] == "partial"
    assert summary["candidate_counts"]["pending"] == 2
