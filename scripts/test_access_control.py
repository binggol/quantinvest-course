from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from flask import g, session

import app as app_module
from scripts.membership_auth import MembershipStore


@contextmanager
def configured_app(tmp_path: Path, monkeypatch):
    old_db = app_module.MEMBERS_DB
    old_signature = app_module._membership_init_signature
    old_secret_state = app_module._secret_is_strong
    app_module.MEMBERS_DB = tmp_path / "members.db"
    app_module._membership_init_signature = None
    app_module._secret_is_strong = True
    monkeypatch.setenv("QI_AUTH_ENABLED", "1")
    app_module.app.config.update(TESTING=True, CSRF_TESTING=True)
    try:
        yield MembershipStore(app_module.MEMBERS_DB)
    finally:
        app_module.MEMBERS_DB = old_db
        app_module._membership_init_signature = old_signature
        app_module._secret_is_strong = old_secret_state
        app_module.app.config.pop("CSRF_TESTING", None)


def make_member(store: MembershipStore, email: str, *, plan: str = "basic", role: str = "member"):
    return store.upsert_member(
        email=email,
        password="A-strong-test-password-2026",
        role=role,
        status="active",
        membership_until="2099-12-31" if role == "member" else None,
        plan=plan,
    )


def sign_in(client, member: dict, csrf: str = "test-csrf-token") -> str:
    with client.session_transaction() as sess:
        sess["member_id"] = member["id"]
        sess["member_email"] = member["email"]
        sess["session_version"] = member["session_version"]
        sess["csrf_token"] = csrf
    return csrf


def test_plan_and_internal_page_boundaries(tmp_path: Path, monkeypatch) -> None:
    with configured_app(tmp_path, monkeypatch) as store:
        basic = make_member(store, "basic@example.com")
        pro = make_member(store, "pro@example.com", plan="data_pro")
        admin = make_member(store, "admin@example.com", plan="enterprise", role="admin")

        basic_client = app_module.app.test_client()
        sign_in(basic_client, basic)
        basic_market = basic_client.get("/")
        assert basic_market.status_code == 200
        assert "行情与 K 线" in basic_market.get_data(as_text=True)
        assert "因子叠加" not in basic_market.get_data(as_text=True)
        assert "/api/money_outflow" not in basic_client.get("/watchlist").get_data(as_text=True)
        assert basic_client.get("/screen").status_code == 403
        assert basic_client.get("/trade").status_code == 403

        pro_client = app_module.app.test_client()
        sign_in(pro_client, pro)
        assert pro_client.get("/screen").status_code == 200
        assert pro_client.get("/backtest").status_code == 403
        assert pro_client.get("/research").status_code == 403
        assert "RD-Agent" not in pro_client.get("/fund-features").get_data(as_text=True)
        assert pro_client.get("/trade").status_code == 403

        admin_client = app_module.app.test_client()
        sign_in(admin_client, admin)
        assert admin_client.get("/trade").status_code == 200
        assert admin_client.get("/backtest").status_code == 200
        assert admin_client.get("/admin/members").status_code == 200


def test_member_watchlists_are_isolated(tmp_path: Path, monkeypatch) -> None:
    with configured_app(tmp_path, monkeypatch) as store:
        first = make_member(store, "first@example.com")
        second = make_member(store, "second@example.com")

        first_client = app_module.app.test_client()
        token = sign_in(first_client, first)
        response = first_client.post(
            "/api/watchlist/add",
            json={"code": "sh600519"},
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 200

        second_client = app_module.app.test_client()
        sign_in(second_client, second)
        data_store = app_module.MemberDataStore(app_module.MEMBERS_DB)
        assert data_store.get(first["id"], "watchlist") == ["sh600519"]
        assert data_store.get(second["id"], "watchlist", default=[]) == []


def test_member_data_apis_redact_internal_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")
    monkeypatch.setattr(app_module, "IPO_JSON", tmp_path / "ipo.json")
    monkeypatch.setattr(app_module, "FUND_FEATURES_META", tmp_path / "fund_features.json")
    (tmp_path / "ipo.json").write_text(json.dumps({
        "updated": "2026-07-11", "strategy": "internal action", "note": "internal note",
        "today_buy": [{"name": "Example"}], "soon_buy": [], "just_listed": [],
    }), encoding="utf-8")
    (tmp_path / "fund_features.json").write_text(json.dumps({
        "n_codes": 10,
        "features": [
            {"name": "gm", "coverage": 0.9, "hist": [1, 2]},
            {"name": "repo_decay", "coverage": 0.8, "hist": [3, 4]},
        ],
    }), encoding="utf-8")
    (tmp_path / "industry.json").write_text(json.dumps({
        "rows": [{"industry": "制造", "n": 20, "dt_yoy": 5, "top_stocks": [{"code": "000001"}]}],
    }), encoding="utf-8")

    with configured_app(tmp_path, monkeypatch) as store:
        pro = make_member(store, "pro-redaction@example.com", plan="data_pro")
        client = app_module.app.test_client()
        sign_in(client, pro)

        ipo = client.get("/api/ipo").get_json()
        assert "strategy" not in ipo and "note" not in ipo
        features = client.get("/api/fund_features").get_json()["features"]
        assert [feature["name"] for feature in features] == ["gm"]
        assert "hist" not in features[0]
        industry = client.get("/api/industry").get_json()["rows"][0]
        assert "top_stocks" not in industry


def test_csrf_and_job_parameter_validation(tmp_path: Path, monkeypatch) -> None:
    with configured_app(tmp_path, monkeypatch) as store:
        admin = make_member(store, "admin@example.com", plan="enterprise", role="admin")
        client = app_module.app.test_client()
        token = sign_in(client, admin)

        assert client.post("/api/refresh/industry").status_code == 400
        response = client.post(
            "/api/rdagent/request?batch=bad%27%3Bwhoami&model=lgb",
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 400


def test_true_boolean_enables_auth(monkeypatch) -> None:
    monkeypatch.setenv("QI_AUTH_ENABLED", "true")
    assert app_module._auth_enabled() is True


def test_auth_disabled_preview_can_use_member_tier(monkeypatch) -> None:
    monkeypatch.setenv("QI_AUTH_ENABLED", "0")
    monkeypatch.setenv("QI_DEV_ROLE", "member")
    monkeypatch.setenv("QI_DEV_PLAN", "basic")
    client = app_module.app.test_client()
    assert client.get("/").status_code == 200
    assert "行情与 K 线" in client.get("/").get_data(as_text=True)
    assert client.get("/screen").status_code == 403


def test_next_url_rejects_backslash_redirects() -> None:
    with app_module.app.test_request_context("/login"):
        assert app_module._safe_next_url("/member") == "/member"
        assert app_module._safe_next_url("/\\example.com") == "/"


def test_authenticated_template_context_never_contains_password_hash(tmp_path: Path, monkeypatch) -> None:
    with configured_app(tmp_path, monkeypatch) as store:
        member = make_member(store, "safe-context@example.com")
        with app_module.app.test_request_context("/account"):
            session["member_id"] = member["id"]
            session["member_email"] = member["email"]
            session["session_version"] = member["session_version"]
            session["csrf_token"] = "test-csrf-token"

            assert app_module.require_membership() is None
            assert "password_hash" not in g.current_member


@pytest.mark.parametrize(
    ("path_name", "url", "body"),
    [
        ("RDAGENT_REQUEST", "/api/rdagent/run_all?batch=bad%27%3Bwhoami", None),
        ("RDAGENT_REQUEST", "/api/rdagent/model_eval?model=lgb&batch=bad%27%3Bwhoami", None),
        ("RDAGENT_REQUEST", "/api/strategy/request?model=lgb&batch=bad%27%3Bwhoami", None),
        ("FUND_COMPARE_REQUEST", "/api/fund_compare/run", {"batch": "bad';whoami", "model": "lgb"}),
        ("BATCH_PREDICT_REQUEST", "/api/batch_predict/run", {"batch": "bad';whoami", "model": "lgb"}),
        ("BATCH_ARENA_REQUEST", "/api/batch_arena/run", {"batch": "bad';whoami", "model": "lgb"}),
        (
            "FACTOR_REQUEST",
            "/api/factor/request?code=sh600519&factor=range_weighted_clv_5d&batch=bad%27%3Bwhoami",
            None,
        ),
    ],
)
def test_job_apis_reject_unsafe_batch_labels_before_writing_request(
    tmp_path: Path,
    monkeypatch,
    path_name: str,
    url: str,
    body: dict | None,
) -> None:
    request_file = tmp_path / f"{path_name.lower()}.json"
    monkeypatch.setattr(app_module, path_name, request_file)
    with configured_app(tmp_path, monkeypatch) as store:
        admin = make_member(store, "job-admin@example.com", plan="enterprise", role="admin")
        client = app_module.app.test_client()
        token = sign_in(client, admin)
        response = client.post(url, json=body, headers={"X-CSRF-Token": token})

    assert response.status_code == 400
    assert not request_file.exists()


def test_tradingagents_rejects_invalid_tickers_and_dates(tmp_path: Path, monkeypatch) -> None:
    request_file = tmp_path / "ta_request.json"
    monkeypatch.setattr(app_module, "TA_REQUEST", request_file)
    with configured_app(tmp_path, monkeypatch) as store:
        admin = make_member(store, "ta-admin@example.com", plan="enterprise", role="admin")
        client = app_module.app.test_client()
        token = sign_in(client, admin)

        bad_ticker = client.post(
            "/api/tradingagents/analyze?tickers=sh600519%3Bwhoami&date=2026-07-10",
            headers={"X-CSRF-Token": token},
        )
        bad_date = client.post(
            "/api/tradingagents/analyze?tickers=sh600519&date=not-a-date",
            headers={"X-CSRF-Token": token},
        )

    assert bad_ticker.status_code == 400
    assert bad_date.status_code == 400
    assert not request_file.exists()


def test_login_failure_cache_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "_LOGIN_ATTEMPT_KEY_LIMIT", 3)
    app_module._login_attempts.clear()
    try:
        for index in range(10):
            app_module._record_login_failure(f"127.0.0.1|user-{index}@example.com")
        assert len(app_module._login_attempts) <= 3
    finally:
        app_module._login_attempts.clear()


def test_login_ip_limit_cannot_be_bypassed_by_rotating_emails(monkeypatch) -> None:
    monkeypatch.setattr(app_module, "_LOGIN_IP_ATTEMPT_LIMIT", 3)
    app_module._login_attempts.clear()
    try:
        with app_module.app.test_request_context("/login", environ_base={"REMOTE_ADDR": "203.0.113.9"}):
            ip_key = app_module._login_ip_key()
            for _ in range(3):
                app_module._record_login_failure(ip_key)
            assert app_module._login_is_limited(
                ip_key,
                limit=app_module._LOGIN_IP_ATTEMPT_LIMIT,
            )
    finally:
        app_module._login_attempts.clear()


def test_mutating_api_rejects_non_object_json_before_endpoint(tmp_path: Path, monkeypatch) -> None:
    request_file = tmp_path / "universe_arena_request.json"
    monkeypatch.setattr(app_module, "UNIVERSE_ARENA_REQUEST", request_file)
    with configured_app(tmp_path, monkeypatch) as store:
        admin = make_member(store, "json-admin@example.com", plan="enterprise", role="admin")
        client = app_module.app.test_client()
        token = sign_in(client, admin)

        response = client.post(
            "/api/universe_arena/request",
            json=["csi300", "lgb"],
            headers={"X-CSRF-Token": token},
        )

    assert response.status_code == 400
    assert response.get_json() == {"error": "JSON object required"}
    assert not request_file.exists()


def test_authenticated_and_login_responses_are_not_cacheable(tmp_path: Path, monkeypatch) -> None:
    with configured_app(tmp_path, monkeypatch) as store:
        member = make_member(store, "cache-safe@example.com")
        client = app_module.app.test_client()
        sign_in(client, member)

        account = client.get("/account")
        login = client.get("/login")
        static_asset = client.get("/static/security.js")

    assert "no-store" in account.headers.get("Cache-Control", "")
    assert "no-store" in login.headers.get("Cache-Control", "")
    assert "no-store" not in static_asset.headers.get("Cache-Control", "")


def test_file_selecting_apis_reject_unsafe_names(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "PREDICT_JSON", tmp_path / "predictions.json")
    monkeypatch.setattr(app_module, "RDAGENT_BATCHES", tmp_path / "rdagent_batches.json")
    with configured_app(tmp_path, monkeypatch) as store:
        admin = make_member(store, "file-admin@example.com", plan="enterprise", role="admin")
        client = app_module.app.test_client()
        token = sign_in(client, admin)

        ensemble = client.post(
            "/api/ensemble",
            json={"models": ["../../secret"], "scheme": "equal"},
            headers={"X-CSRF-Token": token},
        )
        batch = client.get("/api/batch_predict?batch=bad%2Apattern&u=csi300")

    assert ensemble.status_code == 400
    assert batch.status_code == 400
