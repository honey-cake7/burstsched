# predictor/__init__.py
"""
BurstSched Predictor Package
=============================

Track 2 deliverable: user-space prediction daemon that reads burst
events, computes predicted burst lengths, and writes them back to the
shared BPF map (or a mock simulation bridge for local development).

Modules
-------
bpf_bridge     Live BPF ringbuf/map I/O and --mock simulator mode.
ewma           Textbook EWMA predictor with burst-variance confidence.
learned_model  Scikit-learn Decision Tree / Logistic Regression predictor.
daemon         Main user-space loop tying everything together.
"""
