#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Result:
    status: int
    elapsed_ms: float


async def one(client: httpx.AsyncClient, url: str, semaphore: asyncio.Semaphore) -> Result:
    async with semaphore:
        started = time.perf_counter()
        response = await client.get(url, follow_redirects=False)
        return Result(response.status_code, (time.perf_counter() - started) * 1000)


async def run(args: argparse.Namespace) -> int:
    semaphore = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )
    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        results = await asyncio.gather(
            *(one(client, args.url, semaphore) for _ in range(args.requests)),
            return_exceptions=True,
        )
    successes = [item for item in results if isinstance(item, Result) and item.status < 500]
    failures = [item for item in results if not isinstance(item, Result) or item.status >= 500]
    latencies = sorted(item.elapsed_ms for item in successes)
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0
    print(f"requests={len(results)} successes={len(successes)} failures={len(failures)}")
    if latencies:
        print(
            f"latency_ms median={statistics.median(latencies):.2f} "
            f"p95={p95:.2f} max={max(latencies):.2f}"
        )
    return 1 if failures or p95 > args.max_p95_ms else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded HTTP load smoke test.")
    parser.add_argument("url", help="A fake/staging URL; never print bearer tokens in reports.")
    parser.add_argument("--requests", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--max-p95-ms", type=float, default=500)
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        parser.error("requests and concurrency must be positive")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
