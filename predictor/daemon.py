"""
daemon.py — User-space predictor daemon for BurstSched.

Main loop:
  1. Poll burst events from the BPF bridge (live or mock).
  2. Feed each burst to the active predictor (EWMA and/or learned).
  3. Compute queue hint and slice hint from the prediction.
  4. Write the prediction entry back to the BPF map.

Usage:
  python -m predictor.daemon --mock                       # mock simulation
  python -m predictor.daemon --mock --predictor learned    # use learned model
  python -m predictor.daemon --mock --duration 60          # run for 60 seconds
  python -m predictor.daemon                              # live BPF mode (Linux only)

CLI flags:
  --mock           Use mock simulator instead of live BPF.
  --predictor      Which predictor to use: ewma | learned | both (default: ewma).
  --alpha          EWMA smoothing factor (default: 0.5).
  --duration       How many seconds to run (0 = indefinite, default: 30).
  --seed           RNG seed for mock mode (default: 42).
  --confidence     Enable confidence gating (default: on).
  --no-confidence  Disable confidence gating.
  --verbose        Print every prediction to stdout.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from typing import Dict

from .bpf_bridge import BPFBridge, BurstEvent, PredictionEntry
from .ewma import EWMAPredictor

# ------------------------------------------------------------------ #
#  Queue / slice helpers (mirror burstsched_common.h constants)       #
# ------------------------------------------------------------------ #

BURST_THRESH_Q0 =  1_000_000    # < 1 ms  → Q0
BURST_THRESH_Q1 =  4_000_000    # < 4 ms  → Q1
BURST_THRESH_Q2 = 16_000_000    # < 16 ms → Q2

SLICE_SHORT_NS  =  1_000_000    # 1 ms
SLICE_MEDIUM_NS =  4_000_000    # 4 ms
SLICE_LONG_NS   = 16_000_000    # 16 ms
SLICE_MAX_NS    = 32_000_000    # 32 ms

CONFIDENCE_THRESHOLD = 500       # 50% in fixed-point 0–1000


def queue_from_burst(predicted_ns: int) -> int:
    if predicted_ns < BURST_THRESH_Q0:
        return 0
    if predicted_ns < BURST_THRESH_Q1:
        return 1
    if predicted_ns < BURST_THRESH_Q2:
        return 2
    return 3


def slice_for_queue(q: int) -> int:
    return [SLICE_SHORT_NS, SLICE_MEDIUM_NS, SLICE_LONG_NS, SLICE_MAX_NS][
        min(q, 3)
    ]


def compute_predicted_slice(predicted_ns: int, queue: int) -> int:
    """
    Scale the time slice to the predicted burst.  Give the task a slice
    ~20% above its predicted burst so it finishes naturally without
    being preempted, reducing context switches.
    """
    # 1.2× predicted burst, clamped to [SLICE_SHORT, SLICE_MAX]
    scaled = int(predicted_ns * 1.2)
    return max(SLICE_SHORT_NS, min(SLICE_MAX_NS, scaled))


# ------------------------------------------------------------------ #
#  Daemon main loop                                                   #
# ------------------------------------------------------------------ #

class PredictorDaemon:
    """
    Orchestrates the predict → update loop.
    """

    def __init__(self, bridge: BPFBridge, predictor_type: str = "ewma",
                 alpha: float = 0.5, confidence_gate: bool = True,
                 predicted_slices: bool = True, verbose: bool = False):
        self.bridge = bridge
        self.predictor_type = predictor_type
        self.confidence_gate = confidence_gate
        self.predicted_slices = predicted_slices
        self.verbose = verbose

        # Initialise predictors.
        self.ewma = EWMAPredictor(alpha=alpha)
        self.learned = None

        if predictor_type in ("learned", "both"):
            try:
                from .learned_model import LearnedPredictor
                self.learned = LearnedPredictor(model_type="tree")
            except ImportError as e:
                print(f"[daemon] WARNING: learned predictor unavailable: {e}",
                      file=sys.stderr)
                if predictor_type == "learned":
                    print("[daemon] falling back to EWMA", file=sys.stderr)
                    self.predictor_type = "ewma"

        # Counters for summary.
        self.total_events = 0
        self.total_predictions = 0
        self.gated_fallbacks = 0

    def process_events(self, events: list[BurstEvent]) -> None:
        """Process a batch of burst events and update predictions."""
        for evt in events:
            self.total_events += 1

            # ---- Run predictors ---------------------------------- #
            predicted_ns = 0
            confidence = 500
            predictor_type_id = 0   # 0 = EWMA, 1 = learned

            # Always update EWMA (it's cheap and provides fallback).
            ewma_pred, ewma_conf = self.ewma.update(evt.pid, evt.burst_ns)

            if self.predictor_type == "ewma":
                predicted_ns = ewma_pred
                confidence = ewma_conf
                predictor_type_id = 0

            elif self.predictor_type == "learned" and self.learned:
                l_pred, l_conf = self.learned.update(
                    evt.pid, evt.burst_ns, evt.sleep_ns, evt.preempted
                )
                predicted_ns = l_pred
                confidence = l_conf
                predictor_type_id = 1

            elif self.predictor_type == "both" and self.learned:
                # Use learned when confident, EWMA otherwise.
                l_pred, l_conf = self.learned.update(
                    evt.pid, evt.burst_ns, evt.sleep_ns, evt.preempted
                )
                if l_conf >= CONFIDENCE_THRESHOLD:
                    predicted_ns = l_pred
                    confidence = l_conf
                    predictor_type_id = 1
                else:
                    predicted_ns = ewma_pred
                    confidence = ewma_conf
                    predictor_type_id = 0

            else:
                predicted_ns = ewma_pred
                confidence = ewma_conf

            # ---- Confidence gating ------------------------------- #
            if self.confidence_gate and confidence < CONFIDENCE_THRESHOLD:
                self.gated_fallbacks += 1
                # Fall back to reactive MLFQ: set queue_hint to 0 and
                # slice_hint to 0 so the scheduler uses its own demotion.
                entry = PredictionEntry(
                    predicted_burst_ns=predicted_ns,
                    confidence=confidence,
                    queue_hint=0,
                    slice_hint_ns=0,
                    predictor_type=predictor_type_id,
                )
            else:
                queue = queue_from_burst(predicted_ns)
                s_hint = (compute_predicted_slice(predicted_ns, queue)
                          if self.predicted_slices else 0)
                entry = PredictionEntry(
                    predicted_burst_ns=predicted_ns,
                    confidence=confidence,
                    queue_hint=queue,
                    slice_hint_ns=s_hint,
                    predictor_type=predictor_type_id,
                )

            self.bridge.update_prediction(evt.pid, entry)
            self.total_predictions += 1

            if self.verbose:
                gated = " [GATED]" if (self.confidence_gate and
                                       confidence < CONFIDENCE_THRESHOLD) else ""
                print(
                    f"  pid={evt.pid:5d}  burst={evt.burst_ns/1e6:8.2f}ms  "
                    f"pred={predicted_ns/1e6:8.2f}ms  conf={confidence:4d}  "
                    f"q={entry.queue_hint}  slice={entry.slice_hint_ns/1e6:.2f}ms"
                    f"{gated}"
                )

    def summary(self) -> dict:
        """Return a summary of the daemon run."""
        preds = self.bridge.all_predictions()
        return {
            "total_events": self.total_events,
            "total_predictions": self.total_predictions,
            "gated_fallbacks": self.gated_fallbacks,
            "active_tasks": len(preds),
            "predictions": {
                pid: {
                    "predicted_burst_ms": p.predicted_burst_ns / 1e6,
                    "confidence": p.confidence,
                    "queue_hint": p.queue_hint,
                    "slice_hint_ms": p.slice_hint_ns / 1e6,
                    "predictor": "ewma" if p.predictor_type == 0 else "learned",
                }
                for pid, p in preds.items()
            },
        }


# ------------------------------------------------------------------ #
#  CLI entry point                                                    #
# ------------------------------------------------------------------ #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="predictor.daemon",
        description="BurstSched predictor daemon — reads burst events "
                    "and writes predictions to BPF maps.",
    )
    p.add_argument("--mock", action="store_true",
                   help="Use mock simulator (no Linux required)")
    p.add_argument("--predictor", choices=["ewma", "learned", "both"],
                   default="ewma",
                   help="Predictor to use (default: ewma)")
    p.add_argument("--alpha", type=float, default=0.5,
                   help="EWMA smoothing factor (default: 0.5)")
    p.add_argument("--duration", type=int, default=30,
                   help="Seconds to run (0 = indefinite, default: 30)")
    p.add_argument("--seed", type=int, default=42,
                   help="RNG seed for mock mode (default: 42)")
    p.add_argument("--confidence", dest="confidence_gate",
                   action="store_true", default=True,
                   help="Enable confidence gating (default)")
    p.add_argument("--no-confidence", dest="confidence_gate",
                   action="store_false",
                   help="Disable confidence gating")
    p.add_argument("--predicted-slices", dest="predicted_slices",
                   action="store_true", default=True,
                   help="Enable predicted slice scaling (default)")
    p.add_argument("--no-predicted-slices", dest="predicted_slices",
                   action="store_false",
                   help="Disable predicted slice scaling")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print every prediction")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    print("=" * 64)
    print("  BurstSched Predictor Daemon")
    print("=" * 64)
    print(f"  Mode:            {'MOCK' if args.mock else 'LIVE'}")
    print(f"  Predictor:       {args.predictor}")
    print(f"  Alpha (EWMA):    {args.alpha}")
    print(f"  Confidence gate: {args.confidence_gate}")
    print(f"  Predicted slices:{args.predicted_slices}")
    print(f"  Duration:        {args.duration}s")
    print("=" * 64)

    bridge = BPFBridge(mock=args.mock, seed=args.seed)

    daemon = PredictorDaemon(
        bridge=bridge,
        predictor_type=args.predictor,
        alpha=args.alpha,
        confidence_gate=args.confidence_gate,
        predicted_slices=args.predicted_slices,
        verbose=args.verbose,
    )

    # Graceful shutdown on Ctrl-C.
    running = True

    def handle_sig(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_sig)

    start = time.monotonic()
    tick = 0

    while running:
        if args.duration > 0 and (time.monotonic() - start) >= args.duration:
            break

        events = bridge.poll_events()
        tick += 1

        if tick % 10 == 1 or args.verbose:
            elapsed = time.monotonic() - start
            print(f"\n[tick {tick:4d} | {elapsed:.1f}s] "
                  f"polled {len(events)} events")

        daemon.process_events(events)

        # In mock mode, pace at ~100 ticks/sec for readable output.
        if args.mock:
            time.sleep(0.01)

    # ---- Summary ------------------------------------------------- #
    print("\n" + "=" * 64)
    print("  Run Summary")
    print("=" * 64)
    summary = daemon.summary()
    print(f"  Total events processed: {summary['total_events']}")
    print(f"  Total predictions made: {summary['total_predictions']}")
    print(f"  Confidence-gated fallbacks: {summary['gated_fallbacks']}")
    print(f"  Active tasks: {summary['active_tasks']}")
    print()

    for pid, info in summary["predictions"].items():
        print(f"  PID {pid}:")
        print(f"    predicted burst: {info['predicted_burst_ms']:.3f} ms")
        print(f"    confidence:      {info['confidence']}")
        print(f"    queue hint:      {info['queue_hint']}")
        print(f"    slice hint:      {info['slice_hint_ms']:.3f} ms")
        print(f"    predictor:       {info['predictor']}")
        print()

    # Print EWMA stats for each task.
    print("  EWMA predictor stats:")
    for pid in summary["predictions"]:
        stats = daemon.ewma.stats(pid)
        if stats:
            print(f"    PID {pid}: n={stats['n_updates']}  "
                  f"pred={stats['predicted_ns']/1e6:.3f}ms  "
                  f"conf={stats['confidence']}  "
                  f"window_mean={stats['window_mean_ns']/1e6:.3f}ms")

    print("\n[daemon] done.")


if __name__ == "__main__":
    main()
