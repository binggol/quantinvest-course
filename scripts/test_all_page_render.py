from __future__ import annotations

import inspect

import app as app_module


def _simple_page_routes():
    routes = []
    for rule in app_module.app.url_map.iter_rules():
        path = str(rule)
        if rule.endpoint == "static" or "GET" not in rule.methods or "<" in path:
            continue
        view = app_module.app.view_functions[rule.endpoint]
        try:
            source = inspect.getsource(view)
        except (OSError, TypeError):
            continue
        if "render_template" in source:
            routes.append(path)
    return sorted(set(routes))


def test_every_simple_page_route_renders_for_admin_preview(monkeypatch):
    monkeypatch.setenv("QI_AUTH_ENABLED", "0")
    monkeypatch.setenv("QI_DEV_ROLE", "admin")
    monkeypatch.setenv("QI_DEV_PLAN", "enterprise")
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()

    failures = []
    for path in _simple_page_routes():
        response = client.get(path)
        expected = 302 if path == "/membership-expired" else 200
        if response.status_code != expected:
            failures.append((path, response.status_code, response.get_data(as_text=True)[:200]))

    assert len(_simple_page_routes()) >= 80
    assert not failures, failures
