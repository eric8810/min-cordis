# min-cordis (Python)

The minimal Cordis core ported to Python: **context, services, plugins, fiber lifecycle, the event bus, and the traceable caller/caller-shadow machinery**.

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
| `getTraceable` proxy | `get_traceable` → `TraceableView` (see below) |
| `Symbol.hasInstance` dances | `__class__` property on wrappers (`isinstance` just works) |
| `joinPrototype(Service, Function)` callables | any object with `_invoke` + `__call__` dispatch |
| `Service[symbols.init/check/invoke/extend]` | `_init` / `_check` / `_invoke` / `_extend` hook attributes |
| `@Inject` decorators | `Inject(name, config)` on classes and service methods |
| `internal/get`/`internal/set` waterfalls | same names through `Events.waterfall` |

Baked-in audit fixes (same as the TS core): emit containment for async listener rejections, per-callback `internal/status` containment, `update()` validates before storing, lifecycle calls re-anchor to `ctx.fiber`.

## Services and tracing

```python
from min_cordis import Context, Service, Inject, Tracker

class Database(Service):
    inject = ["logger"]                 # dependency declaration

    def __init__(self, ctx, config=None):
        super().__init__(ctx, "database")
        self.config = self._resolve_config(config)   # merged intercept config

    def query(self, sql):
        # self.ctx is the caller's context extended with a shadow marker
        # pointing at this service's provider scope; nested reads below
        # resolve along THIS service's dependency chain.
        self.ctx.logger.info(sql)
        ...

    async def _init(self): ...          # awaited before the fiber reports ACTIVE
    def _check(self): ...               # availability predicate (self = traceable view)
    def _invoke(self, *args): ...       # makes the service callable: ctx.database(...)

root = Context()
await root.plugin(Database, {"pool": 4})   # class plugins construct + run _init
db = root.get("database")                  # traceable view bound at access time
db.query("select 1")
```

Rules ported from the TS core:

- **Binding follows the reference**: `svc = ctx.name` fixes the binding at access
  time; later calls keep it.
- **`self.ctx` inside a service method** is the caller's context extended with
  `_shadow = provider ctx`; `self._caller` reports the accessing service.
- **Shadow stripping**: plugins created from inside service methods attach to
  the real consumer scope (upstream shadow.spec).
- **`Tracker`** objects (`_tracker` attribute) drive wrapping; `no_shadow=True`
  marks identity-aware services.
- **`ctx.accessor(name, {get, set})` / `ctx.mixin(source, mixins)`** define
  computed context properties; mixin methods bind to a service-overlaid
  receiver so associated reads keep their shadow context.
- **Logger service**: `ctx.logger('name')` → named logger, `MIN_CORDIS_LOG`
  threshold, bounded `errors` ring; `internal/get`/`internal/set` waterfalls
  wrap service resolution on plugin contexts.

Intentional deviations (documented in [docs/design-python-traceable.md](../docs/design-python-traceable.md)):

- Attribute reads require a declared inject on every context (the TS root
  context is lenient); `ctx.get(name)` is the explicit escape hatch. Tests
  adapted accordingly.
- `composeError` long-stack splicing is not ported (V8 stack surgery has no
  CPython equivalent); use standard tracebacks.
- `_label_object` walks the context parent chain — equivalent to TS prototype
  lookups, and it also fixes labels interned on the root map after a plugin
  context was created.

## Parity notes

- **Logger service**: `ctx.logger('name')` returns a level-filtered `Logger`
  (`MIN_CORDIS_LOG` threshold); `ctx.logger.error(...)` records into a bounded
  ring. Error containment still routes to the injected `on_error` sink — the
  logger is the user-facing surface, the sink is the diagnostics surface.
- **`internal/get` / `internal/set` waterfalls**: service reads on plugin
  contexts and attribute writes dispatch through the event bus before
  resolution; listeners may intercept (return a value / short-circuit) or
  delegate via `next()`. Root-context reads stay outside the get waterfall
  (TS parity).
- `registry.delete` is fire-and-forget, exactly like the TS registry (the
  upstream snapshot test drains it with `sleep()`); covered by
  `test_compare_snapshot`.
- Object-source `ctx.mixin(instance, ...)` reproduces the upstream
  resolution quirk: plugin-context reads raise `cannot get property ...
  without inject`; root-context reads resolve to None. The upstream
  associate.spec #4 inner block is dead code (waits for a never-provided
  service; verified by probe) and is not ported.
- Attribute reads require an inject declared **somewhere on the context
  ancestor chain** (own ∪ ancestors), mirroring the TS fiber-chain walk;
  names nobody declared still fail loudly (`ctx.get` is the escape hatch).

## Run tests

```sh
uv sync
uv run pytest -q                    # 48 tests
uv run pytest -q -W error::RuntimeWarning
```
