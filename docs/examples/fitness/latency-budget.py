"""Replace exercise() with the operation covered by your latency budget."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from math import ceil, isfinite
from time import perf_counter_ns


def exercise():
    # Use representative inputs and raise on failed requests.
    sha256(b"example workload").digest()


def sample():
    start = perf_counter_ns()
    try:
        exercise()
    except Exception:
        return None
    return (perf_counter_ns() - start) / 1_000_000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--p95-ms", type=float, required=True)
    args = parser.parse_args()
    if (args.requests < max(1, args.concurrency) or args.concurrency < 1 or args.warmup < 0
            or not isfinite(args.p95_ms) or args.p95_ms <= 0):
        parser.error("use positive requests, concurrency and p95 budget, with nonnegative warmup")

    def batch(worker):
        return [sample() for _ in range(worker, args.requests, args.concurrency)]

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        warmup = list(pool.map(lambda _: sample(), range(args.warmup)))
        if any(result is None for result in warmup):
            print(json.dumps({"status": "warmup-failed"}))
            return 1
        start = perf_counter_ns()
        results = [duration for batch_result in pool.map(batch, range(args.concurrency)) for duration in batch_result]
        elapsed = (perf_counter_ns() - start) / 1_000_000_000
    durations = sorted(duration for duration in results if duration is not None)
    p95 = durations[ceil(0.95 * len(durations)) - 1] if durations else None
    errors = len(results) - len(durations)
    print(json.dumps({"samples": len(results), "errors": errors, "requests_per_second": len(results) / elapsed,
                      "p95_ms": p95, "budget_p95_ms": args.p95_ms}))
    return int(errors > 0 or p95 is None or p95 > args.p95_ms)


if __name__ == "__main__":
    raise SystemExit(main())
