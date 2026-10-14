"""HTCO agent runtime: the only package allowed to import LangGraph.

Graphs, checkpoints and the tool bridge arrive in Phase 8. Until then this package pins the
LangGraph version and proves it runs on our Python (tests/agent_runtime).
"""
