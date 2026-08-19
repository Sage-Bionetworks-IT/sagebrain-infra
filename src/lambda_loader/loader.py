"""
Neptune bulk-load worker for the append-only ingestion pipeline.

Invoked as three distinct Step Functions tasks via the ``action`` field:

    start   →  submit a Neptune bulk-load job for one dated snapshot folder
    check   →  poll the load job status (called in the state machine's Wait loop)
    record  →  write the terminal result to the tracking table + emit an audit log

Neptune requests are SigV4-signed for the ``neptune-db`` service (IAM auth is on),
mirroring the query worker in src/lambda/query.py. The Lambda itself does not wait
for the load — the state machine owns the Wait/Choice polling loop, so each
invocation is a single fast HTTP call.

The snapshot folder key drives everything:

    nf/2026-04-25/manifest.ttl
      → portal      = "nf"
      → date        = "2026-04-25"
      → prefix      = "s3://<bucket>/nf/2026-04-25/data/"
      → named_graph = "urn:sagebrain:nf:2026-04-25"   (from GRAPH_URI_TEMPLATE)

The whole ``data/`` subprefix is loaded into a per-snapshot named graph, so every
historical version stays isolated (append-only).

The manifest is the trigger only and deliberately sits *outside* the load path.
Neptune's bulk loader takes a literal S3 prefix with no include/exclude or glob
support — it parses every object under ``source`` as Turtle — and ``failOnError``
is TRUE, so one non-RDF object would fail the whole snapshot. Loading from
``{portal}/{date}/data/`` gives depositors a place to put anything non-Turtle
(``{portal}/{date}/other/``) without breaking the load.
"""

import json
import os
import re
import time
from decimal import Decimal

import boto3
import botocore.session
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

NEPTUNE_ENDPOINT = os.environ["NEPTUNE_ENDPOINT"]
REGION = os.environ["AWS_REGION"]
LOAD_TABLE_NAME = os.environ["LOAD_TABLE_NAME"]
NEPTUNE_LOAD_ROLE_ARN = os.environ["NEPTUNE_LOAD_ROLE_ARN"]
GRAPH_URI_TEMPLATE = os.environ.get(
    "GRAPH_URI_TEMPLATE", "urn:sagebrain:{portal}:{date}"
)
PARALLELISM = os.environ.get("LOAD_PARALLELISM", "HIGH")

NEPTUNE_PORT = 8182
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_dynamodb = boto3.resource("dynamodb")


# ---------------------------------------------------------------------------
# Key parsing
# ---------------------------------------------------------------------------


def parse_snapshot(bucket: str, key: str) -> dict:
    """
    Derive the load target from an S3 manifest object key.

    Expects ``{portal}/{YYYY-MM-DD}/manifest.ttl``. Raises ValueError on any
    other shape so malformed keys fail loudly instead of loading the wrong data.

    The load target is the sibling ``data/`` subprefix — the manifest itself is
    not loaded (see the module docstring).
    """
    parts = key.split("/")
    if len(parts) != 3 or parts[2] != "manifest.ttl":
        raise ValueError(
            f"Unexpected manifest key '{key}'; expected '{{portal}}/YYYY-MM-DD/manifest.ttl'"
        )
    portal, date, _ = parts
    if not portal:
        raise ValueError(f"Empty portal in key '{key}'")
    if not _DATE_RE.match(date):
        raise ValueError(f"Snapshot date '{date}' is not YYYY-MM-DD (key '{key}')")

    return {
        "bucket": bucket,
        "portal": portal,
        "date": date,
        # SK on the tracking table — an ordering-friendly per-portal history key.
        "snapshot": date,
        # Load only the data/ subprefix: Neptune parses every object under the
        # prefix as Turtle, so non-RDF siblings must stay out of the load path.
        "prefix": f"s3://{bucket}/{portal}/{date}/data/",
        "named_graph": GRAPH_URI_TEMPLATE.format(portal=portal, date=date),
    }


# ---------------------------------------------------------------------------
# Auth / Neptune HTTP
# ---------------------------------------------------------------------------


def _signed_headers(method: str, url: str, body: str, headers: dict) -> dict:
    session = botocore.session.Session()
    credentials = session.get_credentials()
    aws_request = AWSRequest(method=method, url=url, data=body, headers=headers)
    SigV4Auth(credentials, "neptune-db", REGION).add_auth(aws_request)
    return dict(aws_request.headers)


def _start_bulk_load(prefix: str, named_graph: str) -> str:
    """Submit an S3 bulk-load job into a named graph. Returns the loadId."""
    url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/loader"
    payload = {
        "source": prefix,
        "format": "turtle",
        "iamRoleArn": NEPTUNE_LOAD_ROLE_ARN,
        "region": REGION,
        # NEW, never the AUTO default: AUTO resumes a prior job from the same
        # source, and our source is byte-identical on a re-publish of the same
        # date. RESUME semantics would then reload nothing and return success,
        # so corrected data would never land while the tracking table recorded
        # a completed load. NEW also means dropped graph data does get reloaded.
        "mode": "NEW",
        # Append-only: surface bad data instead of silently skipping it.
        # Caveat: on error the loader stops but data loaded up to that point
        # persists, so a failed load leaves a partial named graph behind.
        "failOnError": "TRUE",
        "parallelism": PARALLELISM,
        # Queue behind any in-flight load rather than erroring on a busy cluster.
        "queueRequest": "TRUE",
        # Route every triple in this snapshot into its own named graph.
        "parserConfiguration": {"namedGraphUri": named_graph},
    }
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json"}
    resp = requests.post(
        url, data=body, headers=_signed_headers("POST", url, body, headers), timeout=30
    )
    resp.raise_for_status()
    return resp.json()["payload"]["loadId"]


def _load_status(load_id: str) -> dict:
    """Return the ``overallStatus`` block for a load job."""
    url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/loader/{load_id}"
    resp = requests.get(url, headers=_signed_headers("GET", url, "", {}), timeout=30)
    resp.raise_for_status()
    return resp.json()["payload"]["overallStatus"]


# ---------------------------------------------------------------------------
# Tracking table
# ---------------------------------------------------------------------------


def _table():
    return _dynamodb.Table(LOAD_TABLE_NAME)


def _get_load(portal: str, snapshot: str) -> dict | None:
    resp = _table().get_item(Key={"portal": portal, "snapshot": snapshot})
    return resp.get("Item")


def _put_load(portal: str, snapshot: str, **fields):
    item = {"portal": portal, "snapshot": snapshot}
    for k, v in fields.items():
        item[k] = Decimal(str(v)) if isinstance(v, float) else v
    _table().put_item(Item=item)


def _log(**fields):
    print(json.dumps({"event": "kg_load", **fields}))


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _start(event: dict) -> dict:
    """
    Submit a bulk load for the snapshot described by the S3 event detail.

    Idempotent: skip if this exact snapshot content already loaded successfully.
    The manifest's etag distinguishes a duplicate S3 delivery (same etag → skip)
    from a genuine re-publish of the same date (new etag → reload).
    """
    bucket = event["bucket"]["name"]
    key = event["object"]["key"]
    etag = event["object"].get("etag", "")
    snap = parse_snapshot(bucket, key)
    snap["etag"] = etag

    existing = _get_load(snap["portal"], snap["snapshot"])
    if (
        existing
        and existing.get("status") == "complete"
        and existing.get("etag") == etag
    ):
        _log(
            action="start",
            skip=True,
            portal=snap["portal"],
            snapshot=snap["snapshot"],
            reason="already_complete",
        )
        return {**snap, "skip": True, "load_id": existing.get("load_id")}

    load_id = _start_bulk_load(snap["prefix"], snap["named_graph"])
    _put_load(
        snap["portal"],
        snap["snapshot"],
        status="in_progress",
        load_id=load_id,
        named_graph=snap["named_graph"],
        prefix=snap["prefix"],
        etag=etag,
        started_at=int(time.time()),
    )
    _log(
        action="start",
        skip=False,
        portal=snap["portal"],
        snapshot=snap["snapshot"],
        named_graph=snap["named_graph"],
        prefix=snap["prefix"],
        load_id=load_id,
    )
    return {**snap, "skip": False, "load_id": load_id}


def _check(event: dict) -> dict:
    """Poll the load job. Returns the state machine's routing value."""
    load_id = event["load_id"]
    status = _load_status(load_id)
    return {"load_status": status["status"], "overall_status": status}


def _record(event: dict) -> dict:
    """Persist the terminal result and emit an audit log line."""
    portal = event["portal"]
    snapshot = event["snapshot"]
    overall = event.get("overall_status") or {}
    load_status = event.get("load_status", "UNKNOWN")
    succeeded = load_status == "LOAD_COMPLETED"

    _put_load(
        portal,
        snapshot,
        status="complete" if succeeded else "error",
        load_id=event.get("load_id"),
        named_graph=event.get("named_graph"),
        prefix=event.get("prefix"),
        etag=event.get("etag", ""),
        total_records=int(overall.get("totalRecords", 0)),
        parsing_errors=int(overall.get("parsingErrors", 0)),
        load_status=load_status,
        finished_at=int(time.time()),
        error="" if succeeded else json.dumps(overall)[:1000],
    )
    _log(
        action="record",
        portal=portal,
        snapshot=snapshot,
        load_id=event.get("load_id"),
        named_graph=event.get("named_graph"),
        status="complete" if succeeded else "error",
        load_status=load_status,
        total_records=int(overall.get("totalRecords", 0)),
        parsing_errors=int(overall.get("parsingErrors", 0)),
    )
    return {"status": "complete" if succeeded else "error", "load_status": load_status}


_ACTIONS = {"start": _start, "check": _check, "record": _record}


def handler(event, context):
    """Dispatch on ``event['action']`` (start | check | record)."""
    action = event.get("action")
    fn = _ACTIONS.get(action)
    if fn is None:
        raise ValueError(f"Unknown action '{action}'; expected one of {list(_ACTIONS)}")
    return fn(event)
