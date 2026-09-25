"""Opaque wire form of the environments-page keyset cursor (#467).

`base64url("<created_at isoformat>|<uuid>")`, unpadded. Not signed: a cursor only picks a starting
position, and visibility and filters are re-applied server-side on every request, so a crafted
cursor can't show anything the user couldn't already see.
"""
import base64
import binascii
from datetime import datetime
from uuid import UUID

from app.domain.pagination import KeysetCursor

_SEP = "|"


class InvalidCursorError(ValueError):
    """The cursor is missing or doesn't decode to a timezone-aware timestamp and a UUID."""


def encode_cursor(cursor: KeysetCursor) -> str:
    raw = f"{cursor.created_at.isoformat()}{_SEP}{cursor.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str | None) -> KeysetCursor:
    if not token:
        raise InvalidCursorError("missing cursor")
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        created_at_s, id_s = raw.split(_SEP)
        created_at = datetime.fromisoformat(created_at_s)
        env_id = UUID(id_s)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursorError("malformed cursor") from exc
    if created_at.tzinfo is None:
        raise InvalidCursorError("cursor timestamp has no timezone")
    return KeysetCursor(created_at=created_at, id=env_id)
