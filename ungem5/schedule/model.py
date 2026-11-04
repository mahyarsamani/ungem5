"""A learned estimate of a job's peak memory, for jobs never run before.

From the memory history's complete runs, `PeakModel.fit` learns the log of
the peak from what describes a job: its features (`Job.features`,
flattened), its group, and the user's guess. It uses scikit-learn's
gradient-boosted trees with the quantile loss (QUANTILE), which take
missing values and categories as they come.

A conformal correction then makes the reservations honest: the model is
fit on all but the newest HOLDOUT of the runs, and the correction is the
COVERAGE quantile of peak/prediction over those held out, so about
COVERAGE of similar new jobs fit in what the model reserves.

Needs scikit-learn (`pip install "ungem5[model]"`); without it, or with
fewer than MIN_RUNS complete runs, there is no model and the history's
other estimates apply.
"""

import importlib.util
import math

from dataclasses import dataclass

MIN_RUNS = 50
QUANTILE = 0.9
COVERAGE = 0.95
HOLDOUT = 0.2
MIN_HOLDOUT = 10
# NOTE: At most this many feature columns (the most common), and this many
# categories per column (the rest share one), as the trees allow.
MAX_COLUMNS = 200
MAX_CATEGORIES = 250


def available() -> bool:
    return importlib.util.find_spec("sklearn") is not None


def flatten(value, prefix: str = "") -> dict:
    """A JSON value as {path: number or string}: dicts by key, lists by
    index plus their length; numbers stay numbers, all else is text."""
    if isinstance(value, dict):
        flat = {}
        for key, item in value.items():
            flat.update(flatten(item, f"{prefix}{key}."))
        return flat
    if isinstance(value, (list, tuple)):
        flat = {f"{prefix}#len": float(len(value))}
        for index, item in enumerate(value):
            flat.update(flatten(item, f"{prefix}{index}."))
        return flat
    name = prefix.rstrip(".") or "value"
    if isinstance(value, bool) or value is None:
        return {name: str(value)}
    if isinstance(value, (int, float)):
        return {name: float(value)}
    return {name: str(value)}


@dataclass(frozen=True)
class Run:
    features: dict
    group: str
    guess: int | None
    peak: int


def _describe(features: dict, group: str, guess: int | None) -> dict:
    row = flatten(features)
    row["@group"] = group
    row["@guess"] = math.log(guess) if guess else None
    return row


class PeakModel:
    def __init__(self, columns, categories, trees, correction, runs) -> None:
        self._columns = columns
        # NOTE: Per categorical column, its categories' codes.
        self._categories = categories
        self._trees = trees
        self._correction = correction
        self._runs = runs

    def _matrix(self, rows: list[dict]):
        import numpy

        matrix = numpy.full((len(rows), len(self._columns)), numpy.nan)
        for i, row in enumerate(rows):
            for j, column in enumerate(self._columns):
                value = row.get(column)
                if value is None:
                    continue
                codes = self._categories.get(column)
                if codes is None:
                    if isinstance(value, float):
                        matrix[i, j] = value
                else:
                    matrix[i, j] = codes.get(str(value), len(codes))
        return matrix

    @classmethod
    def fit(cls, runs: list[Run]) -> "PeakModel | None":
        """A model of `runs` (complete ones, oldest first), or None if
        there are too few or scikit-learn is missing."""
        if len(runs) < MIN_RUNS or not available():
            return None
        import numpy

        from sklearn.ensemble import HistGradientBoostingRegressor

        rows = [_describe(r.features, r.group, r.guess) for r in runs]
        counts: dict[str, int] = {}
        for row in rows:
            for column, value in row.items():
                if value is not None:
                    counts[column] = counts.get(column, 0) + 1
        columns = sorted(counts, key=lambda c: (-counts[c], c))[:MAX_COLUMNS]
        categories = {}
        for column in columns:
            values = [row[column] for row in rows if row.get(column) is not None]
            if all(isinstance(value, float) for value in values):
                continue
            seen: dict[str, int] = {}
            for value in values:
                seen[str(value)] = seen.get(str(value), 0) + 1
            common = sorted(seen, key=lambda v: (-seen[v], v))[:MAX_CATEGORIES]
            categories[column] = {value: code for code, value in enumerate(common)}

        model = cls(columns, categories, None, 1.0, len(runs))
        matrix = model._matrix(rows)
        target = numpy.log([r.peak for r in runs])
        holdout = max(MIN_HOLDOUT, int(len(runs) * HOLDOUT))
        trees = HistGradientBoostingRegressor(
            loss="quantile",
            quantile=QUANTILE,
            categorical_features=[c in categories for c in columns],
            max_iter=200,
            random_state=0,
        )
        trees.fit(matrix[:-holdout], target[:-holdout])
        predicted = numpy.exp(trees.predict(matrix[-holdout:]))
        ratios = numpy.array([r.peak for r in runs[-holdout:]]) / predicted
        model._trees = trees
        model._correction = float(numpy.quantile(ratios, COVERAGE))
        return model

    def predict(self, features: dict, group: str, guess: int | None) -> int:
        """The memory to reserve for a job: the model's QUANTILE of its
        peak, times the conformal correction."""
        import numpy

        matrix = self._matrix([_describe(features, group, guess)])
        return int(numpy.exp(self._trees.predict(matrix)[0]) * self._correction)

    def describe(self) -> str:
        return (
            f"learned from {self._runs} run(s), corrected x"
            f"{self._correction:.2f} to cover {COVERAGE:.0%} of the newest"
        )
