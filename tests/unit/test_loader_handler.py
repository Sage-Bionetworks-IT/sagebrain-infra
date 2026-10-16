import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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


def _start_event(
    key="nf/2026-04-25/manifest.ttl",
    etag="etag-1",
    sequencer="0055AED6DCD90281E5",
    released_at="2026-10-15T12:00:00.000Z",
):
    return {
        "action": "start",
        "bucket": {"name": "mybucket"},
        "object": {"key": key, "etag": etag, "sequencer": sequencer},
        "released_at": released_at,
    }


def _load_response(load_id):
    resp = MagicMock()
    resp.json.return_value = {"payload": {"loadId": load_id}}
    resp.raise_for_status = MagicMock()
    return resp


def test_start_submits_load_into_named_graph(loader, mock_table):
    with patch.object(
        loader.requests, "post", return_value=_load_response("load-42")
    ) as mock_post:
        out = loader.handler(_start_event(), None)

    assert out["skip"] is False
    assert out["load_id"] == "load-42"
    assert out["named_graph"] == "urn:sagebrain:nf:2026-04-25"

    body = json.loads(mock_post.call_args.kwargs["data"])
    assert body["source"] == "s3://mybucket/nf/2026-04-25/data/"
    assert body["format"] == "turtle"
    assert body["failOnError"] == "TRUE"
    # Never AUTO: AUTO would resume the prior job for this source and no-op a
    # re-publish of the same tag, reporting success without loading anything.
    assert body["mode"] == "NEW"
    assert body["parserConfiguration"]["namedGraphUri"] == "urn:sagebrain:nf:2026-04-25"

    # in_progress row written
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["portal"] == "nf"
    assert item["snapshot"] == "2026-04-25"
    assert item["status"] == "in_progress"
    assert item["load_id"] == "load-42"


def test_start_records_sequencer_and_released_at(loader, mock_table):
    with patch.object(loader.requests, "post", return_value=_load_response("l")):
        out = loader.handler(_start_event(), None)

    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["sequencer"] == out["sequencer"] == "0055AED6DCD90281E5"
    assert item["released_at"] == out["released_at"] == "2026-10-15T12:00:00.000Z"


def test_start_loads_release_tag(loader, mock_table):
    with patch.object(
        loader.requests, "post", return_value=_load_response("l")
    ) as mock_post:
        out = loader.handler(_start_event(key="reactome/v97/manifest.ttl"), None)

    assert out["named_graph"] == "urn:sagebrain:reactome:v97"
    body = json.loads(mock_post.call_args.kwargs["data"])
    assert body["source"] == "s3://mybucket/reactome/v97/data/"


def test_start_skips_duplicate_delivery_same_sequencer(loader, mock_table):
    mock_table.get_item.return_value = {
        "Item": {"status": "complete", "sequencer": "SEQ-1", "load_id": "old"}
    }
    with patch.object(loader.requests, "post") as mock_post:
        out = loader.handler(_start_event(sequencer="SEQ-1"), None)

    assert out["skip"] is True
    mock_post.assert_not_called()


def test_start_reloads_identical_reupload(loader, mock_table):
    """D-5: a re-upload of identical bytes has the same etag but a new sequencer.

    Old behaviour: the etag matched, so the re-upload was skipped as a duplicate.
    """
    mock_table.get_item.return_value = {
        "Item": {
            "status": "complete",
            "etag": "etag-1",
            "sequencer": "SEQ-1",
            "load_id": "old",
        }
    }
    with patch.object(loader.requests, "post", return_value=_load_response("load-99")):
        out = loader.handler(_start_event(etag="etag-1", sequencer="SEQ-2"), None)

    assert out["skip"] is False
    assert out["load_id"] == "load-99"


def test_start_does_not_skip_rows_without_sequencer(loader, mock_table):
    # Rows written before D-5 carry no sequencer; an event with no sequencer must not match them.
    mock_table.get_item.return_value = {
        "Item": {"status": "complete", "etag": "etag-1", "load_id": "old"}
    }
    with patch.object(loader.requests, "post", return_value=_load_response("l")):
        out = loader.handler(_start_event(sequencer=""), None)

    assert out["skip"] is False


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
