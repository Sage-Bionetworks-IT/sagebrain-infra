"""
Seed the live registry (spec 004) from the load tracking table.

For each portal, registers its newest `complete` snapshot as `_live/<portal>`, the item the
pipeline's Publish step writes. Run it in an env after the registry PR deploys and **before**
`NEPTUNE_API.graph_scoping` is turned on there: once scoping is on, a portal with no registry
entry is invisible to queries.

"Newest" is the row's `released_at` (the arrival order the pipeline uses), falling back to
`finished_at` for rows loaded before spec 004, then to the tag. An existing registry entry is
never overwritten, so the tool is idempotent and can't roll back a portal the pipeline has
since published.

Usage:
    python tools/backfill_live_registry.py --profile sagebrain-dev --env dev --portal nf --dry-run
    python tools/backfill_live_registry.py --profile sagebrain-dev --env dev --portal nf

Without --portal every portal with a complete snapshot is registered. In dev, register only
`nf` and leave `test` unregistered (hidden).
"""

import argparse
import sys
import time
from datetime import datetime, timezone

LIVE_PARTITION = "_live"


def _iso(epoch) -> str:
    """Epoch seconds → the fixed-width ISO-8601 UTC form of `$$.Execution.StartTime`."""
    dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _released_at(row: dict) -> str:
    if row.get("released_at"):
        return row["released_at"]
    if row.get("finished_at"):
        return _iso(row["finished_at"])
    return ""


def scan(table) -> list:
    rows, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        rows.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            return rows
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def plan(rows: list, portals: set | None = None) -> dict:
    """The newest complete snapshot of each portal (optionally only `portals`) → its row."""
    newest = {}
    for row in rows:
        portal = row["portal"]
        if portal == LIVE_PARTITION or row.get("status") != "complete":
            continue
        if portals and portal not in portals:
            continue
        key = (_released_at(row), row["snapshot"])
        best = newest.get(portal)
        if best is None or key > (_released_at(best), best["snapshot"]):
            newest[portal] = row
    return newest


def registry_item(row: dict) -> dict:
    return {
        "portal": LIVE_PARTITION,
        "snapshot": row["portal"],
        "tag": row["snapshot"],
        "named_graph": row["named_graph"],
        "released_at": _released_at(row),
        "published_at": int(time.time()),
        "backfilled": True,
    }


def apply(table, newest: dict, dry_run: bool, out=print) -> int:
    """Write each registry entry unless one exists. Returns the number written."""
    from botocore.exceptions import ClientError

    existing = {r["snapshot"]: r for r in scan(table) if r["portal"] == LIVE_PARTITION}
    written = 0
    for portal, row in sorted(newest.items()):
        item = registry_item(row)
        if portal in existing:
            out(
                f"{portal}: already live → {existing[portal]['named_graph']} (unchanged)"
            )
            continue
        if dry_run:
            out(
                f"{portal}: would register {item['named_graph']} ({item['released_at']})"
            )
            continue
        try:
            table.put_item(
                Item=item, ConditionExpression="attribute_not_exists(portal)"
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            out(f"{portal}: registered concurrently by the pipeline (unchanged)")
            continue
        written += 1
        out(f"{portal}: registered {item['named_graph']} ({item['released_at']})")
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--profile", required=True, help="AWS profile, e.g. sagebrain-dev"
    )
    parser.add_argument("--env", required=True, choices=["dev", "stage", "prod"])
    parser.add_argument(
        "--portal", action="append", help="register only this portal (repeatable)"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    import boto3

    table = (
        boto3.Session(profile_name=args.profile)
        .resource("dynamodb")
        .Table(f"app-{args.env}-neptune-pipeline-loads")
    )
    newest = plan(scan(table), set(args.portal) if args.portal else None)
    missing = set(args.portal or []) - set(newest)
    for portal in sorted(missing):
        print(f"{portal}: no complete snapshot in the tracking table", file=sys.stderr)
    apply(table, newest, args.dry_run)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
