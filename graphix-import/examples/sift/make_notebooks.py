"""Build the tutorial notebooks from their sources in `src/`.

Each source is a Python file in "percent" format:

    # %% [markdown]
    # # A heading
    # Prose, one sentence per line.

    # %%
    code = "a cell"

The sources are the reviewable form; the `.ipynb` files next to this script
are generated, with no outputs.
Run `python make_notebooks.py` after editing a source.
Editing a notebook directly is fine for experiments, but the next build
overwrites it.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCES = HERE / "src"


def cells_from_source(text: str):
    cells = []
    kind, lines = None, []

    def flush():
        if kind is None:
            return
        while lines and not lines[-1].strip():
            lines.pop()
        if kind == "markdown":
            body = [line[2:] if line.startswith("# ") else line.lstrip("#") for line in lines]
        else:
            body = lines
        source = "\n".join(body)
        cell = {"cell_type": kind, "metadata": {}, "source": source}
        if kind == "code":
            cell.update(execution_count=None, outputs=[])
        cells.append(cell)

    for line in text.splitlines():
        if line.startswith("# %%"):
            flush()
            kind = "markdown" if "[markdown]" in line else "code"
            lines = []
        elif kind is not None:
            lines.append(line)
    flush()
    return cells


def build(source: Path) -> dict:
    return {
        "cells": cells_from_source(source.read_text()),
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def main() -> None:
    for source in sorted(SOURCES.glob("*.py")):
        target = HERE / f"{source.stem}.ipynb"
        target.write_text(json.dumps(build(source), indent=1) + "\n")
        print(f"wrote {target.name}", file=sys.stderr)


if __name__ == "__main__":
    main()
