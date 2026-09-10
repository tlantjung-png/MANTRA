"""Tree-query tool: stable import location for the registry and docs.

The implementation lives in core/tools/extract.py beside the bounded
extraction tool; both are small workspace-analysis additions.
"""
from __future__ import annotations

from core.tools.extract import QueryTreeTool

__all__ = ["QueryTreeTool"]
