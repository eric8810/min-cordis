"""Core behavior tests for the Python port, mirroring the TS suite's spirit:

- fiber lifecycle and reverse-order disposal
- provide/inject dependency reload
- the five dispatch modes
- the audit fixes (emit containment, status containment, rejected update)
"""

from __future__ import annotations

import asyncio

import pytest

from min_cordis import Context, FiberState


@pytest.fixture
def errors():
    collected: list[BaseException] = []
    return collected


@pytest.fixture
def ctx(errors):
    return Context(on_error=errors.append)


async def test_mount_reverse_disposal_and_await_resolves_view(ctx):
    order: list[str] = []

    async def plugin(c, cfg):
        c.effect(lambda: (order.append("setup-a"), lambda: order.append("dispose-a"))[1])
        c.effect(lambda: (order.append("setup-b"), lambda: order.append("dispose-b"))[1])
        c.effect(lambda: (order.append("setup-c"), lambda: order.append("dispose-c"))[1])

    view = ctx.plugin(plugin)
    real = view._fiber
    assert await view is real
    assert real.state == FiberState.ACTIVE
    await view.dispose()
    assert order == ["setup-a", "setup-b", "setup-c", "dispose-c", "dispose-b", "dispose-a"]


async def test_provide_inject_reload(ctx):
    seen: list[str] = []
    consumer = ctx.inject_plugins({"svc": None}, lambda c, cfg: seen.append(c.get("svc")))

    provider = ctx.plugin(lambda c, cfg: c.provide("svc", "v1"))
    await provider
    await consumer
    assert seen == ["v1"]
    assert consumer._fiber.state == FiberState.ACTIVE

    provider2 = ctx.plugin(lambda c, cfg: c.provide("svc", "v2"))
    await provider.dispose()
    await provider2
    await consumer
    assert seen == ["v1", "v2"]

    await consumer.dispose()
    await provider2.dispose()


async def test_service_requires_inject_for_attribute_read(ctx):
    view = ctx.plugin(lambda c, cfg: c.provide("svc", 42))
    await view
    assert view.state == FiberState.ACTIVE
    with pytest.raises(RuntimeError, match="without inject"):
        _ = ctx.svc
    assert ctx.get("svc") == 42


async def test_five_dispatch_modes(ctx):
    order: list[str] = []
    ctx.on("evt/simple", lambda arg: order.append(f"simple:{arg}"))
    ctx.emit("evt/simple", "x")
    assert order == ["simple:x"]

    async def boom():
        raise RuntimeError("par-boom")

    ctx.on("evt/par", boom)
    with pytest.raises(ExceptionGroup, match="parallel dispatch failed"):
        await ctx.parallel("evt/par")

    async def noop():
        return None

    async def first():
        return "first-bail"

    ctx.on("evt/serial", noop)
    ctx.on("evt/serial", first)
    assert await ctx.serial("evt/serial") == "first-bail"

    ctx.on("evt/bail", lambda: None)
    ctx.on("evt/bail", lambda: "sync-bail")
    assert ctx.bail("evt/bail") == "sync-bail"

    # waterfall: shared mutable object, delegate via next()
    def a(req, next):
        req["v"] += "+a"
        return next()

    def b(req, next):
        req["v"] += "+b"
        return next()

    ctx.on("evt/wf", a)
    ctx.on("evt/wf", b)
    req = {"v": "v"}
    assert ctx.waterfall("evt/wf", req, lambda r: r["v"] + "!") == "v+a+b!"

    ctx.on("evt/wf", lambda req, next: "vetoed")
    req2 = {"v": "v"}
    assert ctx.waterfall("evt/wf", req2, lambda r: r["v"] + "!") == "vetoed"

    once_count = []
    ctx.once("evt/once", lambda: once_count.append(1))
    ctx.emit("evt/once")
    ctx.emit("evt/once")
    assert len(once_count) == 1


async def test_listeners_unload_with_fiber(ctx):
    hits: list[str] = []
    view = ctx.plugin(lambda c, cfg: c.on("evt/scoped", lambda: hits.append("hit")))
    await view
    ctx.emit("evt/scoped")
    assert hits == ["hit"]
    await view.dispose()
    ctx.emit("evt/scoped")
    assert hits == ["hit"]


async def test_isolate_scopes_service(ctx):
    iso = ctx.isolate("svc")
    view = iso.plugin(lambda c, cfg: c.provide("svc", "scoped"))
    await view
    assert iso.get("svc") == "scoped"
    assert ctx.get("svc") is None
    await view.dispose()


async def test_audit_emit_contains_async_rejections(ctx, errors):
    async def bad():
        raise RuntimeError("async boom")

    ctx.on("evt/async", bad)
    ctx.emit("evt/async")
    await asyncio.sleep(0.05)
    assert any("async boom" in str(e) for e in errors)


async def test_audit_status_observer_cannot_stall_dependents(ctx, errors):
    def bad_observer(fiber, old):
        raise RuntimeError("observer boom")

    ctx.on("internal/status", bad_observer)

    seen: list[str] = []
    consumer = ctx.inject_plugins({"svc": None}, lambda c, cfg: seen.append(c.get("svc")))
    provider = ctx.plugin(lambda c, cfg: c.provide("svc", "value"))
    await provider
    await consumer
    assert consumer._fiber.state == FiberState.ACTIVE
    assert seen == ["value"]
    assert any("observer boom" in str(e) for e in errors)

    await consumer.dispose()
    await provider.dispose()


async def test_audit_rejected_update_does_not_poison(ctx):
    calls: list[int] = []

    def validator(config):
        if isinstance(config, dict) and isinstance(config.get("value"), int) and not isinstance(config.get("value"), bool):
            return config
        from min_cordis import ValidationError

        return ValidationError("value must be an int")

    def plugin(c, cfg):
        calls.append(cfg["value"])
        c.effect(lambda: lambda: calls.append(-99))

    plugin.Config = validator

    view = ctx.plugin(plugin, {"value": 1})
    await view

    await view.update({"value": 2})
    assert calls == [1, -99, 2]

    with pytest.raises(Exception):
        view.update({"value": "bad"})

    await view.restart()
    # After a rejected update, restart must reuse the last good config (2),
    # not the rejected one.
    assert calls == [1, -99, 2, -99, 2]
    await view.dispose()

