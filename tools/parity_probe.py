"""
Replay tests/contract/cases.yaml against one or more live deployments and diff them.

Used to (1) pin down exactly what API Gateway returns today for edge cases that unit tests
can't see (auth failures, unknown routes, preflight), and (2) prove the FastAPI service
behaves identically before cutover.

Usage:
    export SYNAPSE_AUTH_TOKEN=<synapse-pat>          # team member token for "valid" auth cases
    # API Gateway: the two APIs have different hosts, so pass both bases (stage included)
    python tools/parity_probe.py \\
        --target apigw https://<query-api>/prod https://<agent-api>/prod
    # FastAPI: one host serves both; pass the same base twice
    python tools/parity_probe.py \\
        --target apigw https://<query-api>/prod https://<agent-api>/prod \\
        --target app   https://api.dev.example.org https://api.dev.example.org \\
        --legacy apigw

--legacy LABEL checks that target against a case's `legacy` expectation (the recorded
current behaviour) where one exists; a Lambda that `raises` surfaces as API Gateway 502.
Expected legacy-vs-new differences are then not reported as DIFFs.

Safety: cases that would create jobs are skipped except `query_submit_ok_live`
(one cheap LIMIT 1 query). Cases with `given` (seeded DynamoDB items) can't be replayed
live and are always skipped. /ask jobs are never submitted (they cost Bedrock calls).

Exit code is non-zero if any case fails its expectation on any target, or if targets
disagree with each other.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from tests.contract.cases import (  # noqa: E402
    check_response,
    job_id_for,
    load_cases,
    request_body,
    request_path,
)

TIMEOUT = 15
BAD_TOKEN = "parity-probe-not-a-real-token"

# Ad-hoc requests whose responses we only *record* (no expectation yet) — the answers go
# into specs/001-api-gateway-to-fastapi/research.md and then become cases.
OBSERVATIONS = [
    ("query", "GET", "/query/", None, {}),
    ("query", "POST", "/query", "x" * (11 * 1024 * 1024), {}),
]


def _send(base, method, path, body, headers):
    data = body.encode() if isinstance(body, str) and body else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return None, {}, f"<transport error: {e}>"


def _decode(text):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _auth_headers(mode, token):
    if mode == "none":
        return {}
    if mode == "bad_bearer":
        return {"Authorization": f"Bearer {BAD_TOKEN}"}
    return {"Authorization": f"Bearer {token}"}


def _runnable(case):
    if case.get("given"):
        return False, "needs seeded item"
    status = case["expect"]["status"]
    if status == 202 and case["id"] != "query_submit_ok_live":
        return False, "would create a job"
    return True, ""


def _expectation(case, legacy):
    if legacy and case.get("legacy"):
        expect = dict(case["legacy"])
        if "raises" in expect:
            return {"status": 502}
        return expect
    return case["expect"]


def run_case(case, target, token, legacy=False):
    label, query_base, ask_base = target
    base = query_base if case["api"] == "query" else ask_base
    headers = {**(case["request"].get("headers") or {})}
    headers.update(_auth_headers(case.get("auth", "valid"), token))
    path = request_path(case, job_id_for(case))
    body = request_body(case) if case["request"]["method"] == "POST" else None
    status, resp_headers, text = _send(
        base, case["request"]["method"], path, body, headers
    )
    decoded = _decode(text)
    try:
        expect = {
            k: v
            for k, v in _expectation(case, legacy).items()
            if not k.startswith(("stored", "enqueued"))
        }
        check_response(expect, status, decoded, resp_headers)
        error = None
    except AssertionError as e:
        error = str(e)
    return {"status": status, "body": decoded, "error": error}


def _comparable(result):
    body = result["body"]
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k != "job_id"}
    return result["status"], body


def _parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--target",
        nargs=3,
        action="append",
        required=True,
        metavar=("LABEL", "QUERY_BASE", "ASK_BASE"),
        help="deployment to probe; repeat to compare deployments",
    )
    parser.add_argument(
        "--legacy",
        action="append",
        default=[],
        metavar="LABEL",
        help="target label running the current (API Gateway) implementation",
    )
    parser.add_argument("--token", default=os.environ.get("SYNAPSE_AUTH_TOKEN"))
    parser.add_argument("--only", help="comma-separated case ids")
    parser.add_argument("--no-observations", action="store_true")
    args = parser.parse_args()

    if not args.token:
        parser.error(
            "set SYNAPSE_AUTH_TOKEN or pass --token (Synapse PAT of a team member)"
        )
    for label, *bases in args.target:
        if not all(b.startswith(("http://", "https://")) for b in bases):
            parser.error(
                f"target {label!r}: base URLs must start with http(s):// (got {bases})"
            )
    return args


def _probe_case(case, args) -> int:
    """Run one case on every target; print results; return the number of failures."""
    absent_on_legacy = (case.get("legacy") or {}).get("absent")
    results = {
        t[0]: run_case(case, t, args.token, t[0] in args.legacy)
        for t in args.target
        if not (absent_on_legacy and t[0] in args.legacy)
    }
    failures = 0
    for label, r in results.items():
        mark = "ok  " if r["error"] is None else "FAIL"
        print(
            f"{mark} {case['id']:<32} [{label}] {r['status']} {json.dumps(r['body'])[:120]}"
        )
        if r["error"]:
            print(f"       {r['error']}")
            failures += 1

    expected_divergence = case.get("legacy") and args.legacy
    distinct = {json.dumps(_comparable(r), sort_keys=True) for r in results.values()}
    if not expected_divergence and len(distinct) > 1:
        print(f"DIFF {case['id']}: targets disagree")
        failures += 1
    return failures


def _print_observations(args):
    print(
        "\n--- observations (record in specs/001-api-gateway-to-fastapi/research.md) ---"
    )
    for api, method, path, body, headers in OBSERVATIONS:
        for label, query_base, ask_base in args.target:
            base = query_base if api == "query" else ask_base
            hdrs = {**headers, **_auth_headers("valid", args.token)}
            status, resp_headers, text = _send(base, method, path, body, hdrs)
            cors = {
                k: v
                for k, v in resp_headers.items()
                if k.lower().startswith("access-control")
            }
            print(
                f"{method} {path[:40]:<40} [{label}] {status} {text[:200]!r} cors={cors}"
            )


def main():
    args = _parse_args()
    only = set(args.only.split(",")) if args.only else None
    failures = 0

    for case in load_cases():
        if only and case["id"] not in only:
            continue
        ok, reason = _runnable(case)
        if not ok:
            if only:
                print(f"SKIP {case['id']}: {reason}")
            continue
        failures += _probe_case(case, args)

    if not args.no_observations:
        _print_observations(args)

    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
