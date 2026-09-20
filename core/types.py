"""Abstract contracts for pluggable components."""

import copy
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecResult:
    """Outcome of one command execution inside the sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class Sandbox(ABC):
    """Execution environment for one task run.

    Implementations own their lifecycle: ``setup`` provisions the working
    environment, ``exec`` runs commands, ``cleanup`` releases resources and
    must be safe to call more than once.
    """

    @abstractmethod
    def setup(self, task: dict) -> None:
        """Prepare the environment for the task (repo checkout, deps)."""
        raise NotImplementedError

    @abstractmethod
    def exec(self, command: str, timeout: float = 120.0) -> ExecResult:
        """Run a shell command in the sandbox and capture its output."""
        raise NotImplementedError

    def screen_command(self, command: str) -> str | None:
        """Reject a command before execution; return a reason or None.

        Sandboxes with command-level screening (the host sandbox)
        override this so every execution path — foreground and
        background alike — can share one implementation. The default
        accepts everything.
        """
        return None

    @abstractmethod
    def read_file(self, path: str) -> str:
        """Return file content relative to the sandbox workspace."""
        raise NotImplementedError

    @abstractmethod
    def write_file(self, path: str, content: str) -> None:
        """Write (create or overwrite) a file relative to the sandbox workspace."""
        raise NotImplementedError

    @abstractmethod
    def cleanup(self) -> None:
        """Tear down the environment. Idempotent."""
        raise NotImplementedError


class Tool(ABC):
    """A single agent-facing tool.

    ``parameters`` is a JSON Schema object describing the arguments; it is
    passed verbatim to the LLM as part of the function-calling spec.
    """

    name: str = ""
    description: str = ""
    # None means "no arguments": schema() builds a fresh object per call,
    # so no shared mutable class default is exposed.
    parameters: dict[str, Any] | None = None

    @abstractmethod
    def execute(self, sandbox: Sandbox, **kwargs: Any) -> str:
        """Run the tool against the sandbox and return an observation string."""
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        """Function-calling schema fragment for this tool."""
        params = self.parameters
        if params is None:
            params = {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                # A copy: callers may hold or mutate the schema without
                # corrupting a subclass's own parameters dict.
                "parameters": copy.deepcopy(params),
            },
        }


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """Normalized model reply: either tool calls or a final answer."""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] | None = None  # {"prompt_tokens": n, "completion_tokens": n}

    @property
    def is_final(self) -> bool:
        return not self.tool_calls


class LLMClient(ABC):
    """Chat client with optional function calling."""

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: Any = None,
    ) -> LLMResponse:
        """Send the conversation and tool schemas, return a normalized response.

        ``on_delta(content_fragment)`` is invoked per streamed fragment when
        the client supports streaming; implementations may ignore it.
        """
        raise NotImplementedError


@dataclass
class EvaluationResult:
    """Verdict plus evidence from an evaluation pass."""

    passed: bool
    detail: str = ""
    metrics: dict[str, float] = field(default_factory=dict)


class Evaluator(ABC):
    """Scores the final sandbox state after the agent loop finishes."""

    @abstractmethod
    def evaluate(self, sandbox: Sandbox, task: dict) -> EvaluationResult:
        """Verdict for the final sandbox state; must never raise."""
        raise NotImplementedError


class Logger(ABC):
    """Receives structured events from the harness.

    Implementations must never raise: observability failures must not kill
    an agent run. Wrap sinks defensively.
    """

    @abstractmethod
    def log(self, event: str, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Release resources. Optional and idempotent."""


