"""min-cordis: the minimal Cordis core, ported to Python.

Context, services, plugins, fiber lifecycle, the event bus, and the
traceable caller/caller-shadow machinery — nothing else. Ported from the
trimmed TypeScript core (itself derived from Cordis 4.0.0-rc.7). See the
repository README for what was removed and which audit fixes are baked in.
"""

from ._context import Context
from ._events import Events
from ._fiber import CordisError, Fiber, FiberState, ValidationError
from ._logger import Logger, LoggerService
from ._registry import Registry
from ._service import Inject, Service
from ._traceable import Tracker, get_traceable

__version__ = "0.1.0"

__all__ = [
    "Context",
    "Events",
    "Fiber",
    "FiberState",
    "CordisError",
    "ValidationError",
    "Registry",
    "Service",
    "Inject",
    "Tracker",
    "get_traceable",
    "Logger",
    "LoggerService",
]
