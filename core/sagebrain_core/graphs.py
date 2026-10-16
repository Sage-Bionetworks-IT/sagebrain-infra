"""The snapshot graphs a query may read: each portal's live graph, from the pipeline's registry.

The ingestion pipeline publishes one item per portal under partition `_live` of its tracking
table (spec 004). `execute_query` sends this list as the SPARQL Protocol dataset, so readers see
only the latest published snapshot of each portal.
"""

import time

import boto3
from boto3.dynamodb.conditions import Key

from . import limits

LIVE_PARTITION = "_live"
# Sent when no portal is published: it names no graph, so the query matches nothing. Sending no
# dataset at all would mean Neptune's default, the union of every graph (FR-13, fail closed).
NO_GRAPHS = "urn:sagebrain:none"

# One cached list per table, per Lambda instance: (graphs, monotonic expiry).
_cache: dict = {}


def live_graphs(table_name: str) -> list:
    """Each portal's live graph IRI, sorted; cached for `live_graphs_cache_seconds`.

    Raises if the registry can't be read: the query fails rather than reading the union.
    """
    cached = _cache.get(table_name)
    now = time.monotonic()
    if cached and now < cached[1]:
        return cached[0]
    graphs = _read(table_name)
    _cache[table_name] = (graphs, now + limits.LIVE_GRAPHS_CACHE_SECONDS)
    return graphs


def _read(table_name: str) -> list:
    table = boto3.resource("dynamodb").Table(table_name)
    kwargs = {
        "KeyConditionExpression": Key("portal").eq(LIVE_PARTITION),
        "ProjectionExpression": "named_graph",
    }
    graphs = set()
    while True:
        page = table.query(**kwargs)
        graphs.update(item["named_graph"] for item in page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return sorted(graphs) or [NO_GRAPHS]
