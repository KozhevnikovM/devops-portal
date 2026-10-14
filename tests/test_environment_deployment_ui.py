"""Read-only deployment cells, exact escaped links and reconciliation parity (#535)."""

import re
from dataclasses import replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.domain.entities import Environment
from app.presentation.reconcile import environment_row_version
from app.presentation.templating import templates


@pytest.fixture
def deployment_environment():
    now = datetime(2026, 10, 14, 20, 15, tzinfo=UTC)
    environment = Environment(
        id=uuid4(),
        name="dev <blue>",
        blueprint_name=None,
        user_id=str(uuid4()),
        ttl_minutes=240,
        expires_at=now,
        created_at=now,
        deployment_ref='<img src=x onerror="window.injected=true">' + "branch/" * 60,
        deployment_commit="a" * 40,
        deployment_status="SUCCESS",
        deployment_frontend_url="https://example.test:443/front/path?q=a&next=b#frag",
        deployment_admin_url="https://example.test/admin?mode=1&view=all#settings",
        deployment_updated_at=now,
    )
    environment.derived_status = "READY"
    return environment


def render_deployment(environment):
    return templates.env.get_template("partials/environment_deployment.html").render(
        environment=environment,
        current_user=SimpleNamespace(timezone="Europe/Moscow"),
    )


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def find(self, tag):
        return [attrs for name, attrs in self.tags if name == tag]


def test_report_escaped_read_only_exact_links_and_timezone(deployment_environment):
    html = render_deployment(deployment_environment)
    tags = Tags(html)
    assert not tags.find("img")
    assert not tags.find("input") and not tags.find("form")
    button = tags.find("button")[0]
    assert button["aria-expanded"] == "false"
    assert button["aria-controls"] == f"deployment-panel-{deployment_environment.id}"
    assert "aaaaaaa" in html
    assert "23:15" in html
    assert "2026-10-14 23:15 (MSK)" in html
    for anchor in tags.find("a"):
        assert anchor["href"] in {
            deployment_environment.deployment_frontend_url,
            deployment_environment.deployment_admin_url,
        }
        assert anchor["target"] == "_blank"
        assert set(anchor["rel"].split()) == {"noopener", "noreferrer"}
        assert deployment_environment.name in anchor["aria-label"]
    assert any(
        "truncate" in span.get("class", "").split() for span in tags.find("span")
    )
    assert "status-SUCCESS" not in html


def test_empty_and_released_reports(deployment_environment):
    environment = replace(deployment_environment, deployment_ref=None)
    html = render_deployment(environment)
    assert "No deployment information" in html
    tags = Tags(html)
    assert not tags.find("a") and not tags.find("button")
    deployment_environment.derived_status = "RELEASED"
    assert "Last report (released environment)" in render_deployment(
        deployment_environment
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("deployment_ref", "new/ref"),
        ("deployment_commit", "b" * 64),
        ("deployment_status", "FAILED"),
        ("deployment_frontend_url", "https://example.test/front2"),
        ("deployment_admin_url", "https://example.test/admin2"),
        ("deployment_updated_at", datetime(2026, 10, 15, tzinfo=UTC)),
    ],
)
def test_every_report_field_changes_version(deployment_environment, field, value):
    before = environment_row_version(deployment_environment)
    setattr(deployment_environment, field, value)
    assert environment_row_version(deployment_environment) != before


def test_deployment_column_and_seven_column_spans(deployment_environment):
    from app.presentation.routes.environments import new_rows_context

    html = templates.env.get_template("partials/environment_list_section.html").render(
        environments=[deployment_environment],
        current_user=SimpleNamespace(
            username="owner", role="user", id=uuid4(), timezone="UTC"
        ),
        active_filter="mine",
        load_more_url="/environments/rows?cursor=next",
        **new_rows_context(filter="mine", show_released=False, label=None, show=True),
    )
    assert re.findall(r"<th[^>]*>([^<]+)</th>", html) == [
        "Name",
        "Status",
        "Resources",
        "Deployment",
        "Owner",
        "Expires",
        "Actions",
    ]
    row = html.split(f'id="environment-{deployment_environment.id}"', 1)[1].split(
        "</tr>", 1
    )[0]
    assert row.count("<td ") == 7
    assert re.findall(r'colspan="(\d+)"', html) == ["7", "7"]


def test_reconciliation_over_limit_placeholder_spans_seven_columns(
    deployment_environment,
):
    from app.presentation.routes.environments import new_rows_context

    html = templates.env.get_template("partials/environment_reconcile.html").render(
        changed=[],
        over_limit=[deployment_environment],
        removed=[],
        **new_rows_context(filter="mine", show_released=False, label=None),
    )
    # Name is its own cell, the warning covers all six remaining columns.
    assert re.findall(r'colspan="(\d+)"', html) == ["6"]
    assert len(Tags(html).find("td")) == 2
