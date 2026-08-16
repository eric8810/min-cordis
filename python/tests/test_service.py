"""Service base-class behaviors, ported from tests/service.spec.ts and
tests/decorator.spec.ts.

The "compare snapshot" test from the TS suite is deferred: it depends on
``registry.delete`` synchronously draining fibers, which is the pending R3
item (fire-and-forget disposal).
"""

from __future__ import annotations

import asyncio

import pytest

from min_cordis import Context, Inject, Service, Tracker


@pytest.fixture
def errors():
    collected: list[BaseException] = []
    return collected


@pytest.fixture
def root(errors):
    return Context(on_error=errors.append)


class _Counter:
    """Effect-registering counter (TS tests/utils ``Counter``).

    ``increase()`` registers an effect through the traceable view's
    ``self.ctx`` — the discriminating contract of the "traceable effect"
    tests: the effect must land on the fiber of the context the service was
    ACCESSED from, so disposing an unrelated consumer fiber must leave the
    increment in place (and the undo must not run).
    """

    def __init__(self, ctx):
        self._tracker = Tracker(associate="counter", property="ctx")
        self.ctx = ctx
        self.value = 0

    def increase(self):
        def setup():
            self.value += 1

            def undo():
                self.value -= 1

            return undo

        return self.ctx.effect(setup)


async def test_pending_inject_blocked_by_service_init(root):
    started: list = []

    class Foo(Service):
        def __init__(self, ctx, config=None):
            super().__init__(ctx, "foo")

        async def _init(self):
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self.ctx.on("custom-event", lambda: fut.done() or fut.set_result(None))
            await fut
            started.append("init")

    seen: list = []
    root.inject_plugins(["foo"], lambda ctx, cfg: seen.append("consumer"))

    # inject stays blocked by the pending `_init`
    root.plugin(Foo)
    await asyncio.sleep(0.05)
    assert seen == []

    root.emit("custom-event")
    await asyncio.sleep(0.05)
    assert seen == ["consumer"]
    assert started == ["init"]


async def test_traceable_effect_with_inject(root, errors):
    class Foo(Service):
        inject = ["counter"]

        def __init__(self, ctx, config=None):
            super().__init__(ctx, "foo")

        @property
        def value(self):
            return self.ctx.counter.value

        def increase(self):
            return self.ctx.counter.increase()

    root.provide("counter")
    root.set("counter", _Counter(root))

    await root.plugin(Foo)
    root.get("foo").increase()
    assert root.get("foo").value == 1
    assert errors == []

    seen: list = []

    def consumer(ctx, config):
        root.get("foo").increase()
        seen.append(ctx.foo.value)

    fiber = await root.inject_plugins(["foo"], consumer)
    assert seen == [2]
    assert errors == []

    await fiber.dispose()
    root.get("foo").increase()
    assert root.get("foo").value == 3
    assert errors == []


async def test_traceable_effect_without_inject(root, errors):
    class Foo(Service):
        def __init__(self, ctx, config=None):
            super().__init__(ctx, "foo")

        @property
        def value(self):
            return self.ctx.counter.value

        def increase(self):
            return self.ctx.counter.increase()

    root.provide("counter")
    root.set("counter", _Counter(root))

    await root.plugin(Foo)
    root.get("foo").increase()
    assert root.get("foo").value == 1

    def consumer(ctx, config):
        root.get("foo").increase()
        seen.append(root.get("foo").value)

    seen: list = []
    fiber = await root.inject_plugins(["foo"], consumer)
    assert seen == [2]

    await fiber.dispose()
    root.get("foo").increase()
    assert root.get("foo").value == 3
    assert errors == []


async def test_multiple_injects(root):
    calls = {"foo": 0, "bar": 0, "qux": 0}

    class Foo(Service):
        inject = ["qux"]

        def __init__(self, ctx, config=None):
            super().__init__(ctx, "foo")

        def _init(self):
            calls["foo"] += 1

    class Bar(Service):
        inject = ["foo", "qux"]

        def __init__(self, ctx, config=None):
            super().__init__(ctx, "bar")

        def _init(self):
            calls["bar"] += 1

    class Qux(Service):
        def __init__(self, ctx, config=None):
            super().__init__(ctx, "qux")

        def _init(self):
            calls["qux"] += 1

    await root.plugin(Foo)
    await root.plugin(Bar)
    await root.plugin(Qux)
    await asyncio.sleep(0.05)
    assert calls == {"foo": 1, "bar": 1, "qux": 1}


async def test_compare_snapshot(root):
    """service.spec #4: registry.delete leaves no residual event hooks."""
    class Test(Service):
        def __init__(self, ctx, config=None):
            super().__init__(ctx, "test")
            ctx.inject_plugins(["test"], lambda c, cfg: None)

    def snapshot():
        return {name: len(hooks) for name, hooks in root.events._hooks.items() if hooks}

    before = snapshot()
    await root.plugin(Test)
    after = snapshot()
    root.registry.delete(Test)
    # registry.delete is fire-and-forget (TS parity); drain the disposal
    await asyncio.sleep(0.05)
    assert snapshot() == before
    await root.plugin(Test)
    assert snapshot() == after


class TestInjectDecorator:
    async def test_on_class_method(self, root):
        calls: list = []
        disposed: list = []

        class Foo(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")

        class Bar(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "bar")

            @Inject("foo")
            def method(self):
                calls.append(1)
                return lambda: disposed.append(1)

        await root.plugin(Bar)
        assert calls == []
        assert disposed == []
        fiber = await root.plugin(Foo)
        # the @Inject consumer reloads as a separate task chain (TS drains
        # it in microtasks; Python needs a loop tick)
        await asyncio.sleep(0.05)
        assert calls == [1]
        assert disposed == []
        await fiber.dispose()
        assert calls == [1]
        assert disposed == [1]

    def test_on_class_merges_with_inheritance(self):
        @Inject("db", {"pool": 1})
        class Plugin(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "plugin")

        assert Plugin.inject == {"db": {"pool": 1}}

        @Inject("cache")
        class Child(Plugin):
            pass

        assert Child.inject == {"db": {"pool": 1}, "cache": None}
        assert Plugin.inject == {"db": {"pool": 1}}  # parent untouched

    async def test_on_class_blocks_until_ready(self, root):
        started: list = []

        @Inject("svc")
        class Consumer(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "consumer")

            def _init(self):
                started.append(1)

        await root.plugin(Consumer)
        assert started == []
        await root.plugin(lambda c, cfg: c.provide("svc", 1))
        await asyncio.sleep(0.05)
        assert started == [1]


class TestResolveConfig:
    async def test_merges_intercept_chain_root_first(self, root):
        class Foo(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")

        ctx1 = root.intercept("foo", {"a": 1})
        ctx2 = ctx1.intercept("foo", {"b": 2})

        await ctx2.plugin(Foo)
        svc = ctx2.get("foo")
        assert svc._resolve_config() == {"a": 1, "b": 2}
        assert svc._resolve_config({"z": 0}) == {"z": 0, "a": 1, "b": 2}
        assert svc._resolve_config(None, {"y": 9}) == {"a": 1, "b": 2, "y": 9}

    async def test_custom_merge_function(self, root):
        # TS uses one static `Config` for both roles: plugin schema validator
        # and intercept-merge policy (`.merge`). A function with a `merge`
        # attribute expresses the same pair here.
        def passthrough(config):
            return config

        def merge(configs):
            return {"joined": "-".join("".join(c.keys()) for c in configs if c)}

        passthrough.merge = merge

        class Foo(Service):
            Config = passthrough

            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")

        ctx1 = root.intercept("foo", {"a": 1})
        await ctx1.plugin(Foo)
        svc = ctx1.get("foo")
        assert svc._resolve_config() == {"joined": "a"}

    async def test_inject_config_becomes_intercept_entry(self, root):
        class Foo(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")

            def _invoke(self, init=None):
                result = {}
                for entry in self.ctx._intercept_entries("foo"):
                    result.update(entry)
                return result

        await root.plugin(Foo)
        ctx1 = root.intercept("foo", {"a": 1})
        seen: list = []

        def consumer(ctx, config):
            seen.append(ctx.foo())

        await ctx1.inject_plugins({"foo": {"b": 2}}, consumer)
        assert seen == [{"a": 1, "b": 2}]
