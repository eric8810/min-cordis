"""Runnable demo for the agent-loop example — no network, fully scripted.

Shows the loop's three integration surfaces in one run:

1. plugin assembly with inject gating (agent loads before the llm and waits),
2. an audit plugin observing the run over the event bus,
3. a tool round-trip whose result is fed back to the model.

Run from python/::

    uv run python examples/demo.py
"""

from __future__ import annotations

import asyncio

from min_cordis import Context
from agent_loop import AgentLoop, LLMResponse, ScriptedLLM, ToolCall, ToolSpec


async def lookup_weather(city: str) -> dict:
    """A fake async backend — stands in for a real API call."""
    await asyncio.sleep(0.01)
    return {"city": city, "temp": 21, "sky": "clear"}


WEATHER = ToolSpec(
    name="get_weather",
    description="current weather for a city",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}},
    run=lookup_weather,
)


def audit(ctx, config):
    """Observability plugin: print the loop's bus events, no loop access."""

    def on_step(step, content, calls):
        print(f"[step {step}] content={content!r} tool_calls={calls}")

    def on_tool(name, args, ok, output):
        print(f"[tool] {name}({args}) -> {'ok' if ok else 'ERR'}: {output}")

    return [ctx.on("agent/step", on_step), ctx.on("agent/tool", on_tool)]


async def main() -> None:
    root = Context()
    await root.plugin(audit)

    # The agent loads FIRST and stays PENDING until the llm is provided —
    # plugin load order never matters.
    agent_view = root.plugin(AgentLoop, {"tools": [WEATHER], "max_steps": 8})
    assert root.get("agent", strict=False) is None, "gated on the llm service"

    llm_view = root.plugin(ScriptedLLM, {"responses": [
        # 1) the model asks for the weather tool
        LLMResponse(tool_calls=[ToolCall(id="w1", name="get_weather", arguments={"city": "Oslo"})]),
        # 2) it sees the tool result and answers
        LLMResponse(content="Oslo is 21 degrees under a clear sky."),
    ]})
    await llm_view
    await agent_view

    answer = await root.get("agent").run("What's the weather in Oslo?")
    print(f"answer: {answer}")

    await root.fiber.dispose()  # tears down audit listeners + both plugins


if __name__ == "__main__":
    asyncio.run(main())
