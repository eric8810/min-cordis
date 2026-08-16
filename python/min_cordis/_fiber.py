"""Fiber: one plugin activation's runtime state and effect ledger.

Semantics ported from the TypeScript core (vendor/cordis + audit fixes):

- ``PENDING -> LOADING -> ACTIVE -> ...`` state machine; a throwing
  ``internal/status`` observer no longer stalls dependency notification
  (per-callback containment).
- Effects register disposers; teardown runs in reverse order. Async setups
  are barrier-protected: a dispose that arrives before setup settles waits
  for it (no leaked disposers).
- Plugin bodies are effects: a returned disposer, list of disposers, or a
  (async) generator yielding disposers is collected the same way.
- Epoch = provider-fiber uids joined; any change reloads the dependent.
  Constructor performs the initial dependency scan so providers mounted
  BEFORE consumers still activate them.
- A child fiber's disposal is an effect on its PARENT's ledger, so unloading
  the parent tears down the whole subtree.
- ``update()`` validates and resolves config *before* storing it, so a
  rejected update cannot poison later reloads.
- Lifecycle mutation re-anchors to ``self.ctx.fiber``; the registry may hand
  out wrapper views, and lifecycle state must always live on the real fiber.
"""

from __future__ import annotations

import asyncio
import inspect
from enum import IntEnum
from typing import Any, Callable

from ._service import collect_inject_hooks
from ._traceable import get_traceable
from ._utils import DisposableList

INACTIVE = "__INACTIVE__"


class FiberState(IntEnum):
    PENDING = 0
    LOADING = 1
    ACTIVE = 2
    FAILED = 3
    DISPOSED = 4
    UNLOADING = 5


class CordisError(RuntimeError):
    """Framework error with a stable code."""

    INACTIVE_EFFECT = "cannot create effect on inactive context"

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class ValidationError(TypeError):
    """Raised when plugin config fails schema validation."""


def resolve_config(runtime: Any, config: Any) -> Any:
    """Validate config through a standard-schema-like validator, if present."""
    validator = getattr(runtime, "Config", None)
    if validator is None:
        return config
    result = validator(config)
    if isinstance(result, Exception):
        raise result
    if isinstance(result, tuple):  # (ok, normalized) style
        ok, normalized = result
        if not ok:
            raise ValidationError(str(normalized))
        return normalized
    return result


def _iterate_effect(value: Any):
    """Return an iterator over disposers for generator-like effects."""
    if inspect.isgenerator(value) or inspect.isasyncgen(value):
        return value
    if hasattr(value, "__iter__") and not isinstance(value, (list, tuple, str, bytes, dict)):
        return iter(value)
    return None


class Fiber:
    """Runtime instance of one plugin application."""

    uid: int | None
    state: FiberState
    inject: dict[str, Any]
    runtime: Any | None
    parent: Any  # Context
    ctx: Any  # Context

    def __init__(
        self,
        parent: Any,
        config: Any,
        inject: dict[str, Any],
        runtime: Any | None,
        registry: Any,
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._config = config
        self.config = None
        self.inject = inject
        self.runtime = runtime
        self.parent = parent
        self.registry = registry
        self.on_error = on_error
        self.state = FiberState.PENDING
        self.store: dict[str, Any] | None = None
        self.inertia: asyncio.Task[None] | None = None
        self._error: BaseException | None = None
        self._disposables = DisposableList()
        self._hooks: dict[str, DisposableList] = {}
        self._epoch: Any = INACTIVE
        self._store: dict[str, Any] = {}
        self._effect_labels: list[tuple[str, int]] = []
        self.uid = None

        if runtime is not None:
            self.uid = registry.counter
            self.ctx = parent.extend(fiber=self)
            # Inject declarations carrying config become intercept entries on
            # the plugin context (TS Object.create(parent intercept) + own).
            own_intercept = {k: v for k, v in self.inject.items() if v is not None}
            if own_intercept:
                self.ctx._intercept = dict(own_intercept)
            # Emit internal/plugin through the bus with containment.
            parent.events._emit_plugin_created(self)

            def dispose_child() -> Any:
                self.uid = None
                parent.events._emit_plugin_disposed(self)
                if registry.has(runtime.callback):
                    runtime.fibers.delete(self)
                    if len(runtime.fibers) == 0:
                        registry.delete_by_callback(runtime.callback)
                self._set_epoch(INACTIVE)

                async def drain() -> None:
                    if self.inertia is None:
                        def transition() -> FiberState:
                            self.inertia = asyncio.ensure_future(self._unload())
                            return FiberState.UNLOADING
                        self._update_state(transition)
                    # Wait until the unload chain (and any reload it cascades
                    # into) settles, so store entries are gone when we return.
                    while self.inertia is not None:
                        await asyncio.wait([self.inertia])

                return drain()

            # F1 fix: the child's disposal is an effect on the PARENT fiber's
            # ledger, so unloading the parent tears down the whole subtree.
            parent.fiber._disposables.push(dispose_child)
            self._dispose_child = dispose_child
            # F2 fix: initial dependency scan, so providers mounted before
            # this fiber still satisfy its injects immediately.
            for name in self.inject:
                self._check_impl(name)
            self._refresh_deps()
        else:
            self.uid = 0
            self.ctx = parent
            self.state = FiberState.ACTIVE
            self.store = {}

    # ---------------------------------------------------------------- effects

    @property
    def name(self) -> str:
        """Display name inherited from the nearest named ancestor, else 'root'."""
        fiber: Fiber = self
        while True:
            if fiber.runtime is not None and fiber.runtime.name:
                return fiber.runtime.name
            if fiber.parent is fiber.ctx:
                return "root"
            parent_fiber = fiber.parent.fiber
            if parent_fiber is fiber:
                return "root"
            fiber = parent_fiber

    def assert_active(self) -> None:
        if self.uid is None:
            raise CordisError(CordisError.INACTIVE_EFFECT)

    def effect(self, execute: Callable[[], Any], label: str = "anonymous") -> Callable[[], Any]:
        """Run ``execute`` now; collect the disposer(s) it yields.

        Supported effect shapes (mirroring the TS core):
        - a disposer callable
        - a list/tuple of disposers
        - a (sync or async) generator yielding disposers
        - an async callable returning any of the above
        - a coroutine resolving to any of the above

        Disposers run in reverse order when the returned disposer (or the
        fiber) tears down. A dispose that arrives while an async setup is
        still settling waits for the setup first (setup barrier).
        """
        self.assert_active()
        if self.state == FiberState.UNLOADING:
            raise CordisError(CordisError.INACTIVE_EFFECT)

        disposables: list[Callable[[], Any]] = []
        removing = False
        setup_task: asyncio.Task | None = None

        def collect(value: Any) -> None:
            if value is None:
                return
            if callable(value):
                disposables.append(value)
                return
            if isinstance(value, (list, tuple)):
                for item in value:
                    collect(item)
                return
            raise TypeError("Invalid effect")

        def run_disposers() -> Any:
            nonlocal removing
            if removing:
                return None
            removing = True

            async def run_all() -> None:
                # Setup barrier: wait for a still-settling async setup so its
                # disposer(s) are collected before teardown (F3 fix).
                if setup_task is not None and not setup_task.done():
                    try:
                        await setup_task
                    except BaseException as exc:
                        self.on_error(exc)
                snapshot = list(disposables)
                disposables.clear()
                for d in reversed(snapshot):
                    result = d()
                    if asyncio.iscoroutine(result):
                        await result

            return run_all()

        # Drive the setup: handle generators by draining them eagerly (each
        # yielded disposer is registered as produced), and coroutines by a
        # barrier task.
        setup_coro: Any = None

        result = execute()

        if inspect.isasyncgen(result):
            async def drain_async_gen() -> None:
                async for item in result:
                    collect(item)
            setup_coro = drain_async_gen()
        elif inspect.isgenerator(result):
            for item in result:
                collect(item)
        elif asyncio.iscoroutine(result):
            async def await_then_collect() -> None:
                value = await result
                gen = _iterate_effect(value)
                if gen is None:
                    collect(value)
                    return
                if inspect.isasyncgen(gen):
                    async for item in gen:
                        collect(item)
                else:
                    for item in gen:
                        collect(item)
            setup_coro = await_then_collect()
        elif (gen := _iterate_effect(result)) is not None:
            for item in gen:
                collect(item)
        else:
            collect(result)

        if setup_coro is not None:
            setup_task = asyncio.ensure_future(setup_coro)
            setup_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        remove = self._disposables.push(run_disposers)
        self._effect_labels.append((label, id(disposables)))

        def dispose() -> Any:
            remove()
            self._effect_labels = [e for e in self._effect_labels if e[1] != id(disposables)]
            return run_disposers()

        return dispose

    # ------------------------------------------------------------- lifecycle

    def get_effects(self) -> list[str]:
        """Labels of currently registered effects (flat list)."""
        return [label for label, _ in self._effect_labels]

    def _get_state(self) -> FiberState:
        if self.uid is None:
            return FiberState.DISPOSED
        if self._error is not None:
            return FiberState.FAILED
        if self._epoch != INACTIVE:
            return FiberState.ACTIVE
        return FiberState.PENDING

    def _update_state(self, callback: Callable[[], FiberState | None]) -> None:
        old = self.state
        result = callback()
        self.state = result if result is not None else self._get_state()
        if old == self.state:
            return
        self.ctx.events._emit_status_contained(self, old)
        if old != FiberState.ACTIVE and self.state != FiberState.ACTIVE:
            return
        # Notify dependents about services provided by this fiber.
        for impl in list(self.ctx.reflect.store.values()):
            if impl["fiber"] is self:
                self.ctx.reflect.notify([impl["name"]])

    def _check_impl(self, name: str) -> None:
        impl = self.ctx.reflect._get_impl_for(self.ctx, name)
        if impl is None:
            self._store.pop(name, None)
            return
        check = impl.get("check")
        if check is not None and not check(get_traceable(self.ctx, impl["value"])):
            # TS invokes `impl.check.call(traceableView)`; here the view is
            # passed as the first argument (`self` of a Service `_check`).
            self._store.pop(name, None)
            return
        self._store[name] = impl

    def _refresh_deps(self) -> None:
        epoch: str = ""
        for name in self.inject:
            impl = self._store.get(name)
            if impl is None:
                epoch = INACTIVE
                break
            epoch += f":{impl['fiber'].uid}"
        self._set_epoch(epoch)

    def _set_epoch(self, epoch: Any) -> None:
        old = self._epoch
        if epoch == old:
            return
        self._epoch = epoch
        if self.inertia is not None:
            return

        def transition() -> FiberState:
            if epoch != INACTIVE and old == INACTIVE:
                self.inertia = asyncio.ensure_future(self._reload())
                return FiberState.LOADING
            self.inertia = asyncio.ensure_future(self._unload())
            return FiberState.UNLOADING

        self._update_state(transition)

    async def _reload(self) -> None:
        self.store = dict(self._store)
        old_epoch = self._epoch
        try:
            await asyncio.sleep(0)  # microtask checkpoint, like `await Promise.resolve()`
            if self._epoch == old_epoch:
                self.config = self._resolve_config(self._config)
                await self._execute()
                self._error = None
        except BaseException as exc:
            self.on_error(exc)
            self._error = exc
            self._epoch = INACTIVE

        def settle() -> FiberState | None:
            if self._epoch == old_epoch:
                self.inertia = None
                return None
            self.inertia = asyncio.ensure_future(self._unload())
            return FiberState.UNLOADING

        self._update_state(settle)

    async def _execute(self) -> Any:
        callback = self.runtime.callback
        if isinstance(callback, type):
            # Class plugin (e.g. a Service subclass): construct, run @Inject
            # init hooks, then await/collect the `_init` result — never the
            # instance itself (TS `new callback(ctx, config)` + initHooks +
            # `instance[symbols.init]?.()`).
            instance = callback(self.ctx, self.config)
            if getattr(instance, "_init_hooks", None) is None:
                instance._init_hooks = collect_inject_hooks(instance)
            for hook in list(instance._init_hooks or []):
                hook()
            init = getattr(instance, "_init", None)
            value = init() if init is not None else None
        else:
            value = callback(self.ctx, self.config)
        # A plugin body IS an effect: a returned disposer, list, or generator
        # is collected exactly like `ctx.effect` (F4 fix).
        gen = _iterate_effect(value)
        if inspect.isasyncgen(value):
            async for item in value:
                self._collect_one(item)
            return None
        if asyncio.iscoroutine(value):
            value = await value
            gen = _iterate_effect(value)
        if gen is not None:
            for item in gen:
                self._collect_one(item)
            return None
        if value is not None:
            self._collect_one(value)
        return value

    def _collect_one(self, value: Any) -> None:
        if value is None:
            return
        # A FiberView (or Fiber) return is a mounted child plugin: its
        # lifecycle is already owned by the parent ledger, not a disposer.
        from ._registry import _FiberView

        if isinstance(value, (_FiberView, Fiber)):
            return
        if callable(value):
            self._collect_plugin_disposer([value])
            return
        if isinstance(value, (list, tuple)):
            self._collect_plugin_disposer(list(value))
            return
        raise TypeError("Invalid effect")

    def _collect_plugin_disposer(self, snapshot: list[Callable[[], Any]]) -> None:
        removing = {"done": False}

        def run_disposers() -> Any:
            if removing["done"]:
                return None
            removing["done"] = True

            async def run_all() -> None:
                for d in reversed(snapshot):
                    result = d()
                    if asyncio.iscoroutine(result):
                        await result

            return run_all()

        self._disposables.push(run_disposers)

    def _resolve_config(self, config: Any) -> Any:
        config = self.ctx.events._run_config_waterfall(self, config)
        return resolve_config(self.runtime, config) if self.runtime is not None else config

    async def _unload(self) -> None:
        for dispose in self._disposables.clear():
            try:
                result = dispose()
                if asyncio.iscoroutine(result):
                    await result
            except BaseException as exc:
                self.on_error(exc)
        self.store = None

        def settle() -> FiberState | None:
            if self._epoch == INACTIVE:
                self.inertia = None
                return None
            self.inertia = asyncio.ensure_future(self._reload())
            return FiberState.LOADING

        self._update_state(settle)

    async def await_fiber(self) -> "Fiber":
        while self.inertia is not None:
            await asyncio.wait([self.inertia])
        if self._error is not None:
            raise self._error
        return self

    def __await__(self):
        return self.await_fiber().__await__()

    async def dispose(self) -> None:
        await self.ctx.events._dispose_fiber_via_parent(self)

    async def restart(self) -> None:
        fiber = self.ctx.fiber  # re-anchor: wrappers delegate lifecycle to the real fiber
        fiber.assert_active()
        fiber._set_epoch(INACTIVE)
        fiber._refresh_deps()
        await fiber.await_fiber()

    def update(self, config: Any, no_save: bool = False) -> Any:
        fiber = self.ctx.fiber  # re-anchor (audit fix C1)
        fiber.assert_active()
        if fiber.state != FiberState.ACTIVE:
            # Defer validation until activation.
            fiber._config = config
            fiber._error = None
            fiber._set_epoch(INACTIVE)
            fiber._refresh_deps()
            return None
        resolved = fiber._resolve_config(config)  # validate BEFORE storing (audit fix)
        def apply() -> Any:
            fiber._config = config
            fiber.config = resolved
            fiber._error = None
            return fiber.restart()
        return fiber.ctx.events._run_update_waterfall(fiber, resolved, no_save, apply)
