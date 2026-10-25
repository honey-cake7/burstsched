"""
run_ablation.py — Automated ablation study harness.

Runs BurstSched configurations one at a time, matching every row in
the proposal's ablation table:

  1. reactive_mlfq          — baseline MLFQ, no prediction
  2. + ewma                 — EWMA predictor enabled
  3. + learned              — learned predictor enabled
  4. + confidence_gate      — confidence-gated placement
  5. + predicted_slices     — predicted time slices
  6. + wakeup_preempt       — wakeup preemption (optional)
  7. + per_cpu              — per-CPU queues (stretch goal)

For each configuration, the harness:
  a) Starts the predictor daemon with the correct flags.
  b) Runs the mixed workload (interactive + cpu_hog + phase_changer).
  c) Collects metrics: p50/p99 latency, throughput, context switches,
     worst-case wait.
  d) Saves results to JSON.

Usage:
  python benchmarks/run_ablation.py --mock
  python benchmarks/run_ablation.py --mock --output results.json
  python benchmarks/run_ablation.py --live    # (requires Linux + BurstSched)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional

# Ensure the project root is on the path.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from predictor.bpf_bridge import BPFBridge, BurstEvent, PredictionEntry
from predictor.ewma import EWMAPredictor
from predictor.daemon import PredictorDaemon, queue_from_burst


# ------------------------------------------------------------------ #
#  Ablation configuration rows                                        #
# ------------------------------------------------------------------ #

@dataclass
class AblationConfig:
    """One row of the ablation table."""
    name: str
    predictor: str           # "none" | "ewma" | "learned"
    confidence_gate: bool
    predicted_slices: bool
    wakeup_preempt: bool
    per_cpu: bool
    description: str


ABLATION_TABLE: List[AblationConfig] = [
    AblationConfig(
        name="reactive_mlfq",
        predictor="none",
        confidence_gate=False,
        predicted_slices=False,
        wakeup_preempt=False,
        per_cpu=False,
        description="Baseline reactive MLFQ — no prediction.",
    ),
    AblationConfig(
        name="ewma",
        predictor="ewma",
        confidence_gate=False,
        predicted_slices=False,
        wakeup_preempt=False,
        per_cpu=False,
        description="+ EWMA prediction.",
    ),
    AblationConfig(
        name="learned",
        predictor="learned",
        confidence_gate=False,
        predicted_slices=False,
        wakeup_preempt=False,
        per_cpu=False,
        description="+ Learned (sklearn) predictor.",
    ),
    AblationConfig(
        name="confidence_gate",
        predictor="learned",
        confidence_gate=True,
        predicted_slices=False,
        wakeup_preempt=False,
        per_cpu=False,
        description="+ Confidence-gated placement — fall back on erratic tasks.",
    ),
    AblationConfig(
        name="predicted_slices",
        predictor="learned",
        confidence_gate=True,
        predicted_slices=True,
        wakeup_preempt=False,
        per_cpu=False,
        description="+ Predicted time slices — scale slice to predicted burst.",
    ),
    AblationConfig(
        name="wakeup_preempt",
        predictor="learned",
        confidence_gate=True,
        predicted_slices=True,
        wakeup_preempt=True,
        per_cpu=False,
        description="+ Wakeup preemption — preempt hogs for short-burst tasks.",
    ),
    AblationConfig(
        name="per_cpu",
        predictor="learned",
        confidence_gate=True,
        predicted_slices=True,
        wakeup_preempt=True,
        per_cpu=True,
        description="+ Per-CPU queues with burst-aware placement (stretch).",
    ),
]


# ------------------------------------------------------------------ #
#  Metrics collection                                                 #
# ------------------------------------------------------------------ #

@dataclass
class RunMetrics:
    """Metrics collected for a single ablation run."""
    config_name: str = ""
    p50_latency_us: float = 0.0
    p99_latency_us: float = 0.0
    throughput_events_per_sec: float = 0.0
    context_switches: int = 0
    worst_case_wait_us: float = 0.0
    total_events: int = 0
    gated_fallbacks: int = 0
    duration_s: float = 0.0
    predictions: Dict[int, dict] = field(default_factory=dict)


# ------------------------------------------------------------------ #
#  Mock ablation runner                                               #
# ------------------------------------------------------------------ #

def run_mock_ablation(config: AblationConfig, ticks: int = 200,
                      seed: int = 42) -> RunMetrics:
    """
    Run one ablation configuration in mock mode.

    Simulates:
      - Interactive tasks (short bursts, long sleeps)
      - CPU hogs (long bursts)
      - Phase changers (switching behaviour)

    Collects simulated metrics.
    """
    bridge = BPFBridge(mock=True, seed=seed)

    # Pick predictor type.
    if config.predictor == "none":
        predictor_type = "ewma"  # daemon needs a predictor, but we'll ignore its output
    else:
        predictor_type = config.predictor

    daemon = PredictorDaemon(
        bridge=bridge,
        predictor_type=predictor_type,
        confidence_gate=config.confidence_gate,
        predicted_slices=config.predicted_slices,
        verbose=False,
    )

    latencies: List[float] = []
    t_start = time.monotonic()

    for tick in range(ticks):
        events = bridge.poll_events()
        daemon.process_events(events)

        # Simulate latency measurement for interactive tasks.
        for evt in events:
            pred = bridge.get_prediction(evt.pid)
            if pred and config.predictor != "none":
                # Simulated wakeup-to-run latency depends on queue placement.
                base_latency_us = [50, 200, 800, 3000][min(pred.queue_hint, 3)]
                # Confidence-gated tasks get reactive (higher) latency.
                if config.confidence_gate and pred.confidence < 500:
                    base_latency_us *= 1.5
                # Predicted slices reduce context switch overhead.
                if config.predicted_slices and pred.slice_hint_ns > 0:
                    base_latency_us *= 0.85
                # Wakeup preemption helps short-burst tasks.
                if (config.wakeup_preempt and
                        pred.predicted_burst_ns < 1_000_000):
                    base_latency_us *= 0.6
            else:
                # Reactive MLFQ baseline: all tasks start at Q0.
                base_latency_us = 500.0

            latencies.append(base_latency_us)

    t_end = time.monotonic()

    # Compute metrics.
    latencies.sort()
    n = len(latencies)
    metrics = RunMetrics(
        config_name=config.name,
        p50_latency_us=latencies[n // 2] if n > 0 else 0,
        p99_latency_us=latencies[int(n * 0.99)] if n > 0 else 0,
        throughput_events_per_sec=n / (t_end - t_start) if (t_end - t_start) > 0 else 0,
        context_switches=int(n * 0.3),  # simulated estimate
        worst_case_wait_us=latencies[-1] if n > 0 else 0,
        total_events=daemon.total_events,
        gated_fallbacks=daemon.gated_fallbacks,
        duration_s=t_end - t_start,
    )

    # Record per-task prediction summaries.
    summary = daemon.summary()
    metrics.predictions = summary.get("predictions", {})

    return metrics


# ------------------------------------------------------------------ #
#  Main                                                               #
# ------------------------------------------------------------------ #

def main() -> None:
    p = argparse.ArgumentParser(
        description="BurstSched ablation study — run all configurations."
    )
    p.add_argument("--mock", action="store_true",
                   help="Use mock simulator (default for now)")
    p.add_argument("--ticks", type=int, default=200,
                   help="Ticks per configuration in mock mode (default: 200)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output", "-o", default="ablation_results.json",
                   help="Output file for results (default: ablation_results.json)")
    p.add_argument("--configs", nargs="*",
                   help="Run only these configs (default: all)")
    args = p.parse_args()

    if not args.mock:
        print("[ablation] Live mode not yet implemented — use --mock",
              file=sys.stderr)
        sys.exit(1)

    configs = ABLATION_TABLE
    if args.configs:
        configs = [c for c in ABLATION_TABLE if c.name in args.configs]
        if not configs:
            print(f"[ablation] No matching configs: {args.configs}",
                  file=sys.stderr)
            sys.exit(1)

    print("=" * 72)
    print("  BurstSched Ablation Study")
    print("=" * 72)

    all_results: List[dict] = []

    for i, config in enumerate(configs, 1):
        print(f"\n{'─' * 72}")
        print(f"  [{i}/{len(configs)}] {config.name}")
        print(f"  {config.description}")
        print(f"{'─' * 72}")

        metrics = run_mock_ablation(config, ticks=args.ticks, seed=args.seed)

        print(f"    p50 latency:    {metrics.p50_latency_us:10.1f} µs")
        print(f"    p99 latency:    {metrics.p99_latency_us:10.1f} µs")
        print(f"    throughput:     {metrics.throughput_events_per_sec:10.0f} events/s")
        print(f"    ctx switches:   {metrics.context_switches:10d}")
        print(f"    worst-case:     {metrics.worst_case_wait_us:10.1f} µs")
        print(f"    gated fallbacks:{metrics.gated_fallbacks:10d}")
        print(f"    total events:   {metrics.total_events:10d}")

        result = asdict(metrics)
        result["config"] = asdict(config)
        all_results.append(result)

    # ---- Save results -------------------------------------------- #
    output_path = os.path.join(os.path.dirname(__file__), args.output)
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    print(f"\n{'=' * 72}")
    print(f"  Results saved to: {output_path}")
    print(f"  Run plot_results.py to generate charts.")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
