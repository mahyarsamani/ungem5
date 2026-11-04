from functools import reduce
from itertools import permutations
from matplotlib import pyplot as plt
from typing import Any, Dict, List, Union

from .base_types import Node, Stat


def _find_dependecy_pairs(combinations: List[Dict]):
    keys = combinations[0].keys()
    dependency_pairs = []
    for key_a in keys:
        for key_b in keys:
            if key_a == key_b:
                continue

            a2b_map = {}
            dependent = True
            for combination in combinations:
                if combination[key_a] not in a2b_map:
                    a2b_map[combination[key_a]] = combination[key_b]
                if a2b_map[combination[key_a]] != combination[key_b]:
                    dependent = False
                    break
            if dependent:
                dependency_pairs.append((key_a, key_b))
    return list(keys), dependency_pairs


def _build_dependency_straws(
    combinations: List[Dict], unique_index_values: dict
):
    keys, dependency_pairs = _find_dependecy_pairs(combinations)
    all_straws = list(permutations(keys))

    no_dependency_inversion_straws = []
    for straw in all_straws:
        valid = True
        for i in range(len(keys) - 1):
            for j in range(i + 1, len(keys)):
                if (straw[i], straw[j]) in dependency_pairs:
                    valid = False
                    break
            if not valid:
                break
        if valid:
            no_dependency_inversion_straws.append(straw)

    ret = []
    for straw in no_dependency_inversion_straws:
        valid = True
        for i in range(len(straw) - 1):
            if (
                len(unique_index_values[straw[i]])
                > len(unique_index_values[straw[i + 1]])
                and (straw[i + 1], straw[i]) not in dependency_pairs
            ):
                valid = False
                break
        if valid:
            ret.append(straw)

    return ret


def _get_unique_index_values(stats: List[Stat]) -> dict[str, List[Any]]:
    unique_values = dict()
    for index in [stat.index() for stat in stats]:
        for key, value in index.items():
            if key not in unique_values:
                unique_values[key] = set()
            unique_values[key].add(value)
    return {key: list(value) for key, value in unique_values.items()}


def _get_common_parents(stats: List[Stat]) -> set[Node]:
    common_parents = reduce(
        lambda x, y: x & y, (set(stat.parents()) for stat in stats)
    )
    return common_parents


def _build_discriminator_hierarchy(stats: List[Stat]):
    unique_values = _get_unique_index_values(stats)
    common_parents = _get_common_parents(stats)
    unique_values["parent"] = list(common_parents)

    combinations = list()
    for stat in stats:
        for parent in common_parents:
            combination = stat.index()
            assert parent not in combination
            combination["parent"] = parent
            combinations.append(combination)

    straws = _build_dependency_straws(combinations, unique_values)
    pick = straws[0]


def _get_stats_name(stats: List[Stat]) -> str:
    prev_stat_name = None
    for stat in stats:
        stat_name = stat.name()
        if prev_stat_name and stat_name != prev_stat_name:
            raise ValueError(
                "All the stats in the same layer must have the same name."
            )
        prev_stat_name = stat_name
    return prev_stat_name


def _verify_layer_conformity_and_get_layer_names(
    stats: List[List[Stat]],
) -> List[str]:
    return [_get_stats_name(layer) for layer in stats]


def _verify_layer_conformity_and_get_common_parents(
    stats: List[List[Stat]],
) -> set[Node]:
    prev_common_parents = set()
    for layer_num, layer in enumerate(stats):
        common_parents = _get_common_parents(layer)
        if not common_parents:
            raise ValueError(
                f"Could not find any common parents between stats in layer {layer_num}."
            )
        if prev_common_parents and common_parents != prev_common_parents:
            raise ValueError(
                "Layer conformity error: all layers must have the same "
                f"common parents. Layer {layer_num} has different "
                "common parents than the previous layers."
            )
        prev_common_parents = common_parents
    return prev_common_parents


def _verify_layer_conformity_and_get_unique_index_values(
    stats: List[List[Stat]],
) -> dict[str, List[Any]]:
    prev_unique_values = dict()
    for layer_num, layer in enumerate(stats):
        unique_values = _get_unique_index_values(layer)
        if not unique_values:
            raise ValueError(
                f"Could not find any unique values between stats in layer {layer_num}."
            )
        if prev_unique_values and unique_values != prev_unique_values:
            raise ValueError(
                "Layer conformity error: all layers must have the same "
                f"unique values. Layer {layer_num} has different "
                "unique values than the previous layers."
            )
        prev_unique_values = unique_values
    return prev_unique_values


def _draw_spanning_line(
    ax: plt.axes,
    xmin: Union[int, float],
    xmax: Union[int, float],
    y: Union[int, float],
    text: str,
    color: str,
    end_tick_height_multiplier: float,
) -> None:
    ax.hlines(y=y, xmin=xmin, xmax=xmax, color=color)
    ax.plot(
        [xmin, xmin],
        [y * end_tick_height_multiplier, y],
        color=color,
    )
    ax.annotate(
        text,
        xy=((xmin + xmax) / 2, y),
        ha="center",
        va="bottom",
        color=color,
    )
