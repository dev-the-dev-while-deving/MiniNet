"""anomaly.py: turn one host's traffic features into a 0..1 "how unusual is this" number (T19).

The model is an **Isolation Forest**, trained only on normal traffic (detection/train_model.py).
It needs no attack examples: it learns the shape of normal behaviour and reports how far a
sample sits outside it. That matters here, because we have plenty of normal traffic and only
two attack scenarios, so a classifier trained on labelled attacks would only ever recognise
those two.

Reading the number (what to say in the viva):
    0.0   indistinguishable from the baseline
    1.0   as far outside the baseline as the most unusual thing seen during training
          (anything beyond that is also 1.0)
The threat engine (T20) turns this into at most 30 of the 100 points, so the model can raise
suspicion on its own but can never declare a breach without a second, explainable signal.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = ROOT / "detection" / "data" / "model.joblib"


def load(path=DEFAULT_MODEL):
    """The trained model bundle, or None if it hasn't been trained yet (the threat engine then
    runs on its rules alone, which is also the honest fallback if the model misbehaves)."""
    import joblib
    path = Path(path)
    if not path.exists():
        return None
    return joblib.load(path)


def decision(bundle, rows):
    """sklearn's raw decision values for feature dicts: >= 0 means "looks normal"."""
    x = [[float(row[f]) for f in bundle["features"]] for row in rows]
    return list(bundle["pipeline"].decision_function(x))


def score_many(bundle, rows):
    """0..1 anomaly score per feature dict (see the scale in this module's docstring)."""
    ref = max(bundle["ref"], 1e-9)
    return [min(max(-d / ref, 0.0), 1.0) for d in decision(bundle, rows)]


def score(bundle, row):
    """0..1 anomaly score for a single host's feature dict."""
    return score_many(bundle, [row])[0]
