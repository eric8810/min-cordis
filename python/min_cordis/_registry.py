"""Plugin registry: normalize plugin shapes and start fibers."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from ._fiber import Fiber
from ._utils import DisposableList


def resolve_inject(inject: Any) -> dict[str, Any]:
    """Normalize inject declarations (list or dict) into a plain map."""
    result: dict[str, Any] = {}
    if inject is None:
        return result
    if isinstance(inject, dict):
        result.update(inject)
    else:
        for name in inject:
            result[name] = None
    return result


class _FiberView:
    """Awaitable wrapper over the real fiber.

    The Python port keeps lifecycle methods on the real fiber and delegates
    through this view, so wrapper-vs-real desync (audit C1) cannot occur:
    ``update``/``restart`` look up ``ctx.fiber`` first.
    """

    def __init__(self, fiber: Fiber) -> None:
        object.__setattr__(self, "_fiber", fiber)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fiber, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._fiber, name, value)

    def __await__(self):
        return self._fiber.await_fiber().__await__()


class Runtime:
    """Registry record shared by all fibers of one plugin callback."""

    def __init__(self, name: str | None, callback: Callable, config_validator: Any) -> None:
        self.name = name
        self.callback = callback
        self.Config = config_validator
        self.fibers = DisposableList()


class Registry:
    """Plugin registry service (``ctx.registry``)."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self._counter = 0
        self._internal: dict[Callable, Runtime] = {}
        self.parent_fiber_disposables = DisposableList()

    @property
    def counter(self) -> int:
        self._counter += 1
        return self._counter

    def values(self) -> list[Runtime]:
        return list(self._internal.values())

    def _normalize(self, plugin: Any) -> tuple[Callable, str | None, Any, Any] | None:
        if isinstance(plugin, dict) and callable(plugin.get("apply")):
            return plugin["apply"], plugin.get("name"), plugin.get("Config"), plugin.get("inject")
        if isinstance(plugin, type) and hasattr(plugin, "apply"):
            return getattr(plugin, "apply"), getattr(plugin, "name", plugin.__name__), getattr(plugin, "Config", None), getattr(plugin, "inject", None)
        if callable(plugin):
            return plugin, getattr(plugin, "name", None), getattr(plugin, "Config", None), getattr(plugin, "inject", None)
        return None

    def plugin(self, plugin: Any, config: Any = None, parent: Any = None) -> _FiberView:
        normalized = self._normalize(plugin)
        if normalized is None:
            raise TypeError(
                "invalid plugin, expect callable, a class with apply, or a dict with an 'apply' key, received "
                + type(plugin).__name__
            )
        callback, name, config_validator, inject = normalized
        parent = parent or self.ctx
        parent.fiber.assert_active()

        runtime = self._internal.get(callback)
        if runtime is None:
            if name in (None, "apply"):
                name = None
            runtime = Runtime(name, callback, config_validator)
            self._internal[callback] = runtime

        fiber = Fiber(
            parent=parent,
            config=config,
            inject=resolve_inject(inject),
            runtime=runtime,
            registry=self,
            on_error=self.ctx.on_error,
        )
        # The plugin context declares its injected names so attribute reads can
        # distinguish "not injected" from "injected but inactive".
        fiber.ctx._inject_requested = set(fiber.inject.keys())
        runtime.fibers.push(fiber)
        return _FiberView(fiber)

    def inject(self, deps: Any, callback: Callable, parent: Any = None) -> _FiberView:
        def wrapped(ctx: Any, config: Any) -> Any:
            return callback(ctx, config)

        wrapped.inject = resolve_inject_dict(deps)
        return self.plugin(wrapped, parent=parent)

    def has(self, callback: Callable) -> bool:
        return callback in self._internal

    def delete_by_callback(self, callback: Callable) -> Runtime | None:
        return self._internal.pop(callback, None)

    def delete(self, plugin: Any) -> None:
        normalized = self._normalize(plugin)
        if normalized is None:
            return
        callback = normalized[0]
        runtime = self._internal.get(callback)
        if runtime is None:
            return
        del self._internal[callback]
        for fiber in list(runtime.fibers):
            asyncio.ensure_future(fiber.dispose())


def resolve_inject_dict(deps: Any) -> dict[str, Any]:
    return resolve_inject(deps)
