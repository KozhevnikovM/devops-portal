"""Keyset (cursor) pagination value objects (#467).

A page continues strictly after `KeysetCursor` in `(created_at DESC, id DESC)` order. The opaque
wire encoding lives in the presentation layer; these are plain values.
"""
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from app.domain.entities import Environment


@dataclass(frozen=True)
class KeysetCursor:
    """Position of the last row already shown: the next page starts strictly after it."""
    created_at: datetime
    id: UUID


@dataclass(frozen=True)
class EnvironmentPage:
    """One page of environments; `next_cursor` is None on the last page."""
    items: list[Environment] = field(default_factory=list)
    next_cursor: KeysetCursor | None = None
