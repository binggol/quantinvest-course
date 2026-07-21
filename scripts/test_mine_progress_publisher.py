import importlib.util
import json
from pathlib import Path


SOURCE = Path(__file__).parent / "rdagent_backup" / "_mine_progress_pub.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("mine_progress_pub", SOURCE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _configure_attempt(module, tmp_path, monkeypatch, *, request="request-123", attempt="attempt-a"):
    status_path = tmp_path / "rdagent_status.json"
    lease_path = tmp_path / "attempt.running"
    lease_path.touch()
    status_path.write_text(
        json.dumps(
            {
                "state": "running",
                "msg": "starting",
                "request_id": request,
                "attempt_id": attempt,
                "requested_at": "2026-07-14 20:40:22",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ST", str(status_path))
    monkeypatch.setenv(module.REQUEST_ID_ENV, request)
    monkeypatch.setenv(module.ATTEMPT_ID_ENV, attempt)
    monkeypatch.setenv(module.LEASE_PATH_ENV, str(lease_path))
    monkeypatch.delenv(module.OWNER_PID_ENV, raising=False)
    return status_path, lease_path


def test_progress_write_uses_exact_attempt_identity_and_is_atomic(tmp_path, monkeypatch):
    module = _load_module()
    status_path, _ = _configure_attempt(module, tmp_path, monkeypatch)
    monkeypatch.setenv(module.REQUESTED_AT_ENV, "2026-07-14 20:40:22")

    assert module.write("Loop 1") is True

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status == {
        "state": "running",
        "msg": "Loop 1",
        "updated_at": status["updated_at"],
        "request_id": "request-123",
        "attempt_id": "attempt-a",
        "requested_at": "2026-07-14 20:40:22",
    }
    assert list(tmp_path.glob(".rdagent_status.json.*.tmp")) == []


def test_progress_write_rejects_missing_attempt_environment(tmp_path, monkeypatch):
    module = _load_module()
    status_path, _ = _configure_attempt(module, tmp_path, monkeypatch)
    before = status_path.read_bytes()
    monkeypatch.delenv(module.ATTEMPT_ID_ENV)

    assert module.write("stale progress") is False
    assert status_path.read_bytes() == before


def test_progress_write_rejects_terminal_other_request_and_new_attempt(tmp_path, monkeypatch):
    module = _load_module()
    status_path, _ = _configure_attempt(module, tmp_path, monkeypatch)

    variants = [
        {"state": "done", "request_id": "request-123", "attempt_id": "attempt-a"},
        {"state": "running", "request_id": "request-new", "attempt_id": "attempt-a"},
        {"state": "running", "request_id": "request-123", "attempt_id": "attempt-b"},
    ]
    for status in variants:
        status["msg"] = "do not replace"
        status_path.write_text(json.dumps(status), encoding="utf-8")
        before = status_path.read_bytes()
        assert module.write("stale progress") is False
        assert status_path.read_bytes() == before


def test_progress_write_stops_when_lease_is_removed(tmp_path, monkeypatch):
    module = _load_module()
    status_path, lease_path = _configure_attempt(module, tmp_path, monkeypatch)
    lease_path.unlink()
    before = status_path.read_bytes()

    assert module.attempt_alive() is False
    assert module.write("late progress") is False
    assert status_path.read_bytes() == before


def test_main_reads_only_the_configured_log_and_exits_with_lease(tmp_path, monkeypatch):
    module = _load_module()
    _, lease_path = _configure_attempt(module, tmp_path, monkeypatch)
    assigned = tmp_path / "mine_csi300_20260718_010000.log"
    unrelated = tmp_path / "mine_csi1000_20260718_020000.log"
    assigned.write_text(
        "Start Loop 7, Step 3: coding\nUsing chat model openai/model-a\nFile Factor[assigned_factor]\n",
        encoding="utf-8",
    )
    unrelated.write_text("File Factor[wrong_factor]\n", encoding="utf-8")
    monkeypatch.setenv(module.LOG_PATH_ENV, str(assigned))
    messages = []
    monkeypatch.setattr(module, "write", lambda message: messages.append(message) or True)

    def stop_after_first_iteration(_seconds):
        lease_path.unlink()

    monkeypatch.setattr(module.time, "sleep", stop_after_first_iteration)
    module.main()

    assert len(messages) == 1
    assert "csi300" in messages[0]
    assert "Loop 7 Step 3:coding" in messages[0]
    assert "assigned_factor" in messages[0]
    assert "wrong_factor" not in messages[0]


def test_progress_uses_split_stdout_for_semantics_and_freshness(tmp_path, monkeypatch):
    module = _load_module()
    stderr = tmp_path / "mine_csi300_20260719_203214.log"
    stdout = tmp_path / "mine_csi300_20260719_203214.log.stdout.log"
    stderr.write_text("tqdm progress only\n", encoding="utf-8")
    stdout.write_text(
        "Start Loop 4, Step 1: coding\n"
        "Using chat model openai/k3\n"
        "File Factor[range_weighted_clv_10d]\n",
        encoding="utf-8",
    )
    now = 2_000_000_000
    old = now - (30 * 60)
    fresh = now - (2 * 60)
    os = module.os
    os.utime(stderr, (old, old))
    os.utime(stdout, (fresh, fresh))
    monkeypatch.setattr(module.time, "time", lambda: now)

    message = module.format_progress(str(stderr), str(stdout))

    assert "Loop 4 Step 1:coding" in message
    assert "模型k3" in message
    assert "range_weighted_clv_10d" in message
    assert "日志2.0分前" in message
    assert "可能卡住" not in message


def test_progress_warns_only_after_both_logs_are_stale(tmp_path, monkeypatch):
    module = _load_module()
    stderr = tmp_path / "mine_csi300_20260719_203214.log"
    stdout = tmp_path / "mine_csi300_20260719_203214.log.stdout.log"
    stderr.write_text("progress\n", encoding="utf-8")
    stdout.write_text("Start Loop 1, Step 1: coding\n", encoding="utf-8")
    now = 2_000_000_000
    stale = now - ((module.STALE_WARNING_MINUTES + 1) * 60)
    module.os.utime(stderr, (stale, stale))
    module.os.utime(stdout, (stale, stale))
    monkeypatch.setattr(module.time, "time", lambda: now)

    assert "可能卡住" in module.format_progress(str(stderr), str(stdout))


def test_configured_stdout_log_is_bound_to_the_assigned_attempt(tmp_path, monkeypatch):
    module = _load_module()
    assigned = tmp_path / "mine_csi300_20260719_203214.log"
    foreign = tmp_path / "mine_csi1000_20260719_210000.log.stdout.log"
    monkeypatch.setenv(module.LOG_PATH_ENV, str(assigned))
    monkeypatch.setenv(module.STDOUT_LOG_PATH_ENV, str(foreign))

    assert module.configured_stdout_log() == f"{assigned}.stdout.log"
