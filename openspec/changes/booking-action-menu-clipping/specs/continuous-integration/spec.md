## ADDED Requirements

### Requirement: Every pull request runs the browser regression suite

Every pull request targeting `main` SHALL produce a CI check named `browser-tests` that installs a pinned Playwright release and its Chromium browser and runs the repository's browser regression tests. The check SHALL fail when a browser test fails or errors, and SHALL also fail when any selected browser test is skipped, including because Playwright or Chromium is unavailable. The `fast-tests` check SHALL NOT be made to depend on Playwright. Locally, the same tests SHALL be runnable with one documented command, and SHALL skip when Playwright is absent unless the same "browser tests required" setting the CI job uses is set.

#### Scenario: Browser tests pass
- **WHEN** a pull request targets `main` and every browser regression test passes in Chromium
- **THEN** the `browser-tests` check succeeds

#### Scenario: A browser test fails
- **WHEN** a pull request changes a row action menu so that it opens away from its trigger or outside the viewport
- **THEN** the `browser-tests` check fails and the output identifies the failing test

#### Scenario: Browser tests cannot silently skip in CI
- **WHEN** Playwright or Chromium cannot be imported or launched in the `browser-tests` job
- **THEN** the check fails instead of reporting the browser tests as skipped

#### Scenario: Browser suite is reproducible locally
- **WHEN** a developer runs the documented browser-test command with Playwright and Chromium installed
- **THEN** the same browser tests run as in CI

### Requirement: The Docker smoke test verifies page assets are served

The `docker-smoke-test` check SHALL, in addition to its existing probes, sign in to the running production container as its seeded administrator. It SHALL fetch at least one authenticated page built on the shared page layout, the Environments page and a bookings page, plus the login page. It SHALL request every same-origin script and stylesheet those pages reference, and fail, naming the asset, if any is not served with HTTP 200. The check SHALL also include a negative step: with the row-menu script removed from the running container, both the asset check and the readiness probe fail.

#### Scenario: A referenced script is missing from the image
- **WHEN** the shared page layout references `/static/js/<name>.js` and the production image does not contain that file
- **THEN** the `docker-smoke-test` check fails and names the missing asset

#### Scenario: Assets of authenticated pages are checked
- **WHEN** the smoke test runs against a correctly built image
- **THEN** it has requested every same-origin script and stylesheet referenced by the authenticated Environments page, a bookings page and the login page, and each returned HTTP 200

#### Scenario: Negative check detects a removed row-menu script
- **WHEN** the smoke test removes the row-menu script from the running container and re-runs the asset check and readiness probe
- **THEN** both fail and name the removed script, and the smoke test treats that as the expected outcome

#### Scenario: All referenced assets are served
- **WHEN** every script and stylesheet referenced by the checked pages is present in the image
- **THEN** the asset step of the `docker-smoke-test` check succeeds
