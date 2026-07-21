#!/usr/bin/env python
"""Build the web mining ledger and distinguish failures from true no-winner runs."""

from __future__ import annotations

import glob
import json
import math
import os
import re
from pathlib import Path


ROOT = Path("C:/rdagent")
LOGDIR = ROOT / "daily_logs"
TRACES = ROOT / "log"
RESID_ARCHIVE = ROOT / "final"
OUT = ROOT / "mine_all_history.json"
LEGACY_NAS_OUT = Path("Z:/claude/qlib/data/csv_tmp/mine_all_history.json")
BACKTEST_ARCHIVE_VERSION = 1
PARETO_QUEUE_VERSION = 2

_CANDIDATE_ID_RE = re.compile(r"[0-9a-f]{64}", re.I)
_SCREEN_FACTOR_FIELDS = (
    "factor",
    "ic60",
    "gain",
    "maxcorr",
    "maxcorr_sat",
    "redundant_with",
    "ic_decay",
    "half_life",
    "half_life_censored",
    "decay_retention",
    "decay_pass",
    "resid_ic",
    "resid_ratio",
    "coverage",
    "style_r2",
    "suspect",
    "style_proxy",
    "base_pass",
    "pass",
)
_RESEARCH_METRIC_FIELDS = (
    "net_annualized_return",
    "net_information_ratio",
    "max_drawdown",
    "ic",
    "rank_ic",
)

_NUMBER = (
    r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|"
    r"nan|none|null|[-+]?inf(?:inity)?|n/?a"
)
_COMBINED_RESULT_RE = re.compile(
    rf"Combined Results:\s*IC of Current Result is\s*(?P<ic>{_NUMBER})\s*,\s*"
    rf"of SOTA Result is\s*(?P<sota_ic>{_NUMBER})\s*;\s*"
    rf"1day\.excess_return_with_cost\.annualized_return of Current Result is\s*"
    rf"(?P<annualized_return>{_NUMBER})\s*,\s*of SOTA Result is\s*"
    rf"(?P<sota_annualized_return>{_NUMBER})\s*;\s*"
    rf"1day\.excess_return_with_cost\.max_drawdown of Current Result is\s*"
    rf"(?P<max_drawdown>{_NUMBER})\s*,\s*of SOTA Result is\s*"
    rf"(?P<sota_max_drawdown>{_NUMBER})",
    re.I,
)
_FACTOR_RE = re.compile(r"File Factor\[([^\]]+)\]")
_REPLACE_BEST_RE = re.compile(
    r'["\']Replace Best Result["\']\s*:\s*["\'](yes|no|true|false)["\']',
    re.I,
)
_RECORDED_DECISION_RE = re.compile(
    r"Decision \(Whether this experiment is SOTA\):\s*(True|False)",
    re.I,
)
_LOOP_STEP_RE = re.compile(r"Start Loop\s+(\d+)\s*,\s*Step\s+[0-3]\s*:", re.I)
_RECORD_STEP_RE = re.compile(r"Start Loop\s+\d+\s*,\s*Step\s+4\s*:\s*record", re.I)
_TIMESTAMP_RE = re.compile(r"(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")


def _nas_out_path() -> Path:
    """Use the watcher's resolved share instead of relying on a mapped drive."""
    shared_dir = os.environ.get("SHARED_DIR", "").strip()
    if shared_dir:
        return Path(shared_dir) / OUT.name
    return LEGACY_NAS_OUT


def _atomic_write_text(path: Path, text: str) -> None:
    """Publish one complete snapshot without exposing a partially written JSON."""
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _publish_payload(payload: dict) -> Path:
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    _atomic_write_text(OUT, text)
    nas_out = _nas_out_path()
    _atomic_write_text(nas_out, text)
    return nas_out


def _backtest_archive_path(name: str) -> Path:
    return RESID_ARCHIVE / f"backtests_{name}.json"


def _read_backtest_archive(name: str):
    path = _backtest_archive_path(name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return [], []
    if not isinstance(payload, dict):
        return [], []
    if payload.get("schema_version") != BACKTEST_ARCHIVE_VERSION or payload.get("trace") != name:
        return [], []
    backtests = payload.get("backtests")
    unevaluated = payload.get("unevaluated_factors")
    if not isinstance(backtests, list) or not isinstance(unevaluated, list):
        return [], []
    return backtests, _ordered_unique(str(value) for value in unevaluated)


def _write_backtest_archive(name: str, backtests: list, unevaluated: list) -> Path:
    path = _backtest_archive_path(name)
    payload = {
        "schema_version": BACKTEST_ARCHIVE_VERSION,
        "trace": name,
        "source": "daily_log",
        "backtests": backtests,
        "unevaluated_factors": unevaluated,
    }
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    try:
        if path.read_text(encoding="utf-8") == text:
            return path
    except (FileNotFoundError, OSError):
        pass
    _atomic_write_text(path, text)
    return path


def parse_trace(name: str):
    match = re.match(r"(minefund|mine)_(?:(csi300|csi500|csi1000)_)?(\d{8})_(\d{6})$", name)
    if not match:
        return None
    route = "fund" if match.group(1) == "minefund" else "ohlcv"
    universe = match.group(2) or "csi300"
    date, clock = match.group(3), match.group(4)
    timestamp = (
        f"{date[:4]}-{date[4:6]}-{date[6:]} "
        f"{clock[:2]}:{clock[2:4]}:{clock[4:]}"
    )
    return route, universe, timestamp


def _finite_number(raw: str) -> float | None:
    """Convert one metric token to JSON-safe float without leaking NaN/Inf."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _ordered_unique(values):
    return list(dict.fromkeys(value for value in values if value))


def _evaluated_factor_names(backtests):
    """Return only factors attached to completed portfolio metric blocks."""
    return _ordered_unique(
        factor
        for backtest in backtests
        if isinstance(backtest, dict)
        for factor in backtest.get("factors", [])
        if isinstance(factor, str)
    )


def parse_backtests(text: str):
    """Extract every completed combined backtest and its newly tested factor group.

    RD-Agent repeats earlier feedback inside later prompts.  The first decision after
    each metric block is therefore the only decision associated with that backtest.
    """
    metrics = list(_COMBINED_RESULT_RE.finditer(text or ""))
    if not metrics:
        return [], _ordered_unique(_FACTOR_RE.findall(text or ""))

    backtests = []
    previous_metric_end = 0
    for index, metric in enumerate(metrics):
        next_metric_start = metrics[index + 1].start() if index + 1 < len(metrics) else len(text)
        factor_window = text[previous_metric_end:metric.start()]
        factor_names = _ordered_unique(
            _FACTOR_RE.findall(factor_window)
        )
        decision_text = text[metric.end():next_metric_start]
        record_step = _RECORD_STEP_RE.search(decision_text)
        if record_step:
            decision_text = decision_text[:record_step.start()]
        decision = _REPLACE_BEST_RE.search(decision_text)
        if not decision:
            decision = _RECORDED_DECISION_RE.search(decision_text)
        accepted = None
        if decision:
            accepted = decision.group(1).lower() in {"yes", "true"}

        values = {key: _finite_number(value) for key, value in metric.groupdict().items()}
        loop_matches = _LOOP_STEP_RE.findall(factor_window)
        timestamp = _TIMESTAMP_RE.search(decision_text)
        backtests.append(
            {
                "round": index + 1,
                "loop_index": int(loop_matches[-1]) if loop_matches else index,
                "evaluated_at": timestamp.group(1) if timestamp else "",
                "factors": factor_names,
                **values,
                "accepted": accepted,
            }
        )
        previous_metric_end = metric.end()

    unevaluated = _ordered_unique(_FACTOR_RE.findall(text[metrics[-1].end():]))
    return backtests, unevaluated


def _read_log_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    except OSError:
        return ""


def _read_run_logs(name: str):
    """Read split console logs without double-counting mirrored metric blocks.

    Current watchers redirect semantic RD-Agent output to ``.log.stdout.log`` and
    tqdm/warnings to ``.log``.  Older runs may contain everything in the latter.
    Parse each stream independently and select the one with the most completed
    backtests; use both streams only for terminal-error classification.
    """
    stderr_text = _read_log_text(LOGDIR / f"{name}.log")
    stdout_text = _read_log_text(LOGDIR / f"{name}.log.stdout.log")
    diagnostic_text = "\n".join(
        value for value in (stderr_text, stdout_text) if value
    )
    failure_tail = "\n".join(
        value[-120_000:] for value in (stderr_text, stdout_text) if value
    )

    choices = []
    for stdout_priority, value in enumerate((stderr_text, stdout_text)):
        backtests, unevaluated = parse_backtests(value)
        choices.append(
            (
                len(backtests),
                len(_evaluated_factor_names(backtests)),
                len(_FACTOR_RE.findall(value)),
                stdout_priority,
                value,
                backtests,
                unevaluated,
            )
        )
    _, _, _, _, semantic_text, backtests, unevaluated = max(choices)
    return diagnostic_text, failure_tail, semantic_text, backtests, unevaluated


def classify_run(
    text: str,
    loops: int,
    evaluations_done: int,
    n_pass: int | None,
    failure_tail: str | None = None,
):
    if not text and loops > 0 and evaluations_done == 0:
        return "unknown", "历史日志已清理, 无法核验", "log_missing"
    tail = text[-120_000:] if failure_tail is None else failure_tail
    failure_code = ""
    failure_label = ""
    if re.search(
        r"does not contain data for day|/C:/qlib_data/cn_data|"
        r"No result file found|Failed to run this experiment",
        text,
        re.I,
    ):
        failure_code, failure_label = "qlib_data_path", "Qlib数据路径错误"
    elif (
        re.search(r"Invalid token|token_rejected|PermissionDeniedError: Error code: 403", tail, re.I)
        and re.search(r"Failed to create chat completion after|RuntimeError:|Traceback", tail, re.I)
    ):
        failure_code, failure_label = "model_auth", "模型API凭据错误"
    elif re.search(r"APITimeout|Request timed out|Failed to create chat completion after", tail, re.I):
        failure_code, failure_label = "model_timeout", "模型API超时"
    elif "UnicodeEncodeError" in tail:
        failure_code, failure_label = "encoding", "进程编码错误"
    elif re.search(r"Cannot connect to the Docker daemon|docker_engine.*permission denied", tail, re.I):
        failure_code, failure_label = "docker", "Docker不可用"
    elif re.search(
        r"Process SpawnPoolWorker-\d+:[\s\S]{0,5000}AssertionError|"
        r"BrokenProcessPool|A process in the process pool was terminated abruptly",
        tail,
        re.I,
    ):
        failure_code, failure_label = "worker_process", "多进程工作进程异常"

    if failure_code:
        if evaluations_done > 0 or n_pass:
            prefix = f"部分完成({evaluations_done}次有效回测)" if evaluations_done else f"{n_pass}个正交检验过关"
            return "partial", f"{prefix}; 后续失败: {failure_label}", failure_code
        return "error", f"失败: {failure_label}", failure_code
    if n_pass:
        return "done", f"{n_pass}个过关", ""
    if evaluations_done > 0:
        return "done", f"无赢家({evaluations_done}次有效回测)", ""
    if loops > 0:
        return "error", "失败: 有循环记录但没有回测指标", "no_metrics"
    return "error", "未完成/崩", "incomplete"


def _screen_counts(payload):
    factors = payload.get("factors")
    if not isinstance(factors, list):
        return None
    names = []
    passed = []
    for factor in factors:
        if not isinstance(factor, dict) or not str(factor.get("factor") or "").strip():
            return None
        name = str(factor["factor"])
        names.append(name)
        if factor.get("pass") is True:
            passed.append(name)
    if len(names) != len(set(names)):
        return None
    try:
        if payload.get("screened") is not None and int(payload["screened"]) != len(factors):
            return None
        if payload.get("n_pass") is not None and int(payload["n_pass"]) != len(passed):
            return None
    except (TypeError, ValueError):
        return None
    return len(passed), len(factors), passed


def _read_screen_payload(path: Path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _valid_exact_workspace(value) -> bool:
    workspace = str(value or "").strip().replace("\\", "/").rstrip("/")
    return bool(
        re.fullmatch(
            r"(?:D:/rdagent_workspace|Z:/claude/rdagent_workspace)/[0-9a-f]{32}",
            workspace,
            re.I,
        )
    )


def _workspace_id(value) -> str:
    """Return only the opaque workspace identity; never expose a local path."""
    workspace = str(value or "").strip().replace("\\", "/").rstrip("/")
    match = re.fullmatch(
        r"(?:D:/rdagent_workspace|Z:/claude/rdagent_workspace)/([0-9a-f]{32})",
        workspace,
        re.I,
    )
    return match.group(1).lower() if match else ""


def _json_safe(value):
    """Keep an audit payload JSON-safe without changing booleans into numbers."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def _invalid_candidate_screen(error: str, artifact: str) -> dict:
    return {"valid": False, "error": error, "artifact": artifact}


def _read_candidate_screen(candidate_id: str, workspace: str, universe: str) -> dict:
    """Read one exact-workspace screen and fail closed on any identity mismatch."""
    artifact = f"pareto_screen_{candidate_id}.json"
    path = RESID_ARCHIVE / artifact
    payload = _read_screen_payload(path)
    if payload is None:
        return _invalid_candidate_screen("missing", artifact)
    if payload.get("scope") != "exact_workspace":
        return _invalid_candidate_screen("scope_mismatch", artifact)
    if payload.get("universe") != universe:
        return _invalid_candidate_screen("universe_mismatch", artifact)
    expected_workspace_id = _workspace_id(workspace)
    actual_workspace_id = _workspace_id(payload.get("workspace"))
    if not expected_workspace_id or actual_workspace_id != expected_workspace_id:
        return _invalid_candidate_screen("workspace_mismatch", artifact)

    counts = _screen_counts(payload)
    if counts is None:
        return _invalid_candidate_screen("count_mismatch", artifact)
    n_pass, screened, passed = counts
    declared_passed = payload.get("passed_factors")
    if isinstance(declared_passed, list) and (
        len(declared_passed) != len(set(map(str, declared_passed)))
        or set(map(str, declared_passed)) != set(passed)
    ):
        return _invalid_candidate_screen("passed_factor_mismatch", artifact)
    try:
        if (
            payload.get("distinct_total") is not None
            and int(payload["distinct_total"]) != screened
        ):
            return _invalid_candidate_screen("distinct_count_mismatch", artifact)
    except (TypeError, ValueError):
        return _invalid_candidate_screen("distinct_count_mismatch", artifact)

    factor_rows = []
    for source in payload["factors"]:
        factor_rows.append(
            {
                key: _json_safe(source.get(key))
                for key in _SCREEN_FACTOR_FIELDS
                if key in source
            }
        )
    return {
        "valid": True,
        "error": "",
        "artifact": artifact,
        "updated": str(payload.get("updated") or ""),
        "evaluator": str(payload.get("evaluator") or ""),
        "workspace_id": actual_workspace_id,
        "base_ic": _json_safe(payload.get("base_ic")),
        "screened": screened,
        "distinct_total": screened,
        "n_pass": n_pass,
        "passed_factors": passed,
        "horizons": _json_safe(payload.get("horizons") or []),
        "decay_gate": _json_safe(payload.get("decay_gate") or {}),
        "factors": factor_rows,
    }


def _read_inline_candidate_screen(
    payload, candidate_id: str, workspace: str, universe: str
) -> dict | None:
    """Validate the trace-scoped screen embedded in a v2 queue.

    Candidate-only screen filenames can be overwritten if the same research
    candidate appears in a later trace.  The queue copy is trace-scoped, so once
    present it is authoritative and an invalid copy must not fall back globally.
    """
    if payload is None:
        return None
    artifact = f"pareto_screen_{candidate_id}.json"
    if not isinstance(payload, dict) or payload.get("status") != "valid":
        return _invalid_candidate_screen("inline_invalid", artifact)
    if payload.get("artifact") not in {None, "", artifact}:
        return _invalid_candidate_screen("artifact_mismatch", artifact)
    if payload.get("scope") != "exact_workspace":
        return _invalid_candidate_screen("scope_mismatch", artifact)
    if payload.get("universe") != universe:
        return _invalid_candidate_screen("universe_mismatch", artifact)
    if str(payload.get("workspace_id") or "").lower() != _workspace_id(workspace):
        return _invalid_candidate_screen("workspace_mismatch", artifact)
    counts = _screen_counts(payload)
    if counts is None:
        return _invalid_candidate_screen("count_mismatch", artifact)
    n_pass, screened, passed = counts
    declared_passed = payload.get("passed_factors")
    if isinstance(declared_passed, list) and (
        len(declared_passed) != len(set(map(str, declared_passed)))
        or set(map(str, declared_passed)) != set(passed)
    ):
        return _invalid_candidate_screen("passed_factor_mismatch", artifact)
    try:
        if (
            payload.get("distinct_total") is not None
            and int(payload["distinct_total"]) != screened
        ):
            return _invalid_candidate_screen("distinct_count_mismatch", artifact)
    except (TypeError, ValueError):
        return _invalid_candidate_screen("distinct_count_mismatch", artifact)

    factor_rows = [
        {
            key: _json_safe(source.get(key))
            for key in _SCREEN_FACTOR_FIELDS
            if key in source
        }
        for source in payload["factors"]
    ]
    return {
        "valid": True,
        "error": "",
        "artifact": artifact,
        "updated": str(payload.get("evaluated_at") or ""),
        "evaluator": "",
        "workspace_id": _workspace_id(workspace),
        "base_ic": _json_safe(payload.get("base_ic")),
        "screened": screened,
        "distinct_total": screened,
        "n_pass": n_pass,
        "passed_factors": passed,
        "horizons": _json_safe(payload.get("horizons") or []),
        "decay_gate": _json_safe(payload.get("decay_gate") or {}),
        "factors": factor_rows,
    }


def _read_pareto_candidates(name: str, universe: str) -> list[dict]:
    """Join research, queue and exact-screen artifacts for one mining trace.

    The returned shape deliberately omits local absolute paths.  A queue status is
    useful operational context, but only a separately validated exact screen is
    allowed to claim that a candidate passed or failed the production gate.
    """
    manifest_path = RESID_ARCHIVE / f"research_candidates_{name}.json"
    manifest = _read_screen_payload(manifest_path)
    if (
        manifest is None
        or manifest.get("kind") != "rdagent_accepted_research_candidates"
        or Path(str(manifest.get("trace") or "").replace("\\", "/")).name != name
        or not isinstance(manifest.get("candidates"), list)
    ):
        return []

    queue_path = RESID_ARCHIVE / f"pareto_queue_{manifest_path.stem}.json"
    queue = _read_screen_payload(queue_path)
    queue_items: dict[str, dict] = {}
    if (
        queue is not None
        and queue.get("kind") == "rdagent_pareto_evaluation_queue"
        and queue.get("universe") == universe
        and isinstance(queue.get("items"), list)
    ):
        manifest_ref = Path(
            str(queue.get("research_manifest") or "").replace("\\", "/")
        ).name
        if not manifest_ref or manifest_ref == manifest_path.name:
            duplicate = False
            for item in queue["items"]:
                candidate_id = str(item.get("candidate_id") or "").lower()
                if (
                    not isinstance(item, dict)
                    or not _CANDIDATE_ID_RE.fullmatch(candidate_id)
                    or candidate_id in queue_items
                ):
                    duplicate = True
                    break
                queue_items[candidate_id] = item
            if duplicate:
                queue_items = {}

    candidates = {}
    for candidate in manifest["candidates"]:
        if not isinstance(candidate, dict) or candidate.get("pareto_research_candidate") is not True:
            continue
        candidate_id = str(candidate.get("candidate_id") or "").lower()
        workspace = str(candidate.get("workspace") or "")
        if (
            not _CANDIDATE_ID_RE.fullmatch(candidate_id)
            or candidate_id in candidates
            or not _valid_exact_workspace(workspace)
        ):
            continue
        try:
            history_index = int(candidate.get("history_index"))
        except (TypeError, ValueError):
            history_index = -1
        metrics_source = candidate.get("metrics")
        metrics_source = metrics_source if isinstance(metrics_source, dict) else {}
        metrics = {
            field: _json_safe(metrics_source.get(field))
            for field in _RESEARCH_METRIC_FIELDS
        }
        queue_item = queue_items.get(candidate_id, {})
        if queue_item and _workspace_id(queue_item.get("workspace")) != _workspace_id(workspace):
            queue_item = {}
        status = str(queue_item.get("status") or "pending")
        if status not in {"pending", "running", "completed", "no_factors", "failed"}:
            status = "pending"
        try:
            attempts = max(0, int(queue_item.get("attempts") or 0))
        except (TypeError, ValueError):
            attempts = 0
        inline_screen = _read_inline_candidate_screen(
            queue_item.get("exact_screen") if queue_item else None,
            candidate_id,
            workspace,
            universe,
        )
        candidates[candidate_id] = {
            "candidate_id": candidate_id,
            "history_index": history_index,
            "research_round": history_index + 1 if history_index >= 0 else None,
            "workspace_id": _workspace_id(workspace),
            "metrics": metrics,
            "status": status,
            "stage": str(queue_item.get("stage") or ""),
            "terminal_reason": str(queue_item.get("terminal_reason") or ""),
            "attempts": attempts,
            "batch": str(queue_item.get("batch") or ""),
            "error": str(queue_item.get("error") or ""),
            "updated_at": str(queue_item.get("updated_at") or ""),
            "exact_screen": (
                inline_screen
                if inline_screen is not None
                else _read_candidate_screen(candidate_id, workspace, universe)
            ),
        }

    def ranking(row):
        metrics = row["metrics"]
        annualized = metrics.get("net_annualized_return")
        information_ratio = metrics.get("net_information_ratio")
        return (
            -(annualized if isinstance(annualized, (int, float)) else -math.inf),
            -(information_ratio if isinstance(information_ratio, (int, float)) else -math.inf),
            row["candidate_id"],
        )

    rows = sorted(candidates.values(), key=ranking)
    for rank, row in enumerate(rows, 1):
        row["pareto_rank"] = rank
    return rows


def _link_pareto_backtests(candidates: list[dict], backtests: list[dict]) -> None:
    """Attach a display round only after loop identity and metrics agree."""
    metric_pairs = (
        ("net_annualized_return", "annualized_return"),
        ("max_drawdown", "max_drawdown"),
        ("ic", "ic"),
    )
    for candidate in candidates:
        candidate["backtest_round"] = None
        candidate["backtest_link_valid"] = False
        history_index = candidate.get("history_index")
        matches = [
            row
            for row in backtests
            if isinstance(row, dict) and row.get("loop_index") == history_index
        ]
        if len(matches) != 1:
            continue
        backtest = matches[0]
        metrics = candidate.get("metrics") or {}
        comparable = True
        for candidate_key, backtest_key in metric_pairs:
            left = metrics.get(candidate_key)
            right = backtest.get(backtest_key)
            if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                comparable = False
                break
            if not math.isclose(float(left), float(right), rel_tol=1e-5, abs_tol=5e-6):
                comparable = False
                break
        if not comparable:
            continue
        candidate["backtest_round"] = backtest.get("round")
        candidate["backtest_link_valid"] = True


def _read_residual_screen(name: str, route: str, universe: str):
    winner_prefix = "fund_winner_resid" if route == "fund" else "ohlcv_winner_resid"
    winner_path = RESID_ARCHIVE / f"{winner_prefix}_{name}.json"
    if winner_path.exists():
        payload = _read_screen_payload(winner_path)
        # Never let an invalid winner-specific publication artifact fall back to
        # an older global screen: doing so could credit a different workspace.
        if (
            payload is None
            or payload.get("scope") != "exact_workspace"
            or not _valid_exact_workspace(payload.get("workspace"))
            or payload.get("universe") != universe
        ):
            return None, None, [], "missing_or_invalid"
        counts = _screen_counts(payload)
        return (
            (*counts, "exact_winner")
            if counts is not None
            else (None, None, [], "missing_or_invalid")
        )

    # Compatibility only for traces archived before winner-specific exact screens
    # existed.  These files have no reliable workspace identity.
    legacy_prefix = "fund_resid" if route == "fund" else "ohlcv_resid"
    legacy_path = RESID_ARCHIVE / f"{legacy_prefix}_{name}.json"
    if not legacy_path.exists():
        return None, None, [], "missing_or_invalid"
    payload = _read_screen_payload(legacy_path)
    if payload is None or payload.get("universe") != universe:
        return None, None, [], "missing_or_invalid"
    counts = _screen_counts(payload)
    return (
        (*counts, "legacy_global")
        if counts is not None
        else (None, None, [], "missing_or_invalid")
    )


def build():
    runs = []
    for trace_dir in sorted(glob.glob(str(TRACES / "mine*_*"))):
        name = os.path.basename(trace_dir)
        parsed = parse_trace(name)
        if not parsed:
            continue
        route, universe, timestamp = parsed
        session = Path(trace_dir) / "__session__"
        loops = (
            sum(1 for loop_dir in session.iterdir() if (loop_dir / "4_record").exists())
            if session.exists()
            else 0
        )

        (
            text,
            failure_tail,
            semantic_text,
            parsed_backtests,
            parsed_unevaluated,
        ) = _read_run_logs(name)
        factors = sorted(set(_FACTOR_RE.findall(semantic_text)))
        model_matches = re.findall(r"Using chat model (\S+)", semantic_text)
        if not model_matches:
            model_matches = re.findall(r"chat_model='([^']+)'", semantic_text)
        if not model_matches:
            model_matches = re.findall(r"Using chat model (\S+)", text)
        if not model_matches:
            model_matches = re.findall(r"chat_model='([^']+)'", text)
        model = model_matches[-1].replace("openai/", "") if model_matches else ""
        archived_backtests, archived_unevaluated = _read_backtest_archive(name)
        if parsed_backtests and len(parsed_backtests) >= len(archived_backtests):
            backtests, unevaluated_factors = parsed_backtests, parsed_unevaluated
            _write_backtest_archive(name, backtests, unevaluated_factors)
        elif archived_backtests:
            backtests, unevaluated_factors = archived_backtests, archived_unevaluated
        else:
            backtests, unevaluated_factors = parsed_backtests, parsed_unevaluated
        evaluations_done = len(backtests)
        evaluated_factors = _evaluated_factor_names(backtests)

        n_pass, n_eval, passed, screen_source = _read_residual_screen(
            name, route, universe
        )
        pareto_candidates = _read_pareto_candidates(name, universe)
        _link_pareto_backtests(pareto_candidates, backtests)
        valid_pareto_screens = [
            row["exact_screen"]
            for row in pareto_candidates
            if row["exact_screen"].get("valid") is True
        ]
        state, outcome, error_code = classify_run(
            text, loops, evaluations_done, n_pass, failure_tail
        )
        runs.append(
            {
                "trace": name,
                "route": route,
                "universe": universe,
                "time": timestamp,
                "loops_done": loops,
                "evaluations_done": evaluations_done,
                "backtests": backtests,
                "accepted_backtests": sum(row["accepted"] is True for row in backtests),
                # Keep discovery and evaluation counts separate.  A crashed tail
                # loop can mention/calculate factors without ever producing a
                # portfolio metric; those factors must not be reported as tested.
                "evaluated_factors": evaluated_factors,
                "n_evaluated_factors": len(evaluated_factors),
                "unevaluated_factors": unevaluated_factors,
                "n_unevaluated_factors": len(unevaluated_factors),
                "factor_count_exact": bool(backtests),
                "factors": factors,
                "n_factors": len(factors),
                "model": model,
                "n_pass": n_pass,
                "n_eval": n_eval,
                "passed": passed,
                "screen_source": screen_source,
                "pareto_candidates": pareto_candidates,
                "pareto_candidate_count": len(pareto_candidates),
                "pareto_screened_candidates": len(valid_pareto_screens),
                "pareto_n_pass": sum(
                    int(screen["n_pass"]) for screen in valid_pareto_screens
                ),
                "pareto_n_eval": sum(
                    int(screen["screened"]) for screen in valid_pareto_screens
                ),
                "state": state,
                "error_code": error_code,
                "outcome": outcome,
            }
        )

    runs.sort(key=lambda row: row["time"], reverse=True)
    payload = {"runs": runs[:200], "n": len(runs)}
    nas_out = _publish_payload(payload)
    print(f"[build_all_mine_history] {len(runs)} runs -> {OUT} + {nas_out}")
    return runs


if __name__ == "__main__":
    build()
