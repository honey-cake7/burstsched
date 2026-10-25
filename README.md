# BurstSched: A Burst-Predicting Scheduler for Linux

> A two-person, four-week OS project that implements burst-aware CPU scheduling via **sched_ext** (Linux 6.12+), asking whether a better predictor makes a better scheduler.

---

## Table of Contents

- [The Idea](#the-idea)
- [How It Works](#how-it-works)
- [Architecture](#architecture)
- [Extensions](#extensions)
- [Ablation Study](#ablation-study)
- [Scope](#scope)
- [Tech Stack](#tech-stack)
- [Repository Structure](#repository-structure)
- [Getting Started](#getting-started)
- [Work Split](#work-split)
- [Risks & Mitigations](#risks--mitigations)
- [What's Compared](#whats-compared)
- [License](#license)

---

## The Idea

Linux lets you load your own CPU scheduler as a BPF program through **sched_ext**, merged in Linux 6.12. You can replace the kernel's scheduling policy without modifying or recompiling the kernel, and a bad scheduler can be unloaded safely. Meta and Google use this mechanism to experiment with scheduling policies in production.

BurstSched builds around one classic OS insight: **shortest-job-first needs to know how long a task will run before it blocks, and the OS can only estimate that.** Textbooks do this with exponential averaging of past CPU bursts. This project implements burst-aware scheduling for real and asks whether a better predictor makes a better scheduler.

The result is a **multilevel feedback queue (MLFQ) whose placement is predicted instead of discovered by demotion.** Classic MLFQ is reactive — a task starts at high priority and sinks after it has already used a full slice. BurstSched tries to place a waking task correctly *immediately*, which matters when a task changes behaviour (a compile job that becomes interactive, a browser tab that goes idle and then bursts).

---

## How It Works

1. **Observe** (in the scheduler) — Each time a task runs and then blocks or is preempted, record its burst length and how long it slept. This is the same information classic textbooks use.

2. **Predict** — Estimate each task's next burst. A user-space helper does the learning and writes the result into a shared BPF map, so ML never has to run inside the kernel.

3. **Decide** (in the scheduler) — Use the prediction to choose a queue and time slice. Tasks expected to run briefly (interactive, I/O-bound) go to a high-priority queue with a short slice. Tasks expected to run long (CPU-bound) go to lower-priority queues with longer slices, which means fewer context switches.

4. **Stay safe** — Aging prevents starvation of long-running tasks, and a misprediction can only cost performance, never correctness.

---

## Architecture

Both tracks meet at a **single interface**: the shared BPF map holding predicted burst lengths.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Linux Kernel (6.12+)                        │
│                                                                     │
│   ┌───────────────────────────────────────────────────────────┐     │
│   │              burstsched.bpf.c (sched_ext)                 │     │
│   │                                                           │     │
│   │   ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐   │     │
│   │   │  Q0     │  │  Q1     │  │  Q2     │  │  Q3     │   │     │
│   │   │ <1ms    │  │ <4ms    │  │ <16ms   │  │ ≥16ms   │   │     │
│   │   │ 1ms sl. │  │ 4ms sl. │  │ 16ms sl.│  │ 32ms sl.│   │     │
│   │   └─────────┘  └─────────┘  └─────────┘  └─────────┘   │     │
│   │                      │                                    │     │
│   │              ┌───────▼───────┐     ┌──────────────┐      │     │
│   │              │  burst_events │     │  predictions │      │     │
│   │              │   (ringbuf)   │     │  (hash map)  │      │     │
│   │              └───────┬───────┘     └──────▲───────┘      │     │
│   └──────────────────────┼────────────────────┼──────────────┘     │
│                          │                    │                      │
└──────────────────────────┼────────────────────┼──────────────────────┘
                           │                    │
              ┌────────────▼────────────────────┴────────────┐
              │           User-Space Predictor Daemon         │
              │                                               │
              │   bpf_bridge.py ──► ewma.py                  │
              │                 ──► learned_model.py          │
              │                                               │
              │   Reads burst events ──► Predicts next burst  │
              │   ──► Writes prediction + confidence to map   │
              └───────────────────────────────────────────────┘
```

---

## Extensions

BurstSched adds four features on top of the base scheduler. Two are essential, one is optional, and one is a stretch goal.

| Feature | What It Does | OS Concepts | Risk | Status |
|---------|-------------|-------------|------|--------|
| **Confidence-gated placement** | Reports how reliable the estimate is. Stable-burst tasks are placed by prediction; erratic tasks fall back to reactive MLFQ. | Robustness under uncertainty, fallback policy | Low | **Essential** |
| **Predicted time slices** | Slice length scales with the predicted burst, so a short-burst task gets a slice just above its burst and a CPU-bound task gets a longer one. | Time slices, context-switch overhead, responsiveness | Low | **Essential** |
| **Wakeup preemption** | When a sleeping task wakes, it preempts the running task only if its predicted burst is short. | Preemption, wakeup latency, priority | Medium | **Optional** |
| **Per-CPU queues** | Each CPU has its own queues; the scheduler picks the CPU for a waking task, weighing cache affinity against load balance. | Multicore scheduling, cache affinity, work stealing | High | **Stretch** |

### Confidence-Gated Placement
Reliability is estimated from how much a task's recent bursts vary (coefficient of variation). This tests directly whether acting on uncertain predictions hurts, and it makes the scheduler safer because a bad prediction can only cost performance.

### Predicted Time Slices
Fixed slices force a trade-off between context-switch overhead and responsiveness. Scaling the slice by the prediction lets you *measure* that trade-off instead of guessing it.

### Wakeup Preemption
A short predicted burst justifies preempting a long-running hog; a long one does not. The scheduler makes a real policy decision on every wakeup.

### Per-CPU Queues
Brings in affinity and work stealing. A half-working load balancer is worse than none, so this is the first thing to drop if time runs short.

---

## Ablation Study

Run BurstSched with each feature added **one at a time**, and measure p99 wakeup latency, batch throughput, context-switch count, and worst-case wait for every configuration. This is a stronger result than a single before-and-after comparison because it shows which component earns its complexity.

| # | Configuration | Question It Answers |
|---|--------------|---------------------|
| 1 | Reactive MLFQ | What does the scheduler do without any prediction? |
| 2 | + EWMA prediction | Does the textbook predictor help? |
| 3 | + Learned predictor | Does learning beat simple averaging? |
| 4 | + Confidence gating | Does falling back on uncertain tasks reduce the cost of mispredictions? |
| 5 | + Predicted slices | Does slice scaling cut context switches without hurting latency? |
| 6 | + Wakeup preemption | Does preempting on short bursts improve interactive latency? |
| 7 | + Per-CPU placement *(if built)* | Does burst-aware placement help on multiple cores? |

A feature that moves none of the metrics should be reported as exactly that.

### Metrics Collected

| Metric | What It Measures |
|--------|-----------------|
| **p50 / p99 wakeup-to-run latency** | Interactive responsiveness |
| **Batch throughput** | CPU-bound job completion rate |
| **Context-switch count** | Overhead from scheduling decisions |
| **Worst-case wait** | Starvation (any single task's maximum delay) |
| **Fairness** | Whether long-running tasks are starved |

---

## Scope

### Essential (Must Deliver)

- [x] Burst tracking in the scheduler
- [x] A multi-queue scheduler with aging
- [x] An EWMA predictor
- [x] A reactive MLFQ baseline
- [x] Confidence-gated placement
- [x] Predicted time slices
- [x] A mixed-workload evaluation with the ablation study

### Optional

- [x] The learned predictor variant (scikit-learn Decision Tree / Ridge Regression)
- [x] Wakeup preemption by predicted burst
- [x] Phase-change workloads
- [ ] Per-CPU queues with burst-aware placement *(stretch goal — only after everything else works)*

### Out of Scope

- Full multi-core load balancing and work-stealing tuning beyond the stretch goal
- NUMA-aware scheduling
- Energy awareness
- Deep learning / reinforcement learning
- GUIs and dashboards

---

## Tech Stack

| Layer | Technologies | Why |
|-------|-------------|-----|
| **Kernel / Scheduler** | Linux 6.12+ with sched_ext, BPF | Load custom scheduler without recompiling kernel |
| **Scheduler code** | C + BPF (eBPF) | BPF runs in kernel safely, C for user-space loader |
| **Predictor** | Python (scikit-learn) | Small models (EWMA, logistic regression, decision trees) are fast to train and deploy |
| **Measurement** | Python (psutil, perf, bpftrace) | Read cgroup stats, run-queue latency, context switches |
| **Load generation** | stress-ng, Python | Synthetic workloads: periodic bursts, ramps, random |
| **Workload server** | Python (socket/http) | Simple request/response service to measure latency |
| **Data analysis** | Python (numpy, pandas, matplotlib) | Plot results, compute p50/p99 latency, throughput |
| **Build system** | Make, clang/llvm | Compile BPF to bytecode |
| **OS environment** | Fedora 41+ or Ubuntu 24.10+ | sched_ext already enabled |

**Minimal required:** Linux 6.12 with sched_ext, C + BPF compiler (clang/llvm), Python 3, stress-ng, and perf.

---

## Repository Structure

```
burstsched/
├── Makefile                          # clang/llvm BPF build + mock test targets
├── include/
│   └── burstsched_common.h           # Shared interface: burst_event, prediction_entry,
│                                     #   ablation feature flags, queue/slice constants
├── src/                              # Track 1: Kernel / Scheduler (C + eBPF)
│   ├── burstsched.bpf.c              # sched_ext MLFQ scheduler with aging, burst
│   │                                 #   tracking, and all extensions
│   └── burstsched_loader.c           # libbpf loader & --feature CLI flags
│
├── predictor/                        # Track 2: Predictor Daemon (Python)
│   ├── __init__.py
│   ├── bpf_bridge.py                 # BPF ringbuf/map I/O + --mock simulator mode
│   ├── ewma.py                       # EWMA predictor + burst-variance confidence
│   ├── learned_model.py              # sklearn Decision Tree / Ridge Regression
│   └── daemon.py                     # Main predict-update loop + CLI
│
├── workloads/                        # Synthetic Load Generation
│   ├── interactive_server.py         # TCP echo server — p50/p99 wakeup-to-run latency
│   ├── cpu_hog.py                    # Batch CPU-bound load (stress-ng / pure Python)
│   └── phase_changer.py              # Multi-phase workload (CPU → interactive → mixed)
│
└── benchmarks/                       # Evaluation & Ablation Study
    ├── run_ablation.py               # Automates all 7 ablation configurations
    ├── plot_results.py               # matplotlib charts (latency, throughput, etc.)
    └── ablation_results.json         # Generated results (after a run)
```

---

## Getting Started

### Prerequisites

- **Python 3.9+** (required for all local development)
- **scikit-learn** (optional — needed only for the learned predictor)
- **numpy, pandas, matplotlib** (optional — needed only for plotting)

For Linux BPF compilation (not needed for mock development):
- Linux 6.12+ kernel with `CONFIG_SCHED_CLASS_EXT=y`
- clang/llvm, libbpf-dev, bpftool

### Install Python Dependencies

```bash
pip install scikit-learn numpy pandas matplotlib
```

### Run the Predictor Daemon (Mock Mode)

Works on **any OS** — no Linux kernel required:

```bash
cd burstsched

# Basic mock run (30 seconds, EWMA predictor)
python -m predictor.daemon --mock

# Verbose output showing every prediction
python -m predictor.daemon --mock --verbose

# Use the learned predictor
python -m predictor.daemon --mock --predictor learned

# Disable confidence gating
python -m predictor.daemon --mock --no-confidence

# Custom duration and seed
python -m predictor.daemon --mock --duration 60 --seed 123
```

### Run the Ablation Study (Mock Mode)

```bash
# Run all 7 configurations
python benchmarks/run_ablation.py --mock

# Run specific configurations
python benchmarks/run_ablation.py --mock --configs ewma confidence_gate

# Generate plots from results
python benchmarks/plot_results.py
```

### Run Workloads (Mock Mode)

```bash
# Interactive server latency simulation
python workloads/interactive_server.py --mock --requests 5000

# CPU-bound batch load
python workloads/cpu_hog.py --mock --ticks 500

# Phase-changing workload
python workloads/phase_changer.py --mock --ticks 400
```

### Build on Linux (When Available)

```bash
# Generate vmlinux.h from running kernel
make vmlinux

# Build everything (BPF object + skeleton + loader)
make

# Load the scheduler with specific features
sudo ./burstsched_loader \
    --feature ewma \
    --feature confidence_gate \
    --feature predicted_slices

# Run predictor daemon in live mode
python -m predictor.daemon --predictor ewma
```

---

## Work Split

The project is designed for **two people** meeting at one interface — the shared BPF map holding predicted burst lengths.

### Person 1: Scheduler Side
- Owns: C, BPF, kernel APIs, perf, bpftrace
- Builds: BPF scheduler, queues, aging, safety mechanisms
- Takes extensions: slice scaling, wakeup preemption, per-CPU work
- Key files: `src/burstsched.bpf.c`, `src/burstsched_loader.c`, `include/burstsched_common.h`

### Person 2: Predictor & Evaluation
- Owns: Python, scikit-learn, workload generation, measurement
- Builds: Predictors (EWMA + learned), workload generators, ablation harness
- Takes extensions: confidence estimation, runs the ablation study
- Key files: `predictor/`, `workloads/`, `benchmarks/`

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| **Environment** | Need Linux 6.12+ with sched_ext | Mock mode lets all Python code run anywhere; verify distro support (Fedora 41+, Ubuntu 24.10+) early |
| **BPF learning curve** | Verifier restricts code, debugging is awkward | Start from example schedulers and modify incrementally |
| **Overfitting** | Synthetic workloads can flatter the design | Test on workloads the predictor never saw |
| **Scope creep** | Each extension adds interactions | Add features one at a time and re-measure after each |

**Fallback:** Build in **xv6** and run in QEMU. Use fixed-point arithmetic or a lookup-based predictor. You lose the "runs on real Linux" angle but keep the scheduler, the predictor, and the evaluation.

---

## What's Compared

| Scheduler | Purpose |
|-----------|---------|
| Default Linux (EEVDF) | Industry baseline |
| A simple sched_ext scheduler (bundled example) | What a minimal scheduler does |
| Reactive MLFQ (no prediction) | Isolates the value of prediction |
| BurstSched with EWMA predictor | Textbook predictor |
| BurstSched with learned predictor | The AI variant |

The interesting question is **how often prediction misses and what that costs**.

---

## Why This Is an OS Project

- Scheduling policy, run queues, time slices, preemption, and context-switch cost
- Starvation, aging, and the trade-off between fairness and responsiveness
- CPU-bound versus I/O-bound behaviour and burst estimation (SJF and MLFQ, directly from the course)
- The kernel / user-space split and the kernel scheduling interface

The AI part is small and replaceable: an exponential-average baseline versus one learned predictor (a small decision tree or logistic regression over recent burst and sleep history). **The scheduler is the main artifact.**

---

## Current Status

| Component | Status |
|-----------|--------|
| Shared interface (`burstsched_common.h`) | ✅ Complete |
| BPF scheduler (`burstsched.bpf.c`) | ✅ Scaffolded — compiles on Linux 6.12+ |
| User-space loader (`burstsched_loader.c`) | ✅ Scaffolded — CLI flags for all ablation rows |
| EWMA predictor | ✅ Fully implemented & tested |
| Learned predictor | ✅ Fully implemented (requires scikit-learn) |
| Mock simulator / BPF bridge | ✅ Fully implemented — runs on any OS |
| Predictor daemon | ✅ Fully implemented with all CLI flags |
| Interactive server workload | ✅ Mock + live TCP modes |
| CPU hog workload | ✅ Mock + pure-Python + stress-ng modes |
| Phase-change workload | ✅ Mock + live modes |
| Ablation study harness | ✅ Runs all 7 configurations in mock mode |
| Plot generation | ✅ matplotlib charts for all metrics |
| Live BPF integration | 🔲 Requires Linux 6.12+ to wire up |
| Per-CPU queues | 🔲 Stretch goal — stubs in place |

---

## License

GPL-2.0 (required for BPF programs that interface with the Linux kernel).
