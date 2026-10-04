"""
Throttle test for the Neptune Query API.

Sends a burst of concurrent requests to verify API Gateway throttling
(burst_limit=100, rate_limit=50 RPS) is working as configured, then
polls all accepted jobs and reports results.

Usage:
    python tools/throttle_test.py --url <ApiUrl>
    python tools/throttle_test.py  # uses default prod URL
    python tools/throttle_test.py --show-results 5  # print first N results

Phases:
    1. Burst:    300 simultaneous requests — expect ~100 accepted, ~200 throttled
    2. Sustained: 200 RPS for 10s         — expect ~50/s accepted, rest throttled
"""

import asyncio
import argparse
import json
import time
from collections import Counter

import aiohttp

API_URL = "https://ewdwpljfla.execute-api.us-east-1.amazonaws.com/prod/query"
TEST_QUERY = json.dumps({"query": "SELECT * WHERE { ?s ?p ?o } LIMIT 1"})
HEADERS = {"Content-Type": "application/json"}

POLL_INTERVAL = 1.0  # seconds between status polls per job
POLL_TIMEOUT = 60.0  # max seconds to wait for a job to complete
POLL_CONCURRENCY = 50  # max simultaneous status requests


async def submit(session: aiohttp.ClientSession, url: str) -> tuple[int, str | None]:
    """POST a query. Returns (http_status, job_id_or_None)."""
    try:
        async with session.post(url, data=TEST_QUERY, headers=HEADERS) as resp:
            if resp.status == 202:
                body = await resp.json()
                return resp.status, body.get("job_id")
            return resp.status, None
    except Exception:
        return 0, None


async def poll_job(
    session: aiohttp.ClientSession, status_url: str, job_id: str, sem: asyncio.Semaphore
) -> dict:
    """Poll GET /query/{job_id} until complete/error or timeout. Returns the final job dict."""
    url = f"{status_url}/{job_id}"
    deadline = time.perf_counter() + POLL_TIMEOUT
    while time.perf_counter() < deadline:
        async with sem:
            try:
                async with session.get(url) as resp:
                    body = await resp.json()
            except Exception:
                await asyncio.sleep(POLL_INTERVAL)
                continue
        status = body.get("status")
        if status in ("complete", "error"):
            return body
        await asyncio.sleep(POLL_INTERVAL)
    return {"status": "timeout", "job_id": job_id}


async def poll_all(status_url: str, job_ids: list[str]) -> list[dict]:
    """Poll all jobs concurrently, capped at POLL_CONCURRENCY simultaneous requests."""
    sem = asyncio.Semaphore(POLL_CONCURRENCY)
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[poll_job(session, status_url, jid, sem) for jid in job_ids]
        )
    return results


def _print_counts(counts: Counter):
    total = sum(counts.values())
    for status in sorted(counts):
        label = {202: "accepted", 429: "throttled", 400: "bad request", 0: "error"}.get(
            status, "other"
        )
        pct = counts[status] / total * 100
        bar = "#" * int(pct / 2)
        print(f"  {status} {label:12s} {counts[status]:4d} ({pct:5.1f}%) {bar}")


def _print_results(results: list[dict], show_n: int):
    outcomes = Counter(r["status"] for r in results)
    print(f"\n  Job outcomes: {dict(outcomes)}")

    complete = [r for r in results if r["status"] == "complete"]
    if complete and show_n > 0:
        print(
            f"\n  Sample results (first {min(show_n, len(complete))} of {len(complete)} completed jobs):"
        )
        for r in complete[:show_n]:
            raw = r.get("results", "")
            try:
                parsed = json.loads(raw)
                bindings = parsed.get("results", {}).get("bindings", [])
                print(
                    f"    job {r.get('job_id', '?')[:8]}... → {len(bindings)} row(s): {bindings[:2]}"
                )
            except Exception:
                print(f"    job {r.get('job_id', '?')[:8]}... → {str(raw)[:120]}")

    errors = [r for r in results if r["status"] == "error"]
    if errors:
        print(f"\n  Errors ({len(errors)}):")
        for r in errors[:3]:
            print(
                f"    job {r.get('job_id', '?')[:8]}... → {r.get('error', '?')[:120]}"
            )


async def run_phase(label: str, url: str, count: int, rps: int | None, show_n: int):
    """Submit requests (burst if rps=None, rate-limited otherwise), then poll results."""
    status_url = url  # GET /query/{job_id} — same base path

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=0)
    ) as session:
        t0 = time.perf_counter()

        if rps is None:
            # Burst: fire all at once
            pairs = await asyncio.gather(*[submit(session, url) for _ in range(count)])
        else:
            # Sustained: throttle submission rate
            tasks = []
            for i in range(count):
                tasks.append(asyncio.create_task(submit(session, url)))
                if (i + 1) % rps == 0:
                    elapsed = time.perf_counter() - t0
                    target = (i + 1) / rps
                    if target > elapsed:
                        await asyncio.sleep(target - elapsed)
            pairs = await asyncio.gather(*tasks)

        elapsed = time.perf_counter() - t0

    statuses = Counter(s for s, _ in pairs)
    job_ids = [jid for _, jid in pairs if jid]

    actual_rps = count / elapsed
    print(f"Submitted {count} requests in {elapsed:.2f}s ({actual_rps:.1f} actual RPS)")
    _print_counts(statuses)

    if not job_ids:
        print("  No jobs accepted.")
        return

    print(f"\n  Polling {len(job_ids)} accepted jobs (timeout={POLL_TIMEOUT}s)...")
    poll_t0 = time.perf_counter()
    results = await poll_all(status_url, job_ids)
    poll_elapsed = time.perf_counter() - poll_t0
    print(f"  Polling completed in {poll_elapsed:.2f}s")

    _print_results(results, show_n)


async def main(url: str, show_n: int):
    print(f"Target: {url}")
    print("Throttle config: burst_limit=100, rate_limit=50 RPS\n")

    print("--- Phase 1: Burst (300 simultaneous requests) ---")
    print("Expected: ~100 accepted, ~200 throttled (429)")
    await run_phase("burst", url, count=300, rps=None, show_n=show_n)

    print("\nWaiting 3s for token bucket to refill...")
    await asyncio.sleep(3)

    print("\n--- Phase 2: Sustained load (200 RPS for 10s = 2000 requests) ---")
    print("Expected: ~500 accepted, ~1500 throttled (429)")
    await run_phase("sustained", url, count=2000, rps=200, show_n=show_n)

    print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url", default=API_URL, help="API Gateway base URL for POST /query"
    )
    parser.add_argument(
        "--show-results",
        type=int,
        default=3,
        metavar="N",
        help="Print first N completed query results (default: 3, 0 to disable)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.url, args.show_results))
