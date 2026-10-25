"""
bpf_bridge.py — Interface between the predictor daemon and the BPF
                 maps / ringbuf.

Two modes:
  1. LIVE mode  — reads burst_events from the BPF ring buffer (via
                  libbpf / bcc bindings) and writes prediction_entry
                  records into the BPF hash map.  Requires Linux 6.12+.

  2. MOCK mode  — fully self-contained simulation that generates
                  synthetic burst events from configurable task
                  profiles, allowing the predictor to be developed and
                  tested on any OS (including Windows / macOS).

Usage:
  bridge = BPFBridge(mock=True)
  for event in bridge.poll_events():
      ...
  bridge.update_prediction(pid, predicted_burst_ns, confidence, ...)
"""

from __future__ import annotations

import dataclasses
import random
import time
from typing import Iterator, Dict, Optional, Callable

# ------------------------------------------------------------------ #
#  Shared data structures (mirror burstsched_common.h)                #
# ------------------------------------------------------------------ #

@dataclasses.dataclass
class BurstEvent:
    """Mirrors struct burst_event from burstsched_common.h."""
    pid: int
    tgid: int
    burst_ns: int
    sleep_ns: int
    timestamp_ns: int
    preempted: bool


@dataclasses.dataclass
class PredictionEntry:
    """Mirrors struct prediction_entry from burstsched_common.h."""
    predicted_burst_ns: int
    confidence: int          # 0–1000 fixed-point (1000 = 100%)
    queue_hint: int          # 0 = highest priority
    slice_hint_ns: int       # 0 = use default
    predictor_type: int      # 0 = EWMA, 1 = learned


# ------------------------------------------------------------------ #
#  Task profile for mock simulation                                   #
# ------------------------------------------------------------------ #

@dataclasses.dataclass
class MockTaskProfile:
    """
    Describes a synthetic task's burst/sleep behaviour for the mock
    simulator.

    Fields
    ------
    pid           Fake PID.
    label         Human-readable name (e.g. "interactive", "cpu_hog").
    burst_mean_ns Mean burst length in nanoseconds.
    burst_std_ns  Std-dev of burst length (Gaussian noise).
    sleep_mean_ns Mean sleep length in nanoseconds.
    sleep_std_ns  Std-dev of sleep length.
    phase_change  Optional callback ``(tick: int) -> MockTaskProfile``
                  that can return a *new* profile mid-run to simulate
                  phase changes (compile → idle → interactive, etc.).
    """
    pid: int
    label: str
    burst_mean_ns: int
    burst_std_ns: int
    sleep_mean_ns: int
    sleep_std_ns: int
    phase_change: Optional[Callable] = dataclasses.field(default=None, repr=False)


# ------------------------------------------------------------------ #
#  Default mock task profiles                                         #
# ------------------------------------------------------------------ #

DEFAULT_MOCK_PROFILES = [
    # Interactive task: short bursts (~0.5 ms), long sleeps (~50 ms)
    MockTaskProfile(
        pid=1001, label="interactive",
        burst_mean_ns=500_000, burst_std_ns=100_000,
        sleep_mean_ns=50_000_000, sleep_std_ns=10_000_000,
    ),
    # CPU-bound hog: long bursts (~20 ms), virtually no sleep
    MockTaskProfile(
        pid=1002, label="cpu_hog",
        burst_mean_ns=20_000_000, burst_std_ns=3_000_000,
        sleep_mean_ns=100_000, sleep_std_ns=50_000,
    ),
    # Mixed / erratic task: moderate bursts with high variance
    MockTaskProfile(
        pid=1003, label="erratic",
        burst_mean_ns=5_000_000, burst_std_ns=4_000_000,
        sleep_mean_ns=10_000_000, sleep_std_ns=8_000_000,
    ),
    # Phase changer: starts CPU-bound, switches to interactive after tick 50
    MockTaskProfile(
        pid=1004, label="phase_changer",
        burst_mean_ns=18_000_000, burst_std_ns=2_000_000,
        sleep_mean_ns=200_000, sleep_std_ns=100_000,
        phase_change=lambda tick: MockTaskProfile(
            pid=1004, label="phase_changer_interactive",
            burst_mean_ns=400_000, burst_std_ns=80_000,
            sleep_mean_ns=60_000_000, sleep_std_ns=10_000_000,
        ) if tick >= 50 else None,
    ),
]


# ------------------------------------------------------------------ #
#  Mock simulator engine                                              #
# ------------------------------------------------------------------ #

class MockSimulator:
    """
    Generates synthetic BurstEvent objects from a set of task profiles.
    Each call to :meth:`generate_tick` produces one event per task.
    """

    def __init__(self, profiles: list[MockTaskProfile] | None = None, seed: int = 42):
        self._profiles: Dict[int, MockTaskProfile] = {}
        self._rng = random.Random(seed)
        self._tick = 0
        self._base_time_ns = int(time.time() * 1e9)

        for p in (profiles or DEFAULT_MOCK_PROFILES):
            self._profiles[p.pid] = p

    def _sample_ns(self, mean: int, std: int) -> int:
        """Sample a non-negative duration from a Gaussian."""
        val = self._rng.gauss(mean, std)
        return max(0, int(val))

    def generate_tick(self) -> list[BurstEvent]:
        """Generate one burst event per task for the current tick."""
        events: list[BurstEvent] = []
        self._tick += 1

        for pid, profile in list(self._profiles.items()):
            # Apply phase change if defined.
            if profile.phase_change is not None:
                new_profile = profile.phase_change(self._tick)
                if new_profile is not None:
                    self._profiles[pid] = new_profile
                    profile = new_profile

            burst_ns = self._sample_ns(profile.burst_mean_ns, profile.burst_std_ns)
            sleep_ns = self._sample_ns(profile.sleep_mean_ns, profile.sleep_std_ns)
            ts = self._base_time_ns + self._tick * 1_000_000  # 1 ms per tick

            events.append(BurstEvent(
                pid=pid,
                tgid=pid,
                burst_ns=burst_ns,
                sleep_ns=sleep_ns,
                timestamp_ns=ts,
                preempted=self._rng.random() < 0.1,   # 10% preempted
            ))

        return events


# ------------------------------------------------------------------ #
#  BPF Bridge                                                         #
# ------------------------------------------------------------------ #

class BPFBridge:
    """
    Unified interface for the predictor daemon to read burst events and
    write predictions.

    Parameters
    ----------
    mock : bool
        If True, use the built-in MockSimulator instead of real BPF
        ringbuf I/O.  This lets the full predictor run on any OS.
    profiles : list[MockTaskProfile] | None
        Custom task profiles for mock mode (uses defaults if None).
    seed : int
        RNG seed for reproducible mock runs.
    """

    def __init__(self, mock: bool = False,
                 profiles: list[MockTaskProfile] | None = None,
                 seed: int = 42):
        self.mock = mock
        self._predictions: Dict[int, PredictionEntry] = {}

        if mock:
            self._sim = MockSimulator(profiles=profiles, seed=seed)
            self._tick_events: list[BurstEvent] = []
        else:
            self._sim = None  # type: ignore[assignment]
            self._init_live()

    # ---- Live BPF helpers ---------------------------------------- #

    def _init_live(self):
        """
        Initialise real BPF ringbuf consumer and map file descriptors.
        Requires Linux with libbpf Python bindings or bcc.
        """
        try:
            # Attempt to import the bcc library for BPF ring buffer access.
            from bcc import BPF  # type: ignore[import-untyped]
            # TODO: open the pinned maps or attach to the running
            #       burstsched BPF program's ring buffer and hash map.
            raise NotImplementedError(
                "Live BPF bridge not yet wired — "
                "run with --mock until Linux 6.12 is available."
            )
        except ImportError:
            raise RuntimeError(
                "bcc Python bindings not found.  Install bcc or run "
                "with --mock for simulation mode."
            )

    # ---- Event polling ------------------------------------------- #

    def poll_events(self) -> list[BurstEvent]:
        """
        Return the next batch of burst events.

        In mock mode this generates one simulated tick.
        In live mode it drains the BPF ringbuf.
        """
        if self.mock:
            return self._sim.generate_tick()
        else:
            # TODO: drain the ringbuf and deserialise burst_event structs.
            raise NotImplementedError("live poll_events")

    # ---- Prediction updates -------------------------------------- #

    def update_prediction(self, pid: int, entry: PredictionEntry) -> None:
        """
        Write a prediction for *pid* into the shared map.

        In mock mode the prediction is stored in-memory so the daemon
        and benchmarks can inspect it.  In live mode it calls
        ``bpf_map_update_elem``.
        """
        self._predictions[pid] = entry

        if not self.mock:
            # TODO: bpf_map_update_elem(predictions_fd, pid, entry)
            raise NotImplementedError("live update_prediction")

    def get_prediction(self, pid: int) -> PredictionEntry | None:
        """Retrieve the last prediction for *pid* (mock only)."""
        return self._predictions.get(pid)

    def all_predictions(self) -> Dict[int, PredictionEntry]:
        """Return a snapshot of all current predictions."""
        return dict(self._predictions)
