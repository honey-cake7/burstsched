"""
phase_changer.py — Workload that switches between CPU-bound and
                    interactive I/O phases.

This is the key workload for testing prediction under behavioural
change, as described in the proposal: "a compile job that becomes
interactive, a browser tab that goes idle and then bursts."

Phase schedule (configurable):
  Phase 1 (0–T1):      CPU-bound — long bursts, minimal sleep.
  Phase 2 (T1–T2):     Interactive — short bursts, long sleeps.
  Phase 3 (T2–T3):     CPU-bound again.
  Phase 4 (T3–end):    Mixed — alternating burst patterns.

Modes:
  --mock     Produce simulated burst/sleep data (works on any OS).
  --live     Do real CPU work / sleeps and report timing.

Usage:
  python workloads/phase_changer.py --mock --ticks 400
  python workloads/phase_changer.py --live --duration 60
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from dataclasses import dataclass
from typing import List, Tuple


# ------------------------------------------------------------------ #
#  Phase definitions                                                  #
# ------------------------------------------------------------------ #

@dataclass
class Phase:
    """Describes a workload phase."""
    name: str
    burst_mean_ns: int
    burst_std_ns: int
    sleep_mean_ns: int
    sleep_std_ns: int


DEFAULT_PHASES: List[Tuple[float, Phase]] = [
    # (fraction_of_total_duration, Phase)
    (0.25, Phase("cpu_bound",    18_000_000, 3_000_000,   200_000,  100_000)),
    (0.25, Phase("interactive",     400_000,   100_000, 60_000_000, 10_000_000)),
    (0.25, Phase("cpu_bound_2",  20_000_000, 4_000_000,   150_000,   80_000)),
    (0.25, Phase("mixed",         5_000_000, 8_000_000, 10_000_000, 15_000_000)),
]


def get_phase(tick: int, total_ticks: int,
              phases: List[Tuple[float, Phase]] | None = None) -> Phase:
    """Return the active phase for the given tick."""
    phases = phases or DEFAULT_PHASES
    cumulative = 0.0
    frac = tick / total_ticks

    for phase_frac, phase in phases:
        cumulative += phase_frac
        if frac < cumulative:
            return phase

    return phases[-1][1]


# ------------------------------------------------------------------ #
#  Mock mode                                                          #
# ------------------------------------------------------------------ #

def run_mock(total_ticks: int = 400, seed: int = 42) -> dict:
    """Generate simulated phase-change burst data."""
    rng = random.Random(seed)
    results: List[dict] = []
    phase_transitions: List[dict] = []
    current_phase_name = ""

    print(f"[phase_changer] MOCK mode — {total_ticks} ticks across "
          f"{len(DEFAULT_PHASES)} phases")

    for tick in range(total_ticks):
        phase = get_phase(tick, total_ticks)

        if phase.name != current_phase_name:
            current_phase_name = phase.name
            phase_transitions.append({"tick": tick, "phase": phase.name})
            print(f"  [tick {tick:4d}] → phase: {phase.name}")

        burst_ns = max(0, int(rng.gauss(phase.burst_mean_ns, phase.burst_std_ns)))
        sleep_ns = max(0, int(rng.gauss(phase.sleep_mean_ns, phase.sleep_std_ns)))

        results.append({
            "tick": tick,
            "phase": phase.name,
            "burst_ns": burst_ns,
            "sleep_ns": sleep_ns,
        })

    # Compute per-phase stats.
    phase_stats = {}
    for _, phase in DEFAULT_PHASES:
        phase_bursts = [r["burst_ns"] for r in results if r["phase"] == phase.name]
        if phase_bursts:
            phase_stats[phase.name] = {
                "count": len(phase_bursts),
                "mean_burst_ms": sum(phase_bursts) / len(phase_bursts) / 1e6,
                "min_burst_ms": min(phase_bursts) / 1e6,
                "max_burst_ms": max(phase_bursts) / 1e6,
            }

    return {
        "total_ticks": total_ticks,
        "phase_transitions": phase_transitions,
        "phase_stats": phase_stats,
        "events": results,
    }


# ------------------------------------------------------------------ #
#  Live mode                                                          #
# ------------------------------------------------------------------ #

def run_live(duration_s: float = 60.0) -> dict:
    """
    Run real CPU work / sleep cycles following the phase schedule.
    Useful on Linux to generate actual scheduler events.
    """
    start = time.monotonic()
    total_ticks = int(duration_s * 100)  # ~100 ticks/s
    results: List[dict] = []
    tick = 0
    current_phase_name = ""

    print(f"[phase_changer] LIVE mode — {duration_s}s, ~{total_ticks} ticks")
    rng = random.Random(42)

    while (time.monotonic() - start) < duration_s and tick < total_ticks:
        phase = get_phase(tick, total_ticks)

        if phase.name != current_phase_name:
            current_phase_name = phase.name
            elapsed = time.monotonic() - start
            print(f"  [{elapsed:.1f}s] → phase: {phase.name}")

        # Simulate CPU burst.
        burst_target_ns = max(0, int(rng.gauss(phase.burst_mean_ns, phase.burst_std_ns)))
        t0 = time.perf_counter_ns()
        # Busy-wait for burst_target_ns.
        count = 0
        while (time.perf_counter_ns() - t0) < burst_target_ns:
            count += 1
            _ = math.sqrt(count * 1.1)
        actual_burst_ns = time.perf_counter_ns() - t0

        # Sleep for sleep phase.
        sleep_target_ns = max(0, int(rng.gauss(phase.sleep_mean_ns, phase.sleep_std_ns)))
        time.sleep(max(0, sleep_target_ns / 1e9))

        results.append({
            "tick": tick,
            "phase": phase.name,
            "burst_ns": actual_burst_ns,
            "sleep_ns": sleep_target_ns,
        })

        tick += 1

    return {
        "total_ticks": tick,
        "events": results,
    }


# ------------------------------------------------------------------ #
#  CLI                                                                #
# ------------------------------------------------------------------ #

def main() -> None:
    p = argparse.ArgumentParser(
        description="Phase-changing workload for BurstSched evaluation."
    )
    p.add_argument("--mock", action="store_true",
                   help="Generate simulated phase-change data")
    p.add_argument("--live", action="store_true",
                   help="Run real CPU work / sleeps")
    p.add_argument("--ticks", type=int, default=400,
                   help="Total ticks for mock mode (default: 400)")
    p.add_argument("--duration", type=float, default=60.0,
                   help="Duration in seconds for live mode (default: 60)")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    if args.mock:
        result = run_mock(args.ticks, args.seed)
        print("\n--- Phase Changer Summary ---")
        for pname, pstats in result["phase_stats"].items():
            print(f"  {pname}: {pstats['count']} bursts, "
                  f"mean={pstats['mean_burst_ms']:.2f}ms, "
                  f"range=[{pstats['min_burst_ms']:.2f}, "
                  f"{pstats['max_burst_ms']:.2f}]ms")
    elif args.live:
        result = run_live(args.duration)
        print(f"\n--- Done: {result['total_ticks']} ticks ---")
    else:
        print("Specify --mock or --live", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
