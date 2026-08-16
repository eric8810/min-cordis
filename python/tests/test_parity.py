"""Regression tests for the Python-parity audit findings (F1-F4, E1-E3, C2).

Each test mirrors the scenario the deepseek-v4-pro audit used to demonstrate
the divergence; they lock the fixed semantics in place.
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


async def test_f2_provider_mounted_before_consumer_activates_it(ctx):
    """Audit F2: a provider that already exists must satisfy a new consumer."""
    seen: list[str] = []
    provider = ctx.plugin(lambda c, cfg: c.provide("svc", 42))
    await provider
    consumer = ctx.inject_plugins({"svc": None}, lambda c, cfg: seen.append(c.get("svc")))
    await consumer
    assert consumer._fiber.state == FiberState.ACTIVE
    assert seen == [42]


async def test_f1_parent_dispose_tears_down_children(ctx):
    """Audit F1: a child fiber's disposal lives on the parent's ledger."""
    hits: list[str] = []

    def child_plugin(c, cfg):
        c.on("evt/child", lambda: hits.append("child"))

    def parent_plugin(c, cfg):
        c.plugin(child_plugin)

    parent = ctx.plugin(parent_plugin)
    await parent
    ctx.emit("evt/child")
    assert hits == ["child"]
    await parent.dispose()
    ctx.emit("evt/child")
    assert hits == ["child"]  # child listener removed with the parent
    assert ctx.registry.values() == []


async def test_f3_async_effect_dispose_before_setup_waits(ctx):
    """Audit F3: dispose arriving during async setup must still run the disposer."""
    order: list[str] = []

    async def slow_setup():
        await asyncio.sleep(0.05)
        order.append("setup")
        return lambda: order.append("dispose")

    dispose = ctx.fiber.effect(slow_setup)
    await asyncio.sleep(0.01)  # dispose while setup is in flight
    await dispose()
    await asyncio.sleep(0.02)
    assert order == ["setup", "dispose"]


async def test_f4_generator_effect_and_plugin_body(ctx):
    """Audit F4: generator effects and plugin bodies collect yielded disposers."""
    order: list[str] = []

    def gen_effect():
        order.append("g-setup-1")
        yield lambda: order.append("g-dispose-1")
        order.append("g-setup-2")
        yield lambda: order.append("g-dispose-2")

    dispose = ctx.fiber.effect(gen_effect)
    assert order == ["g-setup-1", "g-setup-2"]  # sync generator drains eagerly
    await dispose()
    assert order[-2:] == ["g-dispose-2", "g-dispose-1"]

    # Generator plugin body: every yield is a disposer.
    body_order: list[str] = []

    def gen_plugin(c, cfg):
        body_order.append("p-setup-1")
        yield lambda: body_order.append("p-dispose-1")
        body_order.append("p-setup-2")
        yield lambda: body_order.append("p-dispose-2")

    view = ctx.plugin(gen_plugin)
    await view
    assert body_order == ["p-setup-1", "p-setup-2"]
    await view.dispose()
    assert body_order[-2:] == ["p-dispose-2", "p-dispose-1"]


async def test_e1_emit_propagates_sync_errors_and_contains_async(ctx, errors):
    """Audit E1: sync listener errors propagate; only async ones are contained."""

    def sync_boom():
        raise RuntimeError("sync-boom")

    ctx.on("evt/sync", sync_boom)
    with pytest.raises(RuntimeError, match="sync-boom"):
        ctx.emit("evt/sync")
    assert not any("sync-boom" in str(e) for e in errors)

    async def async_boom():
        raise RuntimeError("async-boom")

    ctx.on("evt/async", async_boom)
    ctx.emit("evt/async")
    await asyncio.sleep(0.05)
    assert any("async-boom" in str(e) for e in errors)


async def test_e2_internal_update_is_per_fiber(ctx):
    """Audit E2: `internal/update` hooks run only for the updating fiber."""
    calls: list[str] = []

    def plugin_a(c, cfg):
        c.on("internal/update", lambda cfg2, no_save, next: (calls.append("A"), next())[1])

    def plugin_b(c, cfg):
        c.on("internal/update", lambda cfg2, no_save, next: (calls.append("B"), next())[1])

    a = ctx.plugin(plugin_a)
    b = ctx.plugin(plugin_b)
    await a
    await b

    r = a.update({})
    if asyncio.iscoroutine(r): await r
    assert calls == ["A"]
    r = b.update({})
    if asyncio.iscoroutine(r): await r
    assert calls == ["A", "B"]
    # No accumulation across reloads.
    r = a.update({})
    if asyncio.iscoroutine(r): await r
    assert calls == ["A", "B", "A"]

    await a.dispose()
    await b.dispose()


async def test_e3_object_first_argument_is_dispatch_this(ctx):
    """Audit E3: a non-string first argument is `this`, not an event name."""
    hits: list[str] = []

    class Payload:
        pass

    ctx.on("evt/payload", lambda: hits.append("payload"))
    payload = Payload()
    ctx.emit(payload, "evt/payload")
    assert hits == ["payload"]


async def test_c2_notify_emits_internal_service(ctx):
    """Audit C2: service registration broadcasts `internal/service`."""
    events: list[tuple[str, Any]] = []
    ctx.on("internal/service", lambda name, value: events.append((name, value)))
    provider = ctx.plugin(lambda c, cfg: c.provide("svc", 7))
    await provider
    assert ("svc", 7) in events


async def test_get_effects_returns_labels(ctx):
    """Audit F5: get_effects() reports registered effect labels."""
    dispose = ctx.fiber.effect(lambda: (lambda: None), "my-effect")
    labels = ctx.fiber.get_effects()
    assert "my-effect" in labels
    await dispose()
    assert "my-effect" not in ctx.fiber.get_effects()


async def test_inertia_chain_on_dependency_flap(ctx):
    """Dependency flapping collapses into a single settled lifecycle."""
    seen: list[str] = []
    consumer = ctx.inject_plugins({"svc": None}, lambda c, cfg: seen.append(c.get("svc")))

    p1 = ctx.plugin(lambda c, cfg: c.provide("svc", "v1"))
    await p1
    await consumer
    p2 = ctx.plugin(lambda c, cfg: c.provide("svc", "v2"))
    await p1.dispose()
    await p2
    await consumer
    await asyncio.sleep(0.05)

    assert seen == ["v1", "v2"]
    assert consumer._fiber.state == FiberState.ACTIVE

    await consumer.dispose()
    await p2.dispose()


async def test_c4_isolate_labels_key_store_by_object(ctx):
    """Audit C4: the store is keyed by the label object, not id().

    An id can be reused after garbage collection, silently merging two
    isolation scopes. Keying by the object keeps entries alive-referenced
    and distinct across scope churn.
    """
    import gc

    iso = ctx.isolate("svc")
    view = iso.plugin(lambda c, cfg: c.provide("svc", "scoped"))
    await view
    assert iso.get("svc") == "scoped"

    await view.dispose()
    await asyncio.sleep(0.05)
    gc.collect()
    assert all(impl["name"] != "svc" for impl in ctx.reflect.store.values())

    # a fresh scope under the same name is fully independent
    iso2 = ctx.isolate("svc")
    view2 = iso2.plugin(lambda c, cfg: c.provide("svc", "scoped2"))
    await view2
    assert iso2.get("svc") == "scoped2"
    assert ctx.get("svc") is None
    await view2.dispose()


async def test_c5_ctx_setattr_cannot_bypass_write_validation(ctx):
    """Audit C5: attribute writes on a plugin context route through reflect.set.

    Plain writes would shadow the service-resolution path; the TS proxy set
    trap requires the name to be provided by the same fiber.
    """
    # root context (no runtime): plain writes allowed, mirroring the TS
    # lenient root branch
    ctx.custom = 1
    assert ctx.custom == 1

    def plugin(c, cfg):
        with pytest.raises(RuntimeError, match="cannot set property"):
            c.custom2 = 2

    view = ctx.plugin(plugin)
    await view

    def provider(c, cfg):
        dispose = c.provide("svc", 1)
        # attribute write on the providing fiber routes through reflect.set
        c.svc = 5
        # strict lookups skip non-ACTIVE fibers (still loading here)
        assert c.get("svc", strict=False) == 5
        return dispose

    view2 = ctx.plugin(provider)
    await view2

    # a foreign fiber cannot overwrite the provided value
    with pytest.raises(RuntimeError, match="cannot set property"):
        ctx.set("svc", 9)
    assert ctx.get("svc") == 5

    await view2.dispose()
