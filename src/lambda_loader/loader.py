"""
Neptune bulk-load worker for the append-only ingestion pipeline.

Invoked as distinct Step Functions tasks via the ``action`` field:

    prepare →  dedupe the S3 event, classify the tag, claim its tracking row
    drop    →  delete one batch of a failed tag's graph before it is reloaded
    start   →  submit a Neptune bulk-load job for one snapshot folder
    check   →  poll the load job status (called in the state machine's Wait loop)
    record  →  write the terminal result to the tracking table + emit an audit log
    publish →  make the snapshot its portal's live graph in the registry
    abort   →  mark a claimed row ``error`` when a later step fails, so it can retry

The live registry (spec 004) lives in the tracking table under partition ``_live``:
one item per portal, ``{portal: "_live", snapshot: <portal>, named_graph, released_at}``.
The query service reads it and sends only those graphs to Neptune, so failed,
in-progress and superseded snapshots are invisible to readers.

Neptune requests are SigV4-signed for the ``neptune-db`` service (IAM auth is on),
mirroring the query worker in src/lambda/query.py. The Lambda itself does not wait
for the load — the state machine owns the Wait/Choice polling loop, so each
invocation is a single fast HTTP call.

The snapshot folder key drives everything. The tag is a date or a ``v…`` release:

    nf/2026-04-25/manifest.ttl
      → portal      = "nf"
      → tag         = "2026-04-25"
      → prefix      = "s3://<bucket>/nf/2026-04-25/data/"
      → named_graph = "urn:sagebrain:nf:2026-04-25"   (from GRAPH_URI_TEMPLATE)

    reactome/v97/manifest.ttl → tag "v97", graph "urn:sagebrain:reactome:v97"

The whole ``data/`` subprefix is loaded into a per-snapshot named graph, so every
historical version stays isolated (append-only).

The manifest is the trigger only and deliberately sits *outside* the load path.
Neptune's bulk loader takes a literal S3 prefix with no include/exclude or glob
support — it parses every object under ``source`` as Turtle — and ``failOnError``
is TRUE, so one non-RDF object would fail the whole snapshot. Loading from
``{portal}/{tag}/data/`` gives depositors a place to put anything non-Turtle
(``{portal}/{tag}/other/``) without breaking the load.
"""

import json
import os
import re
import time
from decimal import Decimal
from urllib.parse import urlencode

import boto3
import botocore.session
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

NEPTUNE_ENDPOINT = os.environ["NEPTUNE_ENDPOINT"]
REGION = os.environ["AWS_REGION"]
# Name is defined upstream in the neptune pipeline stack
LOAD_TABLE_NAME = os.environ["LOAD_TABLE_NAME"]
NEPTUNE_LOAD_ROLE_ARN = os.environ["NEPTUNE_LOAD_ROLE_ARN"]
GRAPH_URI_TEMPLATE = os.environ.get(
    "GRAPH_URI_TEMPLATE", "urn:sagebrain:{portal}:{tag}"
)
PARALLELISM = os.environ.get("LOAD_PARALLELISM", "HIGH")
# Triples per DELETE when clearing a failed tag's graph. Each batch must finish inside
# Neptune's 120 s query timeout, or it rolls back (spec 004 thresholds).
DROP_BATCH_SIZE = int(os.environ.get("DROP_BATCH_SIZE", "100000"))

NEPTUNE_PORT = 8182
# Partition of the live registry in the tracking table; '_' portals are rejected below.
LIVE_PARTITION = "_live"
# A dated snapshot or a release tag (spec 004 FR-1). Tags are immutable once published (FR-3).
_TAG_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|v[0-9][0-9A-Za-z.\-]*)$")

_dynamodb = boto3.resource("dynamodb")


# ---------------------------------------------------------------------------
# Key parsing
# ---------------------------------------------------------------------------


def parse_snapshot(bucket: str, key: str) -> dict:
    """
    Derive the load target from an S3 manifest object key.

    Expects ``{portal}/{tag}/manifest.ttl``, where tag is ``YYYY-MM-DD`` or ``v…``.
    Raises ValueError on any other shape so malformed keys fail loudly instead of
    loading the wrong data. Portals starting with ``_`` are reserved (the live
    registry's partition is ``_live``).

    The load target is the sibling ``data/`` subprefix — the manifest itself is
    not loaded (see the module docstring).
    """
    parts = key.split("/")
    if len(parts) != 3 or parts[2] != "manifest.ttl":
        raise ValueError(
            f"Unexpected manifest key '{key}'; expected '{{portal}}/{{tag}}/manifest.ttl'"
        )
    portal, tag, _ = parts
    if not portal:
        raise ValueError(f"Empty portal in key '{key}'")
    if portal.startswith("_"):
        raise ValueError(f"Portal '{portal}' is reserved: '_' prefix (key '{key}')")
    if not _TAG_RE.match(tag):
        raise ValueError(
            f"Snapshot tag '{tag}' is not YYYY-MM-DD or v<release> (key '{key}')"
        )

    return {
        "bucket": bucket,
        "portal": portal,
        "tag": tag,
        # SK on the tracking table — the per-portal history key.
        "snapshot": tag,
        # Load only the data/ subprefix: Neptune parses every object under the
        # prefix as Turtle, so non-RDF siblings must stay out of the load path.
        "prefix": f"s3://{bucket}/{portal}/{tag}/data/",
        "named_graph": GRAPH_URI_TEMPLATE.format(portal=portal, tag=tag),
    }


# ---------------------------------------------------------------------------
# Auth / Neptune HTTP
# ---------------------------------------------------------------------------


# TODO use boto client: https://docs.aws.amazon.com/boto3/latest/reference/services/neptunedata.html
def _signed_headers(method: str, url: str, body: str, headers: dict) -> dict:
    session = botocore.session.Session()
    credentials = session.get_credentials()
    aws_request = AWSRequest(method=method, url=url, data=body, headers=headers)
    SigV4Auth(credentials, "neptune-db", REGION).add_auth(aws_request)
    return dict(aws_request.headers)


# TODO use boto client: https://docs.aws.amazon.com/boto3/latest/reference/services/neptunedata.html
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
        # tag. RESUME semantics would then reload nothing and return success,
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
        url, data=body, headers=_signed_headers("POST", url, body, headers), timeout=25
    )
    resp.raise_for_status()
    return resp.json()["payload"]["loadId"]


# TODO Same here with using boto:
# https://docs.aws.amazon.com/boto3/latest/reference/services/neptunedata/client/get_loader_job_status.html
def _load_status(load_id: str) -> dict:
    """Return the ``overallStatus`` block for a load job."""
    url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/loader/{load_id}"
    resp = requests.get(url, headers=_signed_headers("GET", url, "", {}), timeout=25)
    resp.raise_for_status()
    return resp.json()["payload"]["overallStatus"]


def _load_errors(load_id: str, limit: int = 5) -> list:
    """Per-file parse/insert errors for a failed load (absent from overallStatus).

    Calls ``GET /loader/{loadId}?errors=TRUE&details=TRUE`` which returns
    ``errorLogs`` entries with ``errorCode``, ``errorMessage``, ``fileName``,
    and ``recordNum`` — enough to identify the bad file and line without
    manually querying Neptune from SageMaker Studio.
    Returns an empty list if the request fails so a transient error here
    never masks the original load failure.
    """
    url = (
        f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/loader/{load_id}"
        f"?errors=TRUE&details=TRUE&errorsPerPage={limit}"
    )
    try:
        resp = requests.get(
            url, headers=_signed_headers("GET", url, "", {}), timeout=10
        )
        resp.raise_for_status()
        payload = resp.json().get("payload", {})
        return payload.get("errors", {}).get("errorLogs", [])
    except Exception as exc:  # noqa: BLE001
        _log(action="load_errors_fetch_failed", load_id=load_id, error=str(exc))
        return []


def _sparql(field: str, statement: str, timeout: int) -> dict:
    """POST one SPARQL query or update (``field`` = query | update) to the writer."""
    url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/sparql"
    body = urlencode({field: statement})
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/sparql-results+json",
    }
    resp = requests.post(
        url,
        data=body,
        headers=_signed_headers("POST", url, body, headers),
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json() if field == "query" else {}


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


def _is_conditional_failure(exc: ClientError) -> bool:
    return (
        exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    )


def _claim(snap: dict, existing: dict | None) -> bool:
    """Mark the tag's row ``in_progress`` for this execution. False if another got there first.

    A new tag needs the row to be absent; a failed tag needs it to still be ``error``. Two
    executions racing for one tag can't both claim it.
    """
    item = {
        "portal": snap["portal"],
        "snapshot": snap["snapshot"],
        "status": "in_progress",
        "named_graph": snap["named_graph"],
        "prefix": snap["prefix"],
        "etag": snap["etag"],
        "sequencer": snap["sequencer"],
        "released_at": snap["released_at"],
        "started_at": int(time.time()),
    }
    if existing is None:
        condition = {"ConditionExpression": "attribute_not_exists(portal)"}
    else:
        condition = {
            "ConditionExpression": "#status = :error",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {":error": "error"},
        }
    try:
        _table().put_item(Item=item, **condition)
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        raise
    return True


def _log(**fields):
    # TODO: Use logger?
    print(json.dumps({"event": "kg_load", **fields}))


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _prepare(event: dict) -> dict:
    """
    Classify one manifest event and claim the tag's tracking row (spec 004).

    Returns the snapshot plus ``route``, which the state machine branches on:

        skip    — a duplicate delivery (same S3 sequencer, FR-5) or a load already in
                  progress for this tag (FR-9)
        publish — the tag is already loaded: re-point the registry, no reload (FR-7)
        drop    — the tag's last load failed: clear its never-published graph, then load (FR-8)
        load    — a new tag
    """
    bucket = event["bucket"]["name"]
    key = event["object"]["key"]
    snap = parse_snapshot(bucket, key)
    snap["etag"] = event["object"].get("etag", "")
    # One PUT's identity: a re-upload of identical bytes has the same etag but a new
    # sequencer, so the etag can't tell a duplicate delivery from a re-upload (D-5).
    snap["sequencer"] = event["object"].get("sequencer", "")
    # The Step Functions execution start time (ISO-8601): the arrival order (FR-4).
    snap["released_at"] = event["released_at"]

    existing = _get_load(snap["portal"], snap["snapshot"])
    status = existing.get("status") if existing else None
    if (
        existing
        and snap["sequencer"]
        and existing.get("sequencer") == snap["sequencer"]
    ):
        route, reason = "skip", "duplicate_event"
    elif status == "in_progress":
        route, reason = "skip", "load_in_progress"
    elif status == "complete":
        # Record this arrival on the row; the registry write happens in publish.
        _table().update_item(
            Key={"portal": snap["portal"], "snapshot": snap["snapshot"]},
            UpdateExpression="SET sequencer = :s, released_at = :r",
            ExpressionAttributeValues={
                ":s": snap["sequencer"],
                ":r": snap["released_at"],
            },
        )
        route, reason = "publish", "already_loaded"
    elif _claim(snap, existing):
        route, reason = ("drop", "retry_failed") if existing else ("load", "new_tag")
    else:
        route, reason = "skip", "claimed_concurrently"

    _log(
        action="prepare",
        portal=snap["portal"],
        snapshot=snap["snapshot"],
        named_graph=snap["named_graph"],
        released_at=snap["released_at"],
        route=route,
        reason=reason,
    )
    return {**snap, "route": route, "reason": reason}


def _drop(event: dict) -> dict:
    """
    Delete one batch of a failed tag's graph; report whether any triples remain.

    One ``DROP GRAPH`` over millions of triples exceeds Neptune's 120 s query timeout
    and rolls back, so the state machine loops over LIMIT batches (the bastion's form).
    """
    portal = event["portal"]
    graph = event["named_graph"]
    # Only never-published graphs are dropped. Refuse the portal's live graph outright.
    live = _get_load(LIVE_PARTITION, portal)
    if live and live.get("named_graph") == graph:
        raise RuntimeError(
            f"Refusing to drop {graph}: it is the live graph of {portal}"
        )

    _sparql(
        "update",
        f"DELETE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }} WHERE {{ {{ SELECT ?s ?p ?o "
        f"WHERE {{ GRAPH <{graph}> {{ ?s ?p ?o }} }} LIMIT {DROP_BATCH_SIZE} }} }}",
        timeout=130,
    )
    remaining = _sparql(
        "query", f"ASK {{ GRAPH <{graph}> {{ ?s ?p ?o }} }}", timeout=15
    )
    remaining = bool(remaining.get("boolean"))
    _log(
        action="drop",
        portal=portal,
        snapshot=event.get("snapshot"),
        named_graph=graph,
        batch_size=DROP_BATCH_SIZE,
        remaining=remaining,
    )
    return {"remaining": remaining}


def _start(event: dict) -> dict:
    """Submit a bulk load for a snapshot that ``prepare`` claimed."""
    snap = event["snap"]
    load_id = _start_bulk_load(snap["prefix"], snap["named_graph"])
    _table().update_item(
        Key={"portal": snap["portal"], "snapshot": snap["snapshot"]},
        UpdateExpression="SET load_id = :l",
        ExpressionAttributeValues={":l": load_id},
    )
    _log(
        action="start",
        portal=snap["portal"],
        snapshot=snap["snapshot"],
        named_graph=snap["named_graph"],
        prefix=snap["prefix"],
        load_id=load_id,
        released_at=snap["released_at"],
    )
    return {**snap, "load_id": load_id}


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

    load_id = event.get("load_id")
    errors = [] if succeeded else _load_errors(load_id)

    _put_load(
        portal,
        snapshot,
        status="complete" if succeeded else "error",
        load_id=load_id,
        named_graph=event.get("named_graph"),
        prefix=event.get("prefix"),
        etag=event.get("etag", ""),
        sequencer=event.get("sequencer", ""),
        released_at=event.get("released_at", ""),
        total_records=int(overall.get("totalRecords", 0)),
        parsing_errors=int(overall.get("parsingErrors", 0)),
        load_status=load_status,
        finished_at=int(time.time()),
        error="" if succeeded else json.dumps(overall)[:1000],
        load_errors=json.dumps(errors) if errors else "",
    )
    _log(
        action="record",
        portal=portal,
        snapshot=snapshot,
        load_id=load_id,
        named_graph=event.get("named_graph"),
        status="complete" if succeeded else "error",
        load_status=load_status,
        released_at=event.get("released_at", ""),
        total_records=int(overall.get("totalRecords", 0)),
        parsing_errors=int(overall.get("parsingErrors", 0)),
        load_errors=errors,
    )
    return {"status": "complete" if succeeded else "error", "load_status": load_status}


def _publish(event: dict) -> dict:
    """
    Make this snapshot its portal's live graph, unless a later arrival already did (FR-6).

    The registry item is written only if absent or older than this ``released_at``, so
    executions that finish out of order can't roll a portal back.
    """
    portal = event["portal"]
    released_at = event["released_at"]
    try:
        _table().put_item(
            Item={
                "portal": LIVE_PARTITION,
                "snapshot": portal,
                "tag": event["snapshot"],
                "named_graph": event["named_graph"],
                "released_at": released_at,
                "published_at": int(time.time()),
            },
            ConditionExpression="attribute_not_exists(portal) OR released_at < :r",
            ExpressionAttributeValues={":r": released_at},
        )
        published = True
    except ClientError as exc:
        if not _is_conditional_failure(exc):
            raise
        published = False
    _log(
        action="publish",
        portal=portal,
        snapshot=event["snapshot"],
        named_graph=event["named_graph"],
        released_at=released_at,
        published=published,
        reason="live" if published else "superseded_by_later_arrival",
    )
    return {"published": published}


def _abort(event: dict) -> dict:
    """Mark this execution's claimed row ``error`` after a failure, so the tag can retry.

    Guarded on the sequencer: a row another execution has since claimed is left alone.
    """
    portal = event["portal"]
    snapshot = event["snapshot"]
    cause = json.dumps(event.get("error") or {})[:1000]
    try:
        _table().update_item(
            Key={"portal": portal, "snapshot": snapshot},
            UpdateExpression="SET #status = :error, #error = :cause, finished_at = :t",
            ConditionExpression="sequencer = :s",
            ExpressionAttributeNames={"#status": "status", "#error": "error"},
            ExpressionAttributeValues={
                ":error": "error",
                ":cause": cause,
                ":t": int(time.time()),
                ":s": event.get("sequencer", ""),
            },
        )
        aborted = True
    except ClientError as exc:
        if not _is_conditional_failure(exc):
            raise
        aborted = False
    _log(action="abort", portal=portal, snapshot=snapshot, aborted=aborted, error=cause)
    return {"aborted": aborted}


_ACTIONS = {
    "prepare": _prepare,
    "drop": _drop,
    "start": _start,
    "check": _check,
    "record": _record,
    "publish": _publish,
    "abort": _abort,
}


def handler(event, context):
    """Dispatch on ``event['action']`` (see the module docstring)."""
    action = event.get("action")
    fn = _ACTIONS.get(action)
    if fn is None:
        raise ValueError(f"Unknown action '{action}'; expected one of {list(_ACTIONS)}")
    return fn(event)
