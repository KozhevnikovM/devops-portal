"""Opaque wire form of the environments-page keyset cursor (#467).

`base64url("<created_at isoformat>|<uuid>")`, unpadded. Not signed: a cursor only picks a starting
position, and visibility and filters are re-applied server-side on every request, so a crafted
cursor can't show anything the user couldn't already see.
"""
import base64
import binascii
import re
from datetime import datetime
from uuid import UUID

from app.domain.pagination import KeysetCursor

_SEP = "|"
# Unpadded base64url alphabet only — anything else (junk, whitespace, "=", "+", "/") is malformed.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")


class InvalidCursorError(ValueError):
    """The cursor is missing or doesn't decode to a timezone-aware timestamp and a UUID."""


def encode_cursor(cursor: KeysetCursor) -> str:
    raw = f"{cursor.created_at.isoformat()}{_SEP}{cursor.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str | None) -> KeysetCursor:
    if not token:
        raise InvalidCursorError("missing cursor")
    # Strict: urlsafe_b64decode would silently drop non-alphabet characters, so a valid token
    # with junk appended would still decode (#475 review).
    if not _TOKEN_RE.fullmatch(token):
        raise InvalidCursorError("malformed cursor")
    try:
        raw_bytes = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        # Canonical form only: rejects encodings with non-zero trailing bits, so one cursor has
        # exactly one valid spelling.
        if base64.urlsafe_b64encode(raw_bytes).decode().rstrip("=") != token:
            raise ValueError("non-canonical encoding")
        raw = raw_bytes.decode()
        created_at_s, id_s = raw.split(_SEP)
        created_at = datetime.fromisoformat(created_at_s)
        env_id = UUID(id_s)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursorError("malformed cursor") from exc
    if created_at.tzinfo is None:
        raise InvalidCursorError("cursor timestamp has no timezone")
    return KeysetCursor(created_at=created_at, id=env_id)
