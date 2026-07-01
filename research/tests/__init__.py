"""Test package for the ContextAuth ``research/`` layer.

The suite is intentionally SMOKE-fast: a single tiny synthetic dataset is built
once (session-scoped, see :mod:`research.tests.conftest`) and every test runs on
tiny tensors / 1--2 training epochs so the whole ``pytest research/tests`` run
finishes within a couple of minutes on CPU.
"""
