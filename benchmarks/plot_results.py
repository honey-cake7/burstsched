"""
plot_results.py — Generate publication-quality charts from the
                   ablation study results.

Reads the JSON output from run_ablation.py and produces:
  1. Grouped bar chart: p50 and p99 latency per configuration.
  2. Throughput bar chart.
  3. Context-switch bar chart.
  4. Worst-case wait bar chart.
  5. Combined summary table (printed to stdout).

Dependencies: numpy, pandas, matplotlib.

Usage:
  python benchmarks/plot_results.py                          # default file
  python benchmarks/plot_results.py --input results.json
  python benchmarks/plot_results.py --format pdf
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List

try:
    import numpy as np
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")  # Non-interactive backend.
    import matplotlib.pyplot as plt
    _HAS_DEPS = True
except ImportError:
    _HAS_DEPS = False


# ------------------------------------------------------------------ #
#  Chart generation                                                   #
# ------------------------------------------------------------------ #

def load_results(path: str) -> pd.DataFrame:
    """Load ablation results JSON into a DataFrame."""
    with open(path) as f:
        data = json.load(f)

    rows = []
    for entry in data:
        rows.append({
            "config": entry["config_name"],
            "p50_us": entry["p50_latency_us"],
            "p99_us": entry["p99_latency_us"],
            "throughput": entry["throughput_events_per_sec"],
            "ctx_switches": entry["context_switches"],
            "worst_case_us": entry["worst_case_wait_us"],
            "gated_fallbacks": entry["gated_fallbacks"],
            "total_events": entry["total_events"],
        })

    return pd.DataFrame(rows)


def plot_latency(df: pd.DataFrame, output_dir: str, fmt: str = "png") -> None:
    """Grouped bar chart of p50 and p99 latency."""
    fig, ax = plt.subplots(figsize=(12, 6))
    x = np.arange(len(df))
    width = 0.35

    bars1 = ax.bar(x - width/2, df["p50_us"], width, label="p50",
                   color="#4C72B0", edgecolor="white", linewidth=0.5)
    bars2 = ax.bar(x + width/2, df["p99_us"], width, label="p99",
                   color="#DD8452", edgecolor="white", linewidth=0.5)

    ax.set_xlabel("Configuration", fontsize=12)
    ax.set_ylabel("Latency (µs)", fontsize=12)
    ax.set_title("Wakeup-to-Run Latency by Configuration", fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(df["config"], rotation=30, ha="right", fontsize=10)
    ax.legend(fontsize=11)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, f"latency.{fmt}"), dpi=150)
    plt.close(fig)
    print(f"  [plot] latency.{fmt}")


def plot_throughput(df: pd.DataFrame, output_dir: str, fmt: str = "png") -> None:
    """Bar chart of throughput (events/sec)."""
    fig, ax = plt.subplots(figsize=(10, 5))
    x = np.arange(len(df))
    ax.bar(x, df["throughput"], color="#55A868", edgecolor="white", linewidth=0.5)
    ax.set_xlabel("Configuration", fontsize=12)
    ax.set_ylabel("Throughput (events/s)", fontsize=12)
    ax.set_title("Batch Throughput by Configuration", fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(df["config"], rotation=30, ha="right", fontsize=10)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, f"throughput.{fmt}"), dpi=150)
    plt.close(fig)
    print(f"  [plot] throughput.{fmt}")


def plot_ctx_switches(df: pd.DataFrame, output_dir: str, fmt: str = "png") -> None:
    """Bar chart of context switches."""
    fig, ax = plt.subplots(figsize=(10, 5))
    x = np.arange(len(df))
    ax.bar(x, df["ctx_switches"], color="#C44E52", edgecolor="white", linewidth=0.5)
    ax.set_xlabel("Configuration", fontsize=12)
    ax.set_ylabel("Context Switches", fontsize=12)
    ax.set_title("Context Switches by Configuration", fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(df["config"], rotation=30, ha="right", fontsize=10)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, f"ctx_switches.{fmt}"), dpi=150)
    plt.close(fig)
    print(f"  [plot] ctx_switches.{fmt}")


def plot_worst_case(df: pd.DataFrame, output_dir: str, fmt: str = "png") -> None:
    """Bar chart of worst-case wait."""
    fig, ax = plt.subplots(figsize=(10, 5))
    x = np.arange(len(df))
    ax.bar(x, df["worst_case_us"], color="#8172B2", edgecolor="white", linewidth=0.5)
    ax.set_xlabel("Configuration", fontsize=12)
    ax.set_ylabel("Worst-case Wait (µs)", fontsize=12)
    ax.set_title("Worst-case Task Wait by Configuration", fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(df["config"], rotation=30, ha="right", fontsize=10)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, f"worst_case.{fmt}"), dpi=150)
    plt.close(fig)
    print(f"  [plot] worst_case.{fmt}")


def print_summary_table(df: pd.DataFrame) -> None:
    """Print a formatted summary table to stdout."""
    print("\n" + "=" * 90)
    print("  Ablation Study Summary")
    print("=" * 90)
    header = (f"{'Config':<20s} {'p50 (µs)':>10s} {'p99 (µs)':>10s} "
              f"{'Throughput':>12s} {'Ctx Sw':>8s} {'Worst (µs)':>12s}")
    print(header)
    print("-" * 90)
    for _, row in df.iterrows():
        print(f"{row['config']:<20s} {row['p50_us']:>10.1f} {row['p99_us']:>10.1f} "
              f"{row['throughput']:>12.0f} {row['ctx_switches']:>8d} "
              f"{row['worst_case_us']:>12.1f}")
    print("=" * 90)


# ------------------------------------------------------------------ #
#  CLI                                                                #
# ------------------------------------------------------------------ #

def main() -> None:
    p = argparse.ArgumentParser(
        description="Plot BurstSched ablation study results."
    )
    p.add_argument("--input", "-i", default="ablation_results.json",
                   help="Input JSON file (default: ablation_results.json)")
    p.add_argument("--output-dir", "-d", default=".",
                   help="Directory for output charts (default: .)")
    p.add_argument("--format", "-f", default="png",
                   choices=["png", "pdf", "svg"],
                   help="Chart format (default: png)")
    args = p.parse_args()

    if not _HAS_DEPS:
        print("ERROR: numpy, pandas, and matplotlib are required.\n"
              "Install with: pip install numpy pandas matplotlib",
              file=sys.stderr)
        sys.exit(1)

    input_path = os.path.join(os.path.dirname(__file__), args.input)
    if not os.path.exists(input_path):
        print(f"ERROR: {input_path} not found.\n"
              f"Run 'python benchmarks/run_ablation.py --mock' first.",
              file=sys.stderr)
        sys.exit(1)

    output_dir = os.path.join(os.path.dirname(__file__), args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    print("[plot] Loading results from:", input_path)
    df = load_results(input_path)

    print("[plot] Generating charts...")
    plot_latency(df, output_dir, args.format)
    plot_throughput(df, output_dir, args.format)
    plot_ctx_switches(df, output_dir, args.format)
    plot_worst_case(df, output_dir, args.format)

    print_summary_table(df)

    print(f"\n[plot] Charts saved to: {output_dir}")


if __name__ == "__main__":
    main()
