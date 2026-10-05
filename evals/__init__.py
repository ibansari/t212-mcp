"""Eval suite for the look-through agent.

component: the draft and repair LLM steps, fed fixed inputs and graded against saved issuer files (no live web).
e2e: the full discover -> draft -> test -> repair graph against live issuer sites, from an empty registry.

Run with `uv run python -m evals --help`.
"""
