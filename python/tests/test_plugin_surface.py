"""Plugin surface parity: shapes, rejection paths, display names, nesting,
root disposal, and Service.init disposal — ported from tests/plugin.spec.ts.

Documented deviation: the Python root fiber's ``uid`` is ``None`` (not TS's
``0``); display names surface through ``ctx.fiber.name`` instead of
``util.inspect``.
"""

from __future__ import annotations

import asyncio

import pytest

from min_cordis import Context, Service


@pytest.fixture
def errors():
    collected: list[BaseException] = []
    return collected


@pytest.fixture
def root(errors):
    return Context(on_error=errors.append)


class TestPluginShapes:
    async def test_object_plugin_receives_config_identity(self, root):
        seen: list = []
        options = {"bar": "foo"}

        def apply(c, cfg):
            seen.append(cfg)

        await root.plugin({"apply": apply}, options)
        assert seen == [options]
        assert seen[0] is options

    async def test_invalid_plugin_shapes_raise(self, root):
        with pytest.raises(TypeError):
            root.plugin(None)
        with pytest.raises(TypeError):
            root.plugin({})
        with pytest.raises(TypeError):
            root.plugin({"apply": {}})

    async def test_inactive_context_rejects_effects(self, root):
        checks: list = []

        def probe(c, cfg):
            def dispose():
                with pytest.raises(Exception, match="inactive context"):
                    c.plugin(lambda c2, cfg2: None)
                with pytest.raises(Exception, match="inactive context"):
                    c.effect(lambda: lambda: None)
                with pytest.raises(Exception, match="inactive context"):
                    c.on("evt/x", lambda: None)
                checks.append("done")

            return dispose

        view = await root.plugin(probe)
        await view.dispose()
        assert checks == ["done"]

    async def test_display_names_follow_plugin_shapes(self, root):
        names: list = []

        def unnamed(c, cfg):
            names.append(c.fiber.name)

        async def named_foo(c, cfg):
            names.append(c.fiber.name)

        def bar_apply(c, cfg):
            names.append(c.fiber.name)

        class Qux:
            def __init__(self, c, cfg=None):
                names.append(c.fiber.name)

        assert root.fiber.name == "root"
        await root.plugin(unnamed)
        await root.plugin(named_foo)
        await root.plugin({"name": "bar", "apply": bar_apply})
        await root.plugin(Qux)
        # deviation: Python functions are always named (__name__); TS's
        # anonymous-inherits-ancestor case has no Python equivalent.
        assert names == ["unnamed", "named_foo", "bar", "Qux"]

    async def test_nested_plugins_registry_and_double_dispose(self, root):
        calls: list = []

        async def middle(c, cfg):
            c.on("evt/nest", lambda: calls.append("m"))

            async def inner(c2, cfg2):
                c2.on("evt/nest", lambda: calls.append("i"))
                await c2.plugin(lambda c3, cfg3: c3.on("evt/nest", lambda: calls.append("deep")))

            await c.plugin(inner)

        root.on("evt/nest", lambda: calls.append("root"))
        view = await root.plugin(middle)

        assert len(root.registry.values()) == 3
        root.emit("evt/nest")
        assert calls == ["root", "m", "i", "deep"]

        calls.clear()
        await view.dispose()
        assert len(root.registry.values()) == 0
        root.emit("evt/nest")
        assert calls == ["root"]

        # second dispose is a no-op
        calls.clear()
        await view.dispose()
        assert len(root.registry.values()) == 0
        root.emit("evt/nest")
        assert calls == ["root"]

    async def test_snapshot_restores_after_registry_delete(self, root):
        async def plugin(c, cfg):
            c.on("evt/snap", lambda: None)

            async def inner(c2, cfg2):
                c2.on("evt/snap", lambda: None)
                await c2.plugin(lambda c3, cfg3: c3.on("evt/snap", lambda: None))

            await c.plugin(inner)

        def snapshot():
            return {name: len(hooks) for name, hooks in root.events._hooks.items() if hooks}

        before = snapshot()
        await root.plugin(plugin)
        after = snapshot()
        assert after.get("evt/snap") == 3  # non-trivial: three live listeners

        root.registry.delete(plugin)
        await asyncio.sleep(0.05)
        assert snapshot() == before

        await root.plugin(plugin)
        assert snapshot() == after

    async def test_root_dispose_drains_children_idempotently(self, root):
        order: list = []

        def plugin(c, cfg):
            def dispose():
                order.append(1)

            return dispose

        view = root.plugin(plugin)
        fiber = await view
        # documented alignment: root fiber uid is 0 (TS parity)
        assert root.fiber.uid == 0
        assert fiber.uid == 1
        assert order == []
        assert len(root.fiber._disposables) == 1

        await root.fiber.dispose()
        assert root.fiber.uid == 0  # root uid is stable across disposal
        assert fiber.uid is None
        assert order == [1]
        assert len(root.fiber._disposables) == 0

        # idempotent
        await root.fiber.dispose()
        assert order == [1]
        assert len(root.fiber._disposables) == 0

    async def test_service_init_disposer_runs_once(self, root):
        started: list = []
        stopped: list = []

        class Foo:
            def __init__(self, c, cfg=None):
                pass

            def _init(self):
                started.append(1)

                def stop():
                    stopped.append(1)

                return stop

        view = await root.plugin(Foo)
        assert started == [1]
        assert stopped == []
        await view.dispose()
        assert started == [1]
        assert stopped == [1]
