"""The shared core of every stat class.

A stat is one family of values (for example, every `numCycles`) across runs,
owners, and dimensions.
Each value sits under an `Index`:
- `labels`: the run's labels, such as workload and configuration;
- `owner`: the SimObject that owns the value, in canonical form when an
  `OwnerScheme` is applied, and `path`, its raw SimObject path;
- `dims`: dimension values taken from the stat name (`event`, `thread`, ...);
- `occurrence`: 0, or the repeat number of a name gem5 printed more than
  once in one dump.
Subclasses define the value type and the operations that make sense for it.
"""

import math
import re
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, Union
from warnings import warn

Pairs = Tuple[Tuple[str, Any], ...]

# Units whose values add up meaningfully across owners.
ADDITIVE_UNITS = {"Count", "Byte", "Tick", "Cycle", "Second", "Bit", "Joule"}


@dataclass(frozen=True)
class Index:
    labels: Pairs
    owner: str
    dims: Pairs = ()
    occurrence: int = 0
    path: str = field(default="", compare=False)

    def label(self, name: str, default: Any = None) -> Any:
        return dict(self.labels).get(name, default)

    def dim(self, name: str, default: Any = None) -> Any:
        return dict(self.dims).get(name, default)

    def __str__(self) -> str:
        parts = [f"{k}={v}" for k, v in self.labels if k != "run"]
        parts.append(f"owner={self.owner or '<root>'}")
        parts += [f"{k}={v}" for k, v in self.dims]
        if self.occurrence:
            parts.append(f"occurrence={self.occurrence}")
        return "[" + ", ".join(parts) + "]"

    def get(self, name: str, default: Any = None) -> Any:
        """A label, a dim, `owner`, `path`, or `occurrence`, by name."""
        if name in ("owner", "path", "occurrence"):
            return getattr(self, name)
        for pairs in (self.dims, self.labels):
            for key, value in pairs:
                if key == name:
                    return value
        return default


Matcher = Union[Any, Sequence[Any], Callable[[Any], bool]]


class Stat:
    """Base class: a mapping from `Index` to a value.

    Class attributes describe the family to readers and exporters:
    - `kind`: a short type name;
    - `plots`: the plot kinds this class suits, best first; a spec can
      override them per family;
    - `reductions`: the reductions `reduce` accepts.
    """

    kind = "stat"
    plots: Tuple[str, ...] = ()
    reductions: Tuple[str, ...] = ()

    def __init__(
        self,
        name: str,
        data: Dict[Index, Any],
        unit: Optional[str] = None,
        description: Optional[str] = None,
        spec: Any = None,
        selection: Optional[str] = None,
    ) -> None:
        self._name = name
        self._data = OrderedDict(data)
        self._unit = unit
        self._description = description
        self._spec = spec
        self._selection = selection

    # Basic access ---------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def unit(self) -> Optional[str]:
        return self._unit

    @property
    def description(self) -> Optional[str]:
        return self._description

    @property
    def spec(self) -> Any:
        return self._spec

    @property
    def plot_kinds(self) -> Tuple[str, ...]:
        if self._spec is not None and self._spec.plots is not None:
            return self._spec.plots
        return self.plots

    @property
    def element_dim(self) -> str:
        if self._spec is not None and self._spec.element_dim:
            return self._spec.element_dim
        return "element"

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterator[Tuple[Index, Any]]:
        return iter(self._data.items())

    def __contains__(self, index: Index) -> bool:
        return index in self._data

    def __getitem__(self, index: Index) -> Any:
        return self._data[index]

    def items(self):
        return self._data.items()

    def indexes(self) -> List[Index]:
        return list(self._data)

    def values(self) -> List[Any]:
        return list(self._data.values())

    def owners(self) -> List[str]:
        return _unique(index.owner for index in self._data)

    def labels(self, name: Optional[str] = None) -> List[Any]:
        """Label names, or the distinct values of one label."""
        if name is None:
            return _unique(k for index in self._data for k, _ in index.labels)
        return _unique(index.label(name) for index in self._data)

    def dims(self, name: Optional[str] = None) -> List[Any]:
        """Dim names, or the distinct values of one dim."""
        if name is None:
            return _unique(k for index in self._data for k, _ in index.dims)
        return _unique(index.dim(name) for index in self._data)

    def describe(self) -> str:
        """One paragraph: kind, unit, size, dims, and plot kinds."""
        lines = [
            f"{self._name} ({type(self).__name__}, unit: {self._unit or '-'})",
            f"  {self._description or 'no description'}",
            f"  {len(self._data)} values over {len(self.owners())} owner(s) "
            f"and {len(set(i.labels for i in self._data))} run(s)",
            f"  labels: {', '.join(self.labels()) or '-'}; "
            f"dims: {', '.join(self.dims()) or '-'}",
            f"  plot kinds: {', '.join(self.plot_kinds) or '-'}; "
            f"reductions: {', '.join(self.reductions) or '-'}",
        ]
        return "\n".join(lines)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}({self._name!r}, {len(self._data)} values, "
            f"unit={self._unit!r})"
        )

    # Building new stats ---------------------------------------------------

    def _new(
        self,
        data: Dict[Index, Any],
        name: Optional[str] = None,
        unit: Optional[str] = "__same__",
        selection: Optional[str] = "__same__",
        description: Optional[str] = "__same__",
    ) -> "Stat":
        return type(self)(
            self._name if name is None else name,
            data,
            self._unit if unit == "__same__" else unit,
            self._description if description == "__same__" else description,
            self._spec,
            self._selection if selection == "__same__" else selection,
        )

    def rename(self, name: str) -> "Stat":
        return self._new(dict(self._data), name=name)

    def with_unit(self, unit: Optional[str]) -> "Stat":
        return self._new(dict(self._data), unit=unit)

    def drop_labels(self, *names: str) -> "Stat":
        """Remove labels from every index (for example the unique `run`).

        Raises if two values would then share an index; select first, or
        reduce over the label instead.
        """
        data = OrderedDict()
        for index, value in self._data.items():
            labels = tuple((k, v) for k, v in index.labels if k not in names)
            data[_put(data, replace(index, labels=labels), self._name)] = value
        return self._new(data)

    def map_dim(self, dim: str, function: Callable[[Any, Index], Any]) -> "Stat":
        """Replace the values of one dim with `function(value, index)`."""
        data = {}
        for index, value in self._data.items():
            dims = tuple(
                (k, function(v, index) if k == dim else v) for k, v in index.dims
            )
            data[_put(data, replace(index, dims=dims), self._name)] = value
        return self._new(data)

    # Selection ------------------------------------------------------------

    def select(
        self,
        owner: Optional[str] = None,
        path: Optional[str] = None,
        **match: Matcher,
    ) -> "Stat":
        """Keep the values whose index matches every condition.

        `owner` and `path` take a glob (`"core*.l1d.cache"`) or, when wrapped
        in `re.compile`, a regex.
        Any other keyword names a label, a dim, or `occurrence`, and takes a
        value, a list or set of accepted values, or a predicate.
        """
        data = OrderedDict()
        for index, value in self._data.items():
            if owner is not None and not _match_path(index.owner, owner):
                continue
            if path is not None and not _match_path(index.path, path):
                continue
            if all(
                _match_value(index.get(key, _MISSING), wanted)
                for key, wanted in match.items()
            ):
                data[index] = value
        selection = owner if isinstance(owner, str) else self._selection
        return self._new(data, selection=selection)

    def drop(self, owner: Optional[str] = None, **match: Matcher) -> "Stat":
        """The complement of `select` with the same arguments."""
        kept = self.select(owner=owner, **match)
        data = OrderedDict(
            (index, value)
            for index, value in self._data.items()
            if index not in kept
        )
        return self._new(data)

    # Reduction ------------------------------------------------------------

    def reduce(
        self,
        how: Optional[str] = None,
        over: Union[str, Sequence[str]] = "owner",
        label: Optional[str] = None,
        weights: Optional["Stat"] = None,
    ) -> "Stat":
        """Combine values that differ only in the fields named by `over`.

        `how` defaults to the spec's `reduce`, or else the class's first
        reduction (`sum` for numbers, `merge` for histograms).
        `over` names `owner`, `occurrence`, dims, or labels.
        Reducing over `owner` gives the result a new owner, `label` or by
        default `"<how>(<last owner glob>)"`, so reduced and per-owner values
        can sit side by side in one export.
        Reducing over a dim or label removes it from the index.
        """
        if how is None:
            spec_default = self._spec.reduce if self._spec is not None else None
            how = spec_default or self.reductions[0]
        if how not in self.reductions:
            raise TypeError(
                f"{type(self).__name__} '{self._name}' supports reductions "
                f"{self.reductions}, not '{how}'."
            )
        over = (over,) if isinstance(over, str) else tuple(over)
        self._check_reduction_unit(how, over)
        owner_label = label or f"{how}({self._selection or '*'})"
        groups: Dict[Index, List[Tuple[Index, Any]]] = OrderedDict()
        for index, value in self._data.items():
            key = _reduced_index(index, over, owner_label)
            groups.setdefault(key, []).append((index, value))
        weight_values = None
        if weights is not None:
            weight_values = {index: value for index, value in weights.items()}
        data = OrderedDict()
        for key, members in groups.items():
            values = [value for _, value in members]
            member_weights = None
            if how == "weighted_mean":
                if weight_values is None:
                    raise ValueError("'weighted_mean' needs weights=<Scalar>.")
                try:
                    member_weights = [weight_values[index] for index, _ in members]
                except KeyError as missing:
                    raise KeyError(
                        f"No weight for {missing.args[0]} in '{weights.name}'."
                    ) from None
            data[key] = self._reduce_values(how, values, member_weights)
        return self._new(data)

    def _check_reduction_unit(self, how: str, over: Tuple[str, ...]) -> None:
        # Only a known ratio or rate warns: gem5 prints many counters with no
        # unit (`Unspecified`), and those add up.
        known = self._unit not in (None, "", "Unspecified")
        if how == "sum" and known and not _is_additive(self._unit):
            warn(
                f"Summing '{self._name}' with unit '{self._unit}' over "
                f"{', '.join(over)}; ratios and rates usually need "
                f"'mean' or 'weighted_mean'."
            )

    def _reduce_values(self, how: str, values: List[Any], weights: Optional[List[float]]) -> Any:
        raise NotImplementedError

    # Export ---------------------------------------------------------------

    def _index_columns(self) -> Tuple[List[str], List[str], bool, bool]:
        label_names = self.labels()
        dim_names = self.dims()
        with_path = any(index.path != index.owner for index in self._data)
        with_occurrence = any(index.occurrence for index in self._data)
        return label_names, dim_names, with_path, with_occurrence

    def _index_row(self, index: Index, columns) -> Dict[str, Any]:
        label_names, dim_names, with_path, with_occurrence = columns
        row = {name: index.label(name) for name in label_names}
        row["owner"] = index.owner
        if with_path:
            row["path"] = index.path
        for name in dim_names:
            row[name] = index.dim(name)
        if with_occurrence:
            row["occurrence"] = index.occurrence
        return row

    def rows(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def value_columns(self, **kwargs) -> List[str]:
        """The columns a row has after its index columns, for an empty
        export to keep its shape."""
        return ["stat", "value", "unit"]

    def to_frame(self, **kwargs):
        """A long pandas DataFrame, one row per number; an empty stat gives an
        empty frame with the usual columns."""
        import pandas as pd

        rows = self.rows(**kwargs)
        if not rows:
            return pd.DataFrame(columns=["owner", *self.value_columns(**kwargs)])
        return pd.DataFrame(rows)


_MISSING = object()


def _unique(values: Iterable[Any]) -> List[Any]:
    return list(OrderedDict.fromkeys(values))


def _match_path(value: str, pattern: Any) -> bool:
    if isinstance(pattern, re.Pattern):
        return pattern.fullmatch(value) is not None
    return fnmatchcase(value, pattern)


def _match_value(value: Any, wanted: Matcher) -> bool:
    if value is _MISSING:
        return False
    if callable(wanted) and not isinstance(wanted, type):
        return bool(wanted(value))
    if isinstance(wanted, re.Pattern):
        return wanted.fullmatch(str(value)) is not None
    if isinstance(wanted, (list, tuple, set, frozenset)):
        return value in wanted
    return value == wanted


def _reduced_index(index: Index, over: Tuple[str, ...], owner_label: str) -> Index:
    owner, path = index.owner, index.path
    occurrence = index.occurrence
    dims, labels = index.dims, index.labels
    for name in over:
        if name == "owner":
            owner, path = owner_label, owner_label
        elif name == "occurrence":
            occurrence = 0
        elif any(k == name for k, _ in dims):
            dims = tuple((k, v) for k, v in dims if k != name)
        elif any(k == name for k, _ in labels):
            labels = tuple((k, v) for k, v in labels if k != name)
        else:
            raise KeyError(
                f"Cannot reduce over '{name}': not 'owner', 'occurrence', "
                f"a dim, or a label of this stat."
            )
    return Index(labels, owner, dims, occurrence, path)


def _put(data: Dict[Index, Any], index: Index, name: str) -> Index:
    if index in data:
        raise ValueError(f"'{name}': two values map to the same index {index}.")
    return index


def _is_additive(unit: str) -> bool:
    return unit in ADDITIVE_UNITS


def safe_div(a: float, b: float) -> float:
    try:
        return a / b
    except ZeroDivisionError:
        if a == 0 or math.isnan(a):
            return math.nan
        return math.copysign(math.inf, a)


def safe_floordiv(a: float, b: float) -> float:
    quotient = safe_div(a, b)
    return math.floor(quotient) if math.isfinite(quotient) else quotient


def safe_mod(a: float, b: float) -> float:
    try:
        return a % b
    except ZeroDivisionError:
        return math.nan
