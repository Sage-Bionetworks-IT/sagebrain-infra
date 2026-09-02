import importlib
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
    monkeypatch.setenv("GRAPH_URI_TEMPLATE", "urn:sagebrain:{portal}:{date}")


@pytest.fixture
def mock_table_and_client():
    table = MagicMock()
    client = MagicMock()
    table.get_item.return_value = {}  # no existing load by default
    with patch("boto3.resource") as mock_resource, patch("boto3.client") as mock_client:
        mock_resource.return_value.Table.return_value = table
        mock_client.return_value = client
        yield table, client


@pytest.fixture
def loader(mock_table_and_client):
    sys.modules.pop("loader", None)
    import loader as mod

    importlib.reload(mod)
    mock_table, mock_client = mock_table_and_client
    mod._dynamodb.Table.return_value = mock_table
    mod._neptunedata = mock_client
    return mod


# ---------------------------------------------------------------------------
# Key parsing
# ---------------------------------------------------------------------------


def test_parse_snapshot_valid(loader):
    snap = loader.parse_snapshot("mybucket", "nf/2026-04-25/manifest.ttl")
    assert snap["portal"] == "nf"
    assert snap["date"] == "2026-04-25"
    assert snap["snapshot"] == "2026-04-25"
    assert snap["prefix"] == "s3://mybucket/nf/2026-04-25/"
    assert snap["named_graph"] == "urn:sagebrain:nf:2026-04-25"


def test_parse_snapshot_rejects_wrong_shape(loader):
    with pytest.raises(ValueError):
        loader.parse_snapshot("b", "nf/2026-04-25/data/thing.ttl")
    with pytest.raises(ValueError):
        loader.parse_snapshot("b", "nf/manifest.ttl")


def test_parse_snapshot_rejects_bad_date(loader):
    with pytest.raises(ValueError):
        loader.parse_snapshot("b", "nf/not-a-date/manifest.ttl")


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def _start_event(key="nf/2026-04-25/manifest.ttl", etag="etag-1"):
    return {
        "action": "start",
        "bucket": {"name": "mybucket"},
        "object": {"key": key, "etag": etag},
    }


def test_start_submits_load_into_named_graph(loader, mock_table_and_client):
    mock_table, mock_client = mock_table_and_client
    mock_client.start_loader_job.return_value = {"payload": {"loadId": "load-42"}}
    out = loader.handler(_start_event(), None)

    assert out["skip"] is False
    assert out["load_id"] == "load-42"
    assert out["named_graph"] == "urn:sagebrain:nf:2026-04-25"

    call = mock_client.start_loader_job.call_args.kwargs
    assert call["source"] == "s3://mybucket/nf/2026-04-25/"
    assert call["format"] == "turtle"
    assert call["s3BucketRegion"] == "us-east-1"
    assert call["failOnError"] == "TRUE"
    assert call["parserConfiguration"]["namedGraphUri"] == "urn:sagebrain:nf:2026-04-25"

    # in_progress row written
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["portal"] == "nf"
    assert item["snapshot"] == "2026-04-25"
    assert item["status"] == "in_progress"
    assert item["load_id"] == "load-42"


def test_start_skips_when_already_complete_same_etag(loader, mock_table_and_client):
    mock_table, mock_client = mock_table_and_client
    mock_table.get_item.return_value = {
        "Item": {"status": "complete", "etag": "etag-1", "load_id": "old"}
    }
    out = loader.handler(_start_event(etag="etag-1"), None)

    assert out["skip"] is True
    mock_client.start_loader_job.assert_not_called()


def test_start_reloads_when_etag_changed(loader, mock_table_and_client):
    mock_table, mock_client = mock_table_and_client
    mock_table.get_item.return_value = {
        "Item": {"status": "complete", "etag": "old-etag", "load_id": "old"}
    }
    mock_client.start_loader_job.return_value = {"payload": {"loadId": "load-99"}}
    out = loader.handler(_start_event(etag="new-etag"), None)

    assert out["skip"] is False
    assert out["load_id"] == "load-99"


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_returns_load_status(loader):
    loader._neptunedata.get_loader_job_status.return_value = {
        "payload": {"overallStatus": {"status": "LOAD_IN_PROGRESS", "totalRecords": 10}}
    }
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
        "prefix": "s3://mybucket/nf/2026-04-25/",
        "etag": "etag-1",
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


def test_record_failure(loader, mock_table):
    out = loader.handler(_record_event("LOAD_FAILED"), None)
    assert out["status"] == "error"
    item = mock_table.put_item.call_args.kwargs["Item"]
    assert item["status"] == "error"
    assert item["error"]  # non-empty error payload


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def test_unknown_action_raises(loader):
    with pytest.raises(ValueError):
        loader.handler({"action": "bogus"}, None)
