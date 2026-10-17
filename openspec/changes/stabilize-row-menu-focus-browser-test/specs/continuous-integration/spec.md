## ADDED Requirements

### Requirement: The browser regression suite is deterministic

The browser regression suite SHALL synchronize with the page under test by waiting for observable rendered state — an element being open and visible, focus being where an interaction requires it — before asserting on that state, and SHALL NOT use fixed delays as the means of making an assertion pass. A test that fails only intermittently on unchanged behavior SHALL be treated as a suite defect: it SHALL be repaired by correcting its synchronization or by fixing the product defect it exposes, and SHALL NOT be masked by disabling the test, weakening its assertions, or retrying it unconditionally.

#### Scenario: Menu opened by keyboard is asserted only once visible
- **WHEN** a browser test opens a row action menu via the keyboard and then interacts with a control inside it
- **THEN** the interaction and assertions happen only after the menu is both open and visible — the same readiness condition the click-opened path applies — so controller-held invisibility cannot silently swallow a focus or input action

#### Scenario: Assertions on focus are condition-based
- **WHEN** a browser test asserts where focus landed after an interaction
- **THEN** the assertion waits for the focus condition to hold rather than sampling the focused element exactly once immediately after the interaction

#### Scenario: An intermittently failing test is repaired, not masked
- **WHEN** a browser test fails intermittently on pull requests that do not change the behavior it covers
- **THEN** the repair keeps every existing assertion and scenario coverage, removes the race rather than papering over it with fixed delays, and the test remains enabled and selected by the required-mode suite

#### Scenario: A flakiness repair is proven stable before merging
- **WHEN** an intermittently failing browser test is repaired
- **THEN** the repair ships only after the repaired test passes at least 50 consecutive Chromium runs and the full browser suite passes in required mode
