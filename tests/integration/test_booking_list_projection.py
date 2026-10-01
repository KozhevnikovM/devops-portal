"""Integration: the booking list projection (#477) on real PostgreSQL.

The list reads derive `has_provisioning_log`, the ordered `config_role_names` and (#478)
`has_credentials` in SQL — never returning a raw credential value — and a row
rendered from the list is semantically equal to the same booking's `GET /bookings/{id}/row`
rendering (spec: "List row matches the refreshed row"). Byte equality is not the contract — the
list passes `is_first_row`, which only moves the first row's action menu — so rows are compared
through a summary of their visible text and actions, ignoring `class`.
"""
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import insert

from app.domain.entities import User
from app.domain.pagination import KeysetCursor
from app.infrastructure.auth import require_user
from app.infrastructure.database.models import (
    BookingModel,
    NamespaceModel,
    StaticVMModel,
    UserModel,
)
from app.infrastructure.database.session import get_async_session
from app.infrastructure.repositories.booking_repo import BookingRepository
from app.main import app
from app.presentation.pagination import encode_cursor

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

_repo = BookingRepository()
_ACTION_ATTRS = ("href", "hx-get", "hx-post", "hx-put", "hx-patch", "hx-delete", "hx-confirm",
                 "sse-swap", "hx-trigger")
_ROLES = [
    {"name": "nginx", "ansible_role": "nginx", "vars": {"port": 8080}, "secret_vars": {}},
    {"name": "docker", "ansible_role": "docker", "vars": {}, "secret_vars": {"token": "gAAAA-cipher"}},
]


# ── Semantic row summary ────────────────────────────────────────────────────────

class _RowSummary(HTMLParser):
    """Reduce `<tr id="booking-<id>">` to (visible text, actions, input values)."""

    def __init__(self, row_id: str):
        super().__init__(convert_charrefs=True)
        self._row_id = row_id
        self._depth = 0          # nesting depth inside the target <tr>; 0 = outside
        self._open: list[list] = []  # open action elements: [tag, attrs, text-parts, depth]
        self.text: list[str] = []
        self.actions: list[tuple] = []
        self.inputs: list[tuple] = []

    _VOID = frozenset({"input", "br", "img", "meta", "link", "hr"})

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self._depth == 0:
            if tag == "tr" and attrs.get("id") == self._row_id:
                self._depth = 1
                self._maybe_open(tag, attrs)
            return
        if tag in self._VOID:
            if tag == "input":
                self.inputs.append((attrs.get("name"), attrs.get("value")))
            return
        self._depth += 1
        self._maybe_open(tag, attrs)

    def _maybe_open(self, tag, attrs):
        picked = tuple((k, attrs[k]) for k in _ACTION_ATTRS if k in attrs)
        if picked:
            self._open.append([tag, picked, [], self._depth])

    def handle_endtag(self, tag):
        if self._depth == 0 or tag in self._VOID:
            return
        while self._open and self._open[-1][3] == self._depth:
            t, picked, parts, _ = self._open.pop()
            self.actions.append((t, picked, _norm("".join(parts))))
        self._depth -= 1

    def handle_data(self, data):
        if self._depth:
            self.text.append(data)
            for element in self._open:
                element[2].append(data)

    def summary(self) -> tuple:
        assert self.text, f"row {self._row_id} not found"
        return _norm("".join(self.text)), tuple(sorted(self.actions)), tuple(self.inputs)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _summarize(html: str, booking_id: UUID) -> tuple:
    parser = _RowSummary(f"booking-{booking_id}")
    parser.feed(html)
    return parser.summary()


# ── Fixtures ────────────────────────────────────────────────────────────────────

def _domain_user(m: UserModel) -> User:
    return User(id=m.id, username=m.username, password_hash="", role=m.role, is_active=True,
                created_at=datetime.now(timezone.utc))


@pytest_asyncio.fixture(loop_scope="session")
async def seeded(async_session, seed_catalog):
    token = f"proj-{uuid4().hex[:10]}"
    owner = UserModel(id=uuid4(), username=f"{token}-owner", password_hash="x", role="user")
    admin = UserModel(id=uuid4(), username=f"{token}-admin", password_hash="x", role="admin")
    other = UserModel(id=uuid4(), username=f"{token}-other", password_hash="x", role="user")
    ns = NamespaceModel(id=uuid4(), name=f"{token}-ns", cluster_name="c1",
                        api_url="https://k8s.example:6443", is_active=True,
                        created_at=datetime.now(timezone.utc))
    svm = StaticVMModel(id=uuid4(), name=f"{token}-svm", host="10.9.9.9", username="root",
                        password="svm-pw", ssh_key="ssh-ed25519 AAAA", is_active=True,
                        created_at=datetime.now(timezone.utc))
    svm_key_only = StaticVMModel(id=uuid4(), name=f"{token}-svm-key", host="10.9.9.10",
                                 username="", password=None, ssh_key="ssh-ed25519 KEYONLY",
                                 is_active=True, created_at=datetime.now(timezone.utc))
    async_session.add_all([owner, admin, other, ns, svm, svm_key_only])
    await async_session.flush()

    # Far-future creation times so the seeded rows sort ahead of anything else in the database.
    base = datetime(2999, 6, 1, tzinfo=timezone.utc)
    expires = base + timedelta(hours=4)
    common = {"user_id": str(owner.id), "ttl_minutes": 240, "expires_at": expires, "label": token}
    rows = {
        "vm_log": dict(common, resource_type="VM", status="READY", image_id=seed_catalog["image_id"],
                       image_name="ubuntu", hw_config_id=seed_catalog["hw_id"], hw_config_name="small",
                       vm_ip="10.0.0.5", vm_password="vm-pw", provisioning_log="x" * 60_000,
                       startup_script="#!/bin/sh\necho hi", extra_vars={"k": "v"},
                       config_roles=_ROLES, status_message="Ready", created_at=base),
        "vm_empty_log": dict(common, resource_type="VM", status="FAILED", image_name="ubuntu",
                             hw_config_name="small", provisioning_log="", config_failed=True,
                             created_at=base - timedelta(seconds=1)),
        "static_vm": dict(common, resource_type="STATIC_VM", status="READY", static_vm_id=svm.id,
                          created_at=base - timedelta(seconds=2)),
        "namespace": dict(common, resource_type="NAMESPACE", status="READY", namespace_id=ns.id,
                          created_at=base - timedelta(seconds=3)),
        "ns_queued": dict(common, resource_type="NAMESPACE", status="QUEUED",
                          created_at=base - timedelta(seconds=4)),
        "svm_key_only": dict(common, resource_type="STATIC_VM", status="READY",
                             static_vm_id=svm_key_only.id, created_at=base - timedelta(seconds=5)),
    }
    ids = {}
    for key, values in rows.items():
        ids[key] = uuid4()
        await async_session.execute(insert(BookingModel).values(id=ids[key], **values))
    await async_session.flush()
    return {"token": token, "ids": ids, "owner": _domain_user(owner),
            "admin": _domain_user(admin), "other": _domain_user(other)}


def _client(session, user: User) -> httpx.AsyncClient:
    async def _session():
        yield session

    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[require_user] = lambda: user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


# ── Repository: derived columns ─────────────────────────────────────────────────

async def test_list_derives_log_flag_and_ordered_role_names(async_session, seeded):
    items = await _repo.list_by_user(async_session, str(seeded["owner"].id),
                                     label=seeded["token"])
    by_id = {i.id: i for i in items}
    ids = seeded["ids"]

    assert [i.id for i in items] == [ids[k] for k in ("vm_log", "vm_empty_log", "static_vm",
                                                      "namespace", "ns_queued", "svm_key_only")]
    assert by_id[ids["vm_log"]].has_provisioning_log is True
    assert by_id[ids["vm_log"]].config_role_names == ("nginx", "docker")
    assert by_id[ids["vm_empty_log"]].has_provisioning_log is False
    assert by_id[ids["static_vm"]].has_provisioning_log is False  # NULL log
    assert by_id[ids["static_vm"]].config_role_names == ()
    # #478: credentials reduce to a presence flag; the raw values never reach the application.
    assert by_id[ids["vm_log"]].has_credentials is True           # VM with a password
    assert by_id[ids["vm_empty_log"]].has_credentials is False    # VM without one
    assert by_id[ids["static_vm"]].has_credentials is True
    assert by_id[ids["svm_key_only"]].has_credentials is True     # SSH key only
    assert by_id[ids["namespace"]].has_credentials is False
    for item in items:
        assert not hasattr(item, "vm_password")
        assert not hasattr(item, "static_vm_password")
        assert not hasattr(item, "static_vm_ssh_key")
    assert by_id[ids["namespace"]].api_url == "https://k8s.example:6443"


# ── Route: list row ≡ /row, per page and per viewer ─────────────────────────────

_PAGES = {
    "/book/vm": ("vm_log", "vm_empty_log", "static_vm", "svm_key_only"),
    "/book/namespace": ("namespace", "ns_queued"),
}


@pytest.mark.parametrize("viewer,list_filter", [("owner", "mine"), ("admin", "all")])
@pytest.mark.parametrize("page", list(_PAGES))
async def test_list_row_semantically_matches_refreshed_row(async_session, seeded, viewer,
                                                           list_filter, page):
    user = seeded[viewer]
    async with _client(async_session, user) as client:
        listing = await client.get(page, params={"filter": list_filter, "label": seeded["token"]})
        assert listing.status_code == 200
        for key in _PAGES[page]:
            booking_id = seeded["ids"][key]
            row = await client.get(f"/bookings/{booking_id}/row")
            assert row.status_code == 200
            assert _summarize(listing.text, booking_id) == _summarize(row.text, booking_id), key


# A cursor after every seeded booking, so a Load more fragment returns them as its page (#479).
_TOP_CURSOR = encode_cursor(KeysetCursor(datetime(9999, 1, 1, tzinfo=timezone.utc), UUID(int=2**128 - 1)))


@pytest.mark.parametrize("viewer,list_filter", [("owner", "mine"), ("admin", "all")])
@pytest.mark.parametrize("page", list(_PAGES))
async def test_load_more_row_semantically_matches_refreshed_row(async_session, seeded, viewer,
                                                                list_filter, page):
    """#479: a row appended by Load more is the same row the page and /row render."""
    user = seeded[viewer]
    async with _client(async_session, user) as client:
        fragment = await client.get(f"{page}/rows", params={
            "cursor": _TOP_CURSOR, "filter": list_filter, "label": seeded["token"],
        })
        assert fragment.status_code == 200
        for key in _PAGES[page]:
            booking_id = seeded["ids"][key]
            row = await client.get(f"/bookings/{booking_id}/row")
            assert _summarize(fragment.text, booking_id) == _summarize(row.text, booking_id), key


async def test_list_row_shows_log_link_roles_and_queue_position(async_session, seeded):
    ids = seeded["ids"]
    async with _client(async_session, seeded["owner"]) as client:
        vm_page = (await client.get("/book/vm", params={"label": seeded["token"]})).text
        ns_page = (await client.get("/book/namespace", params={"label": seeded["token"]})).text

    vm_text, vm_actions, _ = _summarize(vm_page, ids["vm_log"])
    assert ("a", (("href", f"/bookings/{ids['vm_log']}/log"),), "View full log ↗") in vm_actions
    assert "nginx" in vm_text and "docker" in vm_text
    # #478: the owner gets the on-demand control, never the value inline.
    assert ("button", (("hx-get", f"/bookings/{ids['vm_log']}/credentials"),), "Show credentials") \
        in vm_actions
    _, empty_actions, _ = _summarize(vm_page, ids["vm_empty_log"])
    assert not any("/log" in dict(a[1]).get("href", "") for a in empty_actions)
    queued_text, _, _ = _summarize(ns_page, ids["ns_queued"])
    assert re.search(r"Queued — position \d+", queued_text)


def _position(html: str, booking_id: UUID) -> str:
    text, _, _ = _summarize(html, booking_id)
    match = re.search(r"Queued — position (\S+)", text)
    assert match, (booking_id, text)
    return match.group(1)


@pytest.mark.parametrize("viewer,list_filter", [("owner", "mine"), ("admin", "all")])
async def test_queue_positions_match_between_list_row_and_label_responses(async_session, seeded,
                                                                         viewer, list_filter):
    """#495: a queued row's position is the same from the list, the row refresh and a label edit,
    across namespace and static-VM queues, with a tie, and with a queued booking of another user
    (counted in the owner's ranks but not listed on their Mine page)."""
    ids, owner, other = seeded["ids"], seeded["owner"], seeded["other"]
    base = datetime(2999, 6, 1, tzinfo=timezone.utc)
    common = {"ttl_minutes": 240, "expires_at": base + timedelta(hours=4), "label": seeded["token"]}
    extra = {
        # Same creation time as ns_queued: the two share a position.
        "ns_tied": dict(common, user_id=str(owner.id), resource_type="NAMESPACE", status="QUEUED",
                        created_at=base - timedelta(seconds=4)),
        "ns_later": dict(common, user_id=str(owner.id), resource_type="NAMESPACE", status="QUEUED",
                         created_at=base - timedelta(milliseconds=3500)),
        "svm_other": dict(common, user_id=str(other.id), resource_type="STATIC_VM",
                          status="QUEUED", created_at=base - timedelta(seconds=9)),
        "svm_queued": dict(common, user_id=str(owner.id), resource_type="STATIC_VM",
                           status="QUEUED", created_at=base - timedelta(seconds=8)),
    }
    for key, values in extra.items():
        ids[key] = uuid4()
        await async_session.execute(insert(BookingModel).values(id=ids[key], **values))
    await async_session.flush()

    rows = {"/book/namespace": ["ns_queued", "ns_tied", "ns_later"], "/book/vm": ["svm_queued"]}
    async with _client(async_session, seeded[viewer]) as client:
        for page, keys in rows.items():
            html = (await client.get(page, params={"filter": list_filter,
                                                   "label": seeded["token"]})).text
            for key in keys:
                row = (await client.get(f"/bookings/{ids[key]}/row")).text
                assert _position(html, ids[key]) == _position(row, ids[key]), (page, key)
        ns_page = (await client.get("/book/namespace", params={"filter": list_filter,
                                                               "label": seeded["token"]})).text
        vm_page = (await client.get("/book/vm", params={"filter": list_filter,
                                                        "label": seeded["token"]})).text
        relabeled = (await client.patch(f"/bookings/{ids['ns_later']}/label",
                                        data={"label": f"{seeded['token']}-renamed"})).text

    tied = _position(ns_page, ids["ns_queued"])
    assert tied != "—" and _position(ns_page, ids["ns_tied"]) == tied
    assert int(_position(ns_page, ids["ns_later"])) == int(tied) + 2
    # The other user's earlier queued static VM is ahead in the global queue for every viewer.
    assert int(_position(vm_page, ids["svm_queued"])) >= 2
    assert f"{seeded['token']}-renamed" in relabeled
    assert _position(relabeled, ids["ns_later"]) == _position(ns_page, ids["ns_later"])


_SECRETS = ("vm-pw", "svm-pw", "ssh-ed25519")


async def test_non_owner_all_list_hides_credentials(async_session, seeded):
    async with _client(async_session, seeded["other"]) as client:
        page = (await client.get("/book/vm", params={"filter": "all", "label": seeded["token"]})).text
    for key in ("vm_log", "static_vm"):
        _, actions, _ = _summarize(page, seeded["ids"][key])
        assert not any("/credentials" in dict(a[1]).get("hx-get", "") for a in actions), key
    assert not [s for s in _SECRETS if s in page]


@pytest.mark.parametrize("viewer,list_filter", [("owner", "mine"), ("admin", "all")])
async def test_bookings_page_never_embeds_credential_values(async_session, seeded, viewer,
                                                            list_filter):
    """#478: even viewers allowed to see credentials get none of them in the page HTML."""
    async with _client(async_session, seeded[viewer]) as client:
        page = (await client.get("/book/vm", params={"filter": list_filter,
                                                     "label": seeded["token"]})).text
    assert not [s for s in _SECRETS if s in page]
    assert 'hx-history="false"' not in page


async def test_owner_reveals_credentials_on_demand(async_session, seeded):
    ids = seeded["ids"]
    async with _client(async_session, seeded["owner"]) as client:
        vm = await client.get(f"/bookings/{ids['vm_log']}/credentials")
        svm = await client.get(f"/bookings/{ids['svm_key_only']}/credentials")
    assert vm.status_code == 200 and "vm-pw" in vm.text
    assert vm.headers["cache-control"] == "no-store"
    assert 'hx-history="false"' in vm.text
    assert svm.status_code == 200 and "ssh-ed25519 KEYONLY" in svm.text


# ── JSON list contract ──────────────────────────────────────────────────────────

async def test_json_list_matches_full_booking_summary(async_session, seeded):
    from app.presentation.routes.api_bookings import _summary

    async with _client(async_session, seeded["owner"]) as client:
        resp = await client.get("/api/v1/bookings", params={"label": seeded["token"]})
    assert resp.status_code == 200
    entries = {e["id"]: e for e in resp.json()}
    assert set(entries) == {str(i) for i in seeded["ids"].values()}

    for booking_id in seeded["ids"].values():
        full = await _repo.get(async_session, booking_id)
        expected = _summary(full)
        # Pre-change derivation of `roles`, independent of config_role_names.
        expected["roles"] = [r.get("name") for r in (full.config_roles or [])]
        assert entries[str(booking_id)] == expected
    vm = entries[str(seeded["ids"]["vm_log"])]
    assert vm["roles"] == ["nginx", "docker"]
    for secret in ("provisioning_log", "startup_script", "extra_vars", "vm_password", "ssh_key"):
        assert secret not in vm
