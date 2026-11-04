"""One number per index: gem5's Scalar, Value, and Average stats."""

import math
import operator
import statistics
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Union
from warnings import warn

from .align import align, combine_units
from .base import Index, Stat, safe_div, safe_floordiv, safe_mod

Number = Union[int, float]

OPERATORS: Dict[str, Callable[[float, float], float]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": safe_div,
    "//": safe_floordiv,
    "%": safe_mod,
    "**": operator.pow,
}


def reduce_numbers(how: str, values: List[float], weights: Optional[List[float]] = None) -> float:
    if how == "sum":
        return math.fsum(values)
    if how == "mean":
        return math.fsum(values) / len(values)
    if how == "min":
        return min(values)
    if how == "max":
        return max(values)
    if how == "gmean":
        if any(v <= 0 for v in values):
            warn("gmean of values <= 0 is undefined; returning nan.")
            return math.nan
        return statistics.geometric_mean(values)
    if how == "weighted_mean":
        total = math.fsum(weights)
        return safe_div(math.fsum(v * w for v, w in zip(values, weights)), total)
    raise ValueError(f"Unknown reduction '{how}'.")


class Scalar(Stat):
    """A single number per index.

    Arithmetic (`+ - * / // % **`, with numbers on either side) aligns the
    two operands by index or by run (see `ungem5.stats.stats.align`).
    Operators require exact alignment; the methods `add`, `sub`, `mul`, and
    `div` take `how="inner"` to keep only the shared indexes.
    Division by zero gives `inf` or `nan`, never an exception.
    """

    kind = "scalar"
    plots = ("bar", "grouped_bar", "line", "normalized_bar", "scatter")
    reductions = ("sum", "mean", "gmean", "min", "max", "weighted_mean")
    zero = 0.0

    @classmethod
    def value_from_block(cls, block) -> float:
        values = block.fields.get(None)
        if values is None or len(values) != 1:
            raise ValueError(
                f"'{block.name}' at '{block.owner}' is not a single value "
                f"(fields: {sorted(map(str, block.fields))})."
            )
        return values[0]

    # Arithmetic -----------------------------------------------------------

    def _binary(
        self,
        other: Any,
        symbol: str,
        how: str = "strict",
        reflected: bool = False,
        fill_value: float = 0.0,
    ) -> "Stat":
        function = OPERATORS[symbol]
        if isinstance(other, Stat):
            if not isinstance(other, Scalar):
                return NotImplemented
            pairs = align(self, other, how, fill_value)
            data = OrderedDict(
                (index, function(b, a) if reflected else function(a, b))
                for index, a, b in pairs
            )
            unit = combine_units(self.unit, other.unit, symbol)
            name = f"({self.name} {symbol} {other.name})"
        elif isinstance(other, (int, float)):
            data = OrderedDict(
                (index, function(other, value) if reflected else function(value, other))
                for index, value in self.items()
            )
            unit = self.unit if symbol in ("+", "-", "*", "/", "//", "%") else None
            name = (
                f"({other} {symbol} {self.name})"
                if reflected
                else f"({self.name} {symbol} {other})"
            )
        else:
            return NotImplemented
        return self._new(data, name=name, unit=unit, description=f"Computed as {name}")

    def __add__(self, other):
        return self._binary(other, "+")

    def __radd__(self, other):
        return self._binary(other, "+", reflected=True)

    def __sub__(self, other):
        return self._binary(other, "-")

    def __rsub__(self, other):
        return self._binary(other, "-", reflected=True)

    def __mul__(self, other):
        return self._binary(other, "*")

    def __rmul__(self, other):
        return self._binary(other, "*", reflected=True)

    def __truediv__(self, other):
        return self._binary(other, "/")

    def __rtruediv__(self, other):
        return self._binary(other, "/", reflected=True)

    def __floordiv__(self, other):
        return self._binary(other, "//")

    def __rfloordiv__(self, other):
        return self._binary(other, "//", reflected=True)

    def __mod__(self, other):
        return self._binary(other, "%")

    def __rmod__(self, other):
        return self._binary(other, "%", reflected=True)

    def __pow__(self, other):
        return self._binary(other, "**")

    def __rpow__(self, other):
        return self._binary(other, "**", reflected=True)

    def __neg__(self):
        return self._new(OrderedDict((i, -v) for i, v in self.items()), name=f"(-{self.name})")

    # `how`: "strict" (the operators' rule), "inner" (shared indexes only) or
    # "outer" (every index of either side, a missing value being fill_value).
    def add(self, other, how: str = "strict", fill_value: float = 0.0):
        return self._binary(other, "+", how, fill_value=fill_value)

    def sub(self, other, how: str = "strict", fill_value: float = 0.0):
        return self._binary(other, "-", how, fill_value=fill_value)

    def mul(self, other, how: str = "strict", fill_value: float = 0.0):
        return self._binary(other, "*", how, fill_value=fill_value)

    def div(self, other, how: str = "strict", fill_value: float = 0.0):
        return self._binary(other, "/", how, fill_value=fill_value)

    def scale(self, factor: float, unit: Optional[str] = None) -> "Scalar":
        """Multiply every value by `factor` and set the unit (for example
        ticks to cycles with `1 / ticks_per_cycle` and `unit="Cycle"`)."""
        data = OrderedDict((index, value * factor) for index, value in self.items())
        return self._new(data, unit=unit)

    def apply(self, function: Callable[[float], float], name: Optional[str] = None) -> "Scalar":
        """Apply a function to every value."""
        data = OrderedDict((index, function(value)) for index, value in self.items())
        return self._new(data, name=name or f"{function.__name__}({self.name})")

    def relative_to(self, **baseline: Any) -> "Scalar":
        """Divide every value by the baseline run's value at the same index.

        `ipc.relative_to(config="baseline")` divides each run's value by the
        value of the run that matches it in every label except `config`, with
        `config` equal to "baseline"; the baseline itself becomes 1.
        """
        data = OrderedDict()
        for index, value in self.items():
            labels = tuple(
                (k, baseline[k] if k in baseline else v) for k, v in index.labels
            )
            missing = set(baseline) - {k for k, _ in index.labels}
            if missing:
                raise KeyError(f"'{self.name}' has no label(s) {sorted(missing)}.")
            reference = Index(labels, index.owner, index.dims, index.occurrence, index.path)
            if reference not in self:
                raise KeyError(
                    f"No baseline for {dict(index.labels)} at '{index.owner}': "
                    f"no run with labels {dict(labels)}."
                )
            data[index] = safe_div(value, self[reference])
        baseline_text = ", ".join(f"{k}={v}" for k, v in baseline.items())
        return self._new(data, name=f"{self.name} / [{baseline_text}]", unit="Ratio")

    # Reduction and export -------------------------------------------------

    def _reduce_values(self, how, values, weights):
        return reduce_numbers(how, values, weights)

    def rows(self) -> List[Dict[str, Any]]:
        columns = self._index_columns()
        rows = []
        for index, value in self.items():
            row = self._index_row(index, columns)
            row["stat"] = self.name
            row["value"] = value
            row["unit"] = self.unit
            rows.append(row)
        return rows

    def to_frame(self, wide: bool = False):
        """Long by default; `wide=True` puts runs in rows and owners in columns."""
        frame = super().to_frame()
        if not wide or frame.empty:
            return frame
        label_names, dim_names, _, with_occurrence = self._index_columns()
        columns = ["owner"] + dim_names + (["occurrence"] if with_occurrence else [])
        return frame.pivot_table(
            index=label_names or None,
            columns=columns,
            values="value",
            aggfunc="first",
            sort=False,
        )

    def to_series(self):
        """A pandas Series indexed by every index field."""
        frame = super().to_frame()
        keys = [c for c in frame.columns if c not in ("stat", "value", "unit")]
        return frame.set_index(keys)["value"].rename(self.name)
