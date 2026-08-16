"""Shared internal helpers: interned symbols, disposable lists, error containment.

The Python port replaces JS Symbol.for with a module-level intern table and
the WeakMap-backed DisposableList with insertion-ordered dicts + weakrefs.
"""

from __future__ import annotations

import sys
import weakref
from collections.abc import Callable, Iterable
from typing import Any

InternedSymbols: dict[str, str] = {}
"""Intern table standing in for JS ``Symbol.for`` (identity by string)."""


class Symbols:
    """Namespaced attribute keys, mirroring ``cordis``'s symbol registry."""

    def __getattr__(self, name: str) -> str:
        raise AttributeError(name)


def sym(key: str) -> str:
    """Return the interned marker for ``key`` (``Symbol.for`` equivalent)."""
    return InternedSymbols.setdefault(key, key)


# Attribute keys used by the core.
SHADOW = sym("cordis.shadow")
CALLER = sym("cordis.caller")
ORIGINAL = sym("cordis.original")
FILTER = sym("cordis.filter")
ISOLATE = sym("cordis.isolate")
INTERCEPT = sym("cordis.intercept")
TRACKER = sym("cordis.tracker")


class DisposableList:
    """Ordered disposables with O(1) removal; iteration is registration order."""

    def __init__(self) -> None:
        self._sn = 0
        self._items: dict[int, Callable[[], Any]] = {}
        self._by_value: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterable[Callable[[], Any]]:
        return iter(list(self._items.values()))

    def push(self, value: Callable[[], Any]) -> Callable[[], None]:
        """Register ``value``; returns a remover closure."""
        self._sn += 1
        sn = self._sn
        self._items[sn] = value
        try:
            self._by_value[value] = sn
        except TypeError:
            pass  # plain functions are not weakref-able targets? they are; keep guard for lambdas-free objects
        return lambda: self._items.pop(sn, None)

    def delete(self, value: Callable[[], Any]) -> bool:
        sn = self._by_value.get(value)
        if sn is None:
            return False
        return self._items.pop(sn, None) is not None

    def clear(self) -> list[Callable[[], Any]]:
        """Remove and return all disposables in reverse registration order."""
        values = list(self._items.values())
        self._items.clear()
        return values[::-1]


def is_nullable(value: Any) -> bool:
    return value is None


def make_error_logger(sink: Callable[[BaseException], None] | None) -> Callable[[BaseException], None]:
    """Contain an error with the given sink (default: stderr print)."""
    if sink is None:
        def sink(exc: BaseException) -> None:  # noqa: F811
            print(f"[min-cordis] contained error: {exc!r}", file=sys.stderr)
    return sink
