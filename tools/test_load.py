#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from pathlib import Path

import httpx

from send_sample_batch import TASK_CATEGORIES, envelope_for, make_batch


async def post_batch(client: httpx.AsyncClient, server: str, device_id: str, category: str, index: int) -> float:
    session_id = f"load-{device_id}-{category}"
    task_started_at = int(time.time() * 1000)
    batch = make_batch(device_id, category, index, session_id, session_id, task_started_at)
    started = time.perf_counter()
    response = await client.post(server.rstrip("/") + "/api/v1/ingest", json=envelope_for(batch), timeout=10)
    response.raise_for_status()
    return (time.perf_counter() - started) * 1000


async def run(args: argparse.Namespace) -> dict[str, object]:
    latencies: list[float] = []
    errors = 0
    async with httpx.AsyncClient() as client:
        for tick in range(args.iterations):
            tasks = []
            for device_index in range(args.devices):
                device_id = f"{device_index:064x}"[-64:]
                category = random.choice(TASK_CATEGORIES)
                tasks.append(post_batch(client, args.server, device_id, category, tick))
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    errors += 1
                else:
                    latencies.append(result)
            if tick != args.iterations - 1:
                await asyncio.sleep(args.interval)
    p95 = statistics.quantiles(latencies, n=20)[-1] if len(latencies) >= 20 else max(latencies, default=0)
    return {
        "requests": len(latencies) + errors,
        "success": len(latencies),
        "errors": errors,
        "p95_ms": p95,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--devices", type=int, default=50)
    parser.add_argument("--iterations", type=int, default=60)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--output", type=Path, default=Path("tools/load_test_result.json"))
    args = parser.parse_args()
    result = asyncio.run(run(args))
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
