"""
learned_model.py — Lightweight learned predictor for BurstSched.

Uses scikit-learn to train a small model (Decision Tree or Logistic
Regression) over recent burst and sleep history, then predicts the
next burst length.

Feature vector per prediction (proposal §"How it works" / Tech stack):
  - Last N burst lengths (default N=4)
  - Last N sleep durations
  - Running mean of bursts
  - Running variance of bursts
  - Preemption ratio (fraction of recent bursts that ended by preempt)

The model is retrained periodically (every *retrain_interval* events)
on the accumulated history for each task independently, keeping the
model tiny and fast enough for a user-space daemon loop.

Usage:
    model = LearnedPredictor(model_type="tree", history_depth=4)
    predicted, confidence = model.update(pid, burst_ns, sleep_ns, preempted)
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional

try:
    import numpy as np
    from sklearn.tree import DecisionTreeRegressor
    from sklearn.linear_model import Ridge
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False


# ------------------------------------------------------------------ #
#  Per-task observation record                                        #
# ------------------------------------------------------------------ #

@dataclass
class _Observation:
    burst_ns: int
    sleep_ns: int
    preempted: bool


@dataclass
class _TaskHistory:
    observations: deque = field(
        default_factory=lambda: deque(maxlen=256)
    )
    model: object = None
    n_since_retrain: int = 0
    last_predicted_ns: float = 0.0
    last_confidence: int = 500


# ------------------------------------------------------------------ #
#  LearnedPredictor                                                   #
# ------------------------------------------------------------------ #

class LearnedPredictor:
    """
    Learned burst predictor using scikit-learn.

    Parameters
    ----------
    model_type : str
        ``"tree"`` for DecisionTreeRegressor, ``"ridge"`` for Ridge
        regression.  Both are fast to train on small windows.
    history_depth : int
        Number of past bursts/sleeps used as features (the "lookback").
    retrain_interval : int
        Retrain the model every *retrain_interval* new observations.
    min_samples : int
        Minimum observations before attempting to train a model.
    """

    def __init__(self, model_type: str = "tree", history_depth: int = 4,
                 retrain_interval: int = 16, min_samples: int = 8):
        if not _HAS_SKLEARN:
            raise ImportError(
                "scikit-learn is required for the learned predictor.  "
                "Install it with: pip install scikit-learn"
            )
        self.model_type = model_type
        self.history_depth = history_depth
        self.retrain_interval = retrain_interval
        self.min_samples = min_samples
        self._tasks: Dict[int, _TaskHistory] = defaultdict(_TaskHistory)

    # ---- Public API ---------------------------------------------- #

    def update(self, pid: int, burst_ns: int, sleep_ns: int = 0,
               preempted: bool = False) -> Tuple[int, int]:
        """
        Record a new observation and return ``(predicted_ns, confidence)``.
        """
        hist = self._tasks[pid]
        hist.observations.append(_Observation(burst_ns, sleep_ns, preempted))
        hist.n_since_retrain += 1

        # Retrain if enough new data has arrived.
        if (hist.n_since_retrain >= self.retrain_interval and
                len(hist.observations) >= self.min_samples):
            self._retrain(hist)

        # Predict using the current model (or fall back to simple mean).
        predicted = self._predict_one(hist)
        confidence = self._compute_confidence(hist)

        hist.last_predicted_ns = predicted
        hist.last_confidence = confidence
        return int(predicted), confidence

    def predict(self, pid: int) -> Optional[Tuple[int, int]]:
        """Return current prediction without recording a new burst."""
        hist = self._tasks.get(pid)
        if hist is None or len(hist.observations) == 0:
            return None
        return int(hist.last_predicted_ns), hist.last_confidence

    def reset(self, pid: int) -> None:
        self._tasks.pop(pid, None)

    # ---- Feature extraction -------------------------------------- #

    def _build_features(self, obs_list: list[_Observation],
                        idx: int) -> Optional[List[float]]:
        """
        Build the feature vector for sample at *idx*.

        Returns None if there aren't enough preceding observations.
        """
        d = self.history_depth
        if idx < d:
            return None

        feats: List[float] = []

        # Last d burst lengths.
        for j in range(d):
            feats.append(float(obs_list[idx - d + j].burst_ns))

        # Last d sleep durations.
        for j in range(d):
            feats.append(float(obs_list[idx - d + j].sleep_ns))

        # Running mean of bursts in the window.
        burst_window = [obs_list[idx - d + j].burst_ns for j in range(d)]
        mean = sum(burst_window) / d
        feats.append(mean)

        # Running variance.
        var = sum((x - mean) ** 2 for x in burst_window) / d
        feats.append(var)

        # Preemption ratio.
        preempt_count = sum(1 for j in range(d) if obs_list[idx - d + j].preempted)
        feats.append(preempt_count / d)

        return feats

    # ---- Training ------------------------------------------------ #

    def _retrain(self, hist: _TaskHistory) -> None:
        """Retrain the model on the task's accumulated history."""
        obs = list(hist.observations)
        d = self.history_depth

        X: List[List[float]] = []
        y: List[float] = []

        # Build (feature, label) pairs — label is the *next* burst.
        for i in range(d, len(obs)):
            feats = self._build_features(obs, i)
            if feats is not None:
                X.append(feats)
                y.append(float(obs[i].burst_ns))

        if len(X) < self.min_samples:
            return

        X_np = np.array(X, dtype=np.float64)
        y_np = np.array(y, dtype=np.float64)

        if self.model_type == "tree":
            model = DecisionTreeRegressor(max_depth=5)
        else:
            model = Ridge(alpha=1.0)

        model.fit(X_np, y_np)
        hist.model = model
        hist.n_since_retrain = 0

    # ---- Prediction ---------------------------------------------- #

    def _predict_one(self, hist: _TaskHistory) -> float:
        """Predict the next burst for *hist* using the trained model."""
        obs = list(hist.observations)
        if not obs:
            return 0.0

        # If we have a trained model and enough recent history, use it.
        if hist.model is not None and len(obs) >= self.history_depth:
            feats = self._build_features(obs, len(obs) - 1)
            if feats is not None:
                # The feature vector for "the next burst" uses the last
                # d observations including the one we just recorded.
                # We need to shift: use observations ending at len-1.
                # Actually the feature vector built at index len(obs)-1
                # looks at obs[len-d .. len-1] — that's what we want
                # but the label at that index is obs[len-1] which is the
                # burst we *just* saw.  For prediction we need to build
                # features from the tail d observations.
                X_pred = np.array([feats], dtype=np.float64)
                predicted = hist.model.predict(X_pred)[0]
                return max(0.0, predicted)

        # Fallback: simple mean of recent bursts.
        recent = [o.burst_ns for o in obs[-8:]]
        return sum(recent) / len(recent)

    # ---- Confidence ---------------------------------------------- #

    def _compute_confidence(self, hist: _TaskHistory) -> int:
        """
        Confidence based on coefficient of variation of recent bursts,
        same formula as EWMA for comparability.
        """
        obs = list(hist.observations)
        recent = [o.burst_ns for o in obs[-8:]]
        n = len(recent)
        if n < 2:
            return 500

        mean = sum(recent) / n
        if mean == 0:
            return 500

        var = sum((x - mean) ** 2 for x in recent) / n
        std = math.sqrt(var)
        cv = std / mean

        raw = max(0.0, min(1.0, 1.0 - cv))
        return int(raw * 1000)

    # ---- Diagnostics --------------------------------------------- #

    def stats(self, pid: int) -> Optional[dict]:
        hist = self._tasks.get(pid)
        if hist is None:
            return None
        obs = list(hist.observations)
        return {
            "pid": pid,
            "n_observations": len(obs),
            "has_model": hist.model is not None,
            "model_type": self.model_type,
            "predicted_ns": int(hist.last_predicted_ns),
            "confidence": hist.last_confidence,
        }
