"""Exception hierarchy."""

from __future__ import annotations


class HarnessError(Exception):
    """Base class for all harness failures."""


class ConfigError(HarnessError):
    """Bad config or unknown component."""


class ToolError(HarnessError):
    """Unrecoverable tool failure."""


class SandboxError(HarnessError):
    """Sandbox provisioning or fatal exec failure."""


class LLMError(HarnessError):
    """LLM call failed after retries."""

    # Set by clients whose stream dropped mid-tool-call so the loop can
    # treat it as a retryable truncation instead of a hard failure.
    retryable_truncation: bool = False


class EvaluationError(HarnessError):
    """Evaluator produced no verdict."""


class AbortError(HarnessError):
    """Operator interrupt; stop cleanly."""
