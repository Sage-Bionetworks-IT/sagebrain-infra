"""T113 — the app's routes are exactly the spec's operations (constitution, article I)."""

from pathlib import Path

import yaml
from fastapi.routing import APIRoute

from sagebrain_api.guard import ApiGuard

SPEC = yaml.safe_load((Path(__file__).parents[3] / "api" / "openapi.yaml").read_text())


def _spec_operations():
    return {
        (path, method.upper())
        for path, ops in SPEC["paths"].items()
        for method in ops
        if method != "parameters"
    }


def _flatten(routes):
    # FastAPI keeps included routers as wrappers in app.routes; routers here have no prefix.
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            yield from _flatten(included.routes)
        else:
            yield route


def _routes(app):
    return [r for r in _flatten(app.routes) if isinstance(r, APIRoute)]


def _guards(route):
    return [
        d.call for d in route.dependant.dependencies if isinstance(d.call, ApiGuard)
    ]


def test_app_operations_equal_spec_operations(app):
    served = {(r.path, m) for r in _routes(app) for m in r.methods - {"HEAD"}}
    assert served == _spec_operations()


def test_public_routes_equal_spec_public_operations(app):
    public_in_spec = {
        (path, method.upper())
        for path, ops in SPEC["paths"].items()
        for method, op in ops.items()
        if op.get("security") == []
    }
    public_in_app = {
        (r.path, m) for r in _routes(app) if not _guards(r) for m in r.methods
    }
    assert public_in_app == public_in_spec


def test_guarded_routes_charge_their_own_api_bucket(app):
    for route in _routes(app):
        for guard in _guards(route):
            assert route.path.split("/")[1] == guard.api, route.path


def test_only_non_operation_mount_is_swagger_assets(app):
    others = [r.path for r in _flatten(app.routes) if not isinstance(r, APIRoute)]
    assert others == ["/api/static"]


def test_job_id_bound_matches_spec():
    from sagebrain_api.routers._common import MAX_JOB_ID_CHARS

    schema = SPEC["components"]["parameters"]["JobId"]["schema"]
    assert MAX_JOB_ID_CHARS == schema["maxLength"]
