"""Minimal named logger service, ported from the TS core's ``logger.ts``.

Replaces the vendored Cordis logger (buffer, exporters, i18n levels) with a
level-filtered console sink. The core's error containment routes to the
``on_error`` sink injected at ``Context`` construction (the Python port's
diagnostics surface); this service exposes the TS parity API —
``ctx.logger('name')`` returns a named logger, ``ctx.logger.error(...)``
records into a bounded ring for inspection.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any

from ._service import Service
from ._traceable import Tracker

__all__ = ["Logger", "LoggerService"]

LEVELS = {"debug": 0, "info": 1, "success": 1, "warn": 2, "error": 3}


def _threshold() -> int:
    raw = os.environ.get("MIN_CORDIS_LOG")
    return LEVELS.get(raw, LEVELS["info"])  # type: ignore[arg-type]


class Logger:
    """A named logger; every line is prefixed with ``name``."""

    def __init__(self, name: str) -> None:
        self.name = name

    def _output(self, level: str, args: tuple) -> None:
        if LEVELS[level] < _threshold():
            return
        sink = sys.stderr if level in ("error", "warn") else sys.stdout
        stamp = datetime.now(timezone.utc).isoformat()
        print(f"[{stamp}] {level.upper():<7} {self.name}", *args, file=sink)

    def debug(self, *args: Any) -> None:
        self._output("debug", args)

    def info(self, *args: Any) -> None:
        self._output("info", args)

    def success(self, *args: Any) -> None:
        self._output("success", args)

    def warn(self, *args: Any) -> None:
        self._output("warn", args)

    def error(self, *args: Any) -> None:
        self._output("error", args)


class LoggerService(Service):
    """Logger service installed as ``ctx.logger``.

    Callable: ``ctx.logger('my-plugin')`` returns a named :class:`Logger`.
    The service also exposes the level methods directly so
    ``ctx.logger.error(...)`` works from framework diagnostics paths.
    Contained errors land in the bounded ``errors`` ring (newest kept).
    """

    ERROR_RING_LIMIT = 1000

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, "logger")
        # noShadow: the service is identity-aware (derives names from
        # callers), so tracing must keep the origin context association.
        self._tracker = Tracker(property="ctx", associate="logger", no_shadow=True)
        self.errors: list[tuple] = []

    def _invoke(self, name: str = "app") -> Logger:
        return Logger(name)

    def __call__(self, name: str = "app") -> Logger:
        return self._invoke(name)

    def debug(self, *args: Any) -> None:
        self._invoke().debug(*args)

    def info(self, *args: Any) -> None:
        self._invoke().info(*args)

    def success(self, *args: Any) -> None:
        self._invoke().success(*args)

    def warn(self, *args: Any) -> None:
        self._invoke().warn(*args)

    def error(self, *args: Any) -> None:
        self.errors.append(args)
        if len(self.errors) > LoggerService.ERROR_RING_LIMIT:
            del self.errors[: len(self.errors) - LoggerService.ERROR_RING_LIMIT]
        self._invoke().error(*args)
