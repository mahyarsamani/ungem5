"""Run every tutorial notebook's code cells, in order, in one namespace.

The venv has no nbconvert, so this runs the cells directly: from
`examples/sift/` (the notebooks' working directory), with matplotlib's
non-interactive backend.
It also checks that each `.ipynb` matches its source in `src/`.
"""

import contextlib
import io
import json
import os
import sys
import unittest
import warnings
from pathlib import Path

import gxtest  # noqa: E402,F401  (path and a temporary cache)

ROOT = Path(__file__).resolve().parents[1]
TUTORIAL = ROOT / "examples" / "sift"
EXAMPLE_DATA = ROOT / "example_data"
sys.path.insert(0, str(TUTORIAL))

from make_notebooks import build  # noqa: E402


def run_notebook(path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    notebook = json.loads(path.read_text())
    namespace = {"__name__": "__main__"}
    previous = Path.cwd()
    os.chdir(TUTORIAL)
    try:
        with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
            warnings.simplefilter("ignore")
            for number, cell in enumerate(notebook["cells"]):
                if cell["cell_type"] != "code":
                    continue
                source = cell["source"]
                source = "".join(source) if isinstance(source, list) else source
                try:
                    exec(compile(source, f"{path.name}[cell {number}]", "exec"), namespace)
                except Exception as error:
                    raise AssertionError(
                        f"{path.name}, cell {number} failed: {error!r}\n{source}"
                    ) from error
                plt.close("all")
    finally:
        os.chdir(previous)


@unittest.skipUnless(EXAMPLE_DATA.exists(), "example_data/ not present")
class TestTutorial(unittest.TestCase):
    def test_notebooks_match_sources(self):
        for source in sorted((TUTORIAL / "src").glob("*.py")):
            with self.subTest(notebook=source.stem):
                notebook = TUTORIAL / f"{source.stem}.ipynb"
                self.assertTrue(notebook.exists(), f"run make_notebooks.py for {source.name}")
                self.assertEqual(
                    json.loads(notebook.read_text())["cells"],
                    build(source)["cells"],
                    f"{notebook.name} is stale; run make_notebooks.py",
                )

    def test_notebooks_run(self):
        for notebook in sorted(TUTORIAL.glob("0*.ipynb")):
            with self.subTest(notebook=notebook.name):
                run_notebook(notebook)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        for name in sys.argv[1:]:
            run_notebook(TUTORIAL / name)
            print(f"{name}: ok")
    else:
        unittest.main()
