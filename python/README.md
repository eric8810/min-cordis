# min-cordis (Python)

The minimal Cordis core ported to Python: **context, services, plugins, fiber lifecycle, and the event bus**.

Ported from the trimmed TypeScript core ([../README.md](../README.md)), itself derived from Cordis 4.0.0-rc.7. Zero runtime dependencies; Python 3.11+ (asyncio).

## Translation map (TS → Python)

| TypeScript | Python |
|---|---|
| `Symbol.for` | interned string table (`min_cordis._utils.sym`) |
| Proxy get/set traps | `__getattr__` + `reflect.get/set` |
| Prototype-chain scopes | parent pointer + isolate label lookup |
| `await Promise.resolve()` checkpoint | `await asyncio.sleep(0)` |
| thenable fiber wrapper | `_FiberView` delegating to the real fiber |
| WeakMap DisposableList | dict + `weakref.WeakKeyDictionary` |
| AggregateError | `ExceptionGroup` |

Baked-in audit fixes (same as the TS core): emit containment for async listener rejections, per-callback `internal/status` containment, `update()` validates before storing, lifecycle calls re-anchor to `ctx.fiber`.

## Run tests

```sh
uv sync
uv run pytest -q
```
