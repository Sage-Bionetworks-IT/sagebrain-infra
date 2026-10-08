#!/usr/bin/env python3
"""
Delete named graphs from Amazon Neptune.

Run from any machine with VPC access to Neptune and the sagebrain AWS profile.
Credentials are picked up automatically from the AWS profile.

Requirements:
    pip install requests aws-requests-auth

Usage:
    # List all named graphs:
    python tools/delete_graph.py --endpoint <neptune-endpoint> --list

    # Delete a specific graph (with confirmation):
    python tools/delete_graph.py --endpoint <neptune-endpoint> --graph "urn:sagebrain:nf:2026-02-20"

    # Delete without confirmation (use with caution):
    python tools/delete_graph.py --endpoint <neptune-endpoint> --graph "..." --force

    # Delete the default graph:
    python tools/delete_graph.py --endpoint <neptune-endpoint> \
        --graph "http://aws.amazon.com/neptune/vocab/v01/DefaultNamedGraph" --force

    # Dry-run (print what would happen, no changes):
    python tools/delete_graph.py --endpoint <neptune-endpoint> --graph "..." --dry-run

    # Show triple count for a graph before deleting:
    python tools/delete_graph.py --endpoint <neptune-endpoint> --graph "..." --stats

Environment variables (override CLI flags):
    NEPTUNE_ENDPOINT    Neptune cluster endpoint hostname
    AWS_DEFAULT_REGION  AWS region (default: us-east-1)
"""

import argparse
import os
import sys

import requests
from aws_requests_auth.boto_utils import BotoAWSRequestsAuth

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_REGION = "us-east-1"
NEPTUNE_PORT = 8182

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


def list_graphs(endpoint: str, auth) -> list:
    """List all named graphs and their triple counts."""
    query = """
    SELECT ?g (COUNT(*) AS ?n)
    WHERE { GRAPH ?g { ?s ?p ?o } }
    GROUP BY ?g
    ORDER BY DESC(?n)
    """
    resp = requests.post(
        f"https://{endpoint}:{NEPTUNE_PORT}/sparql",
        data={"query": query},
        auth=auth,
        headers={"Accept": "application/sparql-results+json"},
        timeout=60,
    )
    resp.raise_for_status()
    bindings = resp.json()["results"]["bindings"]
    return [
        {
            "graph": b["g"]["value"],
            "triples": int(b["n"]["value"]),
        }
        for b in bindings
    ]


def graph_triple_count(endpoint: str, auth, graph_uri: str) -> int:
    """Count triples in a specific graph."""
    query = f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{graph_uri}> {{ ?s ?p ?o }} }}"
    resp = requests.post(
        f"https://{endpoint}:{NEPTUNE_PORT}/sparql",
        data={"query": query},
        auth=auth,
        headers={"Accept": "application/sparql-results+json"},
        timeout=60,
    )
    resp.raise_for_status()
    return int(resp.json()["results"]["bindings"][0]["n"]["value"])


def delete_graph(endpoint: str, auth, graph_uri: str, dry_run: bool = False) -> bool:
    """
    Delete a named graph using SPARQL UPDATE.
    Returns True on success, False on failure.
    """
    update = f"DROP GRAPH <{graph_uri}>"

    if dry_run:
        print(f"  [dry-run] Would execute: {update}")
        return True

    try:
        resp = requests.post(
            f"https://{endpoint}:{NEPTUNE_PORT}/sparql",
            data={"update": update},
            auth=auth,
            timeout=120,
        )
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        print(f"  FAILED: {e.response.status_code} {e.response.text}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="Delete named graphs from Amazon Neptune.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("NEPTUNE_ENDPOINT"),
        help="Neptune cluster endpoint hostname",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION),
        help=f"AWS region (default: {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--graph",
        help="Named graph URI to delete",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all named graphs and their triple counts",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show triple count for the graph before deleting",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without making any changes",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.endpoint:
        print("Error: missing required argument: --endpoint", file=sys.stderr)
        sys.exit(1)

    auth = make_auth(args.endpoint, args.region)

    # List mode
    if args.list:
        print("Listing all named graphs...")
        graphs = list_graphs(args.endpoint, auth)
        if not graphs:
            print("  No named graphs found.")
            return

        print(f"\nFound {len(graphs)} graph(s):\n")
        for g in graphs:
            print(f"  {g['triples']:>12,} triples  {g['graph']}")
        return

    # Delete mode
    if not args.graph:
        print(
            "Error: --graph is required (or use --list to see available graphs)",
            file=sys.stderr,
        )
        sys.exit(1)

    graph_uri = args.graph

    # Show stats if requested
    if args.stats:
        try:
            count = graph_triple_count(args.endpoint, auth, graph_uri)
            print(f"Graph contains {count:,} triples")
        except Exception as e:
            print(f"Warning: could not count triples: {e}", file=sys.stderr)

    # Confirm unless --force or --dry-run
    if not args.force and not args.dry_run:
        print(f"\n{'='*70}")
        print("  WARNING: You are about to DELETE this graph:")
        print(f"  {graph_uri}")
        print(f"{'='*70}\n")
        response = input("Type 'DELETE' to confirm: ")
        if response != "DELETE":
            print("Aborted.")
            sys.exit(0)

    print(f"{'[DRY-RUN] ' if args.dry_run else ''}Deleting graph: {graph_uri}")
    ok = delete_graph(args.endpoint, auth, graph_uri, args.dry_run)

    if ok:
        print(f"{'[DRY-RUN] ' if args.dry_run else ''}Done.")
    else:
        print("Delete failed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
