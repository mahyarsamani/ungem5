from math import sqrt
from matplotlib import pyplot as plt
from matplotlib.patches import Patch
from numpy import atleast_1d
from typing import Any, List, Optional
from warnings import warn

from .base_types import Node, Stat
from .compare_util import BiggestThing, SmallestThing
from .markers import get_color_from_id, get_hatch_from_id
from .stats import Scalar
from .util import (
    _draw_spanning_line,
    _verify_layer_conformity_and_get_layer_names,
    _verify_layer_conformity_and_get_common_parents,
    _verify_layer_conformity_and_get_unique_index_values,
)

# TODO: Think about handling common parents, should it just be a string that
# represents the path to the parent (i.e. should we track common parent paths)?


class BarPlotDiscriminatorMap:
    def __init__(
        self,
        discriminator_mapping: Optional[dict[str, str]],
        custom_index_order: Optional[dict[str, List[str]]],
    ) -> None:
        # NOTE: from now on, we can assume self._discriminators is a list
        # sorted based on the priority of the discriminators.
        # The higher the discriminator is in the order, the more diverse
        # indices it would be assigned to. That is, in case,
        # `discriminator_mapping` is not provided.
        self._discriminators = ["hue", "subgroup", "group", "hatch", "subplot"]
        self._user_provided_discriminator_mapping = discriminator_mapping
        self._custom_index_order = custom_index_order

    # NOTE: This function builds the discriminator mapping which is a
    # complex dictionary that maps the name of the discriminator to
    # the index it should use to determine different values for it
    # as well as the different values of that index to an integer id and
    # a reverse mapping of that.
    def build(self, stats: List[List[Scalar]]) -> None:
        the_indices = stats[0][0].index().keys()
        if not all(
            all(stat.index().keys() == the_indices for stat in stat_list)
            for stat_list in stats
        ):
            raise ValueError(
                "Provided stats do not have the same index. "
                "Please check the indices of the stats."
            )

        layer_names = _verify_layer_conformity_and_get_layer_names(stats)

        common_parents = _verify_layer_conformity_and_get_common_parents(stats)
        num_common_parents = len(common_parents)
        num_layers = len(stats)

        unique_index_values = (
            _verify_layer_conformity_and_get_unique_index_values(stats)
        )

        if self._custom_index_order is not None:
            if "layer" in self._custom_index_order:
                raise ValueError(
                    "Custom index order should not contain `layer`. The "
                    "`layer` order is determined by the order of the lists for"
                    " each layer in `stats`."
                )
            if "parent" in self._custom_index_order:
                raise ValueError(
                    "Custom index order should not contain `parent`. The "
                    "`parent` order is determined by the alphabetical order of"
                    "common parents of the stats."
                )
            for index, order in self._custom_index_order.items():
                if index not in unique_index_values:
                    raise ValueError(f"Index {index} not found in the stats.")
                order_dict = {value: rank for rank, value in enumerate(order)}
                unique_index_values[index] = sorted(
                    unique_index_values[index],
                    key=lambda x: order_dict.get(x, BiggestThing()),
                )

        temp_discriminators = self._discriminators.copy()
        partial_discriminator_index_mapping = dict()
        use_parent_as = None
        use_layer_as = None
        if self._user_provided_discriminator_mapping is None:
            warn(
                "`discriminator_mapping` not provided. Figuring out automatically."
            )
            if num_common_parents > 1:
                warn(
                    "Provided stats have more than one common parent. "
                    "Using `subgroup` to demonstrate parents."
                )
                use_parent_as = "subgroup"
                temp_discriminators.remove("subgroup")
            if num_layers > 1:
                warn(
                    "Provided stats have more than one layer. "
                    "Using `hue` to demonstrate layers."
                )
                use_layer_as = "hue"
                temp_discriminators.remove("hue")
            partial_discriminator_index_mapping = (
                self._figure_out_partial_discriminator_index_mapping(
                    temp_discriminators,
                    unique_index_values,
                )
            )
            partial_discriminator_index_mapping[use_parent_as] = "parent"
            partial_discriminator_index_mapping[use_layer_as] = "layer"
            warn(
                f"Automatically figured out mapping: {partial_discriminator_index_mapping}"
            )
        else:
            # NOTE: Catch if the user has specified the same
            # value for two different keys
            if len(
                set(self._user_provided_discriminator_mapping.values())
            ) != len(list(self._user_provided_discriminator_mapping.values())):
                raise ValueError(
                    "Provided values in `marker_mapping` should be "
                    f"unique and should be from {self._discriminators}."
                )
            # NOTE: values specified should be in self._discriminators
            if not set(
                self._user_provided_discriminator_mapping.values()
            ) <= set(self._discriminators):
                raise ValueError(
                    "Provided values in `marker_mapping` should be in "
                    f"{self._discriminators}."
                )
            use_parent_as = self._user_provided_discriminator_mapping.get(
                "parent", None
            )
            if use_parent_as is None and num_common_parents > 1:
                raise ValueError(
                    "Provided stats have more than one common parent. "
                    "No instruction provided for `parent`. "
                )
            if use_parent_as is not None and num_common_parents == 1:
                warn(
                    "Provided stats have only one common parent. "
                    "Instruction provided for `parent`."
                )
            use_layer_as = self._user_provided_discriminator_mapping.get(
                "layer", None
            )
            if use_layer_as is None and num_layers > 1:
                raise ValueError(
                    "Provided stats have more than one layer. "
                    "No instruction provided for `layer`. Note that the only "
                    "discriminator that can be used for `layer` is `hue`."
                )
            if use_layer_as is not None and num_layers == 1:
                warn(
                    "Provided stats have only one layer. "
                    "Instruction provided for `layer`."
                )
            if use_layer_as is not None and use_layer_as not in ["hue"]:
                raise ValueError("`layer` can only be used for `hue`.")
            copy_user_provided_discriminator_mapping = (
                self._user_provided_discriminator_mapping.copy()
            )
            copy_user_provided_discriminator_mapping.pop("parent", None)
            copy_user_provided_discriminator_mapping.pop("layer", None)
            if set(copy_user_provided_discriminator_mapping.keys()) != set(
                the_indices
            ):
                raise ValueError(
                    "With the exception of `parent` and `layer`, provided "
                    "keys in `marker_mapping` should exhaustively cover "
                    "the keys in the index of the stats."
                )
            for key, value in copy_user_provided_discriminator_mapping.items():
                if value not in self._discriminators:
                    raise ValueError(
                        "Provided values in `marker_mapping` should be in "
                        f"{self._discriminators}."
                    )
                partial_discriminator_index_mapping[value] = key
            partial_discriminator_index_mapping[use_parent_as] = "parent"
            partial_discriminator_index_mapping[use_layer_as] = "layer"

        unique_index_values["parent"] = sorted(common_parents)
        unique_index_values["layer"] = layer_names

        discriminator_mapping = dict()
        for key in self._discriminators:
            temp = partial_discriminator_index_mapping.get(key, None)
            if temp is not None:
                if (temp not in unique_index_values) and (
                    temp not in ["parent", "layer"]
                ):
                    raise RuntimeError(
                        "Values inside partial_discriminator_index_mapping "
                        f"should be in unique_values. {temp} not in {unique_index_values}."
                    )
                value_id_map = self._map_values_to_id(
                    unique_index_values.get(temp, [])
                )
                id_value_map = {
                    value: key for key, value in value_id_map.items()
                }
                discriminator_mapping[key] = {
                    "index": temp,
                    "num_unique_values": len(unique_index_values[temp]),
                    "value_id_map": value_id_map,
                    "id_value_map": id_value_map,
                }
        self._discriminator_mapping = discriminator_mapping
        return num_layers, layer_names, common_parents

    def get_mapping(self):
        return self._discriminator_mapping

    def mapped(self, discriminator: str) -> bool:
        return discriminator in self._discriminator_mapping

    def get_index_for(self, discriminator: str) -> str:
        if discriminator not in self._discriminator_mapping:
            return None
        return self._discriminator_mapping[discriminator]["index"]

    def get_num_unique_values(self, discriminator: str) -> int:
        return (
            self._discriminator_mapping[discriminator]["num_unique_values"]
            if discriminator in self._discriminator_mapping
            else 1
        )

    def _get_discriminator_value(
        self, discriminator: str, index: dict, parent: Node, layer_name: str
    ):
        if discriminator not in self._discriminator_mapping:
            return None
        tie = self._discriminator_mapping[discriminator]["index"]
        if tie == "parent":
            return parent
        if tie == "layer":
            return layer_name
        else:
            return index[tie]

    def get_id_for(
        self, discriminator: str, index: dict, parent: Node, layer_name: str
    ):
        discriminator_value = self._get_discriminator_value(
            discriminator, index, parent, layer_name
        )
        if discriminator_value is not None:
            return self._discriminator_mapping[discriminator]["value_id_map"][
                discriminator_value
            ]
        else:
            return 0

    def get_value_for(self, discriminator: str, id) -> str:
        if discriminator not in self._discriminator_mapping:
            return None
        return self._discriminator_mapping[discriminator]["id_value_map"][id]

    def _figure_out_partial_discriminator_index_mapping(
        self, discriminators: List[str], unique_values: dict[str, List[Any]]
    ) -> dict[str, str]:
        ret = dict()
        marker_count = [
            (marker, len(uniques)) for marker, uniques in unique_values.items()
        ]
        marker_count.sort(key=lambda x: x[1], reverse=True)
        markers, _ = zip(*marker_count)

        if len(markers) > len(discriminators):
            raise ValueError(
                f"This package only supports up to {len(discriminators)} markers."
            )

        for index, marker in enumerate(markers):
            ret[discriminators[index]] = marker
        return ret

    def _map_values_to_id(self, values: List) -> dict[str, int]:
        return {value: idx for idx, value in enumerate(values)}


def plot_bar(
    stats: List[List[Scalar]],
    discriminator_mapping: Optional[dict[str, str]] = None,
    custom_order: Optional[dict[str, List[Any]]] = None,
    nrows: Optional[int] = None,
    ncols: Optional[int] = None,
    sharex: Optional[bool] = False,
    sharey: Optional[bool] = False,
    figsize: Optional[tuple[int, int]] = (14, 8.32),
    hatch_density: Optional[int] = 3,
    hatch_color: Optional[str] = "black",
    bar_width: int = 1,
    subgroup_gap: int = 1,
    group_gap: int = 4,
    annotation_color: str = "black",
    end_tick_height_multiplier: float = 0.9875,
    subgroup_id_height_multiplier: float = 1.0375,
    group_id_subgroup_id_height_difference: float = 0.05,
    **kwargs,
) -> tuple[plt.figure, List[plt.axes]]:
    discriminator_map = BarPlotDiscriminatorMap(
        discriminator_mapping, custom_order
    )
    num_layers, layer_names, common_parents = discriminator_map.build(stats)
    print(discriminator_map.get_mapping())

    def _get_good_dimensions(one_d: int) -> tuple[int, int]:
        side = int(sqrt(one_d))
        while one_d % side != 0:
            side -= 1
        return max(side, one_d // side), min(side, one_d // side)

    if nrows is not None and ncols is not None:
        if nrows * ncols != discriminator_map.get_num_unique_values("subplot"):
            raise ValueError(
                "Provided nrows and ncols should multiply to number of unique "
                f"values for discriminator_mapping['subplot'] "
                f"({discriminator_map.get_index_for('subplot')})."
                f"which is {discriminator_map.get_num_unique_values('subplot')}."
            )
    elif (nrows is not None) != (ncols is not None):
        raise ValueError(
            "If one of nrows or ncols is provided, both should be provided."
        )
    else:
        nrows, ncols = _get_good_dimensions(
            discriminator_map.get_num_unique_values("subplot")
        )

    if nrows is None or ncols is None:
        raise RuntimeError("Something went wrong with the dimensions.")

    if sharex and nrows == 1:
        warn(f"`sharex` set to True when there is one row.")
    if sharey and ncols == 1:
        warn(f"`sharey` set to True when there is one column.")

    subgroup_width = (
        (discriminator_map.get_num_unique_values("hue") // num_layers)
        * discriminator_map.get_num_unique_values("hatch")
        * bar_width
    )
    group_width = (
        discriminator_map.get_num_unique_values("subgroup") * subgroup_width
        + (discriminator_map.get_num_unique_values("subgroup") - 1)
        * subgroup_gap
    )
    global_offset = subgroup_gap
    group_offset_multiplier = group_width + group_gap
    subgroup_offset_multiplier = subgroup_width + subgroup_gap
    hatch_offset_multiplier = (
        discriminator_map.get_num_unique_values("hue") // num_layers
    ) * bar_width

    fig, axes = plt.subplots(
        nrows=nrows,
        ncols=ncols,
        sharex=sharex,
        sharey=sharey,
        figsize=figsize,
        **kwargs,
    )

    axes = atleast_1d(axes)
    axes = axes.flatten()

    max_height = dict()
    number_bars_per_layer = (
        (discriminator_map.get_num_unique_values("hue") // num_layers)
        * (discriminator_map.get_num_unique_values("hatch"))
        * discriminator_map.get_num_unique_values("subgroup")
        * discriminator_map.get_num_unique_values("group")
        * discriminator_map.get_num_unique_values("subplot")
    )
    height_offset = [0] * number_bars_per_layer
    for layer_num, layer in enumerate(stats):
        layer_name = layer_names[layer_num]
        for i, stat in enumerate(layer):
            index = stat.index()
            for j, parent in enumerate(common_parents):
                hue_id = discriminator_map.get_id_for(
                    "hue", index, parent, layer_name
                )
                color = get_color_from_id(hue_id)
                hatch_id = discriminator_map.get_id_for(
                    "hatch", index, parent, layer_name
                )
                pattern = get_hatch_from_id(hatch_id)
                group_id = discriminator_map.get_id_for(
                    "group", index, parent, layer_name
                )
                subgroup_id = discriminator_map.get_id_for(
                    "subgroup", index, parent, layer_name
                )
                subplot_id = discriminator_map.get_id_for(
                    "subplot", index, parent, layer_name
                )
                height = stat.value()[parent]
                x = (
                    global_offset
                    + group_id * group_offset_multiplier
                    + subgroup_id * subgroup_offset_multiplier
                    + hatch_id * hatch_offset_multiplier
                    + (hue_id // num_layers) * bar_width
                )
                height_offset_index = i * len(common_parents) + j
                bar = axes[subplot_id].bar(
                    x=x,
                    height=height,
                    bottom=height_offset[height_offset_index],
                    width=bar_width,
                    color=color,
                    hatch=pattern * hatch_density,
                    edgecolor=hatch_color,
                )
                print(
                    f"subplot_id: {subplot_id}, group_id: {group_id}, subgroup_id: {subgroup_id}"
                )
                height_offset[height_offset_index] += height
                effective_height = height_offset[height_offset_index]
                if subplot_id not in max_height:
                    max_height[subplot_id] = dict()
                if group_id not in max_height[subplot_id]:
                    max_height[subplot_id][group_id] = dict()
                    max_height[subplot_id][group_id][
                        "max_height"
                    ] = SmallestThing()
                if subgroup_id not in max_height[subplot_id][group_id]:
                    max_height[subplot_id][group_id][
                        subgroup_id
                    ] = SmallestThing()
                max_height[subplot_id][group_id][subgroup_id] = max(
                    effective_height,
                    max_height[subplot_id][group_id][subgroup_id],
                )
                max_height[subplot_id][group_id]["max_height"] = max(
                    max_height[subplot_id][group_id]["max_height"],
                    max_height[subplot_id][group_id][subgroup_id],
                )

    print(max_height)
    handles = []
    if discriminator_map.mapped("hue"):
        handles += [
            Patch(
                facecolor=get_color_from_id(i),
                hatch="",
                label=f"{discriminator_map.get_index_for('hue')}: {discriminator_map.get_value_for('hue', i)}",
            )
            for i in range(discriminator_map.get_num_unique_values("hue"))
        ]
    if discriminator_map.mapped("hatch"):
        handles += [
            Patch(
                facecolor="white",
                hatch=get_hatch_from_id(i) * (hatch_density + 1),
                label=f"{discriminator_map.get_index_for('hatch')}: {discriminator_map.get_value_for('hatch', i)}",
                edgecolor=hatch_color,
            )
            for i in range(discriminator_map.get_num_unique_values("hatch"))
        ]

    for subplot_id, ax in enumerate(axes):
        for group_id in range(
            discriminator_map.get_num_unique_values("group")
        ):
            print(f"group_id: {group_id}")
            for subgroup_id in range(
                discriminator_map.get_num_unique_values("subgroup")
            ):
                print(f"subgroup_id: {subgroup_id}")
                if discriminator_map.mapped("subgroup"):
                    tall_bar = (
                        max_height[subplot_id][group_id][subgroup_id]
                        * subgroup_id_height_multiplier
                    )
                    left = (
                        global_offset
                        + group_id * group_offset_multiplier
                        + subgroup_id * subgroup_offset_multiplier
                        - (bar_width / 2)
                    )
                    right = left + subgroup_width
                    _draw_spanning_line(
                        ax,
                        left,
                        right,
                        tall_bar,
                        f"{discriminator_map.get_index_for('subgroup')}: {discriminator_map.get_value_for('subgroup', subgroup_id)}",
                        annotation_color,
                        end_tick_height_multiplier,
                    )
            if discriminator_map.mapped("group"):
                tall_bar = (
                    max_height[subplot_id][group_id]["max_height"]
                    * subgroup_id_height_multiplier
                    + group_id_subgroup_id_height_difference
                )
                left = (
                    global_offset
                    + group_id * group_offset_multiplier
                    - (bar_width / 2)
                )
                right = left + group_width
                _draw_spanning_line(
                    ax,
                    left,
                    right,
                    tall_bar,
                    f"{discriminator_map.get_index_for('group')}: {discriminator_map.get_value_for('group', group_id)}",
                    annotation_color,
                    end_tick_height_multiplier,
                )
        if discriminator_map.mapped("subplot"):
            ax.title.set_text(
                f"{discriminator_map.get_index_for('subplot')}: {discriminator_map.get_value_for('subplot', subplot_id)}",
            )
        ax.legend(handles=handles)
        ax.set_xticks([])
        ax.set_xticklabels([])

    return (
        fig,
        axes,
    )
