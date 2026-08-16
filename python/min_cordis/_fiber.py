"""Fiber: one plugin activation's runtime state and effect ledger.

Semantics ported from the TypeScript core (vendor/cordis + audit fixes):

- ``PENDING -> LOADING -> ACTIVE -> ...`` state machine; a throwing
  ``internal/status`` observer no longer stalls dependency notification
  (per-callback containment).
- Effects register disposers; teardown runs in reverse order.
- Epoch = provider-fiber uids joined; any change reloads the dependent.
- ``update()`` validates and resolves config *before* storing it, so a
  rejected update cannot poison later reloads.
- Lifecycle mutation re-anchors to ``self.ctx.fiber``; the registry may hand
  out wrapper views, and lifecycle state must always live on the real fiber.
"""

from __future__ import annotations

import asyncio
from enum import IntEnum
from typing import Any, Callable

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
        self.uid = None

        if runtime is not None:
            self.uid = registry.counter
            self.ctx = parent.extend(fiber=self)
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

            registry.parent_fiber_disposables.push(dispose_child)
            self._dispose_child = dispose_child
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

        Supports: a disposer callable, a list/tuple of disposers, or an async
        callable returning any of those. Disposers run in reverse order when
        the returned disposer (or the fiber) tears down.
        """
        self.assert_active()
        if self.state == FiberState.UNLOADING:
            raise CordisError(CordisError.INACTIVE_EFFECT)

        disposables: list[Callable[[], Any]] = []
        removing = False

        def run_disposers() -> Any:
            nonlocal removing
            if removing:
                return None
            removing = True
            snapshot = list(disposables)
            disposables.clear()

            async def run_all() -> None:
                for d in reversed(snapshot):
                    result = d()
                    if asyncio.iscoroutine(result):
                        await result

            return run_all()

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

        result = execute()
        if asyncio.iscoroutine(result):

            async def async_collect() -> None:
                collect(await result)

            # Fire-and-settle: collect as soon as the coroutine resolves; the
            # disposer still runs on teardown.
            task = asyncio.ensure_future(async_collect())
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            remove = self._disposables.push(run_disposers)
        else:
            collect(result)
            remove = self._disposables.push(run_disposers)

        def dispose() -> Any:
            remove()
            return run_disposers()

        return dispose

    # ------------------------------------------------------------- lifecycle

    def get_effects(self) -> list[str]:
        return [label for label in self._labels()]

    def _labels(self) -> list[str]:
        return getattr(self, "_effect_labels", [])

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
        if check is not None and not check():
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
        instance_or_result = callback(self.ctx, self.config)
        if asyncio.iscoroutine(instance_or_result):
            instance_or_result = await instance_or_result
        # A plugin body IS an effect: a returned callable/list is collected as
        # its disposer(s), exactly like `ctx.effect`.
        if instance_or_result is not None:
            self._collect_plugin_disposer(instance_or_result)
        return instance_or_result

    def _collect_plugin_disposer(self, value: Any) -> None:
        if callable(value) or isinstance(value, (list, tuple)):
            snapshot = [value] if callable(value) else list(value)
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
