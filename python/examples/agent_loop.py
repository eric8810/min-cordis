"""Example: a basic agent loop built on min-cordis.

This is the framework's reference application — *not* part of the core
package. It shows how the kernel's pieces (services, the plugin lifecycle,
inject gating, fiber-owned listeners, the event bus) compose into the classic
agent cycle — perceive → think (LLM) → act (tool) → observe → … → answer:

- :class:`LLMClient` — the model backend boundary, a service provided as
  ``llm`` (class attr ``provide``). :class:`ScriptedLLM` gives deterministic
  responses for demos and tests; subclass for a real client.
- :class:`ToolSpec` — a declarative tool: JSON-schema parameters plus a sync
  or async ``run``.
- :class:`AgentLoop` — the cycle itself, provided as ``agent``. It follows the
  full plugin contract: ``(ctx, config)`` construction, ``inject = ["llm"]``
  dependency gating (the fiber stays PENDING until the llm service is
  provided), and an ``_init`` hook awaited before the fiber reports ACTIVE.

Everything is assembled **through the plugin machinery**, not around it::

    view = ctx.plugin(AgentLoop, {"tools": [weather], "max_steps": 16})
    await view                    # resolves once llm is available and _init ran
    answer = await ctx.get("agent").run("weather in Oslo?")

Contracts demonstrated:

- Tool failures (including unknown tools) are fed back to the model as
  ``error: ...`` results instead of crashing the loop.
- ``max_steps`` bounds runaway loops; :meth:`AgentLoop.stop` cancels
  cooperatively between steps.
- Observability flows over the bus: ``agent/step`` (step, content,
  tool-call count) and ``agent/tool`` (name, arguments, ok, output).
  Listeners attach with ``ctx.on`` and unload with their fiber.

Run the demo (demo.py)::

    uv run python examples/demo.py
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Iterable

from min_cordis import Context
from min_cordis._service import Service

__all__ = [
    "AgentLoop",
    "AgentLoopError",
    "AgentStopped",
    "LLMClient",
    "LLMResponse",
    "ScriptedLLM",
    "ToolCall",
    "ToolSpec",
]


class AgentStopped(RuntimeError):
    """The loop was cancelled through ``AgentLoop.stop()`` between steps."""


class AgentLoopError(RuntimeError):
    """The loop exceeded its step budget (``max_steps``)."""


@dataclass
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """One model response: final content, requested tool calls, or both."""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolSpec:
    """A declarative tool: JSON-schema parameters plus a sync/async runner.

    ``run`` is invoked as ``run(**arguments)``; a returned coroutine is
    awaited. The result is stringified (JSON for non-strings) into the tool
    message.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[..., Any]


class LLMClient(Service):
    """Model backend boundary, provided as the ``llm`` service.

    Subclass and implement :meth:`complete`. ``messages`` is the running
    transcript (user/assistant/tool dicts, OpenAI-ish shape); ``tools`` is
    the function-schema list built from the agent's :class:`ToolSpec`\\s.
    Loaded as a plugin like any Service subclass.
    """

    provide: ClassVar[str] = "llm"

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        raise NotImplementedError


class ScriptedLLM(LLMClient):
    """Deterministic backend for tests: pops one scripted response per call.

    Config: ``{"responses": [LLMResponse, ...]}``. Also records every
    transcript it was shown (``calls``: one ``{"messages": [...],
    "tools": [...]}`` entry per call), so tests can assert exactly what the
    model saw.
    """

    def __init__(self, ctx: Context, config: Any = None) -> None:
        super().__init__(ctx)
        self.responses = list((config or {}).get("responses", ()))
        self.calls: list[dict] = []

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        if not self.responses:
            raise RuntimeError("ScriptedLLM has no responses left")
        return self.responses.pop(0)


class AgentLoop(Service):
    """The perceive/think/act/observe cycle, provided as the ``agent`` service.

    Full plugin lifecycle:

    - ``(ctx, config)`` construction — config keys: ``tools`` (iterable of
      :class:`ToolSpec`), ``max_steps`` (default 16).
    - ``inject = ["llm"]`` — the fiber stays PENDING until an ``llm`` service
      is provided; load order between the two plugins does not matter.
    - ``_init`` — awaited by the fiber before ACTIVE; binds the llm through
      the plugin context.
    """

    provide: ClassVar[str] = "agent"
    inject: ClassVar[Any] = ["llm"]

    def __init__(self, ctx: Context, config: Any = None) -> None:
        super().__init__(ctx)
        config = dict(config or {})
        self.tools: dict[str, ToolSpec] = {tool.name: tool for tool in config.get("tools", ())}
        self.max_steps = int(config.get("max_steps", 16))
        self.steps = 0
        self.llm: LLMClient | None = None  # bound in _init, after gating
        self._stopped = False

    async def _init(self) -> None:
        # Inject gating guarantees "llm" is provided before the fiber starts;
        # bind through the plugin context (access-time traceable view).
        self.llm = self.ctx.llm

    def stop(self) -> None:
        """Request cancellation; takes effect between steps."""
        self._stopped = True

    def tool_schemas(self) -> list[dict]:
        """The function-schema list handed to the model each step."""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self.tools.values()
        ]

    async def run(self, prompt: str) -> str:
        """Run the loop for one user prompt; return the final answer.

        @param prompt — the user message that seeds the transcript.
        @throws RuntimeError — the fiber never started (bypassed ``ctx.plugin``).
        @throws AgentStopped — cancelled via :meth:`stop` between steps.
        @throws AgentLoopError — the step budget was exhausted.
        """
        if self.llm is None:
            raise RuntimeError(
                "agent fiber never started: load AgentLoop via ctx.plugin(AgentLoop, {...})"
                " so inject gating can bind the llm service in _init"
            )
        messages: list[dict] = [{"role": "user", "content": prompt}]
        for _ in range(self.max_steps):
            if self._stopped:
                raise AgentStopped("agent loop stopped between steps")
            self.steps += 1
            response = await self.llm.complete(messages, self.tool_schemas())
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in response.tool_calls
                ],
            })
            self.ctx.emit("agent/step", self.steps, response.content, len(response.tool_calls))
            if not response.tool_calls:
                return response.content or ""
            for call in response.tool_calls:
                content, ok = await self._run_tool(call)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": content,
                })
        raise AgentLoopError(f"agent loop exceeded max_steps={self.max_steps}")

    async def _run_tool(self, call: ToolCall) -> tuple[str, bool]:
        """Execute one tool call; failures become model-visible error results."""
        tool = self.tools.get(call.name)
        if tool is None:
            content = f"error: unknown tool {call.name!r}"
            self.ctx.emit("agent/tool", call.name, call.arguments, False, content)
            return content, False
        try:
            result = tool.run(**call.arguments)
            if inspect.iscoroutine(result):
                result = await result
        except Exception as exc:
            # Contain per-call failures: report to the model and the bus,
            # never crash the loop.
            content = f"error: {exc!r}"
            self.ctx.emit("agent/tool", call.name, call.arguments, False, content)
            return content, False
        content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        self.ctx.emit("agent/tool", call.name, call.arguments, True, content)
        return content, True
