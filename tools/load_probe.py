"""
Load and burst probes for the unified API (spec 001 success criteria).

  load   The design target: USERS simulated users, each submitting a cheap `LIMIT 1` query
         and polling its status, honouring Retry-After (2 s), for DURATION seconds, then
         submitting the next one. Passes if the status method returned no 429s.
  burst  BURST concurrent POST /query requests at once. Expect about method_burst (100) of
         them through and the rest 429. The body is an empty query, so the requests that get
         through are rejected 400 by the handler and no job is created.

Usage:
    export SYNAPSE_AUTH_TOKEN=<synapse-pat>   # team member
    BASE=https://<api-id>.execute-api.us-east-1.amazonaws.com/<env>
    python tools/load_probe.py load  "$BASE" --users 100 --duration 600
    python tools/load_probe.py burst "$BASE" --requests 300

The load probe can't see the queue. While it runs, watch the query queue's
ApproximateAgeOfOldestMessage in CloudWatch (it should stay under 5 minutes).

Exit code is non-zero if the probe's pass condition fails.
"""

import argparse
import json
import os
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

TIMEOUT = 15
CHEAP_QUERY = "SELECT * WHERE { ?s ?p ?o } LIMIT 1"
DEFAULT_POLL_SECONDS = 2


def _send(base, method, path, token, body=None):
    """(status, headers, decoded body, seconds). status is None on a transport error."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-Source", "load-probe")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status, headers, text = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        status, headers, text = e.code, dict(e.headers), e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return None, {}, None, time.monotonic() - start
    try:
        decoded = json.loads(text)
    except ValueError:
        decoded = None
    return status, headers, decoded, time.monotonic() - start


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self.statuses = {"submit": Counter(), "status": Counter()}
        self.latencies = {"submit": [], "status": []}
        self.jobs = Counter()
        self.job_seconds = []

    def record(self, kind, status, seconds):
        with self._lock:
            self.statuses[kind][status] += 1
            self.latencies[kind].append(seconds)

    def finish_job(self, outcome, seconds):
        with self._lock:
            self.jobs[outcome] += 1
            if outcome == "complete":
                self.job_seconds.append(seconds)


def _retry_after(headers, default):
    value = {k.lower(): v for k, v in headers.items()}.get("retry-after")
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _user(base, token, deadline, stats):
    # Spread the users' first submits over one poll interval
    time.sleep(random.uniform(0, DEFAULT_POLL_SECONDS))
    while time.monotonic() < deadline:
        status, headers, body, seconds = _send(
            base, "POST", "/query", token, {"query": CHEAP_QUERY}
        )
        stats.record("submit", status, seconds)
        if status != 202:
            time.sleep(_retry_after(headers, DEFAULT_POLL_SECONDS))
            continue

        job_id, started = body["job_id"], time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(_retry_after(headers, DEFAULT_POLL_SECONDS))
            status, headers, body, seconds = _send(
                base, "GET", f"/query/{job_id}", token
            )
            stats.record("status", status, seconds)
            if status == 200 and body["status"] in ("complete", "error"):
                stats.finish_job(body["status"], time.monotonic() - started)
                break


def _summary(values):
    if not values:
        return "n/a"
    ordered = sorted(values)
    p95 = ordered[int(0.95 * (len(ordered) - 1))]
    return f"p50 {statistics.median(ordered) * 1000:.0f} ms, p95 {p95 * 1000:.0f} ms"


def run_load(args) -> int:
    stats = Stats()
    deadline = time.monotonic() + args.duration
    print(f"load: {args.users} users for {args.duration} s against {args.base}")
    with ThreadPoolExecutor(max_workers=args.users) as pool:
        for _ in range(args.users):
            pool.submit(_user, args.base, args.token, deadline, stats)

    for kind in ("submit", "status"):
        total = sum(stats.statuses[kind].values())
        rate = total / args.duration
        print(
            f"{kind:<7} {total} requests ({rate:.1f}/s), {_summary(stats.latencies[kind])},"
            f" statuses {dict(stats.statuses[kind])}"
        )
    print(f"jobs    {dict(stats.jobs)}, end-to-end {_summary(stats.job_seconds)}")

    status_429 = stats.statuses["status"][429]
    print(
        f"{'PASS' if status_429 == 0 else 'FAIL'}: {status_429} 429s on GET /query/{{job_id}}"
    )
    return 0 if status_429 == 0 else 1


def run_burst(args) -> int:
    barrier = threading.Barrier(args.requests)

    def one(_):
        barrier.wait()
        return _send(args.base, "POST", "/query", args.token, {"query": ""})[0]

    print(f"burst: {args.requests} concurrent POST /query against {args.base}")
    with ThreadPoolExecutor(max_workers=args.requests) as pool:
        statuses = Counter(pool.map(one, range(args.requests)))

    through = sum(n for status, n in statuses.items() if status not in (429, None))
    print(f"statuses {dict(statuses)}; {through} through, {statuses[429]} throttled")
    # The bucket holds method_burst (100) and refills at 50/s while the burst is in flight
    ok = statuses[429] > 0 and through <= args.expect_through
    print(
        f"{'PASS' if ok else 'FAIL'}: expected some 429s and at most {args.expect_through} through"
    )
    return 0 if ok else 1


def _parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    load = sub.add_parser("load", help="design-target load probe")
    load.add_argument("base", help="base URL, stage included")
    load.add_argument("--users", type=int, default=100)
    load.add_argument("--duration", type=int, default=600, help="seconds")

    burst = sub.add_parser("burst", help="throttle burst probe")
    burst.add_argument("base", help="base URL, stage included")
    burst.add_argument("--requests", type=int, default=300)
    burst.add_argument(
        "--expect-through",
        type=int,
        default=150,
        help="most non-429 responses that still count as throttled correctly",
    )

    for p in (load, burst):
        p.add_argument("--token", default=os.environ.get("SYNAPSE_AUTH_TOKEN"))
    args = parser.parse_args()
    if not args.token:
        parser.error(
            "set SYNAPSE_AUTH_TOKEN or pass --token (Synapse PAT of a team member)"
        )
    return args


if __name__ == "__main__":
    args = _parse_args()
    sys.exit(run_load(args) if args.mode == "load" else run_burst(args))
