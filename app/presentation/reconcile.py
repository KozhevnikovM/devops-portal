"""Page row reconciliation helpers (#497): row versions, list keys and request parsing.

A list section sends one bounded request per interval naming displayed rows and the version it
holds for each; the server re-renders only rows whose version differs. A version is a digest of
the list-safe values a row displays, so every render path (list, Load more, single row, live
update, action responses) emits the same version for the same state.
"""
import hashlib
import json
import re
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum
from uuid import UUID

from app.domain.booking_list import BookingListItem
from app.domain.enums import BookingStatus
from app.domain.pagination import KeysetCursor
from app.presentation.pagination import InvalidCursorError, decode_cursor, encode_cursor

VERSION_LENGTH = 16  # hex characters (64 bits of SHA-256)

# Every value the booking row can show is a BookingListItem field, and `Booking` exposes the same
# names (the derived ones as properties) — so hashing all of them covers every displayed value,
# for either type, with nothing to forget when the row gains a field.
_BOOKING_FIELDS = tuple(f.name for f in fields(BookingListItem))
_ENVIRONMENT_FIELDS = ("name", "blueprint_name", "owner_username", "created_by",
                       "created_by_username", "ttl_minutes", "expires_at", "derived_status")
# What environment_row.html shows of each child — never its log or credentials.
_CHILD_FIELDS = ("id", "status", "environment_label", "resource_type", "namespace_name",
                 "static_vm_name", "static_vm_host", "image_name", "vm_ip", "config_failed")

# Settled rows can still change (e.g. READY → RELEASING) but much more rarely than in-flight ones;
# the client reconciles in-flight rows first (spec: "Reconciliation states its worst-case
# convergence"). RELEASED is final: such a row is neither live nor reconciled.
_SETTLED = frozenset({"READY", "FAILED"})


def _plain(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, UUID)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _digest(values) -> str:
    raw = json.dumps(_plain(values), separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:VERSION_LENGTH]


def row_version(booking) -> str:
    """The version of a booking row (a `Booking` or a `BookingListItem`)."""
    return _digest([getattr(booking, name) for name in _BOOKING_FIELDS])


def environment_row_version(environment) -> str:
    """The version of an environment row; `derived_status` must already be attached."""
    return _digest([
        [getattr(environment, name, None) for name in _ENVIRONMENT_FIELDS],
        [[getattr(child, name) for name in _CHILD_FIELDS] for child in environment.bookings],
    ])


def list_key(row) -> str:
    """The row's position in its list, in the Load more cursor's wire form."""
    return encode_cursor(KeysetCursor(created_at=row.created_at, id=row.id))


def live_class(status) -> str | None:
    """`inflight`, `settled`, or None for a final (RELEASED) row."""
    value = status.value if isinstance(status, BookingStatus) else status
    if value == BookingStatus.RELEASED.value:
        return None
    return "settled" if value in _SETTLED else "inflight"


_VERSION_RE = re.compile(rf"[0-9a-f]{{{VERSION_LENGTH}}}")


class InvalidReconcileRequestError(ValueError):
    """A reconciliation request over the id cap, with a duplicate id or a malformed value."""


@dataclass(frozen=True)
class ReconcileRequest:
    """A parsed reconciliation request: the displayed rows' versions, in request order, and the
    newest displayed row's key (None when the section shows no row)."""
    versions: dict[UUID, str]
    newest: KeysetCursor | None


def parse_reconcile_request(rows: list[str], newest: str | None, *, max_ids: int) -> ReconcileRequest:
    """Validate a reconciliation request before anything is read (#497 D2).

    `rows` are `<uuid>.<version>` tokens. Over `max_ids` of them, a repeated id, a malformed id,
    version or newest key: the whole request is rejected — never truncated, so a client can't
    widen the server's work and a buggy one fails loudly.
    """
    if len(rows) > max_ids:
        raise InvalidReconcileRequestError(f"at most {max_ids} rows per request")
    versions: dict[UUID, str] = {}
    for token in rows:
        raw_id, sep, version = token.partition(".")
        if not sep or not _VERSION_RE.fullmatch(version):
            raise InvalidReconcileRequestError("malformed row version")
        try:
            row_id = UUID(raw_id)
        except ValueError:
            raise InvalidReconcileRequestError("malformed row id")
        if row_id in versions:
            raise InvalidReconcileRequestError("duplicate row id")
        versions[row_id] = version
    key = None
    if newest is not None:
        try:
            key = decode_cursor(newest)
        except InvalidCursorError as exc:
            raise InvalidReconcileRequestError(str(exc))
    return ReconcileRequest(versions=versions, newest=key)


def has_newer(probe: KeysetCursor | None, newest_displayed: KeysetCursor | None) -> bool:
    """Whether the page's newest matching row is newer than the newest one displayed (#497 D8).

    Keys compare in list order, (created_at, id) — the same order the list and its cursor use.
    """
    if probe is None:
        return False
    if newest_displayed is None:
        return True
    return (probe.created_at, probe.id) > (newest_displayed.created_at, newest_displayed.id)
