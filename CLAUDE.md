# ungem5: everything gem5 research needs that is not the simulator

Python package `ungem5` behind the `helper` command (`initialize`, `build`, `run`, `work`, `schedule`, `console`, `certs`), with the scheduler and workers in `ungem5/schedule/`, jobs in `ungem5/jobs/`, the build and run backends in `ungem5/run/`, and typed gem5 statistics in `ungem5/stats/` (`import ungem5.stats as gx`, formerly graphix).
Run the tests with `pytest` from this directory.
The version lives only in `pyproject.toml` as `0.YY.MM.N`; do not bump it unless asked.

## Project memory

@context/memory.md
