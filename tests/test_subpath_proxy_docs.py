"""The documented /dp subpath proxy (admin guide, Option B) rewrites every root-absolute URL (#494).

Option B strips `/dp` before proxying and adds it back only where nginx `sub_filter` rules match the
HTML. A root-absolute URL with no matching rule escapes the prefix, and the browser requests it
outside `/dp`. These tests read the rules from the guide and check them against every root-absolute
URL attribute in the templates, and in the rendered list pages, whose filter, Load more and row URLs
are built from variables.
"""
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_ROOT = Path(__file__).parent.parent
_GUIDE = _ROOT / "docs" / "admin-guide.md"
_TEMPLATES = _ROOT / "app" / "presentation" / "templates"

# A root-absolute URL in an HTML attribute: attr="/path..." (not protocol-relative "//").
_ATTR_URL = re.compile(r'([\w-]+)="(/(?!/)[^"]*)"')


def _option_b_rules() -> list[str]:
    """The `sub_filter '<from>'` patterns of the Option B `location /dp/` block."""
    guide = _GUIDE.read_text()
    start = guide.index("### Option B")
    block = guide[start:guide.index("#### Alternative: forward the prefix", start)]
    rules = re.findall(r"sub_filter\s+(['\"])(.+?)\1\s", block)
    return [pattern for _, pattern in rules]


def _uncovered(html: str, rules: list[str]) -> set[str]:
    missing = set()
    for attr, url in _ATTR_URL.findall(html):
        with_attr, bare = f'{attr}="{url}"', f'="{url}"'
        if not any(with_attr.startswith(r) or bare.startswith(r) for r in rules):
            missing.add(with_attr)
    return missing


def test_guide_rules_are_found():
    rules = _option_b_rules()
    assert '="/book' in rules and '="/environments' in rules


@pytest.mark.parametrize("template", sorted(_TEMPLATES.rglob("*.html")),
                         ids=lambda p: str(p.relative_to(_TEMPLATES)))
def test_template_urls_are_rewritten_under_the_subpath(template):
    assert _uncovered(template.read_text(), _option_b_rules()) == set()


@pytest.mark.parametrize("path", ["/", "/book/vm/list", "/book/namespace/list",
                                  "/environments", "/environments/list"])
def test_rendered_list_urls_are_rewritten_under_the_subpath(path):
    """Filter-control, Load more and row URLs come from variables, so check them rendered."""
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from app.domain.entities import Environment, User
    from app.domain.pagination import EnvironmentPage, KeysetCursor, KeysetPage
    from app.infrastructure.auth import require_user
    from app.infrastructure.database.session import get_async_session
    from app.main import app

    user = User(id=uuid4(), username="me", password_hash="", role="admin", is_active=True,
                created_at=datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)
    cursor = KeysetCursor(created_at=now, id=uuid4())
    env = Environment(id=uuid4(), name="dev", blueprint_name="dev", user_id=str(user.id),
                      ttl_minutes=60, expires_at=now + timedelta(hours=1), created_at=now,
                      bookings=[], created_by=None, owner_username="me")
    app.dependency_overrides[get_async_session] = lambda: AsyncMock()
    app.dependency_overrides[require_user] = lambda: user
    try:
        with (
            patch("app.presentation.routes.bookings._repo") as repo,
            patch("app.presentation.routes.bookings._image_repo") as img,
            patch("app.presentation.routes.bookings._hw_config_repo") as hw,
            patch("app.presentation.routes.bookings._namespace_repo") as ns,
            patch("app.presentation.routes.bookings._static_vm_repo") as svm,
            patch("app.presentation.routes.bookings._role_repo") as role,
            patch("app.presentation.routes.environments._env_repo") as env_repo,
            patch("app.presentation.routes.environments._blueprint_repo") as bp,
            patch("app.presentation.routes.environments._namespace_repo") as env_ns,
        ):
            repo.list_page = AsyncMock(return_value=KeysetPage(next_cursor=cursor))
            env_repo.list_page = AsyncMock(return_value=EnvironmentPage(items=[env], next_cursor=cursor))
            for mock in (img.list_active, hw.list_active, ns.list_available,
                         svm.list_available, role.list_active, bp.list_active,
                         env_ns.list_available, env_ns.list_held_standalone_by_user):
                mock.side_effect = AsyncMock(return_value=[])
            html = TestClient(app).get(f"{path}?filter=all&label=x").text
    finally:
        app.dependency_overrides.clear()

    assert "-load-more" in html  # the Load more URL is part of what is checked
    assert _uncovered(html, _option_b_rules()) == set()
