"""Event bus with the five Cordis dispatch modes.

Port notes:
- ``emit`` routes async listener failures to the error sink instead of
  letting them escape as unhandled task exceptions (audit fix #2).
- ``internal/status`` is emitted with per-callback containment (audit fix #3).
- ``parallel`` reports its true dispatch mode (audit fix #6).
- The hook map is a plain dict; Python has no inherited ``__proto__`` hazard.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from ._utils import DisposableList, FILTER


class Events:
    """The event bus service (``ctx.events``)."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._hooks: dict[Any, list[dict]] = {}

    # ------------------------------------------------------------- dispatch

    def _dispatch(self, mode: str, args: list[Any]) -> list[Callable]:
        # Only an explicit filter-bearing first argument acts as the dispatch
        # `this`; a bare context is not stripped.
        first = args[0] if args else None
        this_arg = first if (first is not None and _has_filter(first)) else None
        if this_arg is not None:
            args = args[1:]
        name = args[0]
        rest = args[1:]
        if not (isinstance(name, str) and name.startswith("internal/")):
            self.emit("internal/dispatch", mode, name, rest, this_arg)
        hooks = self._hooks.get(name) or []
        filt = getattr(this_arg, FILTER, None) if this_arg is not None else None
        selected = [h for h in hooks if h.get("global") or filt is None or filt(h["ctx"])]
        return [h["callback"] for h in selected]

    def emit(self, *args: Any) -> None:
        """Dispatch synchronously; async listener errors go to the sink."""
        cbs_and_args = self._dispatch_with_args("emit", list(args))
        for cb, rest in cbs_and_args:
            try:
                result = cb(*rest)
            except BaseException as exc:
                self._ctx.on_error(exc)
                continue
            if asyncio.iscoroutine(result):
                task = asyncio.ensure_future(result)
                task.add_done_callback(lambda t: self._ctx.on_error(t.exception()) if (not t.cancelled() and t.exception()) else None)

    def _dispatch_with_args(self, mode: str, args: list[Any]) -> list[tuple[Callable, list[Any]]]:
        first = args[0] if args else None
        this_arg = first if (first is not None and _has_filter(first)) else None
        if this_arg is not None:
            args = args[1:]
        name = args[0]
        rest = args[1:]
        if not (isinstance(name, str) and name.startswith("internal/")):
            self.emit("internal/dispatch", mode, name, rest, this_arg)
        hooks = self._hooks.get(name) or []
        filt = getattr(this_arg, FILTER, None) if this_arg is not None else None
        selected = [h for h in hooks if h.get("global") or filt is None or filt(h["ctx"])]
        return [(h["callback"], rest) for h in selected]

    async def parallel(self, *args: Any) -> None:
        """Run listeners concurrently; collect failures into one exception."""
        cbs_and_args = self._dispatch_with_args("parallel", list(args))
        results = await asyncio.gather(*(self._call(cb, rest) for cb, rest in cbs_and_args), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise ExceptionGroup("parallel dispatch failed", errors)

    @staticmethod
    async def _call(cb: Callable, rest: list[Any]) -> Any:
        result = cb(*rest)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def serial(self, *args: Any) -> Any:
        """Await listeners in order; first truthy-ish bail value wins."""
        for cb, rest in self._dispatch_with_args("serial", list(args)):
            result = cb(*rest)
            if asyncio.iscoroutine(result):
                result = await result
            if result is not None and result is not False:
                return result
        return None

    def bail(self, *args: Any) -> Any:
        """Synchronous serial."""
        for cb, rest in self._dispatch_with_args("bail", list(args)):
            result = cb(*rest)
            if result is not None and result is not False:
                return result
        return None

    def waterfall(self, *args: Any) -> Any:
        """Onion middleware: the last argument is the innermost ``next``."""
        first = args[0] if args else None
        this_arg = first if (first is not None and _has_filter(first)) else None
        stripped = args[1:] if this_arg is not None else args
        args_list = list(stripped[1:])  # drop the event name, keep listener args
        inner = args_list.pop()  # the built-in behavior is the innermost next
        cbs_and_args = self._dispatch_with_args("waterfall", list(args))
        cbs = [cb for cb, _ in cbs_and_args]

        def make_next(index: int) -> Callable[[], Any]:
            def next_cb() -> Any:
                if index < len(cbs):
                    return cbs[index](*args_list, make_next(index + 1))
                return inner(*args_list)
            return next_cb

        return make_next(0)()

    # ------------------------------------------------------------- register

    def on(self, name: Any, listener: Callable, options: bool | dict | None = None) -> Callable[[], None]:
        """Register a listener owned by the current fiber."""
        if isinstance(options, bool):
            options = {"prepend": options}
        options = options or {}
        self._ctx.fiber.assert_active()

        hooks = self._hooks.setdefault(name, [])
        hook = {"ctx": self._ctx, "callback": listener, **options}
        label = f"ctx.on({name!r})"

        def register() -> Callable[[], None]:
            hooks.append(hook) if not options.get("prepend") else hooks.insert(0, hook)

            def unregister() -> None:
                if hook in hooks:
                    hooks.remove(hook)

            return unregister

        return self._ctx.fiber.effect(register, label)

    def once(self, name: Any, listener: Callable, options: bool | dict | None = None) -> Callable[[], None]:
        hooks = self._hooks.setdefault(name, [])
        options_map = {"prepend": options} if isinstance(options, bool) else (options or {})
        hook: dict = {"ctx": self._ctx, "callback": None, **options_map}

        def wrapped(*args: Any) -> Any:
            if hook in hooks:
                hooks.remove(hook)
            return listener(*args)

        hook["callback"] = wrapped
        if options_map.get("prepend"):
            hooks.insert(0, hook)
        else:
            hooks.append(hook)
        # Tie the registration to the fiber so unloading removes it too.
        self._ctx.fiber._disposables.push(lambda: hooks.remove(hook) if hook in hooks else None)

        def unregister() -> None:
            if hook in hooks:
                hooks.remove(hook)

        return unregister

    # -------------------------------------------------- framework internals

    def _emit_plugin_created(self, fiber: Any) -> None:
        self.emit("internal/plugin", fiber)

    def _emit_plugin_disposed(self, fiber: Any) -> None:
        self.emit("internal/plugin", fiber)

    def _emit_status_contained(self, fiber: Any, old_state: Any) -> None:
        """Emit internal/status with per-callback containment (audit fix #3)."""
        for hook in list(self._hooks.get("internal/status") or []):
            try:
                result = hook["callback"](fiber, old_state)
            except BaseException as exc:
                fiber.on_error(exc)
                continue
            if asyncio.iscoroutine(result):
                task = asyncio.ensure_future(result)
                task.add_done_callback(lambda t: fiber.on_error(t.exception()) if (not t.cancelled() and t.exception()) else None)

    def _run_config_waterfall(self, fiber: Any, config: Any) -> Any:
        def default() -> Any:
            return config

        handlers = self._hooks.get("internal/config") or []
        if not handlers:
            return config

        def make_next(index: int) -> Callable[[], Any]:
            def next_cb() -> Any:
                if index < len(handlers):
                    return handlers[index]["callback"](config, make_next(index + 1))
                return config
            return next_cb

        return make_next(0)()

    def _run_update_waterfall(self, fiber: Any, resolved: Any, no_save: bool, apply: Callable[[], Any]) -> Any:
        handlers = self._hooks.get("internal/update") or []

        def make_next(index: int) -> Callable[[], Any]:
            def next_cb() -> Any:
                if index < len(handlers):
                    return handlers[index]["callback"](resolved, no_save, make_next(index + 1))
                return apply()
            return next_cb

        return make_next(0)()

    async def _dispose_fiber_via_parent(self, fiber: Any) -> None:
        """Tear a child fiber down through the disposer registered on its parent."""
        disposer = getattr(fiber, "_dispose_child", None)
        if disposer is None:
            return
        result = disposer()
        if asyncio.iscoroutine(result):
            await result


class EventsBusMixin:
    """Mixin identifying objects allowed as the dispatch ``this`` argument."""

    pass


def _has_filter(obj: Any) -> bool:
    return obj is not None and not isinstance(obj, (str, int, float, bool)) and hasattr(obj, FILTER)
