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

import types
from typing import Any, Callable

from ._events import Events
from ._fiber import Fiber, FiberState
from ._traceable import SHADOW, _find_class_member, _unwrap, get_traceable
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
    """Service directory: name -> implementation, keyed by isolate label.

    The store is keyed by the label *object* itself (identity hashing), not
    by ``id()``: an id can be reused after garbage collection, silently
    merging two scopes (audit item C4).
    """

    def __init__(self, root: "Context") -> None:
        self.root = root
        self.store: dict[Any, dict] = {}
        self.props: dict[str, Any] = {}  # name -> 'service' | accessor dict

    def _label_for(self, ctx: Any, name: str) -> Any | None:
        obj = _label_object(ctx, name)
        return obj

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
                raise RuntimeError(f'property "{name}" is already declared as {self._prop_type(name)}')
            self.props[name] = "service"
            # The label is the nearest one in the PROVIDING context's isolate
            # chain, falling back to a fresh symbol interned on the root map
            # (TS ``root[symbols.isolate][name] ??= Symbol(name)``).
            label_obj = _label_object(ctx, name)
            if label_obj is None:
                label_obj = self.root._isolate.setdefault(name, object())
            if label_obj in self.store:
                other = self.store[label_obj]
                raise RuntimeError(f'service "{name}" has been registered at <{other["fiber"].name}>')
            impl = {"name": name, "value": value, "fiber": fiber, "check": check}
            self.store[label_obj] = impl
            assert fiber.store is not None
            fiber.store[name] = impl
            if fiber.state == FiberState.ACTIVE:
                self.notify([name], ctx)

            async def unregister() -> None:
                self.store.pop(label_obj, None)
                fibers = self.notify([name], ctx)
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

    def notify(self, names: list[str], source_ctx: Any = None) -> list[Any]:
        """Re-evaluate every fiber that requires one of ``names``.

        The isolation filter compares against the PROVIDING context's scope
        (TS: notify's default filter reads ``this.ctx[symbols.isolate]``
        through the traceable view, so the comparison base is the calling
        context, not the root). Fibers outside the providing scope keep
        their current dependency view.
        """
        source = source_ctx if source_ctx is not None else self.root
        fibers: list[Any] = []
        for runtime in list(self.root.registry.values()):
            for fiber in list(runtime.fibers):
                updated = False
                for name in names:
                    if name not in fiber.inject:
                        continue
                    if self._label_for(fiber.ctx, name) != self._label_for(source, name):
                        continue
                    updated = True
                    fiber._check_impl(name)
                if updated:
                    fiber._refresh_deps()
                    fibers.append(fiber)
        # Broadcast internal/service per changed name (C2 fix), resolved in
        # the providing scope.
        for name in names:
            impl = self._get_impl_for(source, name, strict=False)
            value = None if impl is None else impl["value"]
            self.root.events.emit("internal/service", name, value)
        return fibers

    def _current_ctx(self) -> "Context":
        # The reflect service is always used with an explicit ctx in this port;
        # the root context backs direct calls.
        return self.root

    def _prop_type(self, name: str) -> str:
        defn = self.props.get(name)
        if isinstance(defn, dict):
            return defn.get("type", "accessor")
        return defn or "unknown"

    def accessor(self, name: str, options: dict, ctx: Any | None = None) -> Callable[[], Any]:
        """Define a computed context property backed by get/set hooks.

        ``options['get']`` is called as ``get(ctx, receiver)``; ``options['set']``
        as ``set(ctx, value, receiver) -> bool``. ``receiver`` is the traceable
        view the read went through for associated (dotted) access, else None.
        The accessor is removed when the owning fiber unloads.
        """
        ctx = ctx or self._current_ctx()

        def run() -> Callable[[], None]:
            if name in self.props:
                raise RuntimeError(f'property "{name}" is already declared as {self._prop_type(name)}')
            self.props[name] = {"type": "accessor", **options}

            def remove() -> None:
                if self.props.get(name) is not None and self.props.get(name) != "service":
                    self.props.pop(name, None)

            return remove

        return ctx.fiber.effect(run, f"ctx.accessor({name!r})")

    def mixin(self, source: Any, mixins: Any, ctx: Any | None = None) -> Callable[[], Any]:
        """Expose selected members of a service directly on ``ctx``.

        ``mixins`` is a list of member names (exposed under the same name) or
        a ``{service member -> context property}`` map. Each entry becomes an
        accessor forwarding to ``ctx[source]`` (TS ``getTarget = ctx[source]``).
        Methods run bound to a mixin receiver: the service overlaid with the
        reading view (TS ``withProps(receiver, service)``), so associated
        reads keep their shadow context.

        An object ``source`` reproduces the TS resolution quirk: ``ctx[object]``
        is not a valid context property, so reads succeed nowhere — on a
        plugin context they raise ``cannot get property ... without inject``
        and on the root context they resolve to None (lenient lookup).
        """
        ctx = ctx or self._current_ctx()
        entries = dict(mixins) if isinstance(mixins, dict) else {key: key for key in mixins}

        def run() -> Callable[[], None]:
            disposers = [self.accessor(ctx_name, _mixin_accessor(source, member), ctx=ctx) for member, ctx_name in entries.items()]

            def remove() -> None:
                for dispose in reversed(disposers):
                    dispose()

            return remove

        return ctx.fiber.effect(run, f"ctx.mixin({source!r})")


def _mixin_target(ctx: Any, source: Any) -> Any:
    """Resolve the mixin source on the reading context (TS ``ctx[source]``)."""
    if isinstance(source, str):
        return getattr(ctx, source)
    # Object source: no context property can match an instance key. TS walks
    # the fiber chain and raises on plugin contexts, or resolves leniently to
    # undefined on the root context (verified against the upstream suite).
    if ctx.fiber.runtime is not None:
        raise RuntimeError(f'cannot get property "{source!r}" without inject')
    return None


class _MixinBind:
    """Method-binding receiver for mixin accessors (TS ``withProps(receiver, service)``).

    Attribute reads prefer the service; missing names fall back to the
    reading view. Writes go through to the raw service target, honoring
    property setters defined on its class.
    """

    def __init__(self, service: Any, receiver: Any) -> None:
        object.__setattr__(self, "_service", service)
        object.__setattr__(self, "_receiver", receiver)

    @property
    def __class__(self) -> type:
        return type(_unwrap(self._service))

    def __getattr__(self, name: str) -> Any:
        try:
            return getattr(self._service, name)
        except AttributeError:
            return getattr(self._receiver, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_") and name in ("_service", "_receiver"):
            object.__setattr__(self, name, value)
            return
        target = _unwrap(self._service)
        for klass in type(target).__mro__:
            if name in klass.__dict__:
                member = klass.__dict__[name]
                if isinstance(member, property):
                    if member.fset is None:
                        raise AttributeError(f"property {name!r} has no setter")
                    member.fset(self, value)
                    return
                break
        object.__setattr__(target, name, value)


def _mixin_member(service: Any, member: str) -> tuple[str, Any]:
    return _find_class_member(_unwrap(service), member)


def _mixin_accessor(source: Any, member: str) -> dict:
    """Build the get/set hooks forwarding ``ctx[source].member`` (TS mixin)."""

    def get(ctx: Any, receiver: Any) -> Any:
        service = _mixin_target(ctx, source)
        if service is None:
            return None
        bind = _MixinBind(service, receiver) if receiver is not None else _unwrap(service)
        kind, value = _mixin_member(service, member)
        if kind == "instance":
            return value
        if kind == "class":
            if isinstance(value, property):
                return get_traceable(ctx, value.fget(bind))
            if isinstance(value, (staticmethod, classmethod)):
                return value.__get__(None, type(_unwrap(service)))
            if isinstance(value, types.FunctionType):
                return _bind_raw_function(value, bind, ctx)
            if hasattr(value, "__get__"):
                return value.__get__(bind, type(_unwrap(service)))
            return value
        raise AttributeError(f"mixin source has no member {member!r}")

    def set_(ctx: Any, value: Any, receiver: Any) -> bool:
        service = _mixin_target(ctx, source)
        if service is None:
            return False
        bind = _MixinBind(service, receiver) if receiver is not None else _unwrap(service)
        target = _unwrap(service)
        for klass in type(target).__mro__:
            if member in klass.__dict__:
                desc = klass.__dict__[member]
                if isinstance(desc, property):
                    if desc.fset is None:
                        raise AttributeError(f"property {member!r} has no setter")
                    desc.fset(bind, value)
                    return True
                break
        object.__setattr__(target, member, value)
        return True

    return {"get": get, "set": set_}


def _bind_raw_function(fn: Callable, bind: Any, ctx: Any) -> Callable:
    def method(*args: Any, **kwargs: Any) -> Any:
        return get_traceable(ctx, fn(bind, *args, **kwargs))

    method.__name__ = getattr(fn, "__name__", "method")
    return method


def _bind_associated_function(fn: Callable, receiver: Any, ctx: Any) -> Callable:
    """Bind an associated service function to the reading view (JS ``this``)."""

    def method(*args: Any, **kwargs: Any) -> Any:
        return get_traceable(ctx, fn(receiver, *args, **kwargs))

    method.__name__ = getattr(fn, "__name__", "method")
    return method


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
        from ._logger import LoggerService
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

        # Built-in logger service (TS installs LoggerService on the root
        # fiber). Stored as its traceable view so ``ctx.logger('name')`` is
        # callable through the service invoke body.
        self.logger = get_traceable(self, LoggerService(self))
        # Detach the built-in services' bootstrap effects so the root fiber
        # stays immortal across a hypothetical root dispose (TS parity:
        # ``this.fiber._disposables.clear()`` in the Context constructor).
        self.fiber._disposables.clear()

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
        # Accessor properties resolve without an inject requirement (TS
        # handler checks the accessor before the inject error). Reads require
        # a declared inject on every context (tighter than the TS
        # root-context leniency, per the audit finding); `ctx.get(name)` is
        # the opportunistic escape hatch. Reads through a service's shadow
        # context follow the provider's dependency chain instead.
        if name.startswith("_"):
            raise AttributeError(name)
        defn = self.reflect.props.get(name)
        if isinstance(defn, dict) and defn.get("type") == "accessor":
            return defn["get"](self, None)
        if getattr(self, SHADOW, None) is not None:
            return self._resolve_walk(name)
        if self.fiber.runtime is None:
            # TS resolves root-context reads leniently without the
            # internal/get waterfall; the Python port keeps the tightened
            # error instead.
            if name not in self._effective_inject():
                raise RuntimeError(f'cannot get property "{name}" without inject')
            value = self.reflect.get(name, strict=True, ctx=self)
            if value is None:
                raise RuntimeError(f'cannot get required service "{name}" in inactive context')
            return value

        def fallback(*_: Any) -> Any:
            if name not in self._effective_inject():
                raise RuntimeError(f'cannot get property "{name}" without inject')
            value = self.reflect.get(name, strict=True, ctx=self)
            if value is None:
                raise RuntimeError(f'cannot get required service "{name}" in inactive context')
            return value

        error = RuntimeError(f'cannot get property "{name}" without inject')
        return self.events.waterfall("internal/get", self, name, error, fallback)

    def _resolve_dotted(self, name: str, receiver: Any) -> Any:
        """Associated (dotted) resolution for traceable views.

        Accessor properties run with the reading view as receiver (TS
        ``withProp(ctx, symbols.receiver, receiver)``); service properties
        walk the fiber chain. A raw-function service value is bound to the
        reading view: JS ``ctx.foo.baz()`` passes the proxy as ``this``,
        Python has no implicit receiver, so the binding is explicit.
        """
        defn = self.reflect.props.get(name)
        if isinstance(defn, dict) and defn.get("type") == "accessor":
            return defn["get"](self, receiver)
        value = self._resolve_walk(name)
        if isinstance(value, types.FunctionType):
            return _bind_associated_function(value, receiver, self)
        return value

    def _resolve_dotted_set(self, name: str, value: Any, receiver: Any) -> None:
        defn = self.reflect.props.get(name)
        if isinstance(defn, dict) and defn.get("type") == "accessor":
            if "set" not in defn or defn["set"] is None:
                raise AttributeError(f"accessor {name!r} has no setter")
            if not defn["set"](self, value, receiver):
                raise AttributeError(f"accessor {name!r} rejected the write")
            return
        self.reflect.set(name, value, ctx=self)

    def _resolve_walk(self, name: str) -> Any:
        """The TS ReflectService get fallback: walk the fiber store chain.

        Starts at the shadow provider's fiber (or the reading fiber), finds
        the nearest store entry, and stops at root or at an isolation
        boundary. Root-context reads stay lenient (non-strict lookup). The
        walk itself runs inside the ``internal/get`` waterfall, so listeners
        may intercept or synthesize service reads (TS parity).
        """
        marker = getattr(self, SHADOW, None)
        start = marker if marker is not None else self
        fiber = start.fiber
        if marker is None and fiber.runtime is None:
            return self.reflect.get(name, strict=False, ctx=self)

        def fallback(*_: Any) -> Any:
            root_label = self.root._isolate.get(name)
            walk = fiber
            while True:
                impl = walk.store.get(name) if walk.store else None
                if impl is not None:
                    return get_traceable(self, impl["value"])
                if name in walk.inject:
                    raise RuntimeError(f'cannot get required service "{name}" in inactive context')
                if walk.runtime is None:
                    raise RuntimeError(f'cannot get property "{name}" without inject')
                if _label_object(walk.parent, name) is not root_label:
                    raise RuntimeError(f'cannot get property "{name}" without inject')
                walk = walk.parent.fiber

        error = RuntimeError(f'cannot get property "{name}" without inject')
        return self.events.waterfall("internal/get", self, name, error, fallback)

    def _effective_inject(self) -> set[str]:
        """Names readable on this context: own injects plus every ancestor's.

        TS resolves attribute reads by walking the fiber parent chain, so a
        plugin sees services its ancestors injected (subject to isolation).
        The Python port keeps the tightened error for names nobody declared
        anywhere in the chain (`ctx.get` remains the escape hatch).
        """
        names = set(self.__dict__.get("_inject_requested", ()))
        parent = self._parent
        hops = 0
        while parent is not None and hops < 64:
            names |= parent.__dict__.get("_inject_requested", set())
            parent = parent._parent
            hops += 1
        return names

    # Core state lives directly on the instance; service values flow through
    # reflect.provide (TS set trap routes everything else through validation).
    _CORE_ATTRS = frozenset({"root", "events", "reflect", "registry", "fiber", "on_error", "baseUrl"})

    def __setattr__(self, name: str, value: Any) -> None:
        # Underscore names and core bootstrap attributes are plain state.
        if name.startswith("_") or name in Context._CORE_ATTRS or getattr(self, "reflect", None) is None:
            object.__setattr__(self, name, value)
            return
        stripped = self._stripped()
        # TS parity: the root context (no runtime) accepts plain writes;
        # plugin contexts go through the internal/set waterfall into
        # reflect.set, which requires the name to have been provided by the
        # same fiber (audit item C5).
        if self.fiber.runtime is None:
            object.__setattr__(self, name, value)
            return

        def fallback(*_: Any) -> Any:
            self.reflect.set(name, value, ctx=stripped)
            return True

        error = RuntimeError(f'cannot set property "{name}" without provide')
        self.events.waterfall("internal/set", stripped, name, value, error, fallback)

    # ------------------------------------------------------------- facade

    def get(self, name: str, strict: bool = True) -> Any:
        return self.reflect.get(name, strict=strict, ctx=self._stripped())

    def set(self, name: str, value: Any) -> None:
        self.reflect.set(name, value, ctx=self._stripped())

    def provide(self, name: str, value: Any = None, check: Callable[..., bool] | None = None) -> Callable[[], Any]:
        return self.reflect.provide(name, value, check, ctx=self._stripped())

    def accessor(self, name: str, options: dict) -> Callable[[], Any]:
        return self.reflect.accessor(name, options, ctx=self._stripped())

    def mixin(self, source: Any, mixins: Any) -> Callable[[], Any]:
        return self.reflect.mixin(source, mixins, ctx=self._stripped())

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
