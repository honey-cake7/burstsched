"""
cpu_hog.py — Batch CPU-bound load generator.

Creates one or more CPU-bound worker threads/processes that consume
full time slices, simulating batch workloads competing with interactive
tasks.

Modes:
  --stress-ng   Use stress-ng for load generation (Linux only).
  --pure-python Pure Python CPU burn (works on any OS, default).
  --mock        Simulated CPU-bound burst data generation for mock mode.

Usage:
  python workloads/cpu_hog.py --workers 4 --duration 30
  python workloads/cpu_hog.py --mock --ticks 200
"""

from __future__ import annotations

import argparse
import math
import os
import random
import subprocess
import sys
import threading
import time
from typing import List


# ------------------------------------------------------------------ #
#  Pure Python CPU burn                                               #
# ------------------------------------------------------------------ #

def cpu_burn(duration_s: float, worker_id: int = 0) -> dict:
    """
    Burn CPU for *duration_s* seconds doing real computation.
    Returns throughput stats.
    """
    start = time.monotonic()
    iterations = 0

    while (time.monotonic() - start) < duration_s:
        # Do meaningful work: compute a batch of square roots.
        total = 0.0
        for i in range(10_000):
            total += math.sqrt(i * 3.14159)
        iterations += 1

    elapsed = time.monotonic() - start
    return {
        "worker_id": worker_id,
        "iterations": iterations,
        "elapsed_s": elapsed,
        "throughput": iterations / elapsed if elapsed > 0 else 0,
    }


def run_pure_python(workers: int, duration_s: float) -> List[dict]:
    """Launch *workers* CPU-burn threads and return their stats."""
    results: List[dict] = [{}] * workers
    threads: List[threading.Thread] = []

    print(f"[cpu_hog] pure-python mode: {workers} workers × {duration_s}s")

    def worker_fn(wid: int):
        results[wid] = cpu_burn(duration_s, wid)

    for i in range(workers):
        t = threading.Thread(target=worker_fn, args=(i,))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    return results


# ------------------------------------------------------------------ #
#  stress-ng wrapper (Linux)                                          #
# ------------------------------------------------------------------ #

def run_stress_ng(workers: int, duration_s: float) -> dict:
    """Wrap stress-ng for CPU load generation."""
    cmd = [
        "stress-ng",
        "--cpu", str(workers),
        "--timeout", f"{int(duration_s)}s",
        "--metrics-brief",
    ]
    print(f"[cpu_hog] stress-ng mode: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=duration_s + 10)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except FileNotFoundError:
        print("[cpu_hog] stress-ng not found — install it or use --pure-python",
              file=sys.stderr)
        sys.exit(1)


# ------------------------------------------------------------------ #
#  Mock mode: simulated CPU-bound burst data                          #
# ------------------------------------------------------------------ #

def run_mock(ticks: int = 200, workers: int = 4, seed: int = 42) -> dict:
    """
    Generate simulated CPU-hog burst data for mock mode.
    Each worker produces a long burst (~15-25 ms) per tick.
    """
    rng = random.Random(seed)
    results = {"workers": {}}

    print(f"[cpu_hog] MOCK mode: {workers} workers × {ticks} ticks")

    for wid in range(workers):
        bursts = []
        for _ in range(ticks):
            burst_ns = max(5_000_000, int(rng.gauss(20_000_000, 3_000_000)))
            bursts.append(burst_ns)
        mean = sum(bursts) / len(bursts)
        results["workers"][wid] = {
            "n_bursts": len(bursts),
            "mean_burst_ms": mean / 1e6,
            "total_cpu_ms": sum(bursts) / 1e6,
        }

    return results


# ------------------------------------------------------------------ #
#  CLI                                                                #
# ------------------------------------------------------------------ #

def main() -> None:
    p = argparse.ArgumentParser(
        description="CPU-bound batch load generator for BurstSched."
    )
    p.add_argument("--pure-python", action="store_true", default=True,
                   help="Use pure Python CPU burn (default)")
    p.add_argument("--stress-ng", action="store_true",
                   help="Use stress-ng (Linux only)")
    p.add_argument("--mock", action="store_true",
                   help="Simulate CPU-bound burst data")
    p.add_argument("--workers", type=int, default=4,
                   help="Number of CPU-bound workers (default: 4)")
    p.add_argument("--duration", type=float, default=10.0,
                   help="Duration in seconds (default: 10)")
    p.add_argument("--ticks", type=int, default=200,
                   help="Number of simulated ticks for mock mode")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    if args.mock:
        result = run_mock(args.ticks, args.workers, args.seed)
        print("\n--- Mock CPU Hog Summary ---")
        for wid, info in result["workers"].items():
            print(f"  Worker {wid}: {info['n_bursts']} bursts, "
                  f"mean={info['mean_burst_ms']:.2f}ms, "
                  f"total={info['total_cpu_ms']:.1f}ms")
    elif args.stress_ng:
        result = run_stress_ng(args.workers, args.duration)
        print(result.get("stdout", ""))
        if result.get("stderr"):
            print(result["stderr"], file=sys.stderr)
    else:
        results = run_pure_python(args.workers, args.duration)
        print("\n--- CPU Hog Summary ---")
        total_throughput = 0
        for r in results:
            print(f"  Worker {r['worker_id']}: {r['iterations']} iters "
                  f"in {r['elapsed_s']:.2f}s "
                  f"({r['throughput']:.1f} iters/s)")
            total_throughput += r['throughput']
        print(f"  Total throughput: {total_throughput:.1f} iters/s")


if __name__ == "__main__":
    main()
