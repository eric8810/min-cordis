"""Events and isolation parity, ported from tests/events.spec.ts (filter
selection, once, parallel aggregation, waterfall veto) and
tests/isolate.spec.ts (scope matrix, shared label, isolated event dispatch).

Filtering model (TS): a listener may be registered on a context carrying a
``FILTER`` attribute; dispatching with a non-string first argument treats it
as ``this`` and consults its ``FILTER`` hook per listener context. The TS
``Session[Context.filter]`` delegation becomes the same dance over the
string-keyed FILTER attribute.
"""

from __future__ import annotations

import asyncio

import pytest

from min_cordis import Context, Service
from min_cordis._utils import FILTER


@pytest.fixture
def errors():
    collected: list[BaseException] = []
    return collected


@pytest.fixture
def root(errors):
    return Context(on_error=errors.append)


class Session:
    """Dispatch ``this`` (TS utils ``Session``): delegates to the listening
    context's ``filter`` attribute when present."""

    def __init__(self, flag: bool) -> None:
        self.flag = flag

    def _filter(self, hook_ctx) -> bool:
        # TS: `context.filter ? context.filter(this) : true` — a context
        # without the attribute delegates nothing. The dotted attribute name
        # reaches Context.__getattr__ when absent, so catch both.
        try:
            delegate = getattr(hook_ctx, FILTER)
        except (AttributeError, RuntimeError):
            return True
        return delegate(self)


setattr(Session, FILTER, Session._filter)


class TestFilterSelection:
    async def test_filter_selects_matching_listeners(self, root):
        hits: list = []

        def listener_a():
            hits.append("a")

        def listener_b():
            hits.append("b")

        def flag_filter(session: Session) -> bool:
            return session.flag

        true_ctx = root.extend(**{FILTER: flag_filter})
        true_ctx.on("evt/filter", listener_a)
        root.on("evt/filter", listener_b)

        root.emit(Session(True), "evt/filter")
        assert hits == ["a", "b"]  # matching filtered listener + unfiltered

        hits.clear()
        root.emit(Session(False), "evt/filter")
        assert hits == ["b"]  # filtered listener excluded, plain one runs

    async def test_all_modes_apply_filter(self, root):
        hits: list = []

        def flag_filter(session: Session) -> bool:
            return session.flag

        filtered = root.extend(**{FILTER: flag_filter})
        filtered.on("evt/all", lambda: hits.append("filtered"))
        root.on("evt/all", lambda: hits.append("plain"), {"global": True})

        root.emit(Session(False), "evt/all")
        assert hits == ["plain"]

        await root.parallel(Session(True), "evt/all")
        assert hits == ["plain", "filtered", "plain"]

    async def test_once_dispose_before_fire(self, root):
        calls: list = []
        off = root.once("evt/once2", lambda: calls.append(1))
        off()  # the unregister is synchronous
        root.emit("evt/once2")
        root.emit("evt/once2")
        assert calls == []

    async def test_parallel_collects_all_members(self, root):
        async def boom_sync():
            raise RuntimeError("test")

        async def boom_async():
            await asyncio.sleep(0)
            raise RuntimeError("async")

        root.on("evt/par2", boom_sync)
        root.on("evt/par2", boom_async)
        with pytest.raises(BaseException) as excinfo:
            await root.parallel("evt/par2")
        group = excinfo.value
        members = {str(e) for e in getattr(group, "exceptions", ())}
        assert members == {"test", "async"}

    async def test_waterfall_veto_suppresses_later_listeners(self, root):
        order: list = []

        def a(req, nxt):
            order.append("a")
            return nxt()

        def vetoer(req, nxt):
            return "vetoed"

        def c(req, nxt):
            order.append("c")  # must never run
            return nxt()

        root.on("evt/wf2", a)
        root.on("evt/wf2", vetoer)
        root.on("evt/wf2", c)
        assert root.waterfall("evt/wf2", {}, lambda req: "done") == "vetoed"
        assert order == ["a"]

    async def test_sync_listener_error_propagates_in_emit(self, root, errors):
        def bad():
            raise RuntimeError("sync boom")

        root.on("evt/sync-boom", bad)
        with pytest.raises(RuntimeError, match="sync boom"):
            root.emit("evt/sync-boom")
        # a synchronous failure is the caller's, not the sink's
        assert errors == []


class TestIsolation:
    async def test_isolated_context_matrix(self, root):
        callback: list = []
        disposed: list = []

        def plugin_body(c, cfg):
            callback.append(1)

            def dispose():
                disposed.append(1)

            return dispose

        plugin = {"inject": ["foo"], "apply": plugin_body}

        await root.plugin(plugin)
        ctx1 = root.isolate("foo")
        await ctx1.plugin(plugin)
        ctx2 = root.isolate("foo")
        await ctx2.plugin(plugin)

        dispose0 = root.provide("foo", {"bar": 100})
        assert root.get("foo") == {"bar": 100}
        assert ctx1.get("foo") is None
        assert ctx2.get("foo") is None
        await asyncio.sleep(0.05)
        assert len(callback) == 1
        assert len(disposed) == 0

        dispose1 = ctx1.provide("foo", {"bar": 200})
        assert root.get("foo") == {"bar": 100}
        assert ctx1.get("foo") == {"bar": 200}
        assert ctx2.get("foo") is None
        await asyncio.sleep(0.05)
        assert len(callback) == 2
        assert len(disposed) == 0

        await dispose0()
        assert root.get("foo") is None
        assert ctx1.get("foo") == {"bar": 200}
        assert ctx2.get("foo") is None
        await asyncio.sleep(0.05)
        assert len(callback) == 2
        assert len(disposed) == 1

        dispose2 = ctx2.provide("foo", {"bar": 300})
        assert root.get("foo") is None
        assert ctx1.get("foo") == {"bar": 200}
        assert ctx2.get("foo") == {"bar": 300}
        await asyncio.sleep(0.05)
        assert len(callback) == 3
        assert len(disposed) == 1
        await dispose1()
        await dispose2()

    async def test_shared_label_joins_two_scopes(self, root):
        callback: list = []
        disposed: list = []

        def plugin_body(c, cfg):
            callback.append(1)

            def dispose():
                disposed.append(1)

            return dispose

        plugin = {"inject": ["foo"], "apply": plugin_body}

        label = object()
        await root.plugin(plugin)
        ctx1 = root.isolate("foo", label)
        await ctx1.plugin(plugin)
        ctx2 = root.isolate("foo", label)
        await ctx2.plugin(plugin)
        await asyncio.sleep(0.05)
        assert len(callback) == 0

        dispose0 = root.provide("foo", {"bar": 100})
        assert root.get("foo") == {"bar": 100}
        assert ctx1.get("foo") is None
        assert ctx2.get("foo") is None
        await asyncio.sleep(0.05)
        assert len(callback) == 1
        assert len(disposed) == 0

        dispose12 = ctx1.provide("foo", {"bar": 200})
        assert root.get("foo") == {"bar": 100}
        assert ctx1.get("foo") == {"bar": 200}
        assert ctx2.get("foo") == {"bar": 200}  # shared scope
        await asyncio.sleep(0.05)
        assert len(callback) == 3
        assert len(disposed) == 0

        await dispose12()
        assert root.get("foo") == {"bar": 100}
        assert ctx1.get("foo") is None
        assert ctx2.get("foo") is None
        await asyncio.sleep(0.05)
        assert len(callback) == 3
        assert len(disposed) == 2
        await dispose0()

    async def test_isolated_event_reaches_only_inner_listeners(self, root):
        class Foo(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")
                # dispatch with the service itself as `this`: listeners are
                # filtered to contexts sharing this service's isolate label
                self.ctx.emit(self, "evt/scoped2")

        ctx = root.isolate("foo")
        outer: list = []
        inner: list = []
        root.on("evt/scoped2", lambda: outer.append(1))
        ctx.on("evt/scoped2", lambda: inner.append(1))
        await ctx.plugin(Foo)

        assert outer == []
        assert inner == [1]


class TestReflectBoundaries:
    async def test_injected_but_inactive_read_raises(self, root):
        class Foo(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")

        provider = await root.plugin(Foo)
        consumer = root.inject_plugins(["foo"], lambda c, cfg: None)
        await consumer
        await provider.dispose()
        await asyncio.sleep(0.05)

        # the consumer fiber reloads to PENDING; its context still declares
        # the inject, so the read must fail loudly (not return None)
        with pytest.raises(RuntimeError, match="cannot get required service"):
            _ = consumer._fiber.ctx.foo
        await consumer.dispose()

    async def test_undeclared_read_still_loud_after_reload(self, root):
        seen: list = []
        provider = root.plugin(lambda c, cfg: c.provide("svc", 1))
        consumer = root.inject_plugins({"svc": None}, lambda c, cfg: seen.append(1))
        await provider
        await consumer
        with pytest.raises(RuntimeError, match="without inject"):
            _ = consumer._fiber.ctx.other
        await consumer.dispose()
        await provider.dispose()
