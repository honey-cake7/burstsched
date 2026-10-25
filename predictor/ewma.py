"""
ewma.py — Textbook Exponential Weighted Moving Average (EWMA) predictor
           with burst-variance confidence scoring.

This is the baseline predictor described in the proposal: estimate the
next CPU burst using exponential averaging of past bursts, exactly as
OS textbooks (Silberschatz §5.3.4 / Tanenbaum §2.4.3) present it.

    predicted_next = α × actual_last + (1 − α) × predicted_prev

The module also computes a **confidence score** based on the coefficient
of variation (CV) of recent bursts.  When CV is low the task has stable
behaviour and the prediction can be trusted; when CV is high the task
is erratic and the scheduler should fall back to reactive MLFQ.

    CV = σ / μ   (over the last *window_size* bursts)
    confidence = clamp(1 − CV, 0, 1)   → scaled to 0–1000 fixed-point

Usage:
    predictor = EWMAPredictor(alpha=0.5, window_size=8)
    predicted, confidence = predictor.update(pid=1001, burst_ns=450_000)
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, Tuple


# ------------------------------------------------------------------ #
#  Per-task state                                                     #
# ------------------------------------------------------------------ #

@dataclass
class _TaskState:
    """Internal EWMA state for a single task."""
    predicted_ns: float = 0.0           # current EWMA estimate
    burst_history: deque = field(
        default_factory=lambda: deque(maxlen=32)
    )                                     # ring buffer of recent bursts
    n_updates: int = 0                   # total observations


# ------------------------------------------------------------------ #
#  EWMAPredictor                                                      #
# ------------------------------------------------------------------ #

class EWMAPredictor:
    """
    Per-task EWMA burst predictor with variance-based confidence.

    Parameters
    ----------
    alpha : float
        Smoothing factor in (0, 1].  Larger values weigh the most
        recent burst more heavily.  Textbook default is 0.5.
    window_size : int
        Number of recent bursts used to compute variance / confidence.
    confidence_threshold : int
        Fixed-point confidence (0–1000) below which the prediction
        should be considered unreliable.  Exposed so the daemon can
        pass it through to the confidence_gate feature.
    """

    def __init__(self, alpha: float = 0.5, window_size: int = 8,
                 confidence_threshold: int = 500):
        if not 0.0 < alpha <= 1.0:
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        self.alpha = alpha
        self.window_size = window_size
        self.confidence_threshold = confidence_threshold
        self._tasks: Dict[int, _TaskState] = defaultdict(_TaskState)

    # ---- Core update --------------------------------------------- #

    def update(self, pid: int, burst_ns: int) -> Tuple[int, int]:
        """
        Record an observed burst and return ``(predicted_ns, confidence)``.

        Parameters
        ----------
        pid : int
            Task PID.
        burst_ns : int
            The burst length just observed (nanoseconds).

        Returns
        -------
        predicted_ns : int
            Predicted next burst (nanoseconds).
        confidence : int
            Confidence score in [0, 1000].  1000 = perfectly stable.
        """
        state = self._tasks[pid]

        # First observation: initialise to the observed value.
        if state.n_updates == 0:
            state.predicted_ns = float(burst_ns)
        else:
            # Classic EWMA: τ_{n+1} = α × t_n + (1−α) × τ_n
            state.predicted_ns = (
                self.alpha * burst_ns +
                (1.0 - self.alpha) * state.predicted_ns
            )

        state.burst_history.append(burst_ns)
        state.n_updates += 1

        confidence = self._compute_confidence(state)
        return int(state.predicted_ns), confidence

    # ---- Confidence ---------------------------------------------- #

    def _compute_confidence(self, state: _TaskState) -> int:
        """
        Compute confidence from the coefficient of variation (CV) of
        the recent burst window.

            CV = σ / μ
            confidence = clamp(1 − CV, 0, 1)  × 1000

        With fewer than 2 observations, return the midpoint (500).
        """
        hist = state.burst_history
        n = len(hist)
        if n < 2:
            return 500   # insufficient data — neutral confidence

        mean = sum(hist) / n
        if mean == 0:
            return 500

        variance = sum((x - mean) ** 2 for x in hist) / n
        std = math.sqrt(variance)
        cv = std / mean                       # coefficient of variation

        raw = max(0.0, min(1.0, 1.0 - cv))   # clamp to [0, 1]
        return int(raw * 1000)

    # ---- Convenience --------------------------------------------- #

    def predict(self, pid: int) -> Tuple[int, int] | None:
        """Return current prediction without recording a new burst."""
        state = self._tasks.get(pid)
        if state is None or state.n_updates == 0:
            return None
        return int(state.predicted_ns), self._compute_confidence(state)

    def is_confident(self, pid: int) -> bool:
        """True if the current prediction meets the confidence threshold."""
        pred = self.predict(pid)
        if pred is None:
            return False
        _, confidence = pred
        return confidence >= self.confidence_threshold

    def reset(self, pid: int) -> None:
        """Clear all history for a task (e.g. on exec or exit)."""
        self._tasks.pop(pid, None)

    def stats(self, pid: int) -> dict | None:
        """Return diagnostic info for a task (useful for ablation logs)."""
        state = self._tasks.get(pid)
        if state is None:
            return None
        hist = list(state.burst_history)
        mean = sum(hist) / len(hist) if hist else 0
        return {
            "pid": pid,
            "n_updates": state.n_updates,
            "predicted_ns": int(state.predicted_ns),
            "confidence": self._compute_confidence(state),
            "window_mean_ns": int(mean),
            "window_size": len(hist),
            "alpha": self.alpha,
        }
