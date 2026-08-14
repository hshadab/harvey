"""Preflight enforcement hook for Harvey LAB.

The hook wraps LAB's ToolExecutor (composition, not patching — zero
modifications to harvey-labs) and checks every consequential tool call
against a compiled Preflight policy before it executes.
"""
