"""Context: scoped dependency container with attribute-style service reads.

The JS Proxy becomes ``__getattr__``/``__setattr__`` checks: reading an
undeclared name requires it to be injected (or fails loudly), writing
requires being the provider. Prototype-chain scoping becomes a parent
pointer with per-name lookup through the isolate map.
"""

from __future__ import annotations

from typing import Any, Callable

from ._events import Events
from ._fiber import Fiber, FiberState
from ._utils import INTERCEPT, ISOLATE


class ReflectService:
    """Service directory: name -> implementation, keyed by isolate label."""

    def __init__(self, root: "Context") -> None:
        self.root = root
        self.store: dict[int, dict] = {}
        self.props: dict[str, str] = {}  # name -> 'service' | 'accessor'

    def _label_for(self, ctx: Any, name: str) -> int | None:
        labels = ctx._isolate
        if name in labels:
            return id(labels[name])
        parent = getattr(ctx, "_parent_isolate", None)
        while parent is not None:
            if name in parent:
                return id(parent[name])
            parent = parent.get("__parent__")
        return None

    def _get_impl_for(self, ctx: Any, name: str, strict: bool = True) -> dict | None:
        label = self._label_for(ctx, name)
        if label is None:
            return None
        impl = self.store.get(label)
        if impl is None:
            return None
        if strict and impl["fiber"].state != FiberState.ACTIVE:
            return None
        return impl

    def _get_impl(self, name: str, strict: bool = True) -> dict | None:
        label = self._label_for(self.root, name)
        if label is None:
            return None
        impl = self.store.get(label)
        if impl is None:
            return None
        if strict and impl["fiber"].state != FiberState.ACTIVE:
            return None
        return impl

    def provide(self, name: str, value: Any, check: Callable[[], bool] | None = None, ctx: Any | None = None) -> Callable[[], Any]:
        """Register a service owned by the calling context's fiber.

        ``ctx`` is the context the provide call was made on (plugin contexts
        pass their own); defaults to the root for direct calls.
        """
        ctx = ctx or self._current_ctx()
        fiber = ctx.fiber

        def run() -> Callable[[], Any]:
            if name in self.props and self.props[name] != "service":
                raise RuntimeError(f'property "{name}" is already declared as {self.props[name]}')
            self.props[name] = "service"
            # The label is resolved in the PROVIDING context's isolate scope,
            # so an isolated subtree registers under its own label.
            label_obj = ctx._isolate.setdefault(name, object()) if name in ctx._isolate else self.root._isolate.setdefault(name, object())
            label = id(label_obj)
            if label in self.store:
                other = self.store[label]
                raise RuntimeError(f'service "{name}" has been registered at <{other["fiber"].name}>')
            impl = {"name": name, "value": value, "fiber": fiber, "check": check}
            self.store[label] = impl
            assert fiber.store is not None
            fiber.store[name] = impl
            if fiber.state == FiberState.ACTIVE:
                self.notify([name])

            async def unregister() -> None:
                self.store.pop(label, None)
                fibers = self.notify([name])
                await asyncio_gather_all(fibers)
                if fiber.store is not None:
                    fiber.store.pop(name, None)

            return unregister

        return fiber.effect(run, f"ctx.provide({name!r})")

    def get(self, name: str, strict: bool = True, ctx: Any | None = None) -> Any:
        ctx = ctx or self._current_ctx()
        impl = self._get_impl_for(ctx, name, strict)
        return None if impl is None else impl["value"]

    def set(self, name: str, value: Any, ctx: Any | None = None) -> None:
        ctx = ctx or self._current_ctx()
        label = self._label_for(ctx, name)
        if label is None or label not in self.store:
            raise RuntimeError(f'cannot set property "{name}" without provide')
        impl = self.store[label]
        if impl["fiber"] is not ctx.fiber:
            raise RuntimeError(f'cannot set property "{name}" in multiple fibers')
        impl["value"] = value

    def notify(self, names: list[str]) -> list[Any]:
        fibers: list[Any] = []
        for runtime in list(self.root.registry.values()):
            for fiber in list(runtime.fibers):
                updated = False
                for name in names:
                    if name not in fiber.inject:
                        continue
                    if self._label_for(fiber.ctx, name) != self._label_for(self.root, name):
                        continue
                    updated = True
                    fiber._check_impl(name)
                if updated:
                    fiber._refresh_deps()
                    fibers.append(fiber)
        # Broadcast internal/service per changed name (C2 fix).
        for name in names:
            impl = self._get_impl_for(self.root, name, strict=False)
            self.root.events.emit("internal/service", name, None if impl is None else impl["value"])
        return fibers

    def _current_ctx(self) -> "Context":
        # The reflect service is always used with an explicit ctx in this port;
        # the root context backs direct calls.
        return self.root


async def asyncio_gather_all(fibers: list[Any]) -> None:
    import asyncio

    results = []
    for fiber in fibers:
        results.append(fiber.await_fiber())
    await asyncio.gather(*results, return_exceptions=True)


class Context:
    """Root and child dependency containers."""

    def __init__(self, on_error: Callable[[BaseException], None] | None = None) -> None:
        self._isolate: dict[str, Any] = {}
        self._intercept: dict[str, Any] = {}
        self._parent_isolate: dict[str, Any] | None = None
        self._parent_intercept: dict[str, Any] | None = None
        self._parent: Context | None = None
        self.root: Context = self
        self._fiber: Fiber | None = None
        self._declared: set[str] = set()
        self._inject_requested: set[str] = set()

        from ._registry import Registry
        self.events = Events(self)
        self.reflect = ReflectService(self)
        self.registry = Registry(self)
        self.on_error = on_error or _default_error_sink

        self.fiber = Fiber(
            parent=self,
            config={},
            inject={},
            runtime=None,
            registry=self.registry,
            on_error=self.on_error,
        )

    # ---------------------------------------------------------------- fiber

    @property
    def fiber(self) -> Fiber:
        return self._fiber  # type: ignore[return-value]

    @fiber.setter
    def fiber(self, value: Fiber) -> None:
        self._fiber = value

    # ------------------------------------------------------------ scoping

    def extend(self, **meta: Any) -> "Context":
        child = Context.__new__(Context)
        child.__dict__.update(self.__dict__)
        child._parent = self
        child._parent_isolate = self._isolate
        child._parent_intercept = self._intercept
        child._isolate = dict(self._isolate) if meta.get("fiber") is not None else self._isolate
        child._intercept = self._intercept
        child._inject_requested = set(self._inject_requested)
        for key, value in meta.items():
            setattr(child, key, value)
        return child

    def isolate(self, name: str, label: Any | None = None) -> "Context":
        child = self.extend()
        child._isolate = dict(self._isolate)
        child._isolate[name] = label if label is not None else object()
        return child

    def intercept(self, name: str, config: Any) -> "Context":
        child = self.extend()
        child._intercept = dict(self._intercept)
        child._intercept[name] = config
        return child

    # --------------------------------------------------- attribute service

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires when normal lookup fails; services land here.
        # Reads require a declared inject on every context (tighter than the
        # TS root-context leniency, per the audit finding); `ctx.get(name)` is
        # the opportunistic escape hatch.
        if name.startswith("_"):
            raise AttributeError(name)
        injected = name in self._effective_inject()
        if not injected:
            raise RuntimeError(f'cannot get property "{name}" without inject')
        value = self.reflect.get(name, strict=True, ctx=self)
        if value is not None:
            return value
        raise RuntimeError(f'cannot get required service "{name}" in inactive context')

    def _effective_inject(self) -> set[str]:
        return self.__dict__.get("_inject_requested", set())

    def __setattr__(self, name: str, value: Any) -> None:
        # The core stores its own state in underscore attributes and the five
        # builtin services/events/registry directly; service *values* flow
        # through reflect.provide and never land here.
        object.__setattr__(self, name, value)

    # ------------------------------------------------------------- facade

    def get(self, name: str, strict: bool = True) -> Any:
        return self.reflect.get(name, strict=strict, ctx=self)

    def set(self, name: str, value: Any) -> None:
        self.reflect.set(name, value, ctx=self)

    def provide(self, name: str, value: Any, check: Callable[[], bool] | None = None) -> Callable[[], Any]:
        return self.reflect.provide(name, value, check, ctx=self)

    def effect(self, execute: Callable[[], Any], label: str = "anonymous") -> Callable[[], Any]:
        return self.fiber.effect(execute, label)

    def plugin(self, plugin: Any, config: Any = None) -> Any:
        """Load a plugin under this context; returns an awaitable fiber view."""
        return self.registry.plugin(plugin, config, parent=self)

    def inject_plugins(self, deps: Any, callback: Callable) -> Any:
        """Run a callback once the requested services are available."""
        return self.registry.inject(deps, callback, parent=self)

    def on(self, name: Any, listener: Callable, options: bool | dict | None = None) -> Callable[[], None]:
        return self.events.on(name, listener, options, ctx=self)

    def once(self, name: Any, listener: Callable, options: bool | dict | None = None) -> Callable[[], None]:
        return self.events.once(name, listener, options, ctx=self)

    async def parallel(self, *args: Any) -> None:
        await self.events.parallel(*args)

    def emit(self, *args: Any) -> None:
        self.events.emit(*args)

    async def serial(self, *args: Any) -> Any:
        return await self.events.serial(*args)

    def bail(self, *args: Any) -> Any:
        return self.events.bail(*args)

    def waterfall(self, *args: Any) -> Any:
        return self.events.waterfall(*args)

    async def dispose(self) -> None:
        await self.fiber.restart()


def _default_error_sink(exc: BaseException) -> None:
    import sys

    print(f"[min-cordis] contained error: {exc!r}", file=sys.stderr)
