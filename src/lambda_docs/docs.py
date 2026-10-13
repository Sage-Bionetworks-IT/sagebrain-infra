"""GET /api, /api/openapi.json and /api/openapi.yaml for the unified API (spec 001 FR-8).

Public: these routes have no authorizer. The asset (built by bundle.py) holds the committed
api/openapi.yaml and Swagger UI's script and stylesheet, which are inlined into the page so
it loads nothing from a third-party CDN and needs no extra routes.
"""

import copy
import functools
import json
import os
from pathlib import Path

import yaml

ASSET_DIR = Path(os.environ.get("DOCS_ASSET_DIR", Path(__file__).parent))

COMMON_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "public, max-age=300",
}

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div id="swagger-ui"></div>
<script>{js}</script>
<script>
window.ui = SwaggerUIBundle({{ url: {spec_url}, dom_id: "#swagger-ui", deepLinking: true }});
</script>
</body>
</html>
"""


@functools.cache
def _spec() -> dict:
    return yaml.safe_load((ASSET_DIR / "openapi.yaml").read_text())


@functools.cache
def _asset(name: str) -> str:
    text = (ASSET_DIR / "swagger-ui" / name).read_text()
    # Never let asset text close the element it is inlined into
    return text.replace("</script", "<\\/script").replace("</style", "<\\/style")


def _deployed_spec(request_context: dict) -> dict:
    """The contract with `servers` set to the stage this request came through."""
    spec = copy.deepcopy(_spec())
    base_url = f"https://{request_context['domainName']}/{request_context['stage']}"
    spec["servers"] = [{"url": base_url, "description": "This deployment"}]
    return spec


def _response(body: str, content_type: str) -> dict:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": content_type, **COMMON_HEADERS},
        "body": body,
    }


def handler(event, context):
    request_context = event["requestContext"]
    resource = event["resource"]

    if resource == "/api/openapi.json":
        return _response(
            json.dumps(_deployed_spec(request_context)), "application/json"
        )
    if resource == "/api/openapi.yaml":
        return _response(
            yaml.safe_dump(
                _deployed_spec(request_context), sort_keys=False, allow_unicode=True
            ),
            "application/yaml",
        )
    if resource == "/api":
        # requestContext.path includes the stage prefix, e.g. /prod/api
        spec_url = request_context["path"].rstrip("/") + "/openapi.json"
        page = _PAGE.format(
            title=_spec()["info"]["title"],
            css=_asset("swagger-ui.css"),
            js=_asset("swagger-ui-bundle.js"),
            spec_url=json.dumps(spec_url),
        )
        return _response(page, "text/html; charset=utf-8")
    return {
        "statusCode": 404,
        "headers": {"Content-Type": "application/json", **COMMON_HEADERS},
        "body": json.dumps({"message": "Not Found"}),
    }
