"""Model evals: recorded cases scored against a real engine.

Run by hand when a model or prompt changes (``python -m evals.run``).
These make real model calls and are not deterministic, so they are not
part of ``pytest``; ``tests/test_evals.py`` covers the runner itself.
"""
