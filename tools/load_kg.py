#!/usr/bin/env python3
"""
Load NF-OSI KG Turtle files into Amazon Neptune via SPARQL UPDATE.

Run this from the bastion host where Neptune is accessible.

Requirements:
    pip3 install rdflib requests aws-requests-auth boto3

Usage:
    # Load all files (ontology first, then data):
    python3 load_kg.py --endpoint <neptune-cluster-endpoint>

    # Load a single file:
    python3 load_kg.py --endpoint <neptune-cluster-endpoint> --file kgdata/data/rdf/studies.ttl

    # Dry-run (parse only, no upload):
    python3 load_kg.py --endpoint <neptune-cluster-endpoint> --dry-run

    # Clear all data first:
    python3 load_kg.py --endpoint <neptune-cluster-endpoint> --clear-first

Environment variables (override --endpoint / --region):
    NEPTUNE_ENDPOINT   Neptune cluster endpoint hostname
    AWS_DEFAULT_REGION AWS region (default: us-east-1)
"""

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from aws_requests_auth.boto_utils import BotoAWSRequestsAuth
from rdflib import Graph

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_REGION = "us-east-1"
NEPTUNE_PORT = 8182
BATCH_SIZE = 500  # triples per INSERT DATA request
RETRY_LIMIT = 3
RETRY_BACKOFF = 5  # seconds between retries

# Ordered list: ontology first so type info is present when data loads
LOAD_ORDER = [
    "kgdata/schema/ontology.ttl",
    "kgdata/data/rdf/funders.ttl",
    "kgdata/data/rdf/investigators.ttl",
    "kgdata/data/rdf/studies.ttl",
    "kgdata/data/rdf/mutations.ttl",
    "kgdata/data/rdf/mutation_model.ttl",
    "kgdata/data/rdf/animal_models.ttl",
    "kgdata/data/rdf/cell_lines.ttl",
    "kgdata/data/rdf/antibodies.ttl",
    "kgdata/data/rdf/genetic_reagents.ttl",
    "kgdata/data/rdf/donor_tool.ttl",
    "kgdata/data/rdf/donors.ttl",
    "kgdata/data/rdf/biobanks.ttl",
    "kgdata/data/rdf/development.ttl",
    "kgdata/data/rdf/resources.ttl",
    "kgdata/data/rdf/publications.ttl",
    "kgdata/data/rdf/observations.ttl",
    "kgdata/data/rdf/files.ttl",  # largest — loaded last
]


# ---------------------------------------------------------------------------
# Neptune SPARQL helpers
# ---------------------------------------------------------------------------


def make_auth(endpoint: str, region: str) -> BotoAWSRequestsAuth:
    # aws_host must include port so it matches the Host header Neptune receives
    return BotoAWSRequestsAuth(
        aws_host=f"{endpoint}:{NEPTUNE_PORT}",
        aws_region=region,
        aws_service="neptune-db",
    )


def sparql_update(endpoint: str, auth, update: str, dry_run: bool = False) -> bool:
    """Send a SPARQL UPDATE to Neptune. Returns True on success."""
    if dry_run:
        return True

    url = f"https://{endpoint}:{NEPTUNE_PORT}/sparql"
    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            resp = requests.post(
                url,
                data={"update": update},
                auth=auth,
                timeout=120,
            )
            if resp.status_code == 200:
                return True
            print(f"    HTTP {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        except requests.RequestException as exc:
            print(f"    Request error (attempt {attempt}): {exc}", file=sys.stderr)

        if attempt < RETRY_LIMIT:
            time.sleep(RETRY_BACKOFF)

    return False


def sparql_query(endpoint: str, auth, query: str) -> dict:
    """Send a SPARQL SELECT query and return the JSON response."""
    url = f"https://{endpoint}:{NEPTUNE_PORT}/sparql"
    resp = requests.post(
        url,
        data={"query": query},
        auth=auth,
        headers={"Accept": "application/sparql-results+json"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def clear_all(endpoint: str, auth, dry_run: bool = False):
    print("Clearing all named graphs (CLEAR ALL)...")
    ok = sparql_update(endpoint, auth, "CLEAR ALL", dry_run)
    if ok:
        print("  Done." if not dry_run else "  [dry-run] skipped.")
    else:
        print("  FAILED — aborting.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Core loader
# ---------------------------------------------------------------------------


def named_graph_uri(ttl_path: Path) -> str:
    """Derive a stable named graph URI from the file path."""
    # e.g. kgdata/data/rdf/studies.ttl  ->  http://nf-osi.github.com/graphs/studies
    stem = ttl_path.stem  # filename without extension
    return f"http://nf-osi.github.com/graphs/{stem}"


def triples_to_ntriples_lines(graph: Graph) -> list[str]:
    """Serialize all triples as N-Triples strings (one per line)."""
    nt = graph.serialize(format="nt")
    return [
        line for line in nt.splitlines() if line.strip() and not line.startswith("#")
    ]


def build_insert_data(graph_uri: str, nt_lines: list[str]) -> str:
    triples = "\n".join(nt_lines)
    return f"INSERT DATA {{ GRAPH <{graph_uri}> {{ {triples} }} }}"


def load_file(
    ttl_path: Path,
    endpoint: str,
    auth,
    batch_size: int = BATCH_SIZE,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Parse a Turtle file and upload in batches.
    Returns (triples_loaded, batches_sent).
    """
    print(f"\nLoading: {ttl_path}")

    graph = Graph()
    try:
        graph.parse(ttl_path, format="turtle")
    except Exception as exc:
        print(f"  Parse error: {exc}", file=sys.stderr)
        return 0, 0

    total = len(graph)
    graph_uri = named_graph_uri(ttl_path)
    print(f"  Triples parsed : {total:,}")
    print(f"  Named graph    : {graph_uri}")
    print(f"  Batch size     : {batch_size}")

    nt_lines = triples_to_ntriples_lines(graph)
    batches_ok = 0
    failed = 0

    for i in range(0, len(nt_lines), batch_size):
        chunk = nt_lines[i : i + batch_size]
        update = build_insert_data(graph_uri, chunk)
        batch_num = i // batch_size + 1
        total_batches = (len(nt_lines) + batch_size - 1) // batch_size

        ok = sparql_update(endpoint, auth, update, dry_run)
        if ok:
            batches_ok += 1
            print(
                f"  Batch {batch_num}/{total_batches}: {len(chunk)} triples  ✓",
                end="\r",
            )
        else:
            failed += 1
            print(f"  Batch {batch_num}/{total_batches}: FAILED", file=sys.stderr)

    print(f"  Batches: {batches_ok} ok, {failed} failed       ")
    return total, batches_ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(
        description="Load NF-OSI KG Turtle files into Amazon Neptune.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("NEPTUNE_ENDPOINT"),
        help="Neptune cluster endpoint hostname (or set NEPTUNE_ENDPOINT env var)",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION),
        help=f"AWS region (default: {DEFAULT_REGION})",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        help="Load a single TTL file instead of the full dataset",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"Triples per INSERT DATA request (default: {BATCH_SIZE})",
    )
    parser.add_argument(
        "--clear-first",
        action="store_true",
        help="Run CLEAR ALL before loading (destructive!)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse files but do not send to Neptune",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print triple-count stats from Neptune after loading",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if not args.endpoint:
        print("Error: --endpoint or NEPTUNE_ENDPOINT is required.", file=sys.stderr)
        sys.exit(1)

    auth = make_auth(args.endpoint, args.region)

    if args.clear_first:
        clear_all(args.endpoint, auth, args.dry_run)

    # Resolve files to load
    if args.file:
        files = [Path(args.file)]
    else:
        repo_root = Path(__file__).parent.parent
        files = [repo_root / p for p in LOAD_ORDER]

    # Load
    grand_total = 0
    t0 = time.time()
    for ttl_path in files:
        if not ttl_path.exists():
            print(f"  Skipping (not found): {ttl_path}", file=sys.stderr)
            continue
        triples, _ = load_file(
            ttl_path, args.endpoint, auth, args.batch_size, args.dry_run
        )
        grand_total += triples

    elapsed = time.time() - t0
    print(
        f"\n{'[DRY-RUN] ' if args.dry_run else ''}Done. {grand_total:,} triples in {elapsed:.1f}s"
    )

    if args.stats and not args.dry_run:
        result = sparql_query(
            args.endpoint,
            auth,
            "SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }",
        )
        count = result["results"]["bindings"][0]["n"]["value"]
        print(f"Neptune total triples now: {count}")


if __name__ == "__main__":
    main()
