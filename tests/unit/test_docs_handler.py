"""Docs Lambda (src/lambda_docs/docs.py): Swagger UI at /api, the contract at /api/openapi.{json,yaml}."""

import importlib.util
import json
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
SPEC = yaml.safe_load((ROOT / "api" / "openapi.yaml").read_text())
REQUEST_CONTEXT = {
    "domainName": "abc123.execute-api.us-east-1.amazonaws.com",
    "stage": "prod",
}


@pytest.fixture
def docs(tmp_path, monkeypatch):
    """docs.py over an asset dir laid out the way bundle.py builds it, with stub Swagger UI."""
    shutil.copy(ROOT / "api" / "openapi.yaml", tmp_path / "openapi.yaml")
    (tmp_path / "swagger-ui").mkdir()
    (tmp_path / "swagger-ui" / "swagger-ui-bundle.js").write_text(
        'window.SwaggerUIBundle = function () {}; var s = "</script>";'
    )
    (tmp_path / "swagger-ui" / "swagger-ui.css").write_text(".swagger-ui { x: 1 }")
    monkeypatch.setenv("DOCS_ASSET_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "docs_under_test", ROOT / "src" / "lambda_docs" / "docs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _get(docs, resource):
    path = f"/prod{resource}"
    return docs.handler(
        {"resource": resource, "requestContext": {**REQUEST_CONTEXT, "path": path}},
        None,
    )


def _check_deployed_spec(document):
    assert document["servers"] == [
        {
            "url": "https://abc123.execute-api.us-east-1.amazonaws.com/prod",
            "description": "This deployment",
        }
    ]
    assert {k: v for k, v in document.items() if k != "servers"} == {
        k: v for k, v in SPEC.items() if k != "servers"
    }


def test_openapi_json_is_the_contract_with_this_stage_as_server(docs):
    response = _get(docs, "/api/openapi.json")
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/json"
    assert response["headers"]["Access-Control-Allow-Origin"] == "*"
    _check_deployed_spec(json.loads(response["body"]))


def test_openapi_yaml_is_the_same_document(docs):
    response = _get(docs, "/api/openapi.yaml")
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/yaml"
    _check_deployed_spec(yaml.safe_load(response["body"]))


def test_serving_does_not_mutate_the_loaded_contract(docs):
    _get(docs, "/api/openapi.json")
    assert docs._spec()["servers"] == SPEC["servers"]


def test_ui_inlines_swagger_ui_and_points_at_this_stage(docs):
    response = _get(docs, "/api")
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "text/html; charset=utf-8"
    page = response["body"]
    assert "<style>.swagger-ui { x: 1 }</style>" in page
    assert "window.SwaggerUIBundle = function" in page
    assert 'url: "/prod/api/openapi.json"' in page
    # nothing is loaded from another host
    assert "src=" not in page and "href=" not in page
    # inlined asset text can't close its <script> early
    assert page.count("</script>") == 2


def test_unknown_resource_is_404(docs):
    assert _get(docs, "/api/other")["statusCode"] == 404


def test_bundle_pins_swagger_ui_5_or_later():
    """Swagger UI 4 can't render OpenAPI 3.1."""
    spec = importlib.util.spec_from_file_location(
        "docs_bundle", ROOT / "src" / "lambda_docs" / "bundle.py"
    )
    bundle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    assert int(bundle.SWAGGER_UI_VERSION.split(".")[0]) >= 5
    assert bundle.SWAGGER_UI_INTEGRITY.startswith("sha512-")
    assert bundle.SWAGGER_UI_VERSION in bundle.SWAGGER_UI_URL
