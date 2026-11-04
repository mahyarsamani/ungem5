"""Results: where a tool's runs are and which of them finished.

The stats core (`ungem5.stats`) reads one run directory at a time; this
package knows how `helper` lays out its jobs' directories, which of them
finished, and what labels they carry (`jobs`).
It imports neither pandas nor the stats core at module level, so a worker
installed without the `stats` extra can import it.
"""
