import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import pytest
from botocore.exceptions import ClientError

LAMBDA_DIR = str(Path(__file__).parents[2] / "src" / "lambda_loader")
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv(
        "NEPTUNE_ENDPOINT", "test-neptune.cluster.us-east-1.neptune.amazonaws.com"
    )
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("LOAD_TABLE_NAME", "test-load-table")
    monkeypatch.setenv(
        "NEPTUNE_LOAD_ROLE_ARN", "arn:aws:iam::123456789012:role/NeptuneLoadRole"
    )
    monkeypatch.setenv("GRAPH_URI_TEMPLATE", "urn:sagebrain:{portal}:{tag}")
    monkeypatch.setenv("DROP_BATCH_SIZE", "1000")


@pytest.fixture
def mock_table():
    table = MagicMock()
    table.get_item.return_value = {}  # no existing load by default
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = table
        yield table


@pytest.fixture
def loader(mock_table, monkeypatch):
    sys.modules.pop("loader", None)
    import loader as mod

    importlib.reload(mod)
    mod._dynamodb.Table.return_value = mock_table
    # Skip real SigV4 signing (no AWS creds in unit tests).
    monkeypatch.setattr(mod, "_signed_headers", lambda *a, **k: {})
    return mod


# ---------------------------------------------------------------------------
# Key parsing
# ---------------------------------------------------------------------------


def test_parse_snapshot_valid(loader):
    snap = loader.parse_snapshot("mybucket", "nf/2026-04-25/manifest.ttl")
    assert snap["portal"] == "nf"
    assert snap["tag"] == "2026-04-25"
    assert snap["snapshot"] == "2026-04-25"
    # Load path is the data/ subprefix; the manifest sits outside it.
    assert snap["prefix"] == "s3://mybucket/nf/2026-04-25/data/"
    assert snap["named_graph"] == "urn:sagebrain:nf:2026-04-25"


@pytest.mark.parametrize("tag", ["v97", "v3.1", "v1.0.0-rc1", "v24"])
def test_parse_snapshot_accepts_release_tags(loader, tag):
    # Spec 004 FR-1 / D-4 (#61): release-keyed sources. Before, only dates parsed.
    snap = loader.parse_snapshot("mybucket", f"reactome/{tag}/manifest.ttl")
    assert snap["tag"] == snap["snapshot"] == tag
    assert snap["prefix"] == f"s3://mybucket/reactome/{tag}/data/"
    assert snap["named_graph"] == f"urn:sagebrain:reactome:{tag}"


def test_parse_snapshot_rejects_wrong_shape(loader):
    with pytest.raises(ValueError):
        loader.parse_snapshot("b", "nf/2026-04-25/data/thing.ttl")
    with pytest.raises(ValueError):
        loader.parse_snapshot("b", "nf/manifest.ttl")


@pytest.mark.parametrize(
    "tag",
    ["not-a-date", "97", "V97", "v", "va1", "2026-4-25", "v97/x", "latest", "v9 7"],
)
def test_parse_snapshot_rejects_bad_tag(loader, tag):
    with pytest.raises(ValueError):
        loader.parse_snapshot("b", f"nf/{tag}/manifest.ttl")


def test_parse_snapshot_rejects_reserved_portal(loader):
    # '_' portals are reserved for the live registry (partition '_live').
    with pytest.raises(ValueError, match="reserved"):
        loader.parse_snapshot("b", "_live/2026-04-25/manifest.ttl")


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def _conditional_failure():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
        "PutItem",
    )


def _manifest_event(
    action="prepare",
    key="nf/2026-04-25/manifest.ttl",
    etag="etag-1",
    sequencer="SEQ-2",
    released_at="2026-10-15T12:00:00.000Z",
):
    return {
        "action": action,
        "bucket": {"name": "mybucket"},
        "object": {"key": key, "etag": etag, "sequencer": sequencer},
        "released_at": released_at,
    }


def _row(status, sequencer="SEQ-1", **fields):
    return {"Item": {"status": status, "sequencer": sequencer, **fields}}


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_new_tag_claims_row_and_routes_to_load(loader, mock_table):
    out = loader.handler(_manifest_event(), None)

    assert out["route"] == "load"
    assert out["named_graph"] == "urn:sagebrain:nf:2026-04-25"
    assert out["sequencer"] == "SEQ-2"
    assert out["released_at"] == "2026-10-15T12:00:00.000Z"
    kwargs = mock_table.put_item.call_args.kwargs
    assert kwargs["ConditionExpression"] == "attribute_not_exists(portal)"
    item = kwargs["Item"]
    assert (item["portal"], item["snapshot"]) == ("nf", "2026-04-25")
    assert item["status"] == "in_progress"
    assert item["sequencer"] == "SEQ-2"
    assert item["released_at"] == "2026-10-15T12:00:00.000Z"


def test_prepare_release_tag(loader, mock_table):
    out = loader.handler(_manifest_event(key="reactome/v97/manifest.ttl"), None)
    assert out["route"] == "load"
    assert out["prefix"] == "s3://mybucket/reactome/v97/data/"
    assert out["named_graph"] == "urn:sagebrain:reactome:v97"


def test_prepare_bad_key_raises(loader, mock_table):
    # The state machine's catch turns this into BadManifestKey, as before.
    with pytest.raises(ValueError):
        loader.handler(_manifest_event(key="nf/latest/manifest.ttl"), None)
    mock_table.put_item.assert_not_called()


@pytest.mark.parametrize("status", ["complete", "error", "in_progress"])
def test_prepare_skips_duplicate_delivery(loader, mock_table, status):
    # FR-5: same S3 sequencer = the same PUT delivered twice.
    mock_table.get_item.return_value = _row(status, sequencer="SEQ-2")
    out = loader.handler(_manifest_event(sequencer="SEQ-2"), None)
    assert (out["route"], out["reason"]) == ("skip", "duplicate_event")
    mock_table.put_item.assert_not_called()
    mock_table.update_item.assert_not_called()


def test_prepare_identical_reupload_is_not_a_duplicate(loader, mock_table):
    """D-5: an identical re-upload has the same etag but a new sequencer.

    Old behaviour: the etag matched, so it was skipped as a duplicate delivery.
    """
    mock_table.get_item.return_value = _row(
        "complete", sequencer="SEQ-1", etag="etag-1"
    )
    out = loader.handler(_manifest_event(etag="etag-1", sequencer="SEQ-2"), None)
    assert out["route"] == "publish"


def test_prepare_skips_while_load_in_progress(loader, mock_table):
    # FR-9
    mock_table.get_item.return_value = _row("in_progress")
    out = loader.handler(_manifest_event(), None)
    assert (out["route"], out["reason"]) == ("skip", "load_in_progress")
    mock_table.put_item.assert_not_called()


def test_prepare_published_tag_routes_to_publish_without_reload(loader, mock_table):
    """FR-7, D-6: a re-upload of a loaded tag re-points the registry (rollback).

    Old behaviour: a re-publish of the same date reloaded into the same live graph.
    """
    mock_table.get_item.return_value = _row("complete")
    out = loader.handler(_manifest_event(), None)

    assert out["route"] == "publish"
    mock_table.put_item.assert_not_called()  # no claim, no load
    values = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values == {":s": "SEQ-2", ":r": "2026-10-15T12:00:00.000Z"}


def test_prepare_failed_tag_claims_row_and_routes_to_drop(loader, mock_table):
    # FR-8: clear the never-published graph, then load.
    mock_table.get_item.return_value = _row("error")
    out = loader.handler(_manifest_event(), None)

    assert out["route"] == "drop"
    kwargs = mock_table.put_item.call_args.kwargs
    assert kwargs["ConditionExpression"] == "#status = :error"
    assert kwargs["Item"]["status"] == "in_progress"


@pytest.mark.parametrize("existing", [{}, _row("error")])
def test_prepare_skips_when_another_execution_claims_first(
    loader, mock_table, existing
):
    mock_table.get_item.return_value = existing
    mock_table.put_item.side_effect = _conditional_failure()
    out = loader.handler(_manifest_event(), None)
    assert (out["route"], out["reason"]) == ("skip", "claimed_concurrently")


# ---------------------------------------------------------------------------
# drop
# ---------------------------------------------------------------------------


def _drop_event():
    return {
        "action": "drop",
        "portal": "nf",
        "snapshot": "2026-04-25",
        "named_graph": "urn:sagebrain:nf:2026-04-25",
        "sequencer": "SEQ-2",
        "released_at": "2026-10-15T12:00:00.000Z",
    }


def _sparql_responses(ask_result):
    update = MagicMock()
    update.raise_for_status = MagicMock()
    ask = MagicMock()
    ask.raise_for_status = MagicMock()
    ask.json.return_value = {"head": {}, "boolean": ask_result}
    return [update, ask]


@pytest.mark.parametrize("remaining", [True, False])
def test_drop_deletes_one_batch_of_its_own_graph(loader, mock_table, remaining):
    with patch.object(
        loader.requests, "post", side_effect=_sparql_responses(remaining)
    ) as post:
        out = loader.handler(_drop_event(), None)

    assert out == {"remaining": remaining}
    update_call, ask_call = post.call_args_list
    assert update_call.args[0].endswith(":8182/sparql")
    update = parse_qs(update_call.kwargs["data"])["update"][0]
    assert update == (
        "DELETE { GRAPH <urn:sagebrain:nf:2026-04-25> { ?s ?p ?o } } WHERE { { SELECT ?s ?p ?o "
        "WHERE { GRAPH <urn:sagebrain:nf:2026-04-25> { ?s ?p ?o } } LIMIT 1000 } }"
    )
    # Inside the 150 s Lambda timeout, beyond Neptune's 120 s query timeout.
    assert update_call.kwargs["timeout"] == 130
    ask = parse_qs(ask_call.kwargs["data"])["query"][0]
    assert ask == "ASK { GRAPH <urn:sagebrain:nf:2026-04-25> { ?s ?p ?o } }"


def test_drop_refuses_the_live_graph(loader, mock_table):
    mock_table.get_item.return_value = {
        "Item": {"named_graph": "urn:sagebrain:nf:2026-04-25"}
    }
    with patch.object(loader.requests, "post") as post:
        with pytest.raises(RuntimeError, match="live graph"):
            loader.handler(_drop_event(), None)
    post.assert_not_called()
    assert mock_table.get_item.call_args.kwargs["Key"] == {
        "portal": "_live",
        "snapshot": "nf",
    }


def test_drop_propagates_neptune_errors(loader, mock_table):
    resp = MagicMock()
    resp.raise_for_status.side_effect = loader.requests.exceptions.HTTPError("500")
    with patch.object(loader.requests, "post", return_value=resp):
        with pytest.raises(loader.requests.exceptions.HTTPError):
            loader.handler(_drop_event(), None)


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def _load_response(load_id):
    resp = MagicMock()
    resp.json.return_value = {"payload": {"loadId": load_id}}
    resp.raise_for_status = MagicMock()
    return resp


def _prepared(key="nf/2026-04-25/manifest.ttl"):
    portal, tag, _ = key.split("/")
    snap = {
        "bucket": "mybucket",
        "portal": portal,
        "tag": tag,
        "snapshot": tag,
        "prefix": f"s3://mybucket/{portal}/{tag}/data/",
        "named_graph": f"urn:sagebrain:{portal}:{tag}",
        "etag": "etag-1",
        "sequencer": "SEQ-2",
        "released_at": "2026-10-15T12:00:00.000Z",
        "route": "load",
        "reason": "new_tag",
    }
    return {"action": "start", "snap": snap}


def test_start_submits_load_into_named_graph(loader, mock_table):
    with patch.object(
        loader.requests, "post", return_value=_load_response("load-42")
    ) as mock_post:
        out = loader.handler(_prepared(), None)

    assert out["load_id"] == "load-42"
    assert out["named_graph"] == "urn:sagebrain:nf:2026-04-25"
    assert out["sequencer"] == "SEQ-2"

    body = json.loads(mock_post.call_args.kwargs["data"])
    assert body["source"] == "s3://mybucket/nf/2026-04-25/data/"
    assert body["format"] == "turtle"
    assert body["failOnError"] == "TRUE"
    # Never AUTO: AUTO would resume the prior job for this source and no-op a
    # retry of the same tag, reporting success without loading anything.
    assert body["mode"] == "NEW"
    assert body["parserConfiguration"]["namedGraphUri"] == "urn:sagebrain:nf:2026-04-25"

    # load_id added to the row prepare claimed
    kwargs = mock_table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"portal": "nf", "snapshot": "2026-04-25"}
    assert kwargs["ExpressionAttributeValues"] == {":l": "load-42"}


def test_start_loads_release_tag(loader, mock_table):
    with patch.object(
        loader.requests, "post", return_value=_load_response("l")
    ) as mock_post:
        out = loader.handler(_prepared("reactome/v97/manifest.ttl"), None)

    assert out["named_graph"] == "urn:sagebrain:reactome:v97"
    body = json.loads(mock_post.call_args.kwargs["data"])
    assert body["source"] == "s3://mybucket/reactome/v97/data/"


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def _publish_event(released_at="2026-10-15T12:00:00.000Z"):
    return {**_drop_event(), "action": "publish", "released_at": released_at}


def test_publish_writes_registry_entry_conditionally(loader, mock_table):
    out = loader.handler(_publish_event(), None)

    assert out == {"published": True}
    kwargs = mock_table.put_item.call_args.kwargs
    item = kwargs["Item"]
    assert (item["portal"], item["snapshot"]) == ("_live", "nf")
    assert item["named_graph"] == "urn:sagebrain:nf:2026-04-25"
    assert item["tag"] == "2026-04-25"
    assert item["released_at"] == "2026-10-15T12:00:00.000Z"
    # Only if absent or older than this arrival (FR-6)
    assert (
        kwargs["ConditionExpression"]
        == "attribute_not_exists(portal) OR released_at < :r"
    )
    assert kwargs["ExpressionAttributeValues"] == {":r": "2026-10-15T12:00:00.000Z"}


def test_publish_loses_to_a_later_arrival_and_still_succeeds(
    loader, mock_table, capsys
):
    mock_table.put_item.side_effect = _conditional_failure()
    out = loader.handler(_publish_event(), None)

    assert out == {"published": False}
    log = [json.loads(line) for line in capsys.readouterr().out.splitlines()][-1]
    assert log["action"] == "publish"
    assert log["reason"] == "superseded_by_later_arrival"


def test_publish_raises_other_dynamodb_errors(loader, mock_table):
    mock_table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "PutItem"
    )
    with pytest.raises(ClientError):
        loader.handler(_publish_event(), None)


def test_registry_order_is_iso_8601_execution_start_time():
    # The condition compares released_at as strings: Step Functions' fixed-width
    # ISO-8601 UTC timestamps sort the same as the times they name.
    earlier, later = "2026-10-15T09:59:59.999Z", "2026-10-15T10:00:00.000Z"
    assert earlier < later


# ---------------------------------------------------------------------------
# abort
# ---------------------------------------------------------------------------


def test_abort_marks_this_executions_row_error(loader, mock_table):
    error = {"Error": "States.TaskFailed", "Cause": "boom"}
    out = loader.handler({**_drop_event(), "action": "abort", "error": error}, None)

    assert out == {"aborted": True}
    kwargs = mock_table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"portal": "nf", "snapshot": "2026-04-25"}
    assert kwargs["ConditionExpression"] == "sequencer = :s"
    values = kwargs["ExpressionAttributeValues"]
    assert values[":error"] == "error"
    assert values[":s"] == "SEQ-2"
    assert json.loads(values[":cause"]) == error


def test_abort_leaves_a_row_another_execution_claimed(loader, mock_table):
    mock_table.update_item.side_effect = _conditional_failure()
    out = loader.handler({**_drop_event(), "action": "abort", "error": {}}, None)
    assert out == {"aborted": False}


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_returns_load_status(loader):
    resp = MagicMock()
    resp.json.return_value = {
        "payload": {"overallStatus": {"status": "LOAD_IN_PROGRESS", "totalRecords": 10}}
    }
    resp.raise_for_status = MagicMock()

    with patch.object(loader.requests, "get", return_value=resp):
        out = loader.handler({"action": "check", "load_id": "load-42"}, None)

    assert out["load_status"] == "LOAD_IN_PROGRESS"
    assert out["overall_status"]["totalRecords"] == 10


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


def _record_event(load_status, records=100):
    return {
        "action": "record",
        "portal": "nf",
        "snapshot": "2026-04-25",
        "load_id": "load-42",
        "named_graph": "urn:sagebrain:nf:2026-04-25",
        "prefix": "s3://mybucket/nf/2026-04-25/data/",
        "etag": "etag-1",
        "sequencer": "SEQ-1",
        "released_at": "2026-10-15T12:00:00.000Z",
        "load_status": load_status,
        "overall_status": {
            "status": load_status,
            "totalRecords": records,
            "parsingErrors": 0,
        },
    }


def test_record_success(loader, mock_table):
    out = loader.handler(_record_event("LOAD_COMPLETED"), None)
    assert out["status"] == "complete"
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["status"] == "complete"
    assert item["total_records"] == 100
    assert item["sequencer"] == "SEQ-1"
    assert item["released_at"] == "2026-10-15T12:00:00.000Z"


def test_record_failure(loader, mock_table):
    out = loader.handler(_record_event("LOAD_FAILED"), None)
    assert out["status"] == "error"
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["status"] == "error"
    assert item["error"]  # non-empty error payload


def test_record_failure_fetches_and_stores_load_errors(loader, mock_table):
    """_load_errors is called on failure; errors stored in DynamoDB + log."""
    error_entries = [
        {
            "errorCode": "PARSE_ERROR",
            "errorMessage": "unexpected token at line 42",
            "fileName": "s3://mybucket/nf/2026-04-25/data/rdf/bad.ttl",
            "recordNum": 42,
        }
    ]
    errors_resp = MagicMock()
    errors_resp.json.return_value = {
        "payload": {"errors": {"errorLogs": error_entries}}
    }
    errors_resp.raise_for_status = MagicMock()

    with patch.object(loader.requests, "get", return_value=errors_resp):
        out = loader.handler(_record_event("LOAD_FAILED"), None)

    assert out["status"] == "error"
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["status"] == "error"
    stored_errors = json.loads(item["load_errors"])
    assert stored_errors == error_entries


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def test_unknown_action_raises(loader):
    with pytest.raises(ValueError):
        loader.handler({"action": "bogus"}, None)
