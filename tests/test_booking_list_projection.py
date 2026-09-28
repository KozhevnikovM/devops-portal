"""Booking list projection (#477): the bulk list reads select a fixed, lightweight column set.

Guards against detail-only payloads (provisioning log, startup script, extra-vars, full role
dicts) and credential values (#478: VM password, static-VM password / SSH key) creeping back into
the list SQL, and pins the shared row-partial contract that both
BookingListItem and the full Booking satisfy.
"""
import dataclasses
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.domain.booking_list import BookingListItem
from app.domain.entities import Booking
from app.domain.enums import BookingStatus, ResourceType
from app.infrastructure.database.models import BookingModel, StaticVMModel
from app.infrastructure.repositories.booking_repo import (
    BookingRepository,
    _list_item_stmt,
)

FORBIDDEN = {
    "provisioning_log", "startup_script", "extra_vars", "config_roles",
    # #478: raw credential values — only has_credentials is projected.
    "vm_password", "static_vm_password", "static_vm_ssh_key",
}

# The columns holding raw secrets; the list statement may evaluate them (has_credentials) but must
# never select one as a result column.
# Compared as (table, column) names: the statement's labels wrap *annotated* column copies, so an
# identity check against the table columns would never match.
SECRET_COLUMNS = {
    (BookingModel.__tablename__, "vm_password"),
    (StaticVMModel.__tablename__, "password"),
    (StaticVMModel.__tablename__, "ssh_key"),
}

EXPECTED_FIELDS = {
    "id", "user_id", "status", "resource_type", "ttl_minutes", "expires_at", "created_at",
    "label", "status_message", "config_failed", "environment_id",
    "owner_username", "created_by", "created_by_username",
    "image_id", "image_name", "hw_config_id", "hw_config_name", "vm_ip",
    "namespace_name", "cluster_name", "api_url",
    "static_vm_name", "static_vm_host", "static_vm_username",
    "has_provisioning_log", "config_role_names", "has_credentials",
    "queue_position",
}

_BOOKING_COLUMNS = {c.name for c in BookingModel.__table__.columns}


def _item_fields() -> set[str]:
    return {f.name for f in dataclasses.fields(BookingListItem)}


def _booking(**kw) -> Booking:
    now = datetime.now(timezone.utc)
    return Booking(
        id=uuid4(), user_id="u1", status=BookingStatus.READY, ttl_minutes=60,
        expires_at=now, created_at=now, **kw,
    )


# ── 1.1 projection shape ────────────────────────────────────────────────────────

def test_list_item_field_set_is_pinned():
    assert _item_fields() == EXPECTED_FIELDS
    assert not _item_fields() & FORBIDDEN


def test_list_item_attributes_are_a_subset_of_booking():
    """The row partial reads the same names off either type (design D2)."""
    booking = _booking()
    missing = [name for name in EXPECTED_FIELDS if not hasattr(booking, name)]
    assert missing == []


# ── 1.2 Booking's derived properties ────────────────────────────────────────────

@pytest.mark.parametrize("log,expected", [(None, False), ("", False), ("apply started\n", True)])
def test_booking_has_provisioning_log(log, expected):
    assert _booking(provisioning_log=log).has_provisioning_log is expected


def test_booking_config_role_names_empty():
    assert _booking().config_role_names == ()


def test_booking_config_role_names_in_order():
    roles = [{"name": "nginx", "vars": {"a": 1}}, {"name": "docker", "secret_vars": {"s": "x"}}]
    assert _booking(config_roles=roles).config_role_names == ("nginx", "docker")


@pytest.mark.parametrize("kw,expected", [
    ({"vm_password": "s3cret"}, True),
    ({"vm_password": None}, False),
    ({"vm_password": ""}, False),
    ({"resource_type": ResourceType.STATIC_VM, "static_vm_ssh_key": "ssh-ed25519 AAAA"}, True),
    ({"resource_type": ResourceType.STATIC_VM, "static_vm_username": "root"}, True),
    ({"resource_type": ResourceType.STATIC_VM}, False),
    ({"resource_type": ResourceType.NAMESPACE, "namespace_name": "ns-1"}, False),
])
def test_booking_has_credentials(kw, expected):
    assert _booking(**kw).has_credentials is expected


# ── 4.1 guard: the list statement selects exactly the projection ────────────────

def test_statement_labels_equal_projection_fields():
    labels = {c.name for c in _list_item_stmt().selected_columns}
    assert labels == _item_fields() - {"queue_position"}
    assert not labels & FORBIDDEN


def _selected_booking_columns(stmt) -> set[str]:
    """Plain bookings columns the statement selects (unwrapping labels). Derived expressions such
    as ``octet_length(provisioning_log)`` are not plain columns and are not reported."""
    names = set()
    for col in stmt.selected_columns:
        inner = getattr(col, "element", col)
        if getattr(inner, "table", None) is BookingModel.__table__:
            names.add(inner.name)
    return names


async def _captured_stmt(method, *args):
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    await method(session, *args)
    return session.execute.call_args.args[0]


async def _captured_list_stmt(call: str):
    """The statement a bulk list read uses to fetch the listed bookings' fields. For list_page
    (#479) that is phase 2 — the projection for the page's ids — so phase 1 returns one key. With
    a label (#485) phase 1 is the scan window, whose rows also carry the window-end flag."""
    repo = BookingRepository()
    if not call.startswith("list_page"):
        args = ("uid-1",) if call == "list_by_user" else ()
        return await _captured_stmt(getattr(repo, call), *args)
    pin, keys, unpin, items = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    pin.one.return_value = SimpleNamespace(
        _mapping={"enable_bitmapscan": "on", "enable_seqscan": "on", "enable_sort": "on",
                  "enable_indexscan": "on", "jit": "on"}
    )
    keys.all.return_value = [
        SimpleNamespace(created_at=datetime.now(timezone.utc), id=uuid4(), is_window_end=False)
    ]
    items.all.return_value = []
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[pin, keys, unpin, items])
    await repo.list_page(
        session, user_id="uid-1", resource_types=["VM"],
        label="db" if call == "list_page_label" else None, include_released=False,
        limit=50, scan_size=200, after=None,
    )
    return session.execute.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["list_all", "list_by_user", "list_page", "list_page_label"])
async def test_list_reads_never_select_detail_only_columns(call):
    stmt = await _captured_list_stmt(call)

    selected = _selected_booking_columns(stmt)
    assert selected, "expected the statement to select bookings columns"
    assert selected <= _BOOKING_COLUMNS
    assert not selected & FORBIDDEN
    # Not the ORM entity: every selection is a single labelled expression.
    assert all(d["expr"] is not BookingModel for d in stmt.column_descriptions)
    assert {c.name for c in stmt.selected_columns} == _item_fields() - {"queue_position"}


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["list_all", "list_by_user", "list_page", "list_page_label"])
async def test_list_reads_never_select_a_raw_secret_column(call):
    """#478: secrets may be evaluated inside has_credentials, but no result column *is* one."""
    stmt = await _captured_list_stmt(call)

    for col in stmt.selected_columns:
        inner = getattr(col, "element", col)
        table = getattr(inner, "table", None)
        key = (getattr(table, "name", None), getattr(inner, "name", None))
        assert key not in SECRET_COLUMNS, f"{col.name} selects raw secret column {key}"


def test_label_scan_window_selects_only_keys_and_the_window_end_flag():
    """#485: the label window joins bookings to test labels, but returns nothing but keys."""
    from app.infrastructure.repositories.booking_repo import _label_page_keys_stmt

    stmt = _label_page_keys_stmt(
        "uid-1", resource_types=["VM"], label="db", include_released=False, limit=50,
        scan_size=200, after=None,
    )
    assert [c.name for c in stmt.selected_columns] == ["created_at", "id", "is_window_end"]


# ── mapper ──────────────────────────────────────────────────────────────────────

class _Row:
    def __init__(self, mapping):
        self._mapping = mapping


def _row(**overrides):
    now = datetime.now(timezone.utc)
    base = {name: None for name in _item_fields() - {"queue_position"}}
    base.update(
        id=uuid4(), user_id="u1", status="READY", resource_type="VM", ttl_minutes=60,
        expires_at=now, created_at=now, config_failed=False,
        has_provisioning_log=True, config_role_names=["nginx", "docker"], has_credentials=False,
    )
    base.update(overrides)
    return _Row(base)


def test_mapper_builds_list_item_with_enums():
    from app.domain.enums import ResourceType
    from app.infrastructure.repositories.booking_repo import _to_list_item

    item = _to_list_item(_row())
    assert isinstance(item, BookingListItem)
    assert item.status is BookingStatus.READY
    assert item.resource_type is ResourceType.VM
    assert item.config_role_names == ("nginx", "docker")
    assert item.queue_position is None


def test_mapper_rejects_unknown_status():
    from app.infrastructure.repositories.booking_repo import _to_list_item

    with pytest.raises(ValueError, match="unrecognised status"):
        _to_list_item(_row(status="BOGUS"))
