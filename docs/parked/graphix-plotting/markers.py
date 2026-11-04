from matplotlib import colors as mcolors

colors = [
    "darkorange",
    "teal",
    "orchid",
    "darkslateblue",
    "chocolate",
    "yellow",
    "cornflowerblue",
    "palegreen",
    "hotpink",
    "black",
    "coral",
    "seagreen",
    "crimson",
    "mediumblue",
    "gold",
    "fuchsia",
]


def get_color_from_id(id: int) -> str:
    return mcolors.CSS4_COLORS[colors[id % len(colors)]]


hatches = ["", "/", "x", "|", "O", "-", "*", "\\", "+", "o", "."]


def get_hatch_from_id(id: int) -> str:
    return hatches[id % len(hatches)]
