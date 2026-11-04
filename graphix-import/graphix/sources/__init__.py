"""Run sources: how a tool lays out its run directories.

graphix's core reads one run directory at a time; a source knows where a tool
puts its runs, which of them finished, and what labels they carry.
`helper` is the source for `tools/experiment`, the PhD's gem5 job runner.
"""
