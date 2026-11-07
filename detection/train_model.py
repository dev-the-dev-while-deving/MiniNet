#!/usr/bin/env python3
"""train_model.py: train the Isolation Forest on normal traffic and report its error rate (T19).

    ./scripts/vm/vm.sh '~/venvs/fortress/bin/python detection/train_model.py'
    ./scripts/vm/vm.sh '~/venvs/fortress/bin/python detection/train_model.py --evaluate-only'

Input  : the feature CSV from detection/baseline_traffic.py (T18), normal traffic only.
Output : detection/data/model.joblib, plus a printed evaluation.

How it is evaluated, and why that is the honest test: the rows are split into a training part
and a held-out part the model never sees. Both are normal, so **every** alarm on the held-out
part is a false alarm. The false-positive rate is therefore measurable without a single attack
sample, and the acceptance target is under 5%.
"""
import argparse
import csv
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import anomaly
from features import FEATURES

DEFAULT_CSV = Path("/var/log/fortress/collector/baseline.csv")


def read_rows(csv_file):
    rows = list(csv.DictReader(open(csv_file)))
    normal = [r for r in rows if r.get("label", "normal") == "normal"]
    if not normal:
        sys.exit(f"{csv_file} has no rows labelled normal; run detection/baseline_traffic.py first")
    return normal


def train(rows, contamination, test_fraction, seed=0):
    """Fit scaler + Isolation Forest on a training split. Returns (bundle, held-out rows)."""
    import numpy as np
    from sklearn.ensemble import IsolationForest
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    rows = list(rows)
    random.Random(seed).shuffle(rows)
    cut = max(1, int(len(rows) * (1 - test_fraction)))
    train_rows, test_rows = rows[:cut], rows[cut:]
    x = [[float(r[f]) for f in FEATURES] for r in train_rows]

    # StandardScaler first: bytes/s is in the thousands and distinct ports in the tens, and
    # without scaling the forest would effectively only look at the biggest numbers.
    # contamination: the share of the baseline we accept might not be normal after all. It also
    # sets where sklearn puts the "normal / not normal" line (decision_function == 0).
    pipeline = make_pipeline(StandardScaler(),
                             IsolationForest(n_estimators=200, contamination=contamination,
                                             random_state=seed))
    pipeline.fit(x)
    bundle = {"pipeline": pipeline, "features": FEATURES, "ref": 1.0,
              "rows": len(train_rows), "contamination": contamination,
              "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    # The 0..1 scale: 1.0 = as unusual as the most unusual sample in the baseline (anomaly.py).
    worst = min(anomaly.decision(bundle, train_rows))
    bundle["ref"] = abs(min(worst, -1e-6))
    return bundle, test_rows


def evaluate(bundle, rows, label="held-out normal"):
    """Print the false-positive rate and the score spread on normal rows. Returns the rate."""
    values = anomaly.decision(bundle, rows)
    scores = anomaly.score_many(bundle, rows)
    alarms = [i for i, d in enumerate(values) if d < 0]
    rate = len(alarms) / len(rows) if rows else 0.0
    print(f"{label}: {len(rows)} rows, {len(alarms)} flagged as anomalous "
          f"-> false-positive rate {rate * 100:.1f}%")
    print(f"  anomaly score: mean {sum(scores) / len(scores):.3f}, "
          f"max {max(scores):.3f}, over 0.5: {sum(s > 0.5 for s in scores)} rows")
    worst = sorted(zip(scores, rows), key=lambda p: -p[0])[:3]
    for s, row in worst:
        detail = " ".join(f"{f}={row[f]}" for f in ("packets_per_s", "distinct_dst_ports", "failed_handshakes"))
        print(f"  highest: {row['host']} score {s:.2f}  {detail}")
    return rate


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--csv", default=str(DEFAULT_CSV), help=f"baseline feature CSV (default {DEFAULT_CSV})")
    p.add_argument("--model", default=str(anomaly.DEFAULT_MODEL))
    p.add_argument("--contamination", type=float, default=0.02,
                   help="share of the baseline that may not be normal (default 0.02)")
    p.add_argument("--test-fraction", type=float, default=0.3)
    p.add_argument("--max-fp-rate", type=float, default=0.05, help="acceptance target (default 5%%)")
    p.add_argument("--evaluate-only", action="store_true", help="evaluate the saved model, don't retrain")
    a = p.parse_args()

    rows = read_rows(a.csv)
    if a.evaluate_only:
        bundle = anomaly.load(a.model)
        if bundle is None:
            sys.exit(f"no model at {a.model}; train one first")
        rate = evaluate(bundle, rows, "all normal rows")
    else:
        import joblib
        bundle, test_rows = train(rows, a.contamination, a.test_fraction)
        print(f"trained on {bundle['rows']} normal rows, {len(FEATURES)} features, "
              f"contamination {a.contamination}")
        rate = evaluate(bundle, test_rows)
        Path(a.model).parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(bundle, a.model)
        print(f"saved {a.model}")
    ok = rate <= a.max_fp_rate
    print(f"{'OK' if ok else 'TOO HIGH'}: false-positive rate {rate * 100:.1f}% "
          f"(target under {a.max_fp_rate * 100:.0f}%)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
