"""Tests for the agent-loop example (examples/agent_loop.py).

Everything is assembled through the plugin machinery: ScriptedLLM and
AgentLoop load via ``ctx.plugin(...)``, and inject gating ties the agent's
start to the llm service becoming available.
"""

from __future__ import annotations

import asyncio

import pytest

from min_cordis import Context
from agent_loop import (
    AgentLoop,
    AgentLoopError,
    AgentStopped,
    LLMResponse,
    ScriptedLLM,
    ToolCall,
    ToolSpec,
)

WEATHER = ToolSpec(
    name="get_weather",
    description="current weather for a city",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}},
    run=lambda city: {"city": city, "temp": 30},
)


@pytest.fixture
def errors():
    collected: list[BaseException] = []
    return collected


@pytest.fixture
def root(errors):
    return Context(on_error=errors.append)


async def load_agent(root, responses, tools=(), **agent_config):
    """Assemble llm + agent through the plugin machinery; return (views, llm)."""
    llm_view = root.plugin(ScriptedLLM, {"responses": list(responses)})
    agent_view = root.plugin(AgentLoop, {"tools": list(tools), **agent_config})
    await llm_view
    await agent_view
    return agent_view, llm_view, root.get("llm")


def _tool_messages(llm, call_index: int) -> list[dict]:
    return [m for m in llm.calls[call_index]["messages"] if m["role"] == "tool"]


async def test_direct_answer_without_tools(root):
    *_, llm = await load_agent(root, [LLMResponse(content="42")])

    assert await root.get("agent").run("meaning of life?") == "42"

    # the model saw exactly the user message and no tool schemas
    assert llm.calls[0]["messages"] == [{"role": "user", "content": "meaning of life?"}]
    assert llm.calls[0]["tools"] == []
    assert root.get("agent").steps == 1


async def test_tool_round_trip(root):
    *_, llm = await load_agent(
        root,
        [
            LLMResponse(tool_calls=[ToolCall(id="1", name="get_weather", arguments={"city": "Oslo"})]),
            LLMResponse(content="Oslo: 30 degrees"),
        ],
        [WEATHER],
    )

    assert await root.get("agent").run("weather in Oslo?") == "Oslo: 30 degrees"

    # the second model call saw the assistant tool-call AND the tool result
    tool_messages = _tool_messages(llm, 1)
    assert tool_messages == [
        {
            "role": "tool",
            "tool_call_id": "1",
            "name": "get_weather",
            "content": '{"city": "Oslo", "temp": 30}',
        }
    ]
    # the schemas were offered on every call
    assert llm.calls[1]["tools"][0]["function"]["name"] == "get_weather"


async def test_multiple_tool_calls_run_in_order(root):
    order: list = []

    def add(a: int, b: int):
        order.append((a, b))
        return a + b

    add_tool = ToolSpec(
        name="add",
        description="sum two integers",
        parameters={"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}},
        run=add,
    )
    *_, llm = await load_agent(
        root,
        [
            LLMResponse(tool_calls=[
                ToolCall(id="t1", name="add", arguments={"a": 1, "b": 2}),
                ToolCall(id="t2", name="add", arguments={"a": 3, "b": 4}),
            ]),
            LLMResponse(content="done"),
        ],
        [add_tool],
    )

    assert await root.get("agent").run("add twice") == "done"
    assert order == [(1, 2), (3, 4)]
    # both results were appended before the next model call, in order
    tool_messages = _tool_messages(llm, 1)
    assert [m["tool_call_id"] for m in tool_messages] == ["t1", "t2"]
    assert [m["content"] for m in tool_messages] == ["3", "7"]


async def test_tool_error_is_fed_back_not_fatal(root):
    def boom(x: int):
        raise ValueError("boom")

    bad = ToolSpec(
        name="bad",
        description="always fails",
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
        run=boom,
    )
    *_, llm = await load_agent(
        root,
        [
            LLMResponse(tool_calls=[ToolCall(id="e1", name="bad", arguments={"x": 1})]),
            LLMResponse(content="recovered"),
        ],
        [bad],
    )

    assert await root.get("agent").run("try it") == "recovered"
    fed = _tool_messages(llm, 1)[0]
    assert fed["content"] == "error: ValueError('boom')"


async def test_unknown_tool_reported_to_model(root):
    *_, llm = await load_agent(
        root,
        [
            LLMResponse(tool_calls=[ToolCall(id="u1", name="nope", arguments={})]),
            LLMResponse(content="ok"),
        ],
    )

    assert await root.get("agent").run("call something odd") == "ok"
    fed = _tool_messages(llm, 1)[0]
    assert "unknown tool" in fed["content"]


async def test_async_tool_supported(root):
    async def double(x: int):
        await asyncio.sleep(0)
        return x * 2

    tool = ToolSpec(
        name="double",
        description="async doubling",
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
        run=double,
    )
    *_, llm = await load_agent(
        root,
        [
            LLMResponse(tool_calls=[ToolCall(id="d1", name="double", arguments={"x": 4})]),
            LLMResponse(content="8 it is"),
        ],
        [tool],
    )

    assert await root.get("agent").run("double 4") == "8 it is"
    assert _tool_messages(llm, 1)[0]["content"] == "8"


async def test_max_steps_bounds_the_loop(root):
    endless = [
        LLMResponse(tool_calls=[ToolCall(id=f"c{i}", name="get_weather", arguments={"city": "x"})])
        for i in range(10)
    ]
    *_, llm = await load_agent(root, endless, [WEATHER], max_steps=3)

    with pytest.raises(AgentLoopError, match="max_steps=3"):
        await root.get("agent").run("run forever")
    assert len(llm.calls) == 3


async def test_stop_cancels_between_steps(root):
    def stopper():
        root.get("agent").stop()
        return "stopping"

    tool = ToolSpec(
        name="stop",
        description="request cancellation",
        parameters={"type": "object", "properties": {}},
        run=stopper,
    )
    *_, llm = await load_agent(
        root,
        [
            LLMResponse(tool_calls=[ToolCall(id="s1", name="stop", arguments={})]),
            LLMResponse(content="never reached"),
        ],
        [tool],
    )

    with pytest.raises(AgentStopped):
        await root.get("agent").run("go")
    # the loop stopped after the first tool round, before the next model call
    assert len(llm.calls) == 1


async def test_inject_gating_waits_for_llm(root):
    # load the agent BEFORE the llm: the fiber must stay PENDING and the
    # service must not exist until the llm plugin provides it.
    agent_view = root.plugin(AgentLoop, {"tools": [WEATHER]})
    await asyncio.sleep(0.05)
    assert root.get("agent", strict=False) is None

    llm_view = root.plugin(ScriptedLLM, {"responses": [LLMResponse(content="late")]})
    await llm_view
    await agent_view  # resolves once gating passed and _init bound the llm

    assert await root.get("agent").run("hi") == "late"


async def test_run_without_plugin_start_fails_fast(root):
    # Constructing the Service directly bypasses the fiber: no gating, no
    # _init, so run() must refuse instead of firing with a missing llm.
    agent = AgentLoop(root, {"tools": [WEATHER]})
    with pytest.raises(RuntimeError, match="ctx.plugin"):
        await agent.run("hi")
    await root.fiber.dispose()


async def test_events_flow_and_unload_with_fiber(root):
    events: list = []

    def audit_plugin(c, cfg):
        return [
            c.on("agent/step", lambda step, content, calls: events.append(("step", step, content, calls))),
            c.on("agent/tool", lambda name, args, ok, out: events.append(("tool", name, ok))),
        ]

    view = root.plugin(audit_plugin)
    await view

    agent_view, llm_view, llm = await load_agent(
        root,
        [
            LLMResponse(tool_calls=[ToolCall(id="1", name="get_weather", arguments={"city": "Oslo"})]),
            LLMResponse(content="30"),
        ],
        [WEATHER],
    )
    assert await root.get("agent").run("w") == "30"

    assert events == [
        ("step", 1, None, 1),
        ("tool", "get_weather", True),
        ("step", 2, "30", 0),
    ]

    # listeners unload with their fiber, and a disposed agent pair can be
    # reloaded in the same root: no stale listeners, no duplicate services.
    await agent_view.dispose()
    await llm_view.dispose()
    await view.dispose()
    assert root.get("agent", strict=False) is None

    agent_view2, _, _ = await load_agent(root, [LLMResponse(content="again")])
    assert await root.get("agent").run("again") == "again"
    assert len(events) == 3
