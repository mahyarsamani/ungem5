"""Every module of ungem5 imports, and the old package names are gone."""

import ast
import importlib
import importlib.util
import pkgutil
import subprocess
import sys

from pathlib import Path

import pytest

import ungem5

from ungem5.jobs import markers

ROOT = Path(__file__).resolve().parents[1]
# NOTE: gem5-side files that `helper initialize` copies into a project; gem5's
# own Python runs them, with `m5` on its path.
ASSETS = "ungem5.jobs.assets."


def _modules() -> list[str]:
    return sorted(
        info.name
        for info in pkgutil.walk_packages(ungem5.__path__, "ungem5.")
        if not info.name.startswith(ASSETS)
    )


@pytest.mark.parametrize("name", _modules())
def test_every_module_imports(name):
    importlib.import_module(name)


@pytest.mark.parametrize("name", ["experiment", "graphix"])
def test_old_packages_are_not_importable_from_the_repo(name):
    spec = importlib.util.find_spec(name)
    if spec is None:
        return
    places = [spec.origin, *(spec.submodule_search_locations or [])]
    for place in places:
        if place is not None and place != "namespace":
            assert not Path(place).resolve().is_relative_to(ROOT), place


def _run(code: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_stats_does_not_load_the_scheduler():
    loaded = _run(
        "import sys, ungem5.stats; "
        "print(sorted(m for m in sys.modules if m.startswith('ungem5.')))"
    )
    assert "ungem5.results" not in loaded
    assert "ungem5.schedule" not in loaded


def test_results_import_without_pandas():
    # NOTE: None in sys.modules makes `import pandas` raise ImportError, as
    # on a worker installed without the `stats` extra.
    _run(
        "import sys; sys.modules['pandas'] = None; "
        "from ungem5.results import jobs; "
        "assert callable(jobs.select_jobs)"
    )


def test_decorators_spell_the_marker_names_of_markers_py():
    path = Path(ungem5.__file__).parent / "jobs" / "assets" / "decorators.py"
    names = {
        target.id: node.value.value
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    for name in ("PARAMS_FILE", "CALLED_MARKER", "FINISHED_MARKER"):
        assert names[name] == getattr(markers, name), name
