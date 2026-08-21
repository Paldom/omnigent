"""Workloads — the domain-specific half of the loop.

Ship one: :mod:`army.workloads.demo`, which does real work against real
harnesses and exists so a fresh install has something to run. Write your own
next to it and point ``army.toml`` at it; nothing in :mod:`army` itself needs
to change.
"""
