"""Logger service and the internal/get / internal/set waterfall hooks."""

from __future__ import annotations

import pytest

from min_cordis import Context, Logger, LoggerService


@pytest.fixture
def errors():
    collected: list[BaseException] = []
    return collected


@pytest.fixture
def ctx(errors):
    return Context(on_error=errors.append)


class TestLogger:
    def test_named_logger_respects_threshold(self, ctx, monkeypatch, capsys):
        monkeypatch.setenv("MIN_CORDIS_LOG", "debug")
        log = ctx.logger("test-app")
        assert isinstance(log, Logger)
        log.info("hello")
        captured = capsys.readouterr()
        assert "test-app" in captured.out
        assert "hello" in captured.out

        # below the threshold: dropped
        monkeypatch.setenv("MIN_CORDIS_LOG", "error")
        log.info("hidden")
        assert capsys.readouterr().out == ""

        # error level goes to stderr
        log.error("visible")
        captured = capsys.readouterr()
        assert "visible" in captured.err

    async def test_service_is_callable_and_keeps_error_ring(self, ctx, monkeypatch):
        monkeypatch.setenv("MIN_CORDIS_LOG", "error")  # silence stderr in this test
        assert isinstance(ctx.logger, LoggerService)
        named = ctx.logger("plugin-a")
        assert isinstance(named, Logger)
        assert named.name == "plugin-a"

        ctx.logger.error("boom", 42)
        ctx.logger.error("again")
        assert ctx.logger.errors[-2:] == [("boom", 42), ("again",)]

    async def test_error_ring_eviction_keeps_newest(self, ctx, monkeypatch):
        monkeypatch.setenv("MIN_CORDIS_LOG", "error")  # silence stderr in this test
        for i in range(1002):
            ctx.logger.error("e", i)
        assert len(ctx.logger.errors) == LoggerService.ERROR_RING_LIMIT
        assert ctx.logger.errors[0] == ("e", 2)  # oldest kept entry
        assert ctx.logger.errors[-1] == ("e", 1001)

    def test_invalid_log_level_falls_back_to_info(self, ctx, monkeypatch, capsys):
        monkeypatch.setenv("MIN_CORDIS_LOG", "not-a-level")
        log = ctx.logger("fallback")
        log.debug("hidden")
        assert capsys.readouterr().out == ""
        log.info("shown")
        assert "shown" in capsys.readouterr().out


class TestInternalGetWaterfall:
    async def test_intercepts_and_delegates(self, ctx):
        provider = ctx.plugin(lambda c, cfg: c.provide("svc", 42))
        await provider

        calls: list = []

        def handler(c, name, error, nxt):
            calls.append(name)
            if name == "svc":
                return "intercepted"
            return nxt()

        dispose = ctx.on("internal/get", handler)
        seen: list = []
        consumer = ctx.inject_plugins(["svc"], lambda c, cfg: seen.append(c.svc))
        await consumer
        assert seen == ["intercepted"]
        assert "svc" in calls
        await dispose()

        seen2: list = []
        consumer2 = ctx.inject_plugins(["svc"], lambda c, cfg: seen2.append(c.svc))
        await consumer2
        assert seen2 == [42]
        await consumer2.dispose()

    async def test_error_carrier_reaches_listener(self, ctx):
        provider = ctx.plugin(lambda c, cfg: c.provide("svc", 42))
        await provider

        kinds: list = []

        def handler(c, name, error, nxt):
            kinds.append(type(error).__name__)
            return nxt()

        dispose = ctx.on("internal/get", handler)
        seen: list = []
        consumer = ctx.inject_plugins(["svc"], lambda c, cfg: seen.append(c.svc))
        await consumer
        assert seen == [42]
        assert kinds and all(k == "RuntimeError" for k in kinds)
        await dispose()
        await consumer.dispose()
        await provider.dispose()


class TestInternalSetWaterfall:
    async def test_short_circuit_and_delegation(self, ctx):
        blocked: list = []
        carriers: list = []

        def handler(c, name, value, error, nxt):
            carriers.append(type(error).__name__)
            if value == 99:
                blocked.append(name)
                return True  # short-circuit: the write is swallowed
            return nxt()

        ctx.on("internal/set", handler)

        def provider(c, cfg):
            dispose = c.provide("svc", 1)
            c.svc = 5  # delegates through next() into reflect.set
            c.svc = 99  # short-circuited by the handler
            return dispose

        view = ctx.plugin(provider)
        await view

        assert blocked == ["svc"]
        assert ctx.get("svc") == 5
        assert carriers and all(k == "RuntimeError" for k in carriers)
        await view.dispose()
