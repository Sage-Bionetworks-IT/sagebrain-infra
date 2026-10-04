"""Spec 001 T147 — the shared rate-limit table (DynamoLimiter's buckets)."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from src.rate_limit_stack import RateLimitStack


@pytest.fixture(scope="module")
def stack():
    return RateLimitStack(cdk.App(), "app-test-rate-limits")


@pytest.fixture(scope="module")
def template(stack):
    return Template.from_stack(stack)


def test_one_table_and_nothing_else(template):
    assert {r["Type"] for r in template.to_json()["Resources"].values()} == {
        "AWS::DynamoDB::Table"
    }


def test_table_props(template):
    (table,) = template.find_resources("AWS::DynamoDB::Table").values()
    props = table["Properties"]
    assert props["TableName"] == "app-test-rate-limits"
    assert props["KeySchema"] == [{"AttributeName": "key", "KeyType": "HASH"}]
    assert props["AttributeDefinitions"] == [
        {"AttributeName": "key", "AttributeType": "S"}
    ]
    assert props["BillingMode"] == "PAY_PER_REQUEST"
    assert props["TimeToLiveSpecification"] == {
        "AttributeName": "expires_at",
        "Enabled": True,
    }
    # Ephemeral buckets: no point-in-time recovery, deleted with the stack.
    assert props["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": False
    }
    assert table["DeletionPolicy"] == "Delete"
    assert table["UpdateReplacePolicy"] == "Delete"
