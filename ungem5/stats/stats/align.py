"""Pair the values of two stats for arithmetic.

Two stats align in one of two ways:
1. By index: both have values under the same indexes (same run labels,
   owner, dims, and occurrence).
   With `how="strict"` the index sets must be equal; with `how="inner"` only
   their intersection is kept; with `how="outer"` their union is kept, and
   a side's missing value is `fill` (zero for numbers, the natural reading
   of a counter gem5 left out because it was zero).
2. By run: the right-hand stat has exactly one value per run (for example,
   `simTicks`, owned by the root), so that value applies to every index of
   the left-hand stat with the same run labels.
   This is how `misses / simTicks` works across all caches at once.
"""

from typing import Any, List, Tuple

from .base import Index, Stat

Aligned = List[Tuple[Index, Any, Any]]


class AlignmentError(ValueError):
    pass


def align(
    left: Stat,
    right: Stat,
    how: str = "strict",
    fill: Any = 0.0,
    fill_right: Any = None,
) -> Aligned:
    """Pair `left` and `right` (see the module); with `how="outer"`, a
    missing left value is `fill` and a missing right one `fill_right`
    (default: `fill`)."""
    if how not in ("strict", "inner", "outer"):
        raise ValueError("how must be 'strict', 'inner' or 'outer'.")
    left_keys = list(left.indexes())
    right_data = dict(right.items())
    if how == "outer":
        fill_right = fill if fill_right is None else fill_right
        union = left_keys + [index for index in right_data if index not in left]
        return [
            (
                index,
                left[index] if index in left else fill,
                right_data.get(index, fill_right),
            )
            for index in union
        ]
    if any(index in right_data for index in left_keys):
        missing_right = [index for index in left_keys if index not in right_data]
        missing_left = [index for index in right_data if index not in left]
        if how == "strict" and (missing_right or missing_left):
            raise AlignmentError(_describe_mismatch(left, right, missing_right, missing_left))
        return [
            (index, left[index], right_data[index])
            for index in left_keys
            if index in right_data
        ]

    per_run = {}
    for index, value in right.items():
        if index.labels in per_run:
            break
        per_run[index.labels] = value
    else:
        aligned = []
        for index in left_keys:
            if index.labels in per_run:
                aligned.append((index, left[index], per_run[index.labels]))
            elif how == "strict":
                raise AlignmentError(
                    f"'{right.name}' has no value for run {dict(index.labels)}, "
                    f"which '{left.name}' has."
                )
        return aligned

    raise AlignmentError(
        f"Cannot align '{left.name}' with '{right.name}': they share no index, "
        f"and '{right.name}' has more than one value per run. "
        f"Select or reduce one side first (for example, "
        f"right.reduce('sum')), or align on fewer fields."
    )


def _describe_mismatch(left: Stat, right: Stat, missing_right, missing_left) -> str:
    parts = [f"'{left.name}' and '{right.name}' have different indexes."]
    if missing_right:
        parts.append(
            f"{len(missing_right)} index(es) only in '{left.name}', "
            f"e.g. {missing_right[0]}."
        )
    if missing_left:
        parts.append(
            f"{len(missing_left)} index(es) only in '{right.name}', "
            f"e.g. {missing_left[0]}."
        )
    parts.append("Use how='inner' to keep the intersection.")
    return " ".join(parts)


def combine_units(left: Any, right: Any, symbol: str) -> Any:
    known = lambda unit: unit not in (None, "", "Unspecified")
    if symbol in ("+", "-", "%"):
        if known(left) and known(right) and left != right:
            raise ValueError(f"Cannot apply '{symbol}' to units '{left}' and '{right}'.")
        return left if known(left) else right
    if symbol in ("/", "//"):
        if known(left) and known(right):
            return "Ratio" if left == right else f"{left}/{right}"
        return left if known(left) else None
    if symbol == "*":
        if known(left) and known(right):
            return f"{left}*{right}"
        return left if known(left) else right
    return None
