# min-cordis

The minimal Cordis core: **context, services, plugins, fiber lifecycle, and the event bus** — nothing else.

Derived from [Cordis](https://github.com/cordiverse/cordis) `4.0.0-rc.7` (vendored at [deepseek-harness/vendor/cordis](https://github.com/eric8810/deepseek-harness) with local patches), trimmed after a three-wave, fifteen-agent audit. MIT license preserved; see [LICENSE](LICENSE).

## What it is

```ts
import { Context, Service } from 'min-cordis'

const ctx = new Context()

// A plugin: declare dependencies, contribute services, register effects.
const fiber = ctx.inject(['llm'], (c) => {
  const dispose = c.on('agent/start', () => { /* ... */ })
  return () => dispose() // optional: explicit teardown (also automatic)
})

// A service: subclass Service, register on ctx by name.
class Llm extends Service {
  constructor(c: any) { super(c, 'llm') }
  complete(prompt: string) { return `echo: ${prompt}` }
}
```

Everything on the hot path of the original core is kept:

- `Context` with its proxy resolver (`ctx.get/set/provide/accessor/mixin`)
- `Fiber` lifecycle (`PENDING → LOADING → ACTIVE → …`), effects with reverse-order disposal, epoch-based dependency reload
- `EventsService` with all five dispatch modes (`emit/parallel/serial/bail/waterfall`)
- `RegistryService` (`ctx.plugin/inject`), `Service` base class, traceable proxies
- `isolate()` / `intercept()` / `extend()` scoping
- Standard Schema config validation — bring **any** validator (zod, valibot, arktype):

```ts
import { z } from 'zod'

const Config = z.object({ retries: z.number().int().min(0).default(3) })
// zod 4 implements Standard Schema; min-cordis calls `Config['~standard'].validate`
const fiber = ctx.plugin(myPlugin, { retries: 5 }) // or with the schema attached to the plugin
```

## What was removed, and why

The trim follows measured usage in deepseek-harness (grep-verified) and the audit's bug distribution:

| Removed | Why |
|---|---|
| **loader / include / group** (~1,400 lines) | Runtime plugin-tree surgery (transactions, cross-tree `move`, id addressing) was a Koishi chat-bot scenario. Hosts that need config → plugins can compose a flat list at boot and diff/re-mount on change. This structurally eliminates the audit's worst findings: parent-cycle infinite loop, cross-tree move data loss, YAML-alias group sharing. |
| **hmr** (~600 lines + chokidar/picomatch/esbuild) | Module-level hot reload was unused in installed deployments (everything under `node_modules` is ignored). Restart the process in dev; watch config files with a 30-line `fs.watch` wrapper if live config is needed. |
| **schemastery** (900 lines) | Replaced by Standard Schema + any modern validator. Removes the global-refs poisoning, exponential-union DoS, and serialized-callback `new Function` hazards. |
| **cosmokit** (400 lines) | The 3 functions actually used (`defineProperty`, `isNullable`, type helpers) are inlined. The rest was an unused toolbox carrying `deepEqual` Map/Set bugs. |
| **logger (full) / logger-console / timer** | Replaced by a ~60-line level-filtered console logger. Bring your own logger service if you need buffers/exporters; the core only calls `ctx.logger.error` for error containment. |

Runtime dependencies: **zero** (devDependency: tsx for running tests directly on TypeScript source).

## Audit fixes applied on top of rc.7 + deepseek-harness patches

1. **Wrapper lifecycle desync (critical)** — `ctx.plugin()` keeps the prototype wrapper (needed so `await` resolves without thenable recursion), but `restart()`/`update()` re-anchor to `this.ctx.fiber` before mutating lifecycle state (the direction of upstream commit `752dbee`). Without this, config updates through the handle silently desync the real fiber and HMR-style reloads roll config back.
2. **`emit` swallows async listener rejections** — rejections now route to `ctx.logger.error` instead of escaping as process-level `unhandledRejection`.
3. **Throwing `internal/status` observer stalls dependents** — status emission is per-callback contained (same treatment as `internal/plugin`), so one bad observer can no longer leave dependents `PENDING` forever.
4. **Rejected `update()` poisons later reloads** — config is validated and resolved *before* `_config` is touched; a rejected update leaves the previous config intact.
5. **`EventsService._hooks` was a plain object** — now `Object.create(null)`; registering/dispatching `__proto__`/`constructor`/`toString` event names no longer crashes.
6. **`parallel` reported itself as `emit`** on `internal/dispatch`; now reports `parallel` (the mode type is no longer a dead value).

## Known limitations (inherited, documented)

These are core-design positions, not bugs — kept identical to upstream:

- **Plugins are trusted.** A plugin with `ctx` access is process-equivalent JS; there is no isolation layer in the core.
- Same-fiber `ctx.set()` value swaps do not notify dependents (reload happens via provider lifecycle, not object identity).
- `Service.check` predicates run on every dependency re-evaluation; a throwing predicate is contained and logged.
- The traceable-proxy layer means `ctx.foo !== ctx.foo` for tracked services (fresh wrapper per read). Compare via `[symbols.original]` if needed.

## Run

```sh
npm test
```

Two suites:

- `tests/*.spec.ts` — the upstream Cordis core test suite (11 spec files, 62 tests: fiber/inertia, events, reflect, isolate, shadow/caller tracing, invoke, service, plugin, decorator, dispose, associate), ported with only import-path changes. One upstream test asserting the full logger's buffer was adapted to the minimal logger's `errors` ring; `logger.spec.ts` itself is not ported (the full logger is not part of min-cordis).
- `test/core.test.ts` — regression tests written for each audit fix above.

Node >= 22.19 (TypeScript is executed directly through `tsx`; the package ships `src/` as its export). Tests use vitest 3 — the upstream `Inject` decorator targets Stage-3 native decorators, so `experimentalDecorators` must stay off.

## Python port

[python/](python/) carries the same core to Python 3.11+ (asyncio), zero runtime dependencies, with the audit fixes carried over and a TS→Python translation map in its README.

## Provenance

Source of record: `vendor/cordis` @ deepseek-harness (upstream cordis `4.0.0-rc.7`, commit `56b3d4f`, plus the fiber-lifecycle and lazy-config local patches logged in the vendor README). Trimming and fixes documented above; audit reports live in the harness repo under `docs/research/notes/`.
