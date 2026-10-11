"""Tiny async load generator for the HPA lab (via port-forward).

    python scripts/load_test.py --seconds 120 --concurrency 40
"""

import argparse
import asyncio
import time
from collections import Counter

import httpx


async def worker(client: httpx.AsyncClient, url: str, until: float, stats: Counter) -> None:
    while time.monotonic() < until:
        try:
            r = await client.get(url)
            stats[r.status_code] += 1
        except httpx.HTTPError:
            stats["error"] += 1


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000/readyz")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--concurrency", type=int, default=40)
    args = ap.parse_args()
    stats: Counter = Counter()
    until = time.monotonic() + args.seconds
    async with httpx.AsyncClient(timeout=10) as client:
        tasks = [
            asyncio.create_task(worker(client, args.url, until, stats)) for _ in range(args.concurrency)
        ]
        while time.monotonic() < until:
            await asyncio.sleep(10)
            left = int(until - time.monotonic())
            print(f"{left:>4}s left  requests={sum(stats.values())}  {dict(stats)}", flush=True)
        await asyncio.gather(*tasks)
    print("done", dict(stats))


if __name__ == "__main__":
    asyncio.run(main())
