"""tools/backfill_live_registry.py — seeds _live/<portal> from the tracking table (spec 004)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parents[2] / "tools"))

import backfill_live_registry as backfill  # noqa: E402

# 2026-07-28T12:00:00Z and 2026-07-14T12:00:00Z
JUL28, JUL14 = 1785240000, 1784030400


def _row(portal, tag, status="complete", **fields):
    return {
        "portal": portal,
        "snapshot": tag,
        "status": status,
        "named_graph": f"urn:sagebrain:{portal}:{tag}",
        **fields,
    }


ROWS = [
    _row("nf", "2026-07-14", finished_at=JUL14),
    _row("nf", "2026-07-28", finished_at=JUL28),
    _row("nf", "2026-08-01", status="error", finished_at=JUL28 + 99),
    _row("test", "2026-07-28", finished_at=JUL28),
    _row("_live", "old", named_graph="urn:sagebrain:old:x"),
]


def _table(rows):
    table = MagicMock()
    table.scan.return_value = {"Items": rows}
    return table


def test_plan_picks_newest_complete_row_per_portal():
    newest = backfill.plan(ROWS)
    assert {p: r["snapshot"] for p, r in newest.items()} == {
        "nf": "2026-07-28",  # not the failed 08-01
        "test": "2026-07-28",
    }


def test_plan_prefers_released_at_over_finished_at():
    # A rollback: the older tag arrived last, so it is the live one.
    rows = [
        _row("nf", "2026-07-28", released_at="2026-10-01T00:00:00.000Z"),
        _row("nf", "2026-07-14", released_at="2026-10-02T00:00:00.000Z"),
    ]
    assert backfill.plan(rows)["nf"]["snapshot"] == "2026-07-14"


def test_plan_limits_to_portals():
    assert set(backfill.plan(ROWS, {"nf"})) == {"nf"}


def test_registry_item_matches_the_pipelines_publish():
    item = backfill.registry_item(_row("nf", "2026-07-28", finished_at=JUL28))
    assert item["portal"] == "_live"
    assert item["snapshot"] == "nf"
    assert item["tag"] == "2026-07-28"
    assert item["named_graph"] == "urn:sagebrain:nf:2026-07-28"
    # Same fixed-width form as $$.Execution.StartTime, so the pipeline's comparison works.
    assert item["released_at"] == "2026-07-28T12:00:00.000Z"


def test_dry_run_writes_nothing():
    table = _table(ROWS)
    out = []
    assert backfill.apply(table, backfill.plan(ROWS, {"nf"}), True, out.append) == 0
    table.put_item.assert_not_called()
    assert out == [
        "nf: would register urn:sagebrain:nf:2026-07-28 (2026-07-28T12:00:00.000Z)"
    ]


def test_apply_writes_conditionally_and_skips_existing_entries():
    rows = ROWS + [_row("_live", "test", named_graph="urn:sagebrain:test:2026-07-28")]
    table = _table(rows)
    out = []
    assert backfill.apply(table, backfill.plan(rows), False, out.append) == 1

    (call,) = table.put_item.call_args_list
    assert call.kwargs["Item"]["snapshot"] == "nf"
    assert call.kwargs["ConditionExpression"] == "attribute_not_exists(portal)"
    assert "test: already live → urn:sagebrain:test:2026-07-28 (unchanged)" in out


def test_apply_tolerates_a_concurrent_publish():
    table = _table(ROWS)
    table.put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
    )
    assert (
        backfill.apply(table, backfill.plan(ROWS, {"nf"}), False, lambda _: None) == 0
    )
