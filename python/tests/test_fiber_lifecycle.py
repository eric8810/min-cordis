"""Fiber lifecycle parity: inertia states, error containment, update races.

Ported from tests/fiber.spec.ts and tests/dispose.spec.ts. The TS fake-timer
windows become asyncio Events, so state transitions are observed at
deterministic points instead of wall-clock offsets: a plugin body blocks on
a controlled event, and each assertion happens after the matching release.

Documented deviation (containment design, see python/README.md): TS asserts
`await dispose()` REJECTS for failing async setups; the Python port routes
those through the injected `on_error` sink and resolves. The tests below pin
the Python behavior.
"""

from __future__ import annotations

import asyncio

import pytest

from min_cordis import Context, FiberState, Service


@pytest.fixture
def errors():
    collected: list[BaseException] = []
    return collected


@pytest.fixture
def root(errors):
    return Context(on_error=errors.append)


class TestInertia:
    async def test_lock1_unload_then_reload(self, root):
        body_started = asyncio.Event()
        release_body = asyncio.Event()
        undo_started = asyncio.Event()
        release_undo = asyncio.Event()

        async def body(c, cfg):
            body_started.set()
            await release_body.wait()

            async def undo():
                undo_started.set()
                await release_undo.wait()

            return undo

        dispose = root.provide("foo", 1)
        view = root.inject_plugins(["foo"], body)
        await asyncio.wait_for(body_started.wait(), 1)
        assert view._fiber.state == FiberState.LOADING

        # TS fires the provide disposer without awaiting it; the epoch flip
        # happens in its synchronous part, the gather settles later.
        dispose_task = asyncio.ensure_future(dispose())
        await asyncio.sleep(0)
        assert view._fiber.state == FiberState.LOADING

        release_body.set()
        await asyncio.wait_for(undo_started.wait(), 1)
        assert view._fiber.state == FiberState.UNLOADING

        release_undo.set()
        await view
        assert view._fiber.state == FiberState.PENDING

        dispose2 = root.provide("foo", 1)
        await view
        assert view._fiber.state == FiberState.ACTIVE
        await dispose_task
        await dispose2()

    async def test_lock2_reprovide_same_fiber_skips_unload(self, root):
        body_started = asyncio.Event()
        release_body = asyncio.Event()
        undo_started = asyncio.Event()

        async def body(c, cfg):
            body_started.set()
            await release_body.wait()

            async def undo():
                undo_started.set()

            return undo

        dispose = root.provide("foo", 1)
        view = root.inject_plugins(["foo"], body)
        await asyncio.wait_for(body_started.wait(), 1)
        assert view._fiber.state == FiberState.LOADING

        dispose_task = asyncio.ensure_future(dispose())
        await asyncio.sleep(0)
        assert view._fiber.state == FiberState.LOADING

        # re-provide from the SAME fiber mid-load: the epoch string returns
        # to its original value, so the in-flight load just completes
        dispose2 = root.provide("foo", 2)
        release_body.set()
        await view
        assert view._fiber.state == FiberState.ACTIVE
        assert not undo_started.is_set()  # the unload never ran
        await dispose_task
        await dispose2()

    async def test_lock3_provider_dispose_leaves_consumer_pending(self, root):
        class Foo(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")

        provider = await root.plugin(Foo)
        release = asyncio.Event()

        async def body(c, cfg):
            await release.wait()

        view = root.inject_plugins(["foo"], body)
        await asyncio.sleep(0.05)
        assert view._fiber.state == FiberState.LOADING
        release.set()
        await view
        assert view._fiber.state == FiberState.ACTIVE

        await provider.dispose()
        await view
        assert view._fiber.state == FiberState.PENDING


class TestErrors:
    async def test_plugin_error_marks_failed_and_contains(self, root, errors):
        calls: list = []

        def apply(c, cfg):
            c.on("evt/boom", lambda: calls.append(1))
            if not (cfg and cfg.get("foo")):
                raise RuntimeError("plugin error")

        fiber1 = root.plugin(apply)
        fiber2 = root.plugin(apply, {"foo": True})
        await asyncio.sleep(0.05)
        assert fiber1._fiber.state == FiberState.FAILED
        assert fiber2._fiber.state == FiberState.ACTIVE
        assert len([e for e in errors if "plugin error" in str(e)]) == 1

        # the failed fiber's listener did not fire
        root.emit("evt/boom")
        assert calls == [1]

    async def test_dispose_error_is_contained(self, root, errors):
        order: list = []

        def plugin(c, cfg):
            def dispose():
                order.append(1)
                raise RuntimeError("dispose boom")

            return dispose

        view = await root.plugin(plugin)
        assert order == []
        await view.dispose()  # resolves; the error went to the sink
        assert order == [1]
        assert any("dispose boom" in str(e) for e in errors)

    async def test_await_view_raises_on_failed_plugin(self, root):
        def bad(c, cfg):
            raise RuntimeError("kaboom")

        view = root.plugin(bad)
        with pytest.raises(RuntimeError, match="kaboom"):
            await view
        assert view._fiber.state == FiberState.FAILED


class TestUpdateRaces:
    async def test_update_config_on_wrapped_fiber(self, root):
        seen: list = []

        def plugin(c, cfg):
            seen.append(cfg)

        view = root.plugin(plugin, {"msg": "hello"})
        await view
        assert seen == [{"msg": "hello"}]

        await view.update({"msg": "world"})
        await view
        assert seen == [{"msg": "hello"}, {"msg": "world"}]

        await view.update({"msg": "!!!"})
        await view
        assert seen == [{"msg": "hello"}, {"msg": "world"}, {"msg": "!!!"}]

    async def test_update_while_injected_service_reloads(self, root):
        applied: list = []

        class Provider(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "provider")
                self.value = config["value"]

        def consumer(c, cfg):
            applied.append([c.provider.value, cfg["mode"]])

        provider = root.plugin(Provider, {"value": 1})
        consumer_view = root.plugin({"inject": ["provider"], "apply": consumer}, {"mode": "old"})
        await provider
        await consumer_view
        assert applied == [[1, "old"]]

        # both updates race; reloads coalesce into one final application
        t1 = asyncio.ensure_future(provider.update({"value": 2}))
        t2 = asyncio.ensure_future(consumer_view.update({"mode": "new"}))
        await asyncio.gather(t1, t2, return_exceptions=True)
        await provider
        await consumer_view

        assert applied[0] == [1, "old"]
        assert applied[-1] == [2, "new"]
        # every post-update application sees the new provider value
        assert all(row[0] == 2 for row in applied[1:])
        # wrapper transparency: config/state live on the real fiber
        assert consumer_view.config == {"mode": "new"}
        assert consumer_view.state == FiberState.ACTIVE
        assert consumer_view.config is consumer_view._fiber.config

    async def test_update_on_pending_fiber_defers_validation(self, root):
        calls: list = []

        def validator(config):
            if isinstance(config, dict) and isinstance(config.get("value"), int):
                return config
            from min_cordis import ValidationError

            return ValidationError("value must be an int")

        def plugin(c, cfg):
            calls.append(cfg["value"])

        plugin.Config = validator

        gate = asyncio.Event()

        async def provider_plugin(c, cfg):
            c.provide("svc", 1)
            await gate.wait()

        provider = root.plugin(provider_plugin)

        consumer = {"inject": ["svc"], "apply": plugin, "Config": validator}
        view = root.plugin(consumer, {"value": 1})
        # the consumer is PENDING while svc is not yet active
        assert view._fiber.state == FiberState.PENDING
        # deferred: validation happens at activation, not now (returns None)
        assert view.update({"value": 5}) is None

        gate.set()
        await provider
        await view
        assert calls == [5]


class TestAsyncGeneratorEffects:
    async def test_drains_all_then_disposes_reverse(self, root):
        seq: list = []

        async def agen():
            seq.append(1)
            yield lambda: seq.append(2)
            seq.append(3)
            yield lambda: seq.append(4)
            seq.append(5)
            yield lambda: seq.append(6)

        dispose = root.fiber.effect(agen)
        await asyncio.sleep(0.05)
        assert seq == [1, 3, 5]
        await dispose()
        assert seq == [1, 3, 5, 6, 4, 2]

    async def test_abort_before_first_yield(self, root):
        """TS dispose.spec 'async yield 2 (aborted)': the in-flight segment
        settles and is collected; later segments never run."""
        seq: list = []
        gate = asyncio.Event()

        async def agen():
            await gate.wait()
            seq.append(1)
            yield lambda: seq.append(2)
            seq.append(3)
            yield lambda: seq.append(4)

        dispose = root.fiber.effect(agen)
        await asyncio.sleep(0.05)
        assert seq == []
        task = asyncio.ensure_future(dispose())
        await asyncio.sleep(0.05)
        gate.set()
        await task
        assert seq == [1, 2]

    async def test_abort_between_segments(self, root):
        """TS dispose.spec 'async yield 3 (aborted)': one more segment runs
        (its disposer is collected), the rest are aborted."""
        seq: list = []
        gate = asyncio.Event()

        async def agen():
            seq.append(1)
            yield lambda: seq.append(2)
            await gate.wait()
            seq.append(3)
            yield lambda: seq.append(4)
            seq.append(5)
            yield lambda: seq.append(6)

        dispose = root.fiber.effect(agen)
        await asyncio.sleep(0.05)
        assert seq == [1]
        task = asyncio.ensure_future(dispose())
        await asyncio.sleep(0.05)
        gate.set()
        await task
        assert seq == [1, 3, 4, 2]

    async def test_plugin_body_async_gen_aborts_on_unload(self, root):
        seq: list = []
        gate = asyncio.Event()

        async def plugin(c, cfg):
            await gate.wait()  # blocked inside segment 1 (TS 'async yield 2')
            seq.append(1)
            yield lambda: seq.append(2)
            seq.append(3)
            yield lambda: seq.append(4)

        view = root.plugin(plugin)
        await asyncio.sleep(0.05)
        assert seq == []
        task = asyncio.ensure_future(view.dispose())
        await asyncio.sleep(0.05)
        gate.set()
        await task
        assert seq == [1, 2]

    async def test_sync_setup_error_propagates(self, root):
        seq: list = []

        def bad_setup():
            raise RuntimeError("setup boom")
            return lambda: seq.append(1)  # pragma: no cover

        with pytest.raises(RuntimeError, match="setup boom"):
            root.fiber.effect(bad_setup)
        assert seq == []
