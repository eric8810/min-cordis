"""Context: scoped dependency container with attribute-style service reads.

The JS Proxy becomes ``__getattr__``/``__setattr__`` checks: reading an
undeclared name requires it to be injected (or fails loudly), writing
requires being the provider. Prototype-chain scoping becomes a parent
pointer with per-name lookup through the isolate map.

Service reads return *traceable views* bound to the reading context
(:mod:`min_cordis._traceable`). Reads made through a service's shadow
context resolve along the provider's fiber chain (``_resolve_walk``, the
TS ``ReflectService.handler.get`` fallback), so nested access inside a
service method sees the service's own dependency scope.
"""

from __future__ import annotations

from typing import Any, Callable

from ._events import Events
from ._fiber import Fiber, FiberState
from ._traceable import SHADOW, get_traceable
from ._utils import INTERCEPT, ISOLATE


def _label_object(ctx: Any, name: str) -> Any:
    """Nearest isolate label for ``name`` walking the context parent chain.

    Equivalent to the TS prototype-chain read ``ctx[symbols.isolate][name]``
    (nearest map wins; flattened per-fiber copies keep the same values).
    """
    while ctx is not None:
        labels = getattr(ctx, "_isolate", None)
        if labels is not None and name in labels:
            return labels[name]
        ctx = getattr(ctx, "_parent", None)
    return None


class ReflectService:
    """Service directory: name -> implementation, keyed by isolate label."""

    def __init__(self, root: "Context") -> None:
        self.root = root
        self.store: dict[int, dict] = {}
        self.props: dict[str, str] = {}  # name -> 'service' | 'accessor'

    def _label_for(self, ctx: Any, name: str) -> int | None:
        obj = _label_object(ctx, name)
        return id(obj) if obj is not None else None

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
        return self._get_impl_for(self.root, name, strict)

    def provide(
        self,
        name: str,
        value: Any = None,
        check: Callable[..., bool] | None = None,
        ctx: Any | None = None,
    ) -> Callable[[], Any]:
        """Register a service owned by the calling context's fiber.

        ``ctx`` is the context the provide call was made on (plugin contexts
        pass their own); defaults to the root for direct calls. ``check`` is
        invoked by dependents as ``check(traceable_view)`` (TS: called as a
        method of the traced service).
        """
        ctx = ctx or self._current_ctx()
        fiber = ctx.fiber

        def run() -> Callable[[], Any]:
            if name in self.props and self.props[name] != "service":
                raise RuntimeError(f'property "{name}" is already declared as {self.props[name]}')
            self.props[name] = "service"
            # The label is the nearest one in the PROVIDING context's isolate
            # chain, falling back to a fresh symbol interned on the root map
            # (TS ``root[symbols.isolate][name] ??= Symbol(name)``).
            label_obj = _label_object(ctx, name)
            if label_obj is None:
                label_obj = self.root._isolate.setdefault(name, object())
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
        value = None if impl is None else impl["value"]
        return get_traceable(ctx, value)

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
        self._shadow: Any = None
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

    def _stripped(self) -> "Context":
        """The context below any service-shadow markers (TS binding strip)."""
        ctx = self
        while getattr(ctx, SHADOW, None) is not None:
            ctx = ctx._parent
        return ctx

    def isolate(self, name: str, label: Any | None = None) -> "Context":
        child = self.extend()
        child._isolate = dict(self._isolate)
        child._isolate[name] = label if label is not None else object()
        return child

    def intercept(self, name: str, config: Any) -> "Context":
        child = self.extend()
        # Own-entry map over the parent's (TS Object.create chain): reads walk
        # the context parent chain, so ancestor entries for the same name are
        # preserved instead of flattened away.
        child._intercept = {name: config}
        return child

    def _intercept_entries(self, name: str) -> list[Any]:
        """Intercept entries for ``name``, root first, deduplicated by map."""
        entries: list[Any] = []
        seen: set[int] = set()
        ctx: Any = self
        while ctx is not None:
            intercept = getattr(ctx, "_intercept", None)
            if intercept is not None and id(intercept) not in seen:
                seen.add(id(intercept))
                if name in intercept:
                    entries.append(intercept[name])
            ctx = getattr(ctx, "_parent", None)
        entries.reverse()
        return entries

    # --------------------------------------------------- attribute service

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires when normal lookup fails; services land here.
        # Reads require a declared inject on every context (tighter than the
        # TS root-context leniency, per the audit finding); `ctx.get(name)` is
        # the opportunistic escape hatch. Reads through a service's shadow
        # context follow the provider's dependency chain instead.
        if name.startswith("_"):
            raise AttributeError(name)
        if getattr(self, SHADOW, None) is not None:
            return self._resolve_walk(name)
        if name not in self._effective_inject():
            raise RuntimeError(f'cannot get property "{name}" without inject')
        value = self.reflect.get(name, strict=True, ctx=self)
        if value is None:
            raise RuntimeError(f'cannot get required service "{name}" in inactive context')
        return value

    def _resolve_walk(self, name: str) -> Any:
        """The TS ReflectService get fallback: walk the fiber store chain.

        Starts at the shadow provider's fiber (or the reading fiber), finds
        the nearest store entry, and stops at root or at an isolation
        boundary. Root-context reads stay lenient (non-strict lookup).
        """
        marker = getattr(self, SHADOW, None)
        start = marker if marker is not None else self
        fiber = start.fiber
        if marker is None and fiber.runtime is None:
            return self.reflect.get(name, strict=False, ctx=self)
        root_label = self.root._isolate.get(name)
        while True:
            impl = fiber.store.get(name) if fiber.store else None
            if impl is not None:
                return get_traceable(self, impl["value"])
            if name in fiber.inject:
                raise RuntimeError(f'cannot get required service "{name}" in inactive context')
            if fiber.runtime is None:
                raise RuntimeError(f'cannot get property "{name}" without inject')
            if _label_object(fiber.parent, name) is not root_label:
                raise RuntimeError(f'cannot get property "{name}" without inject')
            fiber = fiber.parent.fiber

    def _effective_inject(self) -> set[str]:
        return self.__dict__.get("_inject_requested", set())

    def __setattr__(self, name: str, value: Any) -> None:
        # The core stores its own state in underscore attributes and the five
        # builtin services/events/registry directly; service *values* flow
        # through reflect.provide and never land here.
        object.__setattr__(self, name, value)

    # ------------------------------------------------------------- facade

    def get(self, name: str, strict: bool = True) -> Any:
        return self.reflect.get(name, strict=strict, ctx=self._stripped())

    def set(self, name: str, value: Any) -> None:
        self.reflect.set(name, value, ctx=self._stripped())

    def provide(self, name: str, value: Any = None, check: Callable[..., bool] | None = None) -> Callable[[], Any]:
        return self.reflect.provide(name, value, check, ctx=self._stripped())

    def effect(self, execute: Callable[[], Any], label: str = "anonymous") -> Callable[[], Any]:
        return self.fiber.effect(execute, label)

    def plugin(self, plugin: Any, config: Any = None) -> Any:
        """Load a plugin under this context; returns an awaitable fiber view.

        The parent is the shadow-stripped context: plugins created from
        inside service methods attach to the real consumer scope (upstream
        shadow.spec "strips service shadow before creating plugins").
        """
        return self.registry.plugin(plugin, config, parent=self._stripped())

    def inject_plugins(self, deps: Any, callback: Callable) -> Any:
        """Run a callback once the requested services are available."""
        return self.registry.inject(deps, callback, parent=self._stripped())

    def on(self, name: Any, listener: Callable, options: bool | dict | None = None) -> Callable[[], None]:
        return self.events.on(name, listener, options, ctx=self._stripped())

    def once(self, name: Any, listener: Callable, options: bool | dict | None = None) -> Callable[[], None]:
        return self.events.once(name, listener, options, ctx=self._stripped())

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
