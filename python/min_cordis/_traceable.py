"""Traceable access-time binding — the Python port of Cordis ``getTraceable``.

The TS core wraps services in a ``Proxy`` so that every attribute access and
call through a context sees the *caller's* active context. This module
expresses the same three traps (get / set / apply) with wrapper objects:

- ``_TraceableView`` — the view bound at access time; ``ctx.name`` returns it.
- ``_Overlay`` — the props overlay service methods run on. Its ``ctx`` is the
  binding context extended with ``_shadow = provider ctx`` (TS
  ``createShadow``), so nested reads inside a service method resolve along
  the *provider's* dependency chain, while ``self._caller`` reports the
  service that performed the access.
- ``_DerivedService`` — the ``Service.extend`` result: an own-property
  overlay over a base chain (TS ``Object.create(this)`` + assign).

Two rules from the TS core drive the design:

1. Binding follows the reference, not dynamic scope: the binding context is
   fixed when the view is created; capturing ``svc = ctx.name`` and calling
   it later keeps the original binding.
2. Shadow stripping: when a view is created from a context that carries a
   service-shadow marker, ``_caller`` keeps the marker while the binding
   context is the marker-free context below it. That strip is what keeps
   plugins created from inside service methods attached to the real
   consumer scope (upstream shadow.spec "strips service shadow").
"""

from __future__ import annotations

import types
from typing import Any, Callable

__all__ = ["Tracker", "get_traceable", "TRACKER", "CALLER", "ORIGINAL", "SHADOW"]

# Attribute keys used by the tracing machinery. Underscore names never enter
# the service-resolution path of ``Context.__getattr__``.
TRACKER = "_tracker"
CALLER = "_caller"
ORIGINAL = "_original"
SHADOW = "_shadow"

_SPECIAL = (CALLER, ORIGINAL)


class Tracker:
    """Tracing metadata attached to a service object.

    ``associate`` — dotted sub-service namespace (``ctx.foo.bar`` resolves
    the ``foo.bar`` service when present).
    ``property`` — the context attribute rebound inside method bodies
    (services use ``"ctx"``).
    ``no_shadow`` — identity-aware services (e.g. loggers) run methods on
    the view itself; no shadow context is created.
    """

    __slots__ = ("associate", "property", "no_shadow")

    def __init__(self, associate: str | None = None, property: str = "ctx", no_shadow: bool = False) -> None:
        self.associate = associate
        self.property = property
        self.no_shadow = no_shadow

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Tracker(associate={self.associate!r}, property={self.property!r}, no_shadow={self.no_shadow!r})"


def _unwrap(value: Any) -> Any:
    """Return the raw service instance behind any wrapper chain."""
    while isinstance(value, (_TraceableView, _Overlay, _DerivedService)):
        value = value._root_target()
    return value


def get_traceable(ctx: Any, value: Any) -> Any:
    """Attach ``ctx``'s tracing binding to ``value`` when it has a tracker.

    Non-objects, tracker-less values, and existing wrapper chains (views,
    shadow overlays, derived services) pass through unchanged: TS reaches
    the same outcome through proxy transparency — a wrapper chain keeps
    the binding it carries.
    """
    if value is None or isinstance(value, (bool, int, float, complex, str, bytes, bytearray)):
        return value
    if isinstance(value, (_TraceableView, _Overlay, _DerivedService)):
        return value
    tracker = getattr(value, TRACKER, None)
    if tracker is None:
        return value
    return _TraceableView(ctx, _unwrap(value), tracker)


def _binding_ctx(ctx: Any) -> tuple[Any, Any]:
    """Split ``ctx`` into (caller, binding): strip one shadow layer."""
    marker = getattr(ctx, SHADOW, None)
    if marker is None:
        return ctx, ctx
    return marker, getattr(ctx, "_parent", ctx)


def _find_class_member(target: Any, name: str) -> tuple[str, Any]:
    """Locate ``name`` as ('instance', value) or ('class', member) on the raw target."""
    if name in target.__dict__:
        return "instance", target.__dict__[name]
    for klass in type(target).__mro__:
        if name in klass.__dict__:
            return "class", klass.__dict__[name]
    return "missing", None


def _class_function(target: Any, name: str) -> Callable | None:
    """Return the unbound function stored as ``name`` in the class chain, if any."""
    for klass in type(target).__mro__:
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


class _DunderMixin:
    """Delegate implicit protocol lookups to the wrapped target.

    Python resolves dunders on the wrapper *type* for implicit invocations,
    so ``repr``/``iter``/indexing must be defined here explicitly. Attribute
    reads still flow through ``__getattr__``.
    """

    def _delegate_target(self) -> Any:
        raise NotImplementedError

    def __repr__(self) -> str:
        return repr(self._delegate_target())

    def __str__(self) -> str:
        return str(self._delegate_target())

    def __eq__(self, other: Any) -> bool:
        if self is other:
            return True
        target = self._delegate_target()
        return target is other or target == other

    def __hash__(self) -> int:
        return object.__hash__(self._delegate_target())

    def __bool__(self) -> bool:
        return bool(self._delegate_target())

    def __iter__(self):
        return iter(self._delegate_target())

    def __len__(self) -> int:
        return len(self._delegate_target())

    def __contains__(self, item: Any) -> bool:
        return item in self._delegate_target()

    def __getitem__(self, key: Any) -> Any:
        return self._delegate_target()[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        self._delegate_target()[key] = value

    def __delitem__(self, key: Any) -> None:
        del self._delegate_target()[key]

    def __enter__(self):
        return self._delegate_target().__enter__()

    def __exit__(self, *exc: Any):
        return self._delegate_target().__exit__(*exc)


def _function_wrapper(fn: Callable, receiver: Any, binding: Any, wrap_result: bool = True) -> Callable:
    """Bind ``fn`` to ``receiver`` and trace its result (TS createShadowMethod)."""

    def method(*args: Any, **kwargs: Any) -> Any:
        result = fn(receiver, *args, **kwargs)
        if wrap_result:
            return get_traceable(binding, result)
        return result

    try:
        method.__name__ = getattr(fn, "__name__", "method")
    except Exception:  # pragma: no cover - exotic callables
        pass
    return method


class _TraceableView(_DunderMixin):
    """Access-time-bound view of a tracked service (TS ``createTraceable``)."""

    def __init__(self, ctx: Any, target: Any, tracker: Tracker) -> None:
        caller, binding = _binding_ctx(ctx)
        object.__setattr__(self, "_caller", caller)
        object.__setattr__(self, "_binding", binding)
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_tracker", tracker)

    # ------------------------------------------------------------ protocol

    @property
    def __class__(self) -> type:
        return type(self._target)

    def _delegate_target(self) -> Any:
        return self._target

    def _root_target(self) -> Any:
        return self._target

    def _shadow_self(self) -> Any:
        """The object method bodies run on: binding ctx extended with the shadow marker."""
        if self._tracker.no_shadow:
            return self
        origin = getattr(self._target, self._tracker.property, None)
        if origin is None:
            return self
        shadow_ctx = self._binding.extend(**{SHADOW: origin})
        return _Overlay(self, {self._tracker.property: shadow_ctx, CALLER: self._caller, ORIGINAL: self._target})

    def _receiver(self) -> Any:
        return self if self._tracker.no_shadow else self._shadow_self()

    # ------------------------------------------------------------ members

    def __getattr__(self, name: str) -> Any:
        tracker = self._tracker
        if name == tracker.property:
            return self._binding
        if name in _SPECIAL:
            if name == CALLER:
                return self._caller
            return self._target
        if name.startswith("_"):
            return getattr(self._target, name)

        binding = self._binding
        dotted = f"{tracker.associate}.{name}" if tracker.associate else None
        if dotted is not None and dotted in binding.root.reflect.props:
            return binding._resolve_dotted(dotted, self)

        kind, member = _find_class_member(self._target, name)
        if kind == "instance":
            if isinstance(member, types.FunctionType) and not tracker.no_shadow:
                return _function_wrapper(member, self._shadow_self(), binding)
            return member
        if kind == "class":
            if isinstance(member, property):
                return get_traceable(binding, member.fget(self._receiver()))
            if isinstance(member, (staticmethod, classmethod)):
                return member.__get__(None, type(self._target))
            if isinstance(member, types.FunctionType):
                if tracker.no_shadow:
                    return _function_wrapper(member, self, binding)
                return _function_wrapper(member, self._shadow_self(), binding)
            if hasattr(member, "__get__"):
                return member.__get__(self._receiver(), type(self._target))
            return member
        raise AttributeError(f"{type(self._target).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_") and name in ("_caller", "_binding", "_target", "_tracker"):
            object.__setattr__(self, name, value)
            return
        tracker = self._tracker
        if name == tracker.property or name in _SPECIAL:
            # TS rejects these writes through the proxy set trap.
            raise AttributeError(f"cannot set {name!r} on a traceable view")
        binding = self._binding
        dotted = f"{tracker.associate}.{name}" if tracker.associate else None
        if dotted is not None and dotted in binding.root.reflect.props:
            defn = binding.root.reflect.props[dotted]
            if isinstance(defn, dict) and defn.get("type") == "accessor":
                binding._resolve_dotted_set(dotted, value, self)
            else:
                binding.root.reflect.set(dotted, value, ctx=binding)
            return
        self._set_on_target(name, value)

    def __delattr__(self, name: str) -> None:
        if name in (self._tracker.property, *_SPECIAL):
            raise AttributeError(f"cannot delete {name!r} on a traceable view")
        self._target.__dict__.pop(name, None)

    def _set_on_target(self, name: str, value: Any) -> None:
        # Setters observe the shadow receiver (TS Reflect.set(target, ..., shadow)).
        target = self._target
        for klass in type(target).__mro__:
            if name in klass.__dict__:
                member = klass.__dict__[name]
                if isinstance(member, property):
                    if member.fset is None:
                        raise AttributeError(f"property {name!r} has no setter")
                    member.fset(self._shadow_self(), value)
                    return
                if hasattr(member, "__set__"):
                    member.__set__(self._shadow_self(), value)
                    return
                break
        object.__setattr__(target, name, value)

    # ------------------------------------------------------------ calls

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _dispatch_call(self._target, self._binding, self._receiver(), args, kwargs)


def _view_binding(base: Any) -> Any:
    """Walk the wrapper chain to the view and return its binding context."""
    seen = 0
    while base is not None and isinstance(base, (_Overlay, _DerivedService)) and seen < 64:
        base = base._base
        seen += 1
    if isinstance(base, _TraceableView):
        return base.__dict__.get("_binding")
    return None


class _Missing:
    """Sentinel distinguishing "no overlay prop" from a ``None`` prop value."""


_MISSING = _Missing()


def _overlay_prop(base: Any, name: str) -> Any:
    """Return the nearest props-overlay value for ``name`` in the base chain."""
    seen = 0
    while base is not None and isinstance(base, _Overlay) and seen < 64:
        if name in base._props:
            return base._props[name]
        base = base._base
        seen += 1
    return _MISSING


class _Overlay(_DunderMixin):
    """Props overlay over a wrapper: the shadow-self and ctx-rebinding base.

    Attribute reads check the fixed overlay props first, then resolve class
    members with *this overlay* as the bound instance, then fall through the
    base chain (TS ``withProps`` over the proxy).
    """

    def __init__(self, base: Any, props: dict[str, Any]) -> None:
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_props", props)

    @property
    def __class__(self) -> type:
        return type(self._root_target())

    def _delegate_target(self) -> Any:
        return self._root_target()

    def _root_target(self) -> Any:
        return _unwrap(self._base)

    def _tracker_of(self) -> Tracker | None:
        return getattr(self._root_target(), TRACKER, None)

    def _binding_of(self) -> Any:
        binding = _view_binding(self._base)
        if binding is not None:
            return binding
        # Overlay directly over a raw instance (method-level @Inject).
        return getattr(self._root_target(), "ctx", None)

    def __getattr__(self, name: str) -> Any:
        props = self._props
        if name in props:
            return props[name]
        tracker = self._tracker_of()
        target = self._root_target()
        binding = self._binding_of()
        tracker = self._tracker_of()
        dotted = f"{tracker.associate}.{name}" if (tracker is not None and tracker.associate) else None
        if dotted is not None and binding is not None and dotted in binding.root.reflect.props:
            return binding._resolve_dotted(dotted, self)
        kind, member = _find_class_member(target, name)
        if kind == "instance":
            if isinstance(member, types.FunctionType) and tracker is not None and not tracker.no_shadow:
                return _function_wrapper(member, self, binding)
            return member
        if kind == "class":
            if isinstance(member, property):
                return get_traceable(binding, member.fget(self))
            if isinstance(member, (staticmethod, classmethod)):
                return member.__get__(None, type(target))
            if isinstance(member, types.FunctionType):
                return _function_wrapper(member, self, binding)
            if hasattr(member, "__get__"):
                return member.__get__(self, type(target))
            return member
        return getattr(self._base, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_") and name in ("_base", "_props"):
            object.__setattr__(self, name, value)
            return
        if name in self._props:
            raise AttributeError(f"cannot set overlay prop {name!r}")
        tracker = self._tracker_of()
        if tracker is not None and (name == tracker.property or name in _SPECIAL):
            raise AttributeError(f"cannot set {name!r} on a shadow view")
        target = self._root_target()
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

    def __delattr__(self, name: str) -> None:
        if name in self._props:
            raise AttributeError(f"cannot delete overlay prop {name!r}")
        self._root_target().__dict__.pop(name, None)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _dispatch_call(self._root_target(), self._binding_of(), self, args, kwargs)


class _DerivedService(_DunderMixin):
    """``Service.extend`` result: mutable own props over a base chain.

    Mirrors TS ``Object.create(base)`` + ``Object.assign(props)``: own props
    shadow the base; missing members bind to this derived object; the base
    chain (typically a shadow-self over a view) supplies inherited state
    such as ``ctx``.
    """

    def __init__(self, base: Any, own: dict[str, Any]) -> None:
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_own", own)

    @property
    def __class__(self) -> type:
        return type(self._root_target())

    def _delegate_target(self) -> Any:
        return self._root_target()

    def _root_target(self) -> Any:
        return _unwrap(self._base)

    def _binding_of(self) -> Any:
        binding = _view_binding(self._base)
        if binding is not None:
            return binding
        return getattr(self._root_target(), "ctx", None)

    def __getattr__(self, name: str) -> Any:
        own = self._own
        if name in own:
            member = own[name]
            if isinstance(member, types.FunctionType):
                return _function_wrapper(member, self, self._binding_of())
            return member
        base = self._base
        # TS proto order: derived own props, then the shadow overlay's props
        # (ctx/caller/original), then class members, then the raw instance.
        overlay_value = _overlay_prop(base, name)
        if overlay_value is not _MISSING:
            return overlay_value
        target = self._root_target()
        binding = self._binding_of()
        tracker = getattr(target, TRACKER, None)
        dotted = f"{tracker.associate}.{name}" if (tracker is not None and tracker.associate) else None
        if dotted is not None and binding is not None and dotted in binding.root.reflect.props:
            return binding._resolve_dotted(dotted, self)
        kind, member = _find_class_member(target, name)
        if kind == "instance":
            return member
        if kind == "class":
            if isinstance(member, property):
                return get_traceable(binding, member.fget(self))
            if isinstance(member, (staticmethod, classmethod)):
                return member.__get__(None, type(target))
            if isinstance(member, types.FunctionType):
                return _function_wrapper(member, self, binding)
            if hasattr(member, "__get__"):
                return member.__get__(self, type(target))
            return member
        return getattr(self._base, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_") and name in ("_base", "_own"):
            object.__setattr__(self, name, value)
            return
        # Own props land on the derived object (TS own-property semantics).
        object.__getattribute__(self, "_own")[name] = value

    def __delattr__(self, name: str) -> None:
        object.__getattribute__(self, "_own").pop(name, None)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _dispatch_call(self._root_target(), self._binding_of(), self, args, kwargs)


def _dispatch_call(target: Any, binding: Any, call_receiver: Any, args: tuple, kwargs: dict) -> Any:
    """TS apply trap: prefer the service invoke body, else the class ``__call__``.

    Both run on ``call_receiver`` — the view itself for ``noShadow``
    services, else a freshly built shadow-self — mirroring the TS apply
    trap receiver. Results are traced against the binding context.
    """
    invoke = _class_function(target, "_invoke")
    if invoke is not None:
        return get_traceable(binding, invoke(call_receiver, *args, **kwargs))
    plain = _class_function(target, "__call__")
    if plain is not None:
        return get_traceable(binding, plain(call_receiver, *args, **kwargs))
    raise TypeError(f"{type(target).__name__!r} object is not callable")
