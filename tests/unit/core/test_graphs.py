"""Spec 004 — live_graphs(): the registry read, its cache, the sentinel, fail closed."""

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from sagebrain_core import graphs, limits


@pytest.fixture(autouse=True)
def clear_cache():
    graphs._cache.clear()
    yield
    graphs._cache.clear()


@pytest.fixture
def table():
    table = MagicMock()
    with patch("sagebrain_core.graphs.boto3.resource") as resource:
        resource.return_value.Table.return_value = table
        table.resource = resource
        yield table


def _items(*iris):
    return {"Items": [{"named_graph": iri} for iri in iris]}


def test_reads_the_live_partition(table):
    table.query.return_value = _items(
        "urn:sagebrain:reactome:v97", "urn:sagebrain:nf:2026-07-28"
    )
    assert graphs.live_graphs("loads") == [
        "urn:sagebrain:nf:2026-07-28",
        "urn:sagebrain:reactome:v97",
    ]
    table.resource.return_value.Table.assert_called_with("loads")
    condition = table.query.call_args.kwargs["KeyConditionExpression"]
    assert condition.get_expression()["values"][1] == "_live"


def test_follows_pagination(table):
    table.query.side_effect = [
        {**_items("urn:a"), "LastEvaluatedKey": {"portal": "_live", "snapshot": "a"}},
        _items("urn:b"),
    ]
    assert graphs.live_graphs("loads") == ["urn:a", "urn:b"]
    assert table.query.call_args.kwargs["ExclusiveStartKey"] == {
        "portal": "_live",
        "snapshot": "a",
    }


def test_empty_registry_is_the_sentinel_not_the_union(table):
    # FR-13: no dataset would mean Neptune's default graph, every snapshot.
    table.query.return_value = {"Items": []}
    assert graphs.live_graphs("loads") == ["urn:sagebrain:none"]


def test_read_error_raises(table):
    table.query.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException"}}, "Query"
    )
    with pytest.raises(ClientError):
        graphs.live_graphs("loads")


def test_error_is_not_cached(table):
    table.query.side_effect = [
        ClientError({"Error": {"Code": "InternalServerError"}}, "Query"),
        _items("urn:a"),
    ]
    with pytest.raises(ClientError):
        graphs.live_graphs("loads")
    assert graphs.live_graphs("loads") == ["urn:a"]


def test_cached_for_live_graphs_cache_seconds(table):
    # FR-14: a newly published snapshot is visible within 60 s.
    assert limits.LIVE_GRAPHS_CACHE_SECONDS == 60
    table.query.side_effect = [_items("urn:old"), _items("urn:new")]
    with patch("sagebrain_core.graphs.time.monotonic", return_value=1000.0):
        assert graphs.live_graphs("loads") == ["urn:old"]
    with patch("sagebrain_core.graphs.time.monotonic", return_value=1059.9):
        assert graphs.live_graphs("loads") == ["urn:old"]
    with patch("sagebrain_core.graphs.time.monotonic", return_value=1060.0):
        assert graphs.live_graphs("loads") == ["urn:new"]
    assert table.query.call_count == 2
