from __future__ import annotations

import pytest

import app as app_module


@pytest.fixture(autouse=True)
def deterministic_test_app(monkeypatch):
    """Run legacy endpoint tests in the explicit local-development profile."""
    old_testing = app_module.app.config.get("TESTING", False)
    old_csrf_testing = app_module.app.config.get("CSRF_TESTING")
    monkeypatch.setenv("QI_AUTH_ENABLED", "0")
    app_module.app.config["TESTING"] = True
    try:
        yield
    finally:
        app_module.app.config["TESTING"] = old_testing
        if old_csrf_testing is None:
            app_module.app.config.pop("CSRF_TESTING", None)
        else:
            app_module.app.config["CSRF_TESTING"] = old_csrf_testing
