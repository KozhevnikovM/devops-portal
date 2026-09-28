"""Keyset (cursor) pagination value objects (#467, bookings #479).

A page continues strictly after `KeysetCursor` in `(created_at DESC, id DESC)` order. The opaque
wire encoding lives in the presentation layer; these are plain values.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from app.domain.entities import Environment

T = TypeVar("T")


@dataclass(frozen=True)
class KeysetCursor:
    """Position of the last row already shown: the next page starts strictly after it."""
    created_at: datetime
    id: UUID


@dataclass(frozen=True)
class KeysetPage(Generic[T]):
    """One page of a keyset-paginated list; `next_cursor` is None on the last page."""
    items: list[T] = field(default_factory=list)
    next_cursor: KeysetCursor | None = None


# One page of environments (#467).
EnvironmentPage = KeysetPage[Environment]
