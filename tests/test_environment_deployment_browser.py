"""Optional real Chromium checks: pip install playwright && playwright install chromium.

These exercise the actual controller with server-rendered, escaped report markup.
"""

import os
from pathlib import Path

import pytest

from tests.test_environment_deployment_ui import (
    deployment_environment,  # noqa: F401 -- shared fixture
    render_deployment,
)


@pytest.fixture
def deployment_page(deployment_environment):  # noqa: F811 -- imported pytest fixture
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(
                headless=True,
                args=["--no-sandbox"],
                executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"),
            )
        except playwright.Error as exc:
            pytest.skip(f"Chromium is unavailable: {exc}")
        page = browser.new_page(viewport={"width": 360, "height": 500})
        # Real table overflow is essential: an in-cell absolute panel would be clipped.
        page.set_content(
            '<body><input id="environments-label-filter">'
            '<section id="environments-section"><div style="overflow:auto;width:330px">'
            '<table style="width:900px"><tbody><tr id="report-row">'
            + render_deployment(deployment_environment)
            + "</tr></tbody></table></div></section></body>"
        )
        stylesheet = Path("app/static/css/tailwind.css")
        if stylesheet.exists():
            page.add_style_tag(path=str(stylesheet.resolve()))
        page.set_default_timeout(5000)
        page.add_script_tag(
            path=str(Path("frontend/js/environment_deployment.js").resolve())
        )
        yield page
        browser.close()


def test_keyboard_clamping_escaping_links_and_escape(deployment_page):
    page = deployment_page
    trigger = page.locator("[data-deployment-trigger]")
    trigger.focus()
    page.keyboard.press("Enter")
    panel = page.locator("[data-deployment-overlay-host] > div")
    panel.wait_for()
    assert trigger.get_attribute("aria-expanded") == "true"
    assert panel.evaluate("(e) => e === document.activeElement")
    page.keyboard.press("Tab")
    assert panel.locator("a").first.evaluate("(e) => e === document.activeElement")
    assert panel.locator("img").count() == 0
    assert page.evaluate("window.injected === undefined")
    bounds = panel.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 360
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 500
    assert page.locator("body > [data-deployment-overlay-host]").count() == 1
    # Scroll the actual overflowing table and resize while keeping the detached panel bounded.
    page.locator("#environments-section > div").evaluate("(e) => e.scrollLeft = 200")
    page.set_viewport_size({"width": 320, "height": 400})
    page.wait_for_timeout(30)
    bounds = panel.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 320
    assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 400
    page.keyboard.press("Escape")
    assert page.locator("[data-deployment-overlay-host]").count() == 0
    assert trigger.evaluate("(e) => e === document.activeElement")
    page.keyboard.press("Space")
    assert page.locator("[data-deployment-overlay-host]").count() == 1
    page.mouse.click(2, 2)  # viewport gutter stays outside the clamped panel
    assert trigger.get_attribute("aria-expanded") == "false"


def test_repeated_replacements_removal_and_unchanged_rows(deployment_page):
    page = deployment_page
    for attempt in range(12):
        trigger = page.locator("[data-deployment-trigger]")
        trigger.click()
        assert page.locator("[data-deployment-overlay-host]").count() == 1
        # An unrelated/no-op reconciliation update must preserve an open disclosure.
        page.evaluate(
            "document.getElementById('environments-label-filter').setAttribute('data-tick','1')"
        )
        assert page.locator("[data-deployment-overlay-host]").count() == 1
        page.evaluate(
            """(attempt) => {
            const row = document.getElementById('report-row');
            const clone = row.cloneNode(true);
            clone.querySelector('[data-deployment-template]').content.querySelector('dd').textContent = 'updated-' + attempt;
            document.dispatchEvent(new CustomEvent('htmx:beforeCleanupElement', {detail:{elt:row}}));
            row.replaceWith(clone);
            document.dispatchEvent(new CustomEvent('htmx:afterSwap'));
        }""",
            attempt,
        )
        assert page.locator("[data-deployment-overlay-host]").count() == 0
        assert page.locator("[data-deployment-trigger]").evaluate(
            "(e) => e === document.activeElement"
        )
        page.keyboard.press("Enter")
        assert (
            page.locator("[data-deployment-overlay-host] dd").first.inner_text()
            == f"updated-{attempt}"
        )
        page.keyboard.press("Escape")
    # Native/SSE removal without HTMX cleanup also disposes, then restores stable control focus.
    page.locator("[data-deployment-trigger]").click()
    page.evaluate("document.getElementById('report-row').remove()")
    page.wait_for_function("!document.querySelector('[data-deployment-overlay-host]')")
    assert page.locator("#environments-label-filter").evaluate(
        "(e) => e === document.activeElement"
    )


def test_appended_rows_links_and_section_replacement(deployment_page):
    page = deployment_page
    # Activating a protected link leaves the disclosure alone (link is a sibling, never its child).
    page.route("https://example.test/**", lambda route: route.fulfill(body="service"))
    link = page.locator("#report-row > td > div > a").first
    with page.expect_popup() as popup:
        link.click()
    assert popup.value.evaluate("window.opener === null")
    popup.value.close()
    assert (
        page.locator("[data-deployment-trigger]").get_attribute("aria-expanded")
        == "false"
    )
    page.locator("[data-deployment-trigger]").click()
    # Keyboard activation also remains independent when the panel overlaps its cell.
    link.focus()
    with page.expect_popup() as popup:
        page.keyboard.press("Enter")
    popup.value.close()
    assert (
        page.locator("[data-deployment-trigger]").get_attribute("aria-expanded")
        == "true"
    )
    page.keyboard.press("Escape")
    page.evaluate("""() => {
        const original = document.getElementById('report-row');
        const clone = original.cloneNode(true);
        clone.id = 'appended-row';
        clone.querySelector('button').id = 'appended-trigger';
        original.after(clone);
    }""")
    page.locator("#appended-row button").click()
    assert page.locator("[data-deployment-overlay-host]").count() == 1
    page.evaluate(
        "document.getElementById('environments-section').outerHTML = '<section id=\"environments-section\"><input id=\"new-filter\"></section>'"
    )
    page.wait_for_function("!document.querySelector('[data-deployment-overlay-host]')")
    assert page.locator("#environments-label-filter").evaluate(
        "(e) => e === document.activeElement"
    )
