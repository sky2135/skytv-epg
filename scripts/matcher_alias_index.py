"""Index approved alias keys while retaining the frozen lookup policy verbatim.

Only the shortlist changes: every matching row retains its original position,
and the original matcher still evaluates every target, market, language and
timeshift condition. The resolver remains sequential, like the frozen engine.
"""
from __future__ import annotations

from typing import Any

from skytv_epg_contextual_v8 import ContextualMatcherV8, _normalize_key_v8


class _AliasView:
    """Delegate policy state; expose only rows that can pass its alias test."""

    def __init__(self, resolver: Any, rows: list[dict[str, Any]]):
        self._resolver = resolver
        self.approved_aliases = rows

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolver, name)


class AliasIndex:
    def __init__(self, resolver: ContextualMatcherV8):
        self.resolver = resolver
        self._original_register = resolver.register_approved_aliases
        self._original_lookup = resolver._approved_alias_match.__func__
        self._rows: object = None
        self._length = -1
        self._dirty = True
        self._by_key: dict[str, tuple[int, ...]] = {}
        self.stats = {"builds": 0, "lookups": 0, "candidateRows": 0, "totalRows": 0}
        self.refresh()

    def refresh(self) -> None:
        """Rebuild after an explicit in-place edit to an alias spelling.

        Normal registration/loading and list replacement/append are detected
        automatically. Other policy fields remain live references to the rows.
        """
        rows = self.resolver.approved_aliases
        by_key: dict[str, list[int]] = {}
        for position, row in enumerate(rows):
            key = _normalize_key_v8(row.get("alias", ""))
            by_key.setdefault(key, []).append(position)
        self._by_key = {key: tuple(positions) for key, positions in by_key.items()}
        self._rows = rows
        self._length = len(rows)
        self._dirty = False
        self.stats["builds"] += 1

    def register(self, *args: Any, **kwargs: Any) -> int:
        try:
            return self._original_register(*args, **kwargs)
        finally:
            # A failing input iterator can still have registered an earlier row.
            self._dirty = True

    def lookup(self, query: Any) -> dict[str, Any] | None:
        rows = self.resolver.approved_aliases
        if self._dirty or self._rows is not rows or self._length != len(rows):
            self.refresh()
        keys = {query.strict_key, query.relaxed_key, *query.identity_keys}
        if query.timeshift:
            keys.add(query.edition_key)
        positions = sorted({position for key in keys for position in self._by_key.get(key, ())})
        selected = [rows[position] for position in positions]
        self.stats["lookups"] += 1
        self.stats["candidateRows"] += len(selected)
        self.stats["totalRows"] += len(rows)
        # Execute the frozen body, with its own normalization and original row
        # priority. No matching rule is copied or relaxed in this adapter.
        return self._original_lookup(_AliasView(self.resolver, selected), query)


def install_alias_index(resolver: ContextualMatcherV8) -> AliasIndex:
    """Install once after learned aliases; return private aggregate counters."""
    installed = getattr(resolver, "_alias_index_adapter", None)
    if isinstance(installed, AliasIndex):
        return installed
    lookup = getattr(resolver, "_approved_alias_match", None)
    if getattr(lookup, "__func__", None) is not ContextualMatcherV8._approved_alias_match:
        raise TypeError("Alias indexing requires the unmodified contextual lookup")
    adapter = AliasIndex(resolver)
    resolver.register_approved_aliases = adapter.register
    resolver._approved_alias_match = adapter.lookup
    resolver._alias_index_adapter = adapter
    return adapter
