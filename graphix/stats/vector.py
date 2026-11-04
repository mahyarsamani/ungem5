"""Named or indexed elements per index: gem5's Vector stats and Vector2d rows.

A missing element counts as zero: gem5 drops zero elements of `nozero`
vectors, so the element set can change from one dump to the next.
gem5's `::total` line is not read; `total()` recomputes it.
"""

from collections import OrderedDict
from typing import Any, Dict, List, Optional

from .align import align, combine_units
from .base import Stat
from .scalar import OPERATORS, Scalar, reduce_numbers

SUMMARY_FIELDS = {"total"}


class Vector(Stat):
    """A mapping element -> number per index."""

    kind = "vector"
    plots = ("stacked_bar", "share_bar", "grouped_bar", "heatmap", "pie")
    reductions = ("sum", "mean", "gmean", "min", "max", "weighted_mean")
    zero: Dict[str, float] = {}

    @classmethod
    def value_from_block(cls, block) -> Dict[str, float]:
        elements = OrderedDict()
        for field, values in block.fields.items():
            if field is None:
                # A one-line vector names no elements, so its positions are
                # the element names; a size-1 vector prints a bare value.
                for position, value in enumerate(values):
                    elements[str(position)] = value
            elif field not in SUMMARY_FIELDS:
                elements[field] = values[0]
        return elements

    def elements(self) -> List[str]:
        seen = OrderedDict()
        for value in self.values():
            seen.update((element, None) for element in value)
        return list(seen)

    def element(self, name: str) -> Scalar:
        """One element as a Scalar (missing elements are zero)."""
        data = OrderedDict((index, value.get(name, 0.0)) for index, value in self.items())
        return Scalar(f"{self.name}::{name}", data, self.unit, self.description)

    def total(self) -> Scalar:
        data = OrderedDict((index, sum(value.values())) for index, value in self.items())
        return Scalar(f"{self.name}::total", data, self.unit, self.description)

    def share(self) -> "Vector":
        """Each element as a fraction of its index's total."""
        data = OrderedDict()
        for index, value in self.items():
            total = sum(value.values())
            data[index] = OrderedDict(
                (element, (v / total) if total else float("nan"))
                for element, v in value.items()
            )
        return self._new(data, name=f"share({self.name})", unit="Ratio")

    def keep_elements(self, *names: str) -> "Vector":
        data = OrderedDict(
            (index, OrderedDict((n, value.get(n, 0.0)) for n in names))
            for index, value in self.items()
        )
        return self._new(data)

    # Arithmetic -----------------------------------------------------------

    def _binary(
        self,
        other: Any,
        symbol: str,
        how: str = "strict",
        reflected: bool = False,
        fill_value: float = 0.0,
    ):
        function = OPERATORS[symbol]

        def apply(a, b):
            return function(b, a) if reflected else function(a, b)

        if isinstance(other, Vector):
            data = OrderedDict()
            # A missing vector is one with no element: every element is 0.
            for index, a, b in align(self, other, how, {}, {}):
                names = list(OrderedDict.fromkeys(list(a) + list(b)))
                data[index] = OrderedDict(
                    (n, apply(a.get(n, 0.0), b.get(n, 0.0))) for n in names
                )
            unit = combine_units(self.unit, other.unit, symbol)
            name = f"({self.name} {symbol} {other.name})"
        elif isinstance(other, Scalar):
            data = OrderedDict(
                (index, OrderedDict((n, apply(v, b)) for n, v in a.items()))
                for index, a, b in align(self, other, how, {}, fill_value)
            )
            unit = combine_units(self.unit, other.unit, symbol)
            name = f"({self.name} {symbol} {other.name})"
        elif isinstance(other, (int, float)):
            data = OrderedDict(
                (index, OrderedDict((n, apply(v, other)) for n, v in value.items()))
                for index, value in self.items()
            )
            unit = self.unit
            name = f"({self.name} {symbol} {other})"
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

    # `how` as in Scalar; with "outer", a missing vector has no element (all
    # zero) and a missing scalar is fill_value.
    def add(self, other, how: str = "strict", fill_value: float = 0.0):
        return self._binary(other, "+", how, fill_value=fill_value)

    def sub(self, other, how: str = "strict", fill_value: float = 0.0):
        return self._binary(other, "-", how, fill_value=fill_value)

    def mul(self, other, how: str = "strict", fill_value: float = 0.0):
        return self._binary(other, "*", how, fill_value=fill_value)

    def div(self, other, how: str = "strict", fill_value: float = 0.0):
        return self._binary(other, "/", how, fill_value=fill_value)

    def scale(self, factor: float, unit: Optional[str] = None) -> "Vector":
        """Multiply every element by `factor` and set the unit."""
        data = OrderedDict(
            (index, OrderedDict((n, v * factor) for n, v in value.items()))
            for index, value in self.items()
        )
        return self._new(data, unit=unit)

    # Reduction and export -------------------------------------------------

    def _reduce_values(self, how, values, weights):
        names = list(OrderedDict.fromkeys(n for value in values for n in value))
        return OrderedDict(
            (n, reduce_numbers(how, [value.get(n, 0.0) for value in values], weights))
            for n in names
        )

    def value_columns(self, **kwargs) -> List[str]:
        return ["stat", self.element_dim, "value", "unit"]

    def rows(self) -> List[Dict[str, Any]]:
        columns = self._index_columns()
        element_dim = self.element_dim
        rows = []
        for index, value in self.items():
            base = self._index_row(index, columns)
            for element, number in value.items():
                row = dict(base)
                row["stat"] = self.name
                row[element_dim] = element
                row["value"] = number
                row["unit"] = self.unit
                rows.append(row)
        return rows

    def matrix(self, rows: Optional[List[str]] = None):
        """A DataFrame with one row per index and one column per element.

        `rows` picks the index fields that form the row key; by default all
        labels, the owner, and the dims.
        Missing elements are 0.
        """
        frame = self.to_frame()
        if frame.empty:
            return frame
        if rows is None:
            rows = [c for c in frame.columns if c not in ("stat", self.element_dim, "value", "unit")]
        table = frame.pivot_table(
            index=rows, columns=self.element_dim, values="value", aggfunc="sum", sort=False
        )
        return table.reindex(columns=self.elements()).fillna(0.0)
