"""Traceable caller/shadow semantics, ported from the TS suite.

- tests/shadow.spec.ts (4/4): caller attribution, noShadow services,
  callable services, shadow stripping before plugin creation.
- tests/invoke.spec.ts: functional (callable) services — intercept config,
  ``extend``, context traceability.
- tests/associate.spec.ts #1/#2/#5: dotted sub-service association.
  (#3/#4 exercise ``ctx.mixin``, which is a separate work item.)

Python-specific adaptations (documented deviations, see
docs/design-python-traceable.md):

- Root-context attribute reads stay tightened (inject required); tests use
  ``ctx.get(name)`` — the explicit escape hatch — where the TS suite reads
  services off the root context directly.
"""

from __future__ import annotations

import asyncio

import pytest

from min_cordis import Context, Service, Tracker


@pytest.fixture
def errors():
    collected: list[BaseException] = []
    return collected


@pytest.fixture
def root(errors):
    return Context(on_error=errors.append)


class TestShadowCaller:
    async def test_keeps_caller_separate_from_service_shadow(self, root):
        inner_origin: dict = {}
        outer_origin: dict = {}

        class Inner(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "inner")
                inner_origin["ctx"] = ctx

            def inspect(self):
                return {
                    "caller": self._caller,
                    "shadow": self.ctx._shadow,
                }

        class Outer(Service):
            inject = ["inner"]

            def __init__(self, ctx, config=None):
                super().__init__(ctx, "outer")
                outer_origin["ctx"] = ctx

            def inspect(self):
                result = self.ctx.inner.inspect()
                return {**result, "outer_shadow": self.ctx._shadow}

        await root.plugin(Inner)
        await root.plugin(Outer)

        result: dict = {}

        def consumer(ctx, config):
            result.update(ctx.outer.inspect())

        await root.inject_plugins(["outer"], consumer)

        assert result["caller"] is outer_origin["ctx"]
        assert result["shadow"] is inner_origin["ctx"]
        assert result["outer_shadow"] is outer_origin["ctx"]

    async def test_exposes_caller_without_shadow_for_noshadow_services(self, root):
        outer_origin: dict = {}

        class Probe:
            _tracker = Tracker(property="ctx", no_shadow=True)

            def __init__(self, ctx):
                self.ctx = ctx

            def inspect(self):
                return {
                    "caller": self._caller,
                    "shadow": self.ctx._shadow,
                }

        class Outer(Service):
            inject = ["probe"]

            def __init__(self, ctx, config=None):
                super().__init__(ctx, "outer")
                outer_origin["ctx"] = ctx

            def inspect(self):
                return self.ctx.probe.inspect()

        root.provide("probe", Probe(root))
        await root.plugin(Outer)

        result: dict = {}

        def consumer(ctx, config):
            result.update(ctx.outer.inspect())

        await root.inject_plugins(["outer"], consumer)

        assert result["caller"] is outer_origin["ctx"]
        assert result["shadow"] is None

    async def test_exposes_caller_to_callable_services(self, root):
        outer_origin: dict = {}

        class Callable(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "callable")

            def _invoke(self):
                return self._caller

        class Outer(Service):
            inject = ["callable"]

            def __init__(self, ctx, config=None):
                super().__init__(ctx, "outer")
                outer_origin["ctx"] = ctx

            def call(self):
                return self.ctx.callable()

        await root.plugin(Callable)
        await root.plugin(Outer)

        caller: list = []

        def consumer(ctx, config):
            caller.append(ctx.outer.call())

        await root.inject_plugins(["outer"], consumer)

        assert caller[0] is outer_origin["ctx"]

    async def test_strips_service_shadow_before_creating_plugins(self, root, errors):
        class Loader(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "loader")

            def load(self, plugin):
                return self.ctx.plugin(plugin)

        class Server(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "server")

        injected: list = []

        def consumer(ctx, config):
            def check(c, cfg):
                view = c.server
                # isinstance alone passes for an untraced raw instance too
                # (wrapper __class__ passthrough); the binding assertion is
                # the discriminating check.
                injected.append(isinstance(view, Server) and view._binding is c)

            return ctx.inject_plugins(["server"], check)

        await root.plugin(Loader)

        async def use_loader(ctx, config):
            loader = ctx.loader
            await loader.load(Server)
            await loader.load(consumer)

        await root.inject_plugins(["loader"], use_loader)

        assert injected == [True]
        assert errors == []


class TestFunctionalService:
    async def test_functional_service(self, root, errors):
        class Foo(Service):
            def __init__(self, ctx, config=None):
                self.config = dict(config or {})
                super().__init__(ctx, "foo")

            def _invoke(self, init=None):
                assert isinstance(self.ctx, Context)
                result = dict(self.config)
                for entry in self.ctx._intercept_entries("foo"):
                    result.update(entry)
                result.update(init or {})
                return result

            def invoke(self):
                return self()

            def extend(self, config=None):
                return self._extend({"config": {**self.config, **(config or {})}})

        await root.plugin(Foo, {"a": 1})

        # access from context (root reads tightened; ctx.get is the escape hatch)
        foo = root.get("foo")
        assert foo() == {"a": 1}
        ctx1 = root.intercept("foo", {"b": 2})
        foo1 = ctx1.get("foo")
        assert foo1() == {"a": 1, "b": 2}
        assert isinstance(foo1, Foo)

        # create extension
        foo2 = foo.extend({"c": 3})
        assert isinstance(foo2, Foo)
        assert foo2() == {"a": 1, "c": 3}
        foo3 = foo1.extend({"d": 4})
        assert isinstance(foo3, Foo)
        assert foo3.invoke() == {"a": 1, "b": 2, "d": 4}

        # context traceability
        assert foo1.invoke() == {"a": 1, "b": 2}
        assert errors == []

    async def test_uses_service_shadow_for_callable_extensions(self, root):
        class Dependency(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "dependency")

        class Callable(Service):
            inject = ["dependency"]

            def __init__(self, ctx, config=None):
                super().__init__(ctx, "callable")

            def _invoke(self):
                return self.ctx.dependency

            def extend(self):
                return self._extend()

        class Outer(Service):
            inject = ["callable"]

            def __init__(self, ctx, config=None):
                super().__init__(ctx, "outer")

            def call(self):
                callable_ = self.ctx.callable
                return [callable_(), callable_.extend()()]

        instance: list = []
        orig_init = Dependency.__init__

        def capture_init(self, ctx, config=None):
            orig_init(self, ctx, config)
            instance.append(self)

        Dependency.__init__ = capture_init
        try:
            await root.plugin(Dependency)
        finally:
            Dependency.__init__ = orig_init
        await root.plugin(Callable)
        await root.plugin(Outer)

        result: list = []

        def consumer(ctx, config):
            result.extend(ctx.outer.call())

        await root.inject_plugins(["outer"], consumer)

        assert len(result) == 2
        assert all(isinstance(item, Dependency) for item in result)
        # identity: both views wrap the SAME registered instance (not fresh
        # constructions), and remain traceable wrappers
        from min_cordis._traceable import _TraceableView

        assert all(isinstance(item, _TraceableView) for item in result)
        assert all(item._root_target() is instance[0] for item in result)


class TestViewWriteRejections:
    """TS proxy set trap parity: ctx/caller/original writes are rejected."""

    async def test_rejects_ctx_caller_original_writes(self, root):
        class Foo(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")

        await root.plugin(Foo)
        view = root.get("foo")
        with pytest.raises(AttributeError):
            view.ctx = root
        with pytest.raises(AttributeError):
            view._caller = root
        with pytest.raises(AttributeError):
            view._original = object()
        with pytest.raises(AttributeError):
            del view.ctx

    async def test_rejects_writes_on_derived_services(self, root):
        class Foo(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")

        await root.plugin(Foo)
        derived = root.get("foo")._extend({"x": 1})
        with pytest.raises(AttributeError):
            derived.ctx = root
        with pytest.raises(AttributeError):
            derived._caller = root
        # plain own props still work
        derived.x = 2
        assert derived.x == 2


class TestAssociation:
    async def test_service_injection(self, root):
        class Foo(Service):
            def __init__(self, ctx, config=None):
                self.qux = 1
                super().__init__(ctx, "foo")

        class FooBar(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo.bar")

        await root.plugin(Foo)
        fiber = await root.plugin(FooBar)
        foo = root.get("foo")
        assert isinstance(foo, Foo)
        assert isinstance(foo.bar, FooBar)
        assert foo.qux == 1
        await fiber.dispose()
        assert foo.bar is None

    async def test_property_injection(self, root):
        class Foo(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")

        root.provide("foo.bar")
        root.provide("foo.baz")
        await root.plugin(Foo)
        foo = root.get("foo")
        assert isinstance(foo, Foo)
        foo.qux = 2
        foo.bar = 3

        def baz(self):
            return self

        foo.baz = baz

        seen: list = []

        def consumer(ctx, config):
            assert ctx.foo.qux == 2
            assert ctx.foo.bar == 3
            # direct dotted reads stay tightened: an unprovided name fails
            # loudly, a provided one resolves through the escape hatch.
            with pytest.raises(RuntimeError, match="without inject"):
                getattr(ctx, "foo.qux")
            assert ctx.get("foo.bar") == 3
            assert isinstance(ctx.foo.baz(), Foo)
            seen.append(1)

        await root.inject_plugins(["foo"], consumer)
        assert seen == [1]

    async def test_inspect_passes_args_through(self, root):
        checks: list = []

        class Foo(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")

            def bar(self, arg):
                checks.append(arg.__name__ == "X")
                self.baz(arg)

            def baz(self, arg):
                checks.append(arg.__name__ == "X")

        await root.plugin(Foo)

        class X:
            pass

        root.get("foo").bar(X)
        assert checks == [True, True]

    async def test_associated_type_service_injection(self, root):
        class Session:
            _tracker = Tracker(property="ctx", associate="session")

            def __init__(self, ctx):
                self.ctx = ctx

        class Foo(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")

            def create_session(self):
                return Session(self.ctx)

        class Bar(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "bar")
                ctx.mixin("bar", {"answer": "session.answer"})

            def answer(self):
                return 42

        await root.plugin(Foo)
        ran: list = []

        async def scope(ctx, config):
            session = ctx.foo.create_session()
            assert isinstance(session, Session)
            # no 'session.answer' accessor yet: TS reads undefined, Python
            # raises AttributeError (documented deviation)
            assert getattr(session, "answer", None) is None

            await ctx.plugin(Bar)
            await asyncio.sleep(0.05)

            def inner(ctx2, config2):
                session2 = ctx2.foo.create_session()
                assert isinstance(session2, Session)
                assert session2.answer() == 42
                ran.append("inner")

            await ctx.inject_plugins(["bar"], inner)
            ran.append("scope")

        await root.inject_plugins(["foo"], scope)
        assert ran == ["inner", "scope"]  # outer markers: both bodies ran

    async def test_associated_type_object_source_matches_upstream(self, root):
        # Upstream associate.spec #4: the inner `ctx.inject(['bar'], ...)`
        # block waits for a service that is never provided — it is dead code
        # (verified by probe against the TS suite). The asserted behavior is
        # the outer read: an object-source mixin target is not a valid
        # context property, so plugin-context reads raise.
        class Session:
            _tracker = Tracker(property="ctx", associate="session")

            def __init__(self, ctx):
                self.ctx = ctx

        class Foo(Service):
            def __init__(self, ctx, config=None):
                super().__init__(ctx, "foo")

            def session(self):
                return Session(self.ctx)

        class Bar:
            def __init__(self, ctx, config=None):
                ctx.mixin(self, {"bar": "session.bar"})

            @property
            def bar(self):
                return getattr(self, "_secret", None)

            @bar.setter
            def bar(self, value):
                self._secret = value + 1

        await root.plugin(Foo)
        await root.plugin(Bar)

        # root-context read (no runtime): TS lenient lookup resolves undefined
        assert root.get("foo").session().bar is None

        ran: list = []

        async def scope(ctx, config):
            session = ctx.foo.session()
            with pytest.raises(RuntimeError, match="without inject"):
                _ = session.bar
            ran.append(1)

        await root.inject_plugins(["foo"], scope)
        assert ran == [1]  # outer marker: the scope body ran
