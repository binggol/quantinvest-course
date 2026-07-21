from pathlib import Path

import yaml

from scripts import scheduler_runner


ROOT = Path(__file__).resolve().parents[1]


def _env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def test_compose_uses_one_image_and_one_build_owner() -> None:
    config = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    web = config["services"]["quantinvest"]
    scheduler = config["services"]["quantinvest-scheduler"]

    assert web["image"] == scheduler["image"] == "quantinvest:latest"
    assert web["build"] == "."
    assert "build" not in scheduler
    assert web["ports"] == ["${QI_PUBLISH_HOST:-127.0.0.1}:${QI_PUBLISH_PORT:-5055}:5055"]
    assert scheduler["command"] == ["python", "-u", "-m", "scripts.scheduler_runner"]
    health = scheduler["healthcheck"]
    assert health["test"][:3] == ["CMD", "python", "-c"]
    health_command = health["test"][3]
    assert "quantinvest_scheduler_heartbeat" in health_command
    assert "os.environ.get('QI_SCHEDULER_HEARTBEAT'" in health_command
    assert "os.environ.get('QI_SCHEDULER_HEARTBEAT_INTERVAL'" in health_command
    assert health["interval"] == "60s"
    assert health["start_period"] == "120s"


def test_runtime_and_secret_defaults_are_deployment_safe() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    env = _env_example()

    assert "gunicorn==" in requirements
    assert 'CMD ["gunicorn"' in dockerfile
    assert "curl -fsS http://localhost:5055/api/health" in dockerfile
    assert env["QI_AUTH_ENABLED"] == "1"
    assert env["QI_COOKIE_SECURE"] == "1"
    assert env["QI_TRUST_PROXY"] == "1"
    assert env["QI_PUBLISH_HOST"] == "127.0.0.1"
    for key in (
        "TUSHARE_TOKEN",
        "SECRET_KEY",
        "QI_ADMIN_EMAIL",
        "QI_ADMIN_PASSWORD",
    ):
        assert env[key] == ""


def test_scheduler_runner_can_import_app_from_direct_script_path() -> None:
    runner = (ROOT / "scripts" / "scheduler_runner.py").read_text(encoding="utf-8")
    assert "Path(__file__).resolve().parents[1]" in runner
    assert "sys.path.insert(0, str(ROOT))" in runner
    assert "if not scheduler.running" in runner
    assert "write_heartbeat()" in runner


def test_scheduler_heartbeat_is_atomically_published(tmp_path, monkeypatch) -> None:
    heartbeat = tmp_path / "scheduler.heartbeat"
    monkeypatch.setattr(scheduler_runner.time, "time", lambda: 12345.5)

    scheduler_runner.write_heartbeat(heartbeat)

    assert heartbeat.read_text(encoding="ascii") == "12345.5"
    assert not list(tmp_path.glob("*.tmp"))
