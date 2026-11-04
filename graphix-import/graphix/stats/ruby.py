"""Ruby's collated controller counts.

The controller with version 0 of each machine type registers
`ruby_system.<Machine>_Controller.<event>` and
`ruby_system.<Machine>_Controller.<state>.<event>`: vectors with one element
per controller of that type, filled at dump time by `collateStats`
(`mem/slicc/symbols/StateMachine.py`).
Element i belongs to the controller whose `version` parameter is i.
graphix moves each element to its controller, so the result is one count per
controller, with the event (and state) as dims.
"""

from collections import OrderedDict
from typing import List, Optional, Tuple

from .scalar import Scalar


class ControllerCount(Scalar):
    """Event or transition counts, one value per Ruby controller."""

    kind = "ruby_controller"
    plots = ("heatmap", "stacked_bar", "bar")

    @classmethod
    def values_from_block(cls, block, run) -> List[Tuple[str, float]]:
        machine = block.name.split("_Controller", 1)[0]
        values = block.fields.get(None)
        if values is None:
            raise ValueError(f"'{block.name}' has no per-controller values.")
        if run is None:
            return [(f"{machine}_Controller[{i}]", v) for i, v in enumerate(values)]
        return [
            (run.controller_path(machine, version), value)
            for version, value in enumerate(values)
        ]

    def matrix(self, owner: Optional[str] = None, rows: str = "state", columns: str = "event"):
        """A state x event table of counts (summed over the selected values).

        `owner` narrows to one controller (a glob); without it, the counts of
        all selected controllers and runs are summed.
        """
        stat = self.select(owner=owner) if owner else self
        frame = stat.to_frame()
        if frame.empty:
            return frame
        if rows not in frame.columns:
            raise KeyError(f"'{self.name}' has no '{rows}' dim; transitions have 'state'.")
        return frame.pivot_table(
            index=rows, columns=columns, values="value", aggfunc="sum", fill_value=0
        )
