#!/usr/bin/env python3
"""
Load NF-OSI KG data into Amazon Neptune via the S3 bulk loader.

Run from a SageMaker Studio terminal or notebook where Neptune is accessible
via the VPC. Credentials are picked up automatically from the instance role.

Requirements:
    pip install requests aws-requests-auth

Usage:
    # Reset database and load schema + data for a given date:
    python tools/load_kg.py --endpoint <neptune-endpoint> --bucket <bucket> --prefix 2026-02-20

    # Load without clearing (append mode):
    python tools/load_kg.py --endpoint <neptune-endpoint> --bucket <bucket> --prefix 2026-02-20 --no-reset

    # Dry-run (print what would happen, no changes):
    python tools/load_kg.py --endpoint <neptune-endpoint> --bucket <bucket> --prefix 2026-02-20 --dry-run

    # Print triple count after loading:
    python tools/load_kg.py ... --stats

Environment variables (override CLI flags):
    NEPTUNE_ENDPOINT    Neptune cluster endpoint hostname
    NEPTUNE_LOAD_ROLE   IAM role ARN for the Neptune bulk loader
    NEPTUNE_BUCKET      S3 bucket name
    AWS_DEFAULT_REGION  AWS region (default: us-east-1)
"""

import argparse
import os
import sys
import time

import requests
from aws_requests_auth.boto_utils import BotoAWSRequestsAuth

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_REGION = "us-east-1"
NEPTUNE_PORT = 8182
POLL_INTERVAL = 5  # seconds between load status checks
RESTART_TIMEOUT = 120  # seconds to wait for Neptune after a reset


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def make_auth(endpoint: str, region: str) -> BotoAWSRequestsAuth:
    return BotoAWSRequestsAuth(
        aws_host=f"{endpoint}:{NEPTUNE_PORT}",
        aws_region=region,
        aws_service="neptune-db",
    )


# ---------------------------------------------------------------------------
# Neptune helpers
# ---------------------------------------------------------------------------


def wait_for_neptune(endpoint: str, auth, timeout: int = RESTART_TIMEOUT):
    """Poll /status until Neptune responds — needed after a database reset."""
    print("Waiting for Neptune to be ready...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"https://{endpoint}:{NEPTUNE_PORT}/status",
                auth=auth,
                timeout=5,
            )
            if resp.status_code == 200:
                print("  Neptune is ready.")
                return
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Neptune did not become ready within {timeout}s")


def reset_database(endpoint: str, auth, dry_run: bool = False):
    """
    Full database wipe using the Neptune system reset endpoint.
    Faster and more reliable than SPARQL DROP ALL.
    """
    print("Resetting database...")
    if dry_run:
        print("  [dry-run] skipped.")
        return

    # Step 1: get confirmation token
    resp = requests.post(
        f"https://{endpoint}:{NEPTUNE_PORT}/system",
        data={"action": "initiateDatabaseReset"},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()["payload"]["token"]

    # Step 2: confirm reset
    resp = requests.post(
        f"https://{endpoint}:{NEPTUNE_PORT}/system",
        data={"action": "performDatabaseReset", "token": token},
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    print("  Reset initiated — waiting for Neptune to restart...")
    time.sleep(5)  # brief pause before polling
    wait_for_neptune(endpoint, auth)


def bulk_load(
    endpoint: str,
    auth,
    s3_source: str,
    load_role_arn: str,
    region: str,
    fmt: str = "turtle",
    dry_run: bool = False,
) -> bool:
    """
    Submit an S3 bulk load job and poll until complete.
    Returns True on success, False on failure.
    Non-RDF files (e.g. README.md) are skipped automatically via failOnError=FALSE.
    """
    print(f"  source : {s3_source}")
    if dry_run:
        print("  [dry-run] skipped.")
        return True

    resp = requests.post(
        f"https://{endpoint}:{NEPTUNE_PORT}/loader",
        json={
            "source": s3_source,
            "format": fmt,
            "iamRoleArn": load_role_arn,
            "region": region,
            "failOnError": "FALSE",  # skip non-RDF files (e.g. README.md)
        },
        auth=auth,
        timeout=30,
    )
    resp.raise_for_status()
    load_id = resp.json()["payload"]["loadId"]
    print(f"  load_id: {load_id}")

    while True:
        status_resp = requests.get(
            f"https://{endpoint}:{NEPTUNE_PORT}/loader/{load_id}",
            auth=auth,
            timeout=30,
        )
        status_resp.raise_for_status()
        payload = status_resp.json()["payload"]["overallStatus"]
        state = payload["status"]
        print(f"  status : {state}")

        if state == "LOAD_COMPLETED":
            print(
                f"  records: {payload['totalRecords']:,}  "
                f"dupes: {payload['totalDuplicates']:,}  "
                f"parse_errors: {payload['parsingErrors']}  "
                f"time: {payload['totalTimeSpent']}s"
            )
            return True

        if state in ("LOAD_FAILED", "LOAD_CANCELLED"):
            print(f"  FAILED: {payload}", file=sys.stderr)
            return False

        time.sleep(POLL_INTERVAL)


def triple_count(endpoint: str, auth) -> int:
    resp = requests.post(
        f"https://{endpoint}:{NEPTUNE_PORT}/sparql",
        data={"query": "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }"},
        auth=auth,
        headers={"Accept": "application/sparql-results+json"},
        timeout=60,
    )
    resp.raise_for_status()
    return int(resp.json()["results"]["bindings"][0]["n"]["value"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="Load NF-OSI KG data into Neptune via the S3 bulk loader.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("NEPTUNE_ENDPOINT"),
        help="Neptune cluster endpoint hostname",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("NEPTUNE_BUCKET"),
        help="S3 bucket name",
    )
    parser.add_argument(
        "--prefix",
        required=True,
        help="S3 date prefix, e.g. 2026-02-20",
    )
    parser.add_argument(
        "--load-role",
        default=os.environ.get("NEPTUNE_LOAD_ROLE"),
        help="IAM role ARN for the Neptune bulk loader",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION),
        help=f"AWS region (default: {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--format",
        default="turtle",
        help="RDF format for both schema and data (default: turtle)",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Skip database reset and append to existing data",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without making any changes",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print total triple count after loading",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    missing = [
        f
        for f, v in [
            ("--endpoint", args.endpoint),
            ("--bucket", args.bucket),
            ("--load-role", args.load_role),
        ]
        if not v
    ]
    if missing:
        print(
            f"Error: missing required arguments: {', '.join(missing)}", file=sys.stderr
        )
        sys.exit(1)

    auth = make_auth(args.endpoint, args.region)

    prefix = args.prefix.rstrip("/")
    schema_source = f"s3://{args.bucket}/{prefix}/schema"
    data_source = f"s3://{args.bucket}/{prefix}/data/rdf"

    print(
        f"{'[DRY-RUN] ' if args.dry_run else ''}Starting load from s3://{args.bucket}/{prefix}/"
    )
    print(f"  schema : {schema_source}")
    print(f"  data   : {data_source}")
    print()

    t0 = time.time()

    if not args.no_reset:
        reset_database(args.endpoint, auth, args.dry_run)
        print()

    print("Loading schema...")
    ok = bulk_load(
        args.endpoint,
        auth,
        schema_source,
        args.load_role,
        args.region,
        fmt=args.format,
        dry_run=args.dry_run,
    )
    if not ok:
        print("Schema load failed — aborting.", file=sys.stderr)
        sys.exit(1)
    print()

    print("Loading data...")
    ok = bulk_load(
        args.endpoint,
        auth,
        data_source,
        args.load_role,
        args.region,
        fmt=args.format,
        dry_run=args.dry_run,
    )
    if not ok:
        print("Data load failed.", file=sys.stderr)
        sys.exit(1)
    print()

    elapsed = time.time() - t0
    print(f"{'[DRY-RUN] ' if args.dry_run else ''}Done in {elapsed:.1f}s")

    if args.stats and not args.dry_run:
        count = triple_count(args.endpoint, auth)
        print(f"Total triples in Neptune: {count:,}")


if __name__ == "__main__":
    main()
