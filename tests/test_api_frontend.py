"""The dashboard, served beside the API, and the contract between the two.

``front-end/index.html`` is the approved prototype with its hardcoded arrays
replaced by calls to ``/api``. Two things can break that silently and are
checked here without a browser: the page calling an endpoint the API does not
have (a typo is a 404 nobody sees until the screen is empty), and the page
still carrying the prototype's literal data, which would show fixtures as if
they came from the API. The first test is the contract test: every
``api(method, path)`` call in the page must name a route and method in the
API's own OpenAPI document.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from updater.api import create_app
from updater.api.mock import MockState
from updater.cli import FRONTEND_DIR, main

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "front-end" / "index.html"
PROTOTYPE = ROOT / "docs" / "frontend" / "asil-dashboard-DRAFT-FINAL.html"
PROTOTYPE_DAY = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)

API_CALL = re.compile(r"api\('(GET|POST|PUT|PATCH|DELETE)', [`']([^`']+)[`']")


def page_script() -> str:
    html = DASHBOARD.read_text(encoding="utf-8")
    scripts: list[str] = re.findall(r"<script>(.*?)</script>", html, flags=re.S)
    [script] = scripts
    return script


def as_route(path: str) -> str:
    """``/runs/${rid()}/claims`` -> ``/runs/{}/claims``, for matching."""
    return re.sub(r"\$\{[^}]+\}", "{}", path)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    (tmp_path / "index.html").write_text("<h1>the dashboard</h1>", encoding="utf-8")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "summary").write_text("a file, not the API", encoding="utf-8")
    state = MockState(clock=lambda: PROTOTYPE_DAY)
    with TestClient(create_app(state, frontend=tmp_path)) as client:
        yield client


def test_every_call_the_dashboard_makes_is_an_api_endpoint() -> None:
    calls = API_CALL.findall(page_script())
    openapi = create_app(MockState(clock=lambda: PROTOTYPE_DAY)).openapi()["paths"]
    routes = {
        (method.upper(), re.sub(r"\{[^}]+\}", "{}", path.removeprefix("/api")))
        for path, operations in openapi.items()
        for method in operations
    }

    assert len(calls) >= 15, "the call pattern stopped matching the page"
    missing = sorted({(method, as_route(path)) for method, path in calls} - routes)
    assert missing == [], f"the page calls endpoints the API does not have: {missing}"


def test_the_page_reads_every_screen_the_api_serves() -> None:
    """The reverse direction, for the screens: an endpoint the page never calls
    is a screen still showing the prototype's literal data."""
    called = {(m, as_route(p)) for m, p in API_CALL.findall(page_script())}
    for route in [
        ("GET", "/summary"),
        ("GET", "/activity"),
        ("GET", "/notifications"),
        ("GET", "/sources"),
        ("POST", "/sources"),
        ("PATCH", "/sources/{}"),
        ("POST", "/runs"),
        ("GET", "/runs/{}"),
        ("GET", "/runs/{}/claims"),
        ("PUT", "/claims/{}/decision"),
        ("GET", "/runs/{}/draft"),
        ("POST", "/claims/{}/review"),
        ("GET", "/runs/{}/audit"),
        ("GET", "/runs/{}/publish"),
        ("POST", "/runs/{}/sign-off"),
        ("GET", "/settings"),
        ("PUT", "/settings"),
    ]:
        assert route in called, route
    assert "/export?format=" in page_script()


@pytest.mark.parametrize(
    "literal",
    [
        "const claims = [",
        "const draftText",
        "const notifs = [",
        "const outcomes = [",
        "Showing 12 of 42",
        "<td>J. Ferreira</td>",
    ],
)
def test_the_page_carries_none_of_the_prototypes_literal_data(literal: str) -> None:
    assert literal not in DASHBOARD.read_text(encoding="utf-8")


@pytest.mark.parametrize("page", [DASHBOARD, PROTOTYPE], ids=["dashboard", "prototype"])
def test_no_page_carries_a_client_email_address(page: Path) -> None:
    assert "astartinlife.org.au" not in page.read_text(encoding="utf-8")


def test_the_page_loads_nothing_from_elsewhere_but_its_fonts() -> None:
    html = DASHBOARD.read_text(encoding="utf-8")
    origins = set(re.findall(r'(?:src|href)="(https?://[^/"]+)', html))

    assert origins <= {"https://fonts.googleapis.com", "https://fonts.gstatic.com"}
    assert "const API = '/api';" in page_script(), "same origin, never a host"


def test_api_values_go_into_markup_escaped() -> None:
    """Spot-check the habit rather than every line: the fields most likely to
    hold text a person or a model typed are escaped or reduced to safe tags."""
    script = page_script()
    for wrapped in (
        "esc(c.title)",
        "esc(s.name)",
        "esc(t.passage)",
        "esc(text)",
        "rich(c.note)",
        "rich(n.body)",
    ):
        assert wrapped in script, wrapped
    assert "m.textContent = text" in script, "a typed chat message is text"


# --------------------------------------------------------------------------- #
# Serving
# --------------------------------------------------------------------------- #


def test_the_dashboard_is_served_at_the_root(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "the dashboard" in response.text


def test_the_api_wins_over_a_file_of_the_same_path(client: TestClient) -> None:
    response = client.get("/api/summary")

    assert response.headers["content-type"].startswith("application/json")
    assert response.json()[0]["label"] == "Sources approved"


def test_without_a_frontend_the_app_is_the_api_alone() -> None:
    with TestClient(create_app(MockState(clock=lambda: PROTOTYPE_DAY))) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/summary").status_code == 200


def _capture_serve(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    import uvicorn

    apps: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: apps.append(app))
    return apps


def test_serve_serves_the_repository_dashboard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    apps = _capture_serve(monkeypatch)

    assert main(["serve"]) == 0

    assert DASHBOARD.parent == FRONTEND_DIR
    assert "dashboard: http://127.0.0.1:8000/" in capsys.readouterr().out
    with TestClient(apps[0]) as client:
        page = client.get("/")
    assert page.status_code == 200
    assert 'id="dashStats"' in page.text


def test_serve_with_no_frontend_is_the_api_alone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    apps = _capture_serve(monkeypatch)

    assert main(["serve", "--no-frontend"]) == 0

    assert "not served" in capsys.readouterr().out
    with TestClient(apps[0]) as client:
        assert client.get("/").status_code == 404


def test_serve_refuses_a_frontend_directory_with_no_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    apps = _capture_serve(monkeypatch)

    assert main(["serve", "--frontend", str(tmp_path)]) == 2

    assert apps == []
    assert "no index.html" in capsys.readouterr().err
