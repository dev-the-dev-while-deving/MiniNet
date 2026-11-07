#!/usr/bin/env python3
"""charts.py: draw the chaos-test results as charts for the report (T25).

    ~/venvs/fortress/bin/python scripts/charts.py

Reads docs/results/results.csv (written by chaos_test.py) and writes:
    docs/results/recovery_by_failure.png   recovery time per failure, reactive vs proactive
    docs/results/loss_by_failure.png       packets lost per failure, reactive vs proactive
Each bar is the median of the runs (one slow run can't hide the typical value); the thin line on it spans the fastest to the slowest run, so
the chart shows how repeatable a number is, not just its average.
"""
import csv
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")                    # no display in the VM: draw straight to files
import matplotlib.pyplot as plt

RESULTS = Path(__file__).resolve().parent.parent / "docs" / "results"
COLORS = {"reactive": "#4C72B0", "proactive": "#DD8452"}


def load():
    rows = list(csv.DictReader(open(RESULTS / "results.csv")))
    for r in rows:
        r["recovery_ms"] = float(r["recovery_s"]) * 1000
        r["lost"] = int(r["lost"])
    return rows


def grouped_bars(rows, field, ylabel, title, out):
    failures = list(dict.fromkeys(r["failure"] for r in rows))
    modes = list(dict.fromkeys(r["mode"] for r in rows))
    width = 0.8 / len(modes)
    fig, ax = plt.subplots(figsize=(9, 4.5))
    for i, mode in enumerate(modes):
        means, lows, highs = [], [], []
        for f in failures:
            vals = [r[field] for r in rows if r["failure"] == f and r["mode"] == mode] or [0]
            m = statistics.median(vals)
            means.append(m)
            lows.append(m - min(vals))
            highs.append(max(vals) - m)
        xs = [x + i * width - 0.4 + width / 2 for x in range(len(failures))]
        ax.bar(xs, means, width, label=mode, color=COLORS.get(mode))
        ax.errorbar(xs, means, yerr=[lows, highs], fmt="none", ecolor="black", capsize=3, linewidth=1)
    ax.set_xticks(range(len(failures)))
    ax.set_xticklabels(failures, rotation=12, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(title="controller mode")
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def main():
    rows = load()
    runs = max(int(r["run"]) for r in rows)
    a = grouped_bars(rows, "recovery_ms", "recovery time (ms)",
                     f"Recovery time per failure (median of {runs} runs, line = fastest to slowest)",
                     RESULTS / "recovery_by_failure.png")
    b = grouped_bars(rows, "lost", "pings lost (at 100 pings/s)",
                     f"Packets lost per failure (median of {runs} runs)", RESULTS / "loss_by_failure.png")
    print(f"charts: {a.name}, {b.name}")


if __name__ == "__main__":
    main()
