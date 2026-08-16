"""min-cordis: the minimal Cordis core, ported to Python.

Context, services, plugins, fiber lifecycle, and the event bus — nothing
else. Ported from the trimmed TypeScript core (itself derived from Cordis
4.0.0-rc.7). See the repository README for what was removed and which audit
fixes are baked in.
"""

from ._context import Context
from ._events import Events
from ._fiber import CordisError, Fiber, FiberState, ValidationError
from ._registry import Registry

__version__ = "0.1.0"

__all__ = [
    "Context",
    "Events",
    "Fiber",
    "FiberState",
    "CordisError",
    "ValidationError",
    "Registry",
]
