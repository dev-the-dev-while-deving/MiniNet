#!/usr/bin/env python3
"""charts.py: draw the chaos-test results as charts for the report (T25).

    ~/venvs/fortress/bin/python scripts/charts.py

Reads docs/results/results.csv (written by chaos_test.py) and writes:
    docs/results/recovery_by_failure.png   recovery time per failure, reactive vs proactive
    docs/results/loss_by_failure.png       packets lost per failure, reactive vs proactive
    docs/results/detection_to_block.png    insider runs: first alert, Breach score, block
                                           (from docs/results/detection.csv, if it exists)
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


def detection_chart(out):
    """One line per insider run, with a marker where each stage happened: first IDS alert,
    Breach score, blocked. (The block usually lands in the same engine cycle as the Breach,
    so the two markers sit on top of each other; that is the point: no human delay.)"""
    rows = list(csv.DictReader(open(RESULTS / "detection.csv")))
    stages = [("alert_s", "first IDS alert", "o", "#4C72B0", 9),
              ("breach_s", "score 70+ (Breach)", "s", "#DD8452", 11),
              ("block_s", "blocked at its own port", "x", "#C44E52", 12)]
    end = max(float(r["block_s"] or 0) for r in rows)
    fig, ax = plt.subplots(figsize=(9, 0.55 * len(rows) + 2.2))
    for i, r in enumerate(rows):
        last = max(float(r[f]) for f, *_ in stages if r[f])
        ax.plot([0, last], [i, i], color="#999999", linewidth=1, zorder=1)
        for field, label, marker, color, size in stages:
            if r[field]:
                ax.plot(float(r[field]), i, marker=marker, color=color, markersize=size, linestyle="none",
                        markeredgewidth=2, label=label if i == 0 else None, zorder=2)
        ax.text(last + 0.25, i, f"blocked after {last:.1f} s, score {r['score']}", va="center", fontsize=9)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([f"run {r['run']}" for r in rows])
    ax.invert_yaxis()
    ax.set_xlim(0, end + 3.5)
    ax.set_xlabel("seconds since the port sweep started")
    ax.set_title("Insider attack: detection to block, per run")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=3, frameon=False)
    ax.grid(axis="x", alpha=0.3)
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
    names = [a.name, b.name]
    if (RESULTS / "detection.csv").exists():
        names.append(detection_chart(RESULTS / "detection_to_block.png").name)
    print("charts: " + ", ".join(names))


if __name__ == "__main__":
    main()
