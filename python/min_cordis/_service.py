"""Service base class and the ``@Inject`` decorator.

Ported from the TS core's ``Service`` and ``Inject``. A service registers
itself on construction (``super().__init__(ctx, name)`` →
``ctx.reflect.provide``) and is removed with its owning fiber. The tracing
metadata consumed by :mod:`min_cordis._traceable` is installed here.

Overridable hook attributes (the Python stand-ins for the TS symbol keys):

- ``_invoke(self, *args)`` — the call body of a callable service
  (``Service.invoke``).
- ``_init(self)`` — post-construction initialization; the fiber awaits it
  before reporting ACTIVE (``Service.init``).
- ``_check(self)`` — availability predicate consulted by dependents
  (``Service.check``); runs with ``self`` bound to the *traceable view*.
- ``Config.merge(configs)`` — custom intercept-config merge used by
  ``_resolve_config``.
- class attr ``provide`` — default service name.
- class attr ``inject`` — dependency declaration (list or name→config map).
"""

from __future__ import annotations

import types
from typing import Any, Callable, ClassVar

from ._traceable import Tracker, _DerivedService, _Overlay

__all__ = ["Service", "Inject"]


def _find_own_function(cls: type, name: str) -> Callable | None:
    """Return the unbound function stored under ``name`` in ``cls``'s mro, if any."""
    for klass in cls.__mro__:
        if name in klass.__dict__:
            member = klass.__dict__[name]
            if isinstance(member, staticmethod):
                return member.__func__
            if isinstance(member, types.FunctionType):
                return member
            if callable(member):
                return member
            return None
    return None


def collect_inject_hooks(instance: Any) -> list[Callable[[], Any]]:
    """Register delayed calls for methods marked with the method-form ``@Inject``.

    Mirrors the TS initializer: for each decorated method, run
    ``instance.ctx.inject(deps, callback)`` where the callback rebinds the
    service's context property to the freshly provided context before
    invoking the method.
    """
    hooks: list[Callable[[], Any]] = []
    tracker = getattr(instance, "_tracker", None)
    prop = tracker.property if tracker is not None else None
    seen: set[int] = set()
    for klass in type(instance).__mro__:
        if id(klass) in seen:
            continue
        seen.add(id(klass))
        for _attr, member in list(vars(klass).items()):
            meta = getattr(member, "_inject_meta", None)
            if not meta:
                continue
            fn = member.__func__ if isinstance(member, (staticmethod, classmethod)) else member
            if not callable(fn):
                continue

            def make_hook(fn: Callable = fn, meta: dict = meta) -> Callable[[], Any]:
                def register() -> Any:
                    def run(new_ctx: Any, config: Any = None) -> Any:
                        target = _Overlay(instance, {prop: new_ctx}) if prop else instance
                        return fn(target)

                    return instance.ctx.inject_plugins(meta, run)

                return register

            hooks.append(make_hook())
    return hooks


class Service:
    """Base class for services that expose a named API on ``ctx``.

    Subclasses call ``super().__init__(ctx, name)``; the service registers
    immediately and unregisters automatically when the owning fiber unloads.
    Subclasses may also be used directly as plugins (``ctx.plugin(MyService)``);
    the fiber constructs them with ``(ctx, config)`` and awaits ``_init``.
    """

    provide: ClassVar[str] = ""
    inject: ClassVar[Any] = None
    Config: ClassVar[Any] = None

    def __init__(self, ctx: Any, name: str | None = None) -> None:
        if name is None:
            name = getattr(type(self), "provide", "") or type(self).__name__
        tracker = Tracker(associate=name, property="ctx")
        self._tracker = tracker
        self.ctx = ctx
        self.name = name
        check = _find_own_function(type(self), "_check")
        self._init_hooks = collect_inject_hooks(self)
        # Register through the ctx facade: the effect must land on the
        # CALLING fiber (TS reaches the same via `ctx.reflect` returning a
        # view whose tracker rebinds `this.ctx` to the caller).
        ctx.provide(name, self, check)

    def _extend(self, props: dict[str, Any] | None = None) -> Any:
        """Derive an extended instance overlaying ``props`` (``Service.extend``).

        Callable through the traceable view, in which case the derived object
        inherits the shadow context of the access (TS ``Object.create(this)``
        with ``this`` being the shadow receiver).
        """
        return _DerivedService(self, dict(props or {}))

    def _resolve_config(self, base: Any = None, head: Any = None) -> Any:
        """Merge intercept config from ancestors with optional base and head values.

        Entries added closer to the root apply first; ``base`` is prepended
        and ``head`` appended. Uses ``Config.merge`` when the service class
        declares one, else a shallow dict update.
        """
        configs: list[Any] = []
        seen: set[int] = set()
        ctx = self.ctx
        while ctx is not None:
            intercept = getattr(ctx, "_intercept", None)
            if intercept is not None and id(intercept) not in seen:
                seen.add(id(intercept))
                if self.name in intercept:
                    configs.append(intercept[self.name])
            ctx = getattr(ctx, "_parent", None)
        configs.reverse()
        if base is not None:
            configs.insert(0, base)
        if head is not None:
            configs.append(head)
        merge = getattr(getattr(type(self), "Config", None), "merge", None)
        if callable(merge):
            return merge(configs)
        merged: dict[str, Any] = {}
        for config in configs:
            if config:
                merged.update(config)
        return merged

    def __repr__(self) -> str:
        return f"<Service {self.name!r}>"


def Inject(name: str, config: Any = None) -> Callable:
    """Declare service dependencies on classes or class methods.

    On a class it contributes to the class's ``inject`` map (merged with
    inherited declarations). On a method of a service it delays the call
    until the declared services become available, re-entering the method
    with the service's context rebound to the provided scope.
    """

    def decorate(obj: Any) -> Any:
        if isinstance(obj, type):
            merged: dict[str, Any] = {}
            for klass in reversed(obj.__mro__[1:]):
                inherited = klass.__dict__.get("inject")
                if isinstance(inherited, dict):
                    merged.update(inherited)
                elif inherited:
                    for dep in inherited:
                        merged[dep] = None
            own = obj.__dict__.get("inject")
            if isinstance(own, dict):
                merged.update(own)
            elif own:
                for dep in own:
                    merged[dep] = None
            merged[name] = config
            obj.inject = merged
            return obj
        if callable(obj):
            meta = dict(getattr(obj, "_inject_meta", None) or {})
            meta[name] = config
            obj._inject_meta = meta
            return obj
        raise TypeError("@Inject() can only be used on classes or methods")

    return decorate
