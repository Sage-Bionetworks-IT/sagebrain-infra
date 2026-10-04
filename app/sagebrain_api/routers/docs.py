"""FR-9: the committed contract, published at /api. Public; no rate buckets (WAF still applies).

FastAPI's generated /docs, /redoc and /openapi.json are disabled in main.create_app, so the
hand-written api/openapi.yaml is the only published contract.
"""

from pathlib import Path

import yaml
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

router = APIRouter()

STATIC_PREFIX = "/api/static"

# Assets are served by this service from the image (no CDN). validatorUrl: null stops Swagger UI
# from calling validator.swagger.io.
SWAGGER_UI_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Sage Brain API</title>
  <link rel="stylesheet" href="{STATIC_PREFIX}/swagger-ui.css">
  <link rel="icon" type="image/png" href="{STATIC_PREFIX}/favicon-32x32.png">
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="{STATIC_PREFIX}/swagger-ui-bundle.js"></script>
  <script>
    window.ui = SwaggerUIBundle({{
      url: "/api/openapi.json",
      dom_id: "#swagger-ui",
      deepLinking: true,
      persistAuthorization: true,
      validatorUrl: null
    }});
  </script>
</body>
</html>
"""


class PublishedSpec:
    """api/openapi.yaml verbatim, except `servers` points at this deployment."""

    def __init__(self, spec_path: Path, public_base_url: str):
        document = yaml.safe_load(spec_path.read_text())
        document["servers"] = [{"url": public_base_url}]
        self.document = document
        self.yaml = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


@router.get("/api", response_class=HTMLResponse)
async def api_docs():
    return HTMLResponse(SWAGGER_UI_HTML)


@router.get("/api/openapi.json")
async def openapi_json(request: Request):
    return JSONResponse(request.app.state.published_spec.document)


@router.get("/api/openapi.yaml")
async def openapi_yaml(request: Request):
    return Response(
        request.app.state.published_spec.yaml, media_type="application/yaml"
    )
