"""
interactive_server.py — Socket/HTTP request-response service for
                         measuring interactive (wakeup-to-run) latency.

Runs a simple TCP echo server that accepts connections, receives a
request, does a small amount of work (simulating an interactive burst),
and responds.  Records p50/p99 wakeup-to-run latency for each request.

Modes:
  --live     Run a real TCP server bound to --host/--port.
  --mock     Simulate request/response cycles locally (no sockets).

The --mock mode is useful for testing the benchmark harness on any OS.

Usage:
  python workloads/interactive_server.py --mock --requests 1000
  python workloads/interactive_server.py --live --port 9999
"""

from __future__ import annotations

import argparse
import math
import random
import socket
import statistics
import sys
import time
import threading
from typing import List


# ------------------------------------------------------------------ #
#  Latency recording                                                  #
# ------------------------------------------------------------------ #

class LatencyRecorder:
    """Collects latency samples and computes percentiles."""

    def __init__(self):
        self.samples: List[float] = []

    def record(self, latency_us: float) -> None:
        self.samples.append(latency_us)

    def p50(self) -> float:
        if not self.samples:
            return 0.0
        return self._percentile(50)

    def p99(self) -> float:
        if not self.samples:
            return 0.0
        return self._percentile(99)

    def mean(self) -> float:
        return statistics.mean(self.samples) if self.samples else 0.0

    def _percentile(self, pct: float) -> float:
        s = sorted(self.samples)
        idx = int(math.ceil(pct / 100.0 * len(s))) - 1
        return s[max(0, idx)]

    def summary(self) -> dict:
        return {
            "count": len(self.samples),
            "p50_us": self.p50(),
            "p99_us": self.p99(),
            "mean_us": self.mean(),
            "min_us": min(self.samples) if self.samples else 0,
            "max_us": max(self.samples) if self.samples else 0,
        }


# ------------------------------------------------------------------ #
#  Mock mode: simulated request/response                              #
# ------------------------------------------------------------------ #

def run_mock(num_requests: int, burst_mean_us: float = 200.0,
             burst_std_us: float = 80.0, seed: int = 42) -> dict:
    """
    Simulate request-response cycles and record latency.

    Each "request" simulates:
      1. Wakeup delay   (sleep → run transition, ~10–500 µs)
      2. Processing     (CPU burst, ~100–500 µs)
      3. Response

    Returns a latency summary dict.
    """
    rng = random.Random(seed)
    recorder = LatencyRecorder()

    print(f"[interactive_server] MOCK mode — simulating {num_requests} requests")

    for i in range(num_requests):
        # Simulate wakeup-to-run latency (scheduler delay).
        wakeup_delay_us = max(1.0, rng.gauss(50.0, 30.0))

        # Simulate processing burst.
        processing_us = max(10.0, rng.gauss(burst_mean_us, burst_std_us))

        # Total response latency as perceived by client.
        total_us = wakeup_delay_us + processing_us
        recorder.record(total_us)

        if (i + 1) % (num_requests // 10 or 1) == 0:
            print(f"  [{i+1}/{num_requests}] p50={recorder.p50():.1f}µs "
                  f"p99={recorder.p99():.1f}µs")

    return recorder.summary()


# ------------------------------------------------------------------ #
#  Live mode: TCP echo server                                         #
# ------------------------------------------------------------------ #

def run_live_server(host: str, port: int, max_requests: int = 0) -> dict:
    """
    Run a TCP echo server.  Each connection: recv → process → send.
    Records wakeup-to-run latency using high-resolution timestamps.
    """
    recorder = LatencyRecorder()
    count = 0

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(128)
        srv.settimeout(1.0)
        print(f"[interactive_server] LIVE mode — listening on {host}:{port}")

        try:
            while True:
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue

                with conn:
                    t_wakeup = time.perf_counter_ns()
                    data = conn.recv(4096)
                    if not data:
                        continue
                    t_ready = time.perf_counter_ns()

                    # Simulate a small CPU burst.
                    _ = sum(range(500))

                    t_done = time.perf_counter_ns()
                    conn.sendall(data)

                    latency_us = (t_done - t_wakeup) / 1000.0
                    recorder.record(latency_us)
                    count += 1

                    if count % 100 == 0:
                        print(f"  [{count}] p50={recorder.p50():.1f}µs "
                              f"p99={recorder.p99():.1f}µs")

                if max_requests > 0 and count >= max_requests:
                    break
        except KeyboardInterrupt:
            pass

    return recorder.summary()


# ------------------------------------------------------------------ #
#  CLI                                                                #
# ------------------------------------------------------------------ #

def main() -> None:
    p = argparse.ArgumentParser(
        description="Interactive server workload for BurstSched evaluation."
    )
    p.add_argument("--mock", action="store_true",
                   help="Simulate requests locally (no sockets)")
    p.add_argument("--live", action="store_true",
                   help="Run a real TCP echo server")
    p.add_argument("--requests", type=int, default=1000,
                   help="Number of requests (default: 1000)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9999)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    if args.mock:
        summary = run_mock(args.requests, seed=args.seed)
    elif args.live:
        summary = run_live_server(args.host, args.port, args.requests)
    else:
        print("Specify --mock or --live", file=sys.stderr)
        sys.exit(1)

    print("\n--- Latency Summary ---")
    for k, v in summary.items():
        print(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v}")


if __name__ == "__main__":
    main()
