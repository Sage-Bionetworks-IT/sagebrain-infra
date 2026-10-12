"""T117a — API docs at /api (FR-9): self-hosted Swagger UI and the committed contract."""

import copy
import re
from pathlib import Path

import pytest
import yaml

SPEC = yaml.safe_load((Path(__file__).parents[3] / "api" / "openapi.yaml").read_text())


def _served_spec(settings):
    expected = copy.deepcopy(SPEC)
    expected["servers"] = [{"url": settings.public_base_url}]
    return expected


def test_swagger_ui_html_uses_no_external_hosts(client):
    response = client.get("/api")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    html = response.text
    assert "SwaggerUIBundle" in html
    assert "/api/openapi.json" in html
    assert "://" not in html and "'//" not in html and '"//' not in html
    refs = re.findall(r"""(?:src|href)=["']([^"']+)["']""", html)
    assert refs, "expected the page to load its css/js"
    assert all(ref.startswith("/api/static/") for ref in refs), refs


@pytest.mark.parametrize("asset", ["swagger-ui.css", "swagger-ui-bundle.js"])
def test_swagger_assets_are_served_locally(client, asset):
    response = client.get(f"/api/static/{asset}")
    assert response.status_code == 200


def test_openapi_json_is_the_committed_spec_with_servers_replaced(client, settings):
    response = client.get("/api/openapi.json")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == _served_spec(settings)


def test_openapi_yaml_is_the_same_document(client, settings):
    response = client.get("/api/openapi.yaml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    assert yaml.safe_load(response.text) == _served_spec(settings)


@pytest.mark.parametrize(
    "path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]
)
def test_fastapi_default_docs_are_disabled(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "path", ["/api", "/api/openapi.json", "/api/openapi.yaml", "/healthz"]
)
def test_docs_and_health_need_no_auth(client, synapse, path):
    assert client.get(path).status_code == 200
    assert synapse.calls == []


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["access-control-allow-origin"] == "*"
