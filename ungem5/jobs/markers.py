"""The files a project's run script writes into a job's outdir.

`record_args` in `assets/decorators.py` writes them and `clear_outdir`
deletes them before a rerun; `ungem5.results.jobs` reads them.
This module is the source of truth for their names.
`helper initialize` copies `decorators.py` into a project, where gem5's
own Python runs it without ungem5 on its path, so that file spells the
names out; `tests/test_imports.py` checks that they match these.
"""

# NOTE: The run function's arguments, by name.
PARAMS_FILE = "params.json"
# NOTE: Touched before the run function is called.
CALLED_MARKER = "run_function_called"
# NOTE: Touched after the run function returns.
FINISHED_MARKER = "simulation_finished"
