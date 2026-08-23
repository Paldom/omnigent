"""Workloads a bot may name.

Kept separate from ``army/workloads/`` because the two answer to different
callers: those are driven by a single-workload supervisor from ``army.toml``,
these are named by a bot definition and resolved through
:class:`~army.bots.registry.WorkloadRegistry`, which decides what a bot is
allowed to load.
"""
