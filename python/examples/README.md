# Examples

Reference applications built **on** the min-cordis kernel. They live outside
the core package on purpose: the kernel stays minimal, examples show how its
pieces compose.

## agent_loop — a basic agent loop

The classic perceive → think (LLM) → act (tool) → observe cycle, assembled
strictly through the plugin machinery:

- `LLMClient` — the model backend, provided as the `llm` service
  (`ScriptedLLM` is the deterministic stand-in; subclass for a real client).
- `AgentLoop` — provided as the `agent` service with `inject = ["llm"]`:
  the fiber stays PENDING until the llm exists, `_init` binds it before the
  fiber reports ACTIVE, and load order between the two plugins never matters.
- `ToolSpec` — declarative tools (JSON-schema parameters, sync or async
  `run`); errors and unknown tools are fed back to the model as `error: ...`
  results instead of crashing the loop.
- Observability — every step emits `agent/step` and every tool call
  `agent/tool` on the event bus; listeners attach via `ctx.on` and unload
  with their fiber.

Run the demo (no network, fully scripted):

```sh
cd python
uv run python examples/demo.py
```

The example's tests run with the regular suite (`uv run pytest -q`) and also
document the failure modes: `max_steps` exhaustion (`AgentLoopError`),
cooperative `stop()` between steps (`AgentStopped`), and the fail-fast when
the loop bypasses `ctx.plugin` assembly.
