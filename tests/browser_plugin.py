"""Pytest plugin for the real-browser (Playwright/Chromium) tests (#542), registered in conftest.py.

- `browser`-marked tests run only when the `-m` expression names `browser`
  (`pytest -m browser`), so the canonical fast suite (`pytest tests/ -m "not integration"`)
  deselects them rather than reporting them as skipped.
- With `REQUIRE_BROWSER_TESTS=1` (the `browser-tests` CI job), a selected browser test that
  skips — Playwright missing, Chromium failing to launch, static assets not built — fails
  instead, so the check can't pass without running the browser.
"""

import os

import pytest


def browser_tests_required() -> bool:
    return os.environ.get("REQUIRE_BROWSER_TESTS") == "1"


def pytest_collection_modifyitems(config, items):
    if "browser" in (config.option.markexpr or ""):
        return
    deselected = [item for item in items if item.get_closest_marker("browser")]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if not item.get_closest_marker("browser")]


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if (
        report.skipped
        and browser_tests_required()
        and item.get_closest_marker("browser")
    ):
        report.outcome = "failed"
        report.longrepr = (
            f"browser test skipped with REQUIRE_BROWSER_TESTS=1: {report.longrepr}"
        )
    return report
